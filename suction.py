import omni.usd
from pxr import Sdf, UsdPhysics, UsdGeom, Gf, Usd


SUCTION_TARGET_PATH = "/World/Production_Line/Deckelmagazin/Deckel"
SUCTION_GRIPPER_MESH_PATH = "/World/Production_Line/Schwenkarm_Deckel/Schwenkarm_Deckel_move_translatory/Schwenkarm_Deckel_move_rotatory/tn__Saubnapf1_zH/tn__Volumenkrper2_gm2/Mesh"
SUCTION_JOINT_PATH = "/World/Production_Line/SaugnapfDeckelJoint"

PLACE_TARGET_PATH = "/World/Production_Line/Mazemagazin/Maze"

PLACE_OFFSET_X = 0.0
PLACE_OFFSET_Y = 0.0
PLACE_OFFSET_Z = 0.0

DECKEL_HALF_HEIGHT = 0.0025   # 5 mm / 2
SAFETY_OFFSET = 0.0005        # 0.5 mm


class SuctionGripper:
    def __init__(self, ext=None):
        self._ext = ext
        self._active = False
        self._held = False
        self._press_offset_z = 0.0

    def _log(self, msg, level="info"):
        if self._ext and hasattr(self._ext, "_log"):
            self._ext._log(msg, level)
        else:
            print(f"[Suction] {msg}")

    def _stage(self):
        return omni.usd.get_context().get_stage()

    def _find_rigidbody_ancestor(self, prim):
        current = prim
        while current and current.IsValid():
            if current.HasAPI(UsdPhysics.RigidBodyAPI):
                return current
            current = current.GetParent()
        return None

    def _get_target_prim(self):
        stage = self._stage()
        if not stage:
            return None
        return stage.GetPrimAtPath(SUCTION_TARGET_PATH)

    def _set_target_kinematic(self, enabled: bool):
        target_prim = self._get_target_prim()
        if not target_prim or not target_prim.IsValid():
            self._log("Deckel nicht gefunden für kinematic switch", "error")
            return False

        if not target_prim.HasAPI(UsdPhysics.RigidBodyAPI):
            rb = UsdPhysics.RigidBodyAPI.Apply(target_prim)
            rb.CreateRigidBodyEnabledAttr(True)

        attr = target_prim.GetAttribute("physics:kinematicEnabled")
        if not attr or not attr.IsValid():
            attr = target_prim.CreateAttribute("physics:kinematicEnabled", Sdf.ValueTypeNames.Bool)

        attr.Set(bool(enabled))
        self._log(f"Kinematic {'EIN' if enabled else 'AUS'}", "info")
        return True

    def joint_exists(self):
        stage = self._stage()
        if not stage:
            return False
        return stage.GetPrimAtPath(SUCTION_JOINT_PATH).IsValid()

    def remove_joint(self):
        stage = self._stage()
        if not stage:
            return
        if stage.GetPrimAtPath(SUCTION_JOINT_PATH).IsValid():
            stage.RemovePrim(SUCTION_JOINT_PATH)
            self._log(f"Joint entfernt: {SUCTION_JOINT_PATH}", "info")

    def reset(self):
        self.remove_joint()
        self._active = False
        self._held = False
        self._press_offset_z = 0.0
        self._log("Reset", "info")

    def _compute_maze_snap_world(self):
        stage = self._stage()
        if not stage:
            self._log("Keine Stage vorhanden", "error")
            return None

        time = Usd.TimeCode.Default()
        place_prim = stage.GetPrimAtPath(PLACE_TARGET_PATH)
        if not place_prim.IsValid():
            self._log(f"Maze nicht gefunden: {PLACE_TARGET_PATH}", "error")
            return None

        bbox_cache = UsdGeom.BBoxCache(time, [UsdGeom.Tokens.default_])
        world_bbox = bbox_cache.ComputeWorldBound(place_prim)
        bbox_range = world_bbox.ComputeAlignedBox()

        bbox_min = bbox_range.GetMin()
        bbox_max = bbox_range.GetMax()

        center_x = (bbox_min[0] + bbox_max[0]) * 0.5
        center_y = (bbox_min[1] + bbox_max[1]) * 0.5

        snap_world = Gf.Vec3d(
            float(center_x + PLACE_OFFSET_X),
            float(center_y + PLACE_OFFSET_Y),
            float(bbox_max[2] + DECKEL_HALF_HEIGHT + SAFETY_OFFSET + PLACE_OFFSET_Z + self._press_offset_z)
        )

        return snap_world

    def _set_target_world_position(self, snap_world):
        target_prim = self._get_target_prim()
        if not target_prim or not target_prim.IsValid():
            self._log(f"Deckel nicht gefunden: {SUCTION_TARGET_PATH}", "error")
            return False

        time = Usd.TimeCode.Default()

        parent_prim = target_prim.GetParent()
        if not parent_prim.IsValid():
            self._log("Deckel-Parent ungültig", "error")
            return False

        parent_world = UsdGeom.Xformable(parent_prim).ComputeLocalToWorldTransform(time)
        parent_inv = parent_world.GetInverse()
        local_pos = parent_inv.Transform(snap_world)

        xform = UsdGeom.Xformable(target_prim)
        translate_op = None
        for op in xform.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                translate_op = op
                break

        if translate_op is None:
            translate_op = xform.AddTranslateOp()

        translate_op.Set(Gf.Vec3d(
            float(local_pos[0]),
            float(local_pos[1]),
            float(local_pos[2])
        ))

        return True

    def update_hold_position(self):
        """
        Muss regelmäßig aufgerufen werden, damit der Deckel dem Maze folgt.
        """
        if not self._held:
            return False

        snap_world = self._compute_maze_snap_world()
        if snap_world is None:
            return False

        ok = self._set_target_world_position(snap_world)
        return ok

    def hold_on_maze(self):
        self._held = True
        self._press_offset_z = 0.0

        snap_world = self._compute_maze_snap_world()
        if snap_world is None:
            return False

        ok = self._set_target_world_position(snap_world)
        if not ok:
            return False

        self._set_target_kinematic(True)
        self._log("Deckel wird über Maze gehalten", "ok")
        return True

    def press_down(self, z_down=0.002):
        """
        z_down in Metern. 0.002 = 2 mm
        """
        self._press_offset_z = -abs(z_down)
        self._held = True

        snap_world = self._compute_maze_snap_world()
        if snap_world is None:
            return False

        ok = self._set_target_world_position(snap_world)
        if not ok:
            return False

        self._set_target_kinematic(True)
        self._log(f"Deckel um {z_down} m abgesenkt", "ok")
        return True

    def wait_and_press_if_ready(self, press_prim_path, target_attr, reached_value, z_down=0.002, tolerance=1e-4):
        """
        Prüft ein Presse-Attribut. Wenn Zielwert erreicht ist:
        Deckel 2 mm oder gewünschten Wert nach unten setzen.
        """
        stage = self._stage()
        if not stage:
            self._log("Keine Stage vorhanden", "error")
            return False

        press_prim = stage.GetPrimAtPath(press_prim_path)
        if not press_prim.IsValid():
            self._log(f"Presse-Prim nicht gefunden: {press_prim_path}", "error")
            return False

        attr = press_prim.GetAttribute(target_attr)
        if not attr or not attr.IsValid():
            alt_name = target_attr.replace(":physics:", ":")
            attr = press_prim.GetAttribute(alt_name)

        if not attr or not attr.IsValid():
            self._log(f"Presse-Attribut nicht gefunden: {target_attr}", "error")
            return False

        current = attr.Get()
        self._log(f"Presse Attributwert: {current}", "info")

        if current is None:
            return False

        if abs(float(current) - float(reached_value)) <= float(tolerance):
            self._log("Presse Zielwert erreicht -> Deckel wird abgesenkt", "ok")
            return self.press_down(z_down=z_down)

        return False

    def release_dynamic(self):
        ok = self._set_target_kinematic(False)
        if ok:
            self._held = False
            self._press_offset_z = 0.0
            self._log("Deckel wieder dynamisch", "info")
        return ok

    def detach(self):
        self.hold_on_maze()
        self.remove_joint()
        self._active = False
        self._held = True
        self._log("Sauggreifer AUS - Deckel folgt Maze", "ok")

    def attach(self):
        stage = self._stage()
        if not stage:
            self._log("Keine Stage vorhanden", "error")
            return False

        time = Usd.TimeCode.Default()

        gripper_prim = stage.GetPrimAtPath(SUCTION_GRIPPER_MESH_PATH)
        target_prim = stage.GetPrimAtPath(SUCTION_TARGET_PATH)

        if not gripper_prim.IsValid():
            self._log(f"Greifer-Mesh nicht gefunden: {SUCTION_GRIPPER_MESH_PATH}", "error")
            return False

        if not target_prim.IsValid():
            self._log(f"Deckel nicht gefunden: {SUCTION_TARGET_PATH}", "error")
            return False

        body0_prim = self._find_rigidbody_ancestor(gripper_prim)
        if body0_prim is None:
            self._log("Kein RigidBody-Vorfahre für Greifer gefunden", "error")
            return False

        body0_path = str(body0_prim.GetPath())
        self._log(f"Body0 gefunden: {body0_path}", "info")

        if not target_prim.HasAPI(UsdPhysics.RigidBodyAPI):
            rb = UsdPhysics.RigidBodyAPI.Apply(target_prim)
            rb.CreateRigidBodyEnabledAttr(True)
            self._log("RigidBodyAPI auf Deckel angewendet", "info")

        self._set_target_kinematic(False)

        gripper_world = UsdGeom.Xformable(gripper_prim).ComputeLocalToWorldTransform(time)
        gripper_world_pos = gripper_world.ExtractTranslation()

        parent_prim = target_prim.GetParent()
        if not parent_prim.IsValid():
            self._log("Deckel-Parent ungültig", "error")
            return False

        parent_world = UsdGeom.Xformable(parent_prim).ComputeLocalToWorldTransform(time)
        parent_inv = parent_world.GetInverse()
        local_pos = parent_inv.Transform(gripper_world_pos)

        self._log(f"Gripper World: {gripper_world_pos}", "info")
        self._log(f"Deckel Local Ziel: {local_pos}", "info")

        xform = UsdGeom.Xformable(target_prim)
        translate_op = None
        for op in xform.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                translate_op = op
                break

        if translate_op is None:
            translate_op = xform.AddTranslateOp()

        translate_op.Set(Gf.Vec3d(
            float(local_pos[0]),
            float(local_pos[1]),
            float(local_pos[2])
        ))

        target_world_after = UsdGeom.Xformable(target_prim).ComputeLocalToWorldTransform(time)
        diff = target_world_after.ExtractTranslation() - gripper_world_pos

        self._log(f"Target World nach Teleport: {target_world_after.ExtractTranslation()}", "info")
        self._log(f"Diff zu Gripper: {diff}", "info")

        body0_world = UsdGeom.Xformable(body0_prim).ComputeLocalToWorldTransform(time)
        body0_inv = body0_world.GetInverse()
        contact_in_body0 = body0_inv.Transform(gripper_world_pos)

        self.remove_joint()

        joint = UsdPhysics.FixedJoint.Define(stage, SUCTION_JOINT_PATH)
        joint.GetBody0Rel().SetTargets([Sdf.Path(body0_path)])
        joint.GetBody1Rel().SetTargets([Sdf.Path(SUCTION_TARGET_PATH)])

        joint.GetLocalPos0Attr().Set(Gf.Vec3f(
            float(contact_in_body0[0]),
            -29.0,
            49.0
        ))
        joint.GetLocalRot0Attr().Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))

        joint.GetLocalPos1Attr().Set(Gf.Vec3f(0.0, 0.0, 0.0))
        joint.GetLocalRot1Attr().Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))

        self._active = True
        self._held = False
        self._press_offset_z = 0.0
        self._log("Sauggreifer EIN ✅", "ok")
        return True

    def toggle(self):
        if self.joint_exists():
            self.detach()
            return False
        else:
            ok = self.attach()
            return ok

    @property
    def is_active(self):
        return self.joint_exists()