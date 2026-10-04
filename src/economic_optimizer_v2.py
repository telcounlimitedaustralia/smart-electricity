"""
Smart Electricity - Economic Optimiser V2

Multi-day economic optimisation of:
- ML household load
- solar forecast
- 10:05am-1:55pm shoulder grid charging
- 5:05pm-8:55pm premium export
- battery reserve

SHADOW ONLY - NO FOXESS CONTROL.
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from export_planner import (
    calculate as export_calculate,
    forecast,
    connect,
    pv_for_hour,
)

from forward_load_predictor import forecast_days
from tariff import import_rate, export_rate


BATTERY_KWH = 42.0
TZ = ZoneInfo("Australia/Sydney")

MIN_SOC = 10.0
MIN_KWH = BATTERY_KWH * MIN_SOC / 100.0

CHARGE_EFF = 0.95
DISCHARGE_EFF = 0.95

MAX_GRID_CHARGE_KW = 10.0
MAX_EXPORT_KW = 10.0

SHOULDER_START = 10
SHOULDER_END = 14

EXPORT_START = 17
EXPORT_END = 21

ACTION_STEP_KWH = 1.0

# Forecast and tariff inputs remain hourly. The live FoxESS periods leave a
# boundary guard and therefore provide 3h50m of inverter capacity, not 4h.
CONTROL_WINDOW_HOURS = 3.0 + 50.0 / 60.0

# Shadow-planning assumptions. They are deliberately configurable and do not
# affect the existing rule-based executor.
DEGRADATION_COST_CENTS_PER_BATTERY_KWH = float(
    os.getenv("SHADOW_BATTERY_DEGRADATION_CENTS_PER_KWH", "3.0")
)
SOLAR_PROTECTION_FACTOR = float(
    os.getenv("SHADOW_SOLAR_PROTECTION_FACTOR", "1.0")
)
TERMINAL_ENERGY_VALUE_CENTS_PER_KWH = float(
    os.getenv(
        "SHADOW_TERMINAL_ENERGY_VALUE_CENTS_PER_KWH",
        "16.0",
    )
)

# Protect against forecast error.
BASE_RESERVE_KWH = 3.42
RESERVE_INCREMENT_KWH = 0.855

# Extra protection at end of available forecast horizon.
TERMINAL_RESERVE_KWH = 8.0

# Solar is considered useful when it can cover household load and still add a
# meaningful amount to the battery.  The recovery horizon is the earlier of
# this condition or the next 10AM shoulder-price window.
USEFUL_NET_SOLAR_KWH = 0.20


def replay_without_grid_charge(rows, sample_hours=1.0 / 12.0):
    """Reconstruct battery energy while excluding observed grid charging.

    FoxESS live telemetry is sampled every five minutes.  The replay begins
    with the first recorded SOC, then applies only measured PV and household
    load.  Grid energy attributable to battery charging is measured separately
    so a manual charge is never credited to solar in the counterfactual.
    """

    ordered = sorted(rows, key=lambda item: item["timestamp"])
    if not ordered:
        return {
            "available": False,
            "energy": None,
            "grid_charge_ac": 0.0,
            "grid_stored": 0.0,
        }

    energy = max(
        MIN_KWH,
        min(
            BATTERY_KWH,
            BATTERY_KWH * float(ordered[0]["battery_soc"]) / 100.0,
        ),
    )
    grid_charge_ac = 0.0

    for item in ordered:
        pv_kw = max(0.0, float(item.get("pv_kw") or 0.0))
        load_kw = max(0.0, float(item.get("load_kw") or 0.0))
        grid_import_kw = max(
            0.0,
            float(item.get("grid_import_kw") or 0.0),
        )
        battery_charge_kw = max(
            0.0,
            float(item.get("battery_charge_kw") or 0.0),
        )

        net_solar_kw = pv_kw - load_kw
        if net_solar_kw >= 0.0:
            energy = min(
                BATTERY_KWH,
                energy + net_solar_kw * sample_hours * CHARGE_EFF,
            )
        else:
            energy = max(
                MIN_KWH,
                energy + net_solar_kw * sample_hours / DISCHARGE_EFF,
            )

        # Remove household grid demand before attributing any import to the
        # battery.  The battery-charge measurement provides a second cap.
        household_grid_kw = max(0.0, load_kw - pv_kw)
        grid_for_battery_kw = min(
            battery_charge_kw,
            max(0.0, grid_import_kw - household_grid_kw),
        )
        grid_charge_ac += grid_for_battery_kw * sample_hours

    return {
        "available": True,
        "energy": energy,
        "grid_charge_ac": grid_charge_ac,
        "grid_stored": grid_charge_ac * CHARGE_EFF,
        "start_soc": float(ordered[0]["battery_soc"]),
        "samples": len(ordered),
    }


def reserve_for_day(day_index):
    return (
        BASE_RESERVE_KWH
        + max(0, day_index - 1)
        * RESERVE_INCREMENT_KWH
    )


def candidate_values(maximum):
    values = []
    x = 0.0

    while x <= maximum + 0.001:
        values.append(round(x, 2))
        x += ACTION_STEP_KWH

    # Preserve the exact physical limit when it falls between the normal
    # 1 kWh search increments (the guarded 3h50m window caps at 38.33 kWh).
    if not values or values[-1] < maximum - 0.001:
        values.append(round(maximum, 2))

    return values


def load_forecasts(now=None):
    """
    Convert recursive forward ML output into:
        date -> hourly load + uncertainty metadata
    """

    # Economic optimisation must begin with today.
    #
    # This is important just after midnight: today's completed
    # predecessor can come from yesterday's foxess_live data,
    # then future days can be recursively predicted from today.
    now = now or datetime.now(TZ)
    start_day = now.astimezone(TZ).date()

    result = forecast_days(
        start_day=start_day,
        days=7
    )

    forecasts = {}

    for day in result.get("forecast_days", []):

        if not day.get("available"):
            continue

        hourly = {}

        for item in day.get(
            "hourly_predictions",
            []
        ):
            hourly[int(item["hour"])] = float(
                item["load_kwh"]
            )

        if len(hourly) < 20:
            continue

        forecasts[day["date"]] = {
            "hourly": hourly,
            "predicted_load_kwh": float(
                day["predicted_load_kwh"]
            ),
            "safe_load_kwh": float(
                day["safe_load_kwh"]
            ),
            "buffer_kwh": float(
                day["planning_buffer_kwh"]
            ),
            "recursive": bool(
                day.get("recursive", False)
            ),
        }

    return forecasts


def local_timestamp(value):
    """Interpret offset-free forecast timestamps as Sydney local time."""
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=TZ)
    return stamp.astimezone(TZ)


def weather_forecast(now=None):
    c = connect()

    rows = forecast(c)

    c.close()

    # The initial battery energy is a live state. Exclude the current,
    # partially elapsed hour so its solar and load are not simulated twice.
    now = now or datetime.now(TZ)
    current_hour = now.astimezone(TZ).replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    return [
        row for row in rows
        if local_timestamp(row["timestamp"]) > current_hour
    ]


def build_hours(now=None):
    """
    Join solar/weather forecast with hourly ML load.
    """

    now = now or datetime.now(TZ)
    ml = load_forecasts(now=now)
    weather = weather_forecast(now=now)

    hours = []

    for row in weather:

        ts = datetime.fromisoformat(
            row["timestamp"]
        )

        day = ts.date().isoformat()
        hour = ts.hour

        if day not in ml:
            continue

        point_load_kwh = ml[day][
            "hourly"
        ].get(hour)

        if point_load_kwh is None:
            continue

        point_total = max(
            0.001,
            ml[day]["predicted_load_kwh"]
        )
        safe_scale = max(
            1.0,
            ml[day]["safe_load_kwh"] / point_total
        )
        safe_load_kwh = float(point_load_kwh) * safe_scale

        solar_point_kwh = float(
            pv_for_hour(
                row["shortwave_radiation"]
            )
        )

        # Optimise against a conservative solar case while retaining the
        # unadjusted point forecast for transparent dashboard reporting.
        protected_solar_kwh = (
            solar_point_kwh
            * max(0.0, min(1.0, SOLAR_PROTECTION_FACTOR))
        )

        hours.append({
            "timestamp": row["timestamp"],
            "day": day,
            "hour": hour,
            "solar_kwh": protected_solar_kwh,
            "solar_point_kwh": solar_point_kwh,
            "base_load_kwh": float(point_load_kwh),
            "load_kwh": safe_load_kwh,
            "load_scenario": "CALIBRATED_SAFE",
            "solar_scenario": "PROTECTED_DOWNSIDE",
        })

    return hours, ml


def initial_energy():
    """
    Get current battery SOC using the existing planner.
    """

    p = export_calculate()

    if not p.get("available"):
        raise RuntimeError(
            p.get(
                "reason",
                "Existing planner unavailable"
            )
        )

    soc = float(p["current_soc"])

    energy = (
        BATTERY_KWH
        * soc
        / 100.0
    )

    return energy, soc


def _project_solar_only_to_5pm(day_hours, start_energy):
    """Battery energy at 5PM with forecast solar/load and no grid charging."""

    energy = float(start_energy)

    for item in day_hours:
        if int(item["hour"]) >= EXPORT_START:
            break

        solar = max(0.0, float(item["solar_kwh"]))
        load = max(0.0, float(item["load_kwh"]))

        if solar >= load:
            surplus = solar - load
            energy += min(
                surplus * CHARGE_EFF,
                max(0.0, BATTERY_KWH - energy),
            )
        else:
            deficit = load - solar
            usable = max(0.0, energy - MIN_KWH)
            battery_draw = min(
                deficit / DISCHARGE_EFF if DISCHARGE_EFF > 0 else 0.0,
                usable,
            )
            energy -= battery_draw

        energy = min(BATTERY_KWH, max(MIN_KWH, energy))

    return energy


def _recovery_requirement(hours, day):
    """Energy required at 9PM until useful solar or next 10AM shoulder."""

    requirement_draw = 0.0
    recovery_timestamp = None
    recovery_type = "FORECAST_HORIZON"
    after_export = False

    for item in hours:
        item_day = item["day"]
        hour = int(item["hour"])

        if item_day == day and hour >= EXPORT_END:
            after_export = True
        elif item_day > day:
            after_export = True

        if not after_export:
            continue

        if item_day > day:
            net_solar = max(
                0.0,
                float(item["solar_kwh"]) - float(item["load_kwh"]),
            )

            if hour >= SHOULDER_START:
                recovery_timestamp = item["timestamp"]
                recovery_type = "SHOULDER_10AM"
                break

            if net_solar >= USEFUL_NET_SOLAR_KWH:
                recovery_timestamp = item["timestamp"]
                recovery_type = "USEFUL_SOLAR"
                break

        deficit = max(
            0.0,
            float(item["load_kwh"]) - float(item["solar_kwh"]),
        )
        requirement_draw += (
            deficit / DISCHARGE_EFF
            if DISCHARGE_EFF > 0
            else 0.0
        )

    required_energy = min(
        BATTERY_KWH,
        MIN_KWH + requirement_draw,
    )

    return {
        "required_energy": required_energy,
        "forecast_draw": requirement_draw,
        "recovery_timestamp": recovery_timestamp,
        "recovery_type": recovery_type,
    }


def build_simulation_context(hours):
    """Pre-compute day groupings and recovery requirements for fast search."""

    by_day = {}
    for item in hours:
        by_day.setdefault(item["day"], []).append(item)

    return {
        "hours_by_day": by_day,
        "solar_only_cache": {},
        "recovery": {
            day: _recovery_requirement(hours, day)
            for day in by_day
        },
    }


def solar_only_5pm_energy(context, day, start_energy):
    key = (day, round(float(start_energy), 4))
    cache = context["solar_only_cache"]

    if key not in cache:
        cache[key] = _project_solar_only_to_5pm(
            context["hours_by_day"].get(day, []),
            start_energy,
        )

    return cache[key]


def simulate(
    hours,
    start_energy,
    charges=None,
    exports=None,
    context=None,
):
    """
    Multi-day battery/economic simulation.

    charges:
        {date: requested grid-charge kWh}

    exports:
        {date: requested premium-export kWh}
    """

    charges = charges or {}
    exports = exports or {}
    context = context or build_simulation_context(hours)

    energy = float(start_energy)

    import_cost = 0.0
    export_revenue = 0.0

    total_grid_import = 0.0
    total_grid_export = 0.0
    total_grid_charge = 0.0
    battery_throughput = 0.0

    daily = {}

    for item in hours:

        ts = datetime.fromisoformat(
            item["timestamp"]
        )

        day = item["day"]
        hour = item["hour"]

        if day not in daily:

            solar_only_5pm = solar_only_5pm_energy(
                context,
                day,
                energy,
            )
            recovery = context["recovery"].get(
                day,
                {
                    "required_energy": MIN_KWH,
                    "forecast_draw": 0.0,
                    "recovery_timestamp": None,
                    "recovery_type": "FORECAST_HORIZON",
                },
            )
            stored_shortfall = max(
                0.0,
                BATTERY_KWH - solar_only_5pm,
            )
            charge_cap_ac = (
                stored_shortfall / CHARGE_EFF
                if CHARGE_EFF > 0
                else 0.0
            )

            daily[day] = {
                "start_energy": energy,
                "solar_only_5pm_energy": solar_only_5pm,
                "solar_only_5pm_soc": solar_only_5pm / BATTERY_KWH * 100.0,
                "grid_charge_cap_ac": charge_cap_ac,
                "required_post_export_energy": recovery["required_energy"],
                "required_post_export_soc": (
                    recovery["required_energy"] / BATTERY_KWH * 100.0
                ),
                "forecast_draw_to_recovery": recovery["forecast_draw"],
                "next_recharge_timestamp": recovery["recovery_timestamp"],
                "next_recharge_type": recovery["recovery_type"],
                "solar": 0.0,
                "solar_point": 0.0,
                "load": 0.0,
                "base_load": 0.0,
                "grid_import": 0.0,
                "pre_10am_grid_import": 0.0,
                "grid_charge": 0.0,
                "grid_stored": 0.0,
                "natural_export": 0.0,
                "premium_export": 0.0,
                "premium_export_battery_draw": 0.0,
                "battery_to_home_draw": 0.0,
                "battery_throughput": 0.0,
                "degradation_cost": 0.0,
                "import_cost": 0.0,
                "export_revenue": 0.0,
                "pre_shoulder_energy": None,
                "post_shoulder_energy": None,
                "pre_export_energy": None,
                "post_export_energy": None,
                "min_energy": energy,
                "end_energy": energy,
            }

        d = daily[day]

        # Track solar export during this hour so forced
        # battery export shares the same 10 kW site limit.
        natural_export_this_hour = 0.0

        solar = max(
            0.0,
            float(item["solar_kwh"])
        )

        load = max(
            0.0,
            float(item["load_kwh"])
        )

        d["solar"] += solar
        d["solar_point"] += max(
            0.0,
            float(item.get("solar_point_kwh", solar))
        )
        d["load"] += load
        d["base_load"] += max(
            0.0,
            float(item.get("base_load_kwh", load))
        )

        # Capture battery state immediately before
        # the cheap 10AM shoulder charging window.
        if hour == SHOULDER_START:
            d["pre_shoulder_energy"] = energy

        # ==========================================
        # SOLAR + HOUSEHOLD LOAD
        # ==========================================

        if solar >= load:

            surplus = solar - load

            room = max(
                0.0,
                BATTERY_KWH - energy
            )

            stored = min(
                surplus * CHARGE_EFF,
                room
            )

            energy += stored
            battery_throughput += stored
            d["battery_throughput"] += stored

            solar_used_for_charge = (
                stored / CHARGE_EFF
                if CHARGE_EFF > 0
                else 0.0
            )

            natural_export = max(
                0.0,
                surplus
                - solar_used_for_charge
            )

            if natural_export > 0:

                natural_export_this_hour = (
                    natural_export
                )

                rate, _ = export_rate(ts)

                revenue = (
                    natural_export
                    * rate
                    / 100.0
                )

                export_revenue += revenue
                total_grid_export += (
                    natural_export
                )

                d["natural_export"] += (
                    natural_export
                )

                d["export_revenue"] += (
                    revenue
                )

        else:

            deficit = load - solar

            usable_battery = max(
                0.0,
                energy - MIN_KWH
            )

            ac_available = (
                usable_battery
                * DISCHARGE_EFF
            )

            battery_to_load = min(
                deficit,
                ac_available
            )

            battery_draw = (
                battery_to_load
                / DISCHARGE_EFF
                if DISCHARGE_EFF > 0
                else 0.0
            )

            energy -= battery_draw
            battery_throughput += battery_draw
            d["battery_throughput"] += battery_draw
            d["battery_to_home_draw"] += battery_draw

            grid_needed = max(
                0.0,
                deficit - battery_to_load
            )

            if grid_needed > 0:

                rate, _ = import_rate(ts)

                cost = (
                    grid_needed
                    * rate
                    / 100.0
                )

                import_cost += cost
                total_grid_import += (
                    grid_needed
                )

                d["grid_import"] += (
                    grid_needed
                )

                if hour < SHOULDER_START:
                    d["pre_10am_grid_import"] += (
                        grid_needed
                    )

                d["import_cost"] += cost

        # ==========================================
        # 10AM-2PM SHOULDER GRID CHARGING
        # ==========================================

        requested_charge = float(
            charges.get(day, 0.0)
        )

        if (
            requested_charge > 0
            and SHOULDER_START
            <= hour
            < SHOULDER_END
        ):

            # Spread requested AC grid energy evenly
            # across the four-hour shoulder window.
            hourly_request = (
                requested_charge
                / (
                    SHOULDER_END
                    - SHOULDER_START
                )
            )

            # Respect inverter charging limit.
            hourly_request = min(
                hourly_request,
                MAX_GRID_CHARGE_KW
            )

            room = max(
                0.0,
                BATTERY_KWH - energy
            )

            # AC energy required to fill remaining
            # battery room after charging losses.
            max_ac_for_room = (
                room / CHARGE_EFF
                if CHARGE_EFF > 0
                else 0.0
            )

            actual_charge = min(
                hourly_request,
                max_ac_for_room,
                max(
                    0.0,
                    d["grid_charge_cap_ac"] - d["grid_charge"],
                ),
            )

            stored = (
                actual_charge
                * CHARGE_EFF
            )

            energy += stored
            battery_throughput += stored
            d["battery_throughput"] += stored
            d["grid_stored"] += stored

            if actual_charge > 0:

                rate, period = import_rate(ts)

                cost = (
                    actual_charge
                    * rate
                    / 100.0
                )

                import_cost += cost
                total_grid_import += (
                    actual_charge
                )
                total_grid_charge += (
                    actual_charge
                )

                d["grid_import"] += (
                    actual_charge
                )
                d["grid_charge"] += (
                    actual_charge
                )
                d["import_cost"] += cost

        # State at the end of the final cheap-charge hour.
        if hour == SHOULDER_END - 1:
            d["post_shoulder_energy"] = energy

        # Capture battery state entering the
        # premium export period.
        if hour == EXPORT_START:

            d["pre_export_energy"] = (
                energy
            )

        # ==========================================
        # 5PM-9PM PREMIUM BATTERY EXPORT
        # ==========================================

        requested_export = float(
            exports.get(day, 0.0)
        )

        if (
            requested_export > 0
            and EXPORT_START
            <= hour
            < EXPORT_END
        ):

            # Spread requested AC export evenly
            # across the four-hour premium window.
            hourly_request = (
                requested_export
                / (
                    EXPORT_END
                    - EXPORT_START
                )
            )

            # Solar export and forced battery export
            # share the same 10 kW site export capacity.
            remaining_export_capacity = max(
                0.0,
                MAX_EXPORT_KW
                - natural_export_this_hour
            )

            hourly_request = min(
                hourly_request,
                remaining_export_capacity
            )

            # Never deliberately discharge below
            # the absolute 10% battery floor.
            usable_battery = max(
                0.0,
                energy - MIN_KWH
            )

            # Account for battery discharge loss.
            max_ac_export = (
                usable_battery
                * DISCHARGE_EFF
            )

            actual_export = min(
                hourly_request,
                max_ac_export
            )

            battery_draw = (
                actual_export
                / DISCHARGE_EFF
                if DISCHARGE_EFF > 0
                else 0.0
            )

            energy -= battery_draw
            battery_throughput += battery_draw
            d["battery_throughput"] += battery_draw
            d["premium_export_battery_draw"] += battery_draw

            if actual_export > 0:

                rate, period = export_rate(ts)

                revenue = (
                    actual_export
                    * rate
                    / 100.0
                )

                export_revenue += revenue
                total_grid_export += (
                    actual_export
                )

                d["premium_export"] += (
                    actual_export
                )

                d["export_revenue"] += (
                    revenue
                )

        # State after the final premium-export hour.  This must retain enough
        # battery for forecast household demand until useful solar or the next
        # cheap 10AM shoulder window, plus the absolute FoxESS floor.
        if hour == EXPORT_END - 1:
            d["post_export_energy"] = energy

        # ==========================================
        # HOURLY SAFETY / DAILY STATE
        # ==========================================

        energy = min(
            BATTERY_KWH,
            max(MIN_KWH, energy)
        )

        d["min_energy"] = min(
            d["min_energy"],
            energy
        )

        d["end_energy"] = energy

    degradation_cost = (
        battery_throughput
        * DEGRADATION_COST_CENTS_PER_BATTERY_KWH
        / 100.0
    )
    for d in daily.values():
        d["degradation_cost"] = (
            d["battery_throughput"]
            * DEGRADATION_COST_CENTS_PER_BATTERY_KWH
            / 100.0
        )

    cash_value = export_revenue - import_cost - degradation_cost
    terminal_value = (
        max(0.0, energy - MIN_KWH)
        * TERMINAL_ENERGY_VALUE_CENTS_PER_KWH
        / 100.0
    )

    return {
        "import_cost": import_cost,
        "export_revenue": export_revenue,
        "degradation_cost": degradation_cost,
        "battery_throughput": battery_throughput,

        "net_value": cash_value,
        "terminal_value": terminal_value,
        "planning_value": cash_value + terminal_value,

        "grid_import": total_grid_import,
        "grid_export": total_grid_export,
        "grid_charge": total_grid_charge,

        "end_energy": energy,
        "daily": daily,
    }


def horizon_days(hours):
    """Return forecast dates in simulation order."""

    days = []

    for item in hours:
        day = item["day"]

        if day not in days:
            days.append(day)

    return days


def result_is_safe(
    result,
    days,
    baseline_pre10=None,
):
    """
    Validate battery reserves across the entire horizon.

    Physical 10% SOC is already enforced by simulate().
    This adds forecast-error reserves above that minimum.
    """

    for index, day in enumerate(
        days,
        start=1,
    ):

        d = result["daily"].get(day)

        if not d:
            return False

        # Do not allow optimisation decisions to create
        # additional expensive pre-10AM grid imports.
        #
        # If the no-action baseline already requires some
        # morning import, that unavoidable amount is allowed.
        baseline_morning = 0.0

        if baseline_pre10 is not None:
            baseline_morning = float(
                baseline_pre10.get(day, 0.0)
            )

        candidate_morning = float(
            d.get(
                "pre_10am_grid_import",
                0.0
            )
        )

        if (
            candidate_morning
            > baseline_morning + 0.05
        ):
            return False

        # Forced export must leave the forecast energy needed from 9PM until
        # the earlier of useful solar or next 10AM shoulder charging, including
        # the absolute 10% FoxESS floor.  A no-export baseline is still allowed
        # when the starting conditions make that target physically impossible.
        post_export_energy = d.get("post_export_energy")
        required_post_export = float(
            d.get("required_post_export_energy", MIN_KWH)
        )
        if (
            float(d.get("premium_export", 0.0)) > 0.05
            and post_export_energy is not None
            and post_export_energy < required_post_export - 0.05
        ):
            return False

    # Extra protection at the forecast horizon edge.
    terminal_floor = max(
        MIN_KWH
        + reserve_for_day(len(days)),
        MIN_KWH
        + TERMINAL_RESERVE_KWH,
    )

    if result["end_energy"] < terminal_floor:
        return False

    return True


def score_strategy(
    hours,
    start_energy,
    days,
    charges,
    exports,
    baseline_pre10=None,
    context=None,
):
    """
    Score one complete multi-day strategy.

    Higher score = greater forecast economic value.
    """

    result = simulate(
        hours,
        start_energy,
        charges=charges,
        exports=exports,
        context=context,
    )

    if not result_is_safe(
        result,
        days,
        baseline_pre10=baseline_pre10,
    ):
        return None, result

    # The last forecast day must not be rewarded for emptying the battery just
    # beyond the visible horizon. Retained usable energy receives a
    # conservative, configurable fraction of its future avoided-import value
    # for selection only; cash value stays separately visible in the dashboard.
    return result["planning_value"], result


def optimise_export_only_baseline(
    hours,
    start_energy,
    days,
    baseline_pre10,
    context,
    max_passes=2,
):
    """Return a fair, safe export-planner style baseline.

    The old comparison was a no-action battery, which made the economic
    optimiser's improvement look much larger than a real alternative.  This
    baseline uses the same forecasts, tariffs, efficiencies and constraints
    as the optimiser, but permits premium export only (no cheap grid charge).
    """

    charges = {day: 0.0 for day in days}
    exports = {day: 0.0 for day in days}
    score, result = score_strategy(
        hours,
        start_energy,
        days,
        charges,
        exports,
        baseline_pre10=baseline_pre10,
        context=context,
    )

    if score is None:
        raise RuntimeError("No-action strategy fails reserve constraints")

    export_options = candidate_values(
        MAX_EXPORT_KW * CONTROL_WINDOW_HOURS
    )

    for _ in range(max_passes):
        changed = False

        for day in days:
            current = exports[day]
            local_value = current
            local_score = score
            local_result = result

            for candidate in export_options:
                trial = dict(exports)
                trial[day] = candidate
                candidate_score, candidate_result = score_strategy(
                    hours,
                    start_energy,
                    days,
                    charges,
                    trial,
                    baseline_pre10=baseline_pre10,
                    context=context,
                )

                if candidate_score is None:
                    continue

                delivered = candidate_result["daily"][day]["premium_export"]
                if candidate >= 0.5 and delivered < candidate - 0.10:
                    continue

                if candidate_score > local_score + 0.0001:
                    local_value = candidate
                    local_score = candidate_score
                    local_result = candidate_result

            if abs(local_value - current) >= 0.5:
                exports[day] = local_value
                score = local_score
                result = local_result
                changed = True

        if not changed:
            break

    return score, result, exports


def optimise_horizon(
    hours,
    start_energy,
    max_passes=2,
):
    """Jointly optimise daily charge and export over the whole horizon.

    Charge and export are deliberately evaluated as a *pair*.  This allows
    the search to move directly from (0 charge, 0 export) to a profitable
    (charge, export) combination even when either action is unattractive or
    unsafe on its own.  Every candidate pair is scored by re-simulating the
    complete multi-day horizon, so stored energy can choose its most valuable
    use: premium export, avoided later imports, or forecast reserve.
    """

    days = horizon_days(hours)
    context = build_simulation_context(hours)

    charges = {
        day: 0.0
        for day in days
    }

    exports = {
        day: 0.0
        for day in days
    }

    # The no-action run is used only to establish unavoidable morning import
    # allowances.  It is not the economic comparison shown to the user.
    no_action_result = simulate(
        hours,
        start_energy,
        charges=charges,
        exports=exports,
        context=context,
    )

    baseline_pre10 = {
        day: float(
            no_action_result["daily"][day].get(
                "pre_10am_grid_import",
                0.0
            )
        )
        for day in days
    }

    baseline_score, baseline_result, baseline_exports = (
        optimise_export_only_baseline(
            hours,
            start_energy,
            days,
            baseline_pre10,
            context,
        )
    )

    # Start the joint search from the realistic export-only strategy.  Joint
    # candidates may add cheap charging and alter exports together.
    exports = dict(baseline_exports)
    best_score = baseline_score

    charge_options = candidate_values(
        MAX_GRID_CHARGE_KW * CONTROL_WINDOW_HOURS
    )

    export_options = candidate_values(
        MAX_EXPORT_KW * CONTROL_WINDOW_HOURS
    )

    for pass_number in range(
        1,
        max_passes + 1,
    ):

        changed = False

        for day in days:
            current_charge = charges[day]
            current_export = exports[day]
            local_best_charge = current_charge
            local_best_export = current_export
            local_best_score = best_score
            local_best_result = None

            for candidate_charge in charge_options:
                for candidate_export in export_options:
                    if (
                        candidate_charge == current_charge
                        and candidate_export == current_export
                    ):
                        continue

                    trial_charges = dict(charges)
                    trial_exports = dict(exports)
                    trial_charges[day] = candidate_charge
                    trial_exports[day] = candidate_export

                    score, result = score_strategy(
                        hours,
                        start_energy,
                        days,
                        trial_charges,
                        trial_exports,
                        baseline_pre10=baseline_pre10,
                        context=context,
                    )

                    if score is None:
                        continue

                    delivered_charge = result["daily"][day]["grid_charge"]
                    delivered_export = result["daily"][day]["premium_export"]
                    if (
                        candidate_charge >= 0.5
                        and delivered_charge < candidate_charge - 0.10
                    ):
                        continue
                    if (
                        candidate_export >= 0.5
                        and delivered_export < candidate_export - 0.10
                    ):
                        continue

                    if score > local_best_score + 0.0001:
                        local_best_score = score
                        local_best_charge = candidate_charge
                        local_best_export = candidate_export
                        local_best_result = result

            if (
                abs(local_best_charge - current_charge) >= 0.5
                or abs(local_best_export - current_export) >= 0.5
            ):
                charges[day] = local_best_charge
                exports[day] = local_best_export
                best_score = local_best_score
                changed = True

        if not changed:
            break

    # A second whole-horizon sweep changes two adjacent days together. This
    # catches cases where charging/retaining on one day only becomes valuable
    # when the following day's export changes at the same time.
    def nearby(value, maximum):
        return sorted({
            0.0,
            round(value, 2),
            round(max(0.0, value - 5.0), 2),
            round(min(maximum, value + 5.0), 2),
        })

    max_charge = MAX_GRID_CHARGE_KW * CONTROL_WINDOW_HOURS
    max_export = MAX_EXPORT_KW * CONTROL_WINDOW_HOURS
    for first_day, second_day in zip(days, days[1:]):
        local_best = None
        for first_charge in nearby(charges[first_day], max_charge):
            for first_export in nearby(exports[first_day], max_export):
                for second_charge in nearby(charges[second_day], max_charge):
                    for second_export in nearby(exports[second_day], max_export):
                        trial_charges = dict(charges)
                        trial_exports = dict(exports)
                        trial_charges[first_day] = first_charge
                        trial_exports[first_day] = first_export
                        trial_charges[second_day] = second_charge
                        trial_exports[second_day] = second_export
                        candidate_score, candidate_result = score_strategy(
                            hours,
                            start_energy,
                            days,
                            trial_charges,
                            trial_exports,
                            baseline_pre10=baseline_pre10,
                            context=context,
                        )
                        if candidate_score is None or candidate_score <= best_score + 0.0001:
                            continue
                        requested = (
                            (first_day, first_charge, first_export),
                            (second_day, second_charge, second_export),
                        )
                        if any(
                            (
                                charge >= 0.5
                                and candidate_result["daily"][day]["grid_charge"]
                                < charge - 0.10
                            )
                            or (
                                export >= 0.5
                                and candidate_result["daily"][day]["premium_export"]
                                < export - 0.10
                            )
                            for day, charge, export in requested
                        ):
                            continue
                        best_score = candidate_score
                        local_best = (trial_charges, trial_exports)
        if local_best is not None:
            charges, exports = local_best

    final_score, final_result = (
        score_strategy(
            hours,
            start_energy,
            days,
            charges,
            exports,
            baseline_pre10=baseline_pre10,
            context=context,
        )
    )

    return {
        "days": days,
        "charges": charges,
        "exports": exports,

        "baseline_score":
            baseline_score,

        "baseline_result":
            baseline_result,

        "baseline_exports":
            baseline_exports,

        "baseline_type":
            "SAFE_EXPORT_ONLY",

        "search_method":
            "JOINT_DAILY_PAIRS_PLUS_ADJACENT_DAY_LOOKAHEAD",

        "final_score":
            final_score,

        "final_result":
            final_result,
    }
