"""Apply the frozen joint plan to verified FoxESS scheduler periods."""

import argparse
import json
import os
import sqlite3
import time
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from foxess import foxess_post, get_device
from telegram_notify import send_telegram

DB = "data/energy.db"
TZ = ZoneInfo("Australia/Sydney")
GET_PATH = "/op/v3/device/scheduler/get"
WRITE_PATH = "/op/v3/device/scheduler/enable"
MIN_SOC = 10.0
MAX_POWER_W = 10000.0


def record_event(now, phase, status, detail, plan=None):
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS automation_events (
            event_time TEXT NOT NULL,
            plan_date TEXT NOT NULL,
            phase TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT NOT NULL,
            charge_kwh REAL,
            export_kwh REAL,
            PRIMARY KEY (event_time, phase)
        )
    """)
    conn.execute("""
        INSERT INTO automation_events VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        now.isoformat(timespec="seconds"), now.date().isoformat(), phase,
        status, detail,
        None if plan is None else plan.get("charge_kwh"),
        None if plan is None else plan.get("export_kwh"),
    ))
    conn.commit()
    conn.close()


def latest_plan(now):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT * FROM economic_plan_actions
        WHERE plan_date = ? ORDER BY created_at DESC LIMIT 1
    """, (now.date().isoformat(),)).fetchone()
    conn.close()
    if not row:
        raise RuntimeError("No frozen joint plan for today")
    plan = dict(row)
    created = datetime.fromisoformat(plan["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=TZ)
    if (now - created.astimezone(TZ)).total_seconds() > 6 * 3600:
        raise RuntimeError("Joint plan is over six hours old")
    return plan


def latest_soc():
    conn = sqlite3.connect(DB)
    row = conn.execute("""
        SELECT timestamp, battery_soc FROM foxess_live
        WHERE battery_soc IS NOT NULL ORDER BY id DESC LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        raise RuntimeError("Current battery SOC unavailable")
    return row[0], float(row[1])


def base_extra(groups):
    for group in groups:
        extra = group.get("extraParam")
        if isinstance(extra, dict):
            result = deepcopy(extra)
            result.update({
                "minSocOnGrid": MIN_SOC,
                "fdPwr": MAX_POWER_W,
                "secondWorkMode": "SelfUse",
            })
            return result
    raise RuntimeError("No scheduler parameter template available")


def managed(group):
    slot = (
        int(group.get("startHour", -1)), int(group.get("startMinute", -1)),
        int(group.get("endHour", -1)), int(group.get("endMinute", -1)),
    )
    return slot in {(10, 0, 14, 0), (17, 0, 21, 0), (17, 5, 22, 55)}


def desired_groups(existing, plan, phase):
    groups = [deepcopy(g) for g in existing if not managed(g)]
    template = base_extra(existing)

    charge = float(plan["charge_kwh"])
    if charge >= 0.5:
        extra = deepcopy(template)
        extra["maxSoc"] = round(max(MIN_SOC, min(100.0, float(plan["charge_target_soc"]))), 1)
        extra["fdSoc"] = MIN_SOC
        groups.append({
            "startHour": 10, "startMinute": 0,
            "endHour": 14, "endMinute": 0,
            "workMode": "ForceCharge", "extraParam": extra,
        })

    export = float(plan["export_kwh"])
    if export >= 0.5:
        extra = deepcopy(template)
        cutoff = float(plan["export_cutoff_soc"])
        if phase == "export":
            _, soc = latest_soc()
            battery_draw_pct = export / 0.95 / 42.0 * 100.0
            cutoff = soc - battery_draw_pct
        extra["fdSoc"] = round(max(MIN_SOC, min(100.0, cutoff)), 1)
        extra["maxSoc"] = 100.0
        groups.append({
            "startHour": 17, "startMinute": 0,
            "endHour": 21, "endMinute": 0,
            "workMode": "ForceDischarge", "extraParam": extra,
        })
    return groups


def backup(payload, now):
    folder = "data/foxess_schedule_backups"
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, now.strftime("joint_%Y%m%d_%H%M%S.json"))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=("charge", "export"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    now = datetime.now(TZ)
    plan = latest_plan(now)
    stamp, soc = latest_soc()
    if not MIN_SOC <= soc <= 100.0:
        raise RuntimeError(f"Unsafe live SOC value: {soc}")

    device = get_device()
    current = foxess_post(GET_PATH, {"deviceSN": device["deviceSN"]})
    if not current or current.get("errno") != 0:
        raise RuntimeError(f"Scheduler read failed: {current}")
    result = current.get("result") or {}
    if int(result.get("enable", 0)) != 1:
        raise RuntimeError("FoxESS scheduler is disabled")
    existing = result.get("groups") or []
    groups = desired_groups(existing, plan, args.phase)
    print(json.dumps({
        "phase": args.phase, "live_soc": soc, "live_timestamp": stamp,
        "charge_kwh": plan["charge_kwh"], "export_kwh": plan["export_kwh"],
        "groups": groups,
    }, indent=2))
    if args.dry_run:
        print("DRY RUN: no FoxESS write")
        return

    path = backup(current, now)
    response = foxess_post(WRITE_PATH, {"deviceSN": device["deviceSN"], "groups": groups})
    if not response or response.get("errno") != 0:
        raise RuntimeError(f"Scheduler write failed: {response}; backup={path}")
    expected_slots = sorted(
        (g["startHour"], g["startMinute"], g["endHour"], g["endMinute"], g["workMode"])
        for g in groups if managed(g)
    )
    actual_slots = []
    # FoxESS commonly acknowledges the write before its read endpoint has
    # converged. Retry bounded read-back instead of reporting a false failure.
    for _ in range(4):
        time.sleep(12)
        verify = foxess_post(GET_PATH, {"deviceSN": device["deviceSN"]})
        if not verify or verify.get("errno") != 0:
            continue
        actual = verify.get("result", {}).get("groups") or []
        actual_slots = sorted(
            (g["startHour"], g["startMinute"], g["endHour"], g["endMinute"], g["workMode"])
            for g in actual if managed(g)
        )
        if actual_slots == expected_slots:
            break
    if actual_slots != expected_slots:
        raise RuntimeError(f"Scheduler verification mismatch after retries: {actual_slots} != {expected_slots}")
    print(f"SUCCESS: FoxESS joint schedule verified; backup={path}")
    detail = (
        f"FoxESS verified {args.phase}; charge={float(plan['charge_kwh']):.1f} kWh, "
        f"export={float(plan['export_kwh']):.1f} kWh, live SOC={soc:.1f}%"
    )
    record_event(now, args.phase, "VERIFIED", detail, plan)
    send_telegram(
        "⚡ Smart Electricity - JOINT PLAN VERIFIED\n\n"
        f"Date: {now.date().isoformat()}\n"
        f"Phase: {args.phase.upper()}\n"
        "✅ FoxESS schedule read-back verified.\n\n"
        "Detailed energy and battery values remain available only on the private dashboard."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        now = datetime.now(TZ)
        phase = "unknown"
        if "--phase" in os.sys.argv:
            index = os.sys.argv.index("--phase")
            if index + 1 < len(os.sys.argv):
                phase = os.sys.argv[index + 1]
        detail = f"{type(exc).__name__}: {exc}"
        if "--dry-run" not in os.sys.argv:
            record_event(now, phase, "FAILED", detail)
            send_telegram(
                "🚨 Smart Electricity - JOINT AUTOMATION FAILED\n\n"
                f"Date: {now.date().isoformat()}\n"
                f"Phase: {phase.upper()}\n"
                "FoxESS control was not successfully verified.\n\n"
                "Review the private dashboard and VM log for details."
            )
        raise
