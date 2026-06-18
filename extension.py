"""
extension.py
============
Dobot ROS2 Bridge Extension für NVIDIA Isaac Sim.

Architektur:
    DobotROSNode        – kapselt die serielle Verbindung zum echten Dobot-Roboter
                          sowie den ROS2-Publisher für /joint_states.
    DobotBridgeExtension – omni.ext.IExt-Subklasse; verwaltet das UI-Fenster,
                           die Dynamic-Control-Kopplung und den Frame-Update-Loop.

Bidirektionale Kopplung (Digital Twin):
    Richtung 1  Echter Dobot → Isaac Sim
                update_step() liest Gelenkwinkel per USB (pydobot), berechnet
                die kinematischen Kompensationswinkel und setzt sie über die
                Isaac-Sim-Dynamic-Control-API direkt auf die Artikulationsgelenke.
    Richtung 2  Isaac Sim UI → Echter Dobot
                Jog- und Vertikalbewegungsbuttons senden move_to-Befehle an den
                echten Roboter über pydobot.

Einheitenstrategie:
    Interne Berechnungen in GRAD (einfachere Formeln, direkte pydobot-Werte).
    Konvertierung nach Radiant vor ALLEN DC-API-Aufrufen und dem ROS2-Publisher:
        - set_dof_state.pos           → Radiant (DC-API-Standard)
        - set_dof_position_target     → Radiant (PhysX-Pflicht: [-2π, 2π])
        - ROS2 JointState-Nachricht   → Radiant (ROS2-Konvention)

Kinematisches Modell (Dobot Magician, Parallelogramm-Mechanismus):
    pydobot liefert absolute Weltwinkel (von Horizontal) in Grad:
        j1  Basisgelenk (Yaw)
        j2  Oberarm-Pitch  (0° = Arm senkrecht nach oben)
        j3  Unterarm-Pitch (absolut – durch Parallelogramm nahe 0° = horizontal)
        j4  Endeffektordrehung (Servo)

    Formeln (aus joint_state_feedback.py, empirisch validiert):
        joint_1 = j1                                        [°]
        joint_2 = j2                                        [°]
        joint_5 = 90 + j2 − j3                             [°]
        joint_6 = rad(90 − j3) − π/4 + 0.185               [rad]
                ≈ 55.6° − j3                               [°, näherungsweise]

    Herleitung joint_5:
        Die +90°-Konstante resultiert aus dem URDF: joint_2 = 0° entspricht
        dem senkrechten Arm. Um die Vorderseite (link_5) horizontal zu halten,
        wenn joint_2 = 0° (Arm hoch), muss joint_5 ≈ 90° sein.
        Der Term (j2 − j3) kompensiert Abweichungen der Ist-Weltlage des Unterarms.

    Herleitung joint_6:
        Der Ausdruck (joint_5_rad − joint_2_rad) hebt den j2-Einfluss heraus:
            rad(90 + j2 − j3) − rad(j2) = rad(90 − j3)
        Das verbleibende (−π/4 + 0.185) ist ein empirischer Offset für die
        Endeffector-Geometrie dieses Dobot-Modells (link_6 USD-Nulllage).
"""

import asyncio        # Async-Task für das verzögerte Fenster-Docking
import math           # Radiant/Grad-Konvertierungen für Kinematik
import struct         # Byte-Packing für raw Dobot-Protokoll-Kommandos
import threading      # Hintergrundthread für blockierendes wait_for_cmd
import time as _time  # time.sleep im Hintergrundthread
import omni.ext       # Basis-Interface für Omniverse-Extensions
import omni.kit.app   # Zugriff auf App-Interface und Update-Event-Stream
import omni.kit.pipapi  # Pip-basierte Paketinstallation innerhalb Isaac Sim
import omni.ui as ui  # Omniverse UI-Framework für das Extension-Fenster

from .constants import (
    CLR_BG_DARK, CLR_BG_MID, CLR_BORDER, CLR_TEXT, CLR_TEXT_DIM,
    CLR_GREEN, CLR_RED, CLR_YELLOW, CLR_ACCENT,
    ROBOT_USD_PATH,
    HOME_X, HOME_Y, HOME_Z, HOME_R,
    GP3_PWM_ADDRESS, SERVO_FREQ_HZ, SERVO_MIN_DUTY, SERVO_MAX_DUTY,
)

# ---------------------------------------------------------------------------
# Lazy-Import-Caches für optionale Abhängigkeiten
# ---------------------------------------------------------------------------
# ROS2 (rclpy) und pydobot werden erst beim ersten Verbindungsaufbau geladen,
# da sie in einer reinen Isaac-Sim-Umgebung ohne ROS2-Bridge nicht vorhanden
# sein müssen und ein Fehler beim Modulstart den Extension-Start abbrechen würde.

_rclpy = None        # rclpy-Modul-Referenz nach erstem erfolgreichen Import
_Node = None         # rclpy.node.Node-Klasse
_JointState = None   # sensor_msgs.msg.JointState-Klasse
_Dobot = None        # pydobot.Dobot-Klasse

# USD-Pfade für den Ziel-Würfel (Tracking-Feature)
_CUBE_PRIM_PATH = "/World/DobotTargetCube"
_CUBE_MAT_PATH  = "/World/DobotTargetCubeMat"


def _try_import_ros():
    """Lädt rclpy, Node und JointState einmalig; gibt True zurück wenn erfolgreich.

    Der Lazy-Import-Mechanismus verhindert, dass fehlende ROS2-Pakete den
    Extension-Start blockieren. Einmal geladene Module werden in Modul-globalen
    Variablen gecacht, sodass der Import-Overhead nur beim ersten Aufruf anfällt.
    """
    global _rclpy, _Node, _JointState
    if _rclpy is not None:
        return True  # Bereits gecacht, direkt zurück
    try:
        import rclpy
        from rclpy.node import Node
        from sensor_msgs.msg import JointState
    except Exception:
        return False  # ROS2 nicht verfügbar
    _rclpy = rclpy
    _Node = Node
    _JointState = JointState
    return True


def _try_import_dobot():
    """Lädt pydobot.Dobot einmalig; gibt True zurück wenn erfolgreich.

    pydobot muss separat installiert werden (Button 'Install packages' in der UI
    oder manuell per pip). Der Import wird gecacht um wiederholte Fehler zu
    vermeiden.
    """
    global _Dobot
    if _Dobot is not None:
        return True  # Bereits gecacht
    try:
        from pydobot import Dobot
    except Exception:
        return False  # pydobot nicht installiert
    _Dobot = Dobot
    return True


# ---------------------------------------------------------------------------
# Klasse: DobotROSNode
# ---------------------------------------------------------------------------

class DobotROSNode:
    """Kapselt die Verbindung zum echten Dobot und den ROS2-Publisher.

    Verantwortlichkeiten:
    - Öffnet die serielle USB-Verbindung zum Dobot Magician über pydobot.
    - Initialisiert einen ROS2-Node und einen JointState-Publisher.
    - Liest pro Frame Gelenkwinkel (pose()) vom Dobot, berechnet die
      kinematischen Kompensationswinkel für das Isaac-Sim-Modell und
      publiziert sie auf /joint_states.
    - Stellt Steuermethoden bereit: Sauger, Vertikalbewegung, JOG X/Y.
    """

    def __init__(self, com_port: str):
        """Öffnet ROS2-Node und serielle Verbindung zum Dobot.

        Args:
            com_port: Serieller Port des Dobot, z.B. 'COM3' (Windows)
                      oder '/dev/ttyUSB0' (Linux/WSL).

        Raises:
            RuntimeError: Wenn ROS2-Imports oder pydobot nicht verfügbar sind
                          oder die Dobot-Verbindung fehlschlägt.
        """
        # Abhängigkeiten prüfen; schlägt hier fehlt, ist Verbindung unmöglich
        if not _try_import_ros():
            raise RuntimeError(
                "ROS2 imports konnten nicht geladen werden. "
                "Bitte prüfen Sie die ROS2-Installation."
            )
        if not _try_import_dobot():
            raise RuntimeError(
                "pydobot konnte nicht geladen werden. "
                "Bitte installieren Sie pydobot."
            )

        # ROS2-Kontext nur einmal initialisieren (auch bei mehrfachem Connect)
        if not _rclpy.ok():
            _rclpy.init()

        # ROS2-Node und Publisher für /joint_states (Queue-Tiefe 10)
        self._node = _Node('dobot_extension_bridge')
        self.publisher_ = self._node.create_publisher(
            _JointState, 'joint_states', 10
        )

        # Serielle Verbindung zum Dobot öffnen
        self.device = self._create_device(com_port)

        # Fallback-Puffer: letzte gültige Pose bei Kommunikationsunterbrechung
        self.last_valid_positions = [0.0, 0.0, 0.0, 0.0]
        self.last_pose = {'x': 0.0, 'y': 0.0, 'z': 0.0, 'r': 0.0}

    def _create_device(self, com_port: str):
        """Öffnet die pydobot-Verbindung; wirft RuntimeError bei Fehler."""
        try:
            return _Dobot(port=com_port)
        except Exception as exc:
            raise RuntimeError(f"Dobot-Verbindung fehlgeschlagen: {exc}")

    def update_step(self):
        """Liest aktuelle Pose vom Dobot, berechnet Gelenkwinkel und publiziert.

        Wird jeden Frame vom Update-Event-Stream aufgerufen (~60 Hz).

        Kinematik in Grad:
            joint_5 = j3 − j2        Parallelogramm-Kompensation
            joint_6 = 90 − j3 − j4   Endeffector vertikal

        Returns:
            (pose_dict, joints_deg):
                pose_dict:  {'x','y','z','r'} in mm/°
                joints_deg: [j1, j2, pos_j5, pos_j6] in GRAD
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

                    # Parallelogramm-Kompensation: Unterarm in Weltlage halten
                    pos_j5 = j3 - j2
                    # Endeffector vertikal: Kette j2+(j3-j2)+j6=90 → j6=90-j3-j4
                    pos_j6 = 90.0 - j3 - j4

                    joints_deg = [j1, j2, pos_j5, pos_j6]
                    self.last_valid_positions = joints_deg
                    self.last_pose = {'x': x, 'y': y, 'z': z, 'r': r, 'j1': j1}

                    # ROS2 erwartet Radiant (Konvention sensor_msgs/JointState)
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
        """Aktiviert oder deaktiviert den Vakuumerzeuger über SW1.

        suck(enable) sendet Dobot-Kommando ID 62 und schaltet den SW1-Ausgang
        (Saugnapf-Interface). GP1 versorgt den Vakuumerzeuger mit 24 V;
        SW1 liefert das Ein/Aus-Steuersignal.

        Args:
            active: True = Vakuumpumpe an, False = Vakuumpumpe aus.

        Raises:
            RuntimeError: Wenn kein passendes Kommando in pydobot gefunden.
        """
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")

        # Priorität: suck() ist die korrekte pydobot-Methode für SW1 (ID 62)
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
        """Bewegt den Dobot senkrecht um delta_mm Millimeter.

        Liest die aktuelle kartesische Pose, addiert delta_mm auf Z und
        sendet einen move_to-Befehl an den echten Roboter.

        Args:
            delta_mm: Positive Werte = aufwärts, negative = abwärts.
        """
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")
        pose = self.device.pose()
        if pose is None or len(pose) < 4:
            raise RuntimeError("Aktuelle Pose konnte nicht gelesen werden.")
        x, y, z, r = pose[0], pose[1], pose[2], pose[3]
        move_fn = getattr(self.device, 'move_to', None)
        if not callable(move_fn):
            raise RuntimeError('move_to-Methode in pydobot nicht gefunden.')
        return move_fn(x, y, z + delta_mm, r)

    def jog(self, dx: float, dy: float):
        """Bewegt den Dobot horizontal um (dx, dy) Millimeter in der X-Y-Ebene.

        Args:
            dx: Versatz in X-Richtung [mm].
            dy: Versatz in Y-Richtung [mm].
        """
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")
        pose = self.device.pose()
        if pose is None or len(pose) < 4:
            raise RuntimeError("Aktuelle Pose konnte nicht gelesen werden.")
        x, y, z, r = pose[0], pose[1], pose[2], pose[3]
        move_fn = getattr(self.device, 'move_to', None)
        if not callable(move_fn):
            raise RuntimeError('move_to-Methode in pydobot nicht gefunden.')
        return move_fn(x + dx, y + dy, z, r)

    def go_home(self):
        """Fährt zur gespeicherten Nullposition (HOME_X/Y/Z/R aus constants.py)."""
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")
        move_fn = getattr(self.device, 'move_to', None)
        if not callable(move_fn):
            raise RuntimeError('move_to nicht verfügbar.')
        return move_fn(HOME_X, HOME_Y, HOME_Z, HOME_R)

    def set_servo_gp3(self, angle_deg: float):
        """Setzt den RC-Servo an GP3-PWM1 auf den angegebenen Winkel (0–180°).

        Sendet Dobot-Protokoll-Kommando ID 130 (IO-Multiplexing → PWM-Modus)
        und ID 132 (IO-PWM) direkt, da pydobot dafür keine High-Level-API hat.

        Der Duty-Cycle wird linear interpoliert:
            0°   → SERVO_MIN_DUTY (1 ms Puls bei 50 Hz)
            180° → SERVO_MAX_DUTY (2 ms Puls bei 50 Hz)
        """
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")
        try:
            from pydobot.message import Message as _Msg
        except ImportError:
            raise RuntimeError("pydobot.message nicht importierbar.")

        angle_deg = max(0.0, min(180.0, float(angle_deg)))
        duty = SERVO_MIN_DUTY + (angle_deg / 180.0) * (SERVO_MAX_DUTY - SERVO_MIN_DUTY)

        # Pin-Modus auf PWM setzen (Multiplex-Typ 2)
        mux = _Msg()
        mux.id = 130
        mux.ctrl = 0x03
        mux.params = bytearray()
        mux.params.extend(struct.pack('B', GP3_PWM_ADDRESS))
        mux.params.extend(struct.pack('B', 2))
        self.device._send_command(mux)

        # PWM-Frequenz und Duty-Cycle setzen
        pwm = _Msg()
        pwm.id = 132
        pwm.ctrl = 0x03
        pwm.params = bytearray()
        pwm.params.extend(struct.pack('B', GP3_PWM_ADDRESS))
        pwm.params.extend(struct.pack('f', SERVO_FREQ_HZ))
        pwm.params.extend(struct.pack('f', duty))
        self.device._send_command(pwm)

    def play_sequence(self, poses: list):
        """Spielt eine aufgezeichnete Teach-in-Sequenz ab.

        Jeder Eintrag in poses ist ein Tupel (x, y, z, r, suction_active).
        move_to() und suck() sind beide in der Dobot-internen Queue, d. h.
        sie werden exakt in dieser Reihenfolge ausgeführt – der Arm bewegt
        sich zu Punkt N, bevor der Sauger für Punkt N+1 aktiviert wird.
        Nach dem letzten Punkt fährt der Arm zur Nullposition zurück.
        """
        if not self.device:
            raise RuntimeError("Dobot nicht verbunden.")
        move_fn = getattr(self.device, 'move_to', None)
        if not callable(move_fn):
            raise RuntimeError('move_to nicht verfügbar.')

        for x, y, z, r, suction_active in poses:
            move_fn(x, y, z, r)
            self.set_suction(suction_active)

        # Zurück zur Nullposition, Sauger aus
        move_fn(HOME_X, HOME_Y, HOME_Z, HOME_R)
        self.set_suction(False)

    def stop_sequence(self):
        """Stoppt die Bewegungssequenz sofort und setzt die Dobot-Queue zurück."""
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
        """Trennt die serielle Verbindung und zerstört den ROS2-Node."""
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
    """Omniverse-Extension: UI-Fenster, DC-Kopplung und Update-Loop.

    Lebenszyklus:
        on_startup()   – Fenster erstellen, Zustand initialisieren,
                         Docking-Task starten.
        _on_update()   – Wird jeden Frame aufgerufen; liest Dobot-Daten,
                         treibt das Simulationsmodell, aktualisiert das Dashboard.
        on_shutdown()  – Alle Ressourcen freigeben.

    Dynamic-Control-Initialisierung (Lazy):
        Die Isaac-Sim-DC-API benötigt eine laufende Physik-Simulation.
        Deshalb wird get_articulation() erst dann aufgerufen, wenn
        omni.timeline meldet, dass die Simulation läuft (Play gedrückt).
        Bis dahin wird _art_init_pending=True gehalten und jedes Frame
        ein erneuter Versuch unternommen.
    """

    def on_startup(self, ext_id: str):
        """Initialisiert Zustand, erstellt das UI-Fenster und startet Docking.

        Args:
            ext_id: Von Isaac Sim übergebene Extension-ID (nicht verwendet).
        """
        # --- Verbindungszustand ---
        self._ros_node: DobotROSNode | None = None   # Aktive Dobot-Verbindung
        self._update_sub = None                       # Update-Event-Subscription
        self._suction_active = False                  # Aktueller Sauger-Status

        # --- Servo-Zustand ---
        self._servo_angle       = 90.0   # Aktueller Servo-Winkel [°]
        self._servo_world_angle = None   # Servo + J1 = Weltwinkel; None = kein Tracking

        # --- Teach-in / Pick-and-Place-Aufzeichnung ---
        self._recorded_poses    = []      # Liste von (x,y,z,r,suction)
        self._rec_suction_state = False   # Saugzustand für Aufnahme (nicht physisch)
        self._play_task         = None    # asyncio.Task der laufenden Sequenz
        self._play_thread       = None    # threading.Thread für Wiedergabe
        self._stop_play_flag    = False   # True → Hintergrundthread soll abbrechen

        # --- Ziel-Würfel-Tracking ---
        self._cube_spawned         = False   # Würfel aktuell in Stage vorhanden
        self._stop_tracking_flag   = False   # Tracking-Thread-Abbruchsignal
        self._tracking_thread      = None    # threading.Thread für Tracking
        self._tracking_rate_hz     = 2.0     # Sendefrequenz [Hz]

        # --- Dynamic-Control-Zustand ---
        self._dc = None          # DC-Interface-Instanz nach Initialisierung
        self._dc_mod = None      # dc_mod-Modul (für DofState-Konstruktor nötig)
        self._art_handle = None  # Artikulations-Handle von get_articulation()
        self._dof_handles = []   # Liste von DOF-Handles [j1, j2, j5, j6]
        # Wird True gesetzt wenn Connect gedrückt; False nach erfolgreicher
        # DC-Initialisierung (erst möglich wenn Simulation läuft)
        self._art_init_pending = False

        # --- Async-Docking-Task ---
        self._dock_task = None  # asyncio.Task; wird in on_shutdown gecancelt

        # --- UI-Fenster ---
        self._window = ui.Window("Dobot ROS2 Bridge", width=490, height=590)

        _dim  = {"font_size": 11, "color": CLR_TEXT_DIM}
        _norm = {"font_size": 11, "color": CLR_TEXT}

        with self._window.frame:
            with ui.VStack(spacing=6, padding=10):

                # ── Verbindung ───────────────────────────────────────────
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
                    # Verbindungsstatus-Indikator (kein Button – kein Layout-Konflikt)
                    self._conn_status_label = ui.Label(
                        "● getrennt",
                        style={"font_size": 11, "color": CLR_RED}
                    )

                # ── Robot-USD ────────────────────────────────────────────
                with ui.HStack(spacing=6, height=24):
                    ui.Label("Robot USD:", width=72, style=_norm)
                    self._robot_path_input = ui.StringField()
                    self._robot_path_input.model.set_value(ROBOT_USD_PATH)

                # ── Pakete installieren ──────────────────────────────────
                with ui.HStack(spacing=6, height=24):
                    ui.Spacer(width=72)
                    self._btn_install = ui.Button(
                        "Install packages", width=140,
                        clicked_fn=self._on_install_clicked
                    )

                ui.Line(style={"color": CLR_BORDER}, height=1)

                # ── Tab-Leiste ───────────────────────────────────────────
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

                # ── Tab 0 – Steuerung ────────────────────────────────────
                self._frame_tab0 = ui.Frame(visible=True)
                with self._frame_tab0:
                    with ui.VStack(spacing=6):
                        # Sauger + Vertikal
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

                        # JOG X / Y
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

                        # Servo GP3 (auch im Steuerung-Tab)
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
                            self._tracking_rate_slider.model.set_value(2.0)
                            self._tracking_rate_label = ui.Label(
                                "2.0 Hz", width=46,
                                style={"font_size": 12, "color": CLR_TEXT}
                            )
                            self._tracking_rate_slider.model.add_value_changed_fn(
                                lambda m: self._on_tracking_rate_changed(
                                    m.get_value_as_float()
                                )
                            )
                            self._btn_cube_toggle.enabled = False

                # ── Tab 1 – Pick & Place ─────────────────────────────────
                self._frame_tab1 = ui.Frame(visible=False)
                with self._frame_tab1:
                    with ui.VStack(spacing=6):
                        # Go Home
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

                        # Virtueller Sauger-Toggle (nur für Aufnahme, kein Hardware-Befehl)
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

                # ── Status-Dashboard (immer sichtbar) ────────────────────
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

        # Fenster nach zwei Frames neben das Property-Panel docken.
        # Zwei Frames Verzögerung nötig, da das Fenster zunächst registriert
        # werden muss, bevor dock_in() den Workspace-Eintrag findet.
        self._dock_task = asyncio.ensure_future(self._dock_window_deferred())

    async def _dock_window_deferred(self):
        """Dockt das Fenster als Tab neben das Isaac-Sim-Property-Panel.

        Wartet zwei Update-Frames, damit das Fenster im Workspace registriert
        ist, bevor dock_in() aufgerufen wird.
        """
        await omni.kit.app.get_app().next_update_async()
        await omni.kit.app.get_app().next_update_async()
        prop = ui.Workspace.get_window("Property")
        if prop:
            self._window.dock_in(prop, ui.DockPosition.SAME)

    def _switch_tab(self, index: int):
        """Wechselt zwischen den Tabs 'Steuerung' (0) und 'Pick & Place' (1)."""
        self._active_tab = index
        self._frame_tab0.visible = (index == 0)
        self._frame_tab1.visible = (index == 1)
        active   = {"background_color": CLR_BG_MID,  "color": CLR_TEXT}
        inactive = {"background_color": CLR_BG_DARK, "color": CLR_TEXT_DIM}
        self._tab_btn_0.style = active   if index == 0 else inactive
        self._tab_btn_1.style = inactive if index == 0 else active

    # ------------------------------------------------------------------
    # Hilfsmethoden für UI-Aktualisierungen
    # ------------------------------------------------------------------

    def _set_status(self, text: str, color: int = CLR_TEXT_DIM):
        """Setzt den Text und die Farbe der Status-Zeile im Dashboard."""
        if self._status_label:
            self._status_label.text = text
            self._status_label.style = {"color": color}

    def _update_display(self, pose: dict, joints_deg: list):
        """Aktualisiert Pose- und Gelenkwinkel-Anzeige im Dashboard.

        Args:
            pose:       Dict mit Schlüsseln 'x', 'y', 'z', 'r' (mm / °).
            joints_deg: Liste [j1, j2, j5, j6] in GRAD (direkt anzeigbar).
        """
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
        """Schaltet alle Steuertasten (beide Tabs) ein oder aus."""
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
    # Dynamic-Control-Initialisierung
    # ------------------------------------------------------------------

    def _init_articulation(self, robot_path: str, silent: bool = False):
        """Verbindet die DC-API mit der Roboter-Artikulation in der Szene.

        get_articulation() schlägt fehl, solange die Physik-Simulation
        nicht läuft. Deshalb wird diese Methode mit silent=True wiederholt
        aufgerufen, bis die Simulation gestartet wurde (Play). Erst dann
        werden die DOF-Handles der vier Gelenke abgerufen.

        Args:
            robot_path: USD-Pfad der Artikulations-Root, z.B.
                        '/World/magician/base_link'.
            silent:     True = keine Fehlermeldung in der Status-Zeile.
                        Wird beim automatischen Retry gesetzt.
        """
        # Zustand zurücksetzen (erlaubt Neuinitialisierung nach Disconnect)
        self._dc = None
        self._dc_mod = None
        self._art_handle = None
        self._dof_handles = []
        if not robot_path:
            return
        try:
            from omni.isaac.dynamic_control import _dynamic_control as dc_mod
            dc = dc_mod.acquire_dynamic_control_interface()

            # get_articulation() liefert INVALID_HANDLE wenn Physik nicht läuft
            art = dc.get_articulation(robot_path)
            if art == dc_mod.INVALID_HANDLE:
                if not silent:
                    self._set_status(
                        "Warte auf Physik-Simulation (Play drücken)...",
                        CLR_YELLOW
                    )
                return  # Retry beim nächsten Frame

            # Artikulation gefunden → Interface und Handle sichern
            self._dc = dc
            self._dc_mod = dc_mod   # Modul-Referenz für DofState-Konstruktor
            self._art_handle = art

            # DOF-Handles für die vier Gelenke holen
            joint_names = ['joint_1', 'joint_2', 'joint_5', 'joint_6']
            for jname in joint_names:
                dof = dc.find_articulation_dof(art, jname)
                # None als Platzhalter wenn Gelenk nicht gefunden
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

    # ------------------------------------------------------------------
    # Gelenkwinkel-Übertragung ans Simulationsmodell
    # ------------------------------------------------------------------

    def _drive_joints(self, joints_deg: list):
        """Setzt Gelenkwinkel direkt auf das Isaac-Sim-Simulationsmodell.

        Nutzt zwei komplementäre DC-API-Aufrufe pro Gelenk:
          1. set_dof_state(..., STATE_POS)  – kinematischer Zustand direkt.
          2. set_dof_position_target(...)   – PhysX-Drive-Ziel.

        Beide DC-API-Aufrufe erwarten Radiant. Die Formeln in update_step()
        liefern Grad (einfachere Arithmetik), Konvertierung erfolgt hier.

        Args:
            joints_deg: Liste [j1, j2, j5, j6] in GRAD.
        """
        if not self._dc or self._art_handle is None or not self._dof_handles:
            return

        for dof_handle, angle_deg in zip(self._dof_handles, joints_deg):
            if dof_handle is None:
                continue
            # DC-API erwartet Radiant für beide Aufrufe
            rad = math.radians(float(angle_deg))

            try:
                state = self._dc_mod.DofState()
                state.pos = rad
                state.vel = 0.0
                self._dc.set_dof_state(dof_handle, state, self._dc_mod.STATE_POS)
            except Exception:
                pass

            try:
                self._dc.set_dof_position_target(dof_handle, rad)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # UI-Callback-Methoden
    # ------------------------------------------------------------------

    def _on_go_home_clicked(self):
        """Fährt den Dobot zur gespeicherten Nullposition."""
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
        """Ändert den Servo-Winkel an GP3 um delta Grad.

        Merkt gleichzeitig den Weltwinkel (servo + J1) für die automatische
        J1-Kompensation während der Play-Sequenz.
        """
        if not self._ros_node:
            self._set_status("Keine Verbindung.", CLR_RED)
            return
        self._servo_angle = max(0.0, min(180.0, self._servo_angle + delta))
        try:
            self._ros_node.set_servo_gp3(self._servo_angle)
            # Weltwinkel-Referenz: servo_angle + j1 bleibt konstant im Raum
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
        """Schaltet Saugzustand um UND speichert sofort einen Waypoint.

        Kein Hardware-Befehl – der gespeicherte Zustand wird erst bei Play
        als tatsächlicher Schaltbefehl an den Dobot gesendet.
        """
        self._rec_suction_state = not self._rec_suction_state
        if self._rec_suction_state:
            self._btn_rec_suction.text  = "Saugen EIN"
            self._btn_rec_suction.style = {"background_color": CLR_GREEN,
                                           "color": 0xFF000000}
        else:
            self._btn_rec_suction.text  = "Saugen AUS"
            self._btn_rec_suction.style = {}

        # Waypoint mit dem neuen Saugzustand automatisch aufzeichnen
        if self._ros_node:
            self._do_record_point()

    def _rebuild_rec_list(self):
        """Baut die scrollbare Waypoint-Liste aus self._recorded_poses neu auf."""
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
        """Speichert die aktuelle Pose + virtuellen Saugzustand als Waypoint."""
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
        """Manuelle Waypoint-Aufnahme über 'Rec Punkt'-Button."""
        if not self._ros_node:
            self._set_status("Keine Verbindung.", CLR_RED)
            return
        self._do_record_point()

    def _on_clear_recording_clicked(self):
        """Löscht alle Waypoints und setzt den virtuellen Sauger zurück."""
        self._recorded_poses    = []
        self._rec_suction_state = False
        self._btn_rec_suction.text  = "Saugen AUS"
        self._btn_rec_suction.style = {}
        self._rec_count_label.text  = "0 Pkt."
        self._rebuild_rec_list()
        self._set_status("Aufzeichnung gelöscht.", CLR_YELLOW)

    def _on_play_clicked(self):
        """Startet die Wiedergabe-Sequenz in einem Hintergrundthread."""
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
        """Führt die Teach-in-Sequenz im Hintergrundthread aus.

        Warum threading statt asyncio?
        ───────────────────────────────
        wait_for_cmd() in pydobot ist eine blockierende Polling-Schleife.
        Im asyncio-Kontext würde sie den Event-Loop einfrieren.
        Im Hintergrundthread blockiert nur dieser Thread; Isaac Sim und das
        Update-Event-System laufen auf dem Hauptthread ungehindert weiter.
        pydobots RLock serialisiert alle seriellen Zugriffe thread-sicher.
        """
        device = self._ros_node.device if self._ros_node else None
        if not device:
            self._set_status("Verbindung verloren.", CLR_RED)
            return
        move_fn  = getattr(device, 'move_to', None)
        wait_fn  = getattr(device, 'wait_for_cmd', None)
        if not callable(move_fn):
            self._set_status("move_to nicht in pydobot verfügbar.", CLR_RED)
            return

        APPROACH_MM = 10.0   # 1 cm Sicherheitsabstand von oben

        def _move_and_wait(tx, ty, tz, tr):
            """Bewegt und wartet blockierend; gibt False zurück wenn Abbruch."""
            idx = move_fn(tx, ty, tz, tr)
            if callable(wait_fn) and idx is not None:
                wait_fn(idx)
            else:
                _time.sleep(2.0)
            return not self._stop_play_flag

        def _servo_j1_compensate():
            """Servo so anpassen, dass Weltorientierung des Objekts erhalten bleibt."""
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
                    # 1 cm über dem Zielpunkt anfahren (seitliches Annähern vermeiden)
                    if not _move_and_wait(x, y, z + APPROACH_MM, r):
                        break
                    _servo_j1_compensate()
                    _time.sleep(0.1)

                # Eigentlichen Zielpunkt anfahren
                if not _move_and_wait(x, y, z, r):
                    break
                _servo_j1_compensate()

                if self._stop_play_flag or not self._ros_node:
                    break

                # Saugzustand schalten (nach Positionserreichen)
                self._ros_node.set_suction(suction_active)
                _time.sleep(0.5)   # Druckaufbau / -abbau

                if suction_changes:
                    # Sicher 1 cm abheben (Objekt nicht seitlich wegziehen)
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
        """Bricht die laufende Sequenz sofort ab."""
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
        """Öffnet Dobot-Verbindung und startet den Update-Loop."""
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

            # Verbunden → Connect-Button grün, Status-Indikator grün
            self._btn_connect.text = "● Verbunden"
            self._btn_connect.style = {"background_color": CLR_GREEN,
                                       "color": 0xFF000000}
            self._btn_connect.enabled = False
            self._conn_status_label.text  = f"● {port}"
            self._conn_status_label.style = {"font_size": 11, "color": CLR_GREEN}

            self._btn_disconnect.enabled = True
            self._btn_install.enabled    = False
            self._port_input.enabled     = False
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
        """Frame-Update-Callback: DC-Init, Dobot-Polling und Modellkopplung.

        Wird jeden Frame von Isaac Sims Update-Event-Stream aufgerufen.

        Ablauf pro Frame:
          1. Falls DC-Initialisierung ausstehend und Simulation läuft:
             _init_articulation() aufrufen (Retry bis Erfolg).
          2. update_step() aufrufen: Dobot auslesen, JointState publizieren.
          3. _drive_joints(): Winkel auf das Simulationsmodell übertragen.
          4. Dashboard mit aktuellen Werten aktualisieren.
        """
        if not self._ros_node:
            return

        # Lazy DC-Initialisierung: nur wenn Simulation läuft (kein Warning-Spam)
        if self._art_init_pending and self._dc is None:
            try:
                import omni.timeline
                if omni.timeline.get_timeline_interface().is_playing():
                    robot_path = (
                        self._robot_path_input.model.get_value_as_string().strip()
                    )
                    self._init_articulation(robot_path, silent=True)
                    if self._dc is not None:
                        self._art_init_pending = False  # Erfolgreich, kein Retry mehr
            except Exception:
                pass

        # Dobot-Daten lesen und ROS2 publizieren (immer)
        try:
            pose, joints = self._ros_node.update_step()
        except Exception as exc:
            self._set_status(f"Update-Fehler: {exc}", CLR_RED)
            return

        # DC-Kopplung nur wenn Simulation läuft (sonst DcSetDofState-Warnings)
        try:
            import omni.timeline
            if omni.timeline.get_timeline_interface().is_playing():
                self._drive_joints(joints)
        except Exception:
            pass

        self._update_display(pose, joints)

    def _on_disconnect_clicked(self):
        """Trennt Dobot-Verbindung und setzt UI zurück."""
        self._cleanup()
        self._btn_connect.text  = "Connect"
        self._btn_connect.style = {}          # Standard-Style wiederherstellen
        self._btn_connect.enabled = True
        self._btn_disconnect.enabled = False
        self._conn_status_label.text  = "● getrennt"
        self._conn_status_label.style = {"font_size": 11, "color": CLR_RED}
        self._port_input.enabled = True
        self._robot_path_input.enabled = True
        self._set_buttons_enabled(False)
        self._set_status("Verbindung getrennt.", CLR_YELLOW)

    def _on_suction_toggle_clicked(self):
        """Schaltet den Vakuumgreifer um (AN/AUS)."""
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
        """Sendet einen Vertikalbewegungsbefehl an den echten Dobot.

        Args:
            delta_mm: +10 = 10 mm aufwärts, -10 = 10 mm abwärts.
        """
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
        """Sendet einen JOG-Befehl in der X-Y-Ebene an den echten Dobot.

        Args:
            dx: Versatz X [mm].
            dy: Versatz Y [mm].
        """
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
        """Erstellt einen orangenen Würfel in der Isaac-Sim-Stage an der aktuellen
        Armposition. Startet gleichzeitig den Tracking-Thread."""
        try:
            import omni.usd
            from pxr import UsdGeom, UsdShade, Sdf, Gf
            stage = omni.usd.get_context().get_stage()
            if not stage:
                self._set_status("Keine USD-Stage verfügbar.", CLR_RED)
                return

            # Alten Würfel entfernen falls vorhanden
            for p in (_CUBE_PRIM_PATH, _CUBE_MAT_PATH):
                if stage.GetPrimAtPath(p).IsValid():
                    stage.RemovePrim(p)

            # Startposition = aktuelle Armpose (mm → m)
            pose = self._ros_node.last_pose if self._ros_node else {}
            ix = pose.get('x', 200.0) / 1000.0
            iy = pose.get('y',   0.0) / 1000.0
            iz = pose.get('z',  50.0) / 1000.0

            # Würfel-Prim anlegen (2,5 cm Kantenlänge)
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

            # Tracking-Thread starten
            self._stop_tracking_flag = False
            self._tracking_thread = threading.Thread(
                target=self._tracking_loop_thread,
                daemon=True,
                name="dobot_cube_track"
            )
            self._tracking_thread.start()
            self._set_status(
                f"Würfel gespawnt  x={pose.get('x',200):.0f}  "
                f"y={pose.get('y',0):.0f}  z={pose.get('z',50):.0f}  "
                f"– Tracking {self._tracking_rate_hz:.1f} Hz",
                CLR_GREEN
            )
        except Exception as exc:
            self._set_status(f"Würfel-Fehler: {exc}", CLR_RED)

    def _despawn_target_cube(self):
        """Stoppt Tracking und entfernt den Würfel aus der Stage."""
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
        """Liest die Weltposition des Ziel-Würfels aus der USD-Stage.

        Returns:
            (x, y, z) in Metern oder None wenn Würfel nicht vorhanden.
        """
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
        """Sendet in regelmäßigen Abständen move_to-Befehle an den echten Dobot,
        sodass dieser der aktuellen Würfelposition folgt.

        Die Rate wird über den Slider im Steuerung-Tab eingestellt.
        Isaac-Sim-Einheit Meter wird in Dobot-Einheit mm umgerechnet (×1000).
        Dabei wird dieselbe r-Rotation wie die letzte bekannte Pose beibehalten.
        """
        device = self._ros_node.device if self._ros_node else None
        if not device:
            return
        move_fn = getattr(device, 'move_to', None)
        if not callable(move_fn):
            self._set_status("move_to nicht verfügbar für Tracking.", CLR_RED)
            return

        while not self._stop_tracking_flag and self._ros_node:
            try:
                pos = self._get_cube_world_position()
                if pos is not None:
                    x = pos[0] * 1000.0   # m → mm
                    y = pos[1] * 1000.0
                    z = pos[2] * 1000.0
                    r = self._ros_node.last_pose.get('r', 0.0)
                    move_fn(x, y, z, r)
                    self._set_status(
                        f"● Tracking  x={x:.1f}  y={y:.1f}  z={z:.1f}  "
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
        """Gibt alle Verbindungs-Ressourcen frei (ROS2-Node, DC-Interface, Play-Task)."""
        # Hintergrundthread signalisieren und kurz auf Beendigung warten
        self._stop_play_flag = True
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=1.0)
        self._play_thread = None
        if self._play_task and not self._play_task.done():
            self._play_task.cancel()
        self._play_task = None

        # Tracking-Thread stoppen und Würfel aus Stage entfernen
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

        self._update_sub = None       # Update-Subscription freigeben
        self._dc = None               # DC-Interface loslassen
        self._dc_mod = None
        self._art_handle = None
        self._dof_handles = []
        self._art_init_pending = False
        if self._ros_node:
            try:
                self._ros_node.shutdown()  # COM-Port und ROS2-Node schließen
            except Exception:
                pass
            self._ros_node = None

    def on_shutdown(self):
        """Extension-Shutdown: Dock-Task canceln, Verbindung trennen, Fenster zerstören."""
        # Async-Task canceln, damit der Coroutine-Frame self nicht mehr referenziert
        # (verhindert die "extension object is still alive"-Warnung von omni.ext)
        if self._dock_task and not self._dock_task.done():
            self._dock_task.cancel()
        self._dock_task = None
        self._cleanup()
        if self._window:
            self._window.destroy()
            self._window = None
