"""Viser robot workbench with scripted kinematics and independent visual checks.

This is a kinematic demonstration, not a dynamics simulator or a hardware driver.
The visual model receives a fixed camera image, never the scene's ground truth.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from uuid import uuid4

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .browser import probability
from .client import Choice, Noul

PHASES = {"ready": "等待开始", "hover": "已靠近物体", "grasped": "夹爪已闭合", "lifted": "已抬起", "dropped": "物体掉落", "placed": "已放置", "complete": "动作结束"}
SKILLS = {"approach": "靠近方块", "grasp": "闭合夹爪", "lift": "抬起方块", "place": "放入托盘", "recover": "重新抓取", "retreat": "收回机械臂"}
FAULTS = {"miss": "设置抓空", "drop": "方块掉落", "shift": "移动托盘"}
SCENARIOS = {
    "normal": {"name": "正常抓放", "description": "看机械臂靠近、抓住、抬起方块，放入托盘后收回。", "fault": None},
    "miss": {"name": "抓空后重试", "description": "靠近后设置一次抓空；观察空夹爪抬起，再重新抓取并放置。", "fault": "miss", "phase": "hover"},
    "drop": {"name": "掉落后恢复", "description": "抓住并抬起后模拟一次掉落；观察机械臂重新抓取并完成放置。", "fault": "drop", "phase": "lifted"},
    "shift": {"name": "目标移动", "description": "抬起方块后移动托盘；后续放置使用新的目标位置。", "fault": "shift", "phase": "lifted"},
}


def agreement(record):
    """Keep uncertainty distinct from disagreement; never adjust model scores."""
    if not record:
        return "等待观察"
    uncertain = False
    for key in ("placed", "grasped"):
        p = record["answers"][key]["noul"]
        if .15 < p < .85:
            uncertain = True
        elif (p >= .85) != record["truth"][key]:
            return "需要复核"
    return "模型不确定" if uncertain else "判断与场景一致"


def report_path(root: Path, identifier: str):
    if len(identifier) != 32 or any(ch not in "0123456789abcdef" for ch in identifier):
        raise HTTPException(404, "找不到机器人记录")
    return root / identifier / "report.json"


def read_report(root: Path, identifier: str):
    try:
        return json.loads(report_path(root, identifier).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "找不到有效的机器人记录") from None


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
        self.auto_running = False
        self.playlist = None
        self.viewer_cursor = 0
        self.delay, self.motion_speed = 3., 1.
        self.auto_judge = True
        self.pause_on_review = True
        try:
            preferences = RobotPlayback.model_validate(json.loads((self.root / "settings.json").read_text(encoding="utf-8")))
            self.delay, self.motion_speed, self.auto_judge = preferences.delay, preferences.motion_speed, preferences.judge
            self.pause_on_review = preferences.pause_on_review
        except (OSError, ValueError, TypeError):
            pass
        self.next_step_at = None
        self.pause_reason = None
        self.reset()

    def reset(self, scenario=None):
        np = self.np
        scenario = scenario or getattr(self, "scenario", "normal")
        if scenario not in SCENARIOS:
            raise HTTPException(422, "未知的机器人案例")
        self.scenario, self.scenario_fault_done = scenario, False
        self.id = uuid4().hex
        self.path = self.root / self.id
        self.path.mkdir()
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.phase = "ready"
        self.cube = np.array([.08, -.18, .0375])
        self.tray = np.array([-.02, .23, 0.])
        self.q = self.kinematics.solve([-.15, -.20, .40], np.array([0., -1., 1.7, 0., -.7, 0.]))
        self.attached, self.miss_next = False, False
        self.gap = .13
        self.history: list[dict] = []
        self.events: list[dict] = []
        self.replay = None
        self.capture_count = 0
        self.revision, self.judged_revision = 0, -1
        self.error = None
        self.pause_reason = None
        self.playlist = None
        self.pending_observation, self.retrying_observation = False, False
        self.observation_errors: list[dict] = []
        self.last_capture = None
        self.draw()
        self.record_event("reset", "初始场景", 0.)
        self.save()

    def truth(self) -> dict:
        in_tray = bool(not self.attached and abs(self.cube[0] - self.tray[0]) < .10
                       and abs(self.cube[1] - self.tray[1]) < .08 and abs(self.cube[2] - .0565) < .018)
        return {"placed": in_tray, "grasped": bool(self.attached and self.cube[2] > .12)}

    def next_skill(self):
        return {"ready": "approach", "hover": "grasp", "grasped": "lift", "dropped": "recover", "placed": "retreat"}.get(
            self.phase, ("place" if self.attached else "recover") if self.phase == "lifted" else None)

    def next_action(self):
        case = SCENARIOS[self.scenario]
        fault = case["fault"]
        if fault and not self.scenario_fault_done and self.phase == case["phase"]:
            if fault != "drop" or self.truth()["grasped"]:
                return {"kind": "fault", "name": fault, "label": FAULTS[fault]}
        skill = self.next_skill()
        return {"kind": "skill", "name": skill, "label": SKILLS[skill]} if skill else None

    async def advance(self):
        action = self.next_action()
        if action and action["kind"] == "fault":
            await self.inject(action["name"])
            return action["name"]
        return await self.execute()

    def snapshot(self):
        return {"id": self.id, "phase": self.phase, "phase_label": PHASES[self.phase],
                "next_skill": self.next_skill(), "busy": self.busy, "attached": self.attached,
                "miss_next": self.miss_next, "truth": self.truth(), "revision": self.revision,
                "judged_revision": self.judged_revision, "history": self.history, "error": self.error,
                "clients": len(self.viewers()), "q": self.q.tolist(), "cube": self.cube.tolist(),
                "tray": self.tray.tolist(), "gap": self.gap, "simulation": "kinematic",
                "schema_version": 2, "created_at": self.created_at, "events": self.events,
                "replay": self.replay, "auto_running": self.auto_running, "delay": self.delay,
                "motion_speed": self.motion_speed, "auto_judge": self.auto_judge,
                "pause_on_review": self.pause_on_review,
                "playlist": self.playlist, "playlist_id": self.playlist["id"] if self.playlist else None,
                "pending_observation": self.pending_observation, "retrying_observation": self.retrying_observation,
                "observation_errors": self.observation_errors,
                "next_step_at": self.next_step_at, "pause_reason": self.pause_reason,
                "next_action": self.next_action(), "scenario": {"key": self.scenario,
                    **SCENARIOS[self.scenario], "fault_done": self.scenario_fault_done}}

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
        payload = self.snapshot()
        # A report describes its own live run, never another run's replay selection.
        payload["replay"] = None
        payload.pop("playlist")
        temporary = self.path / "report.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(self.path / "report.json")

    def save_settings(self):
        temporary = self.root / "settings.tmp"
        temporary.write_text(json.dumps({"delay": self.delay, "motion_speed": self.motion_speed,
                                         "judge": self.auto_judge, "pause_on_review": self.pause_on_review}), encoding="utf-8")
        temporary.replace(self.root / "settings.json")

    def pose(self):
        return {"q": self.q.tolist(), "cube": self.cube.tolist(), "tray": self.tray.tolist(),
                "gap": self.gap, "attached": self.attached, "phase": self.phase}

    def record_event(self, kind, label, duration):
        self.events.append({"index": len(self.events), "kind": kind, "label": label,
                            "at": datetime.now(timezone.utc).isoformat(), "duration_s": duration,
                            "revision": self.revision, "pose": self.pose(), "truth": self.truth()})

    def show_replay(self, report, *, event_index=None, history_index=None):
        try:
            if event_index is not None:
                if event_index < 0:
                    raise IndexError
                event = report["events"][event_index]
                pose, truth, label = event["pose"], event["truth"], event["label"]
                record = next((r for r in reversed(report["history"]) if r["revision"] == event["revision"]), None)
            else:
                if history_index is None or history_index < 0:
                    raise IndexError
                record = report["history"][history_index]
                # Older reports have no gripper gap. They remain viewable as images only.
                pose, truth, label = record["pose"], record["truth"], f"第 {history_index + 1} 次视觉检查"
            for key, length, bound in (("q", 6, math.pi + .01), ("cube", 3, 2), ("tray", 3, 2)):
                if len(pose[key]) != length or any(not math.isfinite(v) or abs(v) > bound for v in pose[key]):
                    raise ValueError
            if not math.isfinite(pose["gap"]) or not 0 <= pose["gap"] <= .2:
                raise ValueError
        except (IndexError, KeyError, TypeError, ValueError):
            raise HTTPException(422, "该记录缺少有效的三维姿态，无法回放") from None
        self.replay = {"run_id": report["id"], "event_index": event_index, "history_index": history_index,
                       "label": label, "pose": pose, "truth": truth, "record": record}
        self.draw(pose)

    def draw(self, pose=None):
        from scipy.spatial.transform import Rotation

        np = self.np
        points, rotation = self.kinematics.forward(self.q if pose is None else pose["q"])
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
                gap = self.gap if pose is None else pose["gap"]
                finger.position = ((-1 if index == 0 else 1) * (gap + .018) / 2, 0, -.065)
            if pose is None and self.attached:
                self.cube = points[-1] + rotation @ np.array([0., 0., -.0875])
            self.cube_node.position = self.cube if pose is None else np.array(pose["cube"])
            self.tray_node.position = self.tray if pose is None else np.array(pose["tray"])

    async def move(self, target, duration=1.2):
        duration /= self.motion_speed
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
        frame_time = .025 / self.motion_speed
        for i in range(1, 13):
            self.gap = start + (goal - start) * i / 12
            self.draw()
            await asyncio.sleep(frame_time)

    async def execute(self):
        started = time.perf_counter()
        skill = self.next_skill()
        if skill is None:
            raise HTTPException(409, "动作已结束，可以重置场景再看一次")
        self.revision += 1
        self.pending_observation = False
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
        self.record_event("skill", SKILLS[skill], time.perf_counter() - started)
        return skill

    async def inject(self, kind: str):
        started = time.perf_counter()
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
        self.pending_observation = False
        self.draw()
        if kind == SCENARIOS[self.scenario]["fault"]:
            self.scenario_fault_done = True
        self.record_event("fault", FAULTS[kind], time.perf_counter() - started)

    async def observe(self, client):
        from PIL import Image
        from scipy.spatial.transform import Rotation

        self.last_capture = None
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
        viewer = list(clients.values())[self.viewer_cursor % len(clients)]
        self.viewer_cursor += 1
        frame = await asyncio.to_thread(viewer.get_render, height=720, width=960, position=position,
                                        wxyz=quaternion, fov=.76, transport_format="jpeg", timeout=15)
        stream = io.BytesIO()
        Image.fromarray(frame).save(stream, format="PNG")
        filename = f"{self.capture_count:04d}.png"
        self.capture_count += 1
        (self.path / filename).write_bytes(stream.getvalue())
        self.last_capture = f"/api/robot/runs/{self.id}/{filename}"
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
                  "q": self.q.tolist(), "cube": self.cube.tolist(), "tray": self.tray.tolist(),
                  "pose": self.pose(), "at": datetime.now(timezone.utc).isoformat()}
        self.history.append(record)
        self.judged_revision = self.revision
        return record

    def close(self):
        self.server.stop()


class RobotControl(BaseModel):
    run_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    playlist_id: str | None = None


class RobotAction(RobotControl):
    judge: bool = True


class RobotFault(RobotControl):
    kind: Literal["miss", "drop", "shift"]


class RobotScenario(RobotControl):
    scenario: Literal["normal", "miss", "drop", "shift"] | None = None


class RobotPlayback(RobotAction):
    delay: float = Field(default=3, ge=.5, le=15, allow_inf_nan=False)
    motion_speed: float = Field(default=1, ge=.25, le=2, allow_inf_nan=False)
    pause_on_review: bool = True


class RobotDemo(RobotPlayback):
    scenario: Literal["normal", "miss", "drop", "shift"] | None = None
    pause_on_review: Literal[False] = False
    sequence: Literal["current", "all"] = "current"
    rounds: int = Field(default=1, ge=1, le=20)
    case_delay: float = Field(default=5, ge=.5, le=30, allow_inf_nan=False)


class RobotReplay(RobotControl):
    source_id: str
    event_index: int | None = Field(default=None, ge=0)
    history_index: int | None = Field(default=None, ge=0)


class RobotController:
    """One executor per shared scene, independent of browser tabs and their timers."""

    def __init__(self, live, backend):
        self.live, self.backend = live, backend
        self.auto_task = None
        self.wake = asyncio.Event()

    def save_playlist(self):
        playlist = self.live.playlist
        if not playlist:
            return
        path = report_path(self.live.root / "playlists", playlist["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(playlist, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)

    def update_item(self):
        live = self.live
        playlist = live.playlist
        if playlist:
            item = playlist["items"][playlist["index"]]
            item.update(run_id=live.id, phase=live.phase, steps=max(0, len(live.events) - 1),
                        checks=len(live.history), reviews=sum(agreement(r) != "判断与场景一致" for r in live.history),
                        observation_errors=len(live.observation_errors), truth=live.truth(), error=live.error)
            self.save_playlist()

    def has_next_case(self):
        playlist = self.live.playlist
        return bool(playlist and playlist["status"] in ("running", "paused")
                    and playlist["index"] + 1 < len(playlist["items"]))

    async def check_observation(self, automatic):
        live = self.live
        was_pending = live.pending_observation
        live.pending_observation = True
        for attempt in range(1, 3 if automatic else 2):
            live.last_capture = None
            try:
                if self.backend.state != "ready":
                    raise ConnectionError("模型尚未就绪")
                await live.observe(self.backend.client)
                live.pending_observation = live.retrying_observation = False
                live.error = None
                if was_pending and not live.auto_running:
                    live.pause_reason = "视觉判断已恢复，可继续自动演示"
                return
            except Exception as exc:
                message = str(getattr(exc, "detail", str(exc)))
                live.error = f"动作状态已保留，视觉判断暂未完成：{message}"
                live.observation_errors.append({"revision": live.revision, "attempt": attempt,
                    "at": datetime.now(timezone.utc).isoformat(), "message": message, "image": live.last_capture})
                transient = exc.code in (429, 502, 503, 504) if isinstance(exc, HTTPError) else isinstance(exc, (URLError, OSError, TimeoutError))
                if not automatic or attempt == 2 or not transient or not live.auto_running:
                    break
                live.retrying_observation = True
                live.save()
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=.5)
                except asyncio.TimeoutError:
                    pass
                if not live.auto_running:
                    break
        live.retrying_observation = False

    def validate(self, options, *, revision=True):
        same_playlist = bool(options.playlist_id and self.live.playlist and options.playlist_id == self.live.playlist["id"])
        if options.playlist_id and not same_playlist:
            raise HTTPException(409, "连播任务已变化，请查看最新状态后再操作")
        if options.run_id is not None and options.run_id != self.live.id and not (same_playlist and not revision):
            raise HTTPException(409, "场景已被重置，请查看最新状态后再操作")
        if revision and options.expected_revision is not None and options.expected_revision != self.live.revision:
            raise HTTPException(409, "其他页面已推进场景，请查看最新状态后再操作")

    async def operate(self, action, options, *, judge=False, from_auto=False, replay_ok=False):
        live = self.live
        if not from_auto and self.auto_task and not self.auto_task.done() and not live.auto_running:
            raise HTTPException(409, "播放正在停止，请稍候再操作场景")
        if live.lock.locked():
            raise HTTPException(409, "机械臂正在执行，请稍候")
        async with live.lock:
            self.validate(options)
            if live.auto_running and not from_auto:
                raise HTTPException(409, "请先暂停自动演示，再操作场景")
            if live.replay and not replay_ok:
                raise HTTPException(409, "当前为历史回放，请先返回当前场景")
            live.busy, live.error = True, None
            try:
                await action(live)
                if judge:
                    await self.check_observation(from_auto)
            finally:
                live.busy = False
                self.update_item()
                live.save()
        return live.snapshot()

    def check_start(self, judge):
        live = self.live
        if live.lock.locked() or (self.auto_task and not self.auto_task.done()):
            raise HTTPException(409, "当前动作正在结束，请稍候")
        if live.replay:
            raise HTTPException(409, "请先返回当前场景，再开始演示")
        if judge and self.backend.state != "ready":
            raise HTTPException(409, "请等待模型就绪，或关闭动作后自动判断")

    async def start(self, options):
        live = self.live
        self.validate(options)
        if live.auto_running:
            return live.snapshot()  # A second tab attaches to the existing playback.
        self.check_start(options.judge)
        if not live.next_action() and not self.has_next_case() and not (live.pending_observation and options.judge):
            raise HTTPException(409, "动作已结束，请重置场景")
        live.delay, live.motion_speed = options.delay, options.motion_speed
        live.auto_judge = options.judge
        live.pause_on_review = options.pause_on_review
        live.save_settings()
        live.auto_running, live.pause_reason = True, None
        if live.playlist and live.playlist["status"] == "paused":
            live.playlist["status"] = "running"
            item = live.playlist["items"][live.playlist["index"]]
            if item["status"] != "completed":
                item["status"] = "running"
            self.save_playlist()
        self.wake.clear()
        self.auto_task = asyncio.create_task(self.autoplay())
        live.save()
        return live.snapshot()

    async def demo(self, options):
        """Reset and start as one event-loop operation; retain the previous report."""
        live = self.live
        self.validate(options)
        if live.auto_running:
            raise HTTPException(409, "请先暂停当前演示，再从头开始")
        self.check_start(options.judge)
        # No awaits between validation, reset and start: another tab cannot
        # advance the fresh run before its single automatic executor is created.
        self.finish_playlist("stopped")
        cases = list(SCENARIOS) if options.sequence == "all" else [options.scenario or live.scenario]
        items = [{"scenario": case, "name": SCENARIOS[case]["name"], "round": round_number,
                  "run_id": None, "status": "pending", "checks": 0, "reviews": 0}
                 for round_number in range(1, options.rounds + 1) for case in cases]
        live.reset(items[0]["scenario"])
        if len(items) > 1:
            live.playlist = {"id": uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "running", "sequence": options.sequence, "rounds": options.rounds,
                "case_delay": options.case_delay, "index": 0, "next_case_at": None, "items": items}
            items[0].update(run_id=live.id, status="running")
            self.save_playlist()
        fresh = options.model_copy(update={"run_id": live.id, "expected_revision": 0,
                                           "playlist_id": live.playlist["id"] if live.playlist else None})
        return await self.start(fresh)

    def pause(self, options):
        self.validate(options, revision=False)
        self.live.auto_running = False
        self.live.next_step_at = None
        self.live.pause_reason = "已暂停，当前动作完成后停留" if self.live.busy else "已暂停"
        if self.live.playlist and self.live.playlist["status"] == "running":
            self.live.playlist["status"] = "paused"
            self.live.playlist["next_case_at"] = None
            self.save_playlist()
        self.wake.set()
        self.live.save()
        return self.live.snapshot()

    def finish_playlist(self, status):
        playlist = self.live.playlist
        if playlist and playlist["status"] in ("running", "paused"):
            self.update_item()
            playlist["status"], playlist["next_case_at"] = status, None
            item = playlist["items"][playlist["index"]]
            if item["status"] != "completed":
                item["status"] = status
            self.save_playlist()

    def stop(self, options):
        self.validate(options, revision=False)
        self.pause(options)
        self.finish_playlist("stopped")
        self.live.pause_reason = "连播已结束，当前动作完成后停留" if self.live.busy else "连播已结束，当前场景已保留"
        self.live.save()
        return self.live.snapshot()

    def settings(self, options):
        self.validate(options, revision=False)
        self.live.delay, self.live.motion_speed = options.delay, options.motion_speed
        if not self.live.auto_running:
            self.live.auto_judge = options.judge
            self.live.pause_on_review = options.pause_on_review
        self.live.save_settings()
        self.wake.set()
        self.live.save()
        return self.live.snapshot()

    async def wait_between(self, delay, *, cases=False):
        live = self.live
        completed = time.monotonic()
        try:
            while live.auto_running:
                remaining = completed + delay() - time.monotonic()
                deadline = time.time() + max(0, remaining)
                if cases:
                    live.playlist["next_case_at"] = deadline
                    self.save_playlist()
                else:
                    live.next_step_at = deadline
                if remaining <= 0:
                    break
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
        finally:
            if cases:
                live.playlist["next_case_at"] = None
                self.save_playlist()
            else:
                live.next_step_at = None

    async def autoplay(self):
        live = self.live
        try:
            while live.auto_running:
                if live.pending_observation and live.auto_judge:
                    async def noop(_live):
                        return None
                    await self.operate(noop, RobotControl(), judge=True, from_auto=True)
                elif not live.next_action():
                    if live.playlist:
                        live.playlist["items"][live.playlist["index"]]["status"] = "completed"
                        self.update_item()
                    if not self.has_next_case():
                        self.finish_playlist("completed")
                        live.pause_reason = "连播已完成" if live.playlist and live.playlist["status"] == "completed" else "动作已完成"
                        break
                    await self.wait_between(lambda: live.playlist["case_delay"], cases=True)
                    if not live.auto_running:
                        break
                    playlist = live.playlist
                    live.save()
                    playlist["index"] += 1
                    item = playlist["items"][playlist["index"]]
                    live.reset(item["scenario"])
                    live.playlist = playlist
                    item.update(run_id=live.id, status="running")
                    self.save_playlist()
                    live.save()
                    continue
                else:
                    await self.operate(lambda w: w.advance(), RobotControl(), judge=live.auto_judge, from_auto=True)
                if live.error:
                    live.pause_reason = "视觉判断未完成，演示已暂停；继续时将重试当前画面"
                    break
                if live.auto_judge and live.pause_on_review and agreement(live.history[-1] if live.history else None) != "判断与场景一致":
                    live.pause_reason = "模型判断需要复核，演示已暂停；可检查画面或手动执行"
                    break
                if live.next_action():
                    await self.wait_between(lambda: live.delay)
        except Exception as exc:
            live.error = f"演示暂停：{getattr(exc, 'detail', str(exc))}"
            live.pause_reason = "执行未完成，请检查当前场景"
        finally:
            live.auto_running, live.next_step_at = False, None
            if live.playlist and live.playlist["status"] not in ("completed", "stopped"):
                live.playlist["status"] = "paused"
                item = live.playlist["items"][live.playlist["index"]]
                if item["status"] != "completed":
                    item["status"] = "paused"
            self.update_item()
            live.save()

    async def replay(self, options):
        if (options.event_index is None) == (options.history_index is None):
            raise HTTPException(422, "请选择一条动作或视觉检查记录")
        report = self.live.snapshot() if options.source_id == self.live.id else read_report(self.live.root, options.source_id)

        async def action(live):
            live.show_replay(report, event_index=options.event_index, history_index=options.history_index)

        return await self.operate(action, options, replay_ok=True)

    async def resume_live(self, options):
        async def action(live):
            live.replay = None
            live.draw()
        return await self.operate(action, options, replay_ok=True)

    async def close(self):
        self.live.auto_running = False
        self.wake.set()
        if self.auto_task:
            await self.auto_task
        await asyncio.to_thread(self.live.close)


def install_routes(app, studio):
    """Lazy optional integration; base Studio does not import robotics dependencies."""
    holder = {"world": None, "controller": None}
    start_lock = asyncio.Lock()

    async def world():
        async with start_lock:
            if holder["world"] is None:
                try:
                    holder["world"] = await asyncio.to_thread(RobotWorld, studio.config.output)
                    holder["controller"] = RobotController(holder["world"], studio.backend)
                except ImportError as exc:
                    raise HTTPException(503, "请先安装机器人展示支持：pip install -e '.[robot]'") from exc
            return holder["world"]

    async def close():
        if holder["world"] is not None:
            await holder["controller"].close()

    studio.close_robot = close

    @app.get("/api/robot")
    async def state():
        return (await world()).snapshot()

    @app.get("/robot-view/")
    async def viewer():
        live = await world()
        path = Path(live.viser.__file__).parent / "client/build/index.html"
        # Keep the installed Viser client intact. The adapter only changes its
        # embedded controls; scene rendering and WebSocket messages stay native.
        content = path.read_text(encoding="utf-8").replace(
            "</head>", '<link rel="stylesheet" href="/assets/robot-view.css">'
            '<script src="/assets/robot-view.js" defer></script></head>', 1)
        return HTMLResponse(content, headers={"Cache-Control": "no-cache"})

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

    async def controller():
        await world()
        return holder["controller"]

    @app.post("/api/robot/step")
    async def step(options: RobotAction):
        control = await controller()
        if control.live.pending_observation and options.judge:
            raise HTTPException(409, "请先重试当前画面的视觉判断，或关闭自动判断后继续")
        return await control.operate(lambda live: live.advance(), options, judge=options.judge)

    @app.post("/api/robot/observe")
    async def observe(options: RobotControl = RobotControl()):
        async def noop(live):
            return None
        return await (await controller()).operate(noop, options, judge=True)

    @app.post("/api/robot/reset")
    async def reset(options: RobotScenario = RobotScenario()):
        control = await controller()
        async def action(live):
            control.finish_playlist("stopped")
            live.reset(options.scenario)
        return await control.operate(action, options)

    @app.post("/api/robot/fault")
    async def fault(options: RobotFault):
        return await (await controller()).operate(lambda live: live.inject(options.kind), options)

    @app.post("/api/robot/auto")
    async def auto(options: RobotPlayback):
        return await (await controller()).start(options)

    @app.post("/api/robot/demo")
    async def demo(options: RobotDemo):
        return await (await controller()).demo(options)

    @app.post("/api/robot/pause")
    async def pause(options: RobotControl):
        return (await controller()).pause(options)

    @app.post("/api/robot/stop")
    async def stop(options: RobotControl):
        return (await controller()).stop(options)

    @app.get("/api/robot/playlists")
    async def playlists():
        live = await world()
        reports = []
        for path in sorted((live.root / "playlists").glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                report = read_report(live.root / "playlists", path.parent.name)
                reports.append({"id": report["id"], "created_at": report["created_at"], "status": report["status"],
                                "completed": sum(i["status"] == "completed" for i in report["items"]), "total": len(report["items"])})
            except (HTTPException, KeyError, TypeError):
                continue
            if len(reports) >= 100:
                break
        return {"playlists": reports}

    @app.get("/api/robot/playlists/{identifier}")
    async def playlist_report(identifier: str):
        live = await world()
        if live.playlist and live.playlist["id"] == identifier:
            return live.playlist
        return read_report(live.root / "playlists", identifier)

    @app.post("/api/robot/settings")
    async def settings(options: RobotPlayback):
        return (await controller()).settings(options)

    @app.post("/api/robot/replay")
    async def replay(options: RobotReplay):
        return await (await controller()).replay(options)

    @app.post("/api/robot/live")
    async def resume_live(options: RobotControl):
        return await (await controller()).resume_live(options)

    @app.get("/api/robot/runs")
    async def runs():
        live = await world()
        reports = []
        for path in sorted(live.root.glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                report = read_report(live.root, path.parent.name)
                reports.append({"id": report["id"], "created_at": report.get("created_at"),
                                "phase_label": report["phase_label"], "events": len(report.get("events", [])),
                                "checks": len(report.get("history", [])),
                                "scenario": report.get("scenario", {}).get("name", "自由演示")})
            except (HTTPException, KeyError, TypeError):
                continue
            if len(reports) >= 100:
                break
        return {"runs": reports}

    @app.get("/api/robot/runs/{identifier}")
    async def report(identifier: str):
        live = await world()
        return live.snapshot() if identifier == live.id else read_report(live.root, identifier)

    @app.get("/api/robot/runs/{identifier}/{name}")
    async def image(identifier: str, name: str):
        report_path(studio.config.output / "robot", identifier)
        if len(name) < 8 or not name[:-4].isdigit() or name[-4:] != ".png":
            raise HTTPException(404)
        path = studio.config.output / "robot" / identifier / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")
