"""Persistent, audited operator switches for FoxESS automation."""

import argparse
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

DB = "data/energy.db"
TZ = ZoneInfo("Australia/Sydney")
VALID_SCOPES = {"master", "charge", "export"}


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS control_switches (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            master_enabled INTEGER NOT NULL DEFAULT 0,
            charge_enabled INTEGER NOT NULL DEFAULT 0,
            export_enabled INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS control_switch_events (
            event_time TEXT NOT NULL,
            scope TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            actor TEXT NOT NULL,
            detail TEXT NOT NULL
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO control_switches (
            id, master_enabled, charge_enabled, export_enabled,
            updated_at, updated_by
        ) VALUES (1, 0, 0, 0, ?, 'safe-default')
    """, (datetime.now(TZ).isoformat(timespec="seconds"),))


def get_switches(db_path=DB):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM control_switches WHERE id = 1").fetchone()
    conn.commit()
    conn.close()
    return {
        "master_enabled": bool(row["master_enabled"]),
        "charge_enabled": bool(row["charge_enabled"]),
        "export_enabled": bool(row["export_enabled"]),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def set_switch(scope, enabled, actor="operator", detail="", db_path=DB, now=None):
    if scope not in VALID_SCOPES:
        raise ValueError(f"Unknown control scope: {scope}")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    now = now or datetime.now(TZ)
    stamp = now.isoformat(timespec="seconds")
    column = f"{scope}_enabled"
    conn = sqlite3.connect(str(db_path))
    ensure_schema(conn)
    conn.execute(
        f"UPDATE control_switches SET {column} = ?, updated_at = ?, updated_by = ? WHERE id = 1",
        (int(enabled), stamp, actor),
    )
    conn.execute(
        "INSERT INTO control_switch_events VALUES (?, ?, ?, ?, ?)",
        (stamp, scope, int(enabled), actor, detail or "operator switch"),
    )
    conn.commit()
    conn.close()
    return get_switches(db_path)


def set_all(enabled, actor="operator", detail="", db_path=DB, now=None):
    state = None
    for scope in ("charge", "export", "master"):
        state = set_switch(scope, enabled, actor, detail, db_path, now)
    return state


def phase_enabled(state, phase):
    if not state["master_enabled"]:
        return False
    if phase == "charge":
        return state["charge_enabled"]
    if phase == "export":
        return state["export_enabled"]
    return True


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all-on", action="store_true")
    group.add_argument("--all-off", action="store_true")
    args = parser.parse_args()
    enabled = bool(args.all_on)
    print(set_all(enabled, "deployment", "controller deployment"))


if __name__ == "__main__":
    main()
