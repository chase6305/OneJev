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
from qev.robot import Kinematics, RobotWorld, RobotController, RobotControl, RobotPlayback, RobotDemo, RobotReplay, read_report, reconcile_playlists, process_owner

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


class PlaylistJournalTests(unittest.TestCase):
    def test_only_abandoned_active_journals_are_marked_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for key, owner, status in (("a", {"pid": 999999999}, "running"),
                                       ("b", process_owner(), "running"), ("c", {}, "completed")):
                path = root / "playlists" / (key * 32) / "report.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"id": key * 32, "owner": owner, "status": status, "index": 0,
                    "items": [{"status": status}], "next_case_at": 123.}))
            reconcile_playlists(root)
            interrupted = read_report(root / "playlists", "a" * 32)
            self.assertEqual(interrupted["status"], "interrupted")
            self.assertEqual(interrupted["previous_status"], "running")
            self.assertIsNone(interrupted["next_case_at"])
            self.assertEqual(read_report(root / "playlists", "b" * 32)["status"], "running")
            self.assertEqual(read_report(root / "playlists", "c" * 32)["status"], "completed")

    def test_zombie_process_is_not_mistaken_for_an_active_playlist_owner(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "playlists" / ("d" * 32) / "report.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"id": "d" * 32, "owner": {"pid": 424242, "started": "123"},
                "status": "running", "index": 0, "items": [{"status": "running"}]}))
            original = Path.read_text
            def read(file, *args, **kwargs):
                if str(file) == "/proc/424242/stat":
                    return "424242 (qev) " + " ".join(["Z"] + ["0"] * 18 + ["123"])
                return original(file, *args, **kwargs)
            with patch("qev.robot.os.kill"), patch.object(Path, "read_text", read):
                reconcile_playlists(root)
            self.assertEqual(read_report(root / "playlists", "d" * 32)["status"], "interrupted")


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

    async def test_playlist_runs_two_rounds_in_order_and_keeps_each_report(self):
        async def immediate_delay(_delay, **_kwargs):
            await asyncio.sleep(0)
        self.controller.wait_between = immediate_delay
        await self.controller.demo(RobotDemo(sequence="all", rounds=2, judge=False))
        await asyncio.wait_for(self.controller.auto_task, timeout=8)
        playlist = self.world.playlist
        self.assertEqual(playlist["status"], "completed")
        self.assertEqual([item["scenario"] for item in playlist["items"]], ["normal", "miss", "drop", "shift"] * 2)
        self.assertEqual(len({item["run_id"] for item in playlist["items"]}), 8)
        for item in playlist["items"]:
            self.assertEqual(item["status"], "completed")
            self.assertTrue(item["truth"]["placed"])
            saved = read_report(self.world.root, item["run_id"])
            self.assertEqual(saved["playlist_id"], playlist["id"])
            self.assertNotIn("playlist", saved)
        saved = read_report(self.world.root / "playlists", playlist["id"])
        self.assertEqual(saved["status"], "completed")

    async def test_pausing_between_cases_preserves_the_completed_case_until_resume(self):
        waiting, release = asyncio.Event(), asyncio.Event()
        async def wait(_delay, *, cases=False):
            if cases:
                waiting.set()
                await release.wait()
        self.controller.wait_between = wait
        await self.controller.demo(RobotDemo(sequence="all", judge=False))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        identifier = self.world.id
        self.controller.pause(RobotControl())
        async def reset(live):
            live.reset()
        with self.assertRaises(HTTPException):
            await self.controller.operate(reset, RobotControl())
        self.assertEqual(self.world.id, identifier)
        release.set()
        await self.controller.auto_task
        self.assertEqual(self.world.phase, "complete")
        self.assertEqual(self.world.id, identifier)
        self.assertEqual(self.world.playlist["index"], 0)
        self.assertIsNone(self.world.playlist["items"][1]["run_id"])
        await self.controller.start(RobotPlayback(judge=False))
        await asyncio.wait_for(self.controller.auto_task, timeout=5)
        self.assertEqual(self.world.playlist["status"], "completed")
        self.assertEqual(self.world.playlist["items"][0]["run_id"], identifier)

    async def test_transient_visual_failure_retries_without_repeating_the_action(self):
        calls = 0
        async def observe(_client):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary connection failure")
            self.world.history.append({"revision": self.world.revision, "truth": self.world.truth(),
                                       "answers": {"placed": {"noul": .01}, "grasped": {"noul": .01}}})
            self.controller.pause(RobotControl())
        self.world.observe = observe
        await self.controller.demo(RobotDemo(judge=True))
        await asyncio.wait_for(self.controller.auto_task, timeout=2)
        self.assertEqual(calls, 2)
        self.assertEqual(self.world.revision, 1)
        self.assertEqual(self.world.phase, "hover")
        self.assertEqual(len(self.world.observation_errors), 1)
        self.assertEqual(len(self.world.history), 1)
        self.assertFalse(self.world.pending_observation)

    async def test_resume_retries_failed_observation_before_advancing_scene(self):
        async def fail(_client):
            raise OSError("temporary connection failure")
        self.world.observe = fail
        await self.controller.demo(RobotDemo(judge=True))
        await asyncio.wait_for(self.controller.auto_task, timeout=2)
        original = self.world.pose()
        self.assertTrue(self.world.pending_observation)
        async def succeed(_client):
            self.world.history.append({"revision": self.world.revision, "truth": self.world.truth(),
                                       "answers": {"placed": {"noul": .01}, "grasped": {"noul": .01}}})
            self.controller.pause(RobotControl())
        self.world.observe = succeed
        await self.controller.start(RobotPlayback(judge=True))
        await asyncio.wait_for(self.controller.auto_task, timeout=1)
        self.assertEqual(self.world.pose(), original)
        self.assertEqual(self.world.revision, 1)
        self.assertFalse(self.world.pending_observation)

    async def test_stop_during_visual_retry_does_not_start_another_attempt_or_case(self):
        calls = 0
        async def fail(_client):
            nonlocal calls
            calls += 1
            raise OSError("temporary connection failure")
        self.world.observe = fail
        await self.controller.demo(RobotDemo(sequence="all", judge=True))
        await self.wait_until(lambda: self.world.retrying_observation)
        self.controller.stop(RobotControl())
        await self.controller.auto_task
        self.assertEqual(calls, 1)
        self.assertEqual(self.world.revision, 1)
        self.assertEqual(self.world.playlist["status"], "stopped")
        self.assertIsNone(self.world.playlist["items"][1]["run_id"])

    async def test_invalid_model_output_is_recorded_without_automatic_retry(self):
        calls = 0
        async def fail(_client):
            nonlocal calls
            calls += 1
            raise ValueError("invalid probability")
        self.world.observe = fail
        await self.controller.demo(RobotDemo(judge=True))
        await asyncio.wait_for(self.controller.auto_task, timeout=1)
        self.assertEqual(calls, 1)
        self.assertEqual(self.world.observation_errors[0]["message"], "invalid probability")

    async def test_playlist_token_can_pause_after_case_switch_but_cannot_stop_a_new_demo(self):
        waiting, release = asyncio.Event(), asyncio.Event()
        gaps = 0
        async def wait(_delay, *, cases=False):
            nonlocal gaps
            if cases:
                gaps += 1
                if gaps == 2:
                    waiting.set()
                    await release.wait()
        self.controller.wait_between = wait
        initial = await self.controller.demo(RobotDemo(sequence="all", judge=False))
        old = RobotControl(run_id=initial["id"], playlist_id=initial["playlist"]["id"])
        await asyncio.wait_for(waiting.wait(), timeout=2)
        self.assertNotEqual(self.world.id, old.run_id)
        self.controller.pause(old)
        release.set()
        await self.controller.auto_task
        current = await self.controller.demo(RobotDemo(run_id=self.world.id, playlist_id=old.playlist_id, judge=False))
        with self.assertRaises(HTTPException):
            self.controller.stop(old)
        self.assertEqual(self.world.id, current["id"])
        self.controller.pause(RobotControl())
        await self.controller.auto_task

    async def test_stopped_summary_and_replay_checkpoint_do_not_change_after_manual_actions(self):
        await self.controller.demo(RobotDemo(sequence="all", judge=False, delay=15))
        await self.wait_until(lambda: self.world.phase == "hover")
        self.controller.stop(RobotControl())
        await self.controller.auto_task
        saved = copy.deepcopy(self.world.playlist)
        checkpoint = saved["items"][0]["event_index"]
        await self.controller.operate(lambda w: w.execute(), RobotControl())
        self.assertEqual(self.world.phase, "grasped")
        self.assertEqual(self.world.playlist, saved)
        self.world.show_replay(self.world.snapshot(), event_index=checkpoint)
        self.assertEqual(self.world.replay["pose"]["phase"], "hover")

    async def test_stop_during_motion_records_the_settled_final_checkpoint(self):
        started, release = asyncio.Event(), asyncio.Event()
        move = self.world.move
        async def blocked_move(target, duration=1.2):
            started.set()
            await release.wait()
            await move(target, duration)
        self.world.move = blocked_move
        await self.controller.demo(RobotDemo(sequence="all", judge=False))
        await started.wait()
        self.controller.stop(RobotControl())
        release.set()
        await self.controller.auto_task
        item = self.world.playlist["items"][0]
        self.assertEqual(item["status"], "stopped")
        self.assertEqual(item["phase"], "hover")
        self.assertEqual(item["event_index"], 1)
        self.assertEqual(item["revision"], 1)


if __name__ == "__main__":
    unittest.main()
