from flask import Flask, render_template, jsonify, request
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
import sys
import time
from threading import Lock
from dotenv import load_dotenv

BASE = Path(os.getenv("SMART_ELECTRICITY_HOME", Path.home() / "smart-electricity"))
DB = BASE / "data" / "energy.db"
load_dotenv(BASE / ".env")
sys.path.insert(0, str(BASE / "src"))

from tariff import rates_at
from decision import make_decision
from export_planner import calculate as calculate_export_plan
from load_predictor import predict_day as predict_load_day
from ml_risk_analysis import calculate as calculate_ml_risk
from ml_readiness import calculate as calculate_ml_readiness
from ml_daily_performance import calculate as calculate_ml_performance
from control_switches import get_switches, set_switch
from economic_optimizer_v2 import (
    build_hours as build_economic_hours,
    build_simulation_context,
    horizon_days,
    initial_energy as economic_initial_energy,
    optimise_export_only_baseline,
    optimise_horizon,
    replay_without_grid_charge,
    simulate as simulate_economic,
    solar_only_5pm_energy,
    BATTERY_KWH as ECONOMIC_BATTERY_KWH,
    CHARGE_EFF as ECONOMIC_CHARGE_EFF,
    DISCHARGE_EFF as ECONOMIC_DISCHARGE_EFF,
    DEGRADATION_COST_CENTS_PER_BATTERY_KWH,
    SOLAR_PROTECTION_FACTOR,
    TERMINAL_ENERGY_VALUE_CENTS_PER_KWH,
)

app = Flask(__name__)

# Joint horizon search is intentionally thorough.  Keep the dashboard's
# 30-second refresh from rerunning the same 7-day optimisation every time.
ECONOMIC_CACHE_SECONDS = 300
economic_cache = {"created": 0.0, "payload": None}
economic_cache_lock = Lock()


@app.route("/api/control-switches")
def control_switch_status():
    state = get_switches(DB)
    state["control_mode"] = os.getenv("FOXESS_CONTROL_MODE", "rule").strip().lower()
    state["effective_charge_enabled"] = (
        state["control_mode"] == "joint"
        and state["master_enabled"]
        and state["charge_enabled"]
    )
    state["effective_export_enabled"] = (
        state["control_mode"] == "joint"
        and state["master_enabled"]
        and state["export_enabled"]
    )
    return jsonify(state)


@app.route("/api/control-switch", methods=["POST"])
def update_control_switch():
    if request.headers.get("X-Requested-With") != "SmartElectricityDashboard":
        return jsonify({"error": "Dashboard confirmation header required"}), 403
    if not request.is_json:
        return jsonify({"error": "JSON request required"}), 415

    payload = request.get_json(silent=True) or {}
    scope = payload.get("scope")
    enabled = payload.get("enabled")
    if scope not in {"master", "charge", "export"} or not isinstance(enabled, bool):
        return jsonify({"error": "Invalid scheduler switch request"}), 400

    control_mode = os.getenv("FOXESS_CONTROL_MODE", "rule").strip().lower()
    if enabled and control_mode != "joint":
        return jsonify({"error": "Joint controller is not active on this VM"}), 409

    actor = "authenticated-dashboard"
    state = set_switch(
        scope,
        enabled,
        actor=actor,
        detail="operator dashboard switch",
        db_path=DB,
    )
    foxess_result = "enabled for the next fresh scheduled decision"

    if not enabled:
        try:
            from foxess_joint_auto_execute import disable_scope, notify

            outcome, backup_path = disable_scope(scope)
            foxess_result = f"FoxESS schedule {outcome}"
            notify(
                "🛑 Smart Electricity - OPERATOR SWITCH OFF\n\n"
                f"Scope: {scope.upper()}\n"
                f"Result: {foxess_result}\n"
                f"Backup: {backup_path or 'not required'}"
            )
        except Exception as exc:
            return jsonify({
                "error": (
                    "Switch is OFF in the controller, but immediate FoxESS "
                    f"schedule cleanup failed: {type(exc).__name__}: {exc}"
                ),
                "switches": state,
            }), 502

    state["control_mode"] = control_mode
    return jsonify({"ok": True, "result": foxess_result, "switches": state})


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def latest():
    c = db()
    row = c.execute("""
        SELECT *
        FROM foxess_live
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()
    c.close()
    return dict(row) if row else None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    x = latest()
    if not x:
        return jsonify({"error": "No FoxESS data"}), 404

    now = datetime.now().astimezone()
    tariff = rates_at(now)
    decision = make_decision(x, tariff)

    c = db()

    weather = c.execute("""
        SELECT *
        FROM weather
        ORDER BY ABS(
            strftime('%s', timestamp) -
            strftime('%s', ?)
        )
        LIMIT 1
    """, (now.strftime("%Y-%m-%dT%H:%M"),)).fetchone()

    counts = {}
    for table in ["foxess_live", "weather", "ml_features"]:
        try:
            counts[table] = c.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        except:
            counts[table] = 0

    c.close()

    soc = float(x.get("battery_soc") or 0)
    battery_energy = 42.0 * soc / 100
    energy_above_min = 42.0 * max(0, soc - 10) / 100

    return jsonify({
        "time": now.isoformat(timespec="seconds"),
        "foxess": x,
        "battery": {
            "soc": soc,
            "stored_kwh": round(battery_energy, 2),
            "above_min_kwh": round(energy_above_min, 2),
            "min_soc": 10
        },
        "tariff": tariff,
        "decision": decision,
        "weather": dict(weather) if weather else None,
        "counts": counts
    })


@app.route("/api/history")
def history():
    c = db()

    rows = c.execute("""
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
        LIMIT 288
    """).fetchall()

    c.close()

    return jsonify([dict(x) for x in reversed(rows)])


@app.route("/api/today")
def today():
    c = db()

    today = datetime.now().astimezone().strftime("%Y-%m-%d")

    rows = c.execute("""
        SELECT *
        FROM foxess_live
        WHERE substr(timestamp,1,10) = ?
        ORDER BY id
    """, (today,)).fetchall()

    c.close()

    if len(rows) < 2:
        return jsonify({
            "samples": len(rows),
            "pv_kwh": 0,
            "load_kwh": 0,
            "import_kwh": 0,
            "export_kwh": 0
        })

    interval_hours = 5 / 60

    return jsonify({
        "samples": len(rows),
        "pv_kwh": round(sum((r["pv_kw"] or 0) * interval_hours for r in rows), 2),
        "load_kwh": round(sum((r["load_kw"] or 0) * interval_hours for r in rows), 2),
        "import_kwh": round(sum((r["grid_import_kw"] or 0) * interval_hours for r in rows), 2),
        "export_kwh": round(sum((r["grid_export_kw"] or 0) * interval_hours for r in rows), 2)
    })


@app.route("/api/tomorrow")
def tomorrow():
    from datetime import datetime, timedelta

    now = datetime.now().astimezone()
    c = db()

    days = []

    for offset in range(1, 8):
        target = now + timedelta(days=offset)
        target_date = target.strftime("%Y-%m-%d")

        rows = c.execute("""
            SELECT
                timestamp,
                temperature,
                apparent_temperature,
                cloud_cover,
                precipitation,
                shortwave_radiation,
                sunshine_duration,
                wind_speed
            FROM weather
            WHERE substr(timestamp,1,10) = ?
            ORDER BY timestamp
        """, (target_date,)).fetchall()

        rows = [dict(r) for r in rows]

        if not rows:
            days.append({
                "date": target_date,
                "available": False
            })
            continue

        temps = [float(r["temperature"] or 0) for r in rows]
        rain = [float(r["precipitation"] or 0) for r in rows]
        radiation = [float(r["shortwave_radiation"] or 0) for r in rows]

        daylight = [
            r for r in rows
            if float(r["shortwave_radiation"] or 0) > 20
        ]

        avg_cloud = (
            sum(float(r["cloud_cover"] or 0) for r in daylight)
            / len(daylight)
            if daylight else 0
        )

        irradiation = sum(radiation) / 1000.0

        # Baseline estimate only - ML will replace this later.
        pv_capacity_kw = 9.7
        performance_factor = 0.78

        estimated_pv = (
            irradiation *
            pv_capacity_kw *
            performance_factor
        )

        if estimated_pv >= 30:
            rating = "HIGH"
        elif estimated_pv >= 18:
            rating = "MODERATE"
        else:
            rating = "LOW"

        days.append({
            "available": True,
            "date": target_date,

            "summary": {
                "temperature_min": round(min(temps),1),
                "temperature_max": round(max(temps),1),
                "cloud_cover_daylight": round(avg_cloud,0),
                "rain_total": round(sum(rain),1),
                "peak_radiation": round(max(radiation),0),
                "solar_irradiation_kwh_m2": round(irradiation,2),
                "estimated_pv_kwh": round(estimated_pv,1),
                "solar_rating": rating
            },

            "hourly": rows
        })

    c.close()

    return jsonify({
        "available": any(x.get("available") for x in days),
        "days": days
    })


@app.route("/api/export-plan")
def export_plan():
    """
    Before 16:55:
        Today's export strategy is live.

    From 16:55 onward:
        Today's executable strategy is frozen from
        strategy_validation.

    Future-day plans remain live.
    """

    plan = calculate_export_plan()

    if not plan.get("available"):
        return jsonify(plan)

    now = datetime.now().astimezone()
    today = now.date().isoformat()

    # Final daily strategy becomes immutable at 16:55.
    freeze_time = now.replace(
        hour=16,
        minute=55,
        second=0,
        microsecond=0
    )

    if now >= freeze_time:

        c = db()

        frozen = c.execute("""
            SELECT *
            FROM strategy_validation
            WHERE plan_date = ?
            ORDER BY snapshot_timestamp DESC
            LIMIT 1
        """, (today,)).fetchone()

        c.close()

        if frozen:

            frozen = dict(frozen)

            for x in plan.get("plans", []):

                if x.get("date") != today:
                    continue

                # Freeze the values that formed the
                # executable daily strategy.
                x["full_day_solar_kwh"] = (
                    frozen["forecast_solar_kwh"]
                )

                x["remaining_solar_kwh"] = (
                    frozen["forecast_remaining_solar_kwh"]
                )

                full_solar = (
                    frozen["forecast_solar_kwh"] or 0
                )

                remaining_solar = (
                    frozen["forecast_remaining_solar_kwh"]
                    or 0
                )

                x["remaining_solar_percent"] = (
                    round(
                        remaining_solar /
                        full_solar *
                        100.0,
                        1
                    )
                    if full_solar > 0
                    else 0.0
                )

                x["starting_soc"] = (
                    frozen["predicted_start_soc"]
                )

                if frozen["predicted_start_soc"] is not None:
                    x["starting_soc_kwh"] = round(
                        frozen["predicted_start_soc"] /
                        100.0 *
                        42.0,
                        1
                    )

                x["pre_export_soc"] = (
                    frozen["predicted_pre_export_soc"]
                )

                x["midnight_soc"] = (
                    frozen["predicted_midnight_soc"]
                )

                x["next_solar_start_soc"] = (
                    frozen["predicted_next_solar_soc"]
                )

                x["recommended_export_percent"] = (
                    frozen["recommended_export_percent"]
                )

                x["recommended_export_kwh"] = (
                    frozen["recommended_export_kwh"]
                )

                export_kwh = (
                    frozen["recommended_export_kwh"]
                    or 0
                )

                x["export"] = (
                    "YES"
                    if export_kwh >= 0.5
                    else "NO"
                )

                x["predicted_load_kwh"] = (
                    frozen["forecast_load_kwh"]
                )

                # Keep all dashboard values consistent with
                # the frozen executable strategy.

                # Full-day solar rating.
                frozen_solar = (
                    frozen["forecast_solar_kwh"] or 0
                )

                x["solar_kwh"] = frozen_solar

                x["solar_rating"] = (
                    "HIGH"
                    if frozen_solar >= 30
                    else "MODERATE"
                    if frozen_solar >= 18
                    else "LOW"
                )

                # Target SOC shown on the summary card should
                # come from the frozen strategy, not a later
                # live recalculation.
                x["target_soc"] = (
                    frozen["predicted_midnight_soc"]
                )

                # Frozen premium export revenue.
                # Export tariff = 28 c/kWh.
                x["potential_revenue"] = round(
                    export_kwh * 28.0 / 100.0,
                    2
                )

                x["reason"] = (
                    "Final daily strategy frozen at "
                    "16:55 for the 17:05 FoxESS "
                    "execution window."
                )

                x["strategy_frozen"] = True
                x["strategy_snapshot_timestamp"] = (
                    frozen["snapshot_timestamp"]
                )

                break

    return jsonify(plan)


@app.route("/api/strategy-validation")
def strategy_validation():

    # Default dashboard window = last 7 validation days.
    # 14/30 day support is already available for the UI later.
    try:
        days = int(request.args.get("days", 7))
    except (TypeError, ValueError):
        days = 7

    days = max(1, min(days, 30))

    conn = sqlite3.connect("data/energy.db")
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT *
        FROM strategy_validation
        ORDER BY plan_date DESC
        LIMIT ?
    """, (days,)).fetchall()

    conn.close()

    records = [dict(r) for r in rows]

    if not records:
        return jsonify({
            "available": False,
            "mode": "SHADOW",
            "days": days,
            "latest": None,
            "records": [],
            "comparison_rows": []
        })

    comparison_rows = []

    for x in records:

        # ML prediction / recommendation
        ml = {
            "date": x["plan_date"],
            "type": "ML Prediction",

            "solar_kwh": x["forecast_solar_kwh"],

            # Load forecasts kept separately for validation.
            "load_kwh": x["ml_load_forecast_kwh"],
            "baseline_load_kwh": x["forecast_load_kwh"],
            "ml_load_kwh": x["ml_load_forecast_kwh"],
            "safe_ml_load_kwh": x["ml_safe_load_kwh"],
            "ml_load_model": x["ml_load_model"],

            "start_soc": x["predicted_start_soc"],
            "pre_export_soc": x["predicted_pre_export_soc"],

            "export_kwh": x["recommended_export_kwh"],
            "export_battery_percent":
                x["recommended_export_percent"],

            "end_export_soc":
                x["predicted_end_export_soc"],

            "next_solar_soc":
                x["predicted_next_solar_soc"],

            "overnight_load_kwh":
                x["predicted_overnight_load_kwh"],

            "battery_to_load_kwh":
                x["predicted_battery_to_load_kwh"],

            "grid_to_load_kwh":
                x["predicted_grid_to_load_kwh"],

            "grid_import_kwh":
                x["predicted_grid_import_kwh"],

            "soc_cutoff":
                x["predicted_end_export_soc"],

            "import_cost":
                x["predicted_import_cost"],

            "export_revenue":
                x["predicted_export_revenue"],

            "net_value":
                x["predicted_net_value"],

            "status": "PREDICTION"
        }

        # Actual FoxESS result
        actual = {
            "date": x["plan_date"],
            "type": "Actual",

            "solar_kwh": x["actual_solar_kwh"],
            "load_kwh": x["actual_load_kwh"],

            "baseline_load_kwh": None,
            "ml_load_kwh": None,
            "safe_ml_load_kwh": None,
            "ml_load_model": None,

            "start_soc": x["actual_start_soc"],
            "pre_export_soc": x["actual_pre_export_soc"],

            "export_kwh": x["actual_grid_export_kwh"],
            "export_battery_percent":
                x["actual_export_percent"],

            "end_export_soc":
                x["actual_end_export_soc"],

            "next_solar_soc":
                x["actual_next_solar_soc"],

            "overnight_load_kwh":
                x["actual_overnight_load_kwh"],

            "battery_to_load_kwh":
                x["actual_battery_to_load_kwh"],

            "grid_to_load_kwh":
                x["actual_grid_to_load_kwh"],

            "grid_import_kwh":
                x["actual_grid_import_kwh"],

            "soc_cutoff":
                x["foxess_cutoff_soc"],

            "import_cost":
                x["actual_import_cost"],

            "export_revenue":
                x["actual_export_revenue"],

            "net_value":
                x["actual_net_value"],

            "status": x["status"]
        }

        comparison_rows.append(ml)
        comparison_rows.append(actual)

    return jsonify({
        "available": True,
        "mode": "SHADOW",
        "days": days,

        # Retained for existing dashboard cards.
        "latest": records[0],
        "records": records,

        # New 7-day comparison table.
        "comparison_rows": comparison_rows
    })



@app.route("/api/ml-readiness")
def ml_readiness():
    try:
        return jsonify({
            "readiness": calculate_ml_readiness(),
            "risk": calculate_ml_risk()
        })
    except Exception as e:
        return jsonify({
            "readiness": {
                "ready": False,
                "status": "ERROR",
                "error": str(e)
            },
            "risk": {
                "available": False,
                "error": str(e)
            }
        }), 500


@app.route("/api/ml-performance")
def ml_performance():
    try:
        return jsonify(calculate_ml_performance())
    except Exception as e:
        return jsonify({
            "available": False,
            "status": "ERROR",
            "error": str(e)
        }), 500


@app.route("/api/load-ml-shadow")
def load_ml_shadow():
    try:
        return jsonify(predict_load_day())
    except Exception as e:
        return jsonify({
            "available": False,
            "status": "ERROR",
            "error": str(e)
        }), 500



@app.route("/api/economic-optimizer")
def economic_optimizer():
    """
    Economic Optimiser V2 dashboard API.

    The endpoint itself is read-only. The separately scheduled guarded
    executor applies frozen actions and verifies FoxESS read-back.
    """
    try:
        cached = economic_cache.get("payload")
        cache_age = time.monotonic() - economic_cache.get("created", 0.0)
        if cached is not None and cache_age < ECONOMIC_CACHE_SECONDS:
            return jsonify(cached)

        # Only one request may run the expensive joint search. Requests that
        # arrive during the first calculation wait here, then reuse its cache.
        economic_cache_lock.acquire()
        cached = economic_cache.get("payload")
        cache_age = time.monotonic() - economic_cache.get("created", 0.0)
        if cached is not None and cache_age < ECONOMIC_CACHE_SECONDS:
            economic_cache_lock.release()
            return jsonify(cached)

        hours, ml = build_economic_hours()

        if not hours:
            economic_cache_lock.release()
            return jsonify({
                "available": False,
                "mode": "SHADOW",
                "error": "No joined forecast hours available"
            })

        start_energy, start_soc = (
            economic_initial_energy()
        )

        result = optimise_horizon(
            hours,
            start_energy,
            max_passes=2,
        )

        final = result["final_result"]
        baseline = result["baseline_result"]

        today_string = datetime.now().date().isoformat()
        actual_today_conn = db()
        actual_today_solar = actual_today_conn.execute("""
            SELECT COALESCE(SUM(pv_kw) / 12.0, 0)
            FROM foxess_live
            WHERE substr(timestamp, 1, 10) = ?
        """, (today_string,)).fetchone()[0]
        today_shoulder_rows = [
            dict(row)
            for row in actual_today_conn.execute("""
                SELECT
                    timestamp,
                    battery_soc,
                    pv_kw,
                    load_kw,
                    grid_import_kw,
                    battery_charge_kw
                FROM foxess_live
                WHERE substr(timestamp, 1, 10) = ?
                  AND CAST(substr(timestamp, 12, 2) AS INTEGER) >= 10
                  AND CAST(substr(timestamp, 12, 2) AS INTEGER) < 17
                ORDER BY timestamp
            """, (today_string,)).fetchall()
        ]
        today_no_grid = replay_without_grid_charge(
            today_shoulder_rows
        )
        automation_table = actual_today_conn.execute("""
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name = 'automation_events'
        """).fetchone()[0]
        latest_automation = None
        if automation_table:
            event = actual_today_conn.execute("""
                SELECT event_time, phase, status, detail
                FROM automation_events
                WHERE plan_date = ?
                ORDER BY event_time DESC LIMIT 1
            """, (today_string,)).fetchone()
            if event:
                latest_automation = dict(event)
        actual_today_conn.close()

        # Today's actual SOC may already include a manual 10AM-2PM grid
        # charge.  Keep that real SOC for remaining operational decisions,
        # but rebuild the no-charge baseline from measured PV and load so the
        # manually imported energy is never mislabelled as solar.
        observed_grid_charge_ac = float(
            today_no_grid.get("grid_charge_ac", 0.0)
        )
        observed_grid_stored = float(
            today_no_grid.get("grid_stored", 0.0)
        )
        observed_grid_cost = observed_grid_charge_ac * 11.11 / 100.0
        counterfactual_start_energy = today_no_grid.get("energy")
        simulation_context = build_simulation_context(hours)

        if (
            counterfactual_start_energy is not None
            and observed_grid_charge_ac >= 0.05
        ):
            comparison_days = horizon_days(hours)
            no_action_charges = {
                comparison_day: 0.0
                for comparison_day in comparison_days
            }
            no_action_exports = dict(no_action_charges)
            no_action = simulate_economic(
                hours,
                float(counterfactual_start_energy),
                charges=no_action_charges,
                exports=no_action_exports,
                context=simulation_context,
            )
            baseline_pre10 = {
                comparison_day: float(
                    no_action["daily"][comparison_day].get(
                        "pre_10am_grid_import",
                        0.0,
                    )
                )
                for comparison_day in comparison_days
            }
            try:
                (
                    counterfactual_baseline_score,
                    counterfactual_baseline,
                    counterfactual_exports,
                ) = optimise_export_only_baseline(
                    hours,
                    float(counterfactual_start_energy),
                    comparison_days,
                    baseline_pre10,
                    simulation_context,
                    max_passes=2,
                )
                result["baseline_score"] = counterfactual_baseline_score
                result["baseline_result"] = counterfactual_baseline
                result["baseline_exports"] = counterfactual_exports
                result["baseline_type"] = (
                    "COUNTERFACTUAL_EXPORT_ONLY_EXCLUDES_OBSERVED_GRID_CHARGE"
                )
                baseline = counterfactual_baseline
            except RuntimeError:
                # Preserve a transparent no-action counterfactual if the
                # export-only strategy cannot meet the terminal reserve.
                result["baseline_score"] = no_action["planning_value"]
                result["baseline_result"] = no_action
                result["baseline_exports"] = no_action_exports
                result["baseline_type"] = (
                    "COUNTERFACTUAL_NO_ACTION_EXCLUDES_OBSERVED_GRID_CHARGE"
                )
                baseline = no_action

        days = []

        for day_index, day in enumerate(result["days"]):
            d = final["daily"][day]
            b = baseline["daily"][day]
            ml_day = ml.get(day, {})

            charge_kwh = float(result["charges"].get(day, 0))
            export_kwh = float(result["exports"].get(day, 0))
            start_day_energy = float(d.get("start_energy", 0))
            grid_charge_actual = float(d.get("grid_charge", 0))
            grid_stored = float(d.get("grid_stored", 0))
            premium_export_actual = float(d.get("premium_export", 0))
            solar_only_5pm = float(
                d.get("solar_only_5pm_energy", start_day_energy)
            )
            operational_solar_only_5pm = solar_only_5pm
            if (
                day == today_string
                and counterfactual_start_energy is not None
                and observed_grid_charge_ac >= 0.05
            ):
                solar_only_5pm = solar_only_5pm_energy(
                    simulation_context,
                    day,
                    float(counterfactual_start_energy),
                )
            required_post_export = float(
                d.get("required_post_export_energy", ECONOMIC_BATTERY_KWH * 0.10)
            )
            next_recharge_timestamp = d.get("next_recharge_timestamp")
            next_recharge_type = d.get(
                "next_recharge_type",
                "FORECAST_HORIZON",
            )
            export_battery_draw = float(
                d.get("premium_export_battery_draw", 0)
            )
            daily_degradation = float(d.get("degradation_cost", 0))
            daily_cash_value = (
                float(d.get("export_revenue", 0))
                - float(d.get("import_cost", 0))
                - daily_degradation
                - (
                    observed_grid_cost
                    if day == today_string
                    else 0.0
                )
            )
            baseline_daily_value = (
                float(b.get("export_revenue", 0))
                - float(b.get("import_cost", 0))
                - float(b.get("degradation_cost", 0))
            )
            load_point = ml_day.get("predicted_load_kwh")
            load_protected = ml_day.get("safe_load_kwh")
            load_buffer = ml_day.get("buffer_kwh")
            buffer_ratio = (
                float(load_buffer) / max(0.001, float(load_point))
                if load_buffer is not None and load_point is not None
                else None
            )
            if buffer_ratio is None:
                confidence = "Not yet rated"
            elif buffer_ratio <= 0.15:
                confidence = "High"
            elif buffer_ratio <= 0.35:
                confidence = "Moderate"
            else:
                confidence = "Cautious"
            next_solar = None
            if day_index + 1 < len(result["days"]):
                next_day = result["days"][day_index + 1]
                next_solar = final["daily"][next_day].get("solar", 0)

            if day == today_string and observed_grid_charge_ac >= 0.05:
                action_reason = (
                    f"FoxESS observed {observed_grid_charge_ac:.2f} kWh of grid energy "
                    f"used for battery charging today. Without that grid energy, "
                    f"the battery is estimated to reach {solar_only_5pm / ECONOMIC_BATTERY_KWH * 100:.0f}% "
                    "at 5PM. Remaining decisions use the actual live SOC, while "
                    "the no-charge comparison excludes the manual import and its cost."
                )
            elif charge_kwh > 0 and export_kwh > 0:
                action_reason = (
                    f"Solar alone reaches {solar_only_5pm / ECONOMIC_BATTERY_KWH * 100:.0f}% "
                    "at 5PM, so buy only the remaining profitable shortfall; "
                    f"retain {required_post_export / ECONOMIC_BATTERY_KWH * 100:.0f}% "
                    "after export for forecast demand."
                )
            elif charge_kwh > 0:
                action_reason = (
                    f"Solar alone reaches {solar_only_5pm / ECONOMIC_BATTERY_KWH * 100:.0f}% "
                    "at 5PM; buy only the battery shortfall needed to avoid "
                    "higher-cost later imports and protect the forecast reserve."
                )
            elif export_kwh > 0:
                action_reason = (
                    "Solar can supply the planned battery level without cheap "
                    f"grid charging; export only above the {required_post_export / ECONOMIC_BATTERY_KWH * 100:.0f}% "
                    "forecast reserve."
                )
            elif next_solar is not None and next_solar >= 35:
                action_reason = (
                    "Retain energy overnight; stronger solar is forecast "
                    "tomorrow, so no grid charge or forced export adds value."
                )
            else:
                action_reason = (
                    "Retain energy for household load and forecast safety; "
                    "no safe charge/export pair improves whole-horizon value."
                )

            pre_export = d.get(
                "pre_export_energy"
            )

            end_energy = d.get(
                "end_energy"
            )

            days.append({
                "date": day,

                "automation_status": (
                    latest_automation["status"] + " · " +
                    latest_automation["phase"].upper()
                    if day == today_string and latest_automation
                    else (
                        "ENABLED · NEXT RUN"
                        if day == today_string
                        else "SCHEDULED"
                    )
                ),

                "solar_kwh":
                    round(
                        d.get("solar_point", d.get("solar", 0))
                        + (
                            float(actual_today_solar)
                            if day == today_string
                            else 0.0
                        ),
                        2
                    ),

                "solar_basis": (
                    "ACTUAL_SO_FAR_PLUS_REMAINING_FORECAST"
                    if day == today_string
                    else "FULL_DAY_FORECAST"
                ),

                "protected_solar_kwh": round(
                    d.get("solar", 0)
                    + (
                        float(actual_today_solar)
                        if day == today_string
                        else 0.0
                    ),
                    2,
                ),

                "ml_load_kwh":
                    load_point,

                "safe_ml_load_kwh":
                    load_protected,

                "load_buffer_kwh": load_buffer,

                "confidence": confidence,

                "battery_start_kwh": round(start_day_energy, 2),

                "battery_start_soc": round(
                    start_day_energy / ECONOMIC_BATTERY_KWH * 100,
                    1,
                ),

                "solar_only_5pm_kwh": round(solar_only_5pm, 2),

                "solar_only_5pm_soc": round(
                    solar_only_5pm / ECONOMIC_BATTERY_KWH * 100,
                    1,
                ),

                "operational_solar_only_5pm_kwh": round(
                    operational_solar_only_5pm,
                    2,
                ),

                "observed_grid_charge_kwh": round(
                    observed_grid_charge_ac
                    if day == today_string
                    else 0.0,
                    2,
                ),

                "observed_grid_stored_kwh": round(
                    observed_grid_stored
                    if day == today_string
                    else 0.0,
                    2,
                ),

                "observed_grid_charge_cost": round(
                    observed_grid_cost
                    if day == today_string
                    else 0.0,
                    2,
                ),

                "solar_only_basis": (
                    "COUNTERFACTUAL_EXCLUDES_OBSERVED_GRID_CHARGE"
                    if day == today_string and observed_grid_charge_ac >= 0.05
                    else "NO_PLANNED_GRID_CHARGE"
                ),

                "grid_charge_cap_kwh": round(
                    d.get("grid_charge_cap_ac", 0),
                    2,
                ),

                "required_reserve_kwh": round(required_post_export, 2),

                "required_reserve_soc": round(
                    required_post_export / ECONOMIC_BATTERY_KWH * 100,
                    1,
                ),

                "forecast_draw_to_recharge_kwh": round(
                    d.get("forecast_draw_to_recovery", 0),
                    2,
                ),

                "next_recharge_timestamp": next_recharge_timestamp,

                "next_recharge_type": next_recharge_type,

                "baseline_export_kwh": round(
                    b.get("premium_export", 0),
                    2,
                ),

                "recommended_charge_kwh":
                    round(
                        charge_kwh,
                        2
                    ),

                "actual_simulated_charge_kwh":
                    round(grid_charge_actual, 2),

                "stored_from_grid_kwh": round(grid_stored, 2),

                "stored_from_grid_pct": round(
                    grid_stored / ECONOMIC_BATTERY_KWH * 100,
                    1,
                ),

                "recommended_export_kwh":
                    round(
                        export_kwh,
                        2
                    ),

                "simulated_premium_export_kwh":
                    round(premium_export_actual, 2),

                "battery_used_for_export_kwh": round(
                    export_battery_draw,
                    2,
                ),

                "battery_used_for_export_pct": round(
                    export_battery_draw / ECONOMIC_BATTERY_KWH * 100,
                    1,
                ),

                "natural_export_kwh":
                    round(
                        d.get(
                            "natural_export", 0
                        ),
                        2
                    ),

                "soc_5pm":
                    None
                    if pre_export is None
                    else round(
                        pre_export
                        / ECONOMIC_BATTERY_KWH
                        * 100,
                        1
                    ),

                "battery_5pm_kwh": None
                    if pre_export is None
                    else round(pre_export, 2),

                "end_soc":
                    None
                    if end_energy is None
                    else round(
                        end_energy
                        / ECONOMIC_BATTERY_KWH
                        * 100,
                        1
                    ),

                "battery_end_kwh": None
                    if end_energy is None
                    else round(end_energy, 2),

                "pre_10am_import_kwh":
                    round(
                        d.get(
                            "pre_10am_grid_import",
                            0
                        ),
                        3
                    ),

                "grid_import_kwh":
                    round(
                        d.get("grid_import", 0)
                        + (
                            observed_grid_charge_ac
                            if day == today_string
                            else 0.0
                        ),
                        2
                    ),

                "household_import_kwh": round(
                    max(0.0, d.get("grid_import", 0) - grid_charge_actual),
                    2,
                ),

                "import_cost":
                    round(
                        d.get("import_cost", 0)
                        + (
                            observed_grid_cost
                            if day == today_string
                            else 0.0
                        ),
                        2
                    ),

                "export_revenue":
                    round(
                        d.get(
                            "export_revenue", 0
                        ),
                        2
                    ),

                "degradation_cost": round(daily_degradation, 2),

                "net_value":
                    round(daily_cash_value, 2),

                "baseline_net_value": round(baseline_daily_value, 2),

                "daily_improvement": round(
                    daily_cash_value - baseline_daily_value,
                    2,
                ),

                "baseline_end_soc":
                    round(
                        b.get("end_energy", 0)
                        / ECONOMIC_BATTERY_KWH
                        * 100,
                        1
                    ),

                "reason": (
                    f"{action_reason} Solar-first case: {confidence.lower()} "
                    f"load confidence with {float(load_buffer or 0):.1f} kWh "
                    f"forecast buffer. Next recharge: "
                    f"{next_recharge_type.replace('_', ' ').lower()}"
                    + (
                        f" at {next_recharge_timestamp[11:16]}."
                        if next_recharge_timestamp
                        else "."
                    )
                ),
            })

        baseline_value = float(
            result["baseline_score"]
        )

        optimised_value = float(
            result["final_score"]
        ) - observed_grid_cost
        baseline_cash_value = float(baseline.get("net_value", 0))
        optimised_cash_value = (
            float(final.get("net_value", 0))
            - observed_grid_cost
        )
        planning_improvement = optimised_value - baseline_value
        cash_improvement = optimised_cash_value - baseline_cash_value

        switches = get_switches(DB)
        control_mode = os.getenv("FOXESS_CONTROL_MODE", "rule").strip().lower()
        joint_live = control_mode == "joint"

        payload = {
            "available": True,
            "mode": "JOINT_LIVE_GUARDED" if joint_live else "JOINT_SHADOW_RULE_BASED_LIVE",
            "control_enabled": joint_live and switches["master_enabled"],
            "telegram_notifications": True,
            "automation": {
                "control_strategy": "JOINT_OPTIMISER" if joint_live else "RULE_BASED",
                "cheap_charge_enabled": (
                    joint_live
                    and switches["master_enabled"]
                    and switches["charge_enabled"]
                ),
                "premium_export_enabled": (
                    joint_live
                    and switches["master_enabled"]
                    and switches["export_enabled"]
                ),
                "charge_run": "09:55" if joint_live else None,
                "snapshot_run": "09:45 / 16:45" if joint_live else "16:55",
                "export_run": "16:55" if joint_live else "17:00",
                "switches": switches,
                "latest_event": latest_automation,
            },

            "tariffs": {
                "shoulder_buy_cents": 11.11,
                "premium_fit_cents": 28.0,
                "shoulder_window": "10:00-14:00",
                "premium_export_window": "17:00-21:00"
            },

            "starting_soc":
                round(start_soc, 1),

            "battery_capacity_kwh": ECONOMIC_BATTERY_KWH,

            "assumptions": {
                "charge_efficiency_percent": round(ECONOMIC_CHARGE_EFF * 100, 1),
                "discharge_efficiency_percent": round(ECONOMIC_DISCHARGE_EFF * 100, 1),
                "battery_floor_percent": 10.0,
                "degradation_cents_per_battery_kwh": round(
                    DEGRADATION_COST_CENTS_PER_BATTERY_KWH,
                    2,
                ),
                "solar_protection_percent": round(
                    SOLAR_PROTECTION_FACTOR * 100,
                    1,
                ),
                "terminal_energy_value_cents_per_kwh": round(
                    TERMINAL_ENERGY_VALUE_CENTS_PER_KWH,
                    2,
                ),
            },

            "baseline_value":
                round(baseline_cash_value, 2),

            "baseline_planning_value": round(baseline_value, 2),

            "baseline_type":
                result.get(
                    "baseline_type",
                    "SAFE_EXPORT_ONLY"
                ),

            "baseline_label":
                "Export without observed or planned cheap charging",

            "observed_grid_charge_kwh": round(
                observed_grid_charge_ac,
                2,
            ),

            "observed_grid_stored_kwh": round(
                observed_grid_stored,
                2,
            ),

            "observed_grid_charge_cost": round(
                observed_grid_cost,
                2,
            ),

            "optimised_value":
                round(optimised_cash_value, 2),

            "optimised_planning_value": round(optimised_value, 2),

            "projected_improvement":
                round(planning_improvement, 2),

            "cash_improvement": round(cash_improvement, 2),

            "terminal_value_difference": round(
                float(final.get("terminal_value", 0))
                - float(baseline.get("terminal_value", 0)),
                2,
            ),

            "search_method": result.get("search_method"),

            "total_grid_charge_kwh":
                round(
                    final.get(
                        "grid_charge", 0
                    ) + observed_grid_charge_ac,
                    2
                ),

            "total_grid_import_kwh":
                round(
                    final.get(
                        "grid_import", 0
                    ) + observed_grid_charge_ac,
                    2
                ),

            "total_grid_export_kwh":
                round(
                    final.get(
                        "grid_export", 0
                    ),
                    2
                ),

            "total_premium_export_kwh":
                round(
                    sum(
                        d.get("premium_export", 0)
                        for d in final["daily"].values()
                    ),
                    2
                ),

            "days": days,
            "cache_seconds": ECONOMIC_CACHE_SECONDS,
        }

        economic_cache["created"] = time.monotonic()
        economic_cache["payload"] = payload
        economic_cache_lock.release()
        return jsonify(payload)

    except Exception as e:
        if economic_cache_lock.locked():
            economic_cache_lock.release()
        return jsonify({
            "available": False,
            "mode": "JOINT_SHADOW",
            "control_enabled": False,
            "error": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080)
