"""A private, managed Viser viewer for unattended fixed-camera observations."""
from __future__ import annotations

import asyncio
import math
import shutil
import time
from uuid import uuid4

from fastapi import HTTPException


class ManagedCapture:
    def __init__(self, world, channel=None):
        self.world, self.channel = world, channel
        self.lock = asyncio.Lock()
        self.playwright = self.browser = self.page = None
        self.client_id = None
        # Viser's public initialCameraFov URL option identifies this private
        # connection. get_render() still uses the task's fixed fov and pose.
        self.marker = .71 + int(uuid4().hex[:8], 16) / 2**32 * .01
        self.failed_at = None
        self.state = {"mode": "managed", "ready": False, "message": "内置相机待机", "launches": 0}
        world.capture = self.state

    def user_viewers(self):
        return {key: client for key, client in self.world.viewers().items()
                if not math.isclose(client.camera.fov, self.marker, rel_tol=0, abs_tol=1e-9)}

    async def close_page(self):
        try:
            if self.page and not self.page.is_closed():
                await asyncio.wait_for(self.page.close(), timeout=5)
        except Exception:
            # A crashed connection can reject close; it must not block rebuild.
            pass
        finally:
            self.page = None

    async def stop_driver(self):
        try:
            if self.playwright:
                await asyncio.wait_for(self.playwright.stop(), timeout=5)
        except Exception:
            pass
        finally:
            self.playwright = None

    async def get_viewer(self):
        async with self.lock:
            clients = self.world.viewers()
            if self.browser and self.browser.is_connected() and self.page and not self.page.is_closed() and self.client_id in clients:
                return clients[self.client_id]
            external = self.user_viewers()
            if self.failed_at and time.monotonic() - self.failed_at < 30 and external:
                self.state.update(mode="browser", ready=True, message="使用页面相机，请保持三维页面打开")
                return next(iter(external.values()))
            self.world.set_activity("capture_start", "正在准备内置相机")
            self.state.update(mode="managed", ready=False, message="正在准备内置相机")
            try:
                from playwright.async_api import async_playwright
                await self.close_page()
                if not self.browser or not self.browser.is_connected():
                    await self.stop_driver()
                    self.playwright = await async_playwright().start()
                    channel = self.channel
                    if channel is None and (shutil.which("google-chrome") or shutil.which("google-chrome-stable")):
                        channel = "chrome"
                    self.browser = await self.playwright.chromium.launch(headless=True, channel=channel,
                        args=["--enable-unsafe-swiftshader"], timeout=15000)
                    self.state["launches"] += 1
                self.page = await self.browser.new_page(viewport={"width": 960, "height": 720}, device_scale_factor=1)
                await self.page.goto(f"http://127.0.0.1:{self.world.server.get_port()}/?initialCameraFov={self.marker}", timeout=15000)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    for identifier, client in self.world.viewers().items():
                        if math.isclose(client.camera.fov, self.marker, rel_tol=0, abs_tol=1e-9):
                            self.client_id = self.world.capture_client_id = identifier
                            self.failed_at = None
                            self.state.update(mode="managed", ready=True, message="内置相机已就绪，可关闭交互页面")
                            return client
                    await asyncio.sleep(.05)
                raise TimeoutError("内置相机连接超时")
            except Exception as exc:
                self.failed_at = time.monotonic()
                await self.close_page()
                self.client_id = self.world.capture_client_id = None
                external = self.user_viewers()
                if external:
                    self.state.update(mode="browser", ready=True, message="使用页面相机，请保持三维页面打开")
                    return next(iter(external.values()))
                self.state.update(mode="unavailable", ready=False, message="相机未就绪，请连接三维页面或安装浏览器支持")
                raise HTTPException(503, self.state["message"]) from exc

    async def close(self):
        try:
            if self.browser:
                await asyncio.wait_for(self.browser.close(), timeout=5)
        except Exception:
            pass
        finally:
            await self.stop_driver()
        self.browser = self.playwright = self.page = None
        self.client_id = self.world.capture_client_id = None
        self.state.update(ready=False)

    async def invalidate(self):
        """A failed render must not keep selecting the same stalled client."""
        self.client_id = self.world.capture_client_id = None
        self.state.update(ready=False, message="正在重新连接内置相机")
        await self.close_page()
