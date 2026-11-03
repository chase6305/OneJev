"""Model service ownership and persistent Studio artifacts, without a GPU."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError

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


@unittest.skipUnless(os.environ.get("QEV_BROWSER_TESTS") == "1", "set QEV_BROWSER_TESTS=1 to check browser recovery")
class BrowserRecoveryTests(unittest.IsolatedAsyncioTestCase):
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
