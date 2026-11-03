"""Opt-in browser/HTTP checks with deterministic model replies; no GPU needed.

    QEV_BROWSER_TESTS=1 QEV_BROWSER_CHANNEL=chrome python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_browser_verify import answers

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("QEV_BROWSER_TESTS") == "1", "set QEV_BROWSER_TESTS=1 to run browser checks")
class BrowserIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = b"<!doctype html><html><title>Local test</title><h1>Task complete</h1></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.requests.append(request)
                reply = self.server.replies.pop(0) if self.server.replies else {}
                payload = {"model": "test-onejev", "answers": reply,
                           "usage": {"input_tokens": 100, "output_tokens": len(reply)},
                           "onejev": {"timing": {"total_s": 0.01}}}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("x-typesafe-request-id", "browser-test")
                self.end_headers()
                self.wfile.write(body)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def run_case(self, replies, *arguments, expected_code=0):
        self.server.replies = list(replies)
        self.server.requests = []
        with tempfile.TemporaryDirectory() as temp:
            command = [sys.executable, "examples/browser_verify.py", "--base-url", self.base_url,
                       "--output", temp, "--page-timeout", "1"]
            if os.environ.get("QEV_BROWSER_CHANNEL"):
                command += ["--channel", os.environ["QEV_BROWSER_CHANNEL"]]
            process = subprocess.run(command + list(arguments), cwd=ROOT, text=True, capture_output=True, timeout=45)
            self.assertEqual(process.returncode, expected_code, process.stdout + process.stderr)
            reports = list(Path(temp).glob("*/report.json"))
            self.assertEqual(len(reports), 1, process.stdout + process.stderr)
            report = json.loads(reports[0].read_text())
            if report["result"] == "error":
                self.assertTrue((reports[0].parent / "error.png").is_file())
            for observation in report["observations"]:
                self.assertTrue((reports[0].parent / observation["screenshot"]).is_file())
            for request in self.server.requests:
                self.assertEqual(request["state"]["screen"], "<image:1>")
                self.assertNotIn("page_status", request["state"])
                uri = request["media"][0]["data"]
                self.assertEqual(uri.split(",", 1)[0], "data:image/png;base64")
                self.assertTrue(base64.b64decode(uri.split(",", 1)[1]).startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertFalse(self.server.replies, "Not all expected observations were made")
            return report

    def test_successful_first_attempt(self):
        report = self.run_case([answers(), answers("paid", 0.98)], "--scenario", "success")
        self.assertEqual(report["result"], "verified")
        self.assertEqual([row["attempts"] for row in report["observations"]], [0, 1])
        self.assertEqual(report["observations"][1]["meta"]["timing"]["total_s"], 0.01)

    def test_temporary_failure_is_retried_once(self):
        report = self.run_case([answers(), answers("retry"), answers("paid", 0.98)])
        self.assertEqual([row["decision"]["action"] for row in report["observations"]], ["pay", "retry", "verified"])

    def test_declined_payment_stops(self):
        report = self.run_case([answers(), answers("blocked")], "--scenario", "blocked", expected_code=1)
        self.assertEqual(report["result"], "review")
        self.assertEqual(report["observations"][-1]["attempts"], 1)

    def test_attempt_limit_is_enforced(self):
        report = self.run_case([answers(), answers("retry"), answers("retry")], "--scenario", "exhausted",
                               expected_code=1)
        self.assertEqual(report["result"], "exhausted")
        self.assertEqual(report["observations"][-1]["attempts"], 2)

    def test_processing_payment_is_not_submitted_again(self):
        report = self.run_case([answers(), answers("processing"), answers("processing")], "--scenario", "pending",
                               "--max-observations", "3", expected_code=1)
        self.assertEqual(report["result"], "exhausted")
        self.assertEqual([row["attempts"] for row in report["observations"]], [0, 1, 1])

    def test_final_observation_does_not_start_an_unverifiable_action(self):
        report = self.run_case([answers()], "--max-observations", "1", expected_code=1)
        self.assertEqual(report["observations"][0]["decision"]["action"], "exhausted")
        self.assertEqual(report["observations"][0]["attempts"], 0)

    def test_false_completion_cannot_override_page_state(self):
        report = self.run_case([answers("paid", 0.99)], expected_code=1)
        self.assertEqual(report["result"], "review")
        self.assertEqual(report["observations"][0]["page_status"], "unpaid")

    def test_invalid_reply_retains_raw_answers(self):
        invalid = answers(completed=None)
        report = self.run_case([invalid], expected_code=2)
        self.assertEqual(report["result"], "error")
        self.assertEqual(report["observations"][0]["answers"], invalid)

    def test_existing_page_check_uses_the_requested_statement(self):
        report = self.run_case([{"completed": {"type": "noul", "noul": 0.97}}],
                               "--url", self.base_url, "--success", "The page says Task complete.", "--wait-for", "h1")
        self.assertEqual(report["result"], "verified")
        self.assertEqual(report["settings"]["mode"], "check")
        self.assertEqual(report["settings"]["success"], "The page says Task complete.")
        self.assertEqual(self.server.requests[0]["questions"]["completed"]["instructions"],
                         "The page says Task complete.")

    def test_missing_page_element_reports_a_browser_error(self):
        report = self.run_case([], "--url", self.base_url, "--success", "The page is ready.",
                               "--wait-for", "#missing-element", expected_code=2)
        self.assertEqual(report["result"], "error")
        self.assertIn("Locator.wait_for", report["reason"])
        self.assertEqual(self.server.requests, [])

    def test_unavailable_model_server_reports_endpoint_and_recovery(self):
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            endpoint = f"http://127.0.0.1:{unused.getsockname()[1]}"
            report = self.run_case([], "--base-url", endpoint, expected_code=2)
        self.assertEqual(report["result"], "error")
        self.assertIn(endpoint, report["reason"])
        self.assertIn("Start the model server", report["reason"])
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
