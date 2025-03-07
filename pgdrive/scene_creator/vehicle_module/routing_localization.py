import logging
import math
from pgdrive.scene_creator.blocks.bottleneck import Merge, Split
import numpy as np
from panda3d.core import BitMask32, LQuaternionf, TransparencyAttrib, LineSegs, NodePath
from pgdrive.scene_creator.lane.straight_lane import StraightLane
from pgdrive.constants import COLLISION_INFO_COLOR, RENDER_MODE_ONSCREEN, CamMask
from pgdrive.scene_creator.blocks.first_block import FirstBlock
from pgdrive.scene_creator.lane.circular_lane import CircularLane
from pgdrive.scene_creator.map import Map
from pgdrive.scene_creator.road.road import Road
from pgdrive.utils import clip, norm, get_np_random
from pgdrive.utils.asset_loader import AssetLoader
from pgdrive.utils.pg_space import Parameter, BlockParameterSpace
from pgdrive.utils.scene_utils import ray_localization
from pgdrive.utils.coordinates_shift import panda_position


class RoutingLocalizationModule:
    navigation_info_dim = 10
    NAVI_POINT_DIST = 50
    PRE_NOTIFY_DIST = 40
    MARK_COLOR = COLLISION_INFO_COLOR["green"][1]
    MIN_ALPHA = 0.15
    CKPT_UPDATE_RANGE = 5
    FORCE_CALCULATE = False
    LINE_TO_DEST_HEIGHT = 0.6

    def __init__(
        self,
        pg_world,
        show_navi_mark: bool = False,
        random_navi_mark_color=False,
        show_dest_mark=False,
        show_line_to_dest=False
    ):
        """
        This class define a helper for localizing vehicles and retrieving navigation information.
        It now only support from first block start to the end node, but can be extended easily.
        """
        self.map = None
        self.final_road = None
        self.checkpoints = None
        self.final_lane = None
        self.current_ref_lanes = None
        self.current_road = None
        self._target_checkpoints_index = None
        self._navi_info = np.zeros((self.navigation_info_dim, ))  # navi information res
        self.navi_mark_color = (0.6, 0.8, 0.5) if not random_navi_mark_color else get_np_random().rand(3)

        # Vis
        self._is_showing = True  # store the state of navigation mark
        self._show_navi_info = (
            pg_world.mode == RENDER_MODE_ONSCREEN and not pg_world.world_config["debug_physics_world"]
        )
        self._dest_node_path = None
        self._goal_node_path = None
        self._arrow_node_path = None
        self._line_to_dest = None
        self._show_line_to_dest = show_line_to_dest
        if self._show_navi_info:
            # nodepath
            self._line_to_dest = pg_world.render.attachNewNode("line")
            self._goal_node_path = pg_world.render.attachNewNode("target")
            self._dest_node_path = pg_world.render.attachNewNode("dest")
            self._arrow_node_path = pg_world.aspect2d.attachNewNode("arrow")

            navi_arrow_model = AssetLoader.loader.loadModel(AssetLoader.file_path("models", "navi_arrow.gltf"))
            navi_arrow_model.setScale(0.1, 0.12, 0.2)
            navi_arrow_model.setPos(2, 1.15, -0.221)
            self._left_arrow = self._arrow_node_path.attachNewNode("left arrow")
            self._left_arrow.setP(180)
            self._right_arrow = self._arrow_node_path.attachNewNode("right arrow")
            self._left_arrow.setColor(self.MARK_COLOR)
            self._right_arrow.setColor(self.MARK_COLOR)
            navi_arrow_model.instanceTo(self._left_arrow)
            navi_arrow_model.instanceTo(self._right_arrow)
            self._arrow_node_path.setPos(0, 0, 0.08)
            self._arrow_node_path.hide(BitMask32.allOn())
            self._arrow_node_path.show(CamMask.MainCam)
            self._arrow_node_path.setQuat(LQuaternionf(np.cos(-np.pi / 4), 0, 0, np.sin(-np.pi / 4)))

            # the transparency attribute of gltf model is invalid on windows
            # self._arrow_node_path.setTransparency(TransparencyAttrib.M_alpha)
            if show_navi_mark:
                navi_point_model = AssetLoader.loader.loadModel(AssetLoader.file_path("models", "box.bam"))
                navi_point_model.reparentTo(self._goal_node_path)
            if show_dest_mark:
                dest_point_model = AssetLoader.loader.loadModel(AssetLoader.file_path("models", "box.bam"))
                dest_point_model.reparentTo(self._dest_node_path)
            if show_line_to_dest:
                line_seg = LineSegs("line_to_dest")
                line_seg.setColor(self.navi_mark_color[0], self.navi_mark_color[1], self.navi_mark_color[2], 0.7)
                line_seg.setThickness(2)
                self._dynamic_line_np = NodePath(line_seg.create(True))
                self._dynamic_line_np.reparentTo(pg_world.render)
                self._line_to_dest = line_seg

            self._goal_node_path.setTransparency(TransparencyAttrib.M_alpha)
            self._dest_node_path.setTransparency(TransparencyAttrib.M_alpha)

            self._goal_node_path.setColor(
                self.navi_mark_color[0], self.navi_mark_color[1], self.navi_mark_color[2], 0.7
            )
            self._dest_node_path.setColor(
                self.navi_mark_color[0], self.navi_mark_color[1], self.navi_mark_color[2], 0.7
            )
            self._goal_node_path.hide(BitMask32.allOn())
            self._dest_node_path.hide(BitMask32.allOn())
            self._goal_node_path.show(CamMask.MainCam)
            self._dest_node_path.show(CamMask.MainCam)
        logging.debug("Load Vehicle Module: {}".format(self.__class__.__name__))

    def update(self, map: Map, current_lane_index, final_road_node=None, random_seed=False):
        start_road_node = current_lane_index[0]
        self.map = map
        if start_road_node is None:
            start_road_node = FirstBlock.NODE_1
        if final_road_node is None:
            current_road_negative = Road(*current_lane_index[:-1]).is_negative_road()
            random_seed = random_seed if random_seed is not False else map.random_seed
            # choose first block when born on negative road
            block = map.blocks[0] if current_road_negative else map.blocks[-1]
            sockets = block.get_socket_list()
            while True:
                socket = get_np_random(random_seed).choice(sockets)
                if not socket.is_socket_node(start_road_node):
                    break
                else:
                    sockets.remove(socket)
                    if len(sockets) == 0:
                        raise ValueError("Can not set a destination!")
            # choose negative road end node when current road is negative road
            final_road_node = socket.negative_road.end_node if current_road_negative else socket.positive_road.end_node
        self.set_route(start_road_node, final_road_node)

    def set_route(self, start_road_node: str, end_road_node: str):
        """
        Find a shorest path from start road to end road
        :param start_road_node: start road node
        :param end_road_node: end road node
        :return: None
        """
        self.checkpoints = self.map.road_network.shortest_path(start_road_node, end_road_node)
        assert len(self.checkpoints) > 2, "Can not find a route from {} to {}".format(start_road_node, end_road_node)
        # update routing info
        self.final_road = Road(self.checkpoints[-2], end_road_node)
        final_lanes = self.final_road.get_lanes(self.map.road_network)
        self.final_lane = final_lanes[-1]
        self._target_checkpoints_index = [0, 1]
        self._navi_info.fill(0.0)
        target_road_1_start = self.checkpoints[0]
        target_road_1_end = self.checkpoints[1]
        self.current_ref_lanes = self.map.road_network.graph[target_road_1_start][target_road_1_end]
        self.current_road = Road(target_road_1_start, target_road_1_end)
        if self._dest_node_path is not None:
            ref_lane = final_lanes[0]
            later_middle = (float(self.get_current_lane_num()) / 2 - 0.5) * self.get_current_lane_width()
            check_point = ref_lane.position(ref_lane.length, later_middle)
            self._dest_node_path.setPos(check_point[0], -check_point[1], 1.8)

    def update_navigation_localization(self, ego_vehicle):
        position = ego_vehicle.position
        lane, lane_index = self.get_current_lane(ego_vehicle)
        if lane is None:
            lane, lane_index = ego_vehicle.lane, ego_vehicle.lane_index
            ego_vehicle.on_lane = False
            if self.FORCE_CALCULATE:
                lane_index, _ = self.map.road_network.get_closest_lane_index(position)
                lane = self.map.road_network.get_lane(lane_index)
        long, _ = lane.local_coordinates(position)
        self._update_target_checkpoints(lane_index, long)

        assert len(self.checkpoints) > 2

        target_road_1_start = self.checkpoints[self._target_checkpoints_index[0]]
        target_road_1_end = self.checkpoints[self._target_checkpoints_index[0] + 1]
        target_road_2_start = self.checkpoints[self._target_checkpoints_index[1]]
        target_road_2_end = self.checkpoints[self._target_checkpoints_index[1] + 1]
        target_lanes_1 = self.map.road_network.graph[target_road_1_start][target_road_1_end]
        target_lanes_2 = self.map.road_network.graph[target_road_2_start][target_road_2_end]
        self.current_ref_lanes = target_lanes_1
        self.current_road = Road(target_road_1_start, target_road_1_end)

        self._navi_info.fill(0.0)
        half = self.navigation_info_dim // 2
        self._navi_info[:half], lanes_heading1, checkpoint = self._get_info_for_checkpoint(
            lanes_id=0, lanes=target_lanes_1, ego_vehicle=ego_vehicle
        )

        self._navi_info[half:], lanes_heading2, _ = self._get_info_for_checkpoint(
            lanes_id=1, lanes=target_lanes_2, ego_vehicle=ego_vehicle
        )

        if self._show_navi_info:
            pos_of_goal = checkpoint
            self._goal_node_path.setPos(pos_of_goal[0], -pos_of_goal[1], 1.8)
            self._goal_node_path.setH(self._goal_node_path.getH() + 3)
            self._update_navi_arrow([lanes_heading1, lanes_heading2])
            dest_pos = self._dest_node_path.getPos()
            self._draw_line_to_dest(
                pg_world=ego_vehicle.pg_world,
                start_position=ego_vehicle.position,
                end_position=(dest_pos[0], -dest_pos[1])
            )

        return lane, lane_index

    def _get_info_for_checkpoint(self, lanes_id, lanes, ego_vehicle):
        ref_lane = lanes[0]
        later_middle = (float(self.get_current_lane_num()) / 2 - 0.5) * self.get_current_lane_width()
        check_point = ref_lane.position(ref_lane.length, later_middle)
        if lanes_id == 0:
            # calculate ego v lane heading
            lanes_heading = ref_lane.heading_at(ref_lane.local_coordinates(ego_vehicle.position)[0])
        else:
            lanes_heading = ref_lane.heading_at(min(self.PRE_NOTIFY_DIST, ref_lane.length))
        dir_vec = check_point - ego_vehicle.position
        dir_norm = norm(dir_vec[0], dir_vec[1])
        if dir_norm > self.NAVI_POINT_DIST:
            dir_vec = dir_vec / dir_norm * self.NAVI_POINT_DIST
        proj_heading, proj_side = ego_vehicle.projection(dir_vec)
        bendradius = 0.0
        dir = 0.0
        angle = 0.0
        if isinstance(ref_lane, CircularLane):
            bendradius = ref_lane.radius / (
                BlockParameterSpace.CURVE[Parameter.radius].max +
                self.get_current_lane_num() * self.get_current_lane_width()
            )
            dir = ref_lane.direction
            if dir == 1:
                angle = ref_lane.end_phase - ref_lane.start_phase
            elif dir == -1:
                angle = ref_lane.start_phase - ref_lane.end_phase
        return (
            clip((proj_heading / self.NAVI_POINT_DIST + 1) / 2, 0.0,
                 1.0), clip((proj_side / self.NAVI_POINT_DIST + 1) / 2, 0.0,
                            1.0), clip(bendradius, 0.0, 1.0), clip((dir + 1) / 2, 0.0, 1.0),
            clip((np.rad2deg(angle) / BlockParameterSpace.CURVE[Parameter.angle].max + 1) / 2, 0.0, 1.0)
        ), lanes_heading, check_point

    def _update_navi_arrow(self, lanes_heading):
        lane_0_heading = lanes_heading[0]
        lane_1_heading = lanes_heading[1]
        if abs(lane_0_heading - lane_1_heading) < 0.01:
            if self._is_showing:
                self._left_arrow.detachNode()
                self._right_arrow.detachNode()
                self._is_showing = False
        else:
            dir_0 = np.array([math.cos(lane_0_heading), math.sin(lane_0_heading), 0])
            dir_1 = np.array([math.cos(lane_1_heading), math.sin(lane_1_heading), 0])
            cross_product = np.cross(dir_1, dir_0)
            left = False if cross_product[-1] < 0 else True
            if not self._is_showing:
                self._is_showing = True
            if left:
                if not self._left_arrow.hasParent():
                    self._left_arrow.reparentTo(self._arrow_node_path)
                if self._right_arrow.hasParent():
                    self._right_arrow.detachNode()
            else:
                if not self._right_arrow.hasParent():
                    self._right_arrow.reparentTo(self._arrow_node_path)
                if self._left_arrow.hasParent():
                    self._left_arrow.detachNode()

    def _update_target_checkpoints(self, ego_lane_index, ego_lane_longitude):
        """
        Return should_update: True or False
        """
        if self._target_checkpoints_index[0] == self._target_checkpoints_index[1]:  # on last road
            return False

        # arrive to second checkpoint
        current_road_start_point = ego_lane_index[0]
        if current_road_start_point in self.checkpoints[self._target_checkpoints_index[1]:] \
                and ego_lane_longitude < self.CKPT_UPDATE_RANGE:
            if current_road_start_point not in self.checkpoints[self._target_checkpoints_index[1]:-1]:
                return False
            idx = self.checkpoints.index(current_road_start_point, self._target_checkpoints_index[1], -1)
            self._target_checkpoints_index = [idx]
            if idx + 1 == len(self.checkpoints) - 1:
                self._target_checkpoints_index.append(idx)
            else:
                self._target_checkpoints_index.append(idx + 1)
            return True
        return False

    def get_navi_info(self):
        return self._navi_info

    def destroy(self):
        if self._show_navi_info:
            self._arrow_node_path.removeNode()
            self._line_to_dest.removeNode()
            self._dest_node_path.removeNode()
            self._goal_node_path.removeNode()

    def set_force_calculate_lane_index(self, force: bool):
        self.FORCE_CALCULATE = force

    def __del__(self):
        logging.debug("{} is destroyed".format(self.__class__.__name__))

    def get_current_lateral_range(self, current_position, pg_world) -> float:
        """Return the maximum lateral distance from left to right."""
        # special process for special block
        current_block_id = self.current_road.block_ID()
        if current_block_id == Split.ID or current_block_id == Merge.ID:
            left_lane = self.current_ref_lanes[0]
            assert isinstance(left_lane, StraightLane), "Reference lane should be straight lane here"
            long, lat = left_lane.local_coordinates(current_position)
            current_position = left_lane.position(long, -left_lane.width / 2)
            return self._ray_lateral_range(pg_world, current_position, self.current_ref_lanes[0].direction_lateral)
        else:
            return self.get_current_lane_width() * self.get_current_lane_num()

    def get_current_lane_width(self) -> float:
        return self.map.config[self.map.LANE_WIDTH]

    def get_current_lane_num(self) -> float:
        return len(self.current_ref_lanes)

    def get_current_lane(self, ego_vehicle):
        possible_lanes = ray_localization(
            np.array(ego_vehicle.heading.tolist()), ego_vehicle.position, ego_vehicle.pg_world, return_all_result=True
        )
        for lane, index, l_1_dist in possible_lanes:
            if lane in self.current_ref_lanes:
                return lane, index
        return possible_lanes[0][:-1] if len(possible_lanes) > 0 else (None, None)

    def _ray_lateral_range(self, pg_world, start_position, dir, length=50):
        """
        It is used to measure the lateral range of special blocks
        :param start_position: start_point
        :param dir: ray direction
        :param length: length of ray
        :return: lateral range [m]
        """
        end_position = start_position[0] + dir[0] * length, start_position[1] + dir[1] * length
        start_position = panda_position(start_position, z=0.15)
        end_position = panda_position(end_position, z=0.15)
        mask = BitMask32.bit(FirstBlock.CONTINUOUS_COLLISION_MASK)
        res = pg_world.physics_world.static_world.rayTestClosest(start_position, end_position, mask=mask)
        if not res.hasHit():
            return length
        else:
            return res.getHitFraction() * length

    def _draw_line_to_dest(self, pg_world, start_position, end_position):
        if not self._show_line_to_dest:
            return
        line_seg = self._line_to_dest
        line_seg.moveTo(panda_position(start_position, self.LINE_TO_DEST_HEIGHT))
        line_seg.drawTo(panda_position(end_position, self.LINE_TO_DEST_HEIGHT))
        self._dynamic_line_np.removeNode()
        self._dynamic_line_np = NodePath(line_seg.create(False))
        self._dynamic_line_np.hide(CamMask.Shadow | CamMask.RgbCam)
        self._dynamic_line_np.reparentTo(pg_world.render)
