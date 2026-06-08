"""
routines.py
===========
Hochrangige Choreografien:
- Gesamtprozess: Kompletter Maze-Runner-Ablauf
- BA_Start: Schwenkarm-Sequenz für Deckel-Pickup
"""

import asyncio


class Routines:
    """Vordefinierte automatisierte Abläufe."""

    def __init__(self, ext):
        self._ext = ext
        self._gesamt_running = False
        self._ba_running = False
        self._gesamt_task = None

    # ---------------------------------------------------------------
    # GESAMTPROZESS
    # ---------------------------------------------------------------
    def start_gesamtprozess(self):
        """Wird vom UI-Button aufgerufen."""
        if not self._ext.sim_mode:
            self._ext.logger.log("Simulation nicht aktiv – Gesamtprozess nicht gestartet", "error")
            return
        if self._gesamt_running:
            self._ext.logger.log("Gesamtprozess läuft bereits", "error")
            return
        self._ext.logger.log("▶ Gesamtprozess → Routine gestartet", "info")
        self._gesamt_task = asyncio.ensure_future(self._run_gesamtprozess())

    def cancel(self):
        """Bricht alle laufenden Routinen ab (z.B. bei Simulationsstop)."""
        if self._gesamt_task and not self._gesamt_task.done():
            self._gesamt_task.cancel()
            self._ext.logger.log("Gesamtprozess abgebrochen (Simulation gestoppt)", "info")
        self._gesamt_task = None
        self._gesamt_running = False
        self._ba_running = False

    async def _run_gesamtprozess(self):
        self._gesamt_running = True
        log = self._ext.logger.log
        log("═══ Routine GESAMTPROZESS GESTARTET ═══", "info")

        try:
            # Phase 1 – BM ausfahren
            await self._step("Phase 1 – BM ausfahren (TRUE)")
            self._set("BM_MoveFront_Set", True);    await asyncio.sleep(0.5)

            # Phase 2 – BM einfahren
            await self._step("Phase 2 – BM einfahren (FALSE)")
            self._set("BM_MoveFront_Set", False);   await asyncio.sleep(0.5)

            # Phase 3 – DS Step
            await self._step("Phase 3 – DS Step")
            self._impulse("Start_Stepper_Set");     await asyncio.sleep(1.0)

            # Phase 4 – KM Trigger
            await self._step("Phase 4 – KM Trigger")
            self._impulse("KM_Stepper_Start");      await asyncio.sleep(3.5)

            # Phase 5 – DS Step
            await self._step("Phase 5 – DS Step")
            self._impulse("Start_Stepper_Set");     await asyncio.sleep(1.0)

            # Phase 10d – Deckel dynamisch
            await self._step("Phase 10d – Deckel dynamisch machen")
            self._ext.suction.release_dynamic();    await asyncio.sleep(0.5)

            # Phase 6/7 – DM aus/ein
            await self._step("Phase 6 – DM ausfahren")
            self._set("DM_MoveFront_Set", True);    await asyncio.sleep(0.5)
            await self._step("Phase 7 – DM einfahren")
            self._set("DM_MoveFront_Set", False);   await asyncio.sleep(0.5)

            # Phase 8 – BA_Start
            await self._step("Phase 8 – BA_Start Routine", delay=0.0)
            await self._run_ba_start();             await asyncio.sleep(0.5)

            # Phase 9
            await self._step("Phase 9 – DS Step")
            self._impulse("Start_Stepper_Set");     await asyncio.sleep(0.5)

            # Phase 10 – Squeeze
            await self._step("Phase 10 – Squeeze EIN")
            self._set("Squeezer_Start_Set", True)

            await self._step("Phase 10b – Deckel nach Squeeze absetzen")
            self._ext.suction.press_down(z_down=0.002); await asyncio.sleep(0.5)

            await self._step("Phase 10c – Squeeze AUS")
            self._set("Squeezer_Start_Set", False); await asyncio.sleep(1.0)

            # Phase 11
            await self._step("Phase 11 – DS Step")
            self._impulse("Start_Stepper_Set");     await asyncio.sleep(0.5)

            log("═══ Routine GESAMTPROZESS ABGESCHLOSSEN ✅ ═══", "ok")

        except asyncio.CancelledError:
            log("[Routine] Gesamtprozess ABGEBROCHEN", "error"); raise
        except Exception as e:
            log(f"[Routine] Gesamtprozess FEHLER: {e}", "error")
        finally:
            self._gesamt_running = False

    # ---------------------------------------------------------------
    # BA_START
    # ---------------------------------------------------------------
    async def _run_ba_start(self):
        if self._ba_running:
            self._ext.logger.log("[Routine] BA_Start läuft bereits – ignoriert", "error")
            return

        self._ba_running = True
        log = self._ext.logger.log
        log("═══ Routine BA_Start GESTARTET ═══", "info")

        try:
            await self._step("Schwenkarm runter")
            self._set("Schwenkarm_Deckel_trans", True);   await asyncio.sleep(0.5)

            await self._step("Sauggreifer EIN")
            self._set("Sauggreifer_EIN", True);           await asyncio.sleep(0.5)

            await self._step("Schwenkarm hoch")
            self._set("Schwenkarm_Deckel_trans", False);  await asyncio.sleep(0.5)

            await self._step("Schwenkarm schwenken")
            self._set("Schwenkarm_Deckel_rot", True);     await asyncio.sleep(0.5)

            await self._step("Schwenkarm senken (Fix für Ablage)")
            self._set("Schwenkarm_Deckel_trans", True);   await asyncio.sleep(0.5)

            await self._step("Sauggreifer AUS")
            self._set("Sauggreifer_EIN", False);          await asyncio.sleep(0.5)

            await self._step("Schwenkarm hoch")
            self._set("Schwenkarm_Deckel_trans", False);  await asyncio.sleep(0.5)

            await self._step("Schwenkarm zurückschwenken")
            self._set("Schwenkarm_Deckel_rot", False);    await asyncio.sleep(0.5)

            log("═══ Routine BA_Start ABGESCHLOSSEN ✅ ═══", "ok")

        except asyncio.CancelledError:
            log("[Routine] BA_Start ABGEBROCHEN", "error"); raise
        except Exception as e:
            log(f"[Routine] BA_Start FEHLER: {e}", "error")
        finally:
            self._ba_running = False

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------
    async def _step(self, description: str, delay: float = 1.0):
        """Loggt einen Schritt und wartet."""
        self._ext.logger.log(f"[Auto] ▶ {description}", "info")
        await asyncio.sleep(delay)

    def _set(self, node_id: str, value: bool):
        """Setzt einen Toggle/Suction-Node direkt (analog _set_node aus Original)."""
        nm = self._ext.node_manager
        suction = self._ext.suction
        log = self._ext.logger.log

        node = nm.find(node_id)
        if not node:
            log(f"[Routine] Node nicht gefunden: {node_id}", "error")
            return

        # Sonderfall Sauggreifer
        if node_id == "Sauggreifer_EIN":
            current = suction.is_active
            if value and not current:
                suction.attach()
                nm.node_values[node_id] = True
                nm.set_display(node_id, True)
            elif not value and current:
                suction.detach()
                nm.node_values[node_id] = False
                nm.set_display(node_id, False)
            return

        nm.node_values[node_id] = value
        nm.set_display(node_id, value)
        nm.apply_usd_for_node(node_id, value)
        log(f"[Routine] {node_id} = {value}", "ok")

    def _impulse(self, node_id: str):
        """Triggert einen STEP-Impuls (analog _trigger_impulse aus Original)."""
        nm = self._ext.node_manager
        log = self._ext.logger.log

        node = nm.find(node_id)
        if not node:
            log(f"[Routine] Impulse-Node nicht gefunden: {node_id}", "error")
            return
        if node.get("mode") != "impulse":
            log(f"[Routine] Node {node_id} ist kein impulse-Mode", "error")
            return

        self._ext.execute_step_impulse(node_id, node)
        log(f"[Routine] Impulse ausgelöst: {node_id}", "ok")
