from datetime import datetime, time

HIGH_PEAK = 41.91
LOW_PEAK = 42.416
SHOULDER = 11.11
OFF_PEAK = 32.12

FIT_STANDARD = 3.0
FIT_EVENING = 28.0

SUPPLY_CHARGE = 166.617


def high_season(dt):
    # November to March
    return dt.month in (11, 12, 1, 2, 3)


def import_rate(dt):
    weekday = dt.weekday() < 5
    t = dt.time()

    # Solar soak: 10am-2pm every day
    if time(10, 0) <= t < time(14, 0):
        return SHOULDER, "SHOULDER"

    # Peak: 4pm-8pm business days
    if weekday and time(16, 0) <= t < time(20, 0):
        if high_season(dt):
            return HIGH_PEAK, "HIGH_SEASON_PEAK"
        else:
            return LOW_PEAK, "LOW_SEASON_PEAK"

    return OFF_PEAK, "OFF_PEAK"


def export_rate(dt):
    t = dt.time()

    # Premium FIT: 5pm-9pm every day
    if time(17, 0) <= t < time(21, 0):
        return FIT_EVENING, "EVENING_PEAK_FIT"

    return FIT_STANDARD, "OFF_PEAK_FIT"


def rates_at(dt=None):
    if dt is None:
        dt = datetime.now().astimezone()

    buy_rate, buy_period = import_rate(dt)
    sell_rate, sell_period = export_rate(dt)

    return {
        "timestamp": dt.isoformat(),
        "import_rate": buy_rate,
        "import_period": buy_period,
        "export_rate": sell_rate,
        "export_period": sell_period,
    }


if __name__ == "__main__":
    r = rates_at()

    print("CURRENT ELECTRICITY TARIFF")
    print("==========================")
    print("Time:", r["timestamp"])
    print("Import:", r["import_rate"], "c/kWh", "-", r["import_period"])
    print("Export:", r["export_rate"], "c/kWh", "-", r["export_period"])
