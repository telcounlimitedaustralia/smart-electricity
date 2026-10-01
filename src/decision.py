import sqlite3
from datetime import datetime
from tariff import rates_at

DB_PATH = "data/energy.db"

BATTERY_USABLE_KWH = 42.0
MIN_SOC = 10.0

# Phase 1 safety:
# Never recommend forced battery-to-grid export.
ALLOW_BATTERY_EXPORT = False


def latest_foxess():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    row = conn.execute("""
        SELECT
            timestamp,
            pv_kw,
            load_kw,
            grid_import_kw,
            grid_export_kw,
            battery_soc,
            battery_charge_kw,
            battery_discharge_kw
        FROM foxess_live
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()

    conn.close()

    if row is None:
        raise RuntimeError("No FoxESS readings found.")

    return dict(row)


def available_battery_kwh(soc):
    usable_soc = max(0, soc - MIN_SOC)
    return BATTERY_USABLE_KWH * usable_soc / 100.0


def make_decision(data, tariff):
    soc = float(data.get("battery_soc") or 0)
    pv = float(data.get("pv_kw") or 0)
    load = float(data.get("load_kw") or 0)
    grid_import = float(data.get("grid_import_kw") or 0)
    grid_export = float(data.get("grid_export_kw") or 0)
    battery_charge = float(data.get("battery_charge_kw") or 0)
    battery_discharge = float(data.get("battery_discharge_kw") or 0)

    import_rate = float(tariff["import_rate"])
    export_rate = float(tariff["export_rate"])

    available = available_battery_kwh(soc)

    # ---------------------------------------------------------
    # PHASE 1 RULES
    # ---------------------------------------------------------

    # Battery protection
    if soc <= MIN_SOC + 1:
        decision = "HOLD_BATTERY"
        reason = (
            f"Battery is close to the {MIN_SOC:.0f}% minimum SOC. "
            "Preserve remaining battery energy."
        )

    # Solar surplus exists right now
    elif pv > load + 0.05:
        if battery_charge > 0.05:
            decision = "CHARGE_FROM_SOLAR"
            reason = (
                "Solar production exceeds household demand and the battery "
                "is currently charging from available solar."
            )
        elif grid_export > 0.05:
            decision = "ALLOW_SOLAR_EXPORT"
            reason = (
                f"Solar exceeds household demand and excess solar is being "
                f"exported at {export_rate:.2f} c/kWh."
            )
        else:
            decision = "SOLAR_SURPLUS"
            reason = (
                "Solar production currently exceeds household demand. "
                "No forced battery action is required."
            )

    # House is being supplied by battery
    elif battery_discharge > 0.05 and grid_import < 0.10:
        decision = "USE_BATTERY_FOR_HOME"
        reason = (
            "Battery is supplying household demand and grid import is minimal. "
            "Continue normal self-consumption."
        )

    # Expensive import
    elif import_rate >= 40 and grid_import > 0.10:
        if soc > MIN_SOC + 2:
            decision = "USE_BATTERY_FOR_HOME"
            reason = (
                f"Grid electricity costs {import_rate:.2f} c/kWh. "
                "Available battery energy should preferentially offset "
                "household grid consumption."
            )
        else:
            decision = "HOLD_BATTERY"
            reason = "Battery SOC is too close to minimum for further discharge."

    # Cheap import - but DO NOT automatically grid-charge yet.
    elif import_rate <= 15:
        decision = "HOLD"
        reason = (
            f"Import price is relatively low at {import_rate:.2f} c/kWh. "
            "Grid charging is not enabled in Phase 1; future optimisation "
            "will decide whether charging is economically worthwhile."
        )

    # Premium export period
    elif export_rate >= 20:
        decision = "HOLD_BATTERY"
        reason = (
            f"Premium export tariff is active at {export_rate:.2f} c/kWh, "
            "but forced battery export is disabled in Phase 1. "
            "We first need load and solar forecasts to determine how much "
            "energy can safely be sold without buying expensive energy later."
        )

    # Normal self-consumption
    elif grid_import > 0.10 and soc > MIN_SOC + 2:
        decision = "USE_BATTERY_FOR_HOME"
        reason = (
            "Household demand exceeds current solar production. "
            "Use battery energy for normal self-consumption where the "
            "inverter permits."
        )

    else:
        decision = "HOLD"
        reason = (
            "Current solar, load, battery and tariff conditions do not "
            "justify a special battery action."
        )

    return {
        "decision": decision,
        "reason": reason,
        "available_kwh_above_min": round(available, 2),
        "battery_export_enabled": ALLOW_BATTERY_EXPORT,
    }


def main():
    data = latest_foxess()
    now = datetime.now().astimezone()
    tariff = rates_at(now)
    result = make_decision(data, tariff)

    print()
    print("SMART ELECTRICITY - DECISION SIMULATOR")
    print("======================================")
    print("Time:             ", now.isoformat(timespec="seconds"))

    print()
    print("LIVE FOXESS")
    print("--------------------------------------")
    print(f"PV:                {float(data['pv_kw'] or 0):.3f} kW")
    print(f"House load:        {float(data['load_kw'] or 0):.3f} kW")
    print(f"Grid import:       {float(data['grid_import_kw'] or 0):.3f} kW")
    print(f"Grid export:       {float(data['grid_export_kw'] or 0):.3f} kW")
    print(f"Battery SOC:       {float(data['battery_soc'] or 0):.1f}%")
    print(f"Battery charging:  {float(data['battery_charge_kw'] or 0):.3f} kW")
    print(f"Battery discharge: {float(data['battery_discharge_kw'] or 0):.3f} kW")

    print()
    print("TARIFF")
    print("--------------------------------------")
    print(f"Import:            {tariff['import_rate']:.3f} c/kWh")
    print(f"Import period:     {tariff['import_period']}")
    print(f"Export:            {tariff['export_rate']:.3f} c/kWh")
    print(f"Export period:     {tariff['export_period']}")

    print()
    print("BATTERY")
    print("--------------------------------------")
    print(f"Available >10%:    {result['available_kwh_above_min']:.2f} kWh")
    print("Battery export:    DISABLED (Phase 1)")

    print()
    print("RECOMMENDATION")
    print("--------------------------------------")
    print("ACTION:", result["decision"])
    print("WHY:   ", result["reason"])

    print()
    print("READ-ONLY MODE - no FoxESS settings changed.")
    print()


if __name__ == "__main__":
    main()
