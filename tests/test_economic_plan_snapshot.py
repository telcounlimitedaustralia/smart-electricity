import os
import sqlite3
import tempfile
import threading
import time
import unittest

import economic_plan_snapshot as snapshot


class EconomicPlanSnapshotTests(unittest.TestCase):
    def test_plan_write_waits_for_brief_collector_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = os.path.join(folder, "energy.db")
            ready = threading.Event()

            def collector_write():
                conn = sqlite3.connect(db_path)
                conn.execute("CREATE TABLE IF NOT EXISTS telemetry (value INTEGER)")
                conn.commit()
                conn.execute("BEGIN EXCLUSIVE")
                conn.execute("INSERT INTO telemetry VALUES (1)")
                ready.set()
                time.sleep(0.2)
                conn.commit()
                conn.close()

            collector = threading.Thread(target=collector_write)
            collector.start()
            self.assertTrue(ready.wait(timeout=2))

            def save(conn):
                conn.execute("CREATE TABLE plan (value INTEGER)")
                conn.execute("INSERT INTO plan VALUES (7)")

            snapshot.write_with_retry(save, db_path)
            collector.join(timeout=2)

            conn = sqlite3.connect(db_path)
            value = conn.execute("SELECT value FROM plan").fetchone()[0]
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()

            self.assertEqual(value, 7)
            self.assertEqual(mode.lower(), "wal")


if __name__ == "__main__":
    unittest.main()
