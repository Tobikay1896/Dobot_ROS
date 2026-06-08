"""
extension.py
============
Haupteinstiegspunkt. Setzt alle Komponenten zusammen:
- UIBuilder → baut das Fenster
- Logger → Logausgabe
- NodeManager → Datenhaltung + USD-Zugriff
- APIClient → REST-API
- MQTTHandler → Live-Daten
- SuctionGripper → physikalisches Greifen
- TimelineHandler → SIM-Loop
- Routines → Automatisierte Abläufe
"""

import os
import asyncio
import omni.ext
import omni.kit.app

from .constants import (
    CLR_GREEN, CLR_RED, CLR_YELLOW, CLR_TEXT_DIM,
    CLR_ORANGE, CLR_ACCENT,
)
from .logger import Logger
from .node_manager import NodeManager
from .api_client import APIClient
from .mqtt_handler import MQTTHandler
from .suction_gripper import SuctionGripper
from .timeline_handler import TimelineHandler
from .routines import Routines
from .ui_builder import UIBuilder


async def _cancel_and_await(tasks):
    """Bricht Tasks ab und wartet auf deren Beendigung."""
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class MyExtension(omni.ext.IExt):
    """Omniverse-Extension: Maze Runner Control Center."""

    # =================================================================
    # LIFECYCLE
    # =================================================================
    def on_startup(self, ext_id):
        # 1) asyncio-Loop sicherstellen
        try:
            self._loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

        self._ext_id = ext_id
        self._is_running = True
        self._active_tasks = []
        self.sim_mode = True   # public, damit Routines drauf zugreifen können

        # 2) UI bauen
        self._ui = UIBuilder()
        self._ui.build(
            on_refresh=self._refresh_nodes,
            on_restart=self._restart_extension,
            on_toggle_sim=self._toggle_sim_mode,
            on_clear_log=lambda: self.logger.clear(),
            on_gesamt=self._start_gesamtprozess,
        )

        # 3) Komponenten initialisieren
        self.logger = Logger(self._ui.log_container, self._loop)

        json_path = os.path.join(os.path.dirname(__file__), "nodes_db.json")
        self.node_manager = NodeManager(json_path, self.logger)
        self.node_manager.list_container = self._ui.list_container
        self.node_manager.node_count_label = self._ui.node_count_label

        self.suction = SuctionGripper(self.logger)

        self.api = APIClient(self.node_manager, self.logger, self._set_status_text)
        self.mqtt = MQTTHandler(self.node_manager, self.logger, self._loop,
                                self._set_status_text)
        self.timeline = TimelineHandler(self.suction, self.node_manager, self.logger)
        self.routines = Routines(self)
        self.timeline.set_routines(self.routines)

        # 4) Daten laden + Timeline abonnieren
        self.node_manager.load(self._on_control_clicked)
        self.timeline.subscribe()

        # 5) Sauggreifer-Initialzustand: Startposition speichern + Deckel dynamisch
        # Mit kurzer Verzögerung, damit die Stage vollständig geladen ist
        t = asyncio.ensure_future(self._init_suction())
        self._active_tasks.append(t)

        self.logger.log("Extension gestartet", "info")
        self.logger.log("SIM-Modus aktiv", "info")

    async def _init_suction(self):
        """Stage-Ready abwarten, dann Deckel-Startposition sichern und dynamisch schalten."""
        import asyncio as _asyncio
        await _asyncio.sleep(0.5)
        self.suction.save_start_position()
        self.suction._set_target_kinematic(False)

    # =================================================================
    def on_shutdown(self):
        """Sauberes Aufräumen."""
        self._is_running = False

        # MQTT trennen
        try:
            self.mqtt.stop()
        except Exception:
            pass

        # Sauggreifer reset
        try:
            self.suction.reset()
        except Exception:
            pass

        # Timeline
        self.timeline.unsubscribe()

        # Tasks abbrechen
        tasks_to_cancel = [t for t in self._active_tasks if not t.done()]
        self._active_tasks = []

        # Fenster zerstören
        if self._ui.window:
            self._ui.window.destroy()
            self._ui.window = None

        # Restliche Tasks abräumen
        if tasks_to_cancel:
            asyncio.ensure_future(_cancel_and_await(tasks_to_cancel))

        print("[MazeRunner] Shutdown.")

    # =================================================================
    # UI-CALLBACKS
    # =================================================================
    def _refresh_nodes(self):
        """Button: JSON neu laden."""
        self.node_manager.load(self._on_control_clicked)

    def _restart_extension(self):
        """Button: Extension neu starten."""
        self.logger.log("Extension wird neu gestartet...", "info")
        ext_manager = omni.kit.app.get_app().get_extension_manager()
        ext_manager.set_extension_enabled(self._ext_id, False)
        ext_manager.set_extension_enabled(self._ext_id, True)

    def _toggle_sim_mode(self):
        """Button: zwischen SIM und LIVE wechseln."""
        self.sim_mode = not self.sim_mode

        if self.sim_mode:
            # SIM aktivieren – MQTT stoppen
            self.mqtt.stop()
            self._ui.sim_btn.text = "→ LIVE"
            self._ui.sim_btn.set_style({
                "background_color": 0xFF0D2A0D,
                "border_radius": 3, "font_size": 11, "color": CLR_GREEN,
            })
            self._set_status_text("SIM-Modus aktiv", CLR_GREEN)
            self.logger.log("→ SIM-Modus | Kein API-Polling", "ok")
            self.suction._set_target_kinematic(False)
        else:
            # LIVE aktivieren – MQTT + initiales Polling
            self.mqtt.start()
            t = asyncio.ensure_future(self.api.initial_poll(self.sim_mode))
            self._active_tasks.append(t)

            self._ui.sim_btn.text = "→ SIM"
            self._ui.sim_btn.set_style({
                "background_color": 0xFF2A0D0D,
                "border_radius": 3, "font_size": 11, "color": CLR_RED,
            })
            self._set_status_text("LIVE aktiv", CLR_RED)
            self.logger.log("→ LIVE-Modus | WebSocket-MQTT gestartet", "info")

    def _start_gesamtprozess(self):
        """Button: Gesamtprozess starten."""
        self.routines.start_gesamtprozess()

    # =================================================================
    # NODE-CLICK-HANDLER
    # =================================================================
    def _on_control_clicked(self, node_id):
        """Wird beim Klick auf einen Node-Button (Toggle/Trigger) ausgeführt."""
        if not self._is_running:
            return

        node = self.node_manager.find(node_id)
        if not node:
            return

        # Spezialfall: Sauggreifer
        if node_id == "Sauggreifer_EIN":
            if self.sim_mode:
                active = self.suction.toggle()
                self.node_manager.node_values[node_id] = active
                self.node_manager.set_display(node_id, active)
            else:
                current_val = self.node_manager.node_values.get(node_id, False)
                new_val = not current_val
                self.logger.log(f"Toggle {node_id} -> {new_val}", "info")
                t = asyncio.ensure_future(self.api.send_set(node_id, new_val, self.sim_mode))
                self._active_tasks.append(t)
            return

        mode = node.get("mode", "toggle")

        if mode == "impulse":
            self.logger.log(f"Manueller Step-Impuls: {node_id}", "info")
            self.execute_step_impulse(node_id, node)
            if not self.sim_mode:
                t = asyncio.ensure_future(self.api.send_impulse(node_id, self.sim_mode))
                self._active_tasks.append(t)

        elif mode == "velocity_impulse":
            if self.node_manager._velocity_running.get(node_id, False):
                self.logger.log(f"Velocity-Impuls läuft bereits: {node_id}", "info")
                return
            self.logger.log(f"Manueller Velocity-Impuls: {node_id}", "info")
            t = asyncio.ensure_future(self._execute_velocity_impulse(node_id, node))
            self._active_tasks.append(t)
            if not self.sim_mode:
                t2 = asyncio.ensure_future(self.api.send_impulse(node_id, self.sim_mode))
                self._active_tasks.append(t2)

        else:
            current_val = self.node_manager.node_values.get(node_id, False)
            new_val = not current_val
            self.logger.log(f"Toggle {node_id} -> {new_val}", "info")
            if self.sim_mode:
                self.node_manager.node_values[node_id] = new_val
                self.node_manager.set_display(node_id, new_val)
                self.node_manager.apply_usd_for_node(node_id, new_val)
                self.logger.log(f"SIM: {node_id} = {new_val}", "ok")
            else:
                t = asyncio.ensure_future(self.api.send_set(node_id, new_val, self.sim_mode))
                self._active_tasks.append(t)

        self._active_tasks = [t for t in self._active_tasks if not t.done()]

    # =================================================================
    # IMPULSE-HELFER
    # =================================================================
    def execute_step_impulse(self, node_id, node):
        """Step-Impuls: aktuelle Position + step_degrees."""
        import omni.usd
        step = float(node.get("step_degrees", 90.0))
        current_pos = self.node_manager._impulse_positions.get(node_id, 0.0)
        new_pos = current_pos + step
        self.node_manager._impulse_positions[node_id] = new_pos

        self.logger.log(f"Step: {node_id} {current_pos} -> {new_pos} (+{step})", "ok")

        stage = omni.usd.get_context().get_stage()
        if stage:
            self.node_manager.set_usd_attr(stage, node, new_pos)

        if node_id in self.node_manager.node_labels:
            self.node_manager.node_labels[node_id].text = f"  {new_pos:.0f} deg"
            self.node_manager.node_labels[node_id].set_style({
                "font_size": 12, "color": CLR_GREEN,
            })

    async def _execute_velocity_impulse(self, node_id, node):
        """Velocity-Impuls: setzt target_value für `impulse_duration` Sekunden."""
        import omni.usd

        if self.node_manager._velocity_running.get(node_id, False):
            return

        self.node_manager._velocity_running[node_id] = True
        velocity = float(node.get("target_value", 200.0))
        duration = float(node.get("impulse_duration", 1.8))

        stage = omni.usd.get_context().get_stage()
        if not stage:
            self.node_manager._velocity_running[node_id] = False
            return

        if node_id in self.node_manager.node_labels:
            lbl = self.node_manager.node_labels[node_id]
            lbl.text = "  SPINNING"
            lbl.set_style({"font_size": 12, "color": CLR_ORANGE})

        self.logger.log(f"Velocity START: {node_id} vel={velocity} dur={duration}s", "ok")
        self.node_manager.set_usd_attr(stage, node, velocity)

        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            self.node_manager.set_usd_attr(stage, node, 0.0)
            self.node_manager._velocity_running[node_id] = False
            raise

        self.node_manager.set_usd_attr(stage, node, 0.0)
        self.node_manager._velocity_running[node_id] = False
        self.logger.log(f"Velocity STOP: {node_id}", "ok")

        if node_id in self.node_manager.node_labels:
            lbl = self.node_manager.node_labels[node_id]
            lbl.text = "  READY"
            lbl.set_style({"font_size": 12, "color": CLR_TEXT_DIM})

    # =================================================================
    # STATUSBAR
    # =================================================================
    def _set_status_text(self, text, color=None):
        """Setzt den Text in der Statusleiste oben rechts."""
        if not self._ui.status_label:
            return
        self._ui.status_label.text = text
        if color is None:
            color = CLR_YELLOW
        self._ui.status_label.set_style({"font_size": 11, "color": color})
