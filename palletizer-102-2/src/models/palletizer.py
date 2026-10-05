"""Course starter for Viam 102: copy this over src/models/palletizer.py.

The top of the class is the module contract, already working: the module
answers `status` the moment it is installed. The body below it is the
palletizing logic from Viam 101. `pick` is complete and is your template;
`place` and `pack` are yours to write, and the course pages take each
piece in order.
"""

import asyncio
from typing import ClassVar, Mapping, Optional, Sequence, Tuple

from typing_extensions import Self
from viam.components.gripper import Gripper
from viam.components.sensor import Sensor
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import (
    GeometriesInFrame,
    Geometry,
    Pose,
    PoseInFrame,
    RectangularPrism,
    ResourceName,
    Vector3,
    WorldState,
)
from viam.resource.base import ResourceBase
from viam.resource.easy_resource import EasyResource
from viam.resource.types import Model, ModelFamily
from viam.services.generic import *  # noqa: F403  (exactly what `viam module generate` emits;
#                                     the learner diffs their scaffold against this file, so it
#                                     must not diverge from the tool's output)
from viam.utils import ValueTypes, struct_to_dict  # noqa: F401  (struct_to_dict is unused
#                    HERE on purpose: the attributes page has the learner paste the
#                    validate_config/reconfigure code that calls it. Shipping the import
#                    means their paste runs first try instead of NameError-ing.)

# The pick-station and the pallet are generic COMPONENTS. The `Generic` this
# class inherits from is the generic SERVICE, imported by the star import above.
# They share a name and are different classes, so the component is aliased here.
from viam.components.generic import Generic as GenericComponent
from viam.services.motion import MotionClient
from viam.services.worldstatestore import WorldStateStore

# The work-cell resources this service drives, as the viam102-workcell fragment
# names them.
GRIPPER = "gripper-1"
PICK_STATION = "pick-station"
PALLET = "pallet"
PACK_SEQUENCER = "pack-sequencer"
MOTION = "builtin"
BOX_DETECT = "box-detect"
PALLET_EMPTY = "pallet-empty"
TRAY_DOCK = "tray-dock"

# The box the pick-station presents, and how far the vacuum gripper
# presses onto its top.
BOX_W, BOX_L, BOX_H = 200.0, 150.0, 100.0
GRASP_DEPTH = 10.0


def down_pose(x, y, z) -> Pose:
    """A target pose at (x, y, z) with the tool pointing straight down."""
    return Pose(x=x, y=y, z=z, o_x=0, o_y=0, o_z=-1, theta=0)


def pose_from_map(m) -> Pose:
    return Pose(
        x=m["x"], y=m["y"], z=m["z"],
        o_x=m["o_x"], o_y=m["o_y"], o_z=m["o_z"], theta=m["theta"],
    )


class Palletizer(Generic, EasyResource):  # noqa: F405  (Generic comes from the star import above)
    # The model triplet from the generate prompts. copy-starter-code
    # fills in your namespace and module name.
    MODEL: ClassVar[Model] = Model(
        ModelFamily("swiftjaderobot", "palletizer-102-2"), "palletizer"
    )

    # The pack pattern, hard-coded for now so the logic runs before it is
    # configurable. The Attributes page turns these four into configuration and
    # your constructor starts setting them per instance.
    columns, rows, layers = 2, 2, 2
    box_h = BOX_H

    # ---------------------------------------------------------------- contract
    # validate_config declares what this service needs; new receives it.

    @classmethod
    def new(
        cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ) -> Self:
        """This method creates a new instance of this Generic service."""
        self = super().new(config, dependencies)

        # Get each resource by name and keep it for later use.
        self.gripper = dependencies[Gripper.get_resource_name(GRIPPER)]
        self.pick_station = dependencies[
            GenericComponent.get_resource_name(PICK_STATION)]
        self.sequencer = dependencies[
            WorldStateStore.get_resource_name(PACK_SEQUENCER)]
        self.motion = dependencies[MotionClient.get_resource_name(MOTION)]

        self.placed = []   # world centers of boxes already on the pallet
        self._top = None   # cached pallet-top center

        # Read validated attributes here: whatever
        # validate_config accepted, store on self.

        # The cell's sensors. The infeed's box-detect stays off (pick
        # draws its own box); pallet-empty and tray-dock resolve as
        # optional dependencies so autopack can hand a full tray to the
        # dock and start packing a fresh one.
        self.box_detect = None
        self.pallet = dependencies[
            GenericComponent.get_resource_name(PALLET)
        ]
        self.pallet_empty = dependencies.get(
            Sensor.get_resource_name(PALLET_EMPTY))
        self.tray_dock = dependencies.get(
            Sensor.get_resource_name(TRAY_DOCK))
        return self

    @classmethod
    def validate_config(
        cls, config: ComponentConfig
    ) -> Tuple[Sequence[str], Sequence[str]]:
        """Tell viam-server what this service needs before it starts.

        Returns two lists: required dependencies, then optional ones.
        viam-server calls the constructor only after every required
        resource is up and healthy.
        """
        # Required, then optional. The pallet is yours to declare.
        req_deps = [GRIPPER, PICK_STATION, PALLET,
                    PACK_SEQUENCER, MOTION]
        optional_deps = [PALLET_EMPTY, TRAY_DOCK]

        # Validate new attributes here: read them with
        # struct_to_dict, check each value, and raise
        # ValueError naming exactly what is wrong.

        return req_deps, optional_deps

    async def do_command(
        self,
        command: Mapping[str, ValueTypes],
        *,
        timeout: Optional[float] = None,
        **kwargs
    ) -> Mapping[str, ValueTypes]:
        """Route every command a caller sends to this service.

        The caller's JSON arrives as `command`, a dict; the dict you
        return goes back to the caller as JSON. Each verb routes to a
        method. `status` and `clear` are wired up; routing the rest is yours.
        """
        # Every command names its verb: {"command": "status"}.
        verb = command.get("command")

        # status: report what this service resolved, no hardware touched.
        if verb == "status":
            return await self.status()

        # clear: empty the pallet, the scene, and the progress.
        if verb == "clear":
            await self.clear_boxes()
            self.placed = []
            await self.sequencer.do_command({"reset_cursor": True})
            return {"cleared": True}

        if verb == "pick":
            return{"picked": await self.pick()}    

        # Route new verbs here: match the verb, call the method,
        # return what it returns.

        # Anything unrouted gets an answer and a log line, never a stack trace.
        self.logger.warning(f"unknown command: {verb}")
        return {"error": f"unknown command: {verb}"}

    async def status(self):
        """Report each resource this service resolved, and the count."""
        held = {
            GRIPPER: self.gripper,
            PICK_STATION: self.pick_station,
            PALLET: self.pallet,
            PACK_SEQUENCER: self.sequencer,
            MOTION: self.motion,
        }
        return {
            "resources": {
                name: "ok" if res is not None else "missing"
                for name, res in held.items()
            },
            "boxes_packed": len(self.placed),
        }

    # ------------------------------------------------------------ the verbs
    # pick is complete. place and pack are yours to write.

    async def pick(self, seq=0):
        """Pick the box at the pick-station and lift it clear.

        This method is complete: use it as the template for `place` and
        `pack`. Every step is a helper defined below, reached through
        `self`.
        """
        # Wait until the infeed reports a box (immediate with no sensor).
        await self.wait_for_box()
        # Ask the pick-station where the box is and where to hover.
        home = await self.pick_home_pose()
        grasp = await self.grasp_pose()
        # Move to the hover pose, avoiding any boxes already placed.
        await self.move_gripper(home, self.obstacles())
        if self.box_detect is None:
            # The infeed is off until the sensors page: draw the box
            # this pick is about to take.
            await self.show_box(seq, grasp.x, grasp.y,
                                grasp.z - self.box_h / 2)
        # Descend onto the box and take it.
        await self.move_gripper(down_pose(grasp.x, grasp.y, grasp.z - GRASP_DEPTH))
        await self.gripper.grab()
        await self.attach_box(seq)
        # Tell the pick-station this box is taken. Once the infeed
        # is on, the next box starts down the conveyor during the
        # carry.
        await self.retry("take", lambda: self.pick_station.do_command(
            {"take": True}))
        # Lift back to the hover pose with the box held.
        await self.move_gripper(home, self.obstacles(held=True))
        # Report success. (`place` and `pack` return the slot/count they
        # produce; pick just takes the next box, so `True` says it worked.)
        return True

    async def place(self):
        """One full pick-and-place of the next box onto the pallet.

        Use `pick` as a template: it shows how the pieces below fit
        together. You need the pallet's top, the slot this box belongs
        in, a move that carries the box high enough to clear the stack,
        a straight descent, an open gripper, and a record of where the
        box ended up.
        """
        raise NotImplementedError("implement me")

    async def pack(self):
        """Pack the whole pallet.

        Clear the scene, empty the record of placed boxes, and
        place one box per slot in the pattern.
        """
        raise NotImplementedError("implement me")

    # --------------------------------------------------------------- motion

    async def move_gripper(self, pose, obstacles=None):
        """Move the gripper frame to `pose`, routing around `obstacles`.

        Name the gripper, not the arm: the cups hang below the flange,
        so moving the arm to `pose` puts the cups 196 mm below it.
        """
        destination = PoseInFrame(reference_frame="world", pose=pose)
        return await self.retry("move", lambda: self.motion.move(
            component_name=GRIPPER,
            destination=destination,
            world_state=obstacles,
            extra={"timeout": 15.0},
            timeout=120,
        ))

    def obstacles(self, held=False):
        """Build the planner's obstacle set from the placed boxes.

        Each placed box is a cuboid in the `world` frame, a fixed obstacle. With
        `held=True` the carried box is added in the gripper's frame, so it rides
        along and the planner will not drag it through the stack.
        """
        def cuboid(label, frame, x, y, z):
            return GeometriesInFrame(
                reference_frame=frame,
                geometries=[Geometry(
                    center=Pose(x=x, y=y, z=z, o_x=0, o_y=0, o_z=1, theta=0),
                    box=RectangularPrism(
                        dims_mm=Vector3(x=BOX_W, y=BOX_L, z=self.box_h)),
                    label=label,
                )],
            )

        obs = [cuboid(f"placed-{i}", "world", x, y, z)
               for i, (x, y, z) in enumerate(self.placed)]
        if held:
            obs.append(cuboid("held", GRIPPER, 0, 0, self.box_h / 2))
        return WorldState(obstacles=obs) if obs else None

    # ------------------------------------------------------- pattern geometry

    def place_pose(self, i, cx, cy, top):
        """Box i -> (x, y, gripper-tip z) for the configured pattern."""
        # Which layer box i lands on, and which slot within that layer.
        per_layer = self.columns * self.rows
        layer, slot = i // per_layer, i % per_layer
        col, row = slot % self.columns, slot // self.columns
        # Center the grid on the pallet center (cx, cy).
        x = cx + (col - (self.columns - 1) / 2) * BOX_W
        y = cy + (row - (self.rows - 1) / 2) * BOX_L
        # The gripper tip stops one box-height above the layer below.
        z_tip = top + (layer + 1) * self.box_h
        return x, y, z_tip

    def clear_tip(self, z_tip):
        """A carrying height at which the held box clears the tallest placed box."""
        if not self.placed:
            return z_tip
        max_top = max(z + self.box_h / 2 for (_, _, z) in self.placed)
        return max(z_tip, max_top + self.box_h + 10.0)

    async def pallet_top(self):
        """(cx, cy, z) of the center of the pallet's top face, cached."""
        if self._top is None:
            pose = await self.retry(
                "get_visual_pose",
                lambda: self.pallet.do_command({"get_visual_pose": True}))
            attrs = await self.retry(
                "get_attributes",
                lambda: self.pallet.do_command({"get_attributes": True}))
            self._top = (pose["x"], pose["y"],
                         pose["z"] + attrs["thickness_mm"] / 2.0)
        return self._top

    # -------------------------------------------------- cell sensing helpers
    # These four read the cell's sensors, and every one tolerates the
    # sensor being absent, so the module runs on a cell that has no
    # sensors at all.

    async def wait_for_box(self, timeout_s=60.0):
        """Wait until the infeed reports a box waiting.

        With no box-detect sensor this returns immediately, which is
        what the earlier pages assumed.
        """
        if self.box_detect is None:
            return
        waited = 0.0
        while not await self.box_waiting():
            await asyncio.sleep(1)
            waited += 1
            if waited >= timeout_s:
                raise RuntimeError(
                    f"no box arrived within {timeout_s:.0f}s")

    async def box_waiting(self):
        """True when a box is waiting; with no sensor, assume yes."""
        if self.box_detect is None:
            return True
        readings = await self.box_detect.get_readings()
        return bool(readings.get("box_present"))

    async def pallet_has_room(self):
        """True while the pattern and the pallet both have room."""
        if len(self.placed) >= self.columns * self.rows * self.layers:
            return False
        if self.pallet_empty is None:
            return True
        readings = await self.pallet_empty.get_readings()
        return not bool(readings.get("pallet_full"))

    async def swap_tray(self, timeout_s=120.0):
        """Send the full tray out and wait for the empty one to dock.

        Clearing and dispatching are one motion in the story, so they
        happen together here: the record and the scene empty as the
        tray leaves. With no tray-dock sensor this is just a clear.
        """
        await self.clear_boxes()
        self.placed = []
        await self.sequencer.do_command({"reset_cursor": True})
        if self.tray_dock is None:
            return
        await self.tray_dock.do_command({"dispatch": True})
        waited = 0.0
        while True:
            readings = await self.tray_dock.get_readings()
            if readings.get("tray_present"):
                return
            await asyncio.sleep(1)
            waited += 1
            if waited >= timeout_s:
                raise RuntimeError(
                    f"no tray docked within {timeout_s:.0f}s")

    # -------------------------------------------------- 101 plumbing, ported
    # These started as functions in 101's helpers.py. Here they are
    # methods, reaching their resources through self.

    async def retry(self, what, factory, attempts=6, delay=2.0):
        """Run an RPC, riding out the occasional connection blip."""
        last = None
        for k in range(attempts):
            try:
                return await factory()
            except Exception as e:  # noqa: BLE001
                last = e
                self.logger.warn(
                    f"[retry] {what}: attempt {k + 1}/{attempts} ({type(e).__name__})")
                await asyncio.sleep(delay)
        raise last

    async def grasp_pose(self) -> Pose:
        """Ask the pick-station where to grasp a box of this height."""
        return pose_from_map(await self.retry(
            "grasp_pose",
            lambda: self.pick_station.do_command(
                {"get_vacuum_pose": {"box_height_mm": self.box_h}}),
        ))

    async def pick_home_pose(self) -> Pose:
        """Ask the pick-station for the safe approach pose above the box."""
        return pose_from_map(await self.retry(
            "pick_home_pose",
            lambda: self.pick_station.do_command(
                {"get_pick_home_pose": {"box_height_mm": self.box_h}}),
        ))

    async def _set_box(self, seq, parent, x, y, z):
        try:
            await self.retry(
                "set_box_transform",
                lambda: self.sequencer.do_command(
                    {"set_box_transform": {
                        "seq": seq, "parent": parent, "x": x, "y": y, "z": z,
                        "o_x": 0, "o_y": 0, "o_z": 1, "theta": 0}}),
                attempts=2,
            )
        except Exception as e:  # the scene is cosmetic; never block motion
            self.logger.warn(f"[viz] set_box_transform failed: {type(e).__name__}")

    async def show_box(self, seq, x, y, z):
        """Render box `seq` resting at the world position (x, y, z)."""
        await self._set_box(seq, "world", x, y, z)

    async def attach_box(self, seq):
        """Render box `seq` riding the gripper, hanging from the cups."""
        await self._set_box(seq, GRIPPER, 0, 0, self.box_h / 2)

    async def clear_boxes(self, count=64):
        """Remove the rendered boxes (empty the pallet).

        The count is a fixed, generous number rather than one derived from the
        current pattern: it has to clear whatever the *previous* pattern drew,
        which may have been larger. Clearing a box that was never drawn costs
        nothing. Note this sends `clear_box_transform`, a different command
        from `_set_box`'s `set_box_transform`.
        """
        for i in range(count):
            try:
                await self.sequencer.do_command({"clear_box_transform": {"seq": i}})
            except Exception:  # noqa: BLE001
                pass
