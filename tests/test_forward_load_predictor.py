import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone

import forward_load_predictor as predictor


class ForwardLoadPredictorTests(unittest.TestCase):
    def test_expected_samples_follow_sydney_daylight_saving_day_length(self):
        self.assertEqual(
            predictor.expected_five_minute_readings(date(2026, 10, 4)),
            276,
        )
        self.assertEqual(
            predictor.expected_five_minute_readings(date(2026, 10, 5)),
            288,
        )
        self.assertEqual(
            predictor.expected_five_minute_readings(date(2027, 4, 4)),
            300,
        )

    def test_one_missing_sample_on_spring_forward_day_is_complete(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE foxess_history(timestamp TEXT, load_kw REAL)")
        conn.execute("CREATE TABLE foxess_live(timestamp TEXT, load_kw REAL)")

        start = datetime(2026, 10, 4, 0, 0, tzinfo=predictor.TZ)
        stamps = [
            (
                start.astimezone(timezone.utc) + timedelta(minutes=5 * index)
            ).astimezone(predictor.TZ).isoformat()
            for index in range(276)
        ]
        del stamps[120]
        conn.executemany(
            "INSERT INTO foxess_live VALUES (?, ?)",
            [(stamp, 1.0) for stamp in stamps],
        )
        conn.commit()

        totals = predictor.actual_daily_load(conn, today=date(2026, 10, 5))
        conn.close()

        self.assertIn("2026-10-04", totals)
        self.assertAlmostEqual(totals["2026-10-04"], 275 / 12.0)


if __name__ == "__main__":
    unittest.main()
