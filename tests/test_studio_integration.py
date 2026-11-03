"""Opt-in Studio HTTP + real Chrome tests against scripted model responses."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from test_browser_verify import answers

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("QEV_BROWSER_TESTS") == "1", "set QEV_BROWSER_TESTS=1 to run Studio checks")
class StudioIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class ModelHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_json({"status": "ok", "model": "test-model"})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.requests.append(payload)
                reply = self.server.replies.pop(0) if self.server.replies else {}
                self.send_json({"model": "test-model", "answers": reply, "usage": {"input_tokens": 100, "output_tokens": 3},
                                "onejev": {"timing": {"total_s": 0.01}}})

            def send_json(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        cls.model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        cls.model.requests, cls.model.replies = [], []
        cls.thread = threading.Thread(target=cls.model.serve_forever, daemon=True)
        cls.thread.start()
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.temp = tempfile.TemporaryDirectory()
        cls.log = open(Path(cls.temp.name) / "web.log", "w+")
        command = [sys.executable, "-m", "qev.studio", "--port", str(port), "--no-autostart",
                   "--backend-url", f"http://127.0.0.1:{cls.model.server_port}", "--output", cls.temp.name]
        if os.environ.get("QEV_BROWSER_CHANNEL"):
            command += ["--channel", os.environ["QEV_BROWSER_CHANNEL"]]
        cls.process = subprocess.Popen(command, cwd=ROOT, stdout=cls.log, stderr=subprocess.STDOUT)
        for _ in range(200):
            try:
                with urlopen(cls.base + "/api/status", timeout=1) as response:
                    if json.load(response)["state"] == "ready":
                        return
            except (URLError, OSError):
                pass
            if cls.process.poll() is not None:
                break
            time.sleep(0.05)
        cls.log.seek(0)
        raise RuntimeError("Studio failed to start: " + cls.log.read())

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=15)
        cls.model.shutdown()
        cls.model.server_close()
        cls.thread.join(timeout=5)
        cls.log.close()
        cls.temp.cleanup()

    def setUp(self):
        self.model.requests, self.model.replies = [], []

    def api(self, path, body=None, expected=200, binary=False):
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read()
            self.assertEqual(response.status, expected, payload.decode(errors="replace"))
        return payload if binary else json.loads(payload)

    def create(self, scenario="retry"):
        return self.api("/api/sessions", {"scenario": scenario})["id"]

    def step(self, identifier):
        return self.api(f"/api/sessions/{identifier}/step", {})

    def test_manual_steps_preserve_each_state_and_saved_report(self):
        self.model.replies = [answers(), answers("retry"), answers("paid", .98)]
        identifier = self.create()
        for index, expected in enumerate(("unpaid", "retry", "paid")):
            state = self.step(identifier)
            self.assertEqual(state["history"][-1]["page_status"], expected)
            self.assertEqual(len(state["history"]), index + 1)
            self.assertFalse(state["busy"])
        self.assertEqual(state["result"], "verified")
        self.assertEqual(state["attempts"], 2)
        self.assertEqual(self.api(f"/api/runs/{identifier}")["result"], "verified")
        self.assertTrue(self.api(state["frame"], binary=True).startswith(b"\x89PNG"))
        self.assertIn(identifier, [run["id"] for run in self.api("/api/runs")["runs"]])
        self.assertNotIn("page_status", self.model.requests[0]["state"])

    def test_model_false_completion_is_stopped_by_page_evidence(self):
        self.model.replies = [answers("paid", .99)]
        state = self.step(self.create())
        self.assertEqual(state["result"], "review")
        self.assertEqual(state["attempts"], 0)

    def test_decline_and_attempt_limit(self):
        for scenario, replies, result, attempts in (
                ("blocked", [answers(), answers("blocked")], "review", 1),
                ("exhausted", [answers(), answers("retry"), answers("retry")], "exhausted", 2)):
            with self.subTest(scenario=scenario):
                self.model.replies = replies
                identifier = self.create(scenario)
                while self.model.replies:
                    state = self.step(identifier)
                self.assertEqual(state["result"], result)
                self.assertEqual(state["attempts"], attempts)

    def test_processing_does_not_resubmit_and_observation_limit_stops_it(self):
        self.model.replies = [answers()] + [answers("processing") for _ in range(5)]
        identifier = self.create("pending")
        for _ in range(6):
            state = self.step(identifier)
        self.assertEqual(state["result"], "exhausted")
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(len(state["history"]), 6)

    def test_pause_stops_progress_and_auto_can_resume(self):
        self.model.replies = [answers(), answers("retry"), answers("paid", .98)]
        identifier = self.create()
        self.api(f"/api/sessions/{identifier}/auto", {"delay": .5})
        for _ in range(100):
            state = self.api(f"/api/sessions/{identifier}")
            if len(state["history"]) == 1:
                break
            time.sleep(.02)
        self.api(f"/api/sessions/{identifier}/pause", {})
        time.sleep(.6)
        state = self.api(f"/api/sessions/{identifier}")
        self.assertEqual(len(state["history"]), 1)
        self.assertFalse(state["auto_running"])
        self.api(f"/api/sessions/{identifier}/auto", {"delay": .5})
        for _ in range(150):
            state = self.api(f"/api/sessions/{identifier}")
            if state["result"]:
                break
            time.sleep(.02)
        self.assertEqual(state["result"], "verified")

    def test_text_and_image_requests_share_the_same_model_connection(self):
        self.model.replies = [{"completed": {"type": "noul", "noul": .97}}]
        text = self.api("/api/ask", {"preset": "completion", "text": "谢谢，问题解决了。", "statement": "问题已经解决。"})
        self.assertEqual(text["answers"]["completed"]["noul"], .97)
        self.assertEqual(text["meta"]["timing"]["total_s"], .01)
        import base64

        png = self.api("/api/sample.png", binary=True)
        self.model.replies = [{"completed": {"type": "noul", "noul": .98}}]
        image = self.api("/api/ask", {"mode": "image", "statement": "The invoice is paid.",
                                     "image": "data:image/png;base64," + base64.b64encode(png).decode()})
        self.assertEqual(image["answers"]["completed"]["noul"], .98)
        self.assertEqual(self.model.requests[-1]["state"]["image"], "<image:1>")

    def test_bad_inputs_are_rejected_without_inference(self):
        self.api("/api/sessions", {"scenario": "unknown"}, expected=422)
        self.api("/api/ask", {"text": " "}, expected=400)
        self.api("/api/ask", {"mode": "image", "image": "data:image/png;base64,bad"}, expected=400)
        self.api("/api/runs/not-a-run", expected=404)
        identifier = self.create()
        self.api(f"/api/sessions/{identifier}/auto", {"delay": -1}, expected=422)
        self.assertEqual(self.model.requests, [])

    def test_invalid_prediction_keeps_evidence_and_returns_an_error_state(self):
        self.model.replies = [answers(completed=float("nan"))]
        state = self.step(self.create())
        self.assertEqual(state["result"], "error")
        self.assertEqual(state["history"][0]["decision"]["action"], "error")
        self.assertEqual(state["history"][0]["answers"]["completed"]["noul"], "NaN")


if __name__ == "__main__":
    unittest.main()
