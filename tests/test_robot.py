"""Kinematic correctness and fault recovery; optional robot dependencies only."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from qev.robot import Kinematics, RobotWorld

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

    async def asyncTearDown(self):
        self.world.close()
        self.temp.cleanup()

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


if __name__ == "__main__":
    unittest.main()
