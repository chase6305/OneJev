"""Viser robot workbench with scripted kinematics and independent visual checks.

This is a kinematic demonstration, not a dynamics simulator or a hardware driver.
The visual model receives a fixed camera image, never the scene's ground truth.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .browser import probability
from .client import Choice, Noul

PHASES = {"ready": "等待开始", "hover": "已靠近物体", "grasped": "夹爪已闭合", "lifted": "已抬起", "dropped": "物体掉落", "placed": "已放置", "complete": "动作结束"}
SKILLS = {"approach": "靠近方块", "grasp": "闭合夹爪", "lift": "抬起方块", "place": "放入托盘", "recover": "重新抓取", "retreat": "收回机械臂"}


class Kinematics:
    def __init__(self):
        import numpy as np

        self.np = np
        self.axes = np.array([[0, 0, 1], [0, 1, 0], [0, 1, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]])
        self.links = np.array([[0, 0, .22], [.30, 0, 0], [.28, 0, 0], [.06, 0, 0], [0, 0, -.07], [0, 0, -.04]])
        self.base = np.array([-.46, 0., 0.])

    def forward(self, q):
        from scipy.spatial.transform import Rotation

        p, rotation = self.base.copy(), self.np.eye(3)
        points = [p.copy()]
        for axis, link, angle in zip(self.axes, self.links, q):
            rotation = rotation @ Rotation.from_rotvec(axis * angle).as_matrix()
            p = p + rotation @ link
            points.append(p.copy())
        return self.np.array(points), rotation

    def solve(self, target, seed):
        from scipy.optimize import least_squares
        from scipy.spatial.transform import Rotation

        np = self.np
        target = np.asarray(target)

        def residual(q):
            points, rotation = self.forward(q)
            return np.r_[5 * (points[-1] - target), .3 * Rotation.from_matrix(rotation).as_rotvec()]

        solution = least_squares(residual, seed, bounds=(-np.pi, np.pi), max_nfev=200)
        if np.linalg.norm(self.forward(solution.x)[0][-1] - target) > .005:
            raise ValueError("目标超出演示机械臂可达范围")
        return solution.x


class RobotWorld:
    def __init__(self, output: Path):
        import numpy as np
        import viser

        self.np, self.viser = np, viser
        self.root = output / "robot"
        self.root.mkdir(parents=True, exist_ok=True)
        self.kinematics = Kinematics()
        self.server = viser.ViserServer(host="127.0.0.1", port=0, label="OneJev · Robot Lab", verbose=False)
        self.server.scene.set_up_direction("+z")
        self.server.gui.configure_theme(show_logo=False, show_share_button=False, brand_color=(25, 99, 79))
        self.server.gui.main_panel.minimize()
        self.server.initial_camera.position = (.95, -1.15, .90)
        self.server.initial_camera.look_at = (-.12, .02, .18)
        self.server.initial_camera.fov = .70
        self.server.scene.add_grid("/grid", width=2, height=2, cell_size=.1, section_size=.5, position=(0, 0, -.065))
        self.server.scene.add_box("/table", dimensions=(1.5, 1.15, .06), color=(220, 223, 217), position=(-.12, 0, -.035))
        self.server.scene.add_box("/base", dimensions=(.17, .17, .12), color=(52, 64, 67), position=(-.46, 0, .06))
        self.joints = [self.server.scene.add_icosphere(f"/robot/joint{i}", radius=.049 if i < 3 else .033, color=(64, 75, 78)) for i in range(6)]
        self.links = [self.server.scene.add_box(f"/robot/link{i}", dimensions=(.07, .07, float(np.linalg.norm(length))), color=(232, 153, 61) if i in (1, 2) else (157, 167, 168)) for i, length in enumerate(self.kinematics.links)]
        self.tool = self.server.scene.add_frame("/tool", show_axes=False)
        self.server.scene.add_box("/tool/palm", dimensions=(.12, .065, .035), color=(53, 65, 71), position=(0, 0, -.012))
        self.fingers = [self.server.scene.add_box(f"/tool/finger{i}", dimensions=(.018, .038, .095), color=(94, 109, 116)) for i in range(2)]
        self.cube_node = self.server.scene.add_box("/cube", dimensions=(.075, .075, .075), color=(213, 54, 49))
        self.tray_node = self.server.scene.add_frame("/tray", show_axes=False)
        self.server.scene.add_box("/tray/bottom", dimensions=(.27, .23, .018), color=(60, 116, 208), position=(0, 0, .01))
        for i, (dimensions, position) in enumerate([
            ((.27, .018, .055), (0, -.115, .033)), ((.27, .018, .055), (0, .115, .033)),
            ((.018, .23, .055), (-.135, 0, .033)), ((.018, .23, .055), (.135, 0, .033))]):
            self.server.scene.add_box(f"/tray/wall{i}", dimensions=dimensions, position=position, color=(48, 98, 189))
        self.lock = asyncio.Lock()
        self.busy = False
        self.reset()

    def reset(self):
        np = self.np
        self.id = uuid4().hex
        self.path = self.root / self.id
        self.path.mkdir()
        self.phase = "ready"
        self.cube = np.array([.08, -.18, .0375])
        self.tray = np.array([-.02, .23, 0.])
        self.q = self.kinematics.solve([-.15, -.20, .40], np.array([0., -1., 1.7, 0., -.7, 0.]))
        self.attached, self.miss_next = False, False
        self.gap = .13
        self.history: list[dict] = []
        self.revision, self.judged_revision = 0, -1
        self.error = None
        self.draw()
        self.save()

    def truth(self) -> dict:
        in_tray = bool(not self.attached and abs(self.cube[0] - self.tray[0]) < .10
                       and abs(self.cube[1] - self.tray[1]) < .08 and abs(self.cube[2] - .0565) < .018)
        return {"placed": in_tray, "grasped": bool(self.attached and self.cube[2] > .12)}

    def next_skill(self):
        return {"ready": "approach", "hover": "grasp", "grasped": "lift", "dropped": "recover", "placed": "retreat"}.get(
            self.phase, ("place" if self.attached else "recover") if self.phase == "lifted" else None)

    def snapshot(self):
        return {"id": self.id, "phase": self.phase, "phase_label": PHASES[self.phase],
                "next_skill": self.next_skill(), "busy": self.busy, "attached": self.attached,
                "miss_next": self.miss_next, "truth": self.truth(), "revision": self.revision,
                "judged_revision": self.judged_revision, "history": self.history, "error": self.error,
                "clients": len(self.viewers()), "q": self.q.tolist(), "cube": self.cube.tolist(),
                "tray": self.tray.tolist(), "simulation": "kinematic"}

    def viewers(self):
        ready = {}
        for identifier, client in self.server.get_clients().items():
            try:
                if client.camera.image_width > 0 and client.camera.image_height > 0:
                    ready[identifier] = client
            except AssertionError:
                continue
        return ready

    def save(self):
        (self.path / "report.json").write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    def draw(self):
        from scipy.spatial.transform import Rotation

        np = self.np
        points, rotation = self.kinematics.forward(self.q)
        with self.server.atomic():
            for index, (start, end) in enumerate(zip(points[:-1], points[1:])):
                self.joints[index].position = start
                direction = end - start
                z = direction / np.linalg.norm(direction)
                x = np.cross((0., 1., 0.), z)
                if np.linalg.norm(x) < 1e-6:
                    x = np.cross((1., 0., 0.), z)
                x /= np.linalg.norm(x)
                quaternion = Rotation.from_matrix(np.stack([x, np.cross(z, x), z], axis=1)).as_quat()
                self.links[index].position = (start + end) / 2
                self.links[index].wxyz = quaternion[[3, 0, 1, 2]]
            quaternion = Rotation.from_matrix(rotation).as_quat()
            self.tool.position, self.tool.wxyz = points[-1], quaternion[[3, 0, 1, 2]]
            for index, finger in enumerate(self.fingers):
                finger.position = ((-1 if index == 0 else 1) * (self.gap + .018) / 2, 0, -.065)
            if self.attached:
                self.cube = points[-1] + rotation @ np.array([0., 0., -.0875])
            self.cube_node.position = self.cube
            self.tray_node.position = self.tray

    async def move(self, target, duration=1.2):
        goal = self.kinematics.solve(target, self.q)
        start = self.q.copy()
        frames = max(2, round(duration * 30))
        for index in range(1, frames + 1):
            fraction = index / frames
            fraction = fraction * fraction * (3 - 2 * fraction)
            self.q = start + fraction * (goal - start)
            self.draw()
            await asyncio.sleep(duration / frames)

    async def grip(self, closed: bool):
        start, goal = self.gap, .076 if closed else .13
        for i in range(1, 13):
            self.gap = start + (goal - start) * i / 12
            self.draw()
            await asyncio.sleep(.025)

    async def execute(self):
        skill = self.next_skill()
        if skill is None:
            raise HTTPException(409, "动作已结束，可以重置场景再看一次")
        self.revision += 1
        if skill == "approach":
            await self.move([*self.cube[:2], self.cube[2] + .20])
            self.phase = "hover"
        elif skill == "grasp":
            await self.move([*self.cube[:2], self.cube[2] + .0875])
            await self.grip(True)
            self.attached = not self.miss_next
            self.miss_next = False
            self.phase = "grasped"
        elif skill == "lift":
            position = self.kinematics.forward(self.q)[0][-1]
            await self.move([*position[:2], .38])
            self.phase = "lifted"
        elif skill == "place":
            await self.move([*self.tray[:2], .32])
            await self.move([*self.tray[:2], .144])
            await self.grip(False)
            self.attached = False
            self.cube = self.np.array([*self.tray[:2], .0565])
            self.phase = "placed"
        elif skill == "recover":
            await self.grip(False)
            await self.move([*self.cube[:2], .24])
            await self.move([*self.cube[:2], self.cube[2] + .0875])
            await self.grip(True)
            self.attached = True
            await self.move([*self.cube[:2], .38])
            self.phase = "lifted"
        elif skill == "retreat":
            await self.move([*self.tray[:2], .38])
            self.phase = "complete"
        self.draw()
        return skill

    async def inject(self, kind: str):
        if kind == "miss":
            if self.attached or self.phase not in ("ready", "hover"):
                raise HTTPException(409, "请在抓取前注入抓空故障")
            self.miss_next = True
        elif kind == "drop":
            if not self.truth()["grasped"]:
                raise HTTPException(409, "请先抓住并抬起方块，再模拟掉落")
            self.attached = False
            start = self.cube.copy()
            for i in range(1, 21):
                self.cube = start.copy()
                self.cube[2] = max(.0375, start[2] - (start[2] - .0375) * (i / 20) ** 2)
                self.draw()
                await asyncio.sleep(.025)
            self.phase = "dropped"
            await self.grip(False)
        elif kind == "shift":
            self.tray[0] = -.17 if self.tray[0] > -.10 else -.02
        self.revision += 1
        self.draw()

    async def observe(self, client):
        from PIL import Image
        from scipy.spatial.transform import Rotation

        clients = self.viewers()
        if not clients:
            raise HTTPException(409, "请等待三维视图连接，再进行视觉判断")
        position = self.np.array([.55, -1.05, .70])
        look_at = self.np.array([-.05, .02, .15])
        z = look_at - position
        z /= self.np.linalg.norm(z)
        x = self.np.cross(z, (0., 0., 1.))
        x /= self.np.linalg.norm(x)
        rotation = self.np.stack([x, self.np.cross(z, x), z], axis=1)
        quaternion = Rotation.from_matrix(rotation).as_quat()[[3, 0, 1, 2]]
        self.server.flush()
        viewer = next(iter(clients.values()))
        frame = await asyncio.to_thread(viewer.get_render, height=720, width=960, position=position,
                                        wxyz=quaternion, fov=.76, transport_format="jpeg", timeout=15)
        stream = io.BytesIO()
        Image.fromarray(frame).save(stream, format="PNG")
        filename = f"{len(self.history):04d}.png"
        (self.path / filename).write_bytes(stream.getvalue())
        started = time.perf_counter()
        response = await asyncio.to_thread(
            client.system_one, {"task": "Put the red cube into the blue tray and release it.", "camera": "<image:1>"},
            {"placed": Noul("The red cube is inside the blue tray and has been released by the gripper."),
             "grasped": Noul("The red cube is held between the gripper fingers and lifted above the table."),
             "location": Choice("Where is the red cube?", {"table": "On the table outside the blue tray", "gripper": "In the robot gripper, lifted above the table", "tray": "Inside the blue tray"})},
            media=[{"type": "image", "data": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()}],
        )
        answers = {k: v.raw for k, v in response.answers.items()}
        for key in ("placed", "grasped"):
            probability(answers[key]["noul"], key)
        record = {"step": len(self.history), "phase": self.phase, "revision": self.revision,
                  "image": f"/api/robot/runs/{self.id}/{filename}", "answers": answers,
                  "truth": self.truth(), "model": response.model, "meta": response.meta,
                  "usage": response.usage, "request_s": time.perf_counter() - started,
                  "q": self.q.tolist(), "cube": self.cube.tolist(), "tray": self.tray.tolist()}
        self.history.append(record)
        self.judged_revision = self.revision
        return record

    def close(self):
        self.server.stop()


class RobotAction(BaseModel):
    judge: bool = True


class RobotFault(BaseModel):
    kind: Literal["miss", "drop", "shift"]


def install_routes(app, studio):
    """Lazy optional integration; base Studio does not import robotics dependencies."""
    holder = {"world": None}
    start_lock = asyncio.Lock()

    async def world():
        async with start_lock:
            if holder["world"] is None:
                try:
                    holder["world"] = await asyncio.to_thread(RobotWorld, studio.config.output)
                except ImportError as exc:
                    raise HTTPException(503, "请先安装机器人展示支持：pip install -e '.[robot]'") from exc
            return holder["world"]

    async def close():
        if holder["world"] is not None:
            await asyncio.to_thread(holder["world"].close)

    studio.close_robot = close

    @app.get("/api/robot")
    async def state():
        return (await world()).snapshot()

    @app.get("/robot-view/")
    async def viewer():
        live = await world()
        path = Path(live.viser.__file__).parent / "client/build/index.html"
        return FileResponse(path)

    @app.websocket("/robot-ws")
    async def proxy(websocket: WebSocket):
        import websockets.asyncio.client

        live = await world()
        try:
            async with websockets.asyncio.client.connect(
                f"ws://127.0.0.1:{live.server.get_port()}",
                subprotocols=websocket.scope.get("subprotocols", []), max_size=None,
            ) as upstream:
                await websocket.accept(subprotocol=upstream.subprotocol)

                async def incoming():
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        await upstream.send(message.get("bytes") if message.get("bytes") is not None else message["text"])

                async def outgoing():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, OSError):
            return

    async def operate(action, *, judge=False):
        live = await world()
        if live.lock.locked():
            raise HTTPException(409, "机械臂正在执行，请稍候")
        async with live.lock:
            live.busy, live.error = True, None
            try:
                await action(live)
                if judge:
                    if studio.backend.state == "ready":
                        try:
                            await live.observe(studio.backend.client)
                        except Exception as exc:
                            live.error = f"动作状态已保留，视觉判断暂未完成：{getattr(exc, 'detail', str(exc))}"
                    else:
                        live.error = "模型尚未就绪，动作状态已保留，可稍后点击视觉判断"
            finally:
                live.busy = False
                live.save()
        return live.snapshot()

    @app.post("/api/robot/step")
    async def step(options: RobotAction):
        return await operate(lambda live: live.execute(), judge=options.judge)

    @app.post("/api/robot/observe")
    async def observe():
        async def noop(live):
            return None
        return await operate(noop, judge=True)

    @app.post("/api/robot/reset")
    async def reset():
        async def action(live):
            live.reset()
        return await operate(action)

    @app.post("/api/robot/fault")
    async def fault(options: RobotFault):
        return await operate(lambda live: live.inject(options.kind))

    @app.get("/api/robot/runs/{identifier}/{name}")
    async def image(identifier: str, name: str):
        if len(identifier) != 32 or any(ch not in "0123456789abcdef" for ch in identifier):
            raise HTTPException(404)
        if len(name) != 8 or not name[:4].isdigit() or name[4:] != ".png":
            raise HTTPException(404)
        path = studio.config.output / "robot" / identifier / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")
