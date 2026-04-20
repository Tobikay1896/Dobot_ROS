import omni.ui as ui
from datetime import datetime
from .constants import MAX_LOG_LINES, CLR_TEXT_FAINT, CLR_ACCENT, CLR_RED, CLR_GREEN

def log(ext, message, level="log"):
    ts = datetime.now().strftime("%H:%M:%S")
    ext._log_lines.append((ts, message, level))
    if len(ext._log_lines) > MAX_LOG_LINES:
        ext._log_lines = ext._log_lines[-MAX_LOG_LINES:]
    rebuild_log(ext)
    print(f"[MazeRunner] [{ts}] {message}")

def rebuild_log(ext):
    if not hasattr(ext, "_log_container"):
        return
    ext._log_container.clear()
    color_map = {
        "log": CLR_TEXT_FAINT,
        "info": CLR_ACCENT,
        "error": CLR_RED,
        "ok": CLR_GREEN,
    }
    with ext._log_container:
        for ts, msg, level in ext._log_lines:
            clr = color_map.get(level, CLR_TEXT_FAINT)
            ui.Label(f"  {ts}   {msg}", style={"font_size": 10, "color": clr}, height=14)

def clear_log(ext):
    ext._log_lines = []
    rebuild_log(ext)

def set_node_display(ext, node_id, val):
    label = ext.node_labels.get(node_id)
    if not label:
        return
    if val:
        label.text = "  TRUE"
        label.set_style({"font_size": 12, "color": CLR_GREEN})
    else:
        label.text = "  FALSE"
        label.set_style({"font_size": 12, "color": CLR_RED})

def set_status_text(ext, text, color):
    if hasattr(ext, "_status_label"):
        ext._status_label.text = text
        ext._status_label.set_style({"font_size": 11, "color": color})