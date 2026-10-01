import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = "data/energy.db"
TZ = ZoneInfo("Australia/Sydney")

BATTERY_KWH = 42.0
PROTECTED_SOC = 10.0
PROTECTED_KWH = BATTERY_KWH * PROTECTED_SOC / 100.0

SOLAR_CAPACITY_KW = 9.7
PV_FACTOR = 0.78

EXPORT_RATE = 28.0
EXPORT_START = 17
EXPORT_END = 21

MAX_EXPORT_POWER_KW = 10.0

# Conservative allowance until ML replaces the load model
LOAD_SAFETY_FACTOR = 1.10

# Test export in 1% battery increments
EXPORT_STEP_KWH = BATTERY_KWH * 0.01


def connect():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def latest_state(c):
    r = c.execute("""
        SELECT *
        FROM foxess_live
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()

    return dict(r) if r else None


def load_profile(c):
    rows = c.execute("""
        SELECT
            CAST(substr(timestamp,12,2) AS INTEGER) hour,
            AVG(load_kw) avg_kw
        FROM foxess_history
        WHERE load_kw IS NOT NULL
          AND load_kw >= 0
          AND load_kw < 30
        GROUP BY hour
        ORDER BY hour
    """).fetchall()

    profile = {
        int(r["hour"]): float(r["avg_kw"])
        for r in rows
    }

    if len(profile) < 20:
        return {h:1.0 for h in range(24)}, "FALLBACK"

    return profile, "FOXESS_HISTORY"


def full_day_load(profile):
    """
    Predicted household consumption for a complete 24-hour day.

    load_profile() contains average kW for each hour.
    Each profile value represents one hour, therefore summing
    all 24 hourly values gives predicted daily kWh.

    Apply the same safety factor used by the optimiser.
    """
    return sum(
        float(profile.get(hour, 1.0)) * LOAD_SAFETY_FACTOR
        for hour in range(24)
    )


def forecast(c):
    now = datetime.now(TZ)

    rows = c.execute("""
        SELECT
            timestamp,
            shortwave_radiation,
            cloud_cover,
            precipitation,
            temperature
        FROM weather
        WHERE timestamp >= ?
        ORDER BY timestamp
        LIMIT 168
    """, (now.strftime("%Y-%m-%dT%H:00"),)).fetchall()

    return [dict(r) for r in rows]


def pv_for_hour(radiation):
    radiation = float(radiation or 0)

    return (
        radiation / 1000.0
        * SOLAR_CAPACITY_KW
        * PV_FACTOR
    )



def full_day_solar(c, day):
    """Full calendar-day PV forecast for dashboard display."""
    rows = c.execute("""
        SELECT shortwave_radiation
        FROM weather
        WHERE substr(timestamp,1,10) = ?
        ORDER BY timestamp
    """, (day,)).fetchall()

    return sum(
        pv_for_hour(r["shortwave_radiation"])
        for r in rows
    )



def simulate(hours, profile, start_energy, exports=None):
    """
    Hour-by-hour energy simulation.

    exports:
      dict containing date -> kWh exported during 5-9 PM.
    """

    exports = exports or {}

    energy = start_energy
    grid_import = 0.0
    grid_export = 0.0

    daily = {}

    for row in hours:

        ts = datetime.fromisoformat(row["timestamp"])
        day = ts.date().isoformat()
        hour = ts.hour

        if day not in daily:
            daily[day] = {
                "solar":0.0,
                "load":0.0,
                "grid_import":0.0,
                "export":0.0,
                "min_energy":energy,
                "end_energy":energy,
                "sunset_energy":None,
                "midnight_energy":None,
                "next_solar_start_energy":None,
                "pre_export_energy":None,
                "hourly_energy":[]
            }

        solar = pv_for_hour(
            row["shortwave_radiation"]
        )

        load = (
            profile.get(hour,1.0)
            * LOAD_SAFETY_FACTOR
        )

        daily[day]["solar"] += solar
        daily[day]["load"] += load

        net = solar - load

        if net >= 0:

            charge_room = BATTERY_KWH - energy

            charge = min(
                net,
                charge_room
            )

            energy += charge

            natural_export = net - charge

            grid_export += natural_export

        else:

            need = -net

            usable = max(
                0.0,
                energy - PROTECTED_KWH
            )

            discharge = min(
                need,
                usable
            )

            energy -= discharge

            missing = need - discharge

            grid_import += missing
            daily[day]["grid_import"] += missing


        # Battery immediately before the premium
        # export window begins at 17:00.
        if hour == EXPORT_START:
            daily[day]["pre_export_energy"] = energy

        # Scheduled premium export is spread evenly
        # across 17:00-21:00.
        if (
            EXPORT_START <= hour < EXPORT_END
            and day in exports
        ):

            hourly_export = (
                exports[day] /
                (EXPORT_END - EXPORT_START)
            )

            hourly_export = min(
                hourly_export,
                MAX_EXPORT_POWER_KW
            )

            possible = max(
                0.0,
                energy - PROTECTED_KWH
            )

            actual = min(
                hourly_export,
                possible
            )

            energy -= actual

            grid_export += actual
            daily[day]["export"] += actual


        daily[day]["min_energy"] = min(
            daily[day]["min_energy"],
            energy
        )

        daily[day]["end_energy"] = energy

        daily[day]["hourly_energy"].append({
            "hour": hour,
            "energy": energy,
            "solar": solar
        })

        # Dashboard checkpoints.
        # Sunset is represented by the end of the 18:00 hour.
        if hour == 18:
            daily[day]["sunset_energy"] = energy

        # Last simulated hour of the calendar day.
        if hour == 23:
            daily[day]["midnight_energy"] = energy


    # Exact battery SOC before the next day's useful solar.
    # "Useful solar" = forecast PV >= 0.10 kWh during that hour.
    ordered_days = list(daily.keys())

    for i, day in enumerate(ordered_days[:-1]):

        next_day = ordered_days[i + 1]
        next_hours = daily[next_day]["hourly_energy"]

        first_solar_index = None

        for j, item in enumerate(next_hours):
            if item["solar"] >= 0.10:
                first_solar_index = j
                break

        if first_solar_index is not None:

            if first_solar_index == 0:
                # Battery immediately entering the next day.
                before_solar = daily[day]["end_energy"]
            else:
                # Battery after the hour immediately preceding
                # useful solar generation.
                before_solar = next_hours[
                    first_solar_index - 1
                ]["energy"]

            daily[day]["next_solar_start_energy"] = before_solar

    # For the final forecast day we do not have the following
    # day's forecast, so leave next_solar_start_energy as None.

    return {
        "grid_import":grid_import,
        "grid_export":grid_export,
        "end_energy":energy,
        "daily":daily
    }


def optimise(hours, profile, start_energy):
    """
    Sequential 7-day export optimisation.

    A candidate is accepted only when:

      1. the simulator physically delivers the requested export;
      2. it does not materially increase grid imports;
      3. battery never intentionally exports below the protected floor.

    The selected export for an earlier day becomes part of every
    subsequent simulation.
    """

    days = []

    for row in hours:
        day = row["timestamp"][:10]

        if day not in days:
            days.append(day)

    selected = {}

    baseline = simulate(
        hours,
        profile,
        start_energy,
        {}
    )

    baseline_import = baseline["grid_import"]

    for day in days:

        best = 0.0

        candidate = EXPORT_STEP_KWH

        while candidate <= (
            BATTERY_KWH - PROTECTED_KWH + 0.001
        ):

            trial = dict(selected)
            trial[day] = candidate

            result = simulate(
                hours,
                profile,
                start_energy,
                trial
            )

            actual_export = (
                result["daily"]
                .get(day,{})
                .get("export",0.0)
            )

            # Candidate must actually be physically deliverable.
            fully_delivered = (
                actual_export >= candidate - 0.05
            )

            extra_import = (
                result["grid_import"]
                - baseline_import
            )

            if (
                fully_delivered
                and extra_import <= 0.10
            ):
                best = actual_export
                candidate += EXPORT_STEP_KWH
            else:
                break

        if best >= 0.5:
            selected[day] = best

    final = simulate(
        hours,
        profile,
        start_energy,
        selected
    )

    return selected, final, baseline


def calculate():

    c = connect()

    state = latest_state(c)

    if not state:
        c.close()
        return {
            "available":False,
            "reason":"No live FoxESS data"
        }

    soc = float(
        state.get("battery_soc") or 0
    )

    start_energy = (
        BATTERY_KWH *
        soc /
        100.0
    )

    profile, source = load_profile(c)

    hours = forecast(c)

    if not hours:
        c.close()
        return {
            "available":False,
            "reason":"No weather forecast"
        }

    selected, final, baseline = optimise(
        hours,
        profile,
        start_energy
    )

    plans = []

    previous_end_energy = start_energy

    today = datetime.now(TZ).date().isoformat()

    for day, values in final["daily"].items():

        starting_energy = previous_end_energy

        starting_soc = (
            starting_energy /
            BATTERY_KWH *
            100.0
        )

        # ACTUAL export delivered by simulator
        export_kwh = float(
            values.get("export",0.0)
        )

        # Extra protection:
        # if today's real starting SOC is already at/below
        # FoxESS minimum SOC, do not display an export.
        if (
            day == today
            and start_energy <= PROTECTED_KWH
        ):
            export_kwh = 0.0

        export_percent = (
            export_kwh /
            BATTERY_KWH *
            100.0
        )

        end_energy = float(
            values["end_energy"]
        )

        end_soc = (
            end_energy /
            BATTERY_KWH *
            100.0
        )

        revenue = (
            export_kwh *
            EXPORT_RATE /
            100.0
        )

        rating = (
            "HIGH"
            if values["solar"] >= 30
            else "MODERATE"
            if values["solar"] >= 18
            else "LOW"
        )

        decision = (
            "YES"
            if export_kwh >= 0.5
            else "NO"
        )

        # Solar shown by the optimiser is REMAINING solar
        # because forecast() starts at the current hour.
        remaining_solar_kwh = float(values["solar"])

        full_solar_kwh = full_day_solar(c, day)

        if full_solar_kwh > 0:
            remaining_solar_percent = min(
                100.0,
                remaining_solar_kwh /
                full_solar_kwh *
                100.0
            )
        else:
            remaining_solar_percent = 0.0

        start_kwh = starting_energy

        sunset_energy = values.get("sunset_energy")
        midnight_energy = values.get("midnight_energy")
        next_solar_energy = values.get(
            "next_solar_start_energy"
        )

        def energy_display(v):
            if v is None:
                return None, None

            return (
                round(v / BATTERY_KWH * 100.0,1),
                round(v,1)
            )

        pre_export_soc, pre_export_kwh = energy_display(
            values.get("pre_export_energy")
        )

        sunset_soc, sunset_kwh = energy_display(
            sunset_energy
        )

        midnight_soc, midnight_kwh = energy_display(
            midnight_energy
        )

        next_solar_soc, next_solar_kwh = energy_display(
            next_solar_energy
        )

        plans.append({
            "date":day,

            "solar_kwh":
                round(remaining_solar_kwh,1),

            "full_day_solar_kwh":
                round(full_solar_kwh,1),

            "remaining_solar_kwh":
                round(remaining_solar_kwh,1),

            "remaining_solar_percent":
                round(remaining_solar_percent,1),

            "starting_soc_kwh":
                round(start_kwh,1),

            "foxess_min_soc":
                10.0,

            "foxess_min_kwh":
                round(BATTERY_KWH * 0.10,1),

            "pre_export_soc":
                pre_export_soc,

            "pre_export_kwh":
                pre_export_kwh,

            "sunset_soc":
                sunset_soc,

            "sunset_kwh":
                sunset_kwh,

            "midnight_soc":
                midnight_soc,

            "midnight_kwh":
                midnight_kwh,

            "next_solar_start_soc":
                next_solar_soc,

            "next_solar_start_kwh":
                next_solar_kwh,

            "predicted_load_kwh":
                round(values["load"],1),

            "solar_rating":
                rating,

            "starting_soc":
                round(starting_soc,1),

            "export":
                decision,

            "recommended_export_percent":
                round(export_percent,1),

            "recommended_export_kwh":
                round(export_kwh,2),

            "target_soc":
                round(end_soc,1),

            "potential_revenue":
                round(revenue,2),

            "predicted_grid_import_kwh":
                round(values["grid_import"],2),

            "reason":
                (
                    "Export is forecast to remain surplus while "
                    "maintaining the protected battery reserve without "
                    "materially increasing predicted grid imports."
                    if decision == "YES"
                    else
                    "Preserve battery to maintain the FoxESS minimum SOC "
                    "and minimise predicted grid imports."
                )
        })

        previous_end_energy = end_energy

    daily_load = sum(
        profile.get(h,1.0)
        for h in range(24)
    ) * LOAD_SAFETY_FACTOR

    total_export = sum(selected.values())

    result = {
        "available":True,

        "mode":"HOURLY_7_DAY_PROVISIONAL",

        "load_source":source,

        "current_soc":
            round(soc,1),

        "protected_soc":
            PROTECTED_SOC,

        "predicted_daily_load_kwh":
            round(daily_load,1),

        "baseline_grid_import_kwh":
            round(baseline["grid_import"],2),

        "planned_grid_import_kwh":
            round(final["grid_import"],2),

        "total_recommended_export_kwh":
            round(total_export,2),

        "total_potential_revenue":
            round(
                total_export *
                EXPORT_RATE /
                100.0,
                2
            ),

        "plans":plans,

        "warning":
            (
                "Hourly 7-day simulation. Recommendation only. "
                "No FoxESS settings are changed."
            )
    }

    c.close()

    return result


if __name__ == "__main__":

    import json

    print(
        json.dumps(
            calculate(),
            indent=2
        )
    )
