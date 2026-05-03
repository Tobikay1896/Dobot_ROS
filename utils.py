# omni/mazerunner/utils.py
import asyncio
from datetime import datetime
from pxr import UsdPhysics, Sdf

# -------------------------------------------------------------------------
# Logging (zentral, damit UI und Console gleiches Format nutzen)
# -------------------------------------------------------------------------
def make_timestamp():
    return datetime.now().strftime("%H:%M:%S")

def add_log(log_container, log_lines, message, level, max_lines):
    """Append a log entry, trim list and rebuild UI."""
    ts = make_timestamp()
    log_lines.append((ts, message, level))
    if len(log_lines) > max_lines:
        log_lines[:] = log_lines[-max_lines:]

    # UI‑Rebuild (nur wenn das UI‑Objekt bereits existiert)
    if log_container is not None:
        log_container.clear()
        color_map = {
            "log":   0xFF405070,
            "info":  0xFF4A9EFF,
            "error": 0xFFEF6B6B,
            "ok":    0xFF4ADE80,
        }
        from .constants import CLR_TEXT_FAINT  # lazy import to avoid circular deps
        with log_container:
            for ts, msg, lvl in log_lines:
                clr = color_map.get(lvl, CLR_TEXT_FAINT)
                ui.Label(f"  {ts}   {msg}",
                         style={"font_size": 10, "color": clr},
                         height=14)

# -------------------------------------------------------------------------
# Cancel‑Helper (wird in mehreren Stellen gebraucht)
# -------------------------------------------------------------------------
async def _cancel_and_await(tasks):
    """Cancel a list of asyncio‑Tasks and wait for their termination."""
    for t in tasks:
        if not t.done():
            t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

# -------------------------------------------------------------------------
# USD‑Hilfsfunktion – Setzt ein Attribut und schreibt den Default‑Wert
# -------------------------------------------------------------------------
def set_usd_attribute(stage, prim_path, attr_name, value):
    """Set attribute + default value (falls ein edit‑target existiert)."""
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return False

    attr = prim.GetAttribute(attr_name)
    # Fallback: evtl. ein ":"‑Problem (z. B. "drive:angular:physics:targetPosition")
    if not attr or not attr.IsValid():
        alt = attr_name.replace(":physics:", ":")
        attr = prim.GetAttribute(alt)
        if attr and attr.IsValid():
            attr_name = alt

    if not attr or not attr.IsValid():
        return False

    try:
        attr.Set(value)
        # Default‑Wert im Layer setzen (damit das Attribut nach Reset wieder 0 hat)
        layer = stage.GetEditTarget().GetLayer()
        prim_spec = layer.GetPrimAtPath(prim_path)
        if prim_spec:
            sdf_attr = prim_spec.attributes.get(attr_name)
            if sdf_attr:
                sdf_attr.default = value
        return True
    except Exception as e:
        print(f"[MazeRunner][USD] {prim_path} – {e}")
        return False