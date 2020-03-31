import time
from typing import List, Dict, Tuple

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

__all__ = ["WarehouseContinuous"]


# Engineering notes:
#   - Each agent is identified by int[0, NUM_AGENTS) in string type.
#   - Zero coordinate for the env is on the bottom left, this is then transformed to
#     top left for rendering.
#   - Environment layout:
#       |B|x| | |x|x| | |x|x| | |x|x| | |x|B|
#   - States:
#       - self._agent_delivery_targets = np.zeros(NUM_AGENTS, dtype=np.int32):
#           - -1: not delivering
#           - [0, NUM_DELIVERY_POINTS): target delivery point idx
#       - self._pickup_point_targets = np.zeros(NUM_PICKUP_POINTS, dtype=np.int32):
#           - -1: not waiting (inactive)
#           - [0, NUM_DELIVERY_POINTS): target delivery point idx
#       - self._pickup_point_timers = np.zeros(NUM_PICKUP_POINTS, dtype=np.float32):
#           - -1.0: not waiting (inactive)
#           - [0.0, oo): elapsed time since active
#   - Game mechanism:
#       - There will always be NUM_REQUESTS requests for pickups in which all the pickup points are
#         unique but the delivery points may be not
#       - On each pickup, a new pickup request will be created with a random pickup point (different
#         from the existing requests, but may be the same as a pickup point that is already being
#         served) and a random delivery point (may be the same as existing requests or a delivery
#         point that is already being served)
#   - Constant Assumptions:
#       - NUM_REQUESTS >= NUM_AGENTS

# Environment
AREA_DIMENSION_M: float = 8.0
BORDER_WIDTH_M: float = 1.0
WORLD_DIMENSION_M: float = AREA_DIMENSION_M + 2 * BORDER_WIDTH_M
AGENT_RADIUS: float = 0.4
PICKUP_RACKS_ARRANGEMENT: List[float] = [5.0]
FRAMES_PER_SECOND: int = 10

NUM_AGENTS: int = 2
AGENT_INITIAL_POSITIONS: List[Tuple[float, float]] = [
    (3.0, 3.0),
    (WORLD_DIMENSION_M - 3.0, WORLD_DIMENSION_M - 3.0),
]
NUM_PICKUP_POINTS: int = 4 * len(PICKUP_RACKS_ARRANGEMENT) ** 2
NUM_DELIVERY_POINTS: int = 4 * int(AREA_DIMENSION_M - 4)
NUM_REQUESTS: int = 2

PICKUP_REWARD: float = 1.0
DELIVERY_REWARD: float = 1.0

MAX_EPISODE_TIME: int = 80 * FRAMES_PER_SECOND
MAX_PICKUP_WAIT_TIME: float = 40.0 * FRAMES_PER_SECOND

PICKUP_POSITION_TOLERANCE: float = 0.3
DELIVERY_POSITION_TOLERANCE: float = 0.3

# Rendering
B2_VEL_ITERS: int = 10
B2_POS_ITERS: int = 10
PIXELS_PER_METER: int = 30
VIEWPORT_DIMENSION_PX: int = int(WORLD_DIMENSION_M) * PIXELS_PER_METER

AGENT_COLORS: List[Tuple[float, float, float]] = [
    (0.5, 0.5, 0.5),
    (0.8, 0.8, 0.8),
    (0.0, 0.0, 1.0),
]
BORDER_COLOR: Tuple[float, float, float] = (0.5, 0.5, 0.5)
PICKUP_POINT_COLORS: List[Tuple[float, float, float]] = [
    (0.8, 0.8, 0.8),
    (0.0, 0.0, 1.0),
]
DELIVERY_POINT_COLORS: List[Tuple[float, float, float]] = [
    (0.8, 0.8, 0.8),
    (0.0, 0.0, 1.0),
]


class WarehouseContinuous(MultiAgentEnv):
    metadata = {
        "render.modes": ["human"],
        "video.frames_per_second": FRAMES_PER_SECOND,
    }
    reward_range = (0.0, 1.0)

    action_space = gym.spaces.Box(low=-1.0, high=1.0, shape=(2,), dtype=np.float32)
    observation_space = gym.spaces.Dict(
        {
            "self_position": gym.spaces.Box(
                low=BORDER_WIDTH_M,
                high=WORLD_DIMENSION_M - BORDER_WIDTH_M,
                shape=(2,),
                dtype=np.float32,
            ),
            "self_availability": gym.spaces.MultiBinary(1),
            "self_delivery_target": gym.spaces.Box(
                low=0.0, high=WORLD_DIMENSION_M, shape=(2,), dtype=np.float32,
            ),
            "other_positions": gym.spaces.Box(
                low=BORDER_WIDTH_M,
                high=WORLD_DIMENSION_M - BORDER_WIDTH_M,
                shape=(NUM_AGENTS - 1, 2),
                dtype=np.float32,
            ),
            "other_availabilities": gym.spaces.MultiBinary(NUM_AGENTS - 1),
            "other_delivery_targets": gym.spaces.Box(
                low=0.0, high=WORLD_DIMENSION_M, shape=(NUM_AGENTS - 1, 2), dtype=np.float32,
            ),
            "requests": gym.spaces.Box(
                low=0.0, high=WORLD_DIMENSION_M, shape=(NUM_REQUESTS, 4), dtype=np.float32,
            ),
        }
    )

    def __init__(self) -> None:
        super(WarehouseContinuous, self).__init__()

        self._viewer: gym.Viewer = None

        self._world = world(gravity=(0, 0), doSleep=False)
        self._agent_bodies: List[dynamicBody] = []
        self._border_bodies: List[dynamicBody] = []

        self._agent_positions = np.zeros((NUM_AGENTS, 2), dtype=np.float32)
        self._agent_delivery_targets = np.zeros(NUM_AGENTS, dtype=np.int32)

        self._delivery_point_positions = np.zeros((NUM_DELIVERY_POINTS, 2), dtype=np.float32)
        self._pickup_point_positions = np.zeros((NUM_PICKUP_POINTS, 2), dtype=np.float32)
        self._pickup_point_targets = np.zeros(NUM_PICKUP_POINTS, dtype=np.int32)
        self._pickup_point_timers = np.zeros(NUM_PICKUP_POINTS, dtype=np.float32)

        self._episode_time: int = 0

    def reset(self) -> Dict[str, gym.spaces.Dict]:
        self._episode_time = 0

        # Init agents
        agent_positions: List[List[float]] = []
        self._agent_bodies = []
        for x, y in AGENT_INITIAL_POSITIONS:
            body = self._world.CreateDynamicBody(position=(x, y))
            _ = body.CreateCircleFixture(radius=AGENT_RADIUS, density=1.0, friction=0.0)
            self._agent_bodies.append(body)
            agent_positions.append([x, y])

        self._agent_positions = np.array(agent_positions, dtype=np.float32)
        self._agent_delivery_targets = np.full(NUM_AGENTS, -1, dtype=np.int32)

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
        self._pickup_point_targets = np.full(NUM_PICKUP_POINTS, -1, dtype=np.int32)
        self._pickup_point_timers = np.full(NUM_PICKUP_POINTS, -1.0, dtype=np.float32)

        new_waiting_pickup_points = np.random.choice(
            NUM_PICKUP_POINTS, NUM_REQUESTS, replace=False,
        )
        self._pickup_point_targets[new_waiting_pickup_points] = np.random.choice(
            NUM_DELIVERY_POINTS, NUM_REQUESTS, replace=False,
        )
        self._pickup_point_timers[new_waiting_pickup_points] = MAX_PICKUP_WAIT_TIME

        # Compute observations
        agent_availabilities = np.ones(NUM_AGENTS, dtype=np.int32)
        agent_delivery_target_positions = np.zeros((NUM_AGENTS, 2), dtype=np.float32)

        waiting_pickup_points_mask = self._pickup_point_targets > -1
        requests = self._pickup_point_positions[waiting_pickup_points_mask]
        requests = np.hstack(
            (
                requests,
                self._delivery_point_positions[
                    self._pickup_point_targets[waiting_pickup_points_mask]
                ],
            ),
        )

        return {
            str(i): {
                "self_position": self._agent_positions[i],
                "self_availability": agent_availabilities[np.newaxis, i],
                "self_delivery_target": agent_delivery_target_positions[i],
                "other_positions": np.delete(self._agent_positions, i, axis=0),
                "other_availabilities": np.delete(agent_availabilities, i, axis=0),
                "other_delivery_targets": np.delete(agent_delivery_target_positions, i, axis=0),
                "requests": requests,
            }
            for i in range(NUM_AGENTS)
        }

    def step(
        self, action_dict: Dict[str, np.ndarray]
    ) -> Tuple[
        Dict[str, gym.spaces.Dict], Dict[str, float], Dict[str, bool], Dict[str, Dict[str, str]],
    ]:
        self._episode_time += 1

        # Update agent velocities
        for key, value in action_dict.items():
            self._agent_bodies[int(key)].linearVelocity = value.tolist()

        # Step simulation
        self._world.Step(1.0 / FRAMES_PER_SECOND, 10, 10)

        # Update agent positions
        for idx, body in enumerate(self._agent_bodies):
            self._agent_positions[idx][0] = body.position[0]
            self._agent_positions[idx][1] = body.position[1]

        # Remove expired pickup points
        self._pickup_point_timers[self._pickup_point_targets > -1] -= 1.0
        expired_waiting_pickup_points_mask = self._pickup_point_timers == 0.0
        self._pickup_point_targets[expired_waiting_pickup_points_mask] = -1
        self._pickup_point_timers[expired_waiting_pickup_points_mask] = -1.0

        # Detect pickups
        agent_and_pickup_point_distances = np.linalg.norm(
            np.repeat(self._pickup_point_positions[np.newaxis, :, :], NUM_AGENTS, axis=0)
            - np.repeat(self._agent_positions[:, np.newaxis, :], NUM_PICKUP_POINTS, axis=1),
            axis=2,
        )
        agent_and_pickup_point_collisions = (
            agent_and_pickup_point_distances < PICKUP_POSITION_TOLERANCE
        )
        new_served_pickup_point_candidates = np.argmax(agent_and_pickup_point_collisions, axis=1)
        new_delivering_agents_mask = (
            np.max(agent_and_pickup_point_collisions, axis=1)
            & (self._agent_delivery_targets == -1)
            & (self._pickup_point_targets[new_served_pickup_point_candidates] > -1)
        )
        new_served_pickup_points = new_served_pickup_point_candidates[new_delivering_agents_mask]

        self._agent_delivery_targets[new_delivering_agents_mask] = self._pickup_point_targets[
            new_served_pickup_points
        ]
        self._pickup_point_targets[new_served_pickup_points] = -1
        self._pickup_point_timers[new_served_pickup_points] = -1.0

        # Calculate pickup rewards
        agent_rewards = np.zeros(NUM_AGENTS, dtype=np.float32)
        agent_rewards[new_delivering_agents_mask] += PICKUP_REWARD

        # Regenerate waiting pickup points
        inactive_pickup_points = np.where(self._pickup_point_targets == -1)[0]
        new_waiting_pickup_points = np.random.choice(
            inactive_pickup_points,
            NUM_REQUESTS - NUM_PICKUP_POINTS + inactive_pickup_points.shape[0],
            replace=False,
        )
        self._pickup_point_timers[new_waiting_pickup_points] = MAX_PICKUP_WAIT_TIME

        new_pickup_point_targets = np.random.choice(
            NUM_DELIVERY_POINTS,
            NUM_REQUESTS - NUM_PICKUP_POINTS + inactive_pickup_points.shape[0],
            replace=False,
        )
        self._pickup_point_targets[new_waiting_pickup_points] = new_pickup_point_targets

        # Detect deliveries
        delivering_agents = np.where(self._agent_delivery_targets > -1)[0]
        delivering_agents_completion = (
            np.linalg.norm(
                self._delivery_point_positions[self._agent_delivery_targets[delivering_agents]]
                - self._agent_positions[delivering_agents],
                axis=1,
            )
            < DELIVERY_POSITION_TOLERANCE
        )
        delivered_agents = delivering_agents[delivering_agents_completion]

        self._agent_delivery_targets[delivered_agents] = -1

        # Calculate delivery rewards
        agent_rewards[delivered_agents] += DELIVERY_REWARD

        # Compute observations
        delivering_agents_mask = self._agent_delivery_targets > -1
        agent_availabilities = np.ones(NUM_AGENTS, dtype=np.int32)
        agent_availabilities[delivering_agents_mask] = 0

        agent_delivery_target_positions = np.zeros((NUM_AGENTS, 2), dtype=np.float32)
        agent_delivery_target_positions[delivering_agents_mask] = self._delivery_point_positions[
            self._agent_delivery_targets[delivering_agents_mask]
        ]

        waiting_pickup_points_mask = self._pickup_point_targets > -1
        requests = self._pickup_point_positions[waiting_pickup_points_mask]
        requests = np.hstack(
            (
                requests,
                self._delivery_point_positions[
                    self._pickup_point_targets[waiting_pickup_points_mask]
                ],
            ),
        )

        observations = {
            str(i): {
                "self_position": self._agent_positions[i],
                "self_availability": agent_availabilities[np.newaxis, i],
                "self_delivery_target": agent_delivery_target_positions[i],
                "other_positions": np.delete(self._agent_positions, i, axis=0),
                "other_availabilities": np.delete(agent_availabilities, i, axis=0),
                "other_delivery_targets": np.delete(agent_delivery_target_positions, 1, axis=0),
                "requests": requests,
            }
            for i in range(NUM_AGENTS)
        }

        # Compute rewards
        rewards = {f"{i}": agent_rewards[i] for i in range(NUM_AGENTS)}

        # Compute dones
        episode_done = self._episode_time >= MAX_EPISODE_TIME
        dones = {f"{i}": episode_done for i in range(NUM_AGENTS)}
        dones["__all__"] = episode_done

        return observations, rewards, dones, {f"{i}": {} for i in range(NUM_AGENTS)}

    def render(self, mode: str = "human") -> None:
        from gym.envs.classic_control import rendering

        if mode != "human":
            super(WarehouseContinuous, self).render(mode=mode)

        if self._viewer is None:
            self._viewer = rendering.Viewer(VIEWPORT_DIMENSION_PX, VIEWPORT_DIMENSION_PX)

        for body in self._border_bodies:
            for fixture in body.fixtures:
                self._viewer.draw_polygon(
                    [fixture.body.transform * v * PIXELS_PER_METER for v in fixture.shape.vertices],
                    color=BORDER_COLOR,
                )

        for idx, point in enumerate(self._pickup_point_positions):
            color = (
                PICKUP_POINT_COLORS[1]
                if self._pickup_point_targets[idx] > -1
                else PICKUP_POINT_COLORS[0]
            )
            self._viewer.draw_polygon(
                [
                    ((point[0] - 0.4) * PIXELS_PER_METER, (point[1] - 0.4) * PIXELS_PER_METER,),
                    ((point[0] + 0.4) * PIXELS_PER_METER, (point[1] - 0.4) * PIXELS_PER_METER,),
                    ((point[0] + 0.4) * PIXELS_PER_METER, (point[1] + 0.4) * PIXELS_PER_METER,),
                    ((point[0] - 0.4) * PIXELS_PER_METER, (point[1] + 0.4) * PIXELS_PER_METER,),
                ],
                color=color,
            )

        for idx, point in enumerate(self._delivery_point_positions):
            color = (
                DELIVERY_POINT_COLORS[1]
                if np.isin(idx, self._agent_delivery_targets)
                else DELIVERY_POINT_COLORS[0]
            )
            self._viewer.draw_polygon(
                [
                    ((point[0] - 0.4) * PIXELS_PER_METER, (point[1] - 0.4) * PIXELS_PER_METER,),
                    ((point[0] + 0.4) * PIXELS_PER_METER, (point[1] - 0.4) * PIXELS_PER_METER,),
                    ((point[0] + 0.4) * PIXELS_PER_METER, (point[1] + 0.4) * PIXELS_PER_METER,),
                    ((point[0] - 0.4) * PIXELS_PER_METER, (point[1] + 0.4) * PIXELS_PER_METER,),
                ],
                color=color,
            )
            self._viewer.draw_polygon(
                [
                    ((point[0] - 0.3) * PIXELS_PER_METER, (point[1] - 0.3) * PIXELS_PER_METER,),
                    ((point[0] + 0.3) * PIXELS_PER_METER, (point[1] - 0.3) * PIXELS_PER_METER,),
                    ((point[0] + 0.3) * PIXELS_PER_METER, (point[1] + 0.3) * PIXELS_PER_METER,),
                    ((point[0] - 0.3) * PIXELS_PER_METER, (point[1] + 0.3) * PIXELS_PER_METER,),
                ],
                color=DELIVERY_POINT_COLORS[0],
            )

        for idx, body in enumerate(self._agent_bodies):
            for fixture in body.fixtures:
                self._viewer.draw_circle(
                    fixture.shape.radius * PIXELS_PER_METER, 30, color=AGENT_COLORS[0]
                ).add_attr(
                    rendering.Transform(
                        translation=fixture.body.transform * fixture.shape.pos * PIXELS_PER_METER
                    )
                )
                self._viewer.draw_circle(
                    (fixture.shape.radius) * 3 / 4 * PIXELS_PER_METER, 30, color=AGENT_COLORS[1]
                ).add_attr(
                    rendering.Transform(
                        translation=fixture.body.transform * fixture.shape.pos * PIXELS_PER_METER
                    )
                )
                if self._agent_delivery_targets[idx] > -1:
                    self._viewer.draw_circle(
                        (fixture.shape.radius) / 2 * PIXELS_PER_METER, 30, color=AGENT_COLORS[2]
                    ).add_attr(
                        rendering.Transform(
                            translation=fixture.body.transform
                            * fixture.shape.pos
                            * PIXELS_PER_METER
                        )
                    )

        self._viewer.render()
