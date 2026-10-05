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
from control_switches import get_switches, phase_enabled

DB = "data/energy.db"
TZ = ZoneInfo("Australia/Sydney")
GET_PATH = "/op/v3/device/scheduler/get"
WRITE_PATH = "/op/v3/device/scheduler/enable"
MIN_SOC = 10.0
BATTERY_KWH = 42.0
CHARGE_EFFICIENCY = 0.95
DISCHARGE_EFFICIENCY = 0.95
PLAN_MAX_AGE_MINUTES = 20
SOC_MAX_AGE_MINUTES = 10
VERIFY_ATTEMPTS = 4
VERIFY_WAIT_SECONDS = 12
DB_BUSY_TIMEOUT_MS = 60000
CHARGE_SLOT = (10, 5, 13, 55)
EXPORT_SLOT = (17, 5, 20, 55)

# Keep recognising every period previously owned by this project so an OFF
# switch or watchdog can remove an obsolete schedule during migration.
CHARGE_SLOTS = {CHARGE_SLOT, (10, 5, 13, 50), (10, 0, 14, 0)}
EXPORT_SLOTS = {
    EXPORT_SLOT,
    (17, 5, 20, 50),
    (17, 0, 21, 0),
    (17, 5, 22, 55),
}
MANAGED_SLOTS = CHARGE_SLOTS | EXPORT_SLOTS

# FoxESS disables the scheduler after a verified groups=[] cleanup.  The next
# morning therefore has no active group from which to copy extraParam.  These
# are the parameter values read back from this H3-10.0-Smart installation and
# previously accepted by FoxESS for both ForceCharge and ForceDischarge.  SOC
# fields are replaced with the fresh calculated target before every write.
VERIFIED_EMPTY_SCHEDULER_EXTRA = {
    "fdPwr": 10000.0,
    "minSocOnGrid": MIN_SOC,
    "pvLimit": 300000.0,
    "reactivePower": 0.0,
    "exportLimit": 300000.0,
    "fdSoc": MIN_SOC,
    "importLimit": 300000.0,
    "secondWorkMode": "SelfUse",
    "maxSoc": 100.0,
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
    conn = sqlite3.connect(
        db_path,
        timeout=DB_BUSY_TIMEOUT_MS / 1000.0,
    )
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
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


def latest_plan(now, db_path=DB, use_latest_frozen=False):
    conn = sqlite3.connect(
        db_path,
        timeout=DB_BUSY_TIMEOUT_MS / 1000.0,
    )
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    if use_latest_frozen:
        # Deployment dry-runs may occur late at night after the optimiser has
        # rolled its planning horizon to tomorrow.  Select the first day from
        # the newest inserted snapshot; this mode is never permitted for a
        # live write.  Row order avoids comparing mixed-offset timestamp text.
        row = conn.execute("""
            SELECT * FROM economic_plan_actions
            WHERE created_at = (
                SELECT created_at FROM economic_plan_actions
                ORDER BY rowid DESC LIMIT 1
            )
            ORDER BY plan_date ASC LIMIT 1
        """).fetchone()
    else:
        row = conn.execute("""
            SELECT * FROM economic_plan_actions
            WHERE plan_date = ? ORDER BY rowid DESC LIMIT 1
        """, (now.date().isoformat(),)).fetchone()
    conn.close()
    if not row:
        raise RuntimeError("No frozen joint plan for today")
    plan = dict(row)
    if use_latest_frozen:
        return plan
    age = age_minutes(now, plan["created_at"])
    if age < -2:
        raise RuntimeError("Joint plan timestamp is in the future")
    if age > PLAN_MAX_AGE_MINUTES:
        raise RuntimeError(f"Joint plan is stale: {age:.1f} minutes old")
    return plan


def latest_soc(now, db_path=DB):
    conn = sqlite3.connect(
        db_path,
        timeout=DB_BUSY_TIMEOUT_MS / 1000.0,
    )
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
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


def default_self_use(group):
    """Identify FoxESS Remaining Time Mode exposed as an all-day group.

    The public OpenAPI read does not reliably retain the app's internal
    ``isRemainMode`` marker.  Sending this group back through OpenAPI can turn
    it into an ordinary overlapping schedule, so it must never be round-tripped.
    """
    mode = str(group.get("workMode") or "").replace("-", "").replace("_", "").lower()
    return (
        mode == "selfuse"
        and slot(group) in {(0, 0, 23, 59), (0, 0, 24, 0)}
    )


def active_group(group):
    enabled = group.get("enable")
    if enabled is None:
        return True
    if isinstance(enabled, str):
        return enabled.strip().lower() not in {"0", "false", "off"}
    return bool(enabled)


def outbound_groups(groups):
    """Return explicit active periods safe to send through public OpenAPI."""
    return [
        deepcopy(group)
        for group in groups
        if active_group(group) and not default_self_use(group)
    ]


def scope_managed(group, scope):
    current = slot(group)
    if scope == "charge":
        return current in CHARGE_SLOTS
    if scope == "export":
        return current in EXPORT_SLOTS
    return current in MANAGED_SLOTS


def base_extra(groups):
    """Return a verified parameter set even when cleanup left no periods."""
    for group in groups:
        extra = group.get("extraParam")
        if isinstance(extra, dict):
            result = deepcopy(extra)
            if float(result.get("fdPwr") or 0) <= 0:
                raise RuntimeError("Scheduler template has no verified discharge power")
            result.update({"minSocOnGrid": MIN_SOC, "secondWorkMode": "SelfUse"})
            return result
    return deepcopy(VERIFIED_EMPTY_SCHEDULER_EXTRA)


def desired_groups(existing, plan, phase, live_soc, enabled=True):
    """Return the complete schedule, preserving all non-owned periods."""
    groups = [
        deepcopy(group)
        for group in existing
        if active_group(group)
        and not managed(group)
        and not default_self_use(group)
    ]
    if phase == "watchdog" or not enabled:
        return groups

    template = base_extra(existing)
    if phase == "charge":
        charge = float(plan["charge_kwh"])
        # The optimiser chooses meter-side AC kWh, while FoxESS accepts only
        # an SOC cutoff.  Convert from the live SOC immediately before the
        # window; do not use the simulated afternoon SOC, which also includes
        # forecast solar and household load.
        stored_from_grid = max(0.0, charge) * CHARGE_EFFICIENCY
        target = live_soc + stored_from_grid / BATTERY_KWH * 100.0
        target = max(MIN_SOC, min(100.0, target))
        if charge >= 0.5 and target > live_soc + 0.5:
            extra = deepcopy(template)
            # FoxESS labels fdSoc as the FC/FD cutoff in the app, while some
            # inverter firmware enforces maxSoc during ForceCharge.  Set and
            # verify both to the same target so the visible cutoff and the
            # physical stopping limit cannot diverge.
            target = round(target, 1)
            extra.update({"maxSoc": target, "fdSoc": target})
            groups.append({
                "startHour": CHARGE_SLOT[0], "startMinute": CHARGE_SLOT[1],
                "endHour": CHARGE_SLOT[2], "endMinute": CHARGE_SLOT[3],
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
            "startHour": EXPORT_SLOT[0], "startMinute": EXPORT_SLOT[1],
            "endHour": EXPORT_SLOT[2], "endMinute": EXPORT_SLOT[3],
            "workMode": "ForceDischarge", "extraParam": extra,
        })
    return groups


def group_signature(groups):
    """Stable comparison of fields that change inverter behaviour.

    FoxESS may add or normalise unrelated ``extraParam`` values on read-back.
    Comparing the complete response caused a correctly applied schedule to be
    treated as a failure.  Keep verification strict for the period, work mode,
    SOC limits, discharge power and the post-cutoff mode.
    """
    def number(value):
        if value is None:
            return None
        return round(float(value), 3)

    signatures = []
    for group in outbound_groups(groups):
        mode = str(group.get("workMode") or "")
        extra = group.get("extraParam") or {}
        critical = {
            "minSocOnGrid": number(extra.get("minSocOnGrid")),
        }
        if mode == "ForceCharge":
            critical.update({
                "fdSoc": number(extra.get("fdSoc")),
                "maxSoc": number(extra.get("maxSoc")),
                "secondWorkMode": str(extra.get("secondWorkMode") or ""),
            })
        elif mode == "ForceDischarge":
            critical.update({
                "fdSoc": number(extra.get("fdSoc")),
                "fdPwr": number(extra.get("fdPwr")),
                "secondWorkMode": str(extra.get("secondWorkMode") or ""),
            })
        signatures.append(json.dumps({
            "slot": slot(group),
            "workMode": mode,
            "critical": critical,
        }, sort_keys=True, separators=(",", ":")))
    return sorted(signatures)


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
    # FoxESS represents a successful clear (groups=[]) by disabling the
    # scheduler.  Disabled means there are no active periods; stale groups in
    # the response must not block cleanup verification or tomorrow's re-enable.
    if int(result.get("enable", 0)) != 1:
        return response, []
    return response, result.get("groups") or []


def verify_schedule(device_sn, expected, attempts=VERIFY_ATTEMPTS, wait=VERIFY_WAIT_SECONDS):
    expected_signature = group_signature(expected)
    actual = []
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
    raise RuntimeError(
        "Scheduler verification mismatch after bounded retries; "
        f"expected={expected_signature}; actual={group_signature(actual)}"
    )


def write_schedule(device_sn, groups):
    response = foxess_post(WRITE_PATH, {"deviceSN": device_sn, "groups": groups})
    if not response or response.get("errno") != 0:
        raise RuntimeError(f"Scheduler write failed: {response}")


def apply_schedule(device_sn, current_payload, existing, desired, now):
    existing_payload = outbound_groups(existing)
    desired_payload = outbound_groups(desired)
    if group_signature(existing_payload) == group_signature(desired_payload):
        return "already matched", None
    path = backup(current_payload, now)
    try:
        write_schedule(device_sn, desired_payload)
        verify_schedule(device_sn, desired_payload)
        return "written and verified", path
    except Exception as original_error:
        try:
            write_schedule(device_sn, existing_payload)
            verify_schedule(device_sn, existing_payload)
        except Exception as rollback_error:
            raise RuntimeError(
                f"Schedule change failed and rollback failed: {original_error}; "
                f"rollback={rollback_error}; backup={path}"
            ) from rollback_error
        raise RuntimeError(
            f"Schedule change failed; original schedule restored: {original_error}; backup={path}"
        ) from original_error


def disable_scope(scope, now=None):
    """Immediately remove one or all owned periods and verify FoxESS readback."""
    if scope not in {"master", "charge", "export"}:
        raise ValueError(f"Unknown control scope: {scope}")
    now = now or datetime.now(TZ)
    device_sn = validate_device(get_device())
    current, existing = read_schedule(device_sn)
    desired = [deepcopy(group) for group in existing if not scope_managed(group, scope)]
    return apply_schedule(device_sn, current, existing, desired, now)


def validate_device(device):
    device_sn = device.get("deviceSN")
    if not device_sn:
        raise RuntimeError("FoxESS device serial number missing")
    status = str(device.get("status", "")).strip().lower()
    if status in {"0", "offline", "fault", "error"}:
        raise RuntimeError(f"FoxESS device is not available: {status}")
    return device_sn


def schedule_notification(phase, plan, soc, outcome, groups, now):
    """Describe the verified FoxESS result in customer-facing terms."""
    if phase == "charge":
        active = next((g for g in groups if scope_managed(g, "charge")), None)
        if active:
            target = float(active.get("extraParam", {}).get("fdSoc", MIN_SOC))
            headline = "IMPORT SCHEDULER SET AND VERIFIED"
            decision = (
                "Window: 10:05 AM-1:55 PM\n"
                f"Planned cheap import: {float(plan['charge_kwh']):.1f} kWh\n"
                f"FoxESS battery target: {target:.1f}%"
            )
        else:
            headline = "IMPORT SCHEDULER NOT REQUIRED"
            decision = "No FoxESS import period is active for today."
    elif phase == "export":
        active = next((g for g in groups if scope_managed(g, "export")), None)
        if active:
            cutoff = float(active.get("extraParam", {}).get("fdSoc", MIN_SOC))
            headline = "EXPORT SCHEDULER SET AND VERIFIED"
            decision = (
                "Window: 5:05 PM-8:55 PM\n"
                f"Planned premium export: {float(plan['export_kwh']):.1f} kWh\n"
                f"Protected battery cutoff: {cutoff:.1f}%"
            )
        else:
            headline = "EXPORT SCHEDULER NOT REQUIRED"
            decision = "No FoxESS export period is active for today."
    else:
        headline = "DAILY SCHEDULE CLEANUP VERIFIED"
        decision = "Managed import and export periods were removed after use."

    return (
        f"⚡ Smart Electricity - {headline}\n\n"
        f"Date: {now.date().isoformat()}\n"
        f"Live battery: {soc:.1f}%\n"
        f"{decision}\n"
        f"Result: {outcome}.\n\n"
        "✅ FoxESS scheduler read-back verified."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=("charge", "export", "watchdog"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ignore-switches", action="store_true")
    parser.add_argument("--use-latest-frozen-plan", action="store_true")
    args = parser.parse_args()
    if args.ignore_switches and not args.dry_run:
        raise RuntimeError("--ignore-switches is permitted only with --dry-run")
    if args.use_latest_frozen_plan and not args.dry_run:
        raise RuntimeError(
            "--use-latest-frozen-plan is permitted only with --dry-run"
        )
    now = datetime.now(TZ)
    plan = None if args.phase == "watchdog" else latest_plan(
        now,
        use_latest_frozen=args.use_latest_frozen_plan,
    )
    stamp, soc = latest_soc(now)

    control_mode = os.getenv("FOXESS_CONTROL_MODE", "rule").strip().lower()
    if not args.dry_run and control_mode != "joint":
        raise RuntimeError(
            f"Joint write blocked: FOXESS_CONTROL_MODE is {control_mode!r}, expected 'joint'"
        )

    device_sn = validate_device(get_device())
    current, existing = read_schedule(device_sn)
    switches = get_switches(DB)
    enabled = args.ignore_switches or phase_enabled(switches, args.phase)
    groups = desired_groups(existing, plan, args.phase, soc, enabled=enabled)
    print(json.dumps({
        "phase": args.phase,
        "live_soc": soc,
        "live_timestamp": stamp,
        "plan_created_at": None if plan is None else plan["created_at"],
        "charge_kwh": None if plan is None else plan["charge_kwh"],
        "export_kwh": None if plan is None else plan["export_kwh"],
        "managed_groups": [group for group in groups if managed(group)],
        "changed": group_signature(existing) != group_signature(groups),
        "switches": switches,
        "phase_enabled": enabled,
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
    notify(schedule_notification(args.phase, plan, soc, outcome, groups, now))
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
