"""Apply a fresh joint optimiser plan to guarded FoxESS scheduler periods."""

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
BATTERY_KWH = 42.0
DISCHARGE_EFFICIENCY = 0.95
PLAN_MAX_AGE_MINUTES = 20
SOC_MAX_AGE_MINUTES = 10
VERIFY_ATTEMPTS = 4
VERIFY_WAIT_SECONDS = 12
MANAGED_SLOTS = {
    (10, 0, 14, 0),
    (17, 0, 21, 0),
    (17, 5, 22, 55),
}


def parse_timestamp(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp.replace(tzinfo=TZ) if stamp.tzinfo is None else stamp.astimezone(TZ)


def age_minutes(now, value):
    return (now - parse_timestamp(value)).total_seconds() / 60.0


def notify(message):
    """Telegram is useful but must never decide inverter control success."""
    try:
        send_telegram(message)
    except Exception as exc:
        print(f"WARNING: Telegram notification failed: {type(exc).__name__}: {exc}")


def record_event(now, phase, status, detail, plan=None, db_path=DB):
    conn = sqlite3.connect(db_path)
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
    conn.execute("INSERT INTO automation_events VALUES (?, ?, ?, ?, ?, ?, ?)", (
        now.isoformat(timespec="seconds"), now.date().isoformat(), phase,
        status, detail,
        None if plan is None else plan.get("charge_kwh"),
        None if plan is None else plan.get("export_kwh"),
    ))
    conn.commit()
    conn.close()


def latest_plan(now, db_path=DB):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT * FROM economic_plan_actions
        WHERE plan_date = ? ORDER BY created_at DESC LIMIT 1
    """, (now.date().isoformat(),)).fetchone()
    conn.close()
    if not row:
        raise RuntimeError("No frozen joint plan for today")
    plan = dict(row)
    age = age_minutes(now, plan["created_at"])
    if age < -2:
        raise RuntimeError("Joint plan timestamp is in the future")
    if age > PLAN_MAX_AGE_MINUTES:
        raise RuntimeError(f"Joint plan is stale: {age:.1f} minutes old")
    return plan


def latest_soc(now, db_path=DB):
    conn = sqlite3.connect(db_path)
    row = conn.execute("""
        SELECT timestamp, battery_soc FROM foxess_live
        WHERE battery_soc IS NOT NULL ORDER BY id DESC LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        raise RuntimeError("Current battery SOC unavailable")
    age = age_minutes(now, row[0])
    if age < -2:
        raise RuntimeError("Live SOC timestamp is in the future")
    if age > SOC_MAX_AGE_MINUTES:
        raise RuntimeError(f"Live SOC is stale: {age:.1f} minutes old")
    soc = float(row[1])
    if not MIN_SOC <= soc <= 100.0:
        raise RuntimeError(f"Unsafe live SOC value: {soc}")
    return row[0], soc


def required_reserve_soc(plan):
    """Recover the optimiser's protected post-export reserve."""
    reserve = float(plan.get("export_cutoff_soc") or MIN_SOC)
    try:
        payload = json.loads(plan.get("plan_json") or "{}")
        daily = payload.get("daily") or {}
        if daily.get("required_post_export_soc") is not None:
            reserve = max(reserve, float(daily["required_post_export_soc"]))
        elif daily.get("required_post_export_energy") is not None:
            reserve = max(
                reserve,
                float(daily["required_post_export_energy"]) / BATTERY_KWH * 100.0,
            )
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return max(MIN_SOC, min(100.0, reserve))


def slot(group):
    return (
        int(group.get("startHour", -1)), int(group.get("startMinute", -1)),
        int(group.get("endHour", -1)), int(group.get("endMinute", -1)),
    )


def managed(group):
    return slot(group) in MANAGED_SLOTS


def base_extra(groups):
    for group in groups:
        extra = group.get("extraParam")
        if isinstance(extra, dict):
            result = deepcopy(extra)
            if float(result.get("fdPwr") or 0) <= 0:
                raise RuntimeError("Scheduler template has no verified discharge power")
            result.update({"minSocOnGrid": MIN_SOC, "secondWorkMode": "SelfUse"})
            return result
    raise RuntimeError("No scheduler parameter template available")


def desired_groups(existing, plan, phase, live_soc):
    """Return the complete schedule, preserving all non-owned periods."""
    groups = [deepcopy(group) for group in existing if not managed(group)]
    if phase == "watchdog":
        return groups

    template = base_extra(existing)
    if phase == "charge":
        charge = float(plan["charge_kwh"])
        target = max(MIN_SOC, min(100.0, float(plan["charge_target_soc"])))
        if charge >= 0.5 and target > live_soc + 0.5:
            extra = deepcopy(template)
            extra.update({"maxSoc": round(target, 1), "fdSoc": MIN_SOC})
            groups.append({
                "startHour": 10, "startMinute": 0,
                "endHour": 14, "endMinute": 0,
                "workMode": "ForceCharge", "extraParam": extra,
            })
        return groups

    export = float(plan["export_kwh"])
    reserve = required_reserve_soc(plan)
    planned_cutoff = float(plan["export_cutoff_soc"])
    live_cutoff = live_soc - export / DISCHARGE_EFFICIENCY / BATTERY_KWH * 100.0
    cutoff = round(max(MIN_SOC, reserve, planned_cutoff, live_cutoff), 1)
    if export >= 0.5 and cutoff < live_soc - 0.5:
        extra = deepcopy(template)
        extra.update({"fdSoc": cutoff, "maxSoc": 100.0})
        groups.append({
            "startHour": 17, "startMinute": 0,
            "endHour": 21, "endMinute": 0,
            "workMode": "ForceDischarge", "extraParam": extra,
        })
    return groups


def group_signature(groups):
    """Stable comparison tolerant of FoxESS group ordering."""
    def normalise(value):
        if isinstance(value, dict):
            return {key: normalise(item) for key, item in sorted(value.items())}
        if isinstance(value, list):
            return [normalise(item) for item in value]
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return round(float(value), 4)
        return value

    return sorted(
        json.dumps(normalise(group), sort_keys=True, separators=(",", ":"))
        for group in groups
    )


def backup(payload, now):
    folder = "data/foxess_schedule_backups"
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, now.strftime("joint_%Y%m%d_%H%M%S.json"))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return path


def read_schedule(device_sn):
    response = foxess_post(GET_PATH, {"deviceSN": device_sn})
    if not response or response.get("errno") != 0:
        raise RuntimeError(f"Scheduler read failed: {response}")
    result = response.get("result") or {}
    if int(result.get("enable", 0)) != 1:
        raise RuntimeError("FoxESS scheduler is disabled")
    return response, result.get("groups") or []


def verify_schedule(device_sn, expected, attempts=VERIFY_ATTEMPTS, wait=VERIFY_WAIT_SECONDS):
    expected_signature = group_signature(expected)
    for attempt in range(attempts):
        if wait:
            time.sleep(wait)
        try:
            _, actual = read_schedule(device_sn)
        except RuntimeError:
            if attempt + 1 == attempts:
                raise
            continue
        if group_signature(actual) == expected_signature:
            return actual
    raise RuntimeError("Scheduler verification mismatch after bounded retries")


def write_schedule(device_sn, groups):
    response = foxess_post(WRITE_PATH, {"deviceSN": device_sn, "groups": groups})
    if not response or response.get("errno") != 0:
        raise RuntimeError(f"Scheduler write failed: {response}")


def apply_schedule(device_sn, current_payload, existing, desired, now):
    if group_signature(existing) == group_signature(desired):
        return "already matched", None
    path = backup(current_payload, now)
    try:
        write_schedule(device_sn, desired)
        verify_schedule(device_sn, desired)
        return "written and verified", path
    except Exception as original_error:
        try:
            write_schedule(device_sn, existing)
            verify_schedule(device_sn, existing)
        except Exception as rollback_error:
            raise RuntimeError(
                f"Schedule change failed and rollback failed: {original_error}; "
                f"rollback={rollback_error}; backup={path}"
            ) from rollback_error
        raise RuntimeError(
            f"Schedule change failed; original schedule restored: {original_error}; backup={path}"
        ) from original_error


def validate_device(device):
    device_sn = device.get("deviceSN")
    if not device_sn:
        raise RuntimeError("FoxESS device serial number missing")
    status = str(device.get("status", "")).strip().lower()
    if status in {"0", "offline", "fault", "error"}:
        raise RuntimeError(f"FoxESS device is not available: {status}")
    return device_sn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=("charge", "export", "watchdog"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    now = datetime.now(TZ)
    plan = None if args.phase == "watchdog" else latest_plan(now)
    stamp, soc = latest_soc(now)

    control_mode = os.getenv("FOXESS_CONTROL_MODE", "rule").strip().lower()
    if not args.dry_run and control_mode != "joint":
        raise RuntimeError(
            f"Joint write blocked: FOXESS_CONTROL_MODE is {control_mode!r}, expected 'joint'"
        )

    device_sn = validate_device(get_device())
    current, existing = read_schedule(device_sn)
    groups = desired_groups(existing, plan, args.phase, soc)
    print(json.dumps({
        "phase": args.phase,
        "live_soc": soc,
        "live_timestamp": stamp,
        "plan_created_at": None if plan is None else plan["created_at"],
        "charge_kwh": None if plan is None else plan["charge_kwh"],
        "export_kwh": None if plan is None else plan["export_kwh"],
        "managed_groups": [group for group in groups if managed(group)],
        "changed": group_signature(existing) != group_signature(groups),
    }, indent=2))
    if args.dry_run:
        print("DRY RUN: no FoxESS write")
        return

    outcome, path = apply_schedule(device_sn, current, existing, groups, now)
    detail = (
        f"FoxESS {args.phase} schedule {outcome}; live SOC={soc:.1f}%; "
        f"backup={path or 'not needed'}"
    )
    record_event(now, args.phase, "VERIFIED", detail, plan)
    notify(
        "⚡ Smart Electricity - DAILY CONTROL VERIFIED\n\n"
        f"Date: {now.date().isoformat()}\n"
        f"Phase: {args.phase.upper()}\n"
        f"Live battery: {soc:.1f}%\n"
        f"Result: {outcome}.\n\n"
        "✅ FoxESS scheduler read-back verified."
    )
    print(f"SUCCESS: {detail}")


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
            notify(
                "🚨 Smart Electricity - DAILY CONTROL FAILED\n\n"
                f"Date: {now.date().isoformat()}\n"
                f"Phase: {phase.upper()}\n"
                f"Reason: {detail}\n\n"
                "FoxESS control was not successfully verified."
            )
        raise
