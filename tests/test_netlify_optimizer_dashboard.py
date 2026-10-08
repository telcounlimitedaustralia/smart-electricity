import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NetlifyOptimizerDashboardTests(unittest.TestCase):
    def test_read_only_site_is_generated_from_optimizer_dashboard(self):
        dashboard = (ROOT / "dashboard" / "templates" / "optimizer_review.html").read_text(
            encoding="utf-8"
        )
        readonly = (ROOT / "netlify" / "site" / "index.html").read_text(encoding="utf-8")

        self.assertIn("My Battery Plan", readonly)
        self.assertIn('id="batteryBridge"', readonly)
        self.assertIn('id="performanceChart"', readonly)
        self.assertIn('id="weekPlanDisclosure"', readonly)
        self.assertNotIn(">Main dashboard</a>", readonly)
        self.assertIn("/.netlify/functions/live-data?view=", readonly)
        self.assertNotIn("{{ page_title", readonly)
        self.assertNotIn("{% if show_main_dashboard_link", readonly)
        self.assertGreater(len(readonly), len(dashboard) - 200)

    def test_proxy_allows_optimizer_audit_read(self):
        proxy = (ROOT / "netlify" / "functions" / "live-data.mjs").read_text(
            encoding="utf-8"
        )

        self.assertIn('"optimizer-audit"', proxy)


if __name__ == "__main__":
    unittest.main()
