import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TargetedRewriteUiContractTests(unittest.TestCase):
    def test_targeted_stage_is_visible_and_uses_backend_message(self):
        template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        common = (ROOT / "static" / "common.js").read_text(encoding="utf-8")
        main = (ROOT / "static" / "main.js").read_text(encoding="utf-8")

        self.assertIn('data-step="targeted_rewrite"', template)
        self.assertIn("'targeted_rewrite'", common)
        self.assertIn(
            "const AUTO_LOADING_STEPS = ['parse', 'detect', 'rewrite', 'detect_again']",
            common,
        )
        self.assertIn("idx < AUTO_LOADING_STEPS.length", common)
        self.assertIn("loadingTitle.textContent = prog.message", main)


if __name__ == "__main__":
    unittest.main()
