"""Model service ownership and persistent Studio artifacts, without a GPU."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import URLError

from fastapi import HTTPException

from qev.studio import Backend, Config, Session, Studio


class BackendTests(unittest.IsolatedAsyncioTestCase):
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


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
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
