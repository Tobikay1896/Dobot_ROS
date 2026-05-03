# routines.py
import asyncio

# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

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

# ─────────────────────────────────────────────────────────────────────────────
# Routine: Deckel-Pickup (BA_Start)
# ─────────────────────────────────────────────────────────────────────────────

async def routine_ba_start(ext):
    if getattr(ext, "_routine_ba_running", False):
        ext._log("[Routine] BA_Start läuft bereits – ignoriert", "error")
        return

    ext._routine_ba_running = True
    ext._log("═══ Routine BA_Start GESTARTET ═══", "info")

    try:
        # Phase 1: Schwenkarm runter
        await _step(ext, "Phase 1 – Schwenkarm runter (Schwenkarm_Deckel_trans → TRUE)")
        _set_node(ext, "Schwenkarm_Deckel_trans", True)
        await asyncio.sleep(0.5)

        # Phase 2: Sauggreifer EIN
        await _step(ext, "Phase 2 – Sauggreifer EIN (Sauggreifer_EIN → TRUE)")
        _set_node(ext, "Sauggreifer_EIN", True)
        await asyncio.sleep(0.5)

        # Phase 3: Schwenkarm hoch
        await _step(ext, "Phase 3 – Schwenkarm hoch (Schwenkarm_Deckel_trans → FALSE)")
        _set_node(ext, "Schwenkarm_Deckel_trans", False)
        await asyncio.sleep(0.5)

        # Phase 4: Schwenkarm schwenken
        await _step(ext, "Phase 4 – Schwenkarm schwenken (Schwenkarm_Deckel_rot → TRUE)")
        _set_node(ext, "Schwenkarm_Deckel_rot", True)
        await asyncio.sleep(0.5)
        
        # Schwenkarm wieder absenken (Fix für Deckel-Ablage)
        await _step(ext, "Phase 4b – Schwenkarm senken (Schwenkarm_Deckel_trans → TRUE)")
        _set_node(ext, "Schwenkarm_Deckel_trans", True)
        await asyncio.sleep(0.5)

        # Phase 5: Sauggreifer AUS
        await _step(ext, "Phase 5 – Sauggreifer AUS (Sauggreifer_EIN → FALSE)")
        _set_node(ext, "Sauggreifer_EIN", False)
        await asyncio.sleep(0.5)
        
        # Phase 6: Schwenkarm hoch
        await _step(ext, "Phase 3 – Schwenkarm hoch (Schwenkarm_Deckel_trans → FALSE)")
        _set_node(ext, "Schwenkarm_Deckel_trans", False)
        await asyncio.sleep(0.5)
        
        # Phase 4: Schwenkarm schwenken
        await _step(ext, "Phase 4 – Schwenkarm schwenken (Schwenkarm_Deckel_rot → False)")
        _set_node(ext, "Schwenkarm_Deckel_rot", False)
        await asyncio.sleep(0.5)

        ext._log("═══ Routine BA_Start ABGESCHLOSSEN ✅ ═══", "ok")

    except asyncio.CancelledError:
        ext._log("[Routine] BA_Start ABGEBROCHEN", "error")
        raise
    except Exception as e:
        ext._log(f"[Routine] BA_Start FEHLER: {e}", "error")
    finally:
        ext._routine_ba_running = False

# ─────────────────────────────────────────────────────────────────────────────
# Routine: Gesamtprozess / Automatik
# ─────────────────────────────────────────────────────────────────────────────

async def routine_gesamtprozess(ext):
    if getattr(ext, "_routine_gesamt_running", False):
        ext._log("[Routine] Gesamtprozess läuft bereits – ignoriert", "error")
        return

    ext._routine_gesamt_running = True
    ext._log("═══ Routine GESAMTPROZESS GESTARTET ═══", "info")

    try:
        # Phase 1: BM ausfahren
        await _step(ext, "Phase 1 – BM ausfahren (BM_MoveFront_Set → TRUE)")
        _set_node(ext, "BM_MoveFront_Set", True)
        await asyncio.sleep(0.5)

        # Phase 2: BM einfahren
        await _step(ext, "Phase 2 – BM einfahren (BM_MoveFront_Set → FALSE)")
        _set_node(ext, "BM_MoveFront_Set", False)
        await asyncio.sleep(0.5)

        # Phase 3: DS Step (Nur 1x statt 3x)
        await _step(ext, "Phase 3 – DS Step (Start_Stepper_Set)")
        _trigger_impulse(ext, "Start_Stepper_Set")
        await asyncio.sleep(1.0)

        # Phase 4: KM Trigger
        await _step(ext, "Phase 4 – KM Trigger (KM_Stepper_Start)")
        _trigger_impulse(ext, "KM_Stepper_Start")
        await asyncio.sleep(3.5)

        # Phase 5: DS Step (Nur 1x statt 3x)
        await _step(ext, "Phase 5 – DS Step (Start_Stepper_Set)")
        _trigger_impulse(ext, "Start_Stepper_Set")
        await asyncio.sleep(1.0)

        # Phase 6: DM ausfahren
        await _step(ext, "Phase 6 – DM ausfahren (DM_MoveFront_Set → TRUE)")
        _set_node(ext, "DM_MoveFront_Set", True)
        await asyncio.sleep(0.5)

        # Phase 7: DM einfahren
        await _step(ext, "Phase 7 – DM einfahren (DM_MoveFront_Set → FALSE)")
        _set_node(ext, "DM_MoveFront_Set", False)
        await asyncio.sleep(0.5)

        # Phase 8: BA_Start Routine
        await _step(ext, "Phase 8 – BA_Start Routine wird aufgerufen …", delay=0.0)
        await routine_ba_start(ext)
        await asyncio.sleep(0.5)

        # Phase 9: DS Step (Nur 1x statt 3x)
        await _step(ext, "Phase 9 – DS Step (Start_Stepper_Set)")
        _trigger_impulse(ext, "Start_Stepper_Set")
        await asyncio.sleep(0.5)

        # Phase 10: Squeeze
        await _step(ext, "Phase 10 – Squeeze EIN (Squeezer_start_set → TRUE)")
        _set_node(ext, "Squeezer_start_set", True)
        await asyncio.sleep(1.0)

        await _step(ext, "Phase 10 – Squeeze AUS (Squeezer_start_set → FALSE)")
        _set_node(ext, "Squeezer_start_set", False)
        await asyncio.sleep(1.0)

        # Phase 11: DS Step (Nur 1x statt 5x)
        await _step(ext, "Phase 11 – DS Step (Start_Stepper_Set)")
        _trigger_impulse(ext, "Start_Stepper_Set")
        await asyncio.sleep(0.5)

        ext._log("═══ Routine GESAMTPROZESS ABGESCHLOSSEN ✅ ═══", "ok")

    except asyncio.CancelledError:
        ext._log("[Routine] Gesamtprozess ABGEBROCHEN", "error")
        raise
    except Exception as e:
        ext._log(f"[Routine] Gesamtprozess FEHLER: {e}", "error")
    finally:
        ext._routine_gesamt_running = False