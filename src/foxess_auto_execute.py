import argparse
import json
import os
import sqlite3
import sys
import time
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from foxess import foxess_post, get_device
from telegram_notify import send_telegram


DB = "data/energy.db"
TZ = ZoneInfo("Australia/Sydney")

GET_PATH = "/op/v3/device/scheduler/get"
WRITE_PATH = "/op/v3/device/scheduler/enable"

START_HOUR = 17
START_MINUTE = 0
END_HOUR = 21
END_MINUTE = 0

MIN_SOC = 10.0
MAX_SOC = 100.0

MAX_SNAPSHOT_AGE_MINUTES = 20

EXPECTED_POWER = 10000.0


def notify(message):
    """
    Best-effort Telegram notification.

    Telegram must never interfere with FoxESS control.
    """
    try:
        send_telegram(message)
        log("Telegram notification sent.")
    except Exception as exc:
        log(
            "WARNING: Telegram notification failed: "
            f"{type(exc).__name__}: {exc}"
        )
VERIFY_WAIT_SECONDS = 20


def log(message):
    now = datetime.now(TZ).isoformat(timespec="seconds")
    print(f"{now} | {message}", flush=True)


def get_snapshot(today):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        """
        SELECT
            plan_date,
            snapshot_timestamp,
            recommended_export_kwh,
            recommended_export_percent,
            predicted_start_soc,
            predicted_pre_export_soc,
            predicted_midnight_soc,
            predicted_next_solar_soc,
            status
        FROM strategy_validation
        WHERE plan_date = ?
        """,
        (today,)
    ).fetchone()

    conn.close()

    return dict(row) if row else None


def determine_strategy(snapshot):
    export_percent = snapshot["recommended_export_percent"]
    export_kwh = snapshot["recommended_export_kwh"]
    pre_export_soc = snapshot["predicted_pre_export_soc"]

    if export_percent is None:
        raise RuntimeError(
            "recommended_export_percent is NULL"
        )

    export_percent = float(export_percent)
    export_kwh = float(export_kwh or 0.0)

    if not 0.0 <= export_percent <= 100.0:
        raise RuntimeError(
            f"Invalid export percentage: {export_percent}"
        )

    # NO EXPORT:
    # We have verified on this H3-G2 that changing the
    # 17:05-22:55 scheduler group to SelfUse disables
    # forced discharge while preserving its parameters.
    if export_percent < 0.5 or export_kwh < 0.5:
        return {
            "strategy": "NO_EXPORT",
            "work_mode": "SelfUse",
            "cutoff": None,
            "export_percent": export_percent,
            "export_kwh": export_kwh,
        }

    if pre_export_soc is None:
        raise RuntimeError(
            "predicted_pre_export_soc is NULL for an export day"
        )

    pre_export_soc = float(pre_export_soc)

    if not MIN_SOC <= pre_export_soc <= MAX_SOC:
        raise RuntimeError(
            f"Invalid predicted pre-export SOC: {pre_export_soc}"
        )

    # Example:
    # pre-export SOC 100%, export 55%
    # -> FoxESS cutoff 45%.
    cutoff = pre_export_soc - export_percent

    cutoff = max(MIN_SOC, cutoff)
    cutoff = round(cutoff, 1)

    if not MIN_SOC <= cutoff <= MAX_SOC:
        raise RuntimeError(
            f"Calculated cutoff outside safe range: {cutoff}"
        )

    return {
        "strategy": "EXPORT",
        "work_mode": "ForceDischarge",
        "cutoff": cutoff,
        "export_percent": export_percent,
        "export_kwh": export_kwh,
    }


def validate_snapshot(snapshot, now):
    if not snapshot:
        raise RuntimeError(
            "No frozen strategy snapshot exists for today"
        )

    today = now.date().isoformat()

    if snapshot["plan_date"] != today:
        raise RuntimeError(
            f"Snapshot date mismatch: {snapshot['plan_date']}"
        )

    raw_ts = snapshot["snapshot_timestamp"]

    if not raw_ts:
        raise RuntimeError(
            "Snapshot timestamp missing"
        )

    snapshot_time = datetime.fromisoformat(raw_ts)

    if snapshot_time.tzinfo is None:
        snapshot_time = snapshot_time.replace(tzinfo=TZ)

    age_minutes = (
        now - snapshot_time.astimezone(TZ)
    ).total_seconds() / 60.0

    if age_minutes < -2:
        raise RuntimeError(
            f"Snapshot timestamp is in the future: {raw_ts}"
        )

    if age_minutes > MAX_SNAPSHOT_AGE_MINUTES:
        raise RuntimeError(
            f"Snapshot is stale: {age_minutes:.1f} minutes old"
        )

    if snapshot["status"] not in ("PENDING",):
        raise RuntimeError(
            f"Unexpected strategy status: {snapshot['status']}"
        )

    return age_minutes


def find_target(groups):
    matches = [
        g for g in groups
        if g.get("startHour") == START_HOUR
        and g.get("startMinute") == START_MINUTE
        and g.get("endHour") == END_HOUR
        and g.get("endMinute") == END_MINUTE
    ]

    if len(matches) != 1:
        raise RuntimeError(
            "Safety stop: expected exactly one "
            f"{START_HOUR:02d}:{START_MINUTE:02d}-"
            f"{END_HOUR:02d}:{END_MINUTE:02d} group; "
            f"found {len(matches)}"
        )

    return matches[0]


def backup_schedule(current, today):
    os.makedirs(
        "data/foxess_schedule_backups",
        exist_ok=True
    )

    stamp = datetime.now(TZ).strftime(
        "%Y%m%d_%H%M%S"
    )

    path = (
        "data/foxess_schedule_backups/"
        f"{today}_{stamp}.json"
    )

    with open(path, "w") as f:
        json.dump(current, f, indent=2)

    return path


def record_control(today, mode, cutoff):
    conn = sqlite3.connect(DB)

    conn.execute(
        """
        UPDATE strategy_validation
        SET
            foxess_control_mode = ?,
            foxess_export_start = ?,
            foxess_export_end = ?,
            foxess_cutoff_soc = ?
        WHERE plan_date = ?
        """,
        (
            mode,
            "17:00",
            "21:00",
            cutoff,
            today,
        )
    )

    conn.commit()
    conn.close()

    log(
        "Strategy Validation updated with "
        "verified FoxESS control."
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate everything but do not write FoxESS"
    )

    args = parser.parse_args()

    now = datetime.now(TZ)
    today = now.date().isoformat()

    log("FOXESS AUTO EXECUTOR START")
    log(f"Date: {today}")

    snapshot = get_snapshot(today)

    age = validate_snapshot(snapshot, now)

    log(
        "Frozen strategy found: "
        f"{snapshot['snapshot_timestamp']} "
        f"({age:.1f} min old)"
    )

    strategy = determine_strategy(snapshot)

    export_percent = float(
        snapshot["recommended_export_percent"] or 0
    )

    export_kwh = float(
        snapshot["recommended_export_kwh"] or 0
    )

    log(
        f"Planner: {export_percent:.1f}% / "
        f"{export_kwh:.1f} kWh"
    )

    log(
        "Predicted pre-export SOC: "
        f"{snapshot['predicted_pre_export_soc']}%"
    )

    if strategy["strategy"] == "EXPORT":
        log(
            "Required FoxESS strategy: "
            "ForceDischarge / "
            f"cutoff {strategy['cutoff']:.1f}%"
        )
    else:
        log(
            "Required FoxESS strategy: "
            "SelfUse / NO FORCED EXPORT"
        )

    device = get_device()
    sn = device.get("deviceSN")

    if not sn:
        raise RuntimeError(
            "FoxESS device serial number missing"
        )

    current = foxess_post(
        GET_PATH,
        {"deviceSN": sn}
    )

    if not current or current.get("errno") != 0:
        raise RuntimeError(
            f"Could not read FoxESS scheduler: {current}"
        )

    result = current.get("result") or {}

    if int(result.get("enable", 0)) != 1:
        raise RuntimeError(
            "FoxESS scheduler is not enabled"
        )

    groups = deepcopy(
        result.get("groups", [])
    )

    target = find_target(groups)

    current_mode = target.get("workMode")
    extra = target.get("extraParam")

    if not isinstance(extra, dict):
        raise RuntimeError(
            "Target scheduler extraParam missing"
        )

    # We have explicitly verified these two modes.
    if current_mode not in (
        "ForceDischarge",
        "SelfUse",
    ):
        raise RuntimeError(
            "Safety stop: unexpected target workMode: "
            f"{current_mode}"
        )

    current_cutoff = float(
        extra.get("fdSoc")
    )

    current_power = float(
        extra.get("fdPwr")
    )

    second_mode = extra.get(
        "secondWorkMode"
    )

    if current_power != EXPECTED_POWER:
        raise RuntimeError(
            "Safety stop: expected fdPwr=10000 W, "
            f"FoxESS reports {current_power}"
        )

    if second_mode != "SelfUse":
        raise RuntimeError(
            "Safety stop: expected "
            "secondWorkMode=SelfUse, "
            f"FoxESS reports {second_mode}"
        )

    log(
        "Current FoxESS: "
        f"mode={current_mode}, "
        f"cutoff={current_cutoff:.1f}%, "
        f"power={current_power:.0f}W, "
        f"after={second_mode}"
    )

    # ==================================================
    # EXPORT DAY
    # ==================================================

    if strategy["strategy"] == "EXPORT":

        required_cutoff = strategy["cutoff"]

        already_correct = (
            current_mode == "ForceDischarge"
            and abs(
                current_cutoff - required_cutoff
            ) < 0.01
        )

        if already_correct:
            log(
                "SUCCESS: FoxESS already has "
                "the required ForceDischarge "
                "mode and cutoff."
            )

            if not args.dry_run:
                record_control(
                    today,
                    "AUTO_FORCE_DISCHARGE",
                    current_cutoff,
                )

                notify(
                    "⚡ Smart Electricity - EXPORT VERIFIED\n\n"
                    f"Date: {today}\n"
                    "Mode: ForceDischarge\n"
                    f"Schedule: {START_HOUR:02d}:{START_MINUTE:02d}"
                    f"-{END_HOUR:02d}:{END_MINUTE:02d}\n"
                    f"Cutoff SOC: {current_cutoff:.1f}%\n"
                    f"Power: {current_power:.0f} W\n"
                    f"Planned export: "
                    f"{strategy['export_percent']:.1f}% / "
                    f"{strategy['export_kwh']:.1f} kWh\n\n"
                    "✅ FoxESS already matched the required strategy.\n"
                    "No scheduler write was necessary."
                )

            return

        if args.dry_run:
            log(
                "DRY RUN: would set "
                "17:05-22:55 to ForceDischarge "
                f"with cutoff {required_cutoff:.1f}%"
            )
            log(
                "DRY RUN COMPLETE: nothing written."
            )
            return

        backup = backup_schedule(
            current,
            today
        )

        log(
            f"Schedule backup: {backup}"
        )

        # Preserve every other parameter.
        # Change only the verified fields required
        # for an export day.
        target["workMode"] = "ForceDischarge"
        target["extraParam"]["fdSoc"] = (
            required_cutoff
        )
        target["extraParam"]["fdPwr"] = (
            EXPECTED_POWER
        )
        target["extraParam"][
            "secondWorkMode"
        ] = "SelfUse"

        log(
            "Writing FoxESS: "
            "ForceDischarge / "
            f"{required_cutoff:.1f}%"
        )

    # ==================================================
    # NO EXPORT DAY
    # ==================================================

    else:

        if current_mode == "SelfUse":
            log(
                "SUCCESS: FoxESS target period "
                "is already SelfUse. "
                "No write necessary."
            )

            if not args.dry_run:
                record_control(
                    today,
                    "AUTO_SELF_USE_NO_EXPORT",
                    current_cutoff,
                )

                notify(
                    "⚡ Smart Electricity - NO EXPORT VERIFIED\n\n"
                    f"Date: {today}\n"
                    "Mode: SelfUse\n"
                    f"Schedule: {START_HOUR:02d}:{START_MINUTE:02d}"
                    f"-{END_HOUR:02d}:{END_MINUTE:02d}\n\n"
                    "✅ FoxESS already matched the required strategy.\n"
                    "No scheduler write was necessary."
                )

            return

        if args.dry_run:
            log(
                "DRY RUN: would change "
                "17:05-22:55 from "
                f"{current_mode} to SelfUse."
            )
            log(
                "DRY RUN COMPLETE: nothing written."
            )
            return

        backup = backup_schedule(
            current,
            today
        )

        log(
            f"Schedule backup: {backup}"
        )

        # Verified on this inverter:
        # preserve the complete group and change
        # ONLY workMode to SelfUse.
        target["workMode"] = "SelfUse"

        log(
            "Writing FoxESS: "
            "17:05-22:55 -> SelfUse "
            "(NO EXPORT)"
        )

    # ==================================================
    # COMMON WRITE
    # ==================================================

    write_result = foxess_post(
        WRITE_PATH,
        {
            "deviceSN": sn,
            "groups": groups,
        }
    )

    if (
        not write_result
        or write_result.get("errno") != 0
    ):
        raise RuntimeError(
            f"FoxESS write failed: {write_result}"
        )

    log("FoxESS write returned success.")

    # FoxESS scheduler changes can take several
    # seconds to become visible through GET.
    log(
        f"Waiting {VERIFY_WAIT_SECONDS}s "
        "before verification..."
    )

    time.sleep(VERIFY_WAIT_SECONDS)

    # ==================================================
    # INDEPENDENT READ-BACK
    # ==================================================

    verify = foxess_post(
        GET_PATH,
        {"deviceSN": sn}
    )

    if not verify or verify.get("errno") != 0:
        raise RuntimeError(
            f"Verification read failed: {verify}"
        )

    verified_groups = deepcopy(
        (verify.get("result") or {}).get(
            "groups",
            []
        )
    )

    verified_target = find_target(
        verified_groups
    )

    verified_mode = verified_target.get(
        "workMode"
    )

    verified_extra = verified_target.get(
        "extraParam"
    )

    if not isinstance(verified_extra, dict):
        raise RuntimeError(
            "Verification extraParam missing"
        )

    verified_soc = float(
        verified_extra.get("fdSoc")
    )

    verified_power = float(
        verified_extra.get("fdPwr")
    )

    # ==================================================
    # VERIFY EXPORT
    # ==================================================

    if strategy["strategy"] == "EXPORT":

        required_cutoff = strategy["cutoff"]

        if verified_mode != "ForceDischarge":
            raise RuntimeError(
                "VERIFY FAILED: requested "
                "ForceDischarge but FoxESS reports "
                f"{verified_mode}"
            )

        if (
            abs(
                verified_soc - required_cutoff
            ) >= 0.01
        ):
            raise RuntimeError(
                "VERIFY FAILED: "
                f"requested cutoff "
                f"{required_cutoff:.1f}%, "
                f"FoxESS reports "
                f"{verified_soc:.1f}%"
            )

        if verified_power != EXPECTED_POWER:
            raise RuntimeError(
                "VERIFY FAILED: expected "
                f"{EXPECTED_POWER:.0f} W, "
                f"FoxESS reports "
                f"{verified_power:.0f} W"
            )

        log(
            "SUCCESS: FoxESS verified "
            "ForceDischarge / "
            f"{verified_soc:.1f}%"
        )

        record_control(
            today,
            "AUTO_FORCE_DISCHARGE",
            verified_soc,
        )

        notify(
            "⚡ Smart Electricity - EXPORT VERIFIED\n\n"
            f"Date: {today}\n"
            "Mode: ForceDischarge\n"
            f"Schedule: {START_HOUR:02d}:{START_MINUTE:02d}"
            f"-{END_HOUR:02d}:{END_MINUTE:02d}\n"
            f"Cutoff SOC: {verified_soc:.1f}%\n"
            f"Power: {verified_power:.0f} W\n"
            f"Planned export: "
            f"{strategy['export_percent']:.1f}% / "
            f"{strategy['export_kwh']:.1f} kWh\n\n"
            "✅ FoxESS read-back verified."
        )

    # ==================================================
    # VERIFY NO EXPORT
    # ==================================================

    else:

        if verified_mode != "SelfUse":
            raise RuntimeError(
                "VERIFY FAILED: requested "
                "SelfUse but FoxESS reports "
                f"{verified_mode}"
            )

        log(
            "SUCCESS: FoxESS verified "
            "SelfUse / NO FORCED EXPORT."
        )

        record_control(
            today,
            "AUTO_SELF_USE_NO_EXPORT",
            verified_soc,
        )

        notify(
            "⚡ Smart Electricity - NO EXPORT VERIFIED\n\n"
            f"Date: {today}\n"
            "Mode: SelfUse\n"
            f"Schedule: {START_HOUR:02d}:{START_MINUTE:02d}"
            f"-{END_HOUR:02d}:{END_MINUTE:02d}\n\n"
            "✅ FoxESS read-back verified.\n"
            "No forced battery export scheduled."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_message = (
            f"{type(exc).__name__}: {exc}"
        )

        log(
            f"FAILED: {error_message}"
        )

        # Dry-run failures are diagnostic only.
        # Never send production Telegram alerts for them.
        if "--dry-run" not in sys.argv:
            notify(
                "🚨 Smart Electricity - AUTOMATION FAILED\n\n"
                f"Time: {datetime.now(TZ).isoformat(timespec='seconds')}\n"
                f"Reason: {error_message}\n\n"
                "⚠️ FoxESS automatic control was not successfully verified."
            )
        else:
            log(
                "DRY RUN: Telegram failure notification suppressed."
            )

        sys.exit(1)
