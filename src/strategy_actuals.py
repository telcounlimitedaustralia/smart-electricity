import sqlite3
from datetime import datetime, timedelta

from tariff import import_rate, export_rate
from telegram_notify import send_telegram

DB = "data/energy.db"
BATTERY_KWH = 42.0

# Useful solar threshold for defining solar start/end.
SOLAR_THRESHOLD_KW = 0.5


def notify(message):
    """
    Best-effort Telegram notification.

    Telegram failure must never affect validation.
    """
    try:
        send_telegram(message)
        print("Telegram daily result sent.")
    except Exception as exc:
        print(
            "WARNING: Telegram daily result failed: "
            f"{type(exc).__name__}: {exc}"
        )


def parse_ts(value):
    return datetime.fromisoformat(value)


def rows_for_day(conn, day):
    return conn.execute("""
        SELECT
            timestamp,
            pv_kw,
            load_kw,
            grid_export_kw,
            grid_import_kw,
            battery_soc,
            battery_charge_kw,
            battery_discharge_kw
        FROM foxess_history
        WHERE substr(timestamp,1,10) = ?
        ORDER BY timestamp
    """, (day,)).fetchall()


def integrate(rows, column):
    """Integrate kW readings into kWh using actual sample intervals."""
    total = 0.0

    for i in range(len(rows) - 1):
        a = rows[i]
        b = rows[i + 1]

        value = a[column]

        if value is None:
            continue

        dt = (
            parse_ts(b["timestamp"]) -
            parse_ts(a["timestamp"])
        ).total_seconds() / 3600.0

        # Ignore large data gaps.
        if 0 < dt <= 10 / 60:
            total += max(0.0, float(value)) * dt

    return total


def integrate_financials(rows):
    """
    Calculate actual grid import cost and export revenue.

    Each FoxESS interval is priced using the tariff applicable
    at that interval's timestamp.

    Tariff values are stored in cents/kWh, so totals are
    converted to dollars before returning.
    """
    import_cost_cents = 0.0
    export_revenue_cents = 0.0

    for i in range(len(rows) - 1):
        a = rows[i]
        b = rows[i + 1]

        dt_hours = (
            parse_ts(b["timestamp"]) -
            parse_ts(a["timestamp"])
        ).total_seconds() / 3600.0

        # Match the existing energy integration safety rule.
        if not (0 < dt_hours <= 10 / 60):
            continue

        ts = parse_ts(a["timestamp"])

        grid_import_kw = max(
            0.0,
            float(a["grid_import_kw"] or 0)
        )

        grid_export_kw = max(
            0.0,
            float(a["grid_export_kw"] or 0)
        )

        import_cents, _ = import_rate(ts)
        export_cents, _ = export_rate(ts)

        import_kwh = grid_import_kw * dt_hours
        export_kwh = grid_export_kw * dt_hours

        import_cost_cents += (
            import_kwh * import_cents
        )

        export_revenue_cents += (
            export_kwh * export_cents
        )

    import_cost = import_cost_cents / 100.0
    export_revenue = export_revenue_cents / 100.0

    # Positive value = financial benefit for the day.
    net_value = export_revenue - import_cost

    return (
        import_cost,
        export_revenue,
        net_value,
    )


def integrate_battery_to_load(rows):
    """
    Estimate battery energy used by household load.

    Battery discharge may include forced grid export, therefore
    never count more battery-to-load than household load remaining
    after PV and grid-import contribution.
    """
    total = 0.0

    for i in range(len(rows) - 1):
        a = rows[i]
        b = rows[i + 1]

        dt = (
            parse_ts(b["timestamp"]) -
            parse_ts(a["timestamp"])
        ).total_seconds() / 3600.0

        if not (0 < dt <= 10 / 60):
            continue

        load = max(0.0, float(a["load_kw"] or 0))
        pv = max(0.0, float(a["pv_kw"] or 0))
        grid_import = max(
            0.0,
            float(a["grid_import_kw"] or 0)
        )
        batt_discharge = max(
            0.0,
            float(a["battery_discharge_kw"] or 0)
        )

        residual_load = max(
            0.0,
            load - pv - grid_import
        )

        battery_to_load = min(
            batt_discharge,
            residual_load
        )

        total += battery_to_load * dt

    return total


def soc_near(rows, target_hour):
    """SOC nearest requested clock hour."""
    candidates = []

    for row in rows:
        if row["battery_soc"] is None:
            continue

        ts = parse_ts(row["timestamp"])

        target = ts.replace(
            hour=target_hour,
            minute=0,
            second=0,
            microsecond=0
        )

        distance = abs(
            (ts - target).total_seconds()
        )

        candidates.append(
            (distance, float(row["battery_soc"]))
        )

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def first_soc(rows):
    for row in rows:
        if row["battery_soc"] is not None:
            return float(row["battery_soc"])

    return None


def solar_window(rows):
    useful = [
        row
        for row in rows
        if float(row["pv_kw"] or 0)
        >= SOLAR_THRESHOLD_KW
    ]

    if not useful:
        return None, None

    return (
        parse_ts(useful[0]["timestamp"]),
        parse_ts(useful[-1]["timestamp"])
    )


def next_solar_soc(conn, day):
    """
    Find SOC when useful solar begins on the following day.

    Completed days normally come from foxess_history.
    If the following day is today, use foxess_live so
    yesterday can be validated without waiting another day.
    """

    next_day = (
        datetime.fromisoformat(day).date()
        + timedelta(days=1)
    ).isoformat()

    # First try completed historical data.
    row = conn.execute("""
        SELECT
            timestamp,
            battery_soc
        FROM foxess_history
        WHERE substr(timestamp,1,10) = ?
          AND pv_kw >= ?
          AND battery_soc IS NOT NULL
        ORDER BY timestamp
        LIMIT 1
    """, (
        next_day,
        SOLAR_THRESHOLD_KW
    )).fetchone()

    if row:
        return (
            float(row["battery_soc"]),
            row["timestamp"]
        )

    # For the current day, readings are still in foxess_live.
    row = conn.execute("""
        SELECT
            timestamp,
            battery_soc
        FROM foxess_live
        WHERE substr(timestamp,1,10) = ?
          AND pv_kw >= ?
          AND battery_soc IS NOT NULL
        ORDER BY timestamp
        LIMIT 1
    """, (
        next_day,
        SOLAR_THRESHOLD_KW
    )).fetchone()

    if row:
        return (
            float(row["battery_soc"]),
            row["timestamp"]
        )

    return None, None


def overnight_load(conn, day):
    """
    Household consumption from useful solar ending on plan_date
    until useful solar begins the following day.

    Completed plan day comes from foxess_history.
    Following morning can come from history or foxess_live.
    """

    current = rows_for_day(conn, day)

    next_day = (
        datetime.fromisoformat(day).date()
        + timedelta(days=1)
    ).isoformat()

    following = rows_for_day(conn, next_day)

    # Today's readings may not yet be in foxess_history.
    if not following:
        following = conn.execute("""
            SELECT
                timestamp,
                pv_kw,
                load_kw,
                grid_export_kw,
                grid_import_kw,
                battery_soc,
                battery_charge_kw,
                battery_discharge_kw
            FROM foxess_live
            WHERE substr(timestamp,1,10) = ?
            ORDER BY timestamp
        """, (next_day,)).fetchall()

    if not current or not following:
        return None

    _, solar_end = solar_window(current)
    solar_start, _ = solar_window(following)

    if solar_end is None or solar_start is None:
        return None

    combined = list(current) + list(following)

    selected = [
        row
        for row in combined
        if solar_end <= parse_ts(row["timestamp"])
        <= solar_start
    ]

    if len(selected) < 2:
        return None

    return integrate(selected, "load_kw")


def validate_record(conn, record):
    day = record["plan_date"]
    rows = rows_for_day(conn, day)

    if len(rows) < 250:
        print(
            f"{day}: waiting for history "
            f"({len(rows)} readings)"
        )
        return False

    actual_solar = integrate(rows, "pv_kw")
    actual_load = integrate(rows, "load_kw")
    actual_import = integrate(rows, "grid_import_kw")
    actual_export = integrate(rows, "grid_export_kw")

    (
        actual_import_cost,
        actual_export_revenue,
        actual_net_value,
    ) = integrate_financials(rows)

    battery_to_load = integrate_battery_to_load(rows)

    # Grid → Load currently approximated by total grid import.
    # This remains valid while grid charging is not enabled.
    grid_to_load = actual_import

    start_soc = first_soc(rows)
    pre_export_soc = soc_near(rows, 17)
    end_export_soc = soc_near(rows, 21)

    next_soc, next_soc_time = next_solar_soc(
        conn,
        day
    )

    overnight = overnight_load(conn, day)

    # Wait until following morning data exists.
    if next_soc is None or overnight is None:
        print(
            f"{day}: daily actuals available; "
            "waiting for next morning history"
        )
        return False

    actual_export_percent = (
        actual_export / BATTERY_KWH * 100.0
    )

    forecast_solar = record["forecast_solar_kwh"]
    forecast_load = record["forecast_load_kwh"]
    predicted_soc = record["predicted_next_solar_soc"]

    solar_error = None
    load_error = None
    soc_error = None

    if forecast_solar not in (None, 0):
        solar_error = (
            (actual_solar - float(forecast_solar))
            / float(forecast_solar)
            * 100.0
        )

    if forecast_load not in (None, 0):
        load_error = (
            (actual_load - float(forecast_load))
            / float(forecast_load)
            * 100.0
        )

    if predicted_soc is not None:
        soc_error = (
            next_soc - float(predicted_soc)
        )

    conn.execute("""
        UPDATE strategy_validation
        SET
            actual_solar_kwh = ?,
            actual_load_kwh = ?,
            actual_grid_import_kwh = ?,
            actual_grid_export_kwh = ?,
            actual_next_solar_soc = ?,

            actual_start_soc = ?,
            actual_pre_export_soc = ?,
            actual_end_export_soc = ?,
            actual_overnight_load_kwh = ?,
            actual_battery_to_load_kwh = ?,
            actual_grid_to_load_kwh = ?,
            actual_export_percent = ?,

            actual_import_cost = ?,
            actual_export_revenue = ?,
            actual_net_value = ?,

            solar_error_percent = ?,
            load_error_percent = ?,
            next_solar_soc_error_pp = ?,

            status = 'VALIDATED'

        WHERE id = ?
    """, (
        round(actual_solar, 2),
        round(actual_load, 2),
        round(actual_import, 2),
        round(actual_export, 2),
        round(next_soc, 1),

        None if start_soc is None else round(start_soc, 1),
        None if pre_export_soc is None else round(pre_export_soc, 1),
        None if end_export_soc is None else round(end_export_soc, 1),
        round(overnight, 2),
        round(battery_to_load, 2),
        round(grid_to_load, 2),
        round(actual_export_percent, 1),

        round(actual_import_cost, 2),
        round(actual_export_revenue, 2),
        round(actual_net_value, 2),

        None if solar_error is None
        else round(solar_error, 1),

        None if load_error is None
        else round(load_error, 1),

        None if soc_error is None
        else round(soc_error, 1),

        record["id"]
    ))

    conn.commit()

    print()
    print("VALIDATED:", day)
    print("--------------------------------")
    print("Solar:            ", round(actual_solar,2), "kWh")
    print("Load:             ", round(actual_load,2), "kWh")
    print("Battery -> Load:  ", round(battery_to_load,2), "kWh")
    print("Grid -> Load:     ", round(grid_to_load,2), "kWh")
    print("Grid Import:      ", round(actual_import,2), "kWh")
    print("Grid Export:      ", round(actual_export,2), "kWh")
    print("Export equiv:     ", round(actual_export_percent,1), "%")
    print("Start SOC:        ", start_soc, "%")
    print("SOC @ 5PM:        ", pre_export_soc, "%")
    print("End Export SOC:   ", end_export_soc, "%")
    print("Next Solar SOC:   ", next_soc, "%")
    print("Next Solar Time:  ", next_soc_time)
    print("Overnight Load:   ", round(overnight,2), "kWh")
    print("Import Cost:      $", round(actual_import_cost,2))
    print("Export Revenue:   $", round(actual_export_revenue,2))
    print("Net Value:        $", round(actual_net_value,2))

    # ==================================================
    # TELEGRAM DAILY VALIDATION RESULT
    # ==================================================

    planned_export_percent = (
        record["recommended_export_percent"]
    )

    planned_export_kwh = (
        record["recommended_export_kwh"]
    )

    predicted_next_soc = (
        record["predicted_next_solar_soc"]
    )

    def fmt(value, decimals=1):
        if value is None:
            return "—"
        return f"{float(value):.{decimals}f}"

    def signed(value, suffix=""):
        if value is None:
            return "—"
        return f"{float(value):+.1f}{suffix}"

    net_sign = "+" if actual_net_value >= 0 else ""

    message = (
        "☀️ Smart Electricity - DAILY RESULT\n\n"
        f"Date: {day}\n"
        "Status: VALIDATED\n\n"

        "⚡ STRATEGY\n"
        f"Planned export: "
        f"{fmt(planned_export_percent)}% / "
        f"{fmt(planned_export_kwh, 2)} kWh\n"
        f"Actual grid export: {actual_export:.2f} kWh\n"
        f"Export equivalent: {actual_export_percent:.1f}%\n\n"

        "🔋 BATTERY\n"
        f"Start SOC: {fmt(start_soc)}%\n"
        f"SOC @ 5 PM: {fmt(pre_export_soc)}%\n"
        f"End-export SOC: {fmt(end_export_soc)}%\n"
        f"Next-solar SOC: {fmt(next_soc)}%\n"
        f"Predicted next-solar SOC: "
        f"{fmt(predicted_next_soc)}%\n"
        f"Overnight load: {overnight:.2f} kWh\n\n"

        "🏠 ENERGY\n"
        f"Solar: {actual_solar:.2f} kWh\n"
        f"Load: {actual_load:.2f} kWh\n"
        f"Battery -> Load: {battery_to_load:.2f} kWh\n"
        f"Grid import: {actual_import:.2f} kWh\n\n"

        "💰 FINANCIAL\n"
        f"Import cost: ${actual_import_cost:.2f}\n"
        f"Export revenue: ${actual_export_revenue:.2f}\n"
        f"Net value: {net_sign}${actual_net_value:.2f}\n\n"

        "🤖 FORECAST ACCURACY\n"
        f"Solar error: {signed(solar_error, '%')}\n"
        f"Load error: {signed(load_error, '%')}\n"
        f"Next-solar SOC error: "
        f"{signed(soc_error, ' pp')}\n\n"

        "✅ Daily validation complete."
    )

    notify(message)

    return True


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    records = conn.execute("""
        SELECT *
        FROM strategy_validation
        WHERE status = 'PENDING'
        ORDER BY plan_date
    """).fetchall()

    if not records:
        print("No pending Strategy Validation records.")
        conn.close()
        return

    print("STRATEGY VALIDATION ACTUALS")
    print("===========================")

    for record in records:
        validate_record(conn, record)

    conn.close()


if __name__ == "__main__":
    main()
