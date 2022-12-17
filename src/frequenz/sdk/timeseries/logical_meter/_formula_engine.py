# License: MIT
# Copyright © 2022 Frequenz Energy-as-a-Service GmbH

"""A formula engine that can apply formulas on streaming data."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from frequenz.channels import Broadcast, Receiver

from .. import Sample
from ._formula_steps import (
    Adder,
    Averager,
    Divider,
    FormulaStep,
    MetricFetcher,
    Multiplier,
    OpenParen,
    Subtractor,
)

logger = logging.Logger(__name__)

_operator_precedence = {
    "(": 0,
    "/": 1,
    "*": 2,
    "-": 3,
    "+": 4,
    ")": 5,
}


class FormulaEngine:
    """A post-fix formula engine that operates on `Sample` receivers.

    Use the `FormulaBuilder` to create `FormulaEngine` instances.
    """

    def __init__(
        self,
        name: str,
        steps: List[FormulaStep],
        metric_fetchers: Dict[str, MetricFetcher],
    ) -> None:
        """Create a `FormulaEngine` instance.

        Args:
            name: A name for the formula.
            steps: Steps for the engine to execute, in post-fix order.
            metric_fetchers: Fetchers for each metric stream the formula depends on.
        """
        self._name = name
        self._steps = steps
        self._metric_fetchers = metric_fetchers
        self._first_run = True
        self._channel = Broadcast[Sample](self._name)
        self._task = None

    async def _synchronize_metric_timestamps(
        self, metrics: Set[asyncio.Task[Optional[Sample]]]
    ) -> datetime:
        """Synchronize the metric streams.

        For synchronised streams like data from the `ComponentMetricsResamplingActor`,
        this a call to this function is required only once, before the first set of
        inputs are fetched.

        Args:
            metrics: The finished tasks from the first `fetch_next` calls to all the
                `MetricFetcher`s.

        Returns:
            The timestamp of the latest metric value.

        Raises:
            RuntimeError: when some streams have no value, or when the synchronization
                of timestamps fails.
        """
        metrics_by_ts: Dict[datetime, str] = {}
        for metric in metrics:
            result = metric.result()
            name = metric.get_name()
            if result is None:
                raise RuntimeError(f"Stream closed for component: {name}")
            metrics_by_ts[result.timestamp] = name
        latest_ts = max(metrics_by_ts)

        # fetch the metrics with non-latest timestamps again until we have the values
        # for the same ts for all metrics.
        for metric_ts, name in metrics_by_ts.items():
            if metric_ts == latest_ts:
                continue
            fetcher = self._metric_fetchers[name]
            while metric_ts < latest_ts:
                next_val = await fetcher.fetch_next()
                assert next_val is not None
                metric_ts = next_val.timestamp
            if metric_ts > latest_ts:
                raise RuntimeError(
                    "Unable to synchronize resampled metric timestamps, "
                    f"for formula: {self._name}"
                )
        self._first_run = False
        return latest_ts

    async def _apply(self) -> Sample:
        """Fetch the latest metrics, apply the formula once and return the result.

        Returns:
            The result of the formula.

        Raises:
            RuntimeError: if some samples didn't arrive, or if formula application
                failed.
        """
        eval_stack: List[Optional[float]] = []
        ready_metrics, pending = await asyncio.wait(
            [
                asyncio.create_task(fetcher.fetch_next(), name=name)
                for name, fetcher in self._metric_fetchers.items()
            ],
            return_when=asyncio.ALL_COMPLETED,
        )

        if pending or any(res.result() is None for res in iter(ready_metrics)):
            raise RuntimeError(
                f"Some resampled metrics didn't arrive, for formula: {self._name}"
            )

        if self._first_run:
            metric_ts = await self._synchronize_metric_timestamps(ready_metrics)
        else:
            res = next(iter(ready_metrics)).result()
            assert res is not None
            metric_ts = res.timestamp

        for step in self._steps:
            step.apply(eval_stack)

        # if all steps were applied and the formula was correct, there should only be a
        # single value in the evaluation stack, and that would be the formula result.
        if len(eval_stack) != 1:
            raise RuntimeError(f"Formula application failed: {self._name}")

        return Sample(metric_ts, eval_stack[0])

    async def _run(self) -> None:
        sender = self._channel.new_sender()
        while True:
            try:
                msg = await self._apply()
            except asyncio.CancelledError:
                logger.exception("FormulaEngine task cancelled: %s", self._name)
                break
            except Exception as err:  # pylint: disable=broad-except
                logger.warning(
                    "Formula application failed: %s. Error: %s", self._name, err
                )
            else:
                await sender.send(msg)

    def new_receiver(self) -> Receiver[Sample]:
        """Create a new receiver that streams the output of the formula engine.

        Args:
            name: An optional name for the receiver.
            max_size: The size of the receiver's buffer.

        Returns:
            A receiver that streams output `Sample`s from the formula engine.
        """
        if self._task is None:
            self._task = asyncio.create_task(self._run())

        return self._channel.new_receiver()


class FormulaBuilder:
    """Builds a post-fix formula engine that operates on `Sample` receivers.

    Operators and metrics need to be pushed in in-fix order, and they get rearranged
    into post-fix order.  This is done using the [Shunting yard
    algorithm](https://en.wikipedia.org/wiki/Shunting_yard_algorithm).

    Example:
        To create an engine that adds the latest entries from two receivers, the
        following calls need to be made:

        ```python
        builder = FormulaBuilder()
        builder.push_metric("metric_1", receiver_1)
        builder.push_oper("+")
        builder.push_metric("metric_2", receiver_2)
        engine = builder.build()
        ```

        and then every call to `engine.apply()` would fetch a value from each receiver,
        add the values and return the result.
    """

    def __init__(self, name: str) -> None:
        """Create a `FormulaBuilder` instance.

        Args:
            name: A name for the formula being built.
        """
        self._name = name
        self._build_stack: List[FormulaStep] = []
        self._steps: List[FormulaStep] = []
        self._metric_fetchers: Dict[str, MetricFetcher] = {}

    def push_oper(self, oper: str) -> None:
        """Push an operator into the engine.

        Args:
            oper: One of these strings - "+", "-", "*", "/", "(", ")"
        """
        if self._build_stack and oper != "(":
            op_prec = _operator_precedence[oper]
            while self._build_stack:
                prev_step = self._build_stack[-1]
                if op_prec < _operator_precedence[repr(prev_step)]:
                    break
                if oper == ")" and repr(prev_step) == "(":
                    self._build_stack.pop()
                    break
                if repr(prev_step) == "(":
                    break
                self._steps.append(prev_step)
                self._build_stack.pop()

        if oper == "+":
            self._build_stack.append(Adder())
        elif oper == "-":
            self._build_stack.append(Subtractor())
        elif oper == "*":
            self._build_stack.append(Multiplier())
        elif oper == "/":
            self._build_stack.append(Divider())
        elif oper == "(":
            self._build_stack.append(OpenParen())

    def push_metric(
        self,
        name: str,
        data_stream: Receiver[Sample],
        nones_are_zeros: bool,
    ) -> None:
        """Push a metric receiver into the engine.

        Args:
            name: A name for the metric.
            data_stream: A receiver to fetch this metric from.
            nones_are_zeros: Whether to treat None values from the stream as 0s.  If
                False, the returned value will be a None.
        """
        fetcher = self._metric_fetchers.setdefault(
            name, MetricFetcher(name, data_stream, nones_are_zeros)
        )
        self._steps.append(fetcher)

    def push_average(self, metrics: List[Tuple[str, Receiver[Sample], bool]]) -> None:
        """Push an average calculator into the engine.

        Args:
            metrics: list of arguments to pass to each `MetricFetcher`.
        """
        fetchers: List[MetricFetcher] = []
        for metric in metrics:
            fetcher = self._metric_fetchers.setdefault(
                metric[0], MetricFetcher(*metric)
            )
            fetchers.append(fetcher)
        self._steps.append(Averager(fetchers))

    def build(self) -> FormulaEngine:
        """Finalize and build the formula engine.

        Returns:
            A `FormulaEngine` instance.
        """
        while self._build_stack:
            self._steps.append(self._build_stack.pop())

        return FormulaEngine(self._name, self._steps, self._metric_fetchers)
