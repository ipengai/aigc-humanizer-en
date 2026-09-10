import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DetectorPageUiContractTests(unittest.TestCase):
    def test_risk_bands_and_navigation_match_product_policy(self):
        script = (ROOT / "static" / "detector.js").read_text(encoding="utf-8")
        detector_page = (ROOT / "templates" / "ai_detect.html").read_text(encoding="utf-8")
        faq = (ROOT / "templates" / "faq.html").read_text(encoding="utf-8")
        orders = (ROOT / "templates" / "orders.html").read_text(encoding="utf-8")
        main_routes = (ROOT / "app" / "routes" / "main.py").read_text(encoding="utf-8")

        self.assertIn("if (risk > 60)", script)
        self.assertIn("if (risk >= 20)", script)
        self.assertIn("const showHigh = risk >= 20", script)
        self.assertNotIn("困惑度、突发性", detector_page)
        self.assertIn('href="/ai-detect/"', faq)
        self.assertIn('href="/ai-detect/"', orders)
        self.assertIn("site_url + '/ai-detect/'", main_routes)

    def test_recharge_button_is_reenabled_after_each_attempt(self):
        script = (ROOT / "static" / "detector.js").read_text(encoding="utf-8")
        recharge_function = script.split("async function submitDetRecharge()", 1)[1]
        self.assertIn("detSubmitting = false", recharge_function)
        self.assertIn("submitBtns.forEach(b => b.disabled = false)", recharge_function)
        self.assertIn("if (submitBtn) submitBtn.disabled = false", script)

    def test_primary_navigation_uses_one_label_and_order(self):
        expected = ["AI Writer", "AI Detector", "使用帮助", "定价"]
        for filename in ("index.html", "ai_detect.html", "faq.html", "orders.html"):
            page = (ROOT / "templates" / filename).read_text(encoding="utf-8")
            nav = page.split('<div class="nav-links">', 1)[1].split('</div>', 1)[0]
            positions = [nav.index(label) for label in expected]
            self.assertEqual(positions, sorted(positions), filename)
            self.assertNotIn("AI改写", nav, filename)


if __name__ == "__main__":
    unittest.main()
