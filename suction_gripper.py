"""
suction_gripper.py
==================
Modelliert den Sauggreifer:
- Aufnehmen (attach)
- Halten über dem Maze (hold_on_maze / update_hold_position)
- Pressen (press_down / wait_and_press_if_ready)
- Loslassen (detach / release_dynamic)
- Reset
"""

import omni.usd
from pxr import Sdf, UsdPhysics, UsdGeom, Gf, Usd

from .constants import (
    SUCTION_TARGET_PATH, SUCTION_GRIPPER_MESH_PATH,
    SUCTION_JOINT_PATH, PLACE_TARGET_PATH,
    PLACE_OFFSET_X, PLACE_OFFSET_Y, PLACE_OFFSET_Z,
    DECKEL_HALF_HEIGHT, SAFETY_OFFSET,
    DECKEL_START_LOCAL_POS,
)


class SuctionGripper:
    """Kapselt alle USD/Physics-Operationen rund um den Saugnapf."""

    def __init__(self, logger=None):
        self._logger = logger          # Optionaler Logger
        self._active = False           # Joint aktiv?
        self._held = False             # Deckel wird gehalten?
        self._placed = False           # Bereits einmal abgesetzt?
        self._press_offset_z = 0.0     # Aktuelles Press-Delta
        self._saved_local_pos = None   # Deckel-Ursprungsposition (lokale Koordinaten)

    # ---------------------------------------------------------------
    # Logging-Helfer
    # ---------------------------------------------------------------
    def _log(self, msg, level="info"):
        if self._logger:
            self._logger.log(msg, level)
        else:
            print(f"[Suction] {msg}")

    # ---------------------------------------------------------------
    # USD-Helfer
    # ---------------------------------------------------------------
    def _stage(self):
        """Aktuelle USD-Stage."""
        return omni.usd.get_context().get_stage()

    def _find_rigidbody_ancestor(self, prim):
        """Sucht den nächsten Vorfahren mit RigidBodyAPI (für Joint-Body0)."""
        current = prim
        while current and current.IsValid():
            if current.HasAPI(UsdPhysics.RigidBodyAPI):
                return current
            current = current.GetParent()
        return None

    def _get_target_prim(self):
        """Liefert das Deckel-Prim."""
        stage = self._stage()
        return stage.GetPrimAtPath(SUCTION_TARGET_PATH) if stage else None

    # ---------------------------------------------------------------
    # Kinematic-Switch
    # ---------------------------------------------------------------
    def _set_target_kinematic(self, enabled: bool):
        """Schaltet den Deckel zwischen dynamisch und kinematisch."""
        target_prim = self._get_target_prim()
        if not target_prim or not target_prim.IsValid():
            self._log("Deckel nicht gefunden für kinematic switch", "error")
            return False

        # RigidBodyAPI sicherstellen
        if not target_prim.HasAPI(UsdPhysics.RigidBodyAPI):
            rb = UsdPhysics.RigidBodyAPI.Apply(target_prim)
            rb.CreateRigidBodyEnabledAttr(True)

        attr = target_prim.GetAttribute("physics:kinematicEnabled")
        if not attr or not attr.IsValid():
            attr = target_prim.CreateAttribute(
                "physics:kinematicEnabled", Sdf.ValueTypeNames.Bool
            )

        attr.Set(bool(enabled))
        self._log(f"Kinematic {'EIN' if enabled else 'AUS'}", "info")
        return True

    # ---------------------------------------------------------------
    # Startposition speichern / wiederherstellen
    # ---------------------------------------------------------------
    def save_start_position(self):
        """Einmalig beim Extension-Start aufrufen, solange Deckel noch unberührt ist."""
        target_prim = self._get_target_prim()
        if not target_prim or not target_prim.IsValid():
            self._log("Deckel nicht gefunden – Startposition nicht gespeichert", "error")
            return False
        xform = UsdGeom.Xformable(target_prim)
        translate_op = next(
            (op for op in xform.GetOrderedXformOps()
             if op.GetOpType() == UsdGeom.XformOp.TypeTranslate),
            None,
        )
        if translate_op:
            self._saved_local_pos = translate_op.Get()
            self._log(f"Startposition gespeichert: {self._saved_local_pos}", "info")
            return True
        self._log("Kein TranslateOp gefunden – Startposition nicht gespeichert", "info")
        return False

    def _restore_start_position(self):
        """Setzt den Deckel zurück auf die Ursprungsposition.
        Priorität: dynamisch gespeichert > Konstante aus constants.py"""
        target_pos = self._saved_local_pos if self._saved_local_pos is not None \
            else Gf.Vec3d(*DECKEL_START_LOCAL_POS)

        target_prim = self._get_target_prim()
        if not target_prim or not target_prim.IsValid():
            return
        xform = UsdGeom.Xformable(target_prim)
        translate_op = next(
            (op for op in xform.GetOrderedXformOps()
             if op.GetOpType() == UsdGeom.XformOp.TypeTranslate),
            None,
        )
        if translate_op is None:
            translate_op = xform.AddTranslateOp()
        translate_op.Set(Gf.Vec3d(*[float(x) for x in target_pos]))
        self._log(f"Position wiederhergestellt: {target_pos}", "info")

    # ---------------------------------------------------------------
    # Joint-Verwaltung
    # ---------------------------------------------------------------
    def joint_exists(self):
        """True, wenn der FixedJoint vorhanden ist."""
        stage = self._stage()
        return bool(stage) and stage.GetPrimAtPath(SUCTION_JOINT_PATH).IsValid()

    def remove_joint(self):
        """Entfernt den FixedJoint."""
        stage = self._stage()
        if stage and stage.GetPrimAtPath(SUCTION_JOINT_PATH).IsValid():
            stage.RemovePrim(SUCTION_JOINT_PATH)
            self._log(f"Joint entfernt: {SUCTION_JOINT_PATH}", "info")

    # ---------------------------------------------------------------
    def reset(self):
        """Setzt den Gripper komplett zurück: Joint entfernen, Deckel auf Startposition, dynamisch."""
        self.remove_joint()
        self._restore_start_position()
        self._set_target_kinematic(False)
        self._active = False
        self._held = False
        self._placed = False
        self._press_offset_z = 0.0
        self._log("Reset", "info")

    # ---------------------------------------------------------------
    # Positionsberechnung
    # ---------------------------------------------------------------
    def _compute_maze_snap_world(self):
        """Berechnet die Ziel-Weltposition oberhalb des Maze."""
        stage = self._stage()
        if not stage:
            return None

        time = Usd.TimeCode.Default()
        place_prim = stage.GetPrimAtPath(PLACE_TARGET_PATH)
        if not place_prim.IsValid():
            self._log(f"Maze nicht gefunden: {PLACE_TARGET_PATH}", "error")
            return None

        # BoundingBox des Maze bestimmen
        bbox_cache = UsdGeom.BBoxCache(time, [UsdGeom.Tokens.default_])
        world_bbox = bbox_cache.ComputeWorldBound(place_prim)
        bbox_range = world_bbox.ComputeAlignedBox()
        bbox_min, bbox_max = bbox_range.GetMin(), bbox_range.GetMax()

        # Mittelpunkt in X/Y, Oberkante + Sicherheitsabstand in Z
        center_x = (bbox_min[0] + bbox_max[0]) * 0.5
        center_y = (bbox_min[1] + bbox_max[1]) * 0.5

        return Gf.Vec3d(
            float(center_x + PLACE_OFFSET_X),
            float(center_y + PLACE_OFFSET_Y),
            float(
                bbox_max[2] + DECKEL_HALF_HEIGHT + SAFETY_OFFSET
                + PLACE_OFFSET_Z + self._press_offset_z
            ),
        )

    def _set_target_world_position(self, snap_world):
        """Setzt den Deckel auf die übergebene Weltposition."""
        target_prim = self._get_target_prim()
        if not target_prim or not target_prim.IsValid():
            self._log(f"Deckel nicht gefunden: {SUCTION_TARGET_PATH}", "error")
            return False

        time = Usd.TimeCode.Default()
        parent_prim = target_prim.GetParent()
        if not parent_prim.IsValid():
            self._log("Deckel-Parent ungültig", "error")
            return False

        # Welt -> Lokal des Parents transformieren
        parent_world = UsdGeom.Xformable(parent_prim).ComputeLocalToWorldTransform(time)
        local_pos = parent_world.GetInverse().Transform(snap_world)

        # Existenten TranslateOp suchen oder neu anlegen
        xform = UsdGeom.Xformable(target_prim)
        translate_op = next(
            (op for op in xform.GetOrderedXformOps()
             if op.GetOpType() == UsdGeom.XformOp.TypeTranslate),
            None,
        )
        if translate_op is None:
            translate_op = xform.AddTranslateOp()

        translate_op.Set(Gf.Vec3d(*[float(x) for x in local_pos]))
        return True

    # ---------------------------------------------------------------
    # Halten / Update / Press
    # ---------------------------------------------------------------
    def update_hold_position(self):
        """Wird pro SIM-Frame aufgerufen, hält den Deckel über dem Maze."""
        if not self._held:
            return False

        snap_world = self._compute_maze_snap_world()
        if snap_world is None:
            return False

        target_prim = self._get_target_prim()
        if not target_prim or not target_prim.IsValid():
            return False

        # Nur setzen, wenn Differenz relevant ist (Performance)
        cur = UsdGeom.Xformable(target_prim).ComputeLocalToWorldTransform(
            Usd.TimeCode.Default()).ExtractTranslation()
        diff = snap_world - cur
        if diff[0]**2 + diff[1]**2 + diff[2]**2 < 1e-10:
            return True

        return self._set_target_world_position(snap_world)

    def hold_on_maze(self):
        """Aktiviert das Halten über dem Maze."""
        self._held = True
        self._press_offset_z = 0.0
        snap_world = self._compute_maze_snap_world()
        if snap_world is None or not self._set_target_world_position(snap_world):
            return False
        self._set_target_kinematic(True)
        self._log("Deckel wird über Maze gehalten", "ok")
        return True

    def press_down(self, z_down=0.002):
        """Senkt den Deckel um z_down Meter ab."""
        self._press_offset_z = -abs(z_down)
        self._held = True
        snap_world = self._compute_maze_snap_world()
        if snap_world is None or not self._set_target_world_position(snap_world):
            return False
        self._set_target_kinematic(True)
        self._log(f"Deckel um {z_down} m abgesenkt", "ok")
        return True

    def wait_and_press_if_ready(
        self, press_prim_path, target_attr,
        reached_value, z_down=0.002, tolerance=1e-4
    ):
        """Drückt den Deckel runter, sobald Presse Zielposition erreicht hat."""
        if not self._placed:
            return False
        stage = self._stage()
        if not stage:
            return False

        press_prim = stage.GetPrimAtPath(press_prim_path)
        if not press_prim.IsValid():
            self._log(f"Presse-Prim nicht gefunden: {press_prim_path}", "error")
            return False

        # Attribut mit/ohne ":physics:" testen
        attr = press_prim.GetAttribute(target_attr)
        if not attr or not attr.IsValid():
            attr = press_prim.GetAttribute(target_attr.replace(":physics:", ":"))
        if not attr or not attr.IsValid():
            self._log(f"Presse-Attribut nicht gefunden: {target_attr}", "error")
            return False

        current = attr.Get()
        if current is None:
            return False
        if abs(float(current) - float(reached_value)) <= float(tolerance):
            return self.press_down(z_down=z_down)
        return False

    # ---------------------------------------------------------------
    # Loslassen
    # ---------------------------------------------------------------
    def release_dynamic(self):
        """Setzt den Deckel wieder auf 'dynamisch' (Physik aktiv)."""
        if self._set_target_kinematic(False):
            self._held = False
            self._placed = False
            self._press_offset_z = 0.0
            self._log("Deckel wieder dynamisch", "info")
            return True
        return False

    def detach(self):
        """Sauggreifer aus: Joint entfernen, Deckel folgt Maze."""
        self.hold_on_maze()
        self.remove_joint()
        self._active = False
        self._held = True
        self._placed = True
        self._log("Sauggreifer AUS - Deckel folgt Maze", "ok")

    def attach(self):
        """Sauggreifer ein: FixedJoint zwischen Greifer-Body und Deckel."""
        stage = self._stage()
        if not stage:
            return False
        time = Usd.TimeCode.Default()

        gripper_prim = stage.GetPrimAtPath(SUCTION_GRIPPER_MESH_PATH)
        target_prim = stage.GetPrimAtPath(SUCTION_TARGET_PATH)
        if not gripper_prim.IsValid() or not target_prim.IsValid():
            self._log("Gripper/Deckel-Prim nicht gefunden", "error")
            return False

        body0_prim = self._find_rigidbody_ancestor(gripper_prim)
        if body0_prim is None:
            self._log("Kein RigidBody-Vorfahre gefunden", "error")
            return False

        # Sicherstellen, dass Deckel RigidBody hat und dynamisch ist
        if not target_prim.HasAPI(UsdPhysics.RigidBodyAPI):
            UsdPhysics.RigidBodyAPI.Apply(target_prim).CreateRigidBodyEnabledAttr(True)
        self._set_target_kinematic(False)

        # Deckel an Greiferposition teleportieren
        gripper_world = UsdGeom.Xformable(gripper_prim).ComputeLocalToWorldTransform(time)
        gripper_world_pos = gripper_world.ExtractTranslation()
        parent_prim = target_prim.GetParent()
        parent_world = UsdGeom.Xformable(parent_prim).ComputeLocalToWorldTransform(time)
        local_pos = parent_world.GetInverse().Transform(gripper_world_pos)

        xform = UsdGeom.Xformable(target_prim)
        translate_op = next(
            (op for op in xform.GetOrderedXformOps()
             if op.GetOpType() == UsdGeom.XformOp.TypeTranslate),
            None,
        )
        if translate_op is None:
            translate_op = xform.AddTranslateOp()
        translate_op.Set(Gf.Vec3d(*[float(x) for x in local_pos]))

        # Kontaktpunkt im Body0-Koordinatensystem berechnen
        body0_world = UsdGeom.Xformable(body0_prim).ComputeLocalToWorldTransform(time)
        contact_in_body0 = body0_world.GetInverse().Transform(gripper_world_pos)

        lp0 = Gf.Vec3f(float(contact_in_body0[0]), float(contact_in_body0[1]), float(contact_in_body0[2]))
        self._log(
            f"[Joint-Debug] Body0: {body0_prim.GetPath()} | "
            f"Greifer-Welt: ({gripper_world_pos[0]:.3f}, {gripper_world_pos[1]:.3f}, {gripper_world_pos[2]:.3f}) | "
            f"LocalPos0: ({lp0[0]:.3f}, {lp0[1]:.3f}, {lp0[2]:.3f})",
            "info"
        )

        # Joint erzeugen
        self.remove_joint()
        joint = UsdPhysics.FixedJoint.Define(stage, SUCTION_JOINT_PATH)
        joint.GetBody0Rel().SetTargets([Sdf.Path(str(body0_prim.GetPath()))])
        joint.GetBody1Rel().SetTargets([Sdf.Path(SUCTION_TARGET_PATH)])

        # Lokaler Anker am Greifer – vollständig aus Szene berechnet
        joint.GetLocalPos0Attr().Set(lp0)
        joint.GetLocalRot0Attr().Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))
        joint.GetLocalPos1Attr().Set(Gf.Vec3f(0.0, 0.0, 0.0))
        joint.GetLocalRot1Attr().Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))

        self._active = True
        self._held = False
        self._placed = False
        self._press_offset_z = 0.0
        self._log("Sauggreifer EIN ✅", "ok")
        return True

    def toggle(self):
        """Wechselt zwischen attach/detach."""
        if self.joint_exists():
            self.detach()
            return False
        return self.attach()

    @property
    def is_active(self):
        return self.joint_exists()
