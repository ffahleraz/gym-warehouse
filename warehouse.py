import time
import typing

import numpy as np
import gym
from ray.rllib.env.multi_agent_env import MultiAgentEnv
import Box2D
from Box2D.b2 import (
    world,
    circleShape,
    polygonShape,
    dynamicBody,
)

# Engineering notes:
#   - Each agent is identified by int[0, NUM_AGENTS) in string type.
#   - Zero coordinate for the env is on the bottom left, this is then transformed to
#     top left for rendering.
#   - Environment layout:
#       |B|x| | |x|x| | |x|x| | |x|x| | |x|B|
#   - States:
#       - self._waiting_pickup_point_target_idxs = np.zeros(NUM_PICKUP_POINTS, dtype=np.int32):
#           - -1: not waiting (inactive)
#           - [0, NUM_DELIVERY_POINTS): target delivery point idx
#       - self._waiting_pickup_point_remaining_times = np.zeros(NUM_PICKUP_POINTS, dtype=np.float32):
#           - -1.0: not waiting (inactive)
#           - [0.0, oo): elapsed time since active
#       - self._served_pickup_point_picker_agent_idxs = np.zeros(NUM_PICKUP_POINTS, dtype=np.int32):
#           - -1: not served or not waiting
#           - [0, NUM_AGENTS): picker agent idx
#       - self._served_pickup_point_target_idxs = np.zeros(NUM_PICKUP_POINTS, dtype=np.int32):
#           - -1: not served or not waiting
#           - [0, NUM_DELIVERY_POINTS): target delivery point idx
#       - self._served_pickup_point_remaining_times = np.zeros(NUM_PICKUP_POINTS, dtype=np.float32):
#           - -1.0: not served or not waiting
#           - [0.0, oo): elapsed time since served

# Environment
AREA_DIMENSION_M: float = 16.0
BORDER_WIDTH_M: float = 1.0
WORLD_DIMENSION_M: float = AREA_DIMENSION_M + 2 * BORDER_WIDTH_M
AGENT_RADIUS: float = 0.3
PICKUP_RACKS_ARRANGEMENT: typing.List[float] = [5.0, 9.0, 13.0]
FRAMES_PER_SECOND: int = 20

NUM_AGENTS: int = (len(PICKUP_RACKS_ARRANGEMENT) + 1) ** 2
NUM_PICKUP_POINTS: int = 4 * len(PICKUP_RACKS_ARRANGEMENT) ** 2
NUM_DELIVERY_POINTS: int = 4 * int(AREA_DIMENSION_M)
NUM_REQUESTS: int = 20

COLLISION_REWARD: float = -1.0
PICKUP_BASE_REWARD: float = 100.0
PICKUP_TIME_REWARD_MULTIPLIER: float = 10.0
DELIVERY_BASE_REWARD: float = 100.0
DELIVERY_TIME_REWARD_MULTIPLIER: float = 10.0

MAX_PICKUP_WAIT_TIME: float = 20.0 * FRAMES_PER_SECOND
MAX_DELIVERY_WAIT_TIME: float = 20.0 * FRAMES_PER_SECOND

AGENT_COLLISION_EPSILON: float = 0.05
PICKUP_POSITION_EPSILON: float = 5  # 0.3
DELIVERY_POSITION_EPSILON: float = 5  # 0.3

# Rendering
B2_VEL_ITERS: int = 10
B2_POS_ITERS: int = 10
PIXELS_PER_METER: int = 30
VIEWPORT_DIMENSION_PX: int = int(WORLD_DIMENSION_M) * PIXELS_PER_METER

AGENT_COLOR: typing.Tuple[float, float, float] = (0.0, 0.0, 0.0)
BORDER_COLOR: typing.Tuple[float, float, float] = (0.5, 0.5, 0.5)
PICKUP_POINT_COLOR: typing.Tuple[float, float, float] = (0.8, 0.8, 0.8)
DELIVERY_POINT_COLOR: typing.Tuple[float, float, float] = (0.8, 0.8, 0.8)


class Warehouse(MultiAgentEnv):
    def __init__(self) -> None:
        super(Warehouse, self).__init__()

        self.metadata = {
            "render.modes": ["human"],
            "video.frames_per_second": FRAMES_PER_SECOND,
        }
        self.reward_range = (-np.inf, -np.inf)
        self.action_space = gym.spaces.Box(low=-1.0, high=1.0, shape=(2,), dtype=np.float32)
        self.observation_space = gym.spaces.Dict(
            {
                "self_position": gym.spaces.Box(
                    low=BORDER_WIDTH_M,
                    high=WORLD_DIMENSION_M - BORDER_WIDTH_M,
                    shape=(2,),
                    dtype=np.float32,
                ),
                "self_availability": gym.spaces.MultiBinary(1),
                "self_delivery_target": gym.spaces.Box(
                    low=np.array([0.0, 0.0, 0.0]),
                    high=np.array([WORLD_DIMENSION_M, WORLD_DIMENSION_M, np.inf]),
                    dtype=np.float32,
                ),
                "other_positions": gym.spaces.Box(
                    low=BORDER_WIDTH_M,
                    high=WORLD_DIMENSION_M - BORDER_WIDTH_M,
                    shape=(NUM_AGENTS - 1, 2),
                    dtype=np.float32,
                ),
                "other_availabilities": gym.spaces.MultiBinary(NUM_AGENTS - 1),
                "other_delivery_targets": gym.spaces.Box(
                    low=np.repeat(
                        np.array([0.0, 0.0, 0.0])[np.newaxis, :,], NUM_AGENTS - 1, axis=0
                    ),
                    high=np.repeat(
                        np.array([WORLD_DIMENSION_M, WORLD_DIMENSION_M, MAX_DELIVERY_WAIT_TIME])[
                            np.newaxis, :,
                        ],
                        NUM_AGENTS - 1,
                        axis=0,
                    ),
                    dtype=np.float32,
                ),
                "requests": gym.spaces.Box(
                    low=np.repeat(
                        np.array([0.0, 0.0, 0.0, 0.0, 0.0])[np.newaxis, :,], NUM_REQUESTS, axis=0,
                    ),
                    high=np.repeat(
                        np.array(
                            [
                                WORLD_DIMENSION_M,
                                WORLD_DIMENSION_M,
                                WORLD_DIMENSION_M,
                                WORLD_DIMENSION_M,
                                MAX_PICKUP_WAIT_TIME,
                            ]
                        )[np.newaxis, :,],
                        NUM_REQUESTS,
                        axis=0,
                    ),
                    dtype=np.float32,
                ),
            }
        )

        self._viewer: gym.Viewer = None

        self._world = world(gravity=(0, 0), doSleep=False)
        self._agent_bodies: typing.List[dynamicBody] = []
        self._border_bodies: typing.List[dynamicBody] = []

        self._agent_positions = np.zeros((NUM_AGENTS, 2), dtype=np.float32)
        self._agent_availabilities = np.zeros(NUM_AGENTS, dtype=np.int8)

        self._pickup_point_positions = np.zeros((NUM_PICKUP_POINTS, 2), dtype=np.float32)
        self._delivery_point_positions = np.zeros((NUM_DELIVERY_POINTS, 2), dtype=np.float32)

        self._waiting_pickup_point_target_idxs = np.zeros(NUM_PICKUP_POINTS, dtype=np.int32)
        self._waiting_pickup_point_remaining_times = np.zeros(NUM_PICKUP_POINTS, dtype=np.float32)

        self._served_pickup_point_picker_agent_idxs = np.zeros(NUM_PICKUP_POINTS, dtype=np.int32)
        self._served_pickup_point_target_idxs = np.zeros(NUM_PICKUP_POINTS, dtype=np.int32)
        self._served_pickup_point_remaining_times = np.zeros(NUM_PICKUP_POINTS, dtype=np.float32)

    def reset(self) -> typing.Dict[str, gym.spaces.Dict]:
        # Init agents
        self._agent_bodies = []
        racks_diff = (PICKUP_RACKS_ARRANGEMENT[1] - PICKUP_RACKS_ARRANGEMENT[0]) / 2
        arrangement = [
            PICKUP_RACKS_ARRANGEMENT[0] - racks_diff,
            *[x + racks_diff for x in PICKUP_RACKS_ARRANGEMENT],
        ]
        agent_positions: typing.List[typing.List[float]] = []
        for x in arrangement:
            for y in arrangement:
                body = self._world.CreateDynamicBody(position=(x, y))
                _ = body.CreateCircleFixture(radius=AGENT_RADIUS, density=1.0, friction=0.0)
                self._agent_bodies.append(body)
                agent_positions.append([x, y])
        self._agent_positions = np.array(agent_positions, dtype=np.float32)
        self._agent_availabilities = np.ones(NUM_AGENTS, dtype=np.int8)

        # Init borders
        self._border_bodies = [
            self._world.CreateStaticBody(
                position=(WORLD_DIMENSION_M / 2, BORDER_WIDTH_M / 2),
                shapes=polygonShape(box=(WORLD_DIMENSION_M / 2, BORDER_WIDTH_M / 2)),
            ),
            self._world.CreateStaticBody(
                position=(WORLD_DIMENSION_M / 2, WORLD_DIMENSION_M - BORDER_WIDTH_M / 2,),
                shapes=polygonShape(box=(WORLD_DIMENSION_M / 2, BORDER_WIDTH_M / 2)),
            ),
            self._world.CreateStaticBody(
                position=(BORDER_WIDTH_M / 2, WORLD_DIMENSION_M / 2,),
                shapes=polygonShape(box=(BORDER_WIDTH_M / 2, WORLD_DIMENSION_M / 2)),
            ),
            self._world.CreateStaticBody(
                position=(WORLD_DIMENSION_M - BORDER_WIDTH_M / 2, WORLD_DIMENSION_M / 2,),
                shapes=polygonShape(box=(BORDER_WIDTH_M / 2, WORLD_DIMENSION_M / 2)),
            ),
        ]

        # Init pickup point positions
        pickup_point_positions = []
        for x in PICKUP_RACKS_ARRANGEMENT:
            for y in PICKUP_RACKS_ARRANGEMENT:
                pickup_point_positions.extend(
                    [
                        [x - 0.5, y - 0.5],
                        [x + 0.5, y - 0.5],
                        [x + 0.5, y + 0.5],
                        [x - 0.5, y + 0.5],
                    ]
                )
        self._pickup_point_positions = np.array(pickup_point_positions, dtype=np.float32)

        # Init delivery point positions
        delivery_point_positions = []
        for val in range(2, int(AREA_DIMENSION_M) - 2):
            delivery_point_positions.extend(
                [
                    [BORDER_WIDTH_M + val + 0.5, BORDER_WIDTH_M + 0.5],
                    [BORDER_WIDTH_M + val + 0.5, WORLD_DIMENSION_M - BORDER_WIDTH_M - 0.5,],
                    [BORDER_WIDTH_M + 0.5, BORDER_WIDTH_M + val + 0.5],
                    [WORLD_DIMENSION_M - BORDER_WIDTH_M - 0.5, BORDER_WIDTH_M + val + 0.5,],
                ]
            )
        self._delivery_point_positions = np.array(delivery_point_positions, dtype=np.float32)

        # Init waiting request states
        self._waiting_pickup_point_target_idxs = np.full(NUM_PICKUP_POINTS, -1, dtype=np.int32)
        self._waiting_pickup_point_remaining_times = np.full(
            NUM_PICKUP_POINTS, -1.0, dtype=np.float32
        )

        new_waiting_pickup_point_idxs = np.random.choice(
            NUM_PICKUP_POINTS, NUM_REQUESTS, replace=False,
        )
        self._waiting_pickup_point_target_idxs[new_waiting_pickup_point_idxs] = np.random.choice(
            NUM_DELIVERY_POINTS, NUM_REQUESTS, replace=False,
        )
        self._waiting_pickup_point_remaining_times[
            new_waiting_pickup_point_idxs
        ] = MAX_PICKUP_WAIT_TIME

        # Init served request states
        self._served_pickup_point_picker_agent_idxs = np.full(NUM_PICKUP_POINTS, -1, dtype=np.int32)
        self._served_pickup_point_target_idxs = np.full(NUM_PICKUP_POINTS, -1, dtype=np.int32)
        self._served_pickup_point_remaining_times = np.full(
            NUM_PICKUP_POINTS, -1.0, dtype=np.float32
        )

        return {
            str(i): {
                "self_position": self._agent_positions[i],
                "self_availability": self._agent_availabilities[np.newaxis, i],
                "self_delivery_target": None,
                "other_positions": np.delete(self._agent_positions, i, axis=0),
                "other_availabilities": np.delete(self._agent_availabilities, i, axis=0),
                "other_delivery_targets": None,
                "requests": None,
            }
            for i in range(NUM_AGENTS)
        }

    def step(
        self, action_dict: typing.Dict[str, np.ndarray]
    ) -> typing.Tuple[
        typing.Dict[str, gym.spaces.Dict],
        typing.Dict[str, float],
        typing.Dict[str, bool],
        typing.Dict[str, typing.Dict[str, str]],
    ]:
        # Update agent velocities
        for key, value in action_dict.items():
            self._agent_bodies[int(key)].linearVelocity = value.tolist()

        # Step simulation
        self._world.Step(1.0 / FRAMES_PER_SECOND, 10, 10)

        # Update agent positions
        for idx, body in enumerate(self._agent_bodies):
            self._agent_positions[idx][0] = body.position[0]
            self._agent_positions[idx][1] = body.position[1]

        # Detect agent each-other collisions and calculate rewards
        agent_eachother_distances = np.linalg.norm(
            np.repeat(self._agent_positions[:, np.newaxis, :], NUM_AGENTS, axis=1)
            - np.repeat(self._agent_positions[np.newaxis, :, :], NUM_AGENTS, axis=0),
            axis=2,
        )
        agent_eachother_collision_counts = (
            np.count_nonzero(
                agent_eachother_distances < 2 * AGENT_RADIUS + AGENT_COLLISION_EPSILON, axis=1
            )
            - 1
        )
        temp_rewards = agent_eachother_collision_counts * COLLISION_REWARD

        # Decrement timers
        self._waiting_pickup_point_remaining_times[
            self._waiting_pickup_point_target_idxs > -1
        ] -= 1.0
        self._served_pickup_point_remaining_times[self._served_pickup_point_target_idxs > -1] -= 1.0

        # Remove expired pickup and deliveries
        expired_waiting_pickup_point_idxs_slice = self._waiting_pickup_point_remaining_times <= 0.0
        self._waiting_pickup_point_target_idxs[expired_waiting_pickup_point_idxs_slice] = -1
        self._waiting_pickup_point_remaining_times[expired_waiting_pickup_point_idxs_slice] = -1.0

        expired_served_pickup_point_idxs_slice = self._served_pickup_point_remaining_times <= 0.0
        self._agent_availabilities[
            self._served_pickup_point_picker_agent_idxs[expired_served_pickup_point_idxs_slice]
        ] = 1

        self._served_pickup_point_picker_agent_idxs[expired_served_pickup_point_idxs_slice] = -1
        self._served_pickup_point_target_idxs[expired_served_pickup_point_idxs_slice] = -1
        self._served_pickup_point_remaining_times[expired_served_pickup_point_idxs_slice] = -1.0

        # Detect pickups
        agent_and_pickup_point_distances = np.linalg.norm(
            np.repeat(self._agent_positions[:, np.newaxis, :], NUM_PICKUP_POINTS, axis=1)
            - np.repeat(self._pickup_point_positions[np.newaxis, :, :], NUM_AGENTS, axis=0),
            axis=2,
        )
        waiting_pickup_point_idxs = np.where(self._waiting_pickup_point_target_idxs > -1.0)[0]
        picker_agent_idxs, valid_idxs_from_waiting_pickup_point_idxs = np.where(
            (agent_and_pickup_point_distances < PICKUP_POSITION_EPSILON)[
                :, waiting_pickup_point_idxs
            ]
        )
        new_served_pickup_point_idxs = waiting_pickup_point_idxs[
            valid_idxs_from_waiting_pickup_point_idxs
        ]

        self._served_pickup_point_target_idxs[
            new_served_pickup_point_idxs
        ] = self._waiting_pickup_point_target_idxs[new_served_pickup_point_idxs]
        self._served_pickup_point_picker_agent_idxs[
            new_served_pickup_point_idxs
        ] = picker_agent_idxs
        self._served_pickup_point_remaining_times[new_served_pickup_point_idxs] = 0.0
        self._agent_availabilities[picker_agent_idxs] = 0

        # Calculate pickup rewards
        temp_rewards[picker_agent_idxs] += (
            PICKUP_BASE_REWARD
            + self._waiting_pickup_point_remaining_times[new_served_pickup_point_idxs]
            * PICKUP_TIME_REWARD_MULTIPLIER
        )

        # Regenerate waiting pickup points
        self._waiting_pickup_point_target_idxs[new_served_pickup_point_idxs] = -1
        self._waiting_pickup_point_remaining_times[new_served_pickup_point_idxs] = -1.0

        inactive_pickup_point_idxs = np.where(self._waiting_pickup_point_target_idxs == -1)[0]
        new_waiting_pickup_point_idxs = np.random.choice(
            inactive_pickup_point_idxs,
            NUM_REQUESTS - NUM_PICKUP_POINTS + inactive_pickup_point_idxs.shape[0],
            replace=False,
        )
        self._waiting_pickup_point_remaining_times[
            new_waiting_pickup_point_idxs
        ] = MAX_PICKUP_WAIT_TIME

        new_waiting_pickup_point_target_idxs = np.random.choice(
            NUM_DELIVERY_POINTS,
            NUM_REQUESTS - NUM_PICKUP_POINTS + inactive_pickup_point_idxs.shape[0],
            replace=False,
        )
        self._waiting_pickup_point_target_idxs[
            new_waiting_pickup_point_idxs
        ] = new_waiting_pickup_point_target_idxs

        # Detect deliveries

        print(temp_rewards)

        observations = {
            str(i): {
                "self_position": self._agent_positions[i],
                "self_availability": self._agent_availabilities[np.newaxis, i],
                "self_delivery_target": None,
                "other_positions": np.delete(self._agent_positions, i, axis=0),
                "other_availabilities": np.delete(self._agent_availabilities, i, axis=0),
                "other_delivery_targets": None,
                "requests": None,
            }
            for i in range(NUM_AGENTS)
        }
        rewards = {f"{i}": 0.0 for i in range(NUM_AGENTS)}
        dones = {f"{i}": False for i in range(NUM_AGENTS)}
        dones["__all__"] = False
        infos = {f"{i}": {"test": "test"} for i in range(NUM_AGENTS)}
        return observations, rewards, dones, infos

    def render(self, mode: str = "human") -> None:
        from gym.envs.classic_control import rendering

        if mode != "human":
            super(Warehouse, self).render(mode=mode)

        if self._viewer is None:
            self._viewer = rendering.Viewer(VIEWPORT_DIMENSION_PX, VIEWPORT_DIMENSION_PX)

        for body in self._border_bodies:
            for fixture in body.fixtures:
                self._viewer.draw_polygon(
                    [fixture.body.transform * v * PIXELS_PER_METER for v in fixture.shape.vertices],
                    color=BORDER_COLOR,
                )

        for point in self._pickup_point_positions:
            self._viewer.draw_polygon(
                [
                    ((point[0] - 0.4) * PIXELS_PER_METER, (point[1] - 0.4) * PIXELS_PER_METER,),
                    ((point[0] + 0.4) * PIXELS_PER_METER, (point[1] - 0.4) * PIXELS_PER_METER,),
                    ((point[0] + 0.4) * PIXELS_PER_METER, (point[1] + 0.4) * PIXELS_PER_METER,),
                    ((point[0] - 0.4) * PIXELS_PER_METER, (point[1] + 0.4) * PIXELS_PER_METER,),
                ],
                color=PICKUP_POINT_COLOR,
            )

        for point in self._delivery_point_positions:
            self._viewer.draw_polygon(
                [
                    ((point[0] - 0.4) * PIXELS_PER_METER, (point[1] - 0.4) * PIXELS_PER_METER,),
                    ((point[0] + 0.4) * PIXELS_PER_METER, (point[1] - 0.4) * PIXELS_PER_METER,),
                    ((point[0] + 0.4) * PIXELS_PER_METER, (point[1] + 0.4) * PIXELS_PER_METER,),
                    ((point[0] - 0.4) * PIXELS_PER_METER, (point[1] + 0.4) * PIXELS_PER_METER,),
                ],
                color=DELIVERY_POINT_COLOR,
            )

        for body in self._agent_bodies:
            for fixture in body.fixtures:
                self._viewer.draw_circle(
                    fixture.shape.radius * PIXELS_PER_METER, 30, color=AGENT_COLOR
                ).add_attr(
                    rendering.Transform(
                        translation=fixture.body.transform * fixture.shape.pos * PIXELS_PER_METER
                    )
                )

        self._viewer.render()

    def close(self) -> None:
        pass
