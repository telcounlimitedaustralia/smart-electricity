"""
Smart Electricity - Shadow Economic Optimiser v1

PURPOSE
-------
Evaluate 10:00-14:00 cheap grid charging and 17:00-21:00
premium battery export.

SHADOW ONLY.
This module contains NO FoxESS write/control functionality.

The optimiser evaluates the incremental economics of purchasing
energy during the AGL shoulder window and later using/exporting it,
while preserving enough battery energy to reach the next cheap
charging opportunity.

v1 intentionally reuses the existing export planner forecast and
simulation outputs. Later versions will use probabilistic ML
P10/P50/P90 forecasts and learned battery efficiency.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from export_planner import calculate


TZ = ZoneInfo("Australia/Sydney")

BATTERY_KWH = 42.0
ABSOLUTE_MIN_SOC = 10.0
ABSOLUTE_MIN_KWH = BATTERY_KWH * ABSOLUTE_MIN_SOC / 100.0

# AGL tariff
SHOULDER_BUY_CENTS = 11.11
PREMIUM_FIT_CENTS = 28.0
OFFPEAK_BUY_CENTS = 32.12
LOW_PEAK_BUY_CENTS = 42.416
HIGH_PEAK_BUY_CENTS = 41.91

# Conservative starting assumptions.
# These are deliberately explicit so they can later be replaced
# with values learned from FoxESS history.
CHARGE_EFFICIENCY = 0.95
DISCHARGE_EFFICIENCY = 0.95

# Operational uncertainty reserve.
# v1 uses a fixed reserve. This will later become ML-derived.
FORECAST_RESERVE_KWH = 2.0

# Avoid tiny charge recommendations.
MIN_ACTION_KWH = 0.50


def pct_to_kwh(percent):
    return BATTERY_KWH * float(percent) / 100.0


def kwh_to_pct(kwh):
    return float(kwh) / BATTERY_KWH * 100.0


def find_plan(plans, day):
    for p in plans:
        if p.get("date") == day:
            return p
    return None


def next_day(day):
    return (
        datetime.strptime(day, "%Y-%m-%d").date()
        + timedelta(days=1)
    ).isoformat()


def reserve_to_next_10am(plan, tomorrow):
    """
    Estimate energy that should remain after premium export.

    v1 starts from the existing export planner's predicted battery
    energy immediately before next useful solar.

    We then add an uncertainty reserve.

    If tomorrow has weak solar, this reserve is especially valuable
    because it reduces the risk of buying 32.12c/42c electricity
    before the next 10am shoulder window.
    """

    next_solar_kwh = plan.get("next_solar_start_kwh")

    if next_solar_kwh is None:
        base = ABSOLUTE_MIN_KWH
    else:
        base = max(
            ABSOLUTE_MIN_KWH,
            float(next_solar_kwh)
        )

    reserve = min(
        BATTERY_KWH,
        base + FORECAST_RESERVE_KWH
    )

    tomorrow_solar = None
    if tomorrow:
        tomorrow_solar = tomorrow.get(
            "full_day_solar_kwh"
        )

    return {
        "reserve_kwh": reserve,
        "reserve_soc": kwh_to_pct(reserve),
        "tomorrow_solar_kwh": tomorrow_solar,
    }


def shoulder_opportunity(plan, tomorrow):
    """
    Determine incremental cheap-energy opportunity.

    Principle:
      Do NOT blindly fill the battery.

    We first use the existing forecast's natural 5PM SOC.
    Cheap grid energy is considered only for unused battery capacity.

    Purchased AC energy is converted to stored battery energy using
    charge efficiency.
    """

    pre_export_soc = plan.get("pre_export_soc")

    if pre_export_soc is None:
        return None

    natural_5pm_kwh = pct_to_kwh(
        pre_export_soc
    )

    battery_room_kwh = max(
        0.0,
        BATTERY_KWH - natural_5pm_kwh
    )

    # AC energy required to fill that storage room.
    max_grid_purchase_kwh = (
        battery_room_kwh / CHARGE_EFFICIENCY
        if CHARGE_EFFICIENCY > 0
        else 0.0
    )

    reserve = reserve_to_next_10am(
        plan,
        tomorrow
    )

    existing_export_kwh = float(
        plan.get(
            "recommended_export_kwh",
            0.0
        ) or 0.0
    )

    # Stored energy added by the cheap purchase.
    added_stored_kwh = (
        max_grid_purchase_kwh
        * CHARGE_EFFICIENCY
    )

    revised_5pm_kwh = min(
        BATTERY_KWH,
        natural_5pm_kwh + added_stored_kwh
    )

    # Maximum battery energy economically/operationally available
    # above tomorrow's protected reserve.
    available_above_reserve = max(
        0.0,
        revised_5pm_kwh
        - reserve["reserve_kwh"]
    )

    # Existing planner already identified naturally safe export.
    # The new cheap-energy tranche can increase that opportunity,
    # but never beyond energy above the dynamic reserve.
    potential_battery_export = min(
        available_above_reserve,
        existing_export_kwh
        + added_stored_kwh
    )

    incremental_battery_export = max(
        0.0,
        potential_battery_export
        - existing_export_kwh
    )

    # AC/grid energy obtained after discharge losses.
    incremental_grid_export = (
        incremental_battery_export
        * DISCHARGE_EFFICIENCY
    )

    purchase_cost = (
        max_grid_purchase_kwh
        * SHOULDER_BUY_CENTS
        / 100.0
    )

    incremental_revenue = (
        incremental_grid_export
        * PREMIUM_FIT_CENTS
        / 100.0
    )

    incremental_profit = (
        incremental_revenue
        - purchase_cost
    )

    profitable = (
        max_grid_purchase_kwh >= MIN_ACTION_KWH
        and incremental_profit > 0
    )

    if not profitable:
        recommended_purchase = 0.0
        added_stored = 0.0
        incremental_export = 0.0
        cost = 0.0
        revenue = 0.0
        profit = 0.0
    else:
        recommended_purchase = max_grid_purchase_kwh
        added_stored = added_stored_kwh
        incremental_export = incremental_grid_export
        cost = purchase_cost
        revenue = incremental_revenue
        profit = incremental_profit

    revised_export_kwh = (
        existing_export_kwh
        + incremental_export
    )

    return {
        "date": plan["date"],

        "solar_forecast_kwh":
            plan.get("full_day_solar_kwh"),

        "natural_5pm_soc":
            round(float(pre_export_soc), 1),

        "natural_5pm_kwh":
            round(natural_5pm_kwh, 2),

        "existing_export_kwh":
            round(existing_export_kwh, 2),

        "existing_export_percent":
            round(
                existing_export_kwh
                / BATTERY_KWH
                * 100.0,
                1
            ),

        "battery_room_kwh":
            round(battery_room_kwh, 2),

        "recommended_grid_charge_kwh":
            round(recommended_purchase, 2),

        "stored_from_grid_kwh":
            round(added_stored, 2),

        "revised_5pm_soc":
            round(
                kwh_to_pct(
                    min(
                        BATTERY_KWH,
                        natural_5pm_kwh
                        + added_stored
                    )
                ),
                1
            ),

        "dynamic_reserve_kwh":
            round(reserve["reserve_kwh"], 2),

        "dynamic_reserve_soc":
            round(reserve["reserve_soc"], 1),

        "tomorrow_solar_kwh":
            reserve["tomorrow_solar_kwh"],

        "incremental_export_kwh":
            round(incremental_export, 2),

        "revised_export_kwh":
            round(revised_export_kwh, 2),

        "revised_export_percent":
            round(
                revised_export_kwh
                / BATTERY_KWH
                * 100.0,
                1
            ),

        "shoulder_charge_cost":
            round(cost, 2),

        "incremental_fit_revenue":
            round(revenue, 2),

        "incremental_profit":
            round(profit, 2),

        "profitable":
            profitable,

        "mode":
            "SHADOW_ONLY",
    }


def calculate_shadow():
    base = calculate()

    if not base.get("available"):
        return {
            "available": False,
            "reason": base.get(
                "reason",
                "Base planner unavailable"
            )
        }

    plans = base.get("plans", [])

    results = []

    for plan in plans:

        # A completed/partial day without a 5PM checkpoint
        # cannot be evaluated for shoulder charging.
        if plan.get("pre_export_soc") is None:
            continue

        tomorrow = find_plan(
            plans,
            next_day(plan["date"])
        )

        result = shoulder_opportunity(
            plan,
            tomorrow
        )

        if result:
            results.append(result)

    return {
        "available": True,
        "generated_at":
            datetime.now(TZ).isoformat(
                timespec="seconds"
            ),
        "mode":
            "SHADOW_ECONOMIC_OPTIMISER_V1",
        "battery_kwh":
            BATTERY_KWH,
        "charge_efficiency":
            CHARGE_EFFICIENCY,
        "discharge_efficiency":
            DISCHARGE_EFFICIENCY,
        "forecast_reserve_kwh":
            FORECAST_RESERVE_KWH,
        "shoulder_buy_cents":
            SHOULDER_BUY_CENTS,
        "premium_fit_cents":
            PREMIUM_FIT_CENTS,
        "plans":
            results,
        "warning":
            (
                "SHADOW ONLY. "
                "No FoxESS settings are changed."
            )
    }


def print_report(result):
    print("SMART ELECTRICITY")
    print("SHADOW ECONOMIC OPTIMISER v1")
    print("=" * 44)

    if not result.get("available"):
        print(
            "Unavailable:",
            result.get("reason")
        )
        return

    print(
        "Generated:",
        result["generated_at"]
    )
    print(
        "Shoulder buy:",
        f'{result["shoulder_buy_cents"]:.2f}c/kWh'
    )
    print(
        "Premium FiT:",
        f'{result["premium_fit_cents"]:.2f}c/kWh'
    )
    print(
        "Charge efficiency:",
        f'{result["charge_efficiency"]*100:.1f}%'
    )
    print(
        "Discharge efficiency:",
        f'{result["discharge_efficiency"]*100:.1f}%'
    )
    print()

    for p in result["plans"]:

        print(p["date"])
        print("-" * 44)

        print(
            "Solar forecast:       ",
            p["solar_forecast_kwh"],
            "kWh"
        )

        print(
            "Natural 5PM SOC:       ",
            p["natural_5pm_soc"],
            "%"
        )

        print(
            "Existing export:       ",
            p["existing_export_percent"],
            "% /",
            p["existing_export_kwh"],
            "kWh"
        )

        print(
            "Cheap grid charge:     ",
            p["recommended_grid_charge_kwh"],
            "kWh"
        )

        print(
            "Revised 5PM SOC:       ",
            p["revised_5pm_soc"],
            "%"
        )

        print(
            "Dynamic reserve:       ",
            p["dynamic_reserve_soc"],
            "% /",
            p["dynamic_reserve_kwh"],
            "kWh"
        )

        print(
            "Tomorrow solar:        ",
            p["tomorrow_solar_kwh"],
            "kWh"
        )

        print(
            "Incremental export:    ",
            p["incremental_export_kwh"],
            "kWh"
        )

        print(
            "Revised export:        ",
            p["revised_export_percent"],
            "% /",
            p["revised_export_kwh"],
            "kWh"
        )

        print(
            "Shoulder charge cost:  $",
            f'{p["shoulder_charge_cost"]:.2f}',
            sep=""
        )

        print(
            "Extra FiT revenue:     $",
            f'{p["incremental_fit_revenue"]:.2f}',
            sep=""
        )

        print(
            "Incremental profit:    $",
            f'{p["incremental_profit"]:.2f}',
            sep=""
        )

        print(
            "Decision:               ",
            "CHARGE"
            if p["profitable"]
            else "NO CHARGE"
        )

        print()

    print(
        "SHADOW ONLY - "
        "NO FOXESS CONTROL PERFORMED"
    )


if __name__ == "__main__":
    print_report(
        calculate_shadow()
    )
