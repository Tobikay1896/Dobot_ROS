"""
timeline_handler.py
===================
Reagiert auf PLAY/STOP der Omniverse-Timeline:
- Bei PLAY  → Startet die SIM-Update-Schleife (Sauggreifer halten/pressen).
- Bei STOP  → Stoppt die Schleife, setzt alle Werte auf 0.
"""

import asyncio
import omni.timeline
import omni.usd


class TimelineHandler:
    """Verbindet die Omniverse-Timeline mit der Simulationslogik."""

    def __init__(self, suction, node_manager, logger):
        self._suction = suction
        self._nm = node_manager
        self._logger = logger
        self._timeline_sub = None
        self._sim_update_task = None
        self._is_running = True
        self._active_tasks = []
        self._routines = None

    def set_routines(self, routines):
        """Verbindet die Routines-Instanz, damit sie bei STOP abgebrochen werden."""
        self._routines = routines

    # ---------------------------------------------------------------
    def subscribe(self):
        timeline = omni.timeline.get_timeline_interface()
        self._timeline_sub = timeline.get_timeline_event_stream() \
            .create_subscription_to_pop(self._on_event)

    def unsubscribe(self):
        self._timeline_sub = None
        if self._sim_update_task and not self._sim_update_task.done():
            self._sim_update_task.cancel()
        self._sim_update_task = None

    # ---------------------------------------------------------------
    def _on_event(self, event):
        if event.type == int(omni.timeline.TimelineEventType.PLAY):
            # Sauggreifer-Reset: alle Joints löschen, Deckel auf Startposition, dynamisch
            self._suction.reset()
            # Vorhandenen Loop ggf. abbrechen
            if self._sim_update_task and not self._sim_update_task.done():
                self._sim_update_task.cancel()
            self._sim_update_task = asyncio.ensure_future(self._sim_loop())
            self._active_tasks.append(self._sim_update_task)

        elif event.type == int(omni.timeline.TimelineEventType.STOP):
            self._on_stop()

    # ---------------------------------------------------------------
    def _on_stop(self):
        self._logger.log("Simulation gestoppt", "info")
        if self._routines:
            self._routines.cancel()
        if self._sim_update_task and not self._sim_update_task.done():
            self._sim_update_task.cancel()
            self._sim_update_task = None
        self._suction.reset()
        self._reset_all_to_zero()

    # ---------------------------------------------------------------
    async def _sim_loop(self):
        """SIM-Loop: hält Deckel, prüft Presse."""
        try:
            while self._is_running:
                try:
                    self._suction.update_hold_position()
                    self._suction.wait_and_press_if_ready(
                        press_prim_path="/World/Production_Line/Presse/PrismaticJoint",
                        target_attr="drive:linear:physics:targetPosition",
                        reached_value=-0.035,
                        z_down=0.002,
                        tolerance=1e-4,
                    )
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._logger.log(f"SIM Update Fehler: {e}", "error")
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    # ---------------------------------------------------------------
    def _reset_all_to_zero(self):
        """Setzt alle Werte (USD + Labels) auf 0 nach Simulationsstop."""
        from .constants import CLR_RED, CLR_TEXT_DIM
        self._logger.log("Simulation gestoppt - setze alle Werte auf 0", "info")
        self._suction.reset()

        stage = omni.usd.get_context().get_stage()
        if not stage:
            return

        # Interne Zustände zurücksetzen
        for nid in self._nm._impulse_positions:
            self._nm._impulse_positions[nid] = 0.0
        for nid in self._nm._impulse_armed:
            self._nm._impulse_armed[nid] = True
        for nid in self._nm._velocity_running:
            self._nm._velocity_running[nid] = False

        # USD + Labels zurücksetzen
        for node in self._nm.nodes:
            node_id = node.get("node_id", "")
            mode = node.get("mode", "toggle")
            self._nm.set_usd_attr(stage, node, 0.0)
            self._nm.node_values[node_id] = False

            if node_id in self._nm.node_labels:
                lbl = self._nm.node_labels[node_id]
                if mode == "impulse":
                    lbl.text = "  0 deg"
                    lbl.set_style({"font_size": 12, "color": CLR_TEXT_DIM})
                elif mode == "velocity_impulse":
                    lbl.text = "  READY"
                    lbl.set_style({"font_size": 12, "color": CLR_TEXT_DIM})
                else:
                    lbl.text = "  FALSE"
                    lbl.set_style({"font_size": 12, "color": CLR_RED})

        self._logger.log("Alle Werte auf 0 gesetzt", "ok")
