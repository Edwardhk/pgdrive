import gym
import numpy as np
from pgdrive.envs.multi_agent_pgdrive import MultiAgentPGDrive
from pgdrive.obs import ObservationType
from pgdrive.obs.state_obs import StateObservation
from pgdrive.scene_creator.blocks.first_block import FirstBlock
from pgdrive.scene_creator.blocks.roundabout import Roundabout
from pgdrive.scene_creator.map import PGMap
from pgdrive.scene_creator.road.road import Road
from pgdrive.utils import get_np_random, norm, PGConfig

MARoundaboutConfig = dict(
    map_config=dict(exit_length=60, lane_num=2),
    top_down_camera_initial_x=95,
    top_down_camera_initial_y=15,
    top_down_camera_initial_z=120
)


class MARoundaboutMap(PGMap):
    def _generate(self, pg_world):
        length = self.config["exit_length"]

        parent_node_path, pg_physics_world = pg_world.worldNP, pg_world.physics_world
        assert len(self.road_network.graph) == 0, "These Map is not empty, please create a new map to read config"

        # Build a first-block
        last_block = FirstBlock(
            self.road_network,
            self.config[self.LANE_WIDTH],
            self.config[self.LANE_NUM],
            parent_node_path,
            pg_physics_world,
            1,
            length=length
        )
        self.blocks.append(last_block)

        # Build roundabout
        Roundabout.EXIT_PART_LENGTH = length
        last_block = Roundabout(1, last_block.get_socket(index=0), self.road_network, random_seed=1)
        last_block.construct_block(
            parent_node_path,
            pg_physics_world,
            extra_config={
                "exit_radius": 10,
                "inner_radius": 30,
                "angle": 70,
                # Note: lane_num is set in config.map_config.lane_num
            }
        )
        self.blocks.append(last_block)


class LidarStateObservationMARound(ObservationType):
    def __init__(self, vehicle_config):
        self.state_obs = StateObservation(vehicle_config)
        super(LidarStateObservationMARound, self).__init__(vehicle_config)
        self.state_length = list(self.state_obs.observation_space.shape)[0]

    @property
    def observation_space(self):
        shape = list(self.state_obs.observation_space.shape)
        if self.config["lidar"]["num_lasers"] > 0 and self.config["lidar"]["distance"] > 0:
            # Number of lidar rays and distance should be positive!
            shape[0] += self.config["lidar"]["num_lasers"] + self.config["lidar"]["num_others"] * self.state_length
        return gym.spaces.Box(-0.0, 1.0, shape=tuple(shape), dtype=np.float32)

    def observe(self, vehicle):
        num_others = self.config["lidar"]["num_others"]
        state = self.state_observe(vehicle)
        other_v_info = []
        if vehicle.lidar is not None:
            if self.config["lidar"]["num_others"] > 0:
                surrounding_vehicles = list(vehicle.lidar.get_surrounding_vehicles())
                surrounding_vehicles.sort(
                    key=lambda v: norm(vehicle.position[0] - v.position[0], vehicle.position[1] - v.position[1])
                )
                surrounding_vehicles += [None] * num_others
                for tmp_v in surrounding_vehicles[:num_others]:
                    if tmp_v is not None:
                        tmp_v = tmp_v.get_vehicle()
                        other_v_info += self.state_observe(tmp_v).tolist()
                    else:
                        other_v_info += [0] * self.state_length
            other_v_info += self._add_noise_to_cloud_points(
                vehicle.lidar.get_cloud_points(),
                gaussian_noise=self.config["lidar"]["gaussian_noise"],
                dropout_prob=self.config["lidar"]["dropout_prob"]
            )
        return np.concatenate((state, np.asarray(other_v_info)))

    def state_observe(self, vehicle):
        return self.state_obs.observe(vehicle)

    def _add_noise_to_cloud_points(self, points, gaussian_noise, dropout_prob):
        if gaussian_noise > 0.0:
            points = np.asarray(points)
            points = np.clip(points + np.random.normal(loc=0.0, scale=gaussian_noise, size=points.shape), 0.0, 1.0)

        if dropout_prob > 0.0:
            assert dropout_prob <= 1.0
            points = np.asarray(points)
            points[np.random.uniform(0, 1, size=points.shape) < dropout_prob] = 0.0

        return list(points)


class MultiAgentRoundaboutEnv(MultiAgentPGDrive):
    spawn_roads = [
        Road(FirstBlock.NODE_2, FirstBlock.NODE_3),
        -Road(Roundabout.node(1, 0, 2), Roundabout.node(1, 0, 3)),
        -Road(Roundabout.node(1, 1, 2), Roundabout.node(1, 1, 3)),
        -Road(Roundabout.node(1, 2, 2), Roundabout.node(1, 2, 3)),
    ]

    @staticmethod
    def default_config() -> PGConfig:
        return MultiAgentPGDrive.default_config().update(MARoundaboutConfig, allow_overwrite=True)

    def _update_map(self, episode_data: dict = None, force_seed=None):
        if episode_data is not None:
            raise ValueError()
        map_config = self.config["map_config"]
        map_config.update({"seed": self.current_seed})

        if self.current_map is None:
            self.current_seed = 0
            new_map = MARoundaboutMap(self.pg_world, map_config)
            self.maps[self.current_seed] = new_map
            self.current_map = self.maps[self.current_seed]

    def _update_destination_for(self, vehicle_id):
        vehicle = self.vehicles[vehicle_id]
        # when agent re-joined to the game, call this to set the new route to destination
        end_road = -get_np_random(self._DEBUG_RANDOM_SEED).choice(self.spawn_roads)  # Use negative road!
        vehicle.routing_localization.set_route(vehicle.lane_index[0], end_road.end_node)

    def get_single_observation(self, vehicle_config: "PGConfig") -> "ObservationType":
        return LidarStateObservationMARound(vehicle_config)


def _draw():
    env = MultiAgentRoundaboutEnv()
    o = env.reset()
    from pgdrive.utils import draw_top_down_map
    import matplotlib.pyplot as plt

    plt.imshow(draw_top_down_map(env.current_map))
    plt.show()
    env.close()


def _expert():
    env = MultiAgentRoundaboutEnv(
        {
            "vehicle_config": {
                "lidar": {
                    "num_lasers": 240,
                    "num_others": 4,
                    "distance": 50
                },
                "use_saver": True,
                "save_level": 1.
            },
            "pg_world_config": {
                "debug_physics_world": True
            },
            "fast": True,
            # "use_render": True,
            "debug": True,
            "manual_control": True,
            "num_agents": 4,
        }
    )
    o = env.reset()
    total_r = 0
    ep_s = 0
    for i in range(1, 100000):
        o, r, d, info = env.step(env.action_space.sample())
        for r_ in r.values():
            total_r += r_
        ep_s += 1
        d.update({"total_r": total_r, "episode length": ep_s})
        # env.render(text=d)
        if d["__all__"]:
            print(
                "Finish! Current step {}. Group Reward: {}. Average reward: {}".format(
                    i, total_r, total_r / env.agent_manager.next_agent_count
                )
            )
            break
        if len(env.vehicles) == 0:
            total_r = 0
            print("Reset")
            env.reset()
    env.close()


def _vis_debug_respawn():
    env = MultiAgentRoundaboutEnv(
        {
            "horizon": 100000,
            "vehicle_config": {
                "lidar": {
                    "num_lasers": 72,
                    "num_others": 0,
                    "distance": 40
                },
                "show_lidar": False,
            },
            "pg_world_config": {
                "debug_physics_world": True
            },
            "fast": True,
            "use_render": True,
            "debug": False,
            "manual_control": True,
            "num_agents": 40,
        }
    )
    o = env.reset()
    total_r = 0
    ep_s = 0
    for i in range(1, 100000):
        action = {k: [0.0, .0] for k in env.vehicles.keys()}
        o, r, d, info = env.step(action)
        for r_ in r.values():
            total_r += r_
        ep_s += 1
        # d.update({"total_r": total_r, "episode length": ep_s})
        render_text = {
            "total_r": total_r,
            "episode length": ep_s,
            "cam_x": env.main_camera.camera_x,
            "cam_y": env.main_camera.camera_y,
            "cam_z": env.main_camera.top_down_camera_height
        }
        env.render(text=render_text)
        if d["__all__"]:
            print(
                "Finish! Current step {}. Group Reward: {}. Average reward: {}".format(
                    i, total_r, total_r / env.agent_manager.next_agent_count
                )
            )
            # break
        if len(env.vehicles) == 0:
            total_r = 0
            print("Reset")
            env.reset()
    env.close()


def _vis():
    env = MultiAgentRoundaboutEnv(
        {
            "horizon": 100000,
            "vehicle_config": {
                "lidar": {
                    "num_lasers": 72,
                    "num_others": 0,
                    "distance": 40
                },
                "show_lidar": False,
            },
            "fast": True,
            "use_render": True,
            "debug": False,
            "manual_control": True,
            "num_agents": 40,
        }
    )
    o = env.reset()
    total_r = 0
    ep_s = 0
    for i in range(1, 100000):
        o, r, d, info = env.step({k: [1.0, 1.0] for k in env.vehicles.keys()})
        for r_ in r.values():
            total_r += r_
        ep_s += 1
        # d.update({"total_r": total_r, "episode length": ep_s})
        render_text = {
            "total_r": total_r,
            "episode length": ep_s,
            "cam_x": env.main_camera.camera_x,
            "cam_y": env.main_camera.camera_y,
            "cam_z": env.main_camera.top_down_camera_height
        }
        env.render(text=render_text)
        if d["__all__"]:
            print(
                "Finish! Current step {}. Group Reward: {}. Average reward: {}".format(
                    i, total_r, total_r / env.agent_manager.next_agent_count
                )
            )
            # break
        if len(env.vehicles) == 0:
            total_r = 0
            print("Reset")
            env.reset()
    env.close()


def _profile():
    import time
    env = MultiAgentRoundaboutEnv({"num_agents": 16})
    obs = env.reset()
    start = time.time()
    for s in range(10000):
        o, r, d, i = env.step(env.action_space.sample())

        # mask_ratio = env.scene_manager.detector_mask.get_mask_ratio()
        # print("Mask ratio: ", mask_ratio)

        if all(d.values()):
            env.reset()
        if (s + 1) % 100 == 0:
            print(
                "Finish {}/10000 simulation steps. Time elapse: {:.4f}. Average FPS: {:.4f}".format(
                    s + 1,
                    time.time() - start, (s + 1) / (time.time() - start)
                )
            )
    print(f"(PGDriveEnvV2) Total Time Elapse: {time.time() - start}")


def _long_run():
    # Please refer to test_ma_roundabout_reward_done_alignment()
    _out_of_road_penalty = 3
    env = MultiAgentRoundaboutEnv(
        {
            "num_agents": 32,
            "vehicle_config": {
                "lidar": {
                    "num_others": 8
                }
            },
            **dict(
                out_of_road_penalty=_out_of_road_penalty,
                crash_vehicle_penalty=1.333,
                crash_object_penalty=11,
                crash_vehicle_cost=13,
                crash_object_cost=17,
                out_of_road_cost=19,
            )
        }
    )
    try:
        obs = env.reset()
        assert env.observation_space.contains(obs)
        for step in range(10000):
            act = env.action_space.sample()
            o, r, d, i = env.step(act)
            if step == 0:
                assert not any(d.values())

            if any(d.values()):
                print("Current Done: {}\nReward: {}".format(d, r))
                for kkk, ddd in d.items():
                    if ddd and kkk != "__all__":
                        print("Info {}: {}\n".format(kkk, i[kkk]))
                print("\n")

            for kkk, rrr in r.items():
                if rrr == -_out_of_road_penalty:
                    assert d[kkk]

            if (step + 1) % 200 == 0:
                print(
                    "{}/{} Agents: {} {}\nO: {}\nR: {}\nD: {}\nI: {}\n\n".format(
                        step + 1, 10000, len(env.vehicles), list(env.vehicles.keys()),
                        {k: (oo.shape, oo.mean(), oo.min(), oo.max())
                         for k, oo in o.items()}, r, d, i
                    )
                )
            if d["__all__"]:
                print('Current step: ', step)
                break
    finally:
        env.close()


if __name__ == "__main__":
    # _draw()
    _vis()
    # _vis_debug_respawn()
    # _profiwdle()
    # _long_run()
