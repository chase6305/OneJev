"""Model service ownership and persistent Studio artifacts, without a GPU."""
import asyncio
import errno
import json
import os
import tempfile
import threading
import unittest
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError, URLError

import httpx
from fastapi import HTTPException

from qev.studio import Backend, Config, Session, Studio, create_app


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_during_environment_probe_does_not_start_a_model_afterwards(self):
        started, release = threading.Event(), threading.Event()
        backend = Backend(Config())

        def check(*_args, **_kwargs):
            started.set()
            release.wait(timeout=2)
            return SimpleNamespace(returncode=0)

        with patch.object(backend.probe, "_get", side_effect=URLError("offline")), \
                patch("qev.studio.subprocess.run", side_effect=check), patch("qev.studio.subprocess.Popen") as spawn:
            backend.reconnect()
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            closing = asyncio.create_task(backend.close())
            try:
                await asyncio.sleep(0)
            finally:
                release.set()
                await closing
            spawn.assert_not_called()

    async def test_invalid_local_ports_fail_before_probing_or_starting_inference(self):
        for url in ("http://localhost:bad", "http://localhost:0", "http://localhost:65536", "http://[::1"):
            with self.subTest(url=url):
                backend = Backend(Config(backend_url=url))
                with patch.object(backend.probe, "_get", side_effect=URLError("offline")), \
                        patch("qev.studio.subprocess.run") as check, patch("qev.studio.subprocess.Popen") as spawn:
                    self.assertEqual((await backend.connect())["state"], "error")
                    check.assert_not_called()
                    spawn.assert_not_called()

    async def test_reconnects_share_one_task_and_close_waits_for_it_without_starting_a_model(self):
        started, release = threading.Event(), threading.Event()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app = create_app(Config(output=Path(temporary.name)))
        backend = app.state.studio.backend
        reconnect = next(route.endpoint for route in app.routes if route.path == "/api/connect")

        def probe(_path):
            started.set()
            release.wait(timeout=2)
            raise URLError("offline")

        with patch.object(backend.probe, "_get", side_effect=probe) as get, \
                patch("qev.studio.subprocess.run") as check, patch("qev.studio.subprocess.Popen") as spawn:
            for _ in range(12):
                await reconnect()
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            closing = asyncio.create_task(backend.close())
            try:
                await asyncio.sleep(.02)
                self.assertFalse(closing.done())
            finally:
                release.set()
                await closing
            await reconnect()
            await asyncio.sleep(0)
            get.assert_called_once()
            check.assert_not_called()
            spawn.assert_not_called()

    async def test_malformed_health_responses_leave_monitoring_recoverable_and_do_not_spawn(self):
        for payload in (None, [], "ok", {}, {"status": []}, {"status": "error"}):
            with self.subTest(payload=payload):
                backend = Backend(Config())
                with patch.object(backend.probe, "_get", return_value=payload), \
                        patch("qev.studio.subprocess.run") as probe, patch("qev.studio.subprocess.Popen") as spawn:
                    self.assertEqual((await backend.connect())["state"], "error")
                    probe.assert_not_called()
                    spawn.assert_not_called()
                with patch.object(backend.probe, "_get", return_value={"status": "ok", "model": "recovered"}):
                    self.assertEqual((await backend.refresh())["state"], "ready")

    async def test_reachable_unhealthy_server_does_not_start_another_process_on_its_port(self):
        errors = [HTTPError("http://127.0.0.1:8000/health", code, "unhealthy", {}, None) for code in (404, 429, 500)]
        errors += [json.JSONDecodeError("invalid health JSON", "not JSON", 0),
                   UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid health encoding"),
                   URLError(IncompleteRead(b"{", 20))]
        for error in errors:
            with self.subTest(error=repr(error)):
                backend = Backend(Config())
                with patch.object(backend.probe, "_get", side_effect=error), \
                        patch("qev.studio.subprocess.run") as probe, patch("qev.studio.subprocess.Popen") as spawn:
                    self.assertEqual((await backend.connect())["state"], "error")
                    probe.assert_not_called()
                    spawn.assert_not_called()

    async def test_reuses_a_healthy_server_without_starting_a_second_model(self):
        backend = Backend(Config(inference_python="/does/not/exist"))
        with patch.object(backend.probe, "_get", return_value={"status": "ok", "model": "existing-model"}), \
                patch("qev.studio.subprocess.Popen") as spawn:
            status = await backend.connect()
            self.assertEqual(status["state"], "ready")
            self.assertEqual(status["model"], "existing-model")
            self.assertIsNone(backend.process)
            spawn.assert_not_called()
            await backend.close()
            spawn.assert_not_called()

    async def test_does_not_spawn_a_local_process_for_a_remote_endpoint(self):
        backend = Backend(Config(backend_url="https://model.example.invalid"))
        with patch.object(backend.probe, "_get", side_effect=URLError("offline")), \
                patch("qev.studio.subprocess.Popen") as spawn:
            self.assertEqual((await backend.connect())["state"], "error")
            spawn.assert_not_called()

    async def test_missing_flide_produces_a_recoverable_startup_error(self):
        backend = Backend(Config())
        with patch.object(backend.probe, "_get", side_effect=URLError("offline")), \
                patch("qev.studio.subprocess.run", return_value=SimpleNamespace(returncode=1)), \
                patch("qev.studio.subprocess.Popen") as spawn:
            status = await backend.connect()
            self.assertEqual(status["state"], "error")
            self.assertIn("Flide", status["message"])
            spawn.assert_not_called()

    async def test_loading_server_is_not_duplicated(self):
        from urllib.error import HTTPError

        backend = Backend(Config())
        loading = HTTPError("http://127.0.0.1:8000/health", 503, "loading", {}, None)
        with patch.object(backend.probe, "_get", side_effect=loading), patch("qev.studio.subprocess.Popen") as spawn:
            self.assertEqual((await backend.connect())["state"], "loading")
            spawn.assert_not_called()

    async def test_only_stops_the_inference_process_it_owns(self):
        backend = Backend(Config())
        backend.process = Mock()
        backend.process.poll.return_value = None
        await backend.close()
        backend.process.terminate.assert_called_once()
        backend.process.wait.assert_called_once_with(timeout=10)


class PersistenceTests(unittest.TestCase):
    def test_nonfinite_model_diagnostics_are_preserved_without_corrupting_report(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session("retry", Path(root))
            session.history.append({"answers": {"completed": {"noul": .25}},
                                    "meta": {"mass": float("nan"), "bounds": [float("inf"), float("-inf")]}})
            session.save()
            saved = json.loads((session.path / "report.json").read_text(),
                               parse_constant=lambda value: self.fail("invalid JSON number: " + value))
            self.assertEqual(saved["history"][0]["answers"]["completed"]["noul"], .25)
            self.assertEqual(saved["history"][0]["meta"], {"mass": "NaN", "bounds": ["Infinity", "-Infinity"]})
            self.assertEqual(session.snapshot()["history"][0]["meta"], saved["history"][0]["meta"])

    def test_saved_report_retains_frames_and_model_decisions(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session("retry", Path(root))
            session.history.append({"step": 0, "decision": {"action": "pay"}, "answers": {"completed": 0.02}})
            session.save()
            loaded = json.loads((session.path / "report.json").read_text())
            self.assertEqual(loaded["id"], session.id)
            self.assertEqual(loaded["history"][0]["decision"]["action"], "pay")
            self.assertFalse(loaded["busy"])
            self.assertFalse((session.path / "report.tmp").exists())


class ImageWorkTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_upload_validation_leaves_status_available(self):
        from PIL import Image
        from qev.media import data_uri

        opened, release = threading.Event(), threading.Event()
        uri = data_uri(Image.new("RGB", (8, 8), "red"), fmt="PNG")
        original = Image.open

        def open_upload(*args, **kwargs):
            opened.set()
            release.wait(timeout=2)
            return original(*args, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            app = create_app(Config(output=Path(root), autostart=False))
            backend = app.state.studio.backend
            backend.state = "ready"
            answer = SimpleNamespace(model="test", answers={"completed": SimpleNamespace(raw={"type": "noul", "noul": .9})},
                                     usage={}, meta={}, request_id=None)
            with patch("PIL.Image.open", side_effect=open_upload), \
                    patch.object(backend.client, "system_one", return_value=answer) as infer:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://studio") as client:
                    asking = asyncio.create_task(client.post("/api/ask", json={"mode": "image", "image": uri, "statement": "It is red"}))
                    try:
                        self.assertTrue(await asyncio.to_thread(opened.wait, 1))
                        self.assertFalse(asking.done())
                        status = await asyncio.wait_for(client.get("/api/status"), timeout=.5)
                        self.assertEqual(status.json()["state"], "ready")
                        infer.assert_not_called()
                    finally:
                        release.set()
                        response = await asking
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(infer.call_args.kwargs["media"], [{"type": "image", "data": uri}])


class ReportListingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_report_disk_reads_leave_status_available(self):
        for endpoint, subdirectory, key in (("/api/runs", "", "runs"),
                ("/api/robot/runs", "robot", "runs"), ("/api/robot/playlists", "robot/playlists", "playlists")):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as root:
                reading, release = threading.Event(), threading.Event()
                app = create_app(Config(output=Path(root), autostart=False))
                live = SimpleNamespace(root=Path(root) / "robot", auto_running=False, close=Mock())
                report = Path(root) / subdirectory / ("a" * 32) / "report.json"
                report.parent.mkdir(parents=True)
                report.write_text(json.dumps({"id": "a" * 32, "scenario": {"name": "正常抓放"},
                    "phase_label": "动作结束", "created_at": "2026-11-01T00:00:00Z", "status": "completed", "items": []}))
                original = Path.read_text

                def read(path, *args, **kwargs):
                    if path == report:
                        reading.set()
                        release.wait(timeout=2)
                    return original(path, *args, **kwargs)

                with patch("qev.robot.RobotWorld", return_value=live):
                    try:
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://studio") as client:
                            with patch.object(Path, "read_text", read):
                                listing = asyncio.create_task(client.get(endpoint))
                                try:
                                    self.assertTrue(await asyncio.to_thread(reading.wait, 1))
                                    self.assertFalse(listing.done())
                                    status = await asyncio.wait_for(client.get("/api/status"), timeout=.5)
                                    self.assertEqual(status.status_code, 200)
                                finally:
                                    release.set()
                                    response = await listing
                            self.assertEqual(response.status_code, 200)
                            self.assertEqual(response.json()[key][0]["id"], "a" * 32)
                    finally:
                        await app.state.studio.close()

    async def test_listing_skips_corrupt_reports_and_finds_older_valid_cases(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app(Config(output=Path(root), autostart=False))
            valid = Path(root) / ("a" * 32) / "report.json"
            valid.parent.mkdir()
            valid.write_text(json.dumps({"id": "a" * 32, "scenario": "retry"}))
            for index in range(35):
                path = Path(root) / f"{index:032x}" / "report.json"
                path.parent.mkdir()
                path.write_text("[]" if index % 2 else "invalid JSON")
            vanished = Path(root) / ("f" * 32) / "report.json"
            vanished.parent.mkdir()
            vanished.write_text(json.dumps({"id": "f" * 32}))
            original = Path.stat

            def stat(path, *args, **kwargs):
                if path == vanished:
                    raise FileNotFoundError(errno.ENOENT, "concurrently removed report", str(path))
                return original(path, *args, **kwargs)

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://studio") as client:
                with patch.object(Path, "stat", stat):
                    response = await client.get("/api/runs")
            self.assertEqual(response.status_code, 200)
            self.assertEqual([run["id"] for run in response.json()["runs"]], ["a" * 32])


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_releases_remaining_resources_after_a_close_failure(self):
        for stage in ("robot", "browser", "driver"):
            with self.subTest(stage=stage):
                studio = Studio(Config())
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                session = Session("retry", Path(temporary.name))
                session.auto_running = True
                session.auto_task = asyncio.create_task(session.pause_event.wait())
                studio.sessions[session.id] = session
                error = RuntimeError("close failed")
                studio.close_robot = AsyncMock(side_effect=error if stage == "robot" else None)
                studio.browser = Mock(close=AsyncMock(side_effect=error if stage == "browser" else None))
                studio.playwright = Mock(stop=AsyncMock(side_effect=error if stage == "driver" else None))
                studio.backend.close = AsyncMock()
                with self.assertRaisesRegex(RuntimeError, "close failed"):
                    await studio.close()
                studio.close_robot.assert_awaited_once()
                studio.browser.close.assert_awaited_once()
                studio.playwright.stop.assert_awaited_once()
                studio.backend.close.assert_awaited_once()
                self.assertFalse(session.auto_running)
                self.assertTrue(session.auto_task.done())

    async def test_simultaneous_sample_requests_share_one_page_and_keep_immutable_pixels(self):
        studio = Studio(Config())
        page = Mock(goto=AsyncMock(), evaluate=AsyncMock(), screenshot=AsyncMock(return_value=b"png"), close=AsyncMock())
        browser = Mock(new_page=AsyncMock(return_value=page))
        with patch.object(studio, "get_browser", AsyncMock(return_value=browser)) as get_browser:
            images = await asyncio.gather(*(studio.sample_image() for _ in range(12)))
            self.assertEqual(images, [b"png"] * 12)
            self.assertEqual(await studio.sample_image(), b"png")
        browser.new_page.assert_awaited_once()
        page.close.assert_awaited_once()
        get_browser.assert_awaited_once()

    async def test_failed_or_cancelled_sample_render_closes_page_and_can_be_retried(self):
        for error in (RuntimeError("render failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                studio = Studio(Config())
                page = Mock(goto=AsyncMock(), evaluate=AsyncMock(),
                            screenshot=AsyncMock(side_effect=[error, b"png"]), close=AsyncMock())
                browser = Mock(new_page=AsyncMock(return_value=page))
                with patch.object(studio, "get_browser", AsyncMock(return_value=browser)):
                    with self.assertRaises(type(error)):
                        await studio.sample_image()
                    self.assertEqual(await studio.sample_image(), b"png")
                self.assertEqual(page.close.await_count, 2)
                self.assertEqual(browser.new_page.await_count, 2)

    async def test_concurrent_creation_bounds_pages_and_keeps_evicted_reports(self):
        pages = []

        async def new_page(**kwargs):
            await asyncio.sleep(0)
            page = Mock(goto=AsyncMock(), screenshot=AsyncMock(), close=AsyncMock())
            pages.append(page)
            return page

        with tempfile.TemporaryDirectory() as root:
            studio = Studio(Config(output=Path(root)))
            with patch.object(studio, "get_browser", AsyncMock(return_value=Mock(new_page=new_page))):
                sessions = await asyncio.gather(*(studio.create_session("retry") for _ in range(12)))
            self.assertEqual(len(studio.sessions), 8)
            self.assertEqual(sum(not page.close.await_count for page in pages), 8)
            for session in sessions:
                self.assertTrue((session.path / "report.json").is_file())

    async def test_active_cases_are_not_evicted_when_capacity_is_reached(self):
        with tempfile.TemporaryDirectory() as root:
            studio = Studio(Config(output=Path(root)))
            for index in range(8):
                session = Session("retry", Path(root))
                session.page = Mock(close=AsyncMock())
                session.auto_running = index % 2 == 0
                if not session.auto_running:
                    await session.lock.acquire()
                studio.sessions[session.id] = session
            with self.assertRaises(HTTPException) as error:
                await studio.create_session("success")
            self.assertEqual(error.exception.status_code, 409)
            for session in studio.sessions.values():
                session.page.close.assert_not_awaited()

    async def test_failed_or_cancelled_creation_releases_page_and_partial_files(self):
        for stage in ("new_page", "goto", "screenshot", "save", "cancel"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as root:
                studio = Studio(Config(output=Path(root)))
                page = Mock(goto=AsyncMock(), screenshot=AsyncMock(), close=AsyncMock())
                browser = Mock(new_page=AsyncMock(return_value=page))
                error = asyncio.CancelledError() if stage == "cancel" else RuntimeError("creation failed")
                if stage == "new_page":
                    browser.new_page.side_effect = error
                elif stage != "save":
                    getattr(page, "goto" if stage == "cancel" else stage).side_effect = error
                with patch.object(studio, "get_browser", AsyncMock(return_value=browser)), \
                        patch.object(Session, "save", side_effect=error if stage == "save" else None):
                    with self.assertRaises(type(error)):
                        await studio.create_session("retry")
                self.assertEqual(studio.sessions, {})
                self.assertEqual(list(Path(root).iterdir()), [])
                if stage != "new_page":
                    page.close.assert_awaited_once()


@unittest.skipUnless(os.environ.get("QEV_BROWSER_TESTS") == "1", "set QEV_BROWSER_TESTS=1 to check browser recovery")
class BrowserRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sample_pixels_are_reused_after_browser_closes_without_leaking_contexts(self):
        import io
        from PIL import Image

        with tempfile.TemporaryDirectory() as root:
            studio = Studio(Config(output=Path(root), channel=os.environ.get("QEV_BROWSER_CHANNEL")))
            try:
                images = await asyncio.gather(*(studio.sample_image() for _ in range(12)))
                self.assertTrue(all(image == images[0] for image in images))
                with Image.open(io.BytesIO(images[0])) as image:
                    self.assertEqual(image.size, (1280, 720))
                self.assertEqual(studio.browser.contexts, [])
                await studio.browser.close()
                self.assertEqual(await studio.sample_image(), images[0])
                self.assertFalse(studio.browser.is_connected())
            finally:
                await studio.close()

    async def test_concurrent_cases_keep_at_most_eight_real_browser_contexts(self):
        with tempfile.TemporaryDirectory() as root:
            studio = Studio(Config(output=Path(root), channel=os.environ.get("QEV_BROWSER_CHANNEL")))
            try:
                sessions = await asyncio.gather(*(studio.create_session("retry") for _ in range(12)))
                self.assertEqual(len(studio.sessions), 8)
                self.assertEqual(len(studio.browser.contexts), 8)
                for session in sessions:
                    self.assertTrue((session.path / "report.json").is_file())
                for session in studio.sessions.values():
                    self.assertFalse(session.page.is_closed())
                    self.assertTrue((session.path / "preview.png").is_file())
            finally:
                await studio.close()

    async def test_closed_browser_is_recreated_for_the_next_case(self):
        with tempfile.TemporaryDirectory() as root:
            studio = Studio(Config(output=Path(root), channel=os.environ.get("QEV_BROWSER_CHANNEL")))
            try:
                first = await studio.create_session("retry")
                original = studio.browser
                await original.close()
                second = await studio.create_session("success")
                self.assertIsNot(studio.browser, original)
                self.assertNotEqual(first.id, second.id)
                self.assertTrue((second.path / "preview.png").is_file())
            finally:
                await studio.close()


if __name__ == "__main__":
    unittest.main()
