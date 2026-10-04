"""Freeze the joint optimiser's current seven-day plan for execution/audit."""

import json
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import economic_optimizer_v2 as optimiser

DB = "data/energy.db"
TZ = ZoneInfo("Australia/Sydney")
DB_BUSY_TIMEOUT_MS = 60000
DB_WRITE_ATTEMPTS = 6


def write_with_retry(callback, db_path=DB):
    """Commit one atomic plan update despite brief live-collector overlap."""
    for attempt in range(DB_WRITE_ATTEMPTS):
        conn = sqlite3.connect(
            db_path,
            timeout=DB_BUSY_TIMEOUT_MS / 1000.0,
        )
        conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        try:
            # WAL is persistent for this database and lets dashboard readers
            # continue while telemetry or a new plan is being committed.
            conn.execute("PRAGMA journal_mode = WAL")
            callback(conn)
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" not in str(exc).lower() or attempt + 1 == DB_WRITE_ATTEMPTS:
                raise
            time.sleep(min(5, attempt + 1))
        finally:
            conn.close()


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS economic_plan_actions (
            plan_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            model_version TEXT NOT NULL,
            solar_kwh REAL NOT NULL,
            point_load_kwh REAL NOT NULL,
            safe_load_kwh REAL NOT NULL,
            charge_kwh REAL NOT NULL,
            charge_target_soc REAL NOT NULL,
            expected_5pm_soc REAL NOT NULL,
            export_kwh REAL NOT NULL,
            export_cutoff_soc REAL NOT NULL,
            expected_end_soc REAL NOT NULL,
            grid_import_kwh REAL NOT NULL,
            net_value REAL NOT NULL,
            baseline_value REAL NOT NULL,
            reason TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            PRIMARY KEY (plan_date, created_at)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_economic_plan_latest
        ON economic_plan_actions(plan_date, created_at DESC)
    """)


def build_plan(now=None):
    now = now or datetime.now(TZ)
    hours, ml = optimiser.build_hours(now=now)
    if not hours:
        raise RuntimeError(
            "No joined solar/load forecast hours; no joint plan was saved"
        )
    start_energy, start_soc = optimiser.initial_energy()
    result = optimiser.optimise_horizon(hours, start_energy)
    if result["final_score"] is None or not result.get("days"):
        raise RuntimeError("No safe joint strategy found")

    created = now.isoformat(timespec="seconds")
    def persist(conn):
        ensure_schema(conn)
        for day in result["days"]:
            d = result["final_result"]["daily"][day]
            forecast = ml[day]
            charge = float(result["charges"][day])
            export = float(result["exports"][day])
            post_shoulder = float(
                d.get("post_shoulder_energy") or d["pre_export_energy"]
            )
            pre_export = float(d["pre_export_energy"])
            end_energy = float(d["end_energy"])
            cutoff_energy = max(
                optimiser.MIN_KWH,
                pre_export - export / optimiser.DISCHARGE_EFF,
            )
            if charge > 0 and export > 0:
                reason = "Buy cheap energy; export only the safe premium surplus."
            elif charge > 0:
                reason = "Buy cheap energy to avoid later imports and protect reserve."
            elif export > 0:
                reason = "Export safe surplus while retaining the calibrated reserve."
            else:
                reason = "Retain energy for load and forecast uncertainty."
            payload = {
                "start_soc": start_soc,
                "charge_kwh": charge,
                "export_kwh": export,
                "daily": d,
                "load": forecast,
            }
            conn.execute("""
                INSERT INTO economic_plan_actions VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                day, created, "load_candidate_v2/calibrated-safe-v1",
                d["solar"], forecast["predicted_load_kwh"],
                forecast["safe_load_kwh"], charge,
                post_shoulder / optimiser.BATTERY_KWH * 100.0,
                pre_export / optimiser.BATTERY_KWH * 100.0,
                export, cutoff_energy / optimiser.BATTERY_KWH * 100.0,
                end_energy / optimiser.BATTERY_KWH * 100.0,
                d["grid_import"], result["final_score"],
                result["baseline_score"], reason,
                json.dumps(payload, default=str),
            ))

    write_with_retry(persist)
    return created, result


if __name__ == "__main__":
    created, result = build_plan()
    print(f"joint plan frozen at {created}")
    for day in result["days"]:
        d = result["final_result"]["daily"][day]
        print(
            day,
            f"charge={result['charges'][day]:.1f}kWh",
            f"export={result['exports'][day]:.1f}kWh",
            f"end_soc={d['end_energy'] / optimiser.BATTERY_KWH * 100:.1f}%",
        )
