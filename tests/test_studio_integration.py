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
                if self.server.fail_next:
                    self.server.fail_next = False
                    self.send_json({"detail": "temporarily unavailable"}, status=503)
                    return
                reply = self.server.replies.pop(0) if self.server.replies else {}
                self.send_json({"model": "test-model", "answers": reply, "usage": {"input_tokens": 100, "output_tokens": 3},
                                "onejev": {"timing": {"total_s": 0.01}}})

            def send_json(self, payload, status=200):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        cls.model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        cls.model.requests, cls.model.replies = [], []
        cls.model.fail_next = False
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
        self.model.fail_next = False

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

    def test_failed_inference_can_resume_without_repeating_the_payment(self):
        self.model.replies = [answers(), answers("retry"), answers("paid", .98)]
        identifier = self.create()
        self.step(identifier)
        self.model.fail_next = True
        failure = self.step(identifier)
        self.assertTrue(failure["retryable"])
        self.assertIsNone(failure["result"])
        self.assertEqual(failure["attempts"], 1)
        self.assertEqual(failure["history"][-1]["page_status"], "retry")
        self.assertEqual(failure["history"][-1]["decision"]["action"], "reconnect")
        recovered = self.step(identifier)
        self.assertFalse(recovered["retryable"])
        self.assertIsNone(recovered["error"])
        self.assertEqual(recovered["attempts"], 1, "Inference retry must not click payment again")
        self.assertEqual(recovered["history"][-1]["page_status"], "retry")
        completed = self.step(identifier)
        self.assertEqual(completed["attempts"], 2)
        self.assertEqual(completed["result"], "verified")

    def test_speed_change_interrupts_the_current_wait(self):
        self.model.replies = [answers(), answers("retry"), answers("paid", .98)]
        identifier = self.create()
        self.api(f"/api/sessions/{identifier}/auto", {"delay": 10})
        for _ in range(100):
            state = self.api(f"/api/sessions/{identifier}")
            if state["next_step_at"] is not None:
                break
            time.sleep(.02)
        self.assertGreater(state["next_step_at"], time.time())
        started = time.monotonic()
        self.api(f"/api/sessions/{identifier}/auto", {"delay": .5})
        for _ in range(150):
            state = self.api(f"/api/sessions/{identifier}")
            if len(state["history"]) >= 2:
                break
            time.sleep(.02)
        self.api(f"/api/sessions/{identifier}/pause", {})
        self.assertGreaterEqual(len(state["history"]), 2)
        self.assertLess(time.monotonic() - started, 3.5)

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

    def test_robot_viewer_uses_one_origin_and_sends_only_camera_input(self):
        import importlib.util
        from playwright.sync_api import sync_playwright

        if importlib.util.find_spec("viser") is None:
            self.skipTest("install the robot optional dependencies")
        self.api("/api/robot/reset", {"scenario": "normal"})
        self.model.replies = [{"placed": {"type": "noul", "noul": .02},
                               "grasped": {"type": "noul", "noul": .03},
                               "location": {"type": "choice", "choice": "table",
                                            "probabilities": {"table": .97, "gripper": .02, "tray": .01}}}]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("QEV_BROWSER_CHANNEL"), headless=True,
                                                  args=["--enable-unsafe-swiftshader"])
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 1000})
                connections = []
                page.on("websocket", lambda connection: connections.append(connection.url))
                page.goto(self.base + "/#robot")
                page.wait_for_function('document.getElementById("robot-connection").textContent.includes("已连接")')
                page.locator("#robot-observe").click()
                page.wait_for_function('document.getElementById("robot-history").querySelectorAll("button").length === 1')
                self.assertTrue(any(url.startswith(self.base.replace("http:", "ws:") + "/robot-ws") for url in connections))
                self.assertEqual(self.model.requests[-1]["state"]["camera"], "<image:1>")
                self.assertEqual(set(self.model.requests[-1]["state"]), {"task", "camera"})
                self.assertTrue(self.model.requests[-1]["media"][0]["data"].startswith("data:image/png;base64,"))
                self.assertIn("2.0", page.locator("#robot-placed").inner_text())
                page.locator("#robot-auto-judge").uncheck()
                page.locator("#robot-step").click()
                page.wait_for_function('document.getElementById("robot-phase").textContent === "已靠近物体"')
                self.assertIn("待判断", page.locator("#robot-verdict").inner_text())
            finally:
                browser.close()

    def test_robot_case_picker_clean_mobile_view_and_network_recovery_preserve_scene(self):
        import importlib.util
        from playwright.sync_api import sync_playwright

        if importlib.util.find_spec("viser") is None:
            self.skipTest("install the robot optional dependencies")
        initial = self.api("/api/robot/reset", {"scenario": "normal"})
        self.api("/api/robot/settings", {"judge": True, "motion_speed": 2})
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("QEV_BROWSER_CHANNEL"), headless=True,
                                                 args=["--enable-unsafe-swiftshader"])
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(self.base + "/#robot")
                page.wait_for_function('!document.getElementById("robot-camera-reset").disabled')
                frame = page.frame_locator("#robot-viewer")
                sheet = frame.locator('.mantine-Paper-root[style*="position: fixed"][style*="max-height: 60%"]')
                sheet.wait_for(state="attached")
                self.assertEqual(sheet.count(), 1)
                self.assertFalse(sheet.is_visible())
                page.locator("#robot-view-settings").click()
                page.wait_for_function('document.getElementById("robot-view-settings").getAttribute("aria-expanded") === "true"')
                sheet.wait_for(state="visible")
                self.assertTrue(sheet.is_visible())
                page.locator("#robot-view-settings").click()
                sheet.wait_for(state="hidden")
                page.locator("#robot-camera-reset").click()
                self.assertEqual(self.api("/api/robot")["revision"], initial["revision"])
                held = []
                page.route("**/api/robot/settings", lambda route: held.append(route))
                page.locator("#robot-auto-judge").uncheck()
                self.assertFalse(page.locator("#robot-auto-judge").is_checked())
                self.assertTrue(page.locator("#robot-step").is_disabled())
                page.wait_for_function('document.getElementById("robot-auto-judge").disabled')
                self.assertEqual(len(held), 1)
                held[0].continue_()
                page.unroute("**/api/robot/settings")
                page.wait_for_function('!document.getElementById("robot-step").disabled')
                self.assertFalse(self.api("/api/robot")["auto_judge"])
                page.locator("#robot-scenario").select_option("miss")
                page.locator("#robot-load-scenario").click()
                page.wait_for_function('document.getElementById("robot-case-title").textContent === "抓空后重试"')
                loaded = self.api("/api/robot")
                self.assertNotEqual(loaded["id"], initial["id"])
                page.locator("#robot-step").click()
                page.wait_for_function('document.getElementById("robot-step").textContent.includes("设置抓空")')
                page.locator("#robot-step").click()
                page.wait_for_function('document.getElementById("robot-fault-hint").textContent.includes("已设置")')
                fault = self.api("/api/robot")
                self.assertEqual(fault["events"][-1]["kind"], "fault")
                page.route("**/api/robot", lambda route: route.abort() if route.request.method == "GET" else route.continue_())
                page.wait_for_function('!document.getElementById("robot-network-error").hidden')
                self.assertTrue(page.locator("#robot-step").is_disabled())
                page.unroute("**/api/robot")
                page.wait_for_function('document.getElementById("robot-network-error").hidden && !document.getElementById("robot-step").disabled')
                page.locator("#robot-view-reconnect").click()
                page.wait_for_function('!document.getElementById("robot-camera-reset").disabled')
                after = self.api("/api/robot")
                self.assertEqual(after["id"], fault["id"])
                self.assertEqual(after["revision"], fault["revision"])
                self.assertEqual(after["q"], fault["q"])
                self.assertFalse(self.model.requests)
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                self.assertFalse(errors)
            finally:
                browser.close()

    def test_robot_drop_preset_completes_with_recorded_fault_and_recovery(self):
        import importlib.util

        if importlib.util.find_spec("viser") is None:
            self.skipTest("install the robot optional dependencies")
        state = self.api("/api/robot/reset", {"scenario": "drop"})
        self.api("/api/robot/settings", {"judge": False, "motion_speed": 2})
        for _ in range(10):
            if state["next_action"] is None:
                break
            state = self.api("/api/robot/step", {"run_id": state["id"], "expected_revision": state["revision"], "judge": False})
        self.assertEqual(state["phase"], "complete")
        self.assertTrue(state["truth"]["placed"])
        self.assertTrue(state["scenario"]["fault_done"])
        self.assertEqual(len([e for e in state["events"] if e["kind"] == "fault"]), 1)
        self.assertTrue(any(e["label"] == "重新抓取" for e in state["events"]))
        self.assertFalse(state["history"])
        self.assertFalse(self.model.requests)
        self.api("/api/robot/reset", {"scenario": "unknown"}, expected=422)

    def test_robot_archived_replay_is_read_only_and_stale_controls_are_rejected(self):
        import importlib.util

        if importlib.util.find_spec("viser") is None:
            self.skipTest("install the robot optional dependencies")
        initial = self.api("/api/robot/reset", {"scenario": "normal"})
        self.api("/api/robot/settings", {"judge": False, "motion_speed": 2})
        stale = {"run_id": initial["id"], "expected_revision": initial["revision"], "judge": False}
        moved = self.api("/api/robot/step", stale)
        self.api("/api/robot/step", stale, expected=409)
        self.assertEqual(moved["phase"], "hover")
        self.assertEqual(len(moved["events"]), 2)
        current = self.api("/api/robot/reset", {"scenario": "normal"})
        replay = self.api("/api/robot/replay", {"source_id": initial["id"], "event_index": 1})
        self.assertEqual(replay["id"], current["id"])
        self.assertEqual(replay["phase"], "ready")
        self.assertEqual(replay["revision"], 0)
        self.assertEqual(replay["replay"]["pose"]["phase"], "hover")
        self.api("/api/robot/step", {"judge": False}, expected=409)
        self.api("/api/robot/observe", {}, expected=409)
        self.assertFalse(self.model.requests)
        archived = self.api(f'/api/robot/runs/{initial["id"]}')
        self.assertEqual(len(archived["events"]), 2)
        self.assertIn(initial["id"], [r["id"] for r in self.api("/api/robot/runs")["runs"]])
        self.api("/api/robot/runs/invalid", expected=404)
        self.api("/api/robot/settings", {"delay": 0}, expected=422)
        restored = self.api("/api/robot/live", {})
        self.assertIsNone(restored["replay"])
        self.assertEqual(restored["q"], current["q"])
        self.api("/api/robot/pause", {"run_id": initial["id"]}, expected=409)

    def test_robot_two_tabs_share_playback_after_reload_and_replay_without_inference(self):
        import importlib.util
        from playwright.sync_api import sync_playwright

        if importlib.util.find_spec("viser") is None:
            self.skipTest("install the robot optional dependencies")
        self.api("/api/robot/reset", {"scenario": "normal"})
        self.api("/api/robot/settings", {"judge": False, "delay": 15, "motion_speed": 2})
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("QEV_BROWSER_CHANNEL"), headless=True,
                                                 args=["--enable-unsafe-swiftshader"])
            try:
                context = browser.new_context(viewport={"width": 1400, "height": 1000})
                first, second = context.new_page(), context.new_page()
                errors = []
                for page in (first, second):
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(self.base + "/#robot")
                    page.wait_for_function('document.getElementById("robot-connection").textContent.includes("已连接")')
                first.locator("#robot-auto").click()
                first.reload()
                first.wait_for_function('document.getElementById("robot-auto").textContent.includes("暂停")')
                second.wait_for_function('document.getElementById("robot-phase").textContent === "已靠近物体"')
                second.locator("#robot-auto").click()
                first.wait_for_function('document.getElementById("robot-auto").textContent.includes("自动演示")')
                state = self.api("/api/robot")
                self.assertEqual(state["revision"], 1)
                self.assertFalse(state["auto_running"])
                first.locator('[data-robot-event="0"]').click()
                for page in (first, second):
                    page.wait_for_function('!document.getElementById("robot-replay-notice").hidden')
                    self.assertTrue(page.locator("#robot-step").is_disabled())
                    self.assertTrue(page.locator("#robot-observe").is_disabled())
                self.assertFalse(self.model.requests)
                self.assertEqual(self.api("/api/robot")["revision"], 1)
                second.locator("#robot-live").click()
                first.wait_for_function('document.getElementById("robot-replay-notice").hidden')
                self.assertEqual(first.locator("#robot-phase").inner_text(), "已靠近物体")
                first.set_viewport_size({"width": 390, "height": 844})
                self.assertTrue(first.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
                # Browser-demo named grid areas must not create implicit robot columns.
                self.assertEqual(first.locator(".robot-results").evaluate('(el) => getComputedStyle(el).gridTemplateColumns.split(" ").length'), 2)
                self.assertGreater(first.locator(".robot-results > .next-action").bounding_box()["width"], 250)
                self.assertFalse(errors)
            finally:
                browser.close()

    def test_frontend_keeps_session_during_transient_network_failure(self):
        from playwright.sync_api import sync_playwright

        self.model.replies = [answers()]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("QEV_BROWSER_CHANNEL"), headless=True)
            try:
                page = browser.new_page()
                page.goto(self.base)
                page.locator("#step").click()
                page.wait_for_function('document.getElementById("step-count").textContent === "1 步"')
                identifier = page.evaluate('localStorage.getItem("onejev-studio-session")')
                page.route("**/api/sessions/*", lambda route: route.abort() if route.request.method == "GET" else route.continue_())
                page.wait_for_function('document.body.dataset.modelState === "offline"')
                self.assertEqual(page.evaluate('localStorage.getItem("onejev-studio-session")'), identifier)
                self.assertIn(identifier, page.locator("#frame").get_attribute("src"))
                page.unroute("**/api/sessions/*")
                page.wait_for_function('document.body.dataset.modelState === "ready"')
                self.assertEqual(page.evaluate('localStorage.getItem("onejev-studio-session")'), identifier)
                self.assertEqual(page.locator("#step-count").inner_text(), "1 步")
            finally:
                browser.close()

    def test_frontend_slow_poll_cannot_replace_a_new_case(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("QEV_BROWSER_CHANNEL"), headless=True)
            try:
                page = browser.new_page()
                page.goto(self.base)
                page.wait_for_function('document.getElementById("frame").naturalWidth > 0')
                original = page.evaluate('localStorage.getItem("onejev-studio-session")')
                held = {}

                def delay_poll(route):
                    if route.request.method == "GET" and not held:
                        held["route"], held["response"] = route, route.fetch()
                    else:
                        route.continue_()

                page.route("**/api/sessions/*", delay_poll)
                for _ in range(100):
                    if held:
                        break
                    page.wait_for_timeout(20)
                self.assertIn("route", held)
                page.locator("#reset").click()
                page.wait_for_function('(old) => localStorage.getItem("onejev-studio-session") !== old', arg=original)
                current = page.evaluate('localStorage.getItem("onejev-studio-session")')
                held["route"].fulfill(response=held["response"])
                page.wait_for_timeout(150)
                self.assertEqual(page.evaluate('localStorage.getItem("onejev-studio-session")'), current)
                self.assertIn(current, page.locator("#frame").get_attribute("src"))
            finally:
                browser.close()

    def test_frontend_history_replay_does_not_show_live_playback_controls(self):
        from playwright.sync_api import sync_playwright

        self.model.replies = [answers()]
        identifier = None
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("QEV_BROWSER_CHANNEL"), headless=True)
            try:
                page = browser.new_page()
                page.goto(self.base)
                page.wait_for_function('document.getElementById("frame").naturalWidth > 0')
                identifier = page.evaluate('localStorage.getItem("onejev-studio-session")')
                self.api(f"/api/sessions/{identifier}/auto", {"delay": 10})
                page.wait_for_function('document.getElementById("step-count").textContent === "1 步"')
                page.locator('[data-tab="runs"]').click()
                page.locator(f'[data-run="{identifier}"]').click()
                page.wait_for_function('!document.getElementById("panel-browser").hidden')
                self.assertTrue(page.locator("#pause").is_hidden())
                self.assertTrue(page.locator("#auto-countdown").is_hidden())
                self.assertTrue(page.locator("#step").is_disabled())
            finally:
                if identifier:
                    self.api(f"/api/sessions/{identifier}/pause", {})
                browser.close()

    def test_frontend_invalidates_results_when_image_or_statement_changes(self):
        from playwright.sync_api import sync_playwright

        self.model.replies = [{"completed": {"type": "noul", "noul": .98}}]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("QEV_BROWSER_CHANNEL"), headless=True)
            try:
                page = browser.new_page()
                page.goto(self.base)
                page.locator('[data-tab="image"]').click()
                page.locator("#image-sample").click()
                page.wait_for_function('!document.getElementById("image-preview").hidden')
                page.locator("#image-submit").click()
                page.wait_for_selector("#image-answers .answer-big")
                page.locator("#image-statement").fill("图片里的发票仍然未付款。")
                self.assertEqual(page.locator("#image-answers .answer-big").count(), 0)
                self.assertIn("输入已更改", page.locator("#image-answers").inner_text())
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
