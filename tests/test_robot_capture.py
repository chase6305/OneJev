"""Real managed Viser viewer lifecycle checks; no GPU/model service required."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from qev.robot import RobotWorld, RobotController, RobotDemo
from qev.robot_capture import ManagedCapture

AVAILABLE = os.environ.get("QEV_BROWSER_TESTS") == "1" and all(
    importlib.util.find_spec(name) for name in ("viser", "scipy", "playwright"))


@unittest.skipUnless(AVAILABLE, "enable browser tests and install robot dependencies")
class ManagedCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.world = RobotWorld(Path(self.temp.name))
        self.capture = ManagedCapture(self.world, os.environ.get("QEV_BROWSER_CHANNEL"))

    async def asyncTearDown(self):
        await self.capture.close()
        self.world.close()
        self.temp.cleanup()

    async def test_private_renderer_recovers_its_page_and_browser_and_keeps_real_pixels(self):
        from PIL import Image, ImageStat

        client = await self.capture.get_viewer()
        first = client.client_id
        self.assertEqual(self.world.snapshot()["clients"], 0)
        self.assertTrue(self.world.capture["ready"])
        requests = []
        def infer(state, questions, **options):
            requests.append((state, options))
            return SimpleNamespace(model="test", answers={key: SimpleNamespace(raw={"type": "noul", "noul": .01})
                for key in ("placed", "grasped")}, meta={}, usage={})
        record = await self.world.observe(SimpleNamespace(system_one=infer), viewer=client)
        self.assertEqual(set(requests[0][0]), {"task", "camera"})
        with Image.open(self.world.path / record["image"].split("/")[-1]) as rendered:
            self.assertEqual(rendered.size, (960, 720))
            self.assertGreater(max(ImageStat.Stat(rendered).stddev), 5)
        self.assertGreater(record["capture_s"], 0)
        await self.capture.page.close()
        client = await self.capture.get_viewer()
        self.assertNotEqual(client.client_id, first)
        await self.capture.browser.close()
        recovered = await self.capture.get_viewer()
        self.assertNotEqual(recovered.client_id, client.client_id)
        self.assertEqual(self.world.snapshot()["clients"], 0)

    async def test_missing_browser_falls_back_to_an_existing_interactive_view(self):
        self.capture.channel = "unavailable-browser-channel"
        external = SimpleNamespace(camera=SimpleNamespace(fov=.7))
        self.world.viewers = lambda: {42: external}
        self.assertIs(await self.capture.get_viewer(), external)
        self.assertEqual(self.world.capture["mode"], "browser")
        self.assertTrue(self.world.capture["ready"])

    async def test_automatic_checks_recover_after_renderer_process_disconnect(self):
        import asyncio
        calls, clients = [], []
        loop = asyncio.get_running_loop()
        def infer(state, questions, **options):
            calls.append(state)
            clients.append(self.capture.client_id)
            if len(calls) == 2:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self.capture.browser.close()))
            return SimpleNamespace(model="test", answers={key: SimpleNamespace(raw={"type": "noul", "noul": .25})
                for key in ("placed", "grasped")}, meta={}, usage={})
        controller = RobotController(self.world, SimpleNamespace(state="ready", client=SimpleNamespace(system_one=infer)), self.capture)
        await controller.demo(RobotDemo(judge=True, motion_speed=2, delay=.5))
        await asyncio.wait_for(controller.auto_task, timeout=30)
        self.assertEqual(self.world.phase, "complete", self.world.error)
        self.assertEqual(self.world.revision, 5)
        self.assertEqual(len(calls), 5)
        self.assertGreaterEqual(len(set(clients)), 2)
        self.assertEqual(self.world.snapshot()["clients"], 0)

    async def test_disconnect_during_a_render_retries_the_same_pose_with_a_new_renderer(self):
        import asyncio

        first = await self.capture.get_viewer()
        render = first.get_render
        loop = asyncio.get_running_loop()

        def interrupted(**options):
            asyncio.run_coroutine_threadsafe(self.capture.browser.close(), loop).result(timeout=5)
            return render(**options)

        first.get_render = interrupted
        infer = Mock(return_value=SimpleNamespace(
            model="test", answers={key: SimpleNamespace(raw={"type": "noul", "noul": .01})
                                    for key in ("placed", "grasped")}, meta={}, usage={}))
        controller = RobotController(self.world, SimpleNamespace(state="ready", client=SimpleNamespace(system_one=infer)), self.capture)
        self.world.auto_running = True
        try:
            await asyncio.wait_for(controller.check_observation(automatic=True), timeout=30)
        finally:
            self.world.auto_running = False
        self.assertEqual(self.world.revision, 0)
        self.assertEqual(len(self.world.history), 1, self.world.error)
        self.assertEqual(len(self.world.observation_errors), 1)
        self.assertFalse(self.world.pending_observation)
        self.assertNotEqual(self.capture.client_id, first.client_id)
        self.assertEqual(self.capture.state["launches"], 2)
        infer.assert_called_once()

    async def test_first_private_frame_has_the_same_lighting_as_a_settled_frame(self):
        import asyncio
        from PIL import Image, ImageChops, ImageStat

        viewer = await self.capture.get_viewer()
        client = SimpleNamespace(system_one=Mock(return_value=SimpleNamespace(
            model="test", answers={key: SimpleNamespace(raw={"type": "noul", "noul": .01})
                                    for key in ("placed", "grasped")}, meta={}, usage={})))
        first = await self.world.observe(client, viewer)
        await asyncio.sleep(1)
        settled = await self.world.observe(client, viewer)
        with Image.open(self.world.path / first["image"].split("/")[-1]) as a, \
                Image.open(self.world.path / settled["image"].split("/")[-1]) as b:
            # Shadow accumulation may change a few pixels. An uninitialized
            # environment instead darkens most of this fixed scene substantially.
            self.assertLess(max(ImageStat.Stat(ImageChops.difference(a, b)).mean), 2)
