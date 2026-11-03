"""Kinematic correctness and fault recovery; optional robot dependencies only."""
import importlib.util
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from qev.robot import Kinematics, RobotWorld, RobotController, RobotControl, RobotPlayback, RobotDemo, RobotReplay, read_report

HAS_ROBOT = all(importlib.util.find_spec(name) is not None for name in ("viser", "scipy", "numpy"))


@unittest.skipUnless(HAS_ROBOT, "install the robot optional dependencies")
class KinematicsTests(unittest.TestCase):
    def test_inverse_kinematics_reaches_all_skill_targets(self):
        import numpy as np

        arm = Kinematics()
        seed = np.array([0., -1., 1.7, 0., -.7, 0.])
        for target in ([.08, -.18, .24], [.08, -.18, .125], [.08, -.18, .38], [-.02, .23, .144], [-.17, .23, .144]):
            seed = arm.solve(target, seed)
            points, rotation = arm.forward(seed)
            self.assertLess(np.linalg.norm(points[-1] - target), .005)
            self.assertTrue(np.all(np.abs(seed) <= np.pi))
            self.assertAlmostEqual(np.linalg.det(rotation), 1)

    def test_unreachable_target_is_rejected(self):
        import numpy as np

        with self.assertRaises(ValueError):
            Kinematics().solve([20., 0., 0.], np.zeros(6))


@unittest.skipUnless(HAS_ROBOT, "install the robot optional dependencies")
class RobotSkillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.world = RobotWorld(Path(self.temp.name))

        async def move(target, duration=1.2):
            self.world.q = self.world.kinematics.solve(target, self.world.q)
            self.world.draw()

        async def grip(closed):
            self.world.gap = .076 if closed else .13
            self.world.draw()

        self.world.move, self.world.grip = move, grip
        self.controller = RobotController(self.world, SimpleNamespace(state="ready", client=None))

    async def asyncTearDown(self):
        await self.controller.close()
        self.temp.cleanup()

    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), timeout=2)

    async def test_normal_pick_and_place_has_independent_success_evidence(self):
        self.assertEqual(self.world.truth(), {"placed": False, "grasped": False})
        executed = []
        while self.world.next_skill():
            executed.append(await self.world.execute())
            if self.world.phase == "lifted":
                self.assertTrue(self.world.truth()["grasped"])
        self.assertEqual(executed, ["approach", "grasp", "lift", "place", "retreat"])
        self.assertTrue(self.world.truth()["placed"])
        self.assertFalse(self.world.attached)

    async def test_missed_grasp_is_detected_and_can_be_recovered(self):
        await self.world.inject("miss")
        for _ in range(3):
            await self.world.execute()
        self.assertEqual(self.world.phase, "lifted")
        self.assertFalse(self.world.truth()["grasped"])
        self.assertEqual(self.world.next_skill(), "recover")
        self.assertAlmostEqual(self.world.cube[2], .0375)
        await self.world.execute()
        self.assertTrue(self.world.truth()["grasped"])
        await self.world.execute()
        self.assertTrue(self.world.truth()["placed"])

    async def test_drop_releases_the_cube_and_preserves_a_recovery_path(self):
        for _ in range(3):
            await self.world.execute()
        await self.world.inject("drop")
        self.assertFalse(self.world.attached)
        self.assertEqual(self.world.phase, "dropped")
        self.assertAlmostEqual(self.world.cube[2], .0375)
        await self.world.execute()
        self.assertTrue(self.world.truth()["grasped"])

    async def test_shifted_tray_changes_the_placement_target(self):
        for _ in range(3):
            await self.world.execute()
        previous = self.world.tray.copy()
        await self.world.inject("shift")
        self.assertNotEqual(previous[0], self.world.tray[0])
        await self.world.execute()
        self.assertAlmostEqual(self.world.cube[0], self.world.tray[0])
        self.assertTrue(self.world.truth()["placed"])

    async def test_invalid_fault_does_not_change_scene_state(self):
        initial = self.world.snapshot()
        with self.assertRaises(HTTPException):
            await self.world.inject("drop")
        self.assertEqual(self.world.snapshot()["revision"], initial["revision"])

    async def test_reset_starts_a_new_report_and_clears_faults(self):
        old_id = self.world.id
        await self.world.inject("miss")
        self.world.reset()
        self.assertNotEqual(self.world.id, old_id)
        self.assertFalse(self.world.miss_next)
        self.assertTrue((self.world.path / "report.json").is_file())

    async def test_every_skill_and_fault_records_an_independent_pose_without_inference(self):
        await self.controller.operate(lambda w: w.inject("miss"), RobotControl())
        await self.controller.operate(lambda w: w.execute(), RobotControl())
        report = read_report(self.world.root, self.world.id)
        self.assertEqual([event["kind"] for event in report["events"]], ["reset", "fault", "skill"])
        self.assertEqual(len(report["history"]), 0)
        self.assertEqual(report["events"][-1]["pose"]["phase"], "hover")
        self.assertNotEqual(report["events"][0]["pose"]["q"], report["events"][-1]["pose"]["q"])
        self.assertFalse((self.world.path / "report.tmp").exists())

    async def test_stale_tab_cannot_execute_another_skill_or_reset_a_new_run(self):
        stale = RobotControl(run_id=self.world.id, expected_revision=0)
        await self.controller.operate(lambda w: w.execute(), stale)
        with self.assertRaises(HTTPException) as caught:
            await self.controller.operate(lambda w: w.execute(), stale)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.world.phase, "hover")
        self.world.reset()
        with self.assertRaises(HTTPException):
            self.controller.pause(stale)
        self.assertEqual(self.world.revision, 0)

    async def test_two_tabs_share_one_automatic_executor_and_pause(self):
        options = RobotPlayback(run_id=self.world.id, expected_revision=0, judge=False, delay=15)
        await self.controller.start(options)
        original_task = self.controller.auto_task
        await self.controller.start(options)
        self.assertIs(self.controller.auto_task, original_task)
        await self.wait_until(lambda: self.world.phase == "hover")
        with self.assertRaises(HTTPException):
            await self.controller.operate(lambda w: w.execute(), RobotControl())
        self.controller.pause(RobotControl(run_id=self.world.id))
        await asyncio.wait_for(original_task, timeout=1)
        self.assertFalse(self.world.auto_running)
        self.assertEqual(self.world.revision, 1)
        self.assertIsNone(self.world.next_step_at)

    async def test_pause_during_a_skill_finishes_only_that_skill(self):
        started, release = asyncio.Event(), asyncio.Event()
        move = self.world.move

        async def blocked_move(target, duration=1.2):
            started.set()
            await release.wait()
            await move(target, duration)

        self.world.move = blocked_move
        await self.controller.start(RobotPlayback(judge=False, delay=.5))
        await asyncio.wait_for(started.wait(), timeout=1)
        state = self.controller.pause(RobotControl())
        self.assertTrue(state["busy"])
        self.assertFalse(state["auto_running"])
        release.set()
        await asyncio.wait_for(self.controller.auto_task, timeout=1)
        self.assertEqual(self.world.phase, "hover")
        self.assertEqual(self.world.revision, 1)

    async def test_changing_delay_wakes_the_existing_timer_and_settings_persist(self):
        await self.controller.start(RobotPlayback(judge=False, delay=15))
        await self.wait_until(lambda: self.world.phase == "hover")
        self.controller.settings(RobotPlayback(judge=False, delay=.5, motion_speed=.5))
        await self.wait_until(lambda: self.world.phase == "grasped")
        self.controller.pause(RobotControl())
        await self.controller.auto_task
        preferences = json.loads((self.world.root / "settings.json").read_text())
        self.assertEqual(preferences, {"delay": .5, "motion_speed": .5, "judge": False, "pause_on_review": True})

    async def test_uncertain_visual_result_pauses_without_rewriting_probability(self):
        async def observe(_client):
            record = {"answers": {"placed": {"noul": .42}, "grasped": {"noul": .03}}, "truth": self.world.truth()}
            self.world.history.append(record)
            return record

        self.world.observe = observe
        await self.controller.start(RobotPlayback(judge=True, delay=.5))
        await asyncio.wait_for(self.controller.auto_task, timeout=1)
        self.assertEqual(self.world.revision, 1)
        self.assertIn("复核", self.world.pause_reason)
        self.assertEqual(self.world.history[0]["answers"]["placed"]["noul"], .42)

    async def test_replay_changes_only_display_and_blocks_execution_and_inference(self):
        import numpy as np

        for _ in range(3):
            await self.world.execute()
        live = self.world.snapshot()
        await self.controller.replay(RobotReplay(source_id=self.world.id, event_index=0))
        self.assertEqual(self.world.phase, live["phase"])
        self.assertEqual(self.world.truth(), live["truth"])
        np.testing.assert_allclose(self.world.q, live["q"])
        np.testing.assert_allclose(self.world.cube, live["cube"])
        self.assertFalse(np.allclose(self.world.cube_node.position, self.world.cube))
        for judge in (False, True):
            with self.assertRaises(HTTPException) as caught:
                await self.controller.operate(lambda w: w.execute(), RobotControl(), judge=judge)
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.world.revision, live["revision"])
        self.assertIsNone(read_report(self.world.root, self.world.id)["replay"])
        await self.controller.resume_live(RobotControl())
        np.testing.assert_allclose(self.world.cube_node.position, live["cube"])
        self.assertIsNone(self.world.replay)

    async def test_archived_replay_does_not_modify_the_new_run(self):
        await self.controller.operate(lambda w: w.execute(), RobotControl())
        old_id = self.world.id
        self.world.reset()
        current = self.world.snapshot()
        await self.controller.replay(RobotReplay(source_id=old_id, event_index=1))
        self.assertEqual(self.world.replay["pose"]["phase"], "hover")
        self.assertEqual(self.world.id, current["id"])
        self.assertEqual(self.world.phase, "ready")
        self.assertEqual(self.world.revision, 0)

    async def test_invalid_replay_pose_is_rejected_without_changing_live_or_display(self):
        report = copy.deepcopy(self.world.snapshot())
        report["events"][0]["pose"]["q"][0] = float("nan")
        with self.assertRaises(HTTPException):
            self.world.show_replay(report, event_index=0)
        self.assertIsNone(self.world.replay)
        self.assertEqual(self.world.revision, 0)

    async def test_new_world_restores_preferences_and_archives_without_resuming_actions(self):
        await self.controller.operate(lambda w: w.execute(), RobotControl())
        old_id = self.world.id
        self.controller.settings(RobotPlayback(judge=False, delay=7, motion_speed=.5, pause_on_review=False))
        restarted = RobotWorld(Path(self.temp.name))
        try:
            self.assertNotEqual(restarted.id, old_id)
            self.assertEqual(restarted.phase, "ready")
            self.assertFalse(restarted.auto_running)
            self.assertEqual((restarted.delay, restarted.motion_speed, restarted.auto_judge), (7, .5, False))
            self.assertFalse(restarted.pause_on_review)
            self.assertEqual(len(read_report(restarted.root, old_id)["events"]), 2)
        finally:
            restarted.close()

    async def test_presets_inject_one_fault_and_finish_the_expected_recovery_path(self):
        for scenario, expected_skills in (
            ("normal", ["靠近方块", "闭合夹爪", "抬起方块", "放入托盘", "收回机械臂"]),
            ("miss", ["靠近方块", "闭合夹爪", "抬起方块", "重新抓取", "放入托盘", "收回机械臂"]),
            ("drop", ["靠近方块", "闭合夹爪", "抬起方块", "重新抓取", "放入托盘", "收回机械臂"]),
            ("shift", ["靠近方块", "闭合夹爪", "抬起方块", "放入托盘", "收回机械臂"]),
        ):
            with self.subTest(scenario=scenario):
                self.world.reset(scenario)
                for _ in range(10):
                    if not self.world.next_action():
                        break
                    await self.world.advance()
                self.assertEqual(self.world.phase, "complete")
                self.assertTrue(self.world.truth()["placed"])
                self.assertEqual([e["label"] for e in self.world.events if e["kind"] == "skill"], expected_skills)
                faults = [e for e in self.world.events if e["kind"] == "fault"]
                self.assertEqual(len(faults), 0 if scenario == "normal" else 1)
                if scenario == "drop":
                    self.assertEqual(faults[0]["pose"]["phase"], "dropped")
                    self.assertFalse(faults[0]["truth"]["grasped"])
                if scenario == "shift":
                    self.assertAlmostEqual(self.world.cube[0], -.17)

    async def test_manual_fault_satisfies_the_same_planned_fault_without_repeating_it(self):
        self.world.reset("miss")
        await self.world.inject("miss")
        while self.world.next_action():
            await self.world.advance()
        self.assertEqual(len([e for e in self.world.events if e["kind"] == "fault"]), 1)
        self.assertTrue(self.world.truth()["placed"])
        self.world.reset()
        self.assertEqual(self.world.scenario, "miss")
        self.assertFalse(self.world.scenario_fault_done)

    async def test_preset_automatic_playback_executes_the_fault_as_a_separate_step(self):
        self.world.reset("miss")
        await self.world.advance()
        self.assertEqual(self.world.next_action()["kind"], "fault")
        await self.controller.start(RobotPlayback(judge=False, delay=15))
        await self.wait_until(lambda: self.world.scenario_fault_done)
        self.controller.pause(RobotControl())
        await self.controller.auto_task
        self.assertEqual(self.world.phase, "hover")
        self.assertTrue(self.world.miss_next)
        self.assertEqual(self.world.events[-1]["kind"], "fault")
        self.assertEqual(self.world.next_action()["name"], "grasp")

    async def test_unknown_scenario_does_not_create_a_new_run(self):
        initial = self.world.id
        with self.assertRaises(HTTPException):
            self.world.reset("unknown")
        self.assertEqual(self.world.id, initial)

    async def test_full_demo_records_uncertain_and_disagreeing_checks_and_finishes(self):
        async def observe(_client):
            self.world.history.append({"revision": self.world.revision, "truth": self.world.truth(),
                                       "answers": {"placed": {"noul": .42}, "grasped": {"noul": .99}}})
            return self.world.history[-1]

        self.world.observe = observe
        old_id = self.world.id
        state = await self.controller.demo(RobotDemo(run_id=old_id, expected_revision=0, judge=True, delay=.5))
        self.assertNotEqual(state["id"], old_id)
        self.assertFalse(state["pause_on_review"])
        await asyncio.wait_for(self.controller.auto_task, timeout=4)
        self.assertEqual(self.world.phase, "complete")
        self.assertTrue(self.world.truth()["placed"])
        self.assertEqual(len(self.world.history), 5)
        self.assertEqual(self.world.pause_reason, "动作已完成")
        self.assertEqual([r["answers"]["placed"]["noul"] for r in self.world.history], [.42] * 5)

    async def test_full_demo_does_not_ignore_inference_errors_or_repeat_motion(self):
        async def observe(_client):
            raise OSError("model unavailable")

        self.world.observe = observe
        await self.controller.demo(RobotDemo(judge=True, delay=.5))
        await asyncio.wait_for(self.controller.auto_task, timeout=1)
        self.assertFalse(self.world.auto_running)
        self.assertEqual(self.world.phase, "hover")
        self.assertEqual(self.world.revision, 1)
        self.assertIn("model unavailable", self.world.error)

    async def test_full_demo_checks_preconditions_before_resetting_the_current_run(self):
        await self.world.execute()
        initial = self.world.snapshot()
        self.controller.backend.state = "offline"
        with self.assertRaises(HTTPException):
            await self.controller.demo(RobotDemo(judge=True))
        self.assertEqual(self.world.id, initial["id"])
        self.assertEqual(self.world.revision, initial["revision"])
        with self.assertRaises(HTTPException):
            await self.controller.demo(RobotDemo(run_id="outdated", judge=False))
        self.assertEqual(self.world.id, initial["id"])

    async def test_full_demo_can_restart_a_completed_case_and_rejects_a_second_start(self):
        self.world.reset("drop")
        while self.world.next_action():
            await self.world.advance()
        self.world.save()
        old_id = self.world.id
        state = await self.controller.demo(RobotDemo(scenario="miss", judge=False, delay=15))
        self.assertNotEqual(state["id"], old_id)
        self.assertEqual(state["scenario"]["key"], "miss")
        self.assertEqual(read_report(self.world.root, old_id)["phase"], "complete")
        with self.assertRaises(HTTPException):
            await self.controller.demo(RobotDemo(judge=False))
        self.assertEqual(self.world.id, state["id"])
        self.controller.pause(RobotControl())
        await self.controller.auto_task


if __name__ == "__main__":
    unittest.main()
