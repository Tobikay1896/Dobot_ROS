"""Dobot ROS2 Bridge – Isaac Sim Extension.

DobotROSNode: USB-Verbindung (pydobot) + ROS2-Publisher (/joint_states).
DobotBridgeExtension: omni.ext.IExt – UI, Dynamic-Control-Kopplung, Update-Loop.
"""

import asyncio
import math
import struct
import threading
import time as _time
import omni.ext
import omni.kit.app
import omni.kit.pipapi
import omni.ui as ui

from .constants import (
    CLR_BG_DARK, CLR_BG_MID, CLR_BORDER, CLR_TEXT, CLR_TEXT_DIM,
    CLR_GREEN, CLR_RED, CLR_YELLOW, CLR_ACCENT,
    ROBOT_USD_PATH,
    HOME_X, HOME_Y, HOME_Z, HOME_R,
    GP3_PWM_ADDRESS, SERVO_FREQ_HZ, SERVO_MIN_DUTY, SERVO_MAX_DUTY,
)

# Lazy-Import-Caches: ROS2 und pydobot erst beim Connect laden
_rclpy = None
_Node = None
_JointState = None
_Dobot = None

# USD-Pfade des Ziel-Würfels (Tracking-Feature)
_CUBE_PRIM_PATH = "/World/DobotTargetCube"
_CUBE_MAT_PATH  = "/World/DobotTargetCubeMat"

# Gierwinkel-Offset J1: Ausrichtung der Simulationsachse gegenüber Realroboter
J1_YAW_OFFSET_DEG = -45.0


def _try_import_ros():
    """Lädt rclpy/Node/JointState einmalig; gibt True zurück wenn erfolgreich."""
    global _rclpy, _Node, _JointState
    if _rclpy is not None:
        return True
    try:
        import rclpy
        from rclpy.node import Node
        from sensor_msgs.msg import JointState
    except Exception:
        return False
    _rclpy, _Node, _JointState = rclpy, Node, JointState
    return True


def _try_import_dobot():
    """Lädt pydobot.Dobot einmalig; gibt True zurück wenn erfolgreich."""
    global _Dobot
    if _Dobot is not None:
        return True
    try:
        from pydobot import Dobot
    except Exception:
        return False
    _Dobot = Dobot
    return True


# ---------------------------------------------------------------------------
# Klasse: DobotROSNode
# ---------------------------------------------------------------------------

class DobotROSNode:
    """USB-Verbindung zum Dobot + ROS2-Node für /joint_states."""

    def __init__(self, com_port: str):
        """Öffnet ROS2-Node und serielle Verbindung. Wirft RuntimeError bei Fehler."""
        if not _try_import_ros():
            raise RuntimeError("ROS2 nicht ladbar – ROS2-Bridge prüfen.")
        if not _try_import_dobot():
            raise RuntimeError("pydobot nicht installiert.")

        # ROS2-Kontext nur einmal initialisieren (auch bei mehrfachem Connect)
        if not _rclpy.ok():
            _rclpy.init()

        self._node = _Node('dobot_extension_bridge')
        self.publisher_ = self._node.create_publisher(
            _JointState, 'joint_states', 10
        )
        self.device = self._create_device(com_port)

        # Fallback bei Kommunikationsunterbrechung: letzte gültige Werte
        self.last_valid_positions = [0.0, 0.0, 0.0, 0.0]
        self.last_pose = {'x': 0.0, 'y': 0.0, 'z': 0.0, 'r': 0.0}

    def _create_device(self, com_port: str):
        """Öffnet pydobot-Verbindung; wirft RuntimeError bei Fehler."""
        try:
            return _Dobot(port=com_port)
        except Exception as exc:
            raise RuntimeError(f"Dobot-Verbindung fehlgeschlagen: {exc}")

    def update_step(self):
        """Liest Pose, berechnet Gelenkwinkel, publiziert JointState.

        Returns (pose_dict, joints_deg): pose in mm/°, joints [j1,j2,j5,j6] in Grad.
        """
        msg = _JointState()
        msg.header.stamp = self._node.get_clock().now().to_msg()
        msg.name = ['joint_1', 'joint_2', 'joint_5', 'joint_6']

        if self.device:
            try:
                pose = self.device.pose()
                if pose is not None and len(pose) >= 8:
                    x, y, z, r = pose[0], pose[1], pose[2], pose[3]
                    # pydobot liefert absolute Weltwinkel in Grad
                    j1, j2, j3, j4 = pose[4], pose[5], pose[6], pose[7]

                    # Gierwinkel-Offset: Simulationsachse gegenüber Realroboter
                    j1 = j1 + J1_YAW_OFFSET_DEG

                    # Parallelogramm: Unterarm in Weltlage halten
                    pos_j5 = j3 - j2
                    # Endeffector vertikal: j2+(j3-j2)+j6=90 → j6=90-j3-j4
                    pos_j6 = 90.0 - j3 - j4

                    joints_deg = [j1, j2, pos_j5, pos_j6]
                    self.last_valid_positions = joints_deg
                    self.last_pose = {'x': x, 'y': y, 'z': z, 'r': r, 'j1': j1}

                    # ROS2 erwartet Radiant
                    msg.position = [math.radians(v) for v in joints_deg]
                    self.publisher_.publish(msg)
                else:
                    msg.position = [math.radians(v) for v in self.last_valid_positions]
                    self.publisher_.publish(msg)
            except Exception:
                msg.position = [math.radians(v) for v in self.last_valid_positions]
                self.publisher_.publish(msg)

        _rclpy.spin_once(self._node, timeout_sec=0.0)
        return self.last_pose, self.last_valid_positions

    def set_suction(self, active: bool):
        """Schaltet Vakuumgreifer (SW1, Kommando ID 62).

        Fallback-Liste deckt bekannte pydobot-Versionen ab.
        """
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")
        for name, args in [
            ('suck',                     (active,)),
            ('set_end_effector_suction', (active,)),
            ('set_vacuum_gripper',       (1 if active else 0,)),
        ]:
            fn = getattr(self.device, name, None)
            if callable(fn):
                return fn(*args)
        raise RuntimeError('Keine Saugbefehl-Methode in pydobot gefunden.')

    def move_vertical(self, delta_mm: float):
        """Bewegt den Dobot senkrecht um delta_mm mm (positiv = aufwärts)."""
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")
        pose = self.device.pose()
        if pose is None or len(pose) < 4:
            raise RuntimeError("Pose konnte nicht gelesen werden.")
        x, y, z, r = pose[0], pose[1], pose[2], pose[3]
        move_fn = getattr(self.device, 'move_to', None)
        if not callable(move_fn):
            raise RuntimeError('move_to nicht verfügbar.')
        return move_fn(x, y, z + delta_mm, r)

    def jog(self, dx: float, dy: float):
        """Horizontaler Versatz um (dx, dy) mm in der X-Y-Ebene."""
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")
        pose = self.device.pose()
        if pose is None or len(pose) < 4:
            raise RuntimeError("Pose konnte nicht gelesen werden.")
        x, y, z, r = pose[0], pose[1], pose[2], pose[3]
        move_fn = getattr(self.device, 'move_to', None)
        if not callable(move_fn):
            raise RuntimeError('move_to nicht verfügbar.')
        return move_fn(x + dx, y + dy, z, r)

    def go_home(self):
        """Fährt zur Nullposition (HOME_X/Y/Z/R aus constants.py)."""
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")
        move_fn = getattr(self.device, 'move_to', None)
        if not callable(move_fn):
            raise RuntimeError('move_to nicht verfügbar.')
        return move_fn(HOME_X, HOME_Y, HOME_Z, HOME_R)

    def set_servo_gp3(self, angle_deg: float):
        """Setzt RC-Servo an GP3-PWM1 auf angle_deg (0–180°).

        Sendet Rohprotokoll-Kommando 130 (Mux → PWM) und 132 (Freq+Duty).
        """
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")
        try:
            from pydobot.message import Message as _Msg
        except ImportError:
            raise RuntimeError("pydobot.message nicht importierbar.")

        angle_deg = max(0.0, min(180.0, float(angle_deg)))
        duty = SERVO_MIN_DUTY + (angle_deg / 180.0) * (SERVO_MAX_DUTY - SERVO_MIN_DUTY)

        # Kommando 130: Pin 15 auf PWM-Modus (Multiplex-Typ 2)
        mux = _Msg()
        mux.id = 130
        mux.ctrl = 0x03
        mux.params = bytearray()
        mux.params.extend(struct.pack('B', GP3_PWM_ADDRESS))
        mux.params.extend(struct.pack('B', 2))
        self.device._send_command(mux)

        # Kommando 132: Frequenz und Duty-Cycle setzen
        pwm = _Msg()
        pwm.id = 132
        pwm.ctrl = 0x03
        pwm.params = bytearray()
        pwm.params.extend(struct.pack('B', GP3_PWM_ADDRESS))
        pwm.params.extend(struct.pack('f', SERVO_FREQ_HZ))
        pwm.params.extend(struct.pack('f', duty))
        self.device._send_command(pwm)

    def play_sequence(self, poses: list):
        """Spielt Teach-in-Sequenz ab; fährt danach zur Nullposition."""
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")
        move_fn = getattr(self.device, 'move_to', None)
        if not callable(move_fn):
            raise RuntimeError('move_to nicht verfügbar.')
        for x, y, z, r, suction_active in poses:
            move_fn(x, y, z, r)
            self.set_suction(suction_active)
        move_fn(HOME_X, HOME_Y, HOME_Z, HOME_R)
        self.set_suction(False)

    def stop_sequence(self):
        """Stoppt Bewegungssequenz und leert die Dobot-interne Queue."""
        if not self.device:
            return
        for fn_name in ('_set_queued_cmd_stop_exec', '_set_queued_cmd_clear',
                        '_set_queued_cmd_start_exec'):
            fn = getattr(self.device, fn_name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

    def shutdown(self):
        """Schließt COM-Port und zerstört ROS2-Node."""
        if self.device:
            try:
                close_fn = getattr(self.device, 'close', None)
                if callable(close_fn):
                    close_fn()
            except Exception:
                pass
        try:
            self._node.destroy_node()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Klasse: DobotBridgeExtension
# ---------------------------------------------------------------------------

class DobotBridgeExtension(omni.ext.IExt):
    """Isaac-Sim-Extension: UI-Fenster, Dynamic-Control-Kopplung, Update-Loop.

    DC-API wird lazy initialisiert – erst wenn die Physik läuft (Play gedrückt).
    """

    def on_startup(self, ext_id: str):
        """Zustand initialisieren, UI aufbauen, Docking starten."""
        # Verbindungszustand
        self._ros_node: DobotROSNode | None = None
        self._update_sub = None
        self._suction_active = False

        # Servo
        self._servo_angle       = 90.0
        self._servo_world_angle = None  # servo + J1 = Weltwinkel; None = kein Tracking

        # Teach-in
        self._recorded_poses    = []
        self._rec_suction_state = False
        self._play_task         = None
        self._play_thread       = None
        self._stop_play_flag    = False

        # Würfel-Tracking
        self._cube_spawned       = False
        self._stop_tracking_flag = False
        self._tracking_thread    = None
        self._tracking_rate_hz   = 1.0

        # Dynamic Control
        self._dc = None
        self._dc_mod = None
        self._art_handle = None
        self._dof_handles = []
        # True nach Connect; False nach erfolgreicher DC-Initialisierung
        self._art_init_pending = False

        # Async-Docking-Task
        self._dock_task = None

        # UI-Fenster
        self._window = ui.Window("Dobot ROS2 Bridge", width=490, height=590)

        _dim  = {"font_size": 11, "color": CLR_TEXT_DIM}
        _norm = {"font_size": 11, "color": CLR_TEXT}

        with self._window.frame:
            with ui.VStack(spacing=6, padding=10):

                # Verbindung
                with ui.HStack(spacing=6, height=26):
                    ui.Label("COM-Port:", width=72, style=_norm)
                    self._port_input = ui.StringField(width=80)
                    self._port_input.model.set_value("COM3")
                    self._btn_connect = ui.Button(
                        "Connect", width=88,
                        clicked_fn=self._on_connect_clicked
                    )
                    self._btn_disconnect = ui.Button(
                        "Disconnect", width=88,
                        clicked_fn=self._on_disconnect_clicked
                    )
                    self._btn_disconnect.enabled = False
                    self._conn_status_label = ui.Label(
                        "● getrennt",
                        style={"font_size": 11, "color": CLR_RED}
                    )

                # Robot-USD-Pfad
                with ui.HStack(spacing=6, height=24):
                    ui.Label("Robot USD:", width=72, style=_norm)
                    self._robot_path_input = ui.StringField()
                    self._robot_path_input.model.set_value(ROBOT_USD_PATH)

                # Paketinstallation
                with ui.HStack(spacing=6, height=24):
                    ui.Spacer(width=72)
                    self._btn_install = ui.Button(
                        "Install packages", width=140,
                        clicked_fn=self._on_install_clicked
                    )

                ui.Line(style={"color": CLR_BORDER}, height=1)

                # Tab-Leiste
                self._active_tab = 0
                _tab_active   = {"background_color": CLR_BG_MID,  "color": CLR_TEXT}
                _tab_inactive = {"background_color": CLR_BG_DARK, "color": CLR_TEXT_DIM}
                with ui.HStack(spacing=2, height=26):
                    self._tab_btn_0 = ui.Button(
                        "Steuerung", width=150,
                        clicked_fn=lambda: self._switch_tab(0),
                        style=_tab_active
                    )
                    self._tab_btn_1 = ui.Button(
                        "Pick & Place", width=150,
                        clicked_fn=lambda: self._switch_tab(1),
                        style=_tab_inactive
                    )
                    ui.Spacer()

                # Tab 0: Steuerung
                self._frame_tab0 = ui.Frame(visible=True)
                with self._frame_tab0:
                    with ui.VStack(spacing=6):
                        # Sauger + Vertikalbewegung
                        with ui.HStack(spacing=6, height=28):
                            self._btn_suction = ui.Button(
                                "Suction ON", width=110,
                                clicked_fn=self._on_suction_toggle_clicked
                            )
                            self._btn_up = ui.Button(
                                "Up 10 mm", width=96,
                                clicked_fn=lambda: self._on_move_vertical_clicked(10.0)
                            )
                            self._btn_down = ui.Button(
                                "Down 10 mm", width=96,
                                clicked_fn=lambda: self._on_move_vertical_clicked(-10.0)
                            )
                            self._btn_suction.enabled = False
                            self._btn_up.enabled      = False
                            self._btn_down.enabled    = False

                        # JOG X/Y
                        with ui.HStack(spacing=6, height=28):
                            self._btn_xp = ui.Button(
                                "X+ 5 mm", width=76,
                                clicked_fn=lambda: self._on_jog_clicked(5.0, 0.0)
                            )
                            self._btn_xn = ui.Button(
                                "X- 5 mm", width=76,
                                clicked_fn=lambda: self._on_jog_clicked(-5.0, 0.0)
                            )
                            self._btn_yp = ui.Button(
                                "Y+ 5 mm", width=76,
                                clicked_fn=lambda: self._on_jog_clicked(0.0, 5.0)
                            )
                            self._btn_yn = ui.Button(
                                "Y- 5 mm", width=76,
                                clicked_fn=lambda: self._on_jog_clicked(0.0, -5.0)
                            )
                            self._btn_xp.enabled = False
                            self._btn_xn.enabled = False
                            self._btn_yp.enabled = False
                            self._btn_yn.enabled = False

                        # Servo GP3
                        with ui.HStack(spacing=6, height=28):
                            ui.Label("Servo GP3:", width=72, style=_dim)
                            self._btn_servo0_n = ui.Button(
                                "− 10°", width=64,
                                clicked_fn=lambda: self._on_servo_clicked(-10.0)
                            )
                            self._servo_angle0_label = ui.Label(
                                "90°", width=36,
                                style={"font_size": 13, "color": CLR_TEXT}
                            )
                            self._btn_servo0_p = ui.Button(
                                "+ 10°", width=64,
                                clicked_fn=lambda: self._on_servo_clicked(10.0)
                            )
                            self._btn_servo0_n.enabled = False
                            self._btn_servo0_p.enabled = False

                        # Ziel-Würfel-Tracking
                        ui.Line(style={"color": CLR_BORDER}, height=1)
                        with ui.HStack(spacing=6, height=28):
                            self._btn_cube_toggle = ui.Button(
                                "Würfel spawnen", width=128,
                                clicked_fn=self._on_cube_toggle_clicked
                            )
                            ui.Label("Rate:", width=34, style=_dim)
                            self._tracking_rate_slider = ui.FloatSlider(
                                min=0.5, max=10.0, step=0.5, width=110
                            )
                            self._tracking_rate_slider.model.set_value(1.0)
                            self._tracking_rate_label = ui.Label(
                                "1.0 Hz", width=46,
                                style={"font_size": 12, "color": CLR_TEXT}
                            )
                            self._tracking_rate_slider.model.add_value_changed_fn(
                                lambda m: self._on_tracking_rate_changed(
                                    m.get_value_as_float()
                                )
                            )
                            self._btn_cube_toggle.enabled = False

                # Tab 1: Pick & Place
                self._frame_tab1 = ui.Frame(visible=False)
                with self._frame_tab1:
                    with ui.VStack(spacing=6):
                        # Nullposition
                        with ui.HStack(spacing=6, height=28):
                            self._btn_home = ui.Button(
                                f"Go Home  (x={HOME_X}  y={HOME_Y}  z={HOME_Z})",
                                clicked_fn=self._on_go_home_clicked
                            )
                            self._btn_home.enabled = False

                        # Servo GP3
                        with ui.HStack(spacing=6, height=28):
                            ui.Label("Servo GP3:", width=72, style=_dim)
                            self._btn_servo_n = ui.Button(
                                "− 10°", width=64,
                                clicked_fn=lambda: self._on_servo_clicked(-10.0)
                            )
                            self._servo_angle_label = ui.Label(
                                "90°", width=36,
                                style={"font_size": 13, "color": CLR_TEXT}
                            )
                            self._btn_servo_p = ui.Button(
                                "+ 10°", width=64,
                                clicked_fn=lambda: self._on_servo_clicked(10.0)
                            )
                            self._btn_servo_n.enabled = False
                            self._btn_servo_p.enabled = False

                        ui.Line(style={"color": CLR_BORDER}, height=1)
                        ui.Label("Teach-in aufzeichnen:", style=_dim, height=16)

                        # Virtueller Sauger-Toggle (kein Hardware-Befehl – erst bei Play)
                        with ui.HStack(spacing=6, height=28):
                            ui.Label("Saugen (Aufn.):", width=105, style=_dim)
                            self._btn_rec_suction = ui.Button(
                                "Saugen AUS", width=120,
                                clicked_fn=self._on_rec_suction_toggle_clicked
                            )
                            ui.Label(
                                "→ wird erst bei Play gesendet",
                                style={"font_size": 10, "color": CLR_TEXT_DIM}
                            )
                            self._btn_rec_suction.enabled = False

                        # Aufnahme-Steuerung
                        with ui.HStack(spacing=6, height=28):
                            self._btn_rec = ui.Button(
                                "Rec Punkt", width=88,
                                clicked_fn=self._on_record_point_clicked
                            )
                            self._rec_count_label = ui.Label(
                                "0 Pkt.", width=46, style=_dim
                            )
                            self._btn_rec_clear = ui.Button(
                                "Löschen", width=68,
                                clicked_fn=self._on_clear_recording_clicked
                            )
                            self._btn_play = ui.Button(
                                "Play", width=60,
                                clicked_fn=self._on_play_clicked
                            )
                            self._btn_stop_seq = ui.Button(
                                "Stop", width=60,
                                clicked_fn=self._on_stop_sequence_clicked
                            )
                            self._btn_rec.enabled       = False
                            self._btn_rec_clear.enabled = False
                            self._btn_play.enabled      = False
                            self._btn_stop_seq.enabled  = False

                        # Scrollbare Waypoint-Liste
                        ui.Label("Aufgezeichnete Punkte:", style=_dim, height=16)
                        scroll = ui.ScrollingFrame(
                            height=130,
                            horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF,
                            vertical_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_AS_NEEDED,
                            style={"background_color": CLR_BG_DARK,
                                   "border_color": CLR_BORDER, "border_width": 1}
                        )
                        with scroll:
                            self._rec_list_frame = ui.Frame()
                        with self._rec_list_frame:
                            ui.Label(
                                "  (noch keine Punkte aufgezeichnet)",
                                style={"font_size": 11, "color": CLR_TEXT_DIM}
                            )

                # Status-Dashboard (immer sichtbar)
                ui.Line(style={"color": CLR_BORDER}, height=1)

                self._status_label = ui.Label(
                    "Bereit", style={"font_size": 11, "color": CLR_TEXT_DIM}
                )
                self._ros_topic_label = ui.Label(
                    "ROS topic: /joint_states", style=_dim
                )
                self._pose_label = ui.Label(
                    "Pose:   x=—  y=—  z=—  r=—", style=_dim
                )
                self._joint_label = ui.Label(
                    "Joints [°]:  j1=—  j2=—  j5=—  j6=—", style=_dim
                )

        # Zwei Frames warten: Fenster muss erst im Workspace registriert sein
        self._dock_task = asyncio.ensure_future(self._dock_window_deferred())

    async def _dock_window_deferred(self):
        """Dockt das Fenster neben Property-Panel nach zwei Update-Frames."""
        await omni.kit.app.get_app().next_update_async()
        await omni.kit.app.get_app().next_update_async()
        prop = ui.Workspace.get_window("Property")
        if prop:
            self._window.dock_in(prop, ui.DockPosition.SAME)

    def _switch_tab(self, index: int):
        """Wechselt zwischen Tab 0 (Steuerung) und Tab 1 (Pick & Place)."""
        self._active_tab = index
        self._frame_tab0.visible = (index == 0)
        self._frame_tab1.visible = (index == 1)
        active   = {"background_color": CLR_BG_MID,  "color": CLR_TEXT}
        inactive = {"background_color": CLR_BG_DARK, "color": CLR_TEXT_DIM}
        self._tab_btn_0.style = active   if index == 0 else inactive
        self._tab_btn_1.style = inactive if index == 0 else active

    # ------------------------------------------------------------------
    # UI-Hilfsmethoden
    # ------------------------------------------------------------------

    def _set_status(self, text: str, color: int = CLR_TEXT_DIM):
        """Setzt Status-Text und -Farbe im Dashboard."""
        if self._status_label:
            self._status_label.text = text
            self._status_label.style = {"color": color}

    def _update_display(self, pose: dict, joints_deg: list):
        """Aktualisiert Pose- und Gelenkanzeige im Dashboard."""
        if self._pose_label:
            self._pose_label.text = (
                f"Pose: x={pose['x']:.1f}, y={pose['y']:.1f}, "
                f"z={pose['z']:.1f}, r={pose['r']:.1f}"
            )
        if self._joint_label:
            self._joint_label.text = (
                f"Joints [°]: j1={joints_deg[0]:.1f}, j2={joints_deg[1]:.1f}, "
                f"j5={joints_deg[2]:.1f}, j6={joints_deg[3]:.1f}"
            )

    def _set_buttons_enabled(self, enabled: bool):
        """Schaltet alle Steuertasten ein oder aus."""
        for btn in (
            self._btn_suction, self._btn_up, self._btn_down,
            self._btn_xp, self._btn_xn, self._btn_yp, self._btn_yn,
            self._btn_servo0_n, self._btn_servo0_p,
            self._btn_cube_toggle,
            self._btn_home,
            self._btn_servo_n, self._btn_servo_p,
            self._btn_rec_suction,
            self._btn_rec, self._btn_rec_clear, self._btn_play, self._btn_stop_seq,
        ):
            btn.enabled = enabled

    # ------------------------------------------------------------------
    # Dynamic-Control-Kopplung
    # ------------------------------------------------------------------

    def _init_articulation(self, robot_path: str, silent: bool = False):
        """Verbindet DC-API mit Roboter-Artikulation in der Szene.

        get_articulation() liefert INVALID_HANDLE solange Physik nicht läuft.
        Wird mit silent=True wiederholt aufgerufen bis Play gedrückt wird.
        """
        self._dc = None
        self._dc_mod = None
        self._art_handle = None
        self._dof_handles = []
        if not robot_path:
            return
        try:
            from omni.isaac.dynamic_control import _dynamic_control as dc_mod
            dc = dc_mod.acquire_dynamic_control_interface()

            art = dc.get_articulation(robot_path)
            if art == dc_mod.INVALID_HANDLE:
                if not silent:
                    self._set_status(
                        "Warte auf Physik-Simulation (Play drücken)...", CLR_YELLOW
                    )
                return

            self._dc = dc
            self._dc_mod = dc_mod
            self._art_handle = art

            # DOF-Handles für alle vier Gelenke holen
            joint_names = ['joint_1', 'joint_2', 'joint_5', 'joint_6']
            for jname in joint_names:
                dof = dc.find_articulation_dof(art, jname)
                self._dof_handles.append(
                    dof if dof != dc_mod.INVALID_HANDLE else None
                )

            found = sum(1 for d in self._dof_handles if d is not None)
            self._set_status(
                f"Modell verbunden: {found}/{len(joint_names)} DOFs gefunden.",
                CLR_GREEN
            )
        except ImportError:
            if not silent:
                self._set_status(
                    "omni.isaac.dynamic_control nicht verfügbar.", CLR_YELLOW
                )
        except Exception as exc:
            if not silent:
                self._set_status(f"DC-Fehler: {exc}", CLR_RED)

    def _drive_joints(self, joints_deg: list):
        """Setzt Gelenkwinkel auf die Simulation (set_dof_state + set_dof_position_target).

        Beide DC-Aufrufe erwarten Radiant; Konvertierung erfolgt hier aus Grad.
        """
        if not self._dc or self._art_handle is None or not self._dof_handles:
            return

        for dof_handle, angle_deg in zip(self._dof_handles, joints_deg):
            if dof_handle is None:
                continue
            rad = math.radians(float(angle_deg))

            try:
                state = self._dc_mod.DofState()
                state.pos = rad
                state.vel = 0.0
                self._dc.set_dof_state(dof_handle, state, self._dc_mod.STATE_POS)
            except Exception:
                pass

            try:
                # PhysX-Drive-Ziel (Pflicht: Bereich [-2π, 2π])
                self._dc.set_dof_position_target(dof_handle, rad)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # UI-Callbacks
    # ------------------------------------------------------------------

    def _on_go_home_clicked(self):
        """Fährt zur Nullposition."""
        if not self._ros_node:
            self._set_status("Keine Verbindung.", CLR_RED)
            return
        try:
            self._ros_node.go_home()
            self._set_status(
                f"Fahre zur Nullposition: x={HOME_X} y={HOME_Y} z={HOME_Z}",
                CLR_GREEN
            )
        except Exception as exc:
            self._set_status(f"Home-Fehler: {exc}", CLR_RED)

    def _on_servo_clicked(self, delta: float):
        """Ändert Servo-Winkel um delta° und speichert Weltwinkel für J1-Kompensation."""
        if not self._ros_node:
            self._set_status("Keine Verbindung.", CLR_RED)
            return
        self._servo_angle = max(0.0, min(180.0, self._servo_angle + delta))
        try:
            self._ros_node.set_servo_gp3(self._servo_angle)
            # Weltwinkel merken: bleibt bei wechselnder J1-Rotation konstant
            j1 = self._ros_node.last_pose.get('j1', 0.0)
            self._servo_world_angle = self._servo_angle + j1
            lbl = f"{self._servo_angle:.0f}°"
            self._servo_angle_label.text  = lbl
            self._servo_angle0_label.text = lbl
            self._set_status(
                f"Servo GP3: {self._servo_angle:.0f}°  (Weltwinkel {self._servo_world_angle:.0f}°)",
                CLR_GREEN
            )
        except Exception as exc:
            self._set_status(f"Servo-Fehler: {exc}", CLR_RED)

    def _on_rec_suction_toggle_clicked(self):
        """Schaltet virtuellen Saugzustand um und speichert sofort Waypoint.

        Kein Hardware-Befehl – Schaltung erfolgt erst bei Play.
        """
        self._rec_suction_state = not self._rec_suction_state
        if self._rec_suction_state:
            self._btn_rec_suction.text  = "Saugen EIN"
            self._btn_rec_suction.style = {"background_color": CLR_GREEN,
                                           "color": 0xFF000000}
        else:
            self._btn_rec_suction.text  = "Saugen AUS"
            self._btn_rec_suction.style = {}

        if self._ros_node:
            self._do_record_point()

    def _rebuild_rec_list(self):
        """Baut scrollbare Waypoint-Liste neu auf."""
        self._rec_list_frame.clear()
        with self._rec_list_frame:
            with ui.VStack(spacing=1):
                if not self._recorded_poses:
                    ui.Label(
                        "  (noch keine Punkte aufgezeichnet)",
                        style={"font_size": 11, "color": CLR_TEXT_DIM}
                    )
                    return
                for i, (x, y, z, r, suction) in enumerate(self._recorded_poses):
                    sauger_txt = "Saugen EIN" if suction else "Saugen AUS"
                    clr = CLR_GREEN if suction else CLR_TEXT_DIM
                    ui.Label(
                        f"  P{i+1:02d}  "
                        f"x={x:7.1f}  y={y:7.1f}  z={z:6.1f}  r={r:6.1f}"
                        f"  {sauger_txt}",
                        style={"font_size": 11, "color": clr}
                    )

    def _do_record_point(self):
        """Speichert aktuelle Pose + virtuellen Saugzustand als Waypoint."""
        pose = self._ros_node.last_pose
        self._recorded_poses.append((
            pose['x'], pose['y'], pose['z'], pose['r'],
            self._rec_suction_state
        ))
        n = len(self._recorded_poses)
        self._rec_count_label.text = f"{n} Pkt."
        self._rebuild_rec_list()
        self._set_status(
            f"P{n:02d} gespeichert:  x={pose['x']:.1f}  y={pose['y']:.1f}  "
            f"z={pose['z']:.1f}  r={pose['r']:.1f}  "
            f"Saugen={'EIN' if self._rec_suction_state else 'AUS'}",
            CLR_GREEN
        )

    def _on_record_point_clicked(self):
        """Waypoint manuell aufzeichnen ('Rec Punkt'-Button)."""
        if not self._ros_node:
            self._set_status("Keine Verbindung.", CLR_RED)
            return
        self._do_record_point()

    def _on_clear_recording_clicked(self):
        """Alle Waypoints und Saugzustand zurücksetzen."""
        self._recorded_poses    = []
        self._rec_suction_state = False
        self._btn_rec_suction.text  = "Saugen AUS"
        self._btn_rec_suction.style = {}
        self._rec_count_label.text  = "0 Pkt."
        self._rebuild_rec_list()
        self._set_status("Aufzeichnung gelöscht.", CLR_YELLOW)

    def _on_play_clicked(self):
        """Startet Teach-in-Wiedergabe im Hintergrundthread."""
        if not self._ros_node:
            self._set_status("Keine Verbindung.", CLR_RED)
            return
        if not self._recorded_poses:
            self._set_status("Keine Punkte aufgezeichnet.", CLR_YELLOW)
            return
        if self._play_thread and self._play_thread.is_alive():
            self._set_status("Sequenz läuft bereits – erst Stop drücken.", CLR_YELLOW)
            return
        self._stop_play_flag = False
        poses = list(self._recorded_poses)
        self._play_thread = threading.Thread(
            target=self._play_sequence_thread,
            args=(poses,),
            daemon=True,
            name="dobot_play"
        )
        self._play_thread.start()
        self._set_status(f"Sequenz gestartet ({len(poses)} Punkte)...", CLR_GREEN)

    def _play_sequence_thread(self, poses: list):
        """Teach-in-Sequenz im Hintergrundthread.

        Threading statt asyncio: wait_for_cmd() ist blockierend – würde den
        Isaac-Sim-Event-Loop einfrieren. Im Thread blockiert nur dieser Thread.
        """
        device = self._ros_node.device if self._ros_node else None
        if not device:
            self._set_status("Verbindung verloren.", CLR_RED)
            return
        move_fn = getattr(device, 'move_to', None)
        wait_fn = getattr(device, 'wait_for_cmd', None)
        if not callable(move_fn):
            self._set_status("move_to nicht in pydobot verfügbar.", CLR_RED)
            return

        APPROACH_MM = 10.0  # Sicherheitsabstand von oben bei Saugerwechsel

        def _move_and_wait(tx, ty, tz, tr):
            """Bewegt und wartet blockierend; gibt False zurück bei Abbruch."""
            idx = move_fn(tx, ty, tz, tr)
            if callable(wait_fn) and idx is not None:
                wait_fn(idx)
            else:
                _time.sleep(2.0)
            return not self._stop_play_flag

        def _servo_j1_compensate():
            """Servo-Weltorientierung bei J1-Rotation konstant halten."""
            if self._servo_world_angle is None or not self._ros_node:
                return
            try:
                raw = device.pose()
                j1_now = float(raw[4]) if raw and len(raw) >= 5 else 0.0
                angle = max(0.0, min(180.0, self._servo_world_angle - j1_now))
                self._ros_node.set_servo_gp3(angle)
                lbl = f"{angle:.0f}°"
                self._servo_angle_label.text  = lbl
                self._servo_angle0_label.text = lbl
            except Exception:
                pass

        total = len(poses)
        try:
            prev_suction = False
            for i, (x, y, z, r, suction_active) in enumerate(poses):
                if self._stop_play_flag or not self._ros_node:
                    break

                suction_changes = (suction_active != prev_suction)
                self._set_status(
                    f"Punkt {i+1}/{total}  →  x={x:.1f}  y={y:.1f}  z={z:.1f}  "
                    f"Saugen={'EIN' if suction_active else 'AUS'}"
                    f"{'  ↓ Anfahrt von oben' if suction_changes else ''}",
                    CLR_YELLOW
                )

                if suction_changes:
                    # 1 cm über Zielpunkt: seitliches Streifen vermeiden
                    if not _move_and_wait(x, y, z + APPROACH_MM, r):
                        break
                    _servo_j1_compensate()
                    _time.sleep(0.1)

                if not _move_and_wait(x, y, z, r):
                    break
                _servo_j1_compensate()

                if self._stop_play_flag or not self._ros_node:
                    break

                self._ros_node.set_suction(suction_active)
                _time.sleep(0.5)  # Druckaufbau / -abbau abwarten

                if suction_changes:
                    # 1 cm abheben bevor nächste Horizontalbewegung
                    if not _move_and_wait(x, y, z + APPROACH_MM, r):
                        break
                    _servo_j1_compensate()

                prev_suction = suction_active

            if not self._stop_play_flag and self._ros_node:
                self._set_status("Zurück zur Nullposition...", CLR_YELLOW)
                _move_and_wait(HOME_X, HOME_Y, HOME_Z + APPROACH_MM, HOME_R)
                _move_and_wait(HOME_X, HOME_Y, HOME_Z, HOME_R)
                _servo_j1_compensate()
                if self._ros_node:
                    self._ros_node.set_suction(False)
                self._set_status(
                    f"Sequenz abgeschlossen ({total} Punkte).", CLR_GREEN
                )
            elif self._stop_play_flag:
                self._set_status("Sequenz abgebrochen.", CLR_YELLOW)

        except Exception as exc:
            self._set_status(f"Wiedergabe-Fehler: {exc}", CLR_RED)

    def _on_stop_sequence_clicked(self):
        """Bricht laufende Sequenz ab und leert Dobot-Queue."""
        self._stop_play_flag = True
        if self._play_task and not self._play_task.done():
            self._play_task.cancel()
        if self._ros_node:
            self._ros_node.stop_sequence()
        self._set_status("Sequenz gestoppt.", CLR_YELLOW)

    def _on_install_clicked(self):
        """Installiert pydobot und pyserial über Isaac Sims pip-API."""
        self._set_status("Installiere pydobot und pyserial...", CLR_ACCENT)
        try:
            omni.kit.pipapi.install("pydobot")
            omni.kit.pipapi.install("pyserial")
            self._set_status(
                "Pakete installiert. Bitte Extension neu starten.", CLR_GREEN
            )
            self._btn_install.enabled = False
        except Exception as exc:
            self._set_status(f"Install fehlgeschlagen: {exc}", CLR_RED)

    def _on_connect_clicked(self):
        """Öffnet Dobot-Verbindung und startet den Frame-Update-Loop."""
        port = self._port_input.model.get_value_as_string().strip()
        if not port:
            self._set_status("Bitte COM-Port angeben.", CLR_RED)
            return

        try:
            self._ros_node = DobotROSNode(com_port=port)

            self._update_sub = (
                omni.kit.app.get_app_interface()
                .get_update_event_stream()
                .create_subscription_to_pop(
                    self._on_update, name="dobot_bridge_update"
                )
            )

            robot_path = self._robot_path_input.model.get_value_as_string().strip()
            self._art_init_pending = bool(robot_path)

            self._btn_connect.text = "● Verbunden"
            self._btn_connect.style = {"background_color": CLR_GREEN,
                                       "color": 0xFF000000}
            self._btn_connect.enabled = False
            self._conn_status_label.text  = f"● {port}"
            self._conn_status_label.style = {"font_size": 11, "color": CLR_GREEN}

            self._btn_disconnect.enabled   = True
            self._btn_install.enabled      = False
            self._port_input.enabled       = False
            self._robot_path_input.enabled = False
            self._set_buttons_enabled(True)
            self._set_status("Verbunden – warte auf Play...", CLR_YELLOW)

        except Exception as exc:
            self._set_status(
                f"Verbindungsfehler: {exc}  |  "
                "Roboter eingeschaltet? USB eingesteckt?",
                CLR_RED
            )

    def _on_update(self, event):
        """Frame-Callback: DC-Lazy-Init, Dobot-Polling, Modellkopplung, Dashboard."""
        if not self._ros_node:
            return

        # Lazy DC-Init: erst wenn Physik läuft (kein Warning-Spam)
        if self._art_init_pending and self._dc is None:
            try:
                import omni.timeline
                if omni.timeline.get_timeline_interface().is_playing():
                    robot_path = (
                        self._robot_path_input.model.get_value_as_string().strip()
                    )
                    self._init_articulation(robot_path, silent=True)
                    if self._dc is not None:
                        self._art_init_pending = False
            except Exception:
                pass

        try:
            pose, joints = self._ros_node.update_step()
        except Exception as exc:
            self._set_status(f"Update-Fehler: {exc}", CLR_RED)
            return

        # DC-Kopplung nur wenn Simulation läuft
        try:
            import omni.timeline
            if omni.timeline.get_timeline_interface().is_playing():
                self._drive_joints(joints)
        except Exception:
            pass

        self._update_display(pose, joints)

    def _on_disconnect_clicked(self):
        """Trennt Verbindung und setzt UI zurück."""
        self._cleanup()
        self._btn_connect.text  = "Connect"
        self._btn_connect.style = {}
        self._btn_connect.enabled = True
        self._btn_disconnect.enabled = False
        self._conn_status_label.text  = "● getrennt"
        self._conn_status_label.style = {"font_size": 11, "color": CLR_RED}
        self._port_input.enabled       = True
        self._robot_path_input.enabled = True
        self._set_buttons_enabled(False)
        self._set_status("Verbindung getrennt.", CLR_YELLOW)

    def _on_suction_toggle_clicked(self):
        """Schaltet Vakuumgreifer um (AN/AUS)."""
        if not self._ros_node:
            self._set_status("Keine Verbindung zum Dobot.", CLR_RED)
            return
        try:
            self._suction_active = not self._suction_active
            self._ros_node.set_suction(self._suction_active)
            self._btn_suction.text = (
                "Suction OFF" if self._suction_active else "Suction ON"
            )
            self._set_status(
                "Saugen aktiviert." if self._suction_active else "Saugen deaktiviert.",
                CLR_GREEN if self._suction_active else CLR_YELLOW,
            )
        except Exception as exc:
            self._set_status(f"Saugfehler: {exc}", CLR_RED)

    def _on_move_vertical_clicked(self, delta_mm: float):
        """Vertikalbewegung: +10 mm aufwärts, -10 mm abwärts."""
        if not self._ros_node:
            self._set_status("Keine Verbindung zum Dobot.", CLR_RED)
            return
        try:
            self._ros_node.move_vertical(delta_mm)
            direction = "hoch" if delta_mm > 0 else "runter"
            self._set_status(
                f"Bewege vertikal {direction} um {abs(delta_mm)} mm.", CLR_GREEN
            )
        except Exception as exc:
            self._set_status(f"Bewegungsfehler: {exc}", CLR_RED)

    def _on_jog_clicked(self, dx: float, dy: float):
        """JOG-Befehl in X-Y-Ebene: dx, dy in mm."""
        if not self._ros_node:
            self._set_status("Keine Verbindung zum Dobot.", CLR_RED)
            return
        try:
            self._ros_node.jog(dx, dy)
            self._set_status(f"JOG: Δx={dx:.1f} mm, Δy={dy:.1f} mm.", CLR_GREEN)
        except Exception as exc:
            self._set_status(f"JOG-Fehler: {exc}", CLR_RED)

    # ------------------------------------------------------------------
    # Ziel-Würfel-Tracking
    # ------------------------------------------------------------------

    def _on_tracking_rate_changed(self, value: float):
        self._tracking_rate_hz = max(0.5, min(10.0, value))
        self._tracking_rate_label.text = f"{self._tracking_rate_hz:.1f} Hz"

    def _on_cube_toggle_clicked(self):
        if not self._ros_node:
            self._set_status("Keine Verbindung.", CLR_RED)
            return
        if self._cube_spawned:
            self._despawn_target_cube()
        else:
            self._spawn_target_cube()

    def _spawn_target_cube(self):
        """Erstellt orangenen Ziel-Würfel in der Stage und startet Tracking-Thread."""
        try:
            import omni.usd
            from pxr import UsdGeom, UsdShade, Sdf, Gf
            stage = omni.usd.get_context().get_stage()
            if not stage:
                self._set_status("Keine USD-Stage verfügbar.", CLR_RED)
                return

            # Alten Würfel entfernen (Idempotenz)
            for p in (_CUBE_PRIM_PATH, _CUBE_MAT_PATH):
                if stage.GetPrimAtPath(p).IsValid():
                    stage.RemovePrim(p)

            # Startposition aus aktueller Armpose (mm → m)
            pose = self._ros_node.last_pose if self._ros_node else {}
            ix = pose.get('x', 200.0) / 1000.0
            iy = pose.get('y',   0.0) / 1000.0
            iz = pose.get('z',  50.0) / 1000.0

            # Würfel-Prim (2,5 cm Kantenlänge)
            cube = UsdGeom.Cube.Define(stage, _CUBE_PRIM_PATH)
            cube.GetSizeAttr().Set(0.025)

            xf = UsdGeom.Xformable(cube.GetPrim())
            xf.ClearXformOpOrder()
            xf.AddTranslateOp().Set(Gf.Vec3d(ix, iy, iz))

            # Orangenes Material
            mat = UsdShade.Material.Define(stage, _CUBE_MAT_PATH)
            shader = UsdShade.Shader.Define(stage, _CUBE_MAT_PATH + "/Shader")
            shader.CreateIdAttr("UsdPreviewSurface")
            shader.CreateInput(
                "diffuseColor", Sdf.ValueTypeNames.Color3f
            ).Set(Gf.Vec3f(1.0, 0.45, 0.0))
            shader.CreateInput(
                "opacity", Sdf.ValueTypeNames.Float
            ).Set(0.9)
            mat.CreateSurfaceOutput().ConnectToSource(
                shader.ConnectableAPI(), "surface"
            )
            UsdShade.MaterialBindingAPI(cube.GetPrim()).Bind(mat)

            self._cube_spawned = True
            self._btn_cube_toggle.text  = "Würfel entfernen"
            self._btn_cube_toggle.style = {
                "background_color": CLR_YELLOW, "color": 0xFF000000
            }

            self._stop_tracking_flag = False
            self._tracking_thread = threading.Thread(
                target=self._tracking_loop_thread,
                daemon=True,
                name="dobot_cube_track"
            )
            self._tracking_thread.start()
            self._set_status(
                f"Würfel gespawnt  x={pose.get('x', 200):.0f}  "
                f"y={pose.get('y', 0):.0f}  z={pose.get('z', 50):.0f}  "
                f"– Tracking {self._tracking_rate_hz:.1f} Hz",
                CLR_GREEN
            )
        except Exception as exc:
            self._set_status(f"Würfel-Fehler: {exc}", CLR_RED)

    def _despawn_target_cube(self):
        """Stoppt Tracking und entfernt Würfel aus Stage."""
        self._stop_tracking_flag = True
        if self._tracking_thread and self._tracking_thread.is_alive():
            self._tracking_thread.join(timeout=1.0)
        self._tracking_thread = None

        try:
            import omni.usd
            stage = omni.usd.get_context().get_stage()
            if stage:
                for p in (_CUBE_PRIM_PATH, _CUBE_MAT_PATH):
                    if stage.GetPrimAtPath(p).IsValid():
                        stage.RemovePrim(p)
        except Exception:
            pass

        self._cube_spawned = False
        self._btn_cube_toggle.text  = "Würfel spawnen"
        self._btn_cube_toggle.style = {}
        self._set_status("Würfel entfernt – Tracking gestoppt.", CLR_YELLOW)

    def _get_cube_world_position(self):
        """Liest Weltposition des Ziel-Würfels aus der USD-Stage (in Metern)."""
        try:
            import omni.usd
            from pxr import UsdGeom, Usd
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return None
            prim = stage.GetPrimAtPath(_CUBE_PRIM_PATH)
            if not prim.IsValid():
                return None
            t = (
                UsdGeom.Xformable(prim)
                .ComputeLocalToWorldTransform(Usd.TimeCode.Default())
                .ExtractTranslation()
            )
            return (float(t[0]), float(t[1]), float(t[2]))
        except Exception:
            return None

    def _tracking_loop_thread(self):
        """Richtet link_6-Vorderfläche auf die dem TCP zugewandte Würfelfläche aus (1 Hz).

        Statt des Würfelzentrums wird der Normalenvektor von Würfelzentrum → TCP
        berechnet und der TCP auf die Würfelfläche (Zentrum + Normale * Halbkante) bewegt.
        """
        device = self._ros_node.device if self._ros_node else None
        if not device:
            return
        move_fn = getattr(device, 'move_to', None)
        if not callable(move_fn):
            self._set_status("move_to nicht verfügbar für Tracking.", CLR_RED)
            return

        CUBE_HALF_MM = 12.5  # halbe Kantenlänge des Ziel-Würfels (0.025 m / 2 in mm)

        while not self._stop_tracking_flag and self._ros_node:
            try:
                pos = self._get_cube_world_position()
                if pos is not None:
                    # Würfelzentrum in mm
                    cx = pos[0] * 1000.0
                    cy = pos[1] * 1000.0
                    cz = pos[2] * 1000.0
                    r  = self._ros_node.last_pose.get('r', 0.0)

                    # Aktuelle TCP-Position (letzte bekannte Pose)
                    rx = self._ros_node.last_pose.get('x', cx)
                    ry = self._ros_node.last_pose.get('y', cy)
                    rz = self._ros_node.last_pose.get('z', cz)

                    # Richtungsvektor Würfelzentrum → TCP, normiert
                    dx, dy, dz = rx - cx, ry - cy, rz - cz
                    dist = math.sqrt(dx*dx + dy*dy + dz*dz)

                    if dist > 1.0:
                        # Ziel: Würfelfläche, die dem TCP zugewandt ist
                        nx, ny, nz = dx / dist, dy / dist, dz / dist
                        tx = cx + nx * CUBE_HALF_MM
                        ty = cy + ny * CUBE_HALF_MM
                        tz = cz + nz * CUBE_HALF_MM
                    else:
                        # TCP bereits sehr nah am Würfel – auf Zentrum fahren
                        tx, ty, tz = cx, cy, cz

                    move_fn(tx, ty, tz, r)
                    self._set_status(
                        f"● Tracking  x={tx:.1f}  y={ty:.1f}  z={tz:.1f}  "
                        f"({self._tracking_rate_hz:.1f} Hz)",
                        CLR_YELLOW
                    )
            except Exception as exc:
                self._set_status(f"Tracking-Fehler: {exc}", CLR_RED)

            _time.sleep(1.0 / max(0.1, self._tracking_rate_hz))

    # ------------------------------------------------------------------
    # Ressourcenverwaltung
    # ------------------------------------------------------------------

    def _cleanup(self):
        """Gibt alle Ressourcen frei: Threads, DC-Interface, ROS2-Node."""
        self._stop_play_flag = True
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=1.0)
        self._play_thread = None
        if self._play_task and not self._play_task.done():
            self._play_task.cancel()
        self._play_task = None

        # Tracking stoppen und Würfel aus Stage entfernen
        self._stop_tracking_flag = True
        if self._tracking_thread and self._tracking_thread.is_alive():
            self._tracking_thread.join(timeout=1.0)
        self._tracking_thread = None
        try:
            import omni.usd
            stage = omni.usd.get_context().get_stage()
            if stage:
                for p in (_CUBE_PRIM_PATH, _CUBE_MAT_PATH):
                    if stage.GetPrimAtPath(p).IsValid():
                        stage.RemovePrim(p)
        except Exception:
            pass
        self._cube_spawned = False

        self._update_sub = None
        self._dc = None
        self._dc_mod = None
        self._art_handle = None
        self._dof_handles = []
        self._art_init_pending = False
        if self._ros_node:
            try:
                self._ros_node.shutdown()
            except Exception:
                pass
            self._ros_node = None

    def on_shutdown(self):
        """Extension-Shutdown: Dock-Task canceln, Verbindung trennen, Fenster zerstören."""
        # Coroutine canceln: verhindert "extension object is still alive"-Warnung
        if self._dock_task and not self._dock_task.done():
            self._dock_task.cancel()
        self._dock_task = None
        self._cleanup()
        if self._window:
            self._window.destroy()
            self._window = None
