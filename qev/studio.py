"""A persistent local UI for OneJev demos: python -m qev.studio."""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.client import HTTPException as HTTPProtocolError
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .browser import GOAL, SUCCESS, STATUSES, Decision, Prediction, decide
from .client import Choice, Client, Noul, Score
from .reporting import json_safe, recent_reports

ASSETS = Path(__file__).with_name("studio_assets")
log = logging.getLogger("qev.studio")


@dataclass
class Config:
    backend_url: str = "http://127.0.0.1:8000"
    model: str = "OmniJev/OneJev1.6-0.8B"
    inference_python: str = sys.executable
    channel: str | None = None
    output: Path = Path("runs/studio")
    autostart: bool = True


class Backend:
    """Reuse a running server, or own one inference subprocess for this Studio."""
    def __init__(self, config: Config):
        self.config = config
        self.client = Client(config.backend_url, timeout=30)
        self.probe = Client(config.backend_url, timeout=2)
        self.process: subprocess.Popen | None = None
        self.reachable = False
        self.state = "connecting"
        self.message = "正在连接模型…"
        self.model = config.model.split("/")[-1]
        self.lock = asyncio.Lock()
        self.connect_task: asyncio.Task | None = None
        self.closing = False

    def snapshot(self) -> dict:
        return {"state": self.state, "message": self.message, "model": self.model}

    def reconnect(self) -> None:
        if not self.closing and (self.connect_task is None or self.connect_task.done()):
            self.connect_task = asyncio.create_task(self.connect())
            self.connect_task.add_done_callback(self.connection_finished)

    def connection_finished(self, task: asyncio.Task) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            self.state, self.message = "error", "模型连接失败，请检查连接地址或网站运行日志"
            log.error("Backend connection failed", exc_info=(type(error), error, error.__traceback__))

    async def refresh(self) -> dict:
        self.reachable = False
        try:
            health = await asyncio.to_thread(self.probe._get, "/health")
            self.reachable = True
            if not isinstance(health, dict) or health.get("status") not in ("ok", "loading"):
                self.state, self.message = "error", "模型服务返回的健康状态无效，请检查连接地址或服务日志"
                return self.snapshot()
            if health.get("status") == "ok":
                self.state, self.message = "ready", "模型已就绪"
                if isinstance(health.get("model"), str) and health["model"]:
                    self.model = health["model"]
            else:
                self.state, self.message = "loading", "模型正在加载，页面会自动更新"
        except HTTPError as exc:
            self.reachable = True
            if exc.code == 503:
                self.state, self.message = "loading", "模型正在加载，页面会自动更新"
            else:
                self.state, self.message = "error", f"模型服务返回 HTTP {exc.code}"
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.reachable = True
            self.state, self.message = "error", "模型服务返回的健康状态无效，请检查连接地址或服务日志"
        except (URLError, OSError, ValueError) as exc:
            self.reachable = isinstance(exc, URLError) and isinstance(exc.reason, HTTPProtocolError)
            if self.reachable:
                self.state, self.message = "error", "模型健康检查响应不完整，正在重试"
            elif self.process is not None and self.process.poll() is None:
                self.state, self.message = "loading", "模型正在加载，页面会自动更新"
            elif self.process is not None:
                self.state, self.message = "error", "模型启动失败，请查看网站运行日志后重新连接"
            elif self.state != "error":
                self.state, self.message = "offline", "模型尚未连接"
        return self.snapshot()

    async def connect(self) -> dict:
        async with self.lock:
            if self.closing:
                return self.snapshot()
            await self.refresh()
            if self.closing or self.reachable or self.state in ("ready", "loading") or not self.config.autostart:
                return self.snapshot()
            try:
                endpoint = urlparse(self.config.backend_url)
                port = endpoint.port if endpoint.port is not None else 80
                if port == 0:
                    raise ValueError("invalid port")
            except ValueError:
                self.state, self.message = "error", "模型服务地址无效，请检查主机和端口"
                return self.snapshot()
            if endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost", "::1"):
                self.state, self.message = "error", "请确认配置的远端模型服务正在运行"
                return self.snapshot()
            self.state, self.message = "loading", "正在启动模型服务…"
            try:
                probe = await asyncio.to_thread(
                    subprocess.run,
                    [self.config.inference_python, "-c", "import flide"],
                    capture_output=True, timeout=15, check=False,
                )
                if self.closing:
                    return self.snapshot()
                if probe.returncode:
                    raise RuntimeError("推理环境尚未安装 Flide，请按使用说明配置推理环境")
                self.config.output.mkdir(parents=True, exist_ok=True)
                with (self.config.output / "backend.log").open("ab") as stream:
                    self.process = subprocess.Popen(
                        [self.config.inference_python, "-m", "flide.cli", "serve", "--model", self.config.model,
                         "--host", endpoint.hostname, "--port", str(port)],
                        stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                        start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED="1"),
                    )
                log.info("Started inference process %s", self.process.pid)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                self.state, self.message = "error", str(exc)
            return self.snapshot()

    async def monitor(self) -> None:
        while True:
            await asyncio.sleep(2)
            await self.refresh()

    async def close(self) -> None:
        self.closing = True
        if self.connect_task is not None:
            await asyncio.gather(self.connect_task, return_exceptions=True)
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.to_thread(self.process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, timeout=5)


class Session:
    def __init__(self, scenario: str, root: Path):
        self.id = uuid4().hex
        self.scenario = scenario
        self.path = root / self.id
        self.path.mkdir(parents=True)
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.page = None
        self.history: list[dict] = []
        self.attempts = 0
        self.result: str | None = None
        self.error: str | None = None
        self.retryable = False
        self.pending_action: str | None = None
        self.observations = 0
        self.frame = "preview.png"
        self.auto_running = False
        self.delay = 3.0
        self.next_step_at: float | None = None
        self.auto_task: asyncio.Task | None = None
        self.pause_event = asyncio.Event()
        self.lock = asyncio.Lock()

    def snapshot(self) -> dict:
        return json_safe({
            "id": self.id, "scenario": self.scenario, "created_at": self.created_at,
            "attempts": self.attempts, "result": self.result, "error": self.error,
            "busy": self.lock.locked(), "auto_running": self.auto_running, "delay": self.delay,
            "retryable": self.retryable, "next_step_at": self.next_step_at,
            "frame": f"/api/runs/{self.id}/images/{self.frame}", "history": self.history,
        })

    def save(self) -> None:
        data = self.snapshot()
        data["busy"] = False
        temporary = self.path / "report.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(self.path / "report.json")


class Studio:
    def __init__(self, config: Config):
        self.config = config
        self.backend = Backend(config)
        self.sessions: dict[str, Session] = {}
        self.playwright = None
        self.browser = None
        self.browser_lock = asyncio.Lock()
        self.session_lock = asyncio.Lock()
        self.sample_lock = asyncio.Lock()
        self.sample_png: bytes | None = None
        self.close_robot = None

    async def get_browser(self):
        async with self.browser_lock:
            if self.browser is not None and not self.browser.is_connected():
                self.browser = None
            if self.browser is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError as exc:
                    raise HTTPException(503, "请先安装浏览器支持：pip install -e '.[browser]'") from exc
                if self.playwright is None:
                    self.playwright = await async_playwright().start()
                channel = self.config.channel
                if channel is None and (shutil.which("google-chrome") or shutil.which("google-chrome-stable")):
                    channel = "chrome"
                try:
                    self.browser = await self.playwright.chromium.launch(headless=True, channel=channel)
                except Exception:
                    await self.playwright.stop()
                    self.playwright = None
                    raise
            return self.browser

    async def sample_image(self) -> bytes:
        """Render the fixed example once, including when requests arrive together."""
        async with self.sample_lock:
            if self.sample_png is None:
                browser = await self.get_browser()
                page = await browser.new_page(viewport={"width": 1280, "height": 720})
                try:
                    await page.goto((ASSETS / "invoice.html").as_uri())
                    await page.evaluate("render('paid')")
                    png = await page.screenshot(animations="disabled")
                finally:
                    await page.close()
                # A failed or cancelled render must leave the next request free
                # to retry, rather than caching an error or a partial result.
                self.sample_png = png
            return self.sample_png

    async def create_session(self, scenario: str) -> Session:
        # Admission and browser allocation must share a lock: concurrent requests
        # otherwise all see free capacity before their first page is registered.
        async with self.session_lock:
            if len(self.sessions) >= 8:
                for key, old in list(self.sessions.items()):
                    if not old.auto_running and not old.lock.locked():
                        async with old.lock:
                            del self.sessions[key]
                            # Disconnected browsers may reject close; saved reports
                            # remain available even after the live page is evicted.
                            with suppress(Exception):
                                await old.page.close()
                        break
                else:
                    raise HTTPException(409, "当前运行中的案例过多，请先暂停一个案例")
            browser = await self.get_browser()
            session = Session(scenario, self.config.output)
            try:
                session.page = await browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
                session.page.set_default_timeout(5000)
                await session.page.goto(ASSETS.joinpath("invoice.html").as_uri() + f"?scenario={scenario}")
                await session.page.screenshot(path=str(session.path / session.frame), animations="disabled")
                session.save()
            except BaseException:
                # Cancellation must release resources too, including failures that
                # happen before navigation or while writing the initial report.
                if session.page is not None:
                    with suppress(Exception):
                        await session.page.close()
                shutil.rmtree(session.path, ignore_errors=True)
                raise
            self.sessions[session.id] = session
            return session

    def session(self, identifier: str) -> Session:
        if identifier not in self.sessions:
            raise HTTPException(404, "这个案例已结束，请新建一个案例")
        return self.sessions[identifier]

    async def step(self, session: Session) -> dict:
        if session.lock.locked():
            raise HTTPException(409, "当前步骤正在执行，请稍候")
        if session.result is not None:
            return session.snapshot()
        if self.backend.state != "ready":
            raise HTTPException(503, "模型尚未就绪，请等待右上角状态变为就绪")
        async with session.lock:
            observation = None
            session.error, session.retryable = None, False
            try:
                if session.pending_action is not None:
                    action = session.pending_action
                    # Consume before awaiting: retrying inference must never repeat an action.
                    session.pending_action = None
                    if action in ("pay", "retry"):
                        session.attempts += 1
                        await session.page.locator("#pay").click()
                    if action in ("pay", "retry", "wait"):
                        from playwright.async_api import TimeoutError as BrowserTimeout

                        try:
                            await session.page.wait_for_function(
                                "document.body.dataset.state !== 'processing'", timeout=1000)
                        except BrowserTimeout:
                            pass
                step = len(session.history)
                name = f"{step:02d}.png"
                png = await session.page.screenshot(path=str(session.path / name), animations="disabled", caret="hide")
                captured = await session.page.locator("body").get_attribute("data-state")
                session.frame = name
                observation = {
                    "step": step, "attempts": session.attempts, "screenshot": name,
                    "image": f"/api/runs/{session.id}/images/{name}", "page_status": captured,
                    "answers": {},
                }
                started = time.perf_counter()
                response = await asyncio.to_thread(
                    self.backend.client.system_one,
                    {"task": GOAL, "screen": "<image:1>"},
                    {"completed": Noul(SUCCESS), "status": Choice("What is the current payment state shown on the screen?", STATUSES),
                     "progress": Score("How far has payment progressed?", ["No payment attempt is shown.",
                                       "Payment was attempted but is incomplete.", "Payment is complete."])},
                    media=[{"type": "image", "data": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}],
                )
                current = await session.page.locator("body").get_attribute("data-state")
                observation.update({
                    "page_status": current,
                    "model": response.model, "answers": {k: v.raw for k, v in response.answers.items()},
                    "usage": response.usage, "meta": response.meta, "request_id": response.request_id,
                    "request_s": time.perf_counter() - started,
                })
                prediction = Prediction.from_answers(observation["answers"])
                session.observations += 1
                decision = (decide(prediction, current, session.attempts, 2, 0.85, 0.6) if current == captured
                            else Decision("wait", "The page changed during inference; take a fresh screenshot."))
                if decision.action in ("pay", "retry", "wait") and session.observations >= 6:
                    decision = Decision("exhausted", "The observation limit has been reached.")
                observation["decision"] = asdict(decision)
                session.history.append(observation)
                if decision.action in ("pay", "retry", "wait"):
                    session.pending_action = decision.action
                else:
                    session.result = decision.action
                    session.auto_running = False
            except Exception as exc:
                log.exception("Session step failed")
                session.retryable = (isinstance(exc, (URLError, OSError, TimeoutError))
                                     and (not isinstance(exc, HTTPError) or exc.code >= 500 or exc.code == 429))
                session.error = ("模型请求暂时失败。连接恢复后点击“重新判断”，会保留当前页面，不重复执行上一步。"
                                 if session.retryable else str(exc))
                session.result = None if session.retryable else "error"
                session.auto_running = False
                session.next_step_at = None
                if observation is not None:
                    observation["decision"] = {"action": "reconnect" if session.retryable else "error", "reason": session.error}
                    session.history.append(observation)
                if session.retryable:
                    await self.backend.refresh()
            session.save()
        return session.snapshot()

    async def automatic(self, session: Session) -> None:
        try:
            while session.auto_running and session.result is None:
                await self.step(session)
                if not session.auto_running or session.result is not None:
                    break
                shown_at = time.monotonic()
                while session.auto_running:
                    remaining = session.delay - (time.monotonic() - shown_at)
                    if remaining <= 0:
                        break
                    session.next_step_at = time.time() + remaining
                    session.pause_event.clear()
                    try:
                        await asyncio.wait_for(session.pause_event.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                session.next_step_at = None
        except Exception as exc:
            session.error, session.result = str(exc), "error"
        finally:
            session.auto_running = False
            session.next_step_at = None
            session.save()

    async def close_sessions(self) -> None:
        tasks = []
        for session in self.sessions.values():
            session.auto_running = False
            session.pause_event.set()
            if session.auto_task is not None:
                tasks.append(session.auto_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        async with AsyncExitStack() as resources:
            resources.push_async_callback(self.backend.close)
            if self.playwright is not None:
                resources.push_async_callback(self.playwright.stop)
            if self.browser is not None:
                resources.push_async_callback(self.browser.close)
            resources.push_async_callback(self.close_sessions)
            if self.close_robot is not None:
                await self.close_robot()


class NewSession(BaseModel):
    scenario: Literal["retry", "success", "blocked", "exhausted", "pending"] = "retry"


class AutoOptions(BaseModel):
    delay: float = Field(default=3, ge=0.5, le=20, allow_inf_nan=False)


class AskOptions(BaseModel):
    mode: Literal["text", "image"] = "text"
    preset: Literal["support", "completion"] = "support"
    text: str = Field(default="", max_length=16000)
    statement: str = Field(default="用户的问题已经解决。", max_length=2000)
    image: str | None = Field(default=None, max_length=14_000_000)


def validate_upload(image: str) -> None:
    from PIL import Image

    raw = base64.b64decode(image.split(",", 1)[1], validate=True)
    with Image.open(io.BytesIO(raw)) as uploaded:
        if uploaded.width * uploaded.height > 16_000_000:
            raise ValueError("图片超过 1600 万像素，请缩小后上传")
        uploaded.verify()


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config()
    config.output = config.output.expanduser().resolve()
    config.output.mkdir(parents=True, exist_ok=True)
    studio = Studio(config)

    @asynccontextmanager
    async def lifespan(app):
        studio.backend.reconnect()
        monitor = asyncio.create_task(studio.backend.monitor())
        try:
            yield
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            await studio.close()

    app = FastAPI(title="OneJev Studio", lifespan=lifespan)
    app.state.studio = studio
    from .robot import install_routes

    install_routes(app, studio)

    @app.get("/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in ("app.js", "robot.js", "robot-view.js", "robot-view.css", "style.css"):
            raise HTTPException(404)
        return FileResponse(ASSETS / name)

    @app.get("/api/status")
    async def status():
        return studio.backend.snapshot()

    @app.post("/api/connect")
    async def connect():
        studio.backend.reconnect()
        return studio.backend.snapshot()

    @app.post("/api/sessions")
    async def create(options: NewSession):
        return (await studio.create_session(options.scenario)).snapshot()

    @app.get("/api/sessions/{identifier}")
    async def get_session(identifier: str):
        return studio.session(identifier).snapshot()

    @app.post("/api/sessions/{identifier}/step")
    async def step(identifier: str):
        session = studio.session(identifier)
        if session.auto_running:
            raise HTTPException(409, "请先暂停自动演示")
        return await studio.step(session)

    @app.post("/api/sessions/{identifier}/auto")
    async def automatic(identifier: str, options: AutoOptions):
        session = studio.session(identifier)
        if session.result is not None:
            raise HTTPException(409, "案例已经结束，可以新建案例重看")
        if studio.backend.state != "ready":
            raise HTTPException(503, "模型尚未就绪，请稍候")
        if not session.auto_running and session.auto_task is not None and not session.auto_task.done():
            # Let a paused in-flight step finish before starting a new automatic loop.
            await session.auto_task
            if session.result is not None:
                return session.snapshot()
        session.delay = options.delay
        session.auto_running = True
        if session.auto_task is None or session.auto_task.done():
            session.pause_event.clear()
            session.auto_task = asyncio.create_task(studio.automatic(session))
        else:
            session.pause_event.set()
        return session.snapshot()

    @app.post("/api/sessions/{identifier}/pause")
    async def pause(identifier: str):
        session = studio.session(identifier)
        session.auto_running = False
        session.next_step_at = None
        session.pause_event.set()
        return session.snapshot()

    def run_path(identifier: str) -> Path:
        try:
            if UUID(identifier).hex != identifier:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(404) from exc
        path = config.output / identifier
        if not path.is_dir():
            raise HTTPException(404)
        return path

    def run_summaries(live_ids):
        items = []
        for data in recent_reports(config.output):
            item = {k: data.get(k) for k in ("id", "scenario", "created_at", "result", "attempts")}
            item["live"] = data["id"] in live_ids
            items.append(item)
            if len(items) >= 30:
                break
        return json_safe({"runs": items})

    @app.get("/api/runs")
    async def runs():
        return await asyncio.to_thread(run_summaries, set(studio.sessions))

    @app.get("/api/runs/{identifier}")
    async def report(identifier: str):
        path = run_path(identifier) / "report.json"
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="application/json")

    @app.get("/api/runs/{identifier}/images/{name}")
    async def image(identifier: str, name: str):
        if name != "preview.png" and (not name.endswith(".png") or not 2 <= len(name[:-4]) <= 6
                                      or not name[:-4].isdigit()):
            raise HTTPException(404)
        path = run_path(identifier) / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})

    @app.get("/api/sample.png")
    async def sample():
        from fastapi.responses import Response

        return Response(await studio.sample_image(), media_type="image/png")

    @app.post("/api/ask")
    async def ask(options: AskOptions):
        if studio.backend.state != "ready":
            raise HTTPException(503, "模型尚未就绪，请等待或重新连接")
        media = None
        if options.mode == "image":
            if not options.image or not options.image.startswith("data:image/"):
                raise HTTPException(400, "请先上传一张图片")
            if not options.statement.strip():
                raise HTTPException(400, "请填写要判断的命题")
            try:
                await asyncio.to_thread(validate_upload, options.image)
            except Exception as exc:
                raise HTTPException(400, f"无法读取图片：{exc}") from exc
            state = {"context": options.text, "image": "<image:1>"}
            media = [{"type": "image", "data": options.image}]
            questions = {"completed": Noul(options.statement)}
        elif options.preset == "completion":
            if not options.text.strip() or not options.statement.strip():
                raise HTTPException(400, "请填写文本和要判断的命题")
            state, questions = options.text, {"completed": Noul(options.statement)}
        else:
            if not options.text.strip():
                raise HTTPException(400, "请先输入一段文本")
            state = options.text
            questions = {
                "department": Choice("哪个团队适合处理这个问题？", {"billing": "付款、订阅或退款问题", "technical": "软件故障或集成问题", "sales": "售前咨询"}),
                "urgent": Noul("用户的问题需要尽快处理。"),
                "frustration": Score("用户表现得多么着急或不满？", ["平静", "有些不满", "非常着急或愤怒"]),
            }
        started = time.perf_counter()
        try:
            response = await asyncio.to_thread(studio.backend.client.system_one, state, questions, media=media)
            answers = {k: v.raw for k, v in response.answers.items()}
            # Invalid numeric answers are an upstream error. Diagnostics remain
            # inspectable even when they include non-finite values.
            json.dumps(answers, allow_nan=False)
        except (URLError, OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(502, "模型请求失败，请检查右上角的连接状态后重试") from exc
        return json_safe({"model": response.model, "answers": answers,
                "usage": response.usage, "meta": response.meta, "request_id": response.request_id,
                "request_s": time.perf_counter() - started})

    @app.exception_handler(Exception)
    async def failure(request, exc):
        log.error("Studio request failed: %s", exc, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "操作失败，请确认浏览器支持已安装，或查看网站运行日志"})

    return app


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--backend-url", default=None)
    parser.add_argument("--model", default="OmniJev/OneJev1.6-0.8B")
    parser.add_argument("--inference-python", default=None, help="Python environment with Flide installed")
    parser.add_argument("--channel", default=None, help="Browser channel, auto-detects installed Chrome")
    parser.add_argument("--output", type=Path, default=Path("runs/studio"))
    parser.add_argument("--no-autostart", action="store_true", help="Only connect to an existing inference server")


def run(args: argparse.Namespace) -> None:
    import uvicorn

    runtime = args.output.expanduser().resolve() / "runtime.json"
    saved = json.loads(runtime.read_text()) if runtime.is_file() else {}
    config = Config(
        backend_url=args.backend_url or saved.get("backend_url", "http://127.0.0.1:8000"), model=args.model,
        inference_python=args.inference_python or os.environ.get("QEV_INFERENCE_PYTHON") or saved.get("inference_python") or sys.executable,
        channel=args.channel, output=args.output, autostart=not args.no_autostart,
    )
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(json.dumps({"backend_url": config.backend_url, "inference_python": config.inference_python}, indent=2))
    logging.basicConfig(level=logging.INFO)
    print(f"OneJev Studio: http://{args.host}:{args.port}", flush=True)
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="info")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
