"""End-to-end account and routing-policy scenarios.

Run as a standard regression test:
    .venv/bin/python -m unittest tests.test_rewrite_routing_account_scenarios -v

Print the captured routing chains:
    .venv/bin/python -m tests.test_rewrite_routing_account_scenarios --report

All detector and rewriter calls are deterministic local fakes. The HTTP,
session, balance, order, background-job and persistence layers are real.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from flask import Flask

import config
import app.models as models
from app.extensions import limiter, set_adapters, set_ai_detector
from app.helpers import close_db
from app.humanizer.adapter import HumanizerAdapter
from app.routes.analysis import analysis_bp
from app.routes.auth import auth_bp
from app.routes.rewrite import rewrite_bp


class TraceProvider(HumanizerAdapter):
    def __init__(self, label):
        self.backend_label = label
        self.calls = []

    def humanize(self, text, mode=None, paragraphs=None):
        self.calls.append({
            "provider": self.backend_label,
            "words": len(text.split()),
            "marker": text.split()[0],
            "mode": mode,
        })
        return f"[{self.backend_label}] {text}"

    def humanize_structured(self, text, mode=None, paragraphs=None,
                             progress_cb=None):
        if paragraphs is None:
            return self.humanize(text, mode=mode), []
        return self._humanize_segmented_structured(
            mode or "median",
            paragraphs,
            lambda block: self.humanize(block, mode=mode),
            progress_cb=progress_cb,
            batch_short_blocks=False,
        )


class TraceDetector:
    backend = "turnitin_fit_detector_v2_test"

    def __init__(self):
        self.calls = []
        self.document_score = 72.0

    def __call__(self, text, stage=None):
        if stage == "route_block":
            if "LOW_RISK" in text:
                score = 10.0
            elif "MEDIUM_" in text:
                score = 30.0
            else:
                score = 70.0
        elif stage == "route_block_recheck":
            score = 55.0 if "MEDIUM_UPGRADE" in text else 12.0
        elif stage == "rewrite_detect_rewritten":
            score = 9.0
        else:
            score = self.document_score
        result = {
            "backend": self.backend,
            "risk_percent": score,
            "ai_score": score,
            "coverage": 1.0,
        }
        self.calls.append({
            "stage": stage,
            "score": score,
            "words": len(text.split()),
        })
        return result


def scenario_text():
    paragraphs = [
        "LOW_RISK People revise this paragraph carefully so its language remains natural and specific for readers today.",
        "MEDIUM_PASS Students compare evidence, explain their choices, and connect each claim to the surrounding discussion clearly.",
        "MEDIUM_UPGRADE Researchers organize these findings into a coherent account that still requires a stronger revision process.",
        "HIGH_ALPHA Automated prose often repeats predictable transitions and evenly shaped sentences across a long academic response.",
        "HIGH_BETA Careful writers vary emphasis, qualify uncertain claims, and select examples that fit the immediate argument closely.",
        "HIGH_GAMMA The final section relates practical consequences to limitations and identifies questions that remain unresolved for future study.",
    ]
    return "\n\n".join(paragraphs)


class ScenarioHarness:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="routing-scenarios-")
        self.original_db_dir = models.DB_DIR
        self.original_db_path = models.DB_PATH
        models.DB_DIR = self.temp.name
        models.DB_PATH = os.path.join(self.temp.name, "test.db")
        models.init_db()

        self.app = Flask(__name__)
        self.app.secret_key = "routing-scenario-secret"
        self.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        limiter.init_app(self.app)
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(analysis_bp)
        self.app.register_blueprint(rewrite_bp)
        self.app.teardown_appcontext(close_db)

        self.main = TraceProvider("configured_main")
        self.translation = TraceProvider("lynote_test")
        self.huma = TraceProvider("huma_test")
        self.detector = TraceDetector()
        set_adapters(
            payment=None,
            humanizer=self.main,
            providers={"translation": self.translation, "huma": self.huma},
        )
        set_ai_detector(self.detector)

    def close(self):
        models.DB_DIR = self.original_db_dir
        models.DB_PATH = self.original_db_path
        self.temp.cleanup()

    def _run_background_now(self, order_id, text, mode, paragraphs, attempt=1):
        from app.helpers.tasks import do_background_rewrite
        do_background_rewrite(order_id, text, mode, paragraphs)
        return True

    def run(self, policy, account_kind, index, clear_process_cache=False):
        for provider in (self.main, self.translation, self.huma):
            provider.calls.clear()
        self.detector.calls.clear()

        client = self.app.test_client()
        email = f"routing-{account_kind}-{index}@example.com"
        register = client.post("/api/register", json={
            "email": email,
            "password": "test1234",
            "confirm_password": "test1234",
        })
        assert register.status_code == 201, register.get_json()
        user = register.get_json()["user"]
        signup_balance = user["word_balance"]

        if account_kind == "existing_balance":
            conn = models.get_connection()
            try:
                models.User.add_balance(conn, user["id"], 4800)
                conn.commit()
            finally:
                conn.close()
        elif account_kind == "zero_balance":
            conn = models.get_connection()
            try:
                conn.execute(
                    "UPDATE users SET word_balance = 0 WHERE id = ?",
                    (user["id"],),
                )
                conn.commit()
            finally:
                conn.close()

        text = scenario_text()
        with mock.patch.object(config, "REWRITE_ROUTING_POLICY", policy), \
             mock.patch(
                 "app.helpers.tasks.submit_rewrite_task",
                 side_effect=self._run_background_now,
             ):
            analyze = client.post("/api/analyze", json={"text": text})
            assert analyze.status_code == 200, analyze.get_json()
            analyze_data = analyze.get_json()
            if clear_process_cache:
                from app.helpers import tasks
                with tasks._ORIGINAL_ANALYSIS_CACHE_LOCK:
                    tasks._ORIGINAL_ANALYSIS_CACHE.clear()
            rewrite = client.post("/api/rewrite", json={
                "text": text,
                "mode": "low",
            })
        assert rewrite.status_code == 200, rewrite.get_json()
        rewrite_data = rewrite.get_json()

        conn = models.get_connection()
        try:
            order = models.Order.get_by_order_id(conn, rewrite_data["order_id"])
            final_balance = models.User.get_balance(conn, user["id"])
        finally:
            conn.close()
        trace = json.loads(order["rewrite_route_trace"])
        calls = {
            "configured_main": list(self.main.calls),
            "translation": list(self.translation.calls),
            "huma": list(self.huma.calls),
        }
        return {
            "account_kind": account_kind,
            "test_user": email,
            "user_id": user["id"],
            "signup_balance": signup_balance,
            "balance_before": analyze_data["balance"],
            "balance_after": final_balance,
            "word_count": analyze_data["word_count"],
            "mode": "low",
            "policy": policy,
            "analyze_score": analyze_data["analysis"]["risk_percent"],
            "order_id": order["order_id"],
            "order_status": order["status"],
            "stored_original_score": order["original_score"],
            "stored_rewritten_score": order["rewritten_score"],
            "stored_rewrite_method": order["rewrite_method"],
            "stored_humanizer_backend": order["humanizer_backend"],
            "balance_words_used": order["balance_words_used"],
            "rewrite_block_count": order["rewrite_block_count"],
            "route": trace["route"],
            "steps": trace["steps"],
            "detector_calls": list(self.detector.calls),
            "provider_calls": calls,
        }


def run_scenario_matrix():
    harness = ScenarioHarness()
    try:
        results = []
        index = 0
        for account_kind in ("existing_balance", "new_signup"):
            for policy in ("legacy_whole_document", "risk_band_segmented"):
                index += 1
                results.append(harness.run(policy, account_kind, index))
        return results
    finally:
        harness.close()


class AccountRoutingScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = run_scenario_matrix()

    def test_all_account_policy_combinations_complete(self):
        self.assertEqual(len(self.results), 4)
        for result in self.results:
            self.assertEqual(result["order_status"], "completed")
            self.assertEqual(
                result["balance_before"] - result["word_count"],
                result["balance_after"],
            )
            if result["account_kind"] == "new_signup":
                self.assertEqual(result["signup_balance"], 200)

    def test_legacy_uses_configured_adapter_without_block_detection(self):
        for result in self.results:
            if result["policy"] != "legacy_whole_document":
                continue
            self.assertEqual(result["route"]["risk_band"], "legacy")
            self.assertEqual(len(result["provider_calls"]["configured_main"]), 1)
            self.assertFalse(result["provider_calls"]["translation"])
            self.assertFalse(result["provider_calls"]["huma"])
            self.assertFalse(any(
                call["stage"] == "route_block"
                for call in result["detector_calls"]
            ))

    def test_segmented_policy_records_six_block_routes(self):
        for result in self.results:
            if result["policy"] != "risk_band_segmented":
                continue
            self.assertEqual(result["route"]["risk_band"], "segment_route")
            self.assertEqual(len(result["steps"]), 6)
            self.assertEqual(
                [step["planned_action"] for step in result["steps"]],
                ["protect", "translation", "translation", "huma", "huma", "huma"],
            )
            self.assertEqual(result["steps"][2]["action_chain"], ["translation", "huma"])
            self.assertEqual(result["stored_rewrite_method"], "hybrid")
            self.assertEqual(
                result["stored_humanizer_backend"],
                "lynote_test->huma_test",
            )

    def test_low_risk_order_is_protected_without_balance_charge(self):
        harness = ScenarioHarness()
        try:
            harness.detector.document_score = 10.0
            result = harness.run(
                "risk_band_segmented", "zero_balance", 99,
                clear_process_cache=True,
            )
        finally:
            harness.close()
        self.assertEqual(result["balance_before"], 0)
        self.assertEqual(result["balance_after"], 0)
        self.assertEqual(result["balance_words_used"], 0)
        self.assertEqual(result["rewrite_block_count"], 0)
        self.assertEqual(result["route"]["risk_band"], "protect")
        self.assertEqual(result["stored_rewrite_method"], "none")
        self.assertFalse(result["provider_calls"]["configured_main"])
        self.assertFalse(result["provider_calls"]["translation"])
        self.assertFalse(result["provider_calls"]["huma"])


if __name__ == "__main__":
    if "--report" in sys.argv:
        print(json.dumps(run_scenario_matrix(), ensure_ascii=False, indent=2))
    else:
        unittest.main()
