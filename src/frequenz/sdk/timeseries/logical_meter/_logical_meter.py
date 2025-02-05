# License: MIT
# Copyright © 2022 Frequenz Energy-as-a-Service GmbH

"""A logical meter for calculating high level metrics for a microgrid."""


import uuid

from frequenz.channels import Sender

from ...actor import ChannelRegistry, ComponentMetricRequest
from ...microgrid.component import ComponentMetricId
from .._quantities import Current, Power, Quantity
from ..formula_engine import FormulaEngine, FormulaEngine3Phase
from ..formula_engine._formula_engine_pool import FormulaEnginePool
from ..formula_engine._formula_generators import (
    CHPPowerFormula,
    ConsumerPowerFormula,
    GridCurrentFormula,
    GridPowerFormula,
    ProducerPowerFormula,
    PVPowerFormula,
)


class LogicalMeter:
    """A logical meter for calculating high level metrics in a microgrid.

    LogicalMeter provides methods for fetching power values from different points in the
    microgrid.  These methods return `FormulaReceiver` objects, which can be used like
    normal `Receiver`s, but can also be composed to form higher-order formula streams.

    !!! note
        `LogicalMeter` instances are not meant to be created directly by users.  Use the
        [`microgrid.logical_meter`][frequenz.sdk.microgrid.logical_meter] method for
        creating `LogicalMeter` instances.

    Example:
        ```python
        from datetime import timedelta

        from frequenz.sdk import microgrid
        from frequenz.sdk.timeseries import ResamplerConfig

        await microgrid.initialize(
            "127.0.0.1",
            50051,
            ResamplerConfig(resampling_period=timedelta(seconds=1))
        )

        logical_meter = microgrid.logical_meter()

        # Get a receiver for a builtin formula
        grid_power_recv = logical_meter.grid_power.new_receiver()
        async for grid_power_sample in grid_power_recv:
            print(grid_power_sample)

        # or compose formulas to create a new formula
        net_power_recv = (
            (
                logical_meter.grid_power
                - logical_meter.pv_power
            )
            .build("net_power")
            .new_receiver()
        )
        async for net_power_sample in net_power_recv:
            print(net_power_sample)
        ```
    """

    def __init__(
        self,
        channel_registry: ChannelRegistry,
        resampler_subscription_sender: Sender[ComponentMetricRequest],
    ) -> None:
        """Create a `LogicalMeter` instance.

        !!! note
            `LogicalMeter` instances are not meant to be created directly by users.  Use
            the [`microgrid.logical_meter`][frequenz.sdk.microgrid.logical_meter] method
            for creating `LogicalMeter` instances.

        Args:
            channel_registry: A channel registry instance shared with the resampling
                actor.
            resampler_subscription_sender: A sender for sending metric requests to the
                resampling actor.
        """
        self._channel_registry = channel_registry
        self._resampler_subscription_sender = resampler_subscription_sender

        # Use a randomly generated uuid to create a unique namespace name for the local
        # meter to use when communicating with the resampling actor.
        self._namespace = f"logical-meter-{uuid.uuid4()}"
        self._formula_pool = FormulaEnginePool(
            self._namespace,
            self._channel_registry,
            self._resampler_subscription_sender,
        )

    def start_formula(
        self,
        formula: str,
        component_metric_id: ComponentMetricId,
        *,
        nones_are_zeros: bool = False,
    ) -> FormulaEngine[Quantity]:
        """Start execution of the given formula.

        Formulas can have Component IDs that are preceeded by a pound symbol("#"), and
        these operators: +, -, *, /, (, ).

        For example, the input string: "#20 + #5" is a formula for adding metrics from
        two components with ids 20 and 5.

        Args:
            formula: formula to execute.
            component_metric_id: The metric ID to use when fetching receivers from the
                resampling actor.
            nones_are_zeros: Whether to treat None values from the stream as 0s.  If
                False, the returned value will be a None.

        Returns:
            A FormulaEngine that applies the formula and streams values.
        """
        return self._formula_pool.from_string(
            formula, component_metric_id, nones_are_zeros=nones_are_zeros
        )

    @property
    def grid_power(self) -> FormulaEngine[Power]:
        """Fetch the grid power for the microgrid.

        This formula produces values that are in the Passive Sign Convention (PSC).

        If a formula engine to calculate grid power is not already running, it will be
        started.

        A receiver from the formula engine can be created using the `new_receiver`
        method.

        Returns:
            A FormulaEngine that will calculate and stream grid power.
        """
        engine = self._formula_pool.from_power_formula_generator(
            "grid_power",
            GridPowerFormula,
        )
        assert isinstance(engine, FormulaEngine)
        return engine

    @property
    def grid_current(self) -> FormulaEngine3Phase[Current]:
        """Fetch the grid power for the microgrid.

        This formula produces values that are in the Passive Sign Convention (PSC).

        If a formula engine to calculate grid current is not already running, it will be
        started.

        A receiver from the formula engine can be created using the `new_receiver`
        method.

        Returns:
            A FormulaEngine that will calculate and stream grid current.
        """
        engine = self._formula_pool.from_3_phase_current_formula_generator(
            "grid_current",
            GridCurrentFormula,
        )
        assert isinstance(engine, FormulaEngine3Phase)
        return engine

    @property
    def consumer_power(self) -> FormulaEngine[Power]:
        """Fetch the consumer power for the microgrid.

        Under normal circumstances this is expected to correspond to the gross
        consumption of the site excluding active parts and battery.

        This formula produces values that are in the Passive Sign Convention (PSC).

        If a formula engine to calculate consumer power is not already running, it will
        be started.

        A receiver from the formula engine can be created using the `new_receiver`
        method.

        Returns:
            A FormulaEngine that will calculate and stream consumer power.
        """
        engine = self._formula_pool.from_power_formula_generator(
            "consumer_power",
            ConsumerPowerFormula,
        )
        assert isinstance(engine, FormulaEngine)
        return engine

    @property
    def producer_power(self) -> FormulaEngine[Power]:
        """Fetch the producer power for the microgrid.

        Under normal circumstances this is expected to correspond to the production
        of the sites active parts excluding ev chargers and batteries.

        This formula produces values that are in the Passive Sign Convention (PSC).

        If a formula engine to calculate producer power is not already running, it will
        be started.

        A receiver from the formula engine can be created using the `new_receiver`
        method.

        Returns:
            A FormulaEngine that will calculate and stream producer power.
        """
        engine = self._formula_pool.from_power_formula_generator(
            "producer_power",
            ProducerPowerFormula,
        )
        assert isinstance(engine, FormulaEngine)
        return engine

    @property
    def pv_power(self) -> FormulaEngine[Power]:
        """Fetch the PV power in the microgrid.

        This formula produces values that are in the Passive Sign Convention (PSC).

        If a formula engine to calculate PV power is not already running, it will be
        started.

        A receiver from the formula engine can be created using the `new_receiver`
        method.

        Returns:
            A FormulaEngine that will calculate and stream PV total power.
        """
        engine = self._formula_pool.from_power_formula_generator(
            "pv_power",
            PVPowerFormula,
        )
        assert isinstance(engine, FormulaEngine)
        return engine

    @property
    def chp_power(self) -> FormulaEngine[Power]:
        """Fetch the CHP power production in the microgrid.

        This formula produces values that are in the Passive Sign Convention (PSC).

        If a formula engine to calculate CHP power production is not already running, it
        will be started.

        A receiver from the formula engine can be created using the `new_receiver`
        method.

        Returns:
            A FormulaEngine that will calculate and stream CHP power production.
        """
        engine = self._formula_pool.from_power_formula_generator(
            "chp_power",
            CHPPowerFormula,
        )
        assert isinstance(engine, FormulaEngine)
        return engine

    async def stop(self) -> None:
        """Stop all formula engines."""
        await self._formula_pool.stop()
