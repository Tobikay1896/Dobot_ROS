import omni.ext
import omni.ui as ui
import omni.usd
import omni.kit.app
import omni.timeline
import os
import json
import asyncio
import aiohttp
from datetime import datetime
from pxr import Sdf, UsdPhysics, UsdGeom, Gf, Usd


# -------------------------------------------------------------------------
# Hilfsfunktionen
# -------------------------------------------------------------------------
async def _cancel_and_await(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# -------------------------------------------------------------------------
# Farben
# -------------------------------------------------------------------------
CLR_BG_DARK = 0xFF0A0F1A
CLR_BG_MID = 0xFF101828
CLR_BG_ROW_A = 0xFF121E30
CLR_BG_ROW_B = 0xFF162438
CLR_BG_HEADER = 0xFF0D1420
CLR_ACCENT = 0xFF4A9EFF
CLR_GREEN = 0xFF4ADE80
CLR_RED = 0xFFEF6B6B
CLR_YELLOW = 0xFFE0B040
CLR_ORANGE = 0xFFE08040
CLR_TEXT = 0xFFD0D8E8
CLR_TEXT_DIM = 0xFF607090
CLR_TEXT_FAINT = 0xFF405070
CLR_BORDER = 0xFF1A2840

MAX_LOG_LINES = 80


# -------------------------------------------------------------------------
# MQTT-Konfiguration (entsprechend deinem Unity Setup)
# -------------------------------------------------------------------------
MQTT_BROKER = "digitaltwinservice.de"
MQTT_PORT = 1883
MQTT_KEEPALIVE = 60


# -------------------------------------------------------------------------
# SuctionGripper Klasse
# -------------------------------------------------------------------------
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
        self._placed = False
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
        self._placed = False
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
        Nur aktualisieren, wenn Abweichung relevant ist.
        """
        if not self._held:
            return False

        snap_world = self._compute_maze_snap_world()
        if snap_world is None:
            return False

        target_prim = self._get_target_prim()
        if not target_prim or not target_prim.IsValid():
            return False

        time = Usd.TimeCode.Default()
        current_world = UsdGeom.Xformable(target_prim).ComputeLocalToWorldTransform(time)
        current_pos = current_world.ExtractTranslation()

        diff = snap_world - current_pos
        dist2 = diff[0] * diff[0] + diff[1] * diff[1] + diff[2] * diff[2]

        # Nur bei relevanter Abweichung wirklich setzen
        if dist2 < 1e-10:
            return True

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
        Pressenlogik erst aktiv, nachdem einmal angesaugt und wieder losgelassen wurde.
        """
        if not self._placed:
            return False

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

        if current is None:
            return False

        if abs(float(current) - float(reached_value)) <= float(tolerance):
            return self.press_down(z_down=z_down)

        return False

    def release_dynamic(self):
        ok = self._set_target_kinematic(False)
        if ok:
            self._held = False
            self._placed = False
            self._press_offset_z = 0.0
            self._log("Deckel wieder dynamisch", "info")
        return ok

    def detach(self):
        self.hold_on_maze()
        self.remove_joint()
        self._active = False
        self._held = True
        self._placed = True
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
        self._placed = False
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


# -------------------------------------------------------------------------
# Routine Funktionen
# -------------------------------------------------------------------------
def _find_node(ext, node_id: str):
    for node in ext.nodes:
        if node.get("node_id") == node_id:
            return node
    return None

def _set_node(ext, node_id: str, value: bool):
    """Setzt einen Toggle/Suction-Node direkt."""
    node = _find_node(ext, node_id)
    if not node:
        ext._log(f"[Routine] Node nicht gefunden: {node_id}", "error")
        return False

    if node_id == "Sauggreifer_EIN":
        current = ext._suction.is_active
        if value and not current:
            ext._suction.attach()
            ext.node_values[node_id] = True
            ext._set_node_display(node_id, True)
        elif not value and current:
            ext._suction.detach()
            ext.node_values[node_id] = False
            ext._set_node_display(node_id, False)
        return True

    ext.node_values[node_id] = value
    ext._set_node_display(node_id, value)
    ext._apply_usd_for_node(node_id, value)
    ext._log(f"[Routine] {node_id} = {value}", "ok")
    return True

def _trigger_impulse(ext, node_id: str):
    """Führt einen Step-Impuls für einen impulse-Mode Node aus."""
    node = _find_node(ext, node_id)
    if not node:
        ext._log(f"[Routine] Impulse-Node nicht gefunden: {node_id}", "error")
        return
    if node.get("mode") != "impulse":
        ext._log(f"[Routine] Node {node_id} ist kein impulse-Mode", "error")
        return
    ext._execute_step_impulse(node_id, node)
    ext._log(f"[Routine] Impulse ausgelöst: {node_id}", "ok")

async def _step(ext, description: str, delay: float = 1.0):
    """Loggt einen Schritt und wartet."""
    ext._log(f"[Auto] ▶ {description}", "info")
    await asyncio.sleep(delay)


# -------------------------------------------------------------------------
# Hauptklasse
# -------------------------------------------------------------------------
class MyExtension(omni.ext.IExt):
    def on_startup(self, ext_id):
        self._ext_id = ext_id

        current_folder = os.path.dirname(__file__)
        self.json_path = os.path.join(current_folder, "nodes_db.json")

        self.api_url_get = "https://digitaltwinservice.de/api/Database/GetValue"
        self.api_url_set = "https://digitaltwinservice.de/api/Database/SetValue"
        self.api_key = "2b56f658-b11f-4067-9537-631bf27a30f0"

        self.node_labels = {}
        self.node_values = {}
        self.nodes = []
        self._is_running = True
        self._active_tasks = []
        self._log_lines = []
        self._poll_count = 0

        self._impulse_positions = {}
        self._impulse_armed = {}
        self._velocity_running = {}
        self._timeline_sub = None

        self._sim_mode = True
        self._poll_task = None
        self._sim_update_task = None
        self._node_buttons = []
        self._btn_gesamt = None  # Gesamtprozess Button

        # MQTT-Attribute
        self._mqtt_client = None
        self._mqtt_connected = False

        self._build_ui()
        self.load_nodes_from_json()
        self._subscribe_timeline()
        self._suction = SuctionGripper(self)

        # Deckel auf kinematicEnabled = false setzen
        self._set_deckel_kinematic(False)
        
        self._log("Extension gestartet", "info")
        self._log("SIM-Modus aktiv", "info")

    # =================================================================
    # UI
    # =================================================================
    def _build_ui(self):
        self._window = ui.Window("Maze Runner", width=880, height=520)
        self._window.deferred_dock_in("Property")

        with self._window.frame:
            with ui.VStack(spacing=0):

                # HEADER
                with ui.ZStack(height=50):
                    ui.Rectangle(style={"background_color": CLR_BG_DARK})
                    with ui.HStack():
                        ui.Spacer(width=14)
                        with ui.VStack(spacing=1):
                            ui.Spacer(height=9)
                            ui.Label("MAZE RUNNER", style={"font_size": 17, "color": CLR_TEXT}, height=22)
                            ui.Label("Web-API Control Center", style={"font_size": 11, "color": CLR_TEXT_DIM}, height=14)
                        ui.Spacer()
                        with ui.VStack(width=180, spacing=1):
                            ui.Spacer(height=10)
                            self._status_label = ui.Label(
                                "SIM-Modus aktiv",
                                style={"font_size": 11, "color": CLR_GREEN},
                                height=16,
                                alignment=ui.Alignment.RIGHT
                            )
                            self._poll_label = ui.Label(
                                "Polls: 0",
                                style={"font_size": 10, "color": CLR_TEXT_FAINT},
                                height=14,
                                alignment=ui.Alignment.RIGHT
                            )
                        ui.Spacer(width=14)

                ui.Line(style={"color": CLR_BORDER}, height=1)

                # TOOLBAR
                with ui.ZStack(height=34):
                    ui.Rectangle(style={"background_color": CLR_BG_MID})
                    with ui.HStack(spacing=6):
                        ui.Spacer(width=10)

                        btn_ref = ui.Button("Refresh JSON", width=120, height=24)
                        btn_ref.set_style({
                            "background_color": 0xFF1A2E48,
                            "border_radius": 3,
                            "font_size": 11,
                            "color": CLR_TEXT
                        })
                        btn_ref.set_clicked_fn(self.load_nodes_from_json)

                        btn_rst = ui.Button("Restart Extension", width=130, height=24)
                        btn_rst.set_style({
                            "background_color": 0xFF2A1520,
                            "border_radius": 3,
                            "font_size": 11,
                            "color": CLR_RED
                        })
                        btn_rst.set_clicked_fn(self._restart_extension)

                        self._sim_btn = ui.Button("→ LIVE", width=90, height=24)
                        self._sim_btn.set_style({
                            "background_color": 0xFF0D2A0D,
                            "border_radius": 3,
                            "font_size": 11,
                            "color": CLR_GREEN
                        })
                        self._sim_btn.set_clicked_fn(self._toggle_sim_mode)

                        ui.Spacer()
                        self._node_count_label = ui.Label(
                            "",
                            style={"font_size": 10, "color": CLR_TEXT_FAINT},
                            width=70,
                            alignment=ui.Alignment.RIGHT
                        )
                        ui.Spacer(width=14)

                ui.Line(style={"color": CLR_BORDER}, height=1)

                # COLUMN HEADERS
                with ui.ZStack(height=22):
                    ui.Rectangle(style={"background_color": CLR_BG_HEADER})
                    with ui.HStack():
                        ui.Spacer(width=14)
                        ui.Label("Node", style={"font_size": 10, "color": CLR_TEXT_FAINT}, width=180)
                        ui.Label("Mode", style={"font_size": 10, "color": CLR_TEXT_FAINT}, width=90)
                        ui.Label("Status", style={"font_size": 10, "color": CLR_TEXT_FAINT}, width=100)
                        ui.Spacer()
                        ui.Label("Action", style={"font_size": 10, "color": CLR_TEXT_FAINT}, width=80, alignment=ui.Alignment.CENTER)
                        ui.Spacer(width=14)

                ui.Line(style={"color": CLR_BORDER}, height=1)

                # NODE LIST
                with ui.ScrollingFrame(style={"background_color": CLR_BG_MID}):
                    self._list_container = ui.VStack(spacing=0)

                ui.Line(style={"color": CLR_BORDER}, height=1)

                # GESAMTPROZESS BUTTON (immer sichtbar)
                with ui.ZStack(height=34):
                    ui.Rectangle(style={"background_color": CLR_BG_MID})
                    with ui.HStack(spacing=6):
                        ui.Spacer(width=10)
                        self._btn_gesamt = ui.Button("▶ Gesamtprozess", width=150, height=24)
                        self._btn_gesamt.set_style({
                            "background_color": 0xFF0D2A0D,
                            "border_radius": 3,
                            "font_size": 11,
                            "color": CLR_GREEN
                        })
                        self._btn_gesamt.set_clicked_fn(self._start_gesamtprozess)
                        ui.Spacer()

                ui.Line(style={"color": CLR_BORDER}, height=1)

                # LOG HEADER
                with ui.ZStack(height=22):
                    ui.Rectangle(style={"background_color": CLR_BG_DARK})
                    with ui.HStack():
                        ui.Spacer(width=14)
                        ui.Label("Log", style={"font_size": 10, "color": CLR_TEXT_FAINT}, width=40)
                        ui.Spacer()
                        btn_clr = ui.Button("Clear", width=48, height=16)
                        btn_clr.set_style({
                            "background_color": 0xFF152030,
                            "border_radius": 2,
                            "font_size": 9,
                            "color": CLR_TEXT_FAINT
                        })
                        btn_clr.set_clicked_fn(self._clear_log)
                        ui.Spacer(width=14)

                # LOG PANEL
                with ui.ScrollingFrame(height=110, style={"background_color": 0xFF080C14}):
                    self._log_container = ui.VStack(spacing=0)

    # =================================================================
    # LOGGER
    # =================================================================
    def _log(self, message, level="log"):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_lines.append((ts, message, level))
        if len(self._log_lines) > MAX_LOG_LINES:
            self._log_lines = self._log_lines[-MAX_LOG_LINES:]
        self._rebuild_log()
        print(f"[MazeRunner] [{ts}] {message}")

    def _rebuild_log(self):
        if not hasattr(self, "_log_container"):
            return
        self._log_container.clear()
        color_map = {"log": CLR_TEXT_FAINT, "info": CLR_ACCENT, "error": CLR_RED, "ok": CLR_GREEN}
        with self._log_container:
            for ts, msg, level in self._log_lines:
                clr = color_map.get(level, CLR_TEXT_FAINT)
                ui.Label(f"  {ts}   {msg}", style={"font_size": 10, "color": clr}, height=14)

    def _clear_log(self):
        self._log_lines = []
        self._rebuild_log()

    # =================================================================
    # TIMELINE
    # =================================================================
    def _subscribe_timeline(self):
        timeline = omni.timeline.get_timeline_interface()
        self._timeline_sub = timeline.get_timeline_event_stream().create_subscription_to_pop(
            self._on_timeline_event
        )

    def _on_timeline_event(self, event):
        if event.type == int(omni.timeline.TimelineEventType.PLAY):
            if self._sim_update_task and not self._sim_update_task.done():
                self._sim_update_task.cancel()
            self._sim_update_task = asyncio.ensure_future(self._sim_update_loop())
            self._active_tasks.append(self._sim_update_task)

        elif event.type == int(omni.timeline.TimelineEventType.STOP):
            self._on_sim_stop()

    def _on_sim_stop(self):
        self._log("Simulation gestoppt", "info")

        if self._sim_update_task and not self._sim_update_task.done():
            self._sim_update_task.cancel()
            self._sim_update_task = None

        self._suction.reset()
        self._reset_all_to_zero()

    async def _sim_update_loop(self):
        """
        Läuft immer während die Simulation PLAY ist.
        Hält den Deckel über dem Maze und prüft die Presse.
        """
        try:
            while self._is_running:
                try:
                    self._suction.update_hold_position()

                    self._suction.wait_and_press_if_ready(
                        press_prim_path="/World/Production_Line/Presse/PrismaticJoint",
                        target_attr="drive:linear:physics:targetPosition",
                        reached_value=-0.035,
                        z_down=0.002,
                        tolerance=1e-4
                    )

                    await asyncio.sleep(0.05)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._log(f"SIM Update Fehler: {e}", "error")
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    def _reset_all_to_zero(self):
        self._log("Simulation gestartet - setze alle Werte auf 0", "info")
        self._suction.reset()

        stage = omni.usd.get_context().get_stage()
        if not stage:
            return

        for node_id in self._impulse_positions:
            self._impulse_positions[node_id] = 0.0
        for node_id in self._impulse_armed:
            self._impulse_armed[node_id] = True
        for node_id in self._velocity_running:
            self._velocity_running[node_id] = False

        for node in self.nodes:
            node_id = node.get("node_id", "")
            mode = node.get("mode", "toggle")

            self._set_usd_attr(stage, node, 0.0)
            self.node_values[node_id] = False

            if node_id in self.node_labels:
                if mode == "impulse":
                    self.node_labels[node_id].text = "  0 deg"
                    self.node_labels[node_id].set_style({"font_size": 12, "color": CLR_TEXT_DIM})
                elif mode == "velocity_impulse":
                    self.node_labels[node_id].text = "  READY"
                    self.node_labels[node_id].set_style({"font_size": 12, "color": CLR_TEXT_DIM})
                else:
                    self.node_labels[node_id].text = "  FALSE"
                    self.node_labels[node_id].set_style({"font_size": 12, "color": CLR_RED})

        self._log("Alle Werte auf 0 gesetzt", "ok")

    # =================================================================
    # JSON
    # =================================================================
    def load_nodes_from_json(self):
        if not hasattr(self, "_list_container"):
            return
        self._list_container.clear()
        self.node_labels.clear()
        self._impulse_positions.clear()
        self._impulse_armed.clear()
        self._velocity_running.clear()
        self._node_buttons = []

        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                self.nodes = json.load(f).get("nodes", [])
        except Exception as e:
            self._log(f"JSON Fehler: {e}", "error")
            return

        self._log(f"JSON geladen: {len(self.nodes)} Nodes", "info")
        if hasattr(self, "_node_count_label"):
            self._node_count_label.text = f"{len(self.nodes)} Nodes"

        for node in self.nodes:
            node_id = node.get("node_id", "")
            mode = node.get("mode", "toggle")
            if mode == "impulse":
                self._impulse_positions[node_id] = 0.0
                self._impulse_armed[node_id] = True
            elif mode == "velocity_impulse":
                self._velocity_running[node_id] = False
                self._impulse_armed[node_id] = True

        mode_labels = {
            "toggle": ("TOGGLE", CLR_TEXT_DIM),
            "impulse": ("STEP", CLR_ACCENT),
            "velocity_impulse": ("VEL-IMP", CLR_ORANGE),
        }

        with self._list_container:
            for idx, node in enumerate(self.nodes):
                node_id = node.get("node_id", "")
                display = node.get("display_name", node_id)
                mode = node.get("mode", "toggle")
                row_bg = CLR_BG_ROW_A if idx % 2 == 0 else CLR_BG_ROW_B
                mode_text, mode_color = mode_labels.get(mode, ("?", CLR_TEXT_FAINT))

                with ui.ZStack(height=32):
                    ui.Rectangle(style={"background_color": row_bg})
                    with ui.HStack():
                        ui.Spacer(width=14)
                        ui.Label(display, style={"font_size": 12, "color": CLR_TEXT}, width=180)
                        ui.Label(mode_text, style={"font_size": 10, "color": mode_color}, width=90)
                        lbl = ui.Label("  --", style={"font_size": 12, "color": CLR_TEXT_FAINT}, width=100)
                        self.node_labels[node_id] = lbl
                        ui.Spacer()

                        btn_text = "Trigger" if mode in ("impulse", "velocity_impulse") else "Toggle"
                        btn = ui.Button(btn_text, width=70, height=22)
                        btn.set_style({"background_color": 0xFF1A2840, "border_radius": 3, "font_size": 11, "color": CLR_TEXT_DIM})
                        btn.set_clicked_fn(lambda n=node_id: self.on_control_clicked(n))
                        self._node_buttons.append(btn)
                        ui.Spacer(width=14)

    # =================================================================
    # DECKEL KINEMATIC
    # =================================================================
    def _set_deckel_kinematic(self, enabled: bool):
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return

        prim = stage.GetPrimAtPath(SUCTION_TARGET_PATH)
        if not prim or not prim.IsValid():
            self._log(f"Deckel-Prim nicht gefunden: {SUCTION_TARGET_PATH}", "error")
            return

        if not prim.HasAPI(UsdPhysics.RigidBodyAPI):
            UsdPhysics.RigidBodyAPI.Apply(prim)

        attr = prim.GetAttribute("physics:kinematicEnabled")
        if not attr or not attr.IsValid():
            attr = prim.CreateAttribute("physics:kinematicEnabled", Sdf.ValueTypeNames.Bool)

        attr.Set(enabled)
        state = "True (kinematisch)" if enabled else "False (Physik aktiv)"
        self._log(f"Deckel kinematicEnabled = {state}", "ok")

    # =================================================================
    # MODE
    # =================================================================
    def _toggle_sim_mode(self):
        self._sim_mode = not self._sim_mode

        if self._sim_mode:
            if self._poll_task and not self._poll_task.done():
                self._poll_task.cancel()
            self._poll_task = None

            self._sim_btn.text = "→ LIVE"
            self._sim_btn.set_style({
                "background_color": 0xFF0D2A0D,
                "border_radius": 3,
                "font_size": 11,
                "color": CLR_GREEN,
            })
            self._set_status_text("SIM-Modus aktiv", CLR_GREEN)
            self._log("→ SIM-Modus | Kein API-Polling", "ok")
            
            # Deckel auf kinematicEnabled = False setzen
            self._set_deckel_kinematic(False)

        else:
            # MQTT-Client starten
            self._start_mqtt()
            self._sim_btn.text = "→ SIM"
            self._sim_btn.set_style({
                "background_color": 0xFF2A0D0D,
                "border_radius": 3,
                "font_size": 11,
                "color": CLR_RED,
            })
            self._set_status_text("LIVE aktiv", CLR_RED)
            self._log("→ LIVE-Modus | MQTT-Polling gestartet", "info")

    # =================================================================
    # MQTT FUNCTIONS
    # =================================================================
    def _start_mqtt(self):
        self._log("MQTT Client wird gestartet...", "info")
        try:
            import paho.mqtt.client as mqtt
            
            # MQTT Topics für alle Nodes (entsprechend deinem Unity Setup)
            topics = [f"/PlcNode/Get/{node.get('node_id')}" for node in self.nodes]
            self._log(f"Abonnierte MQTT Topics: {topics}", "info")
            
            # MQTT Client erstellen
            self._mqtt_client = mqtt.Client()
            self._mqtt_client.on_connect = self._on_mqtt_connect
            self._mqtt_client.on_disconnect = self._on_mqtt_disconnect
            self._mqtt_client.on_message = self._on_mqtt_message
            
            # Verbindung herstellen
            self._mqtt_client.connect(MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE)
            self._mqtt_client.loop_start()
            
            # Abonnieren der Topics
            for topic in topics:
                self._mqtt_client.subscribe(topic)
                self._log(f"MQTT Topic abonniert: {topic}", "info")
                
            self._log("MQTT Client gestartet", "ok")
            
        except ImportError:
            self._log("MQTT Bibliothek nicht verfügbar", "error")
        except Exception as e:
            self._log(f"MQTT Fehler beim Start: {e}", "error")

    def _stop_mqtt(self):
        self._log("MQTT Client wird gestoppt...", "info")
        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            self._mqtt_client = None
        self._set_status_text("MQTT Inaktiv", CLR_TEXT_DIM)

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        self._log(f"MQTT verbunden (rc={rc})", "info")
        self._mqtt_connected = True
        self._set_status_text("MQTT Verbunden", CLR_GREEN)

    def _on_mqtt_disconnect(self, client, userdata, rc):
        self._log(f"MQTT getrennt (rc={rc})", "info")
        self._mqtt_connected = False
        self._set_status_text("MQTT Getrennt", CLR_RED)

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            self._log(f"MQTT Nachricht erhalten: {msg.topic} = {msg.payload.decode('utf-8')}", "info")
            topic = msg.topic
            payload = msg.payload.decode("utf-8")
            
            # Extrahiere node_id aus dem Topic (entsprechend deinem Unity Setup)
            # Beispiel: /PlcNode/Get/Step1_BM -> node_id = Step1_BM
            node_id = topic.split("/")[-1]
            
            # Verarbeite die Nachricht
            val = payload.lower() in ("true", "1", "on")
            
            # Aktualisiere den Wert in node_values
            if node_id in self.node_values:
                old_val = self.node_values[node_id]
                self.node_values[node_id] = val
                
                # Nur wenn sich der Wert geändert hat, aktualisiere die UI
                if old_val != val:
                    self._set_node_display(node_id, val)
                    self._apply_usd_for_node(node_id, val)
                    self._log(f"MQTT: {node_id} = {val}", "ok")
            else:
                # Falls node_id noch nicht bekannt ist, füge sie hinzu
                self.node_values[node_id] = val
                self._log(f"MQTT: Neuer Node {node_id} = {val}", "info")
                
        except Exception as e:
            self._log(f"MQTT Nachricht Fehler: {e}", "error")

    # =================================================================
    # RESTART
    # =================================================================
    def _restart_extension(self):
        self._log("Extension wird neu gestartet...", "info")
        ext_manager = omni.kit.app.get_app().get_extension_manager()
        ext_manager.set_extension_enabled(self._ext_id, False)
        ext_manager.set_extension_enabled(self._ext_id, True)

    # =================================================================
    # CONTROL CLICK
    # =================================================================
    def on_control_clicked(self, node_id):
        if not self._is_running:
            return

        node = self._find_node(node_id)
        if not node:
            return

        # SPEZIALFALL: SAUGGREIFER
        if node_id == "Sauggreifer_EIN":
            if self._sim_mode:
                active = self._suction.toggle()
                self.node_values[node_id] = active
                self._set_node_display(node_id, active)
            else:
                current_val = self.node_values.get(node_id, False)
                new_val = not current_val
                self._log(f"Toggle {node_id} -> {new_val}", "info")
                if node_id in self.node_labels:
                    self.node_labels[node_id].text = "  sending..."
                    self.node_labels[node_id].set_style({"font_size": 12, "color": CLR_YELLOW})
                t = asyncio.ensure_future(self.send_api_update(node_id, new_val))
                self._active_tasks.append(t)
            return

        mode = node.get("mode", "toggle")

        if mode == "impulse":
            self._log(f"Manueller Step-Impuls: {node_id}", "info")
            self._execute_step_impulse(node_id, node)
            if not self._sim_mode:
                t = asyncio.ensure_future(self._send_impulse_to_api(node_id))
                self._active_tasks.append(t)

        elif mode == "velocity_impulse":
            if self._velocity_running.get(node_id, False):
                self._log(f"Velocity-Impuls läuft bereits: {node_id}", "info")
                return
            self._log(f"Manueller Velocity-Impuls: {node_id}", "info")
            t = asyncio.ensure_future(self._execute_velocity_impulse(node_id, node))
            self._active_tasks.append(t)
            if not self._sim_mode:
                t2 = asyncio.ensure_future(self._send_impulse_to_api(node_id))
                self._active_tasks.append(t2)

        else:
            current_val = self.node_values.get(node_id, False)
            new_val = not current_val
            self._log(f"Toggle {node_id} -> {new_val}", "info")

            if self._sim_mode:
                self.node_values[node_id] = new_val
                self._set_node_display(node_id, new_val)
                self._apply_usd_for_node(node_id, new_val)
                self._log(f"SIM: {node_id} = {new_val}", "ok")
            else:
                if node_id in self.node_labels:
                    self.node_labels[node_id].text = "  sending..."
                    self.node_labels[node_id].set_style({"font_size": 12, "color": CLR_YELLOW})
                t = asyncio.ensure_future(self.send_api_update(node_id, new_val))
                self._active_tasks.append(t)

        self._active_tasks = [t for t in self._active_tasks if not t.done()]

    def _find_node(self, node_id):
        for node in self.nodes:
            if node.get("node_id") == node_id:
                return node
        return None

    # =================================================================
    # STEP IMPULSE
    # =================================================================
    def _execute_step_impulse(self, node_id, node):
        step = float(node.get("step_degrees", 90.0))
        current_pos = self._impulse_positions.get(node_id, 0.0)
        new_pos = current_pos + step
        self._impulse_positions[node_id] = new_pos

        self._log(f"Step: {node_id} {current_pos} -> {new_pos} (+{step})", "ok")

        stage = omni.usd.get_context().get_stage()
        if stage:
            self._set_usd_attr(stage, node, new_pos)

        if node_id in self.node_labels:
            self.node_labels[node_id].text = f"  {new_pos:.0f} deg"
            self.node_labels[node_id].set_style({"font_size": 12, "color": CLR_GREEN})

    # =================================================================
    # VELOCITY IMPULSE
    # =================================================================
    async def _execute_velocity_impulse(self, node_id, node):
        if self._velocity_running.get(node_id, False):
            return

        self._velocity_running[node_id] = True
        velocity = float(node.get("target_value", 200.0))
        duration = float(node.get("impulse_duration", 1.8))

        stage = omni.usd.get_context().get_stage()
        if not stage:
            self._velocity_running[node_id] = False
            return

        if node_id in self.node_labels:
            self.node_labels[node_id].text = "  SPINNING"
            self.node_labels[node_id].set_style({"font_size": 12, "color": CLR_ORANGE})

        self._log(f"Velocity START: {node_id} vel={velocity} dur={duration}s", "ok")
        self._set_usd_attr(stage, node, velocity)

        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            self._set_usd_attr(stage, node, 0.0)
            self._velocity_running[node_id] = False
            raise

        self._set_usd_attr(stage, node, 0.0)
        self._velocity_running[node_id] = False

        self._log(f"Velocity STOP: {node_id}", "ok")

        if node_id in self.node_labels:
            self.node_labels[node_id].text = "  READY"
            self.node_labels[node_id].set_style({"font_size": 12, "color": CLR_TEXT_DIM})

    # =================================================================
    # API: Impulse
    # =================================================================
    async def _send_impulse_to_api(self, node_id):
        if self._sim_mode:
            return

        headers = {"X-API-KEY": self.api_key, "accept": "application/json"}
        try:
            async with aiohttp.ClientSession() as session:
                params = {"NodeName": node_id, "Value": "true", "user": "admin", "apiKey": self.api_key}
                async with session.post(self.api_url_set, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=5), ssl=False) as resp:
                    if resp.status == 200:
                        self._log(f"Impulse API true: {node_id}", "ok")

                await asyncio.sleep(0.3)

                params["Value"] = "false"
                async with session.post(self.api_url_set, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=5), ssl=False) as resp:
                    if resp.status == 200:
                        self._log(f"Impulse API reset: {node_id}", "log")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log(f"Impulse API Fehler: {node_id} | {e}", "error")

    # =================================================================
    # API: Toggle SET
    # =================================================================
    async def send_api_update(self, node_id, value):
        if self._sim_mode:
            self.node_values[node_id] = value
            self._set_node_display(node_id, value)
            self._apply_usd_for_node(node_id, value)
            return

        headers = {"X-API-KEY": self.api_key, "accept": "application/json"}
        params = {"NodeName": node_id, "Value": str(value).lower(), "user": "admin", "apiKey": self.api_key}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url_set, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=5), ssl=False) as resp:
                    if resp.status == 200:
                        self.node_values[node_id] = value
                        self._set_node_display(node_id, value)
                        self._apply_usd_for_node(node_id, value)
                        self._log(f"Set OK: {node_id} = {value}", "ok")
                    else:
                        self._log(f"Set Fehler {resp.status}: {node_id}", "error")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log(f"Set Exception: {node_id} | {e}", "error")

    # =================================================================
    # UI HELPERS
    # =================================================================
    def _set_node_display(self, node_id, val):
        label = self.node_labels.get(node_id)
        if not label:
            return
        if val:
            label.text = "  TRUE"
            label.set_style({"font_size": 12, "color": CLR_GREEN})
        else:
            label.text = "  FALSE"
            label.set_style({"font_size": 12, "color": CLR_RED})

    def _set_status_text(self, text, color):
        if not hasattr(self, "_status_label"):
            return
        self._status_label.text = text
        self._status_label.set_style({"font_size": 11, "color": color})

    # =================================================================
    # USD
    # =================================================================
    def _apply_usd_for_node(self, node_id, val):
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return
        for node in self.nodes:
            if node.get("node_id") == node_id:
                target_val = float(node.get("target_value", 1.0))
                new_val = target_val if val else 0.0
                self._set_usd_attr(stage, node, new_val)
                return

    def _set_usd_attr(self, stage, node, value):
        p_path = node.get("prim_path")
        if not p_path:
            return

        prim = stage.GetPrimAtPath(p_path)
        if not prim or not prim.IsValid():
            return

        attr_name = node.get("attribute", "drive:angular:physics:targetPosition")
        attr = prim.GetAttribute(attr_name)

        if not attr or not attr.IsValid():
            alt_name = attr_name.replace(":physics:", ":")
            attr = prim.GetAttribute(alt_name)
            if attr and attr.IsValid():
                attr_name = alt_name

        if not attr or not attr.IsValid():
            return

        try:
            attr.Set(value)
            layer = stage.GetEditTarget().GetLayer()
            prim_spec = layer.GetPrimAtPath(p_path)
            if prim_spec:
                sdf_attr = prim_spec.attributes.get(attr_name)
                if sdf_attr:
                    sdf_attr.default = value
        except Exception as e:
            self._log(f"USD Fehler: {p_path} | {e}", "error")

    # =================================================================
    # SHUTDOWN
    # =================================================================
    def on_shutdown(self):
        try:
            self._suction.reset()
        except Exception:
            pass
        self._is_running = False

        if self._timeline_sub:
            self._timeline_sub = None

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

        if self._sim_update_task and not self._sim_update_task.done():
            self._sim_update_task.cancel()

        # MQTT stoppen
        self._stop_mqtt()

        tasks_to_cancel = [t for t in self._active_tasks if not t.done()]
        self._active_tasks = []
        self.node_labels = {}
        self.node_values = {}
        self.nodes = []
        self._impulse_positions = {}
        self._impulse_armed = {}
        self._velocity_running = {}
        self._sim_update_task = None

        if hasattr(self, "_window") and self._window:
            self._window.destroy()
            self._window = None

        if tasks_to_cancel:
            asyncio.ensure_future(_cancel_and_await(tasks_to_cancel))

        print("[MazeRunner] Shutdown.")

    # =================================================================
    # GESAMTPROZESS ROUTINE
    # =================================================================
    def _start_gesamtprozess(self):
        if not self._sim_mode:
            self._log("Simulation nicht aktiv – Gesamtprozess nicht gestartet", "error")
            return
        if getattr(self, "_routine_gesamt_running", False):
            self._log("Gesamtprozess läuft bereits", "error")
            return
        self._log("▶ Gesamtprozess → Routine gestartet", "info")
        t = asyncio.ensure_future(self._run_gesamtprozess())
        self._active_tasks.append(t)

    async def _run_gesamtprozess(self):
        if getattr(self, "_routine_gesamt_running", False):
            self._log("[Routine] Gesamtprozess läuft bereits – ignoriert", "error")
            return

        self._routine_gesamt_running = True
        self._log("═══ Routine GESAMTPROZESS GESTARTET ═══", "info")

        try:
            # Phase 1: BM ausfahren
            await self._step("Phase 1 – BM ausfahren (BM_MoveFront_Set → TRUE)")
            self._set_node("BM_MoveFront_Set", True)
            await asyncio.sleep(0.5)

            # Phase 2: BM einfahren
            await self._step("Phase 2 – BM einfahren (BM_MoveFront_Set → FALSE)")
            self._set_node("BM_MoveFront_Set", False)
            await asyncio.sleep(0.5)

            # Phase 3: DS Step (Nur 1x statt 3x)
            await self._step("Phase 3 – DS Step (Start_Stepper_Set)")
            self._trigger_impulse("Start_Stepper_Set")
            await asyncio.sleep(1.0)

            # Phase 4: KM Trigger
            await self._step("Phase 4 – KM Trigger (KM_Stepper_Start)")
            self._trigger_impulse("KM_Stepper_Start")
            await asyncio.sleep(3.5)

            # Phase 5: DS Step (Nur 1x statt 3x)
            await self._step("Phase 5 – DS Step (Start_Stepper_Set)")
            self._trigger_impulse("Start_Stepper_Set")
            await asyncio.sleep(1.0)

            # Phase 6: DM ausfahren
            await self._step("Phase 6 – DM ausfahren (DM_MoveFront_Set → TRUE)")
            self._set_node("DM_MoveFront_Set", True)
            await asyncio.sleep(0.5)

            # Phase 7: DM einfahren
            await self._step("Phase 7 – DM einfahren (DM_MoveFront_Set → FALSE)")
            self._set_node("DM_MoveFront_Set", False)
            await asyncio.sleep(0.5)

            # Phase 8: BA_Start Routine
            await self._step("Phase 8 – BA_Start Routine wird aufgerufen …", delay=0.0)
            await self._run_ba_start_routine()
            await asyncio.sleep(0.5)

            # Phase 9: DS Step (Nur 1x statt 3x)
            await self._step("Phase 9 – DS Step (Start_Stepper_Set)")
            self._trigger_impulse("Start_Stepper_Set")
            await asyncio.sleep(0.5)

            # Phase 10: Squeeze
            await self._step("Phase 10 – Squeeze EIN (Squeezer_start_set → TRUE)")
            self._set_node("Squeezer_start_set", True)
            await asyncio.sleep(1.0)

            # Deckel nach Squeeze absetzen
            await self._step("Phase 10b – Deckel nach Squeeze absetzen")
            self._suction.press_down(z_down=0.002)
            await asyncio.sleep(0.5)

            # Squeeze ausschalten
            await self._step("Phase 10c – Squeeze AUS (Squeezer_start_set → FALSE)")
            self._set_node("Squeezer_start_set", False)
            await asyncio.sleep(1.0)

            # Deckel wieder aufnehmen
            await self._step("Phase 10d – Deckel wieder aufnehmen")
            self._suction.release_dynamic()
            await asyncio.sleep(0.5)

            # Phase 11: DS Step (Nur 1x statt 5x)
            await self._step("Phase 11 – DS Step (Start_Stepper_Set)")
            self._trigger_impulse("Start_Stepper_Set")
            await asyncio.sleep(0.5)

            self._log("═══ Routine GESAMTPROZESS ABGESCHLOSSEN ✅ ═══", "ok")

        except asyncio.CancelledError:
            self._log("[Routine] Gesamtprozess ABGEBROCHEN", "error")
            raise
        except Exception as e:
            self._log(f"[Routine] Gesamtprozess FEHLER: {e}", "error")
        finally:
            self._routine_gesamt_running = False

    # =================================================================
    # BA_START ROUTINE
    # =================================================================
    async def _run_ba_start_routine(self):
        if getattr(self, "_routine_ba_running", False):
            self._log("[Routine] BA_Start läuft bereits – ignoriert", "error")
            return

        self._routine_ba_running = True
        self._log("═══ Routine BA_Start GESTARTET ═══", "info")

        try:
            # Phase 1: Schwenkarm runter
            await self._step("Phase 1 – Schwenkarm runter (Schwenkarm_Deckel_trans → TRUE)")
            self._set_node("Schwenkarm_Deckel_trans", True)
            await asyncio.sleep(0.5)

            # Phase 2: Sauggreifer EIN
            await self._step("Phase 2 – Sauggreifer EIN (Sauggreifer_EIN → TRUE)")
            self._set_node("Sauggreifer_EIN", True)
            await asyncio.sleep(0.5)

            # Phase 3: Schwenkarm hoch
            await self._step("Phase 3 – Schwenkarm hoch (Schwenkarm_Deckel_trans → FALSE)")
            self._set_node("Schwenkarm_Deckel_trans", False)
            await asyncio.sleep(0.5)

            # Phase 4: Schwenkarm schwenken
            await self._step("Phase 4 – Schwenkarm schwenken (Schwenkarm_Deckel_rot → TRUE)")
            self._set_node("Schwenkarm_Deckel_rot", True)
            await asyncio.sleep(0.5)
            
            # Schwenkarm wieder absenken (Fix für Deckel-Ablage)
            await self._step("Phase 4b – Schwenkarm senken (Schwenkarm_Deckel_trans → TRUE)")
            self._set_node("Schwenkarm_Deckel_trans", True)
            await asyncio.sleep(0.5)

            # Phase 5: Sauggreifer AUS
            await self._step("Phase 5 – Sauggreifer AUS (Sauggreifer_EIN → FALSE)")
            self._set_node("Sauggreifer_EIN", False)
            await asyncio.sleep(0.5)
            
            # Phase 6: Schwenkarm hoch
            await self._step("Phase 3 – Schwenkarm hoch (Schwenkarm_Deckel_trans → FALSE)")
            self._set_node("Schwenkarm_Deckel_trans", False)
            await asyncio.sleep(0.5)
            
            # Phase 4: Schwenkarm schwenken
            await self._step("Phase 4 – Schwenkarm schwenken (Schwenkarm_Deckel_rot → False)")
            self._set_node("Schwenkarm_Deckel_rot", False)
            await asyncio.sleep(0.5)

            self._log("═══ Routine BA_Start ABGESCHLOSSEN ✅ ═══", "ok")

        except asyncio.CancelledError:
            self._log("[Routine] BA_Start ABGEBROCHEN", "error")
            raise
        except Exception as e:
            self._log(f"[Routine] BA_Start FEHLER: {e}", "error")
        finally:
            self._routine_ba_running = False

    # =================================================================
    # HELPER METHODS
    # =================================================================
    def _step(self, description: str, delay: float = 1.0):
        """Loggt einen Schritt und wartet."""
        self._log(f"[Auto] ▶ {description}", "info")
        return asyncio.sleep(delay)

    def _set_node(self, node_id: str, value: bool):
        """Setzt einen Toggle/Suction-Node direkt."""
        node = _find_node(self, node_id)
        if not node:
            self._log(f"[Routine] Node nicht gefunden: {node_id}", "error")
            return False

        if node_id == "Sauggreifer_EIN":
            current = self._suction.is_active
            if value and not current:
                self._suction.attach()
                self.node_values[node_id] = True
                self._set_node_display(node_id, True)
            elif not value and current:
                self._suction.detach()
                self.node_values[node_id] = False
                self._set_node_display(node_id, False)
            return True

        self.node_values[node_id] = value
        self._set_node_display(node_id, value)
        self._apply_usd_for_node(node_id, value)
        self._log(f"[Routine] {node_id} = {value}", "ok")
        return True

    def _trigger_impulse(self, node_id: str):
        """Führt einen Step-Impuls für einen impulse-Mode Node aus."""
        node = _find_node(self, node_id)
        if not node:
            self._log(f"[Routine] Impulse-Node nicht gefunden: {node_id}", "error")
            return
        if node.get("mode") != "impulse":
            self._log(f"[Routine] Node {node_id} ist kein impulse-Mode", "error")
            return
        self._execute_step_impulse(node_id, node)
        self._log(f"[Routine] Impulse ausgelöst: {node_id}", "ok")