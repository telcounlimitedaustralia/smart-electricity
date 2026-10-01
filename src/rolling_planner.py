"""Pure seven-day battery planning simulator.

This module deliberately has no FoxESS write code and no database writes.  It
turns hourly solar/load forecasts plus a candidate plan into an auditable energy
and economics result.  It is the test bed for planner decisions before any
control integration is considered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping, Sequence

from tariff import export_rate, import_rate


RateFn = Callable[[datetime], tuple[float, str]]


@dataclass(frozen=True)
class PlannerConfig:
    battery_kwh: float = 42.0
    absolute_min_soc: float = 10.0
    safety_buffer_soc: float = 5.0
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    max_grid_charge_kw: float = 10.0
    max_export_kw: float = 10.0
    cheap_charge_start_hour: int = 10
    cheap_charge_end_hour: int = 14
    premium_export_start_hour: int = 17
    premium_export_end_hour: int = 21
    degradation_cost_cents_per_battery_kwh: float = 0.0
    solar_start_threshold_kwh: float = 0.20
    action_step_kwh: float = 5.0

    @property
    def absolute_min_kwh(self) -> float:
        return self.battery_kwh * self.absolute_min_soc / 100.0

    @property
    def planning_floor_kwh(self) -> float:
        return self.absolute_min_kwh + (
            self.battery_kwh * self.safety_buffer_soc / 100.0
        )


@dataclass(frozen=True)
class DailyAction:
    """AC energy requested in the two controllable tariff windows."""

    grid_charge_kwh: float = 0.0
    premium_export_kwh: float = 0.0


@dataclass
class SimulationResult:
    daily: dict[str, dict] = field(default_factory=dict)
    import_cost: float = 0.0
    export_revenue: float = 0.0
    degradation_cost: float = 0.0
    grid_import_kwh: float = 0.0
    grid_charge_kwh: float = 0.0
    premium_export_kwh: float = 0.0
    normal_export_kwh: float = 0.0
    battery_throughput_kwh: float = 0.0
    end_energy_kwh: float = 0.0

    @property
    def net_value(self) -> float:
        return self.export_revenue - self.import_cost - self.degradation_cost


def _days(hours: Sequence[Mapping]) -> list[str]:
    return list(dict.fromkeys(str(item["day"]) for item in hours))


def effective_solar_start(
    hours: Sequence[Mapping],
    day: str,
    threshold_kwh: float = 0.20,
) -> int | None:
    """First forecast hour with meaningful solar; never assume a fixed 07:30."""

    for item in hours:
        if str(item["day"]) != day:
            continue
        if float(item.get("solar_kwh", 0.0)) >= threshold_kwh:
            return int(item["hour"])
    return None


def overnight_requirement_kwh(
    hours: Sequence[Mapping],
    day: str,
    config: PlannerConfig = PlannerConfig(),
) -> float:
    """Forecast battery energy needed from 21:00 to next useful solar."""

    ordered_days = _days(hours)
    try:
        next_day = ordered_days[ordered_days.index(day) + 1]
    except (ValueError, IndexError):
        return 0.0

    solar_start = effective_solar_start(
        hours, next_day, config.solar_start_threshold_kwh
    )
    requirement_ac = 0.0
    for item in hours:
        item_day = str(item["day"])
        hour = int(item["hour"])
        include = item_day == day and hour >= config.premium_export_end_hour
        include = include or (
            item_day == next_day and (solar_start is None or hour < solar_start)
        )
        if include:
            requirement_ac += max(
                0.0,
                float(item.get("load_kwh", 0.0))
                - float(item.get("solar_kwh", 0.0)),
            )
    return requirement_ac / config.discharge_efficiency


def _new_day(energy: float) -> dict:
    return {
        "start_energy_kwh": energy,
        "solar_kwh": 0.0,
        "load_kwh": 0.0,
        "grid_import_kwh": 0.0,
        "pre_cheap_import_kwh": 0.0,
        "grid_charge_kwh": 0.0,
        "premium_export_kwh": 0.0,
        "normal_export_kwh": 0.0,
        "import_cost": 0.0,
        "export_revenue": 0.0,
        "battery_throughput_kwh": 0.0,
        "min_energy_kwh": energy,
        "end_energy_kwh": energy,
    }


def simulate(
    hours: Sequence[Mapping],
    start_energy_kwh: float,
    actions: Mapping[str, DailyAction] | None = None,
    config: PlannerConfig = PlannerConfig(),
    import_rate_fn: RateFn = import_rate,
    export_rate_fn: RateFn = export_rate,
) -> SimulationResult:
    """Simulate solar, load, optional cheap charge, and premium export hourly."""

    if not config.absolute_min_kwh <= start_energy_kwh <= config.battery_kwh:
        raise ValueError("start_energy_kwh is outside the physical battery range")

    actions = actions or {}
    result = SimulationResult(end_energy_kwh=float(start_energy_kwh))
    energy = float(start_energy_kwh)

    for item in hours:
        ts = datetime.fromisoformat(str(item["timestamp"]))
        day = str(item["day"])
        hour = int(item["hour"])
        solar = max(0.0, float(item.get("solar_kwh", 0.0)))
        load = max(0.0, float(item.get("load_kwh", 0.0)))
        action = actions.get(day, DailyAction())
        daily = result.daily.setdefault(day, _new_day(energy))
        daily["solar_kwh"] += solar
        daily["load_kwh"] += load

        # Solar serves the house first, then charges the battery, then exports.
        natural_export = 0.0
        if solar >= load:
            surplus = solar - load
            stored = min(surplus * config.charge_efficiency, config.battery_kwh - energy)
            energy += stored
            daily["battery_throughput_kwh"] += stored
            result.battery_throughput_kwh += stored
            natural_export = max(0.0, surplus - stored / config.charge_efficiency)
        else:
            deficit = load - solar
            available_ac = max(0.0, energy - config.absolute_min_kwh) * config.discharge_efficiency
            battery_to_house = min(deficit, available_ac)
            battery_draw = battery_to_house / config.discharge_efficiency
            energy -= battery_draw
            daily["battery_throughput_kwh"] += battery_draw
            result.battery_throughput_kwh += battery_draw
            grid_needed = deficit - battery_to_house
            if grid_needed:
                rate, _ = import_rate_fn(ts)
                cost = grid_needed * rate / 100.0
                result.grid_import_kwh += grid_needed
                result.import_cost += cost
                daily["grid_import_kwh"] += grid_needed
                daily["import_cost"] += cost
                if hour < config.cheap_charge_start_hour:
                    daily["pre_cheap_import_kwh"] += grid_needed

        if natural_export:
            rate, _ = export_rate_fn(ts)
            revenue = natural_export * rate / 100.0
            result.normal_export_kwh += natural_export
            result.export_revenue += revenue
            daily["normal_export_kwh"] += natural_export
            daily["export_revenue"] += revenue

        # Charge only in the cheap window and only to the physical capacity.
        if (
            action.grid_charge_kwh > 0
            and config.cheap_charge_start_hour <= hour < config.cheap_charge_end_hour
        ):
            requested = action.grid_charge_kwh / (
                config.cheap_charge_end_hour - config.cheap_charge_start_hour
            )
            requested = min(requested, config.max_grid_charge_kw)
            actual = min(requested, (config.battery_kwh - energy) / config.charge_efficiency)
            if actual > 0:
                stored = actual * config.charge_efficiency
                energy += stored
                rate, _ = import_rate_fn(ts)
                cost = actual * rate / 100.0
                result.grid_import_kwh += actual
                result.grid_charge_kwh += actual
                result.import_cost += cost
                result.battery_throughput_kwh += stored
                daily["grid_import_kwh"] += actual
                daily["grid_charge_kwh"] += actual
                daily["import_cost"] += cost
                daily["battery_throughput_kwh"] += stored

        # Premium battery export shares the site limit with natural solar export.
        if (
            action.premium_export_kwh > 0
            and config.premium_export_start_hour <= hour < config.premium_export_end_hour
        ):
            requested = action.premium_export_kwh / (
                config.premium_export_end_hour - config.premium_export_start_hour
            )
            requested = min(requested, config.max_export_kw - natural_export)
            available_ac = max(0.0, energy - config.absolute_min_kwh) * config.discharge_efficiency
            actual = min(max(0.0, requested), available_ac)
            if actual > 0:
                battery_draw = actual / config.discharge_efficiency
                energy -= battery_draw
                rate, _ = export_rate_fn(ts)
                revenue = actual * rate / 100.0
                result.premium_export_kwh += actual
                result.export_revenue += revenue
                result.battery_throughput_kwh += battery_draw
                daily["premium_export_kwh"] += actual
                daily["export_revenue"] += revenue
                daily["battery_throughput_kwh"] += battery_draw

        energy = min(config.battery_kwh, max(config.absolute_min_kwh, energy))
        daily["min_energy_kwh"] = min(daily["min_energy_kwh"], energy)
        daily["end_energy_kwh"] = energy

    result.end_energy_kwh = energy
    result.degradation_cost = (
        result.battery_throughput_kwh
        * config.degradation_cost_cents_per_battery_kwh
        / 100.0
    )
    return result


def is_safe(
    result: SimulationResult,
    baseline: SimulationResult,
    config: PlannerConfig = PlannerConfig(),
) -> bool:
    """Apply physical, safety-buffer, and no-added-morning-import constraints."""

    for day, daily in result.daily.items():
        if daily["min_energy_kwh"] < config.absolute_min_kwh - 1e-6:
            return False
        if daily["end_energy_kwh"] < config.planning_floor_kwh - 1e-6:
            return False
        baseline_morning = baseline.daily.get(day, {}).get("pre_cheap_import_kwh", 0.0)
        if daily["pre_cheap_import_kwh"] > baseline_morning + 0.05:
            return False
    return True


def score(
    hours: Sequence[Mapping],
    start_energy_kwh: float,
    actions: Mapping[str, DailyAction],
    baseline: SimulationResult,
    config: PlannerConfig = PlannerConfig(),
    import_rate_fn: RateFn = import_rate,
    export_rate_fn: RateFn = export_rate,
) -> tuple[float | None, SimulationResult]:
    result = simulate(hours, start_energy_kwh, actions, config, import_rate_fn, export_rate_fn)
    return (result.net_value if is_safe(result, baseline, config) else None), result


def compare_strategies(
    hours: Sequence[Mapping],
    start_energy_kwh: float,
    strategies: Mapping[str, Mapping[str, DailyAction]],
    config: PlannerConfig = PlannerConfig(),
    import_rate_fn: RateFn = import_rate,
    export_rate_fn: RateFn = export_rate,
) -> dict[str, dict]:
    """Return an apples-to-apples economics comparison for named plans.

    The comparison deliberately exposes each value component instead of using a
    misleading revenue-only or do-nothing headline.  A plan that fails a safety
    constraint is still shown, but is marked ineligible for selection.
    """

    baseline = simulate(
        hours, start_energy_kwh, {}, config, import_rate_fn, export_rate_fn
    )
    comparison = {
        "no_action": {
            "safe": is_safe(baseline, baseline, config),
            "net_value": baseline.net_value,
            "export_revenue": baseline.export_revenue,
            "import_cost": baseline.import_cost,
            "degradation_cost": baseline.degradation_cost,
            "grid_import_kwh": baseline.grid_import_kwh,
            "premium_export_kwh": baseline.premium_export_kwh,
            "incremental_value": 0.0,
        }
    }
    for name, actions in strategies.items():
        result = simulate(
            hours, start_energy_kwh, actions, config, import_rate_fn, export_rate_fn
        )
        comparison[name] = {
            "safe": is_safe(result, baseline, config),
            "net_value": result.net_value,
            "export_revenue": result.export_revenue,
            "import_cost": result.import_cost,
            "degradation_cost": result.degradation_cost,
            "grid_import_kwh": result.grid_import_kwh,
            "premium_export_kwh": result.premium_export_kwh,
            "incremental_value": result.net_value - baseline.net_value,
        }
    return comparison


def optimise(
    hours: Sequence[Mapping],
    start_energy_kwh: float,
    config: PlannerConfig = PlannerConfig(),
    import_rate_fn: RateFn = import_rate,
    export_rate_fn: RateFn = export_rate,
    passes: int = 2,
) -> dict:
    """Jointly search daily cheap-charge and premium-export pairs over the horizon."""

    days = _days(hours)
    baseline = simulate(hours, start_energy_kwh, {}, config, import_rate_fn, export_rate_fn)
    actions = {day: DailyAction() for day in days}
    best_score, best_result = score(
        hours, start_energy_kwh, actions, baseline, config, import_rate_fn, export_rate_fn
    )
    if best_score is None:
        best_score = float("-inf")

    max_charge = config.max_grid_charge_kw * (
        config.cheap_charge_end_hour - config.cheap_charge_start_hour
    )
    max_export = config.max_export_kw * (
        config.premium_export_end_hour - config.premium_export_start_hour
    )
    values = lambda maximum: [
        round(value, 6)
        for value in _frange(0.0, maximum, config.action_step_kwh)
    ]

    for _ in range(passes):
        changed = False
        for day in days:
            current = actions[day]
            local_action, local_score = current, best_score
            for charge in values(max_charge):
                for export in values(max_export):
                    candidate = DailyAction(charge, export)
                    trial = dict(actions)
                    trial[day] = candidate
                    candidate_score, _ = score(
                        hours, start_energy_kwh, trial, baseline, config,
                        import_rate_fn, export_rate_fn,
                    )
                    if candidate_score is not None and candidate_score > local_score + 0.0001:
                        local_action, local_score = candidate, candidate_score
            if local_action != current:
                actions[day] = local_action
                best_score = local_score
                changed = True
        if not changed:
            break

    final_score, final_result = score(
        hours, start_energy_kwh, actions, baseline, config, import_rate_fn, export_rate_fn
    )
    return {
        "actions": actions,
        "baseline": baseline,
        "result": final_result,
        "net_value": final_score,
        "avoided_import_cost": baseline.import_cost - final_result.import_cost,
    }


def _frange(start: float, stop: float, step: float):
    if step <= 0:
        raise ValueError("action_step_kwh must be positive")
    value = start
    while value <= stop + 1e-9:
        yield min(value, stop)
        value += step

