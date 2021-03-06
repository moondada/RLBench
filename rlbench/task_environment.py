import logging
from typing import List, Callable

import numpy as np
from pyquaternion import Quaternion
from pyrep import PyRep
from pyrep.errors import IKError

from rlbench import utils
from rlbench.action_modes import ArmActionMode, ActionMode
from rlbench.backend.exceptions import BoundaryError, WaypointError
from rlbench.backend.observation import Observation
from rlbench.backend.robot import Robot
from rlbench.backend.scene import Scene
from rlbench.backend.task import Task
from rlbench.demo import Demo
from rlbench.observation_config import ObservationConfig

_TORQUE_MAX_VEL = 9999
_DT = 0.05
_MAX_RESET_ATTEMPTS = 40
_MAX_DEMO_ATTEMPTS = 10


class InvalidActionError(Exception):
    pass


class TaskEnvironmentError(Exception):
    pass


class TaskEnvironment(object):

    def __init__(self, pyrep: PyRep, robot: Robot, scene: Scene, task: Task,
                 action_mode: ActionMode, dataset_root: str,
                 obs_config: ObservationConfig,
                 static_positions: bool = False):
        self._pyrep = pyrep
        self._robot = robot
        self._scene = scene
        self._task = task
        self._variation_number = 0
        self._action_mode = action_mode
        self._dataset_root = dataset_root
        self._obs_config = obs_config
        self._static_positions = static_positions
        self._reset_called = False
        self._prev_ee_velocity = None

        self._scene.load(self._task)
        self._pyrep.start()

    def get_name(self) -> str:
        return self._task.get_name()

    def sample_variation(self) -> int:
        self._variation_number = np.random.randint(
            0, self._task.variation_count())
        return self._variation_number

    def set_variation(self, v: int) -> None:
        if v >= self.variation_count():
            raise TaskEnvironmentError(
                'Requested variation %d, but there are only %d variations.' % (
                    v, self.variation_count()))
        self._variation_number = v

    def variation_count(self) -> int:
        return self._task.variation_count()

    def reset(self) -> (List[str], Observation):
        logging.info('Resetting task: %s' % self._task.get_name())

        self._scene.reset()
        try:
            desc = self._scene.init_episode(
                self._variation_number, max_attempts=_MAX_RESET_ATTEMPTS,
                randomly_place=not self._static_positions)
        except (BoundaryError, WaypointError) as e:
            raise TaskEnvironmentError(
                'Could not place the task %s in the scene. This should not '
                'happen, please raise an issues on this task.'
                % self._task.get_name()) from e

        ctr_loop = self._robot.arm.joints[0].is_control_loop_enabled()
        locked = self._robot.arm.joints[0].is_motor_locked_at_zero_velocity()
        self._robot.arm.set_control_loop_enabled(False)
        self._robot.arm.set_motor_locked_at_zero_velocity(True)

        self._reset_called = True

        self._robot.arm.set_control_loop_enabled(ctr_loop)
        self._robot.arm.set_motor_locked_at_zero_velocity(locked)

        # Returns a list of descriptions and the first observation
        return desc, self._scene.get_observation()

    def get_observation(self) -> Observation:
        return self._scene.get_observation()

    def _assert_action_space(self, action, expected_shape):
        if np.shape(action) != expected_shape:
            raise RuntimeError(
                'Expected the action shape to be: %s, but was shape: %s' % (
                    str(expected_shape), str(np.shape(action))))

    def _assert_unit_quaternion(self, quat):
        if not np.isclose(np.linalg.norm(quat), 1.0):
            raise RuntimeError('Action contained non unit quaternion!')

    def _torque_action(self, action):
        self._robot.arm.set_joint_target_velocities(
            [(_TORQUE_MAX_VEL if t < 0 else -_TORQUE_MAX_VEL)
             for t in action])
        self._robot.arm.set_joint_forces(np.abs(action))

    def _ee_action(self, action):
        self._assert_unit_quaternion(action[3:])
        try:
            joint_positions = self._robot.arm.solve_ik(
                action[:3], quaternion=action[3:])
            self._robot.arm.set_joint_target_positions(joint_positions)
        except IKError as e:
            raise InvalidActionError('Could not find a path.') from e
        done = False
        while not done:
            self._scene.step()
            done = np.allclose(self._robot.arm.get_joint_positions(),
                               joint_positions, atol=0.01)

    def _path_action(self, action):
        self._assert_unit_quaternion(action[3:])
        try:

            # Check if the target is in the workspace; if not, then quick reject
            # Only checks position, not rotation
            valid = self._scene.check_target_in_workspace(action[:3])
            if not valid:
                raise InvalidActionError('Target is outside of workspace.')

            path = self._robot.arm.get_path(
                action[:3], quaternion=action[3:], ignore_collisions=True)
            done = False
            observations = []
            while not done:
                done = path.step()
                self._scene.step()
                observations.append(self._scene.get_observation())
            return observations
        except IKError as e:
            raise InvalidActionError('Could not find a path.') from e

    def step(self, action) -> (Observation, int, bool):
        # returns observation, reward, done, info
        if not self._reset_called:
            raise RuntimeError(
                "Call 'reset' before calling 'step' on a task.")

        # action should contain 1 extra value for gripper open close state
        arm_action = np.array(action[:-1])
        ee_action = action[-1]

        if 0.0 > ee_action > 1.0:
            raise ValueError('Gripper action expected to be within 0 and 1.')

        # Discretize the gripper action
        current_ee = (1.0 if self._robot.gripper.get_open_amount()[0] > 0.9
                      else 0.0)

        if ee_action > 0.5:
            ee_action = 1.0
        elif ee_action < 0.5:
            ee_action = 0.0

        if self._action_mode.arm == ArmActionMode.ABS_JOINT_VELOCITY:

            self._assert_action_space(arm_action,
                                      (len(self._robot.arm.joints),))
            self._robot.arm.set_joint_target_velocities(arm_action)
            self._scene.step()

        elif self._action_mode.arm == ArmActionMode.DELTA_JOINT_VELOCITY:

            self._assert_action_space(arm_action,
                                      (len(self._robot.arm.joints),))
            cur = np.array(self._robot.arm.get_joint_velocities())
            self._robot.arm.set_joint_target_velocities(cur + arm_action)
            self._scene.step()

        elif self._action_mode.arm == ArmActionMode.ABS_JOINT_POSITION:

            self._assert_action_space(arm_action,
                                      (len(self._robot.arm.joints),))
            self._robot.arm.set_joint_target_positions(arm_action)
            self._scene.step()

        elif self._action_mode.arm == ArmActionMode.DELTA_JOINT_POSITION:

            self._assert_action_space(arm_action,
                                      (len(self._robot.arm.joints),))
            cur = np.array(self._robot.arm.get_joint_positions())
            self._robot.arm.set_joint_target_positions(cur + arm_action)
            self._scene.step()

        elif self._action_mode.arm == ArmActionMode.ABS_EE_POSE:

            self._assert_action_space(arm_action, (7,))
            self._ee_action(list(arm_action))

        elif self._action_mode.arm == ArmActionMode.ABS_EE_POSE_PLAN:

            self._assert_action_space(arm_action, (7,))
            self._path_observations = []
            self._path_observations = self._path_action(list(arm_action))

        elif self._action_mode.arm == ArmActionMode.DELTA_EE_POSE_PLAN:

            self._assert_action_space(arm_action, (7,))
            a_x, a_y, a_z, a_qx, a_qy, a_qz, a_qw = arm_action
            x, y, z, qx, qy, qz, qw = self._robot.arm.get_tip().get_pose()
            new_rot = Quaternion(a_qw, a_qx, a_qy, a_qz) * Quaternion(qw, qx,
                                                                      qy, qz)
            qw, qx, qy, qz = list(new_rot)
            new_pose = [a_x + x, a_y + y, a_z + z] + [qx, qy, qz, qw]
            self._path_observations = []
            self._path_observations = self._path_action(list(new_pose))

        elif self._action_mode.arm == ArmActionMode.DELTA_EE_POSE:

            self._assert_action_space(arm_action, (7,))
            a_x, a_y, a_z, a_qx, a_qy, a_qz, a_qw = arm_action
            x, y, z, qx, qy, qz, qw = self._robot.arm.get_tip().get_pose()
            new_rot = Quaternion(a_qw, a_qx, a_qy, a_qz) * Quaternion(
                qw, qx, qy, qz)
            qw, qx, qy, qz = list(new_rot)
            new_pose = [a_x + x, a_y + y, a_z + z] + [qx, qy, qz, qw]
            self._ee_action(list(new_pose))

        elif self._action_mode.arm == ArmActionMode.ABS_EE_VELOCITY:

            self._assert_action_space(arm_action, (7,))
            pose = self._robot.arm.get_tip().get_pose()
            new_pos = np.array(pose) + (arm_action * _DT)
            self._ee_action(list(new_pos))

        elif self._action_mode.arm == ArmActionMode.DELTA_EE_VELOCITY:

            self._assert_action_space(arm_action, (7,))
            if self._prev_ee_velocity is None:
                self._prev_ee_velocity = np.zeros((7,))
            self._prev_ee_velocity += arm_action
            pose = self._robot.arm.get_tip().get_pose()
            pose = np.array(pose)
            new_pose = pose + (self._prev_ee_velocity * _DT)
            self._ee_action(list(new_pose))

        elif self._action_mode.arm == ArmActionMode.ABS_JOINT_TORQUE:

            self._assert_action_space(
                arm_action, (len(self._robot.arm.joints),))
            self._torque_action(arm_action)
            self._scene.step()

        elif self._action_mode.arm == ArmActionMode.DELTA_JOINT_TORQUE:

            cur = np.array(self._robot.arm.get_joint_forces())
            new_action = cur + arm_action
            self._torque_action(new_action)
            self._scene.step()

        else:
            raise RuntimeError('Unrecognised action mode.')

        if current_ee != ee_action:
            done = False
            while not done:
                done = self._robot.gripper.actuate(ee_action, velocity=0.2)
                self._pyrep.step()
                self._task.step()
            if ee_action == 0.0:
                # If gripper close action, the check for grasp.
                for g_obj in self._task.get_graspable_objects():
                    self._robot.gripper.grasp(g_obj)
            else:
                # If gripper open action, the check for ungrasp.
                self._robot.gripper.release()

        success, terminate = self._task.success()
        return self._scene.get_observation(), int(success), terminate

    def get_path_observations(self):
        if (self._action_mode.arm != ArmActionMode.DELTA_EE_POSE_PLAN and
                self._action_mode.arm != ArmActionMode.ABS_EE_POSE_PLAN):
            raise RuntimeError('Only available in DELTA_EE_POSE_PLAN or '
                               'ABS_EE_POSE_PLAN action mode.')
        return self._path_observations

    def get_demos(self, amount: int, live_demos: bool = False,
                  image_paths: bool = False,
                  callable_each_step: Callable[[Observation], None] = None,
                  max_attempts: int = _MAX_DEMO_ATTEMPTS,
                  ) -> List[Demo]:
        """Negative means all demos"""

        if not live_demos and (self._dataset_root is None
                       or len(self._dataset_root) == 0):
            raise RuntimeError(
                "Can't ask for a stored demo when no dataset root provided.")

        if not live_demos:
            if self._dataset_root is None or len(self._dataset_root) == 0:
                raise RuntimeError(
                    "Can't ask for stored demo when no dataset root provided.")
            demos = utils.get_stored_demos(
                amount, image_paths, self._dataset_root, self._variation_number,
                self._task.get_name(), self._obs_config)
        else:
            ctr_loop = self._robot.arm.joints[0].is_control_loop_enabled()
            self._robot.arm.joints[0].set_control_loop_enabled(True)
            demos = self._get_live_demos(
                amount, callable_each_step, max_attempts)
            self._robot.arm.joints[0].set_control_loop_enabled(ctr_loop)
        return demos

    def _get_live_demos(self, amount: int,
                        callable_each_step: Callable[
                            [Observation], None] = None,
                        max_attempts: int = _MAX_DEMO_ATTEMPTS) -> List[Demo]:
        demos = []
        for i in range(amount):
            attempts = max_attempts
            while attempts > 0:
                random_seed = np.random.get_state()
                self.reset()
                logging.info('Collecting demo %d' % i)
                try:
                    demo = self._scene.get_demo(
                        callable_each_step=callable_each_step)
                    demo.random_seed = random_seed
                    demos.append(demo)
                    break
                except Exception as e:
                    attempts -= 1
                    logging.info('Bad demo. ' + str(e))
            if attempts <= 0:
                raise RuntimeError(
                    'Could not collect demos. Maybe a problem with the task?')
        return demos

    def reset_to_demo(self, demo: Demo) -> None:
        demo.restore_state()
        self.reset()
