import omni.ui as ui
import omni.usd
import asyncio
import json

from .constants import *
from .backend import set_usd_attr


def build_ui(ext):
    ext._window = ui.Window("Maze Runner", width=880, height=520)
    ext._window.deferred_dock_in("Property")

    with ext._window.frame:
        with ui.VStack(spacing=0):

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
                        ext._status_label = ui.Label("SIM-Modus aktiv", style={"font_size": 11, "color": CLR_GREEN}, height=16, alignment=ui.Alignment.RIGHT)
                        ext._poll_label = ui.Label("Polls: 0", style={"font_size": 10, "color": CLR_TEXT_FAINT}, height=14, alignment=ui.Alignment.RIGHT)
                    ui.Spacer(width=14)

            ui.Line(style={"color": CLR_BORDER}, height=1)

            with ui.ZStack(height=34):
                ui.Rectangle(style={"background_color": CLR_BG_MID})
                with ui.HStack(spacing=6):
                    ui.Spacer(width=10)

                    btn_ref = ui.Button("Refresh JSON", width=120, height=24)
                    btn_ref.set_style({"background_color": 0xFF1A2E48, "border_radius": 3, "font_size": 11, "color": CLR_TEXT})
                    btn_ref.set_clicked_fn(ext.load_nodes_from_json)

                    btn_rst = ui.Button("Restart Extension", width=130, height=24)
                    btn_rst.set_style({"background_color": 0xFF2A1520, "border_radius": 3, "font_size": 11, "color": CLR_RED})
                    btn_rst.set_clicked_fn(ext._restart_extension)

                    ext._sim_btn = ui.Button("→ LIVE", width=90, height=24)
                    ext._sim_btn.set_style({"background_color": 0xFF0D2A0D, "border_radius": 3, "font_size": 11, "color": CLR_GREEN})
                    ext._sim_btn.set_clicked_fn(ext._toggle_sim_mode)

                    ui.Spacer()
                    ext._node_count_label = ui.Label("", style={"font_size": 10, "color": CLR_TEXT_FAINT}, width=70, alignment=ui.Alignment.RIGHT)
                    ui.Spacer(width=14)

            ui.Line(style={"color": CLR_BORDER}, height=1)

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

            with ui.ScrollingFrame(style={"background_color": CLR_BG_MID}):
                ext._list_container = ui.VStack(spacing=0)

            ui.Line(style={"color": CLR_BORDER}, height=1)

            with ui.ZStack(height=22):
                ui.Rectangle(style={"background_color": CLR_BG_DARK})
                with ui.HStack():
                    ui.Spacer(width=14)
                    ui.Label("Log", style={"font_size": 10, "color": CLR_TEXT_FAINT}, width=40)
                    ui.Spacer()
                    btn_clr = ui.Button("Clear", width=48, height=16)
                    btn_clr.set_style({"background_color": 0xFF152030, "border_radius": 2, "font_size": 9, "color": CLR_TEXT_FAINT})
                    btn_clr.set_clicked_fn(ext._clear_log)
                    ui.Spacer(width=14)

            with ui.ScrollingFrame(height=110, style={"background_color": 0xFF080C14}):
                ext._log_container = ui.VStack(spacing=0)


def load_nodes_from_json(ext):
    if not hasattr(ext, "_list_container"):
        return
    ext._list_container.clear()
    ext.node_labels.clear()
    ext._impulse_positions.clear()
    ext._impulse_armed.clear()
    ext._velocity_running.clear()

    try:
        with open(ext.json_path, "r", encoding="utf-8") as f:
            ext.nodes = json.load(f).get("nodes", [])
    except Exception as e:
        ext._log(f"JSON Fehler: {e}", "error")
        return

    ext._log(f"JSON geladen: {len(ext.nodes)} Nodes", "info")
    if hasattr(ext, "_node_count_label"):
        ext._node_count_label.text = f"{len(ext.nodes)} Nodes"

    for node in ext.nodes:
        node_id = node.get("node_id", "")
        mode = node.get("mode", "toggle")
        if mode == "impulse":
            ext._impulse_positions[node_id] = 0.0
            ext._impulse_armed[node_id] = True
        elif mode == "velocity_impulse":
            ext._velocity_running[node_id] = False
            ext._impulse_armed[node_id] = True

    mode_labels = {
        "toggle": ("TOGGLE", CLR_TEXT_DIM),
        "impulse": ("STEP", CLR_ACCENT),
        "velocity_impulse": ("VEL-IMP", CLR_ORANGE),
    }

    with ext._list_container:
        for idx, node in enumerate(ext.nodes):
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
                    ext.node_labels[node_id] = lbl
                    ui.Spacer()

                    btn_text = "Trigger" if mode in ("impulse", "velocity_impulse") else "Toggle"
                    btn = ui.Button(btn_text, width=70, height=22)
                    btn.set_style({"background_color": 0xFF1A2840, "border_radius": 3, "font_size": 11, "color": CLR_TEXT_DIM})
                    btn.set_clicked_fn(lambda n=node_id: ext.on_control_clicked(n))
                    ui.Spacer(width=14)


def find_node(ext, node_id):
    for node in ext.nodes:
        if node.get("node_id") == node_id:
            return node
    return None


def on_control_clicked(ext, node_id):
    if not ext._is_running:
        return

    node = find_node(ext, node_id)
    if not node:
        return

    mode = node.get("mode", "toggle")

    if mode == "impulse":
        ext._log(f"Manueller Step-Impuls: {node_id}", "info")
        execute_step_impulse(ext, node_id, node)
        if not ext._sim_mode:
            t = asyncio.ensure_future(ext._send_impulse_to_api(node_id))
            ext._active_tasks.append(t)

    elif mode == "velocity_impulse":
        if ext._velocity_running.get(node_id, False):
            ext._log(f"Velocity-Impuls laeuft bereits: {node_id}", "info")
            return
        ext._log(f"Manueller Velocity-Impuls: {node_id}", "info")
        t = asyncio.ensure_future(execute_velocity_impulse(ext, node_id, node))
        ext._active_tasks.append(t)
        if not ext._sim_mode:
            t2 = asyncio.ensure_future(ext._send_impulse_to_api(node_id))
            ext._active_tasks.append(t2)

    else:
        current_val = ext.node_values.get(node_id, False)
        new_val = not current_val
        ext._log(f"Toggle {node_id} -> {new_val}", "info")

        if ext._sim_mode:
            ext.node_values[node_id] = new_val
            ext._set_node_display(node_id, new_val)
            ext._apply_usd_for_node(node_id, new_val)
            ext._log(f"SIM: {node_id} = {new_val}", "ok")
        else:
            if node_id in ext.node_labels:
                ext.node_labels[node_id].text = "  sending..."
                ext.node_labels[node_id].set_style({"font_size": 12, "color": CLR_YELLOW})
            t = asyncio.ensure_future(ext.send_api_update(node_id, new_val))
            ext._active_tasks.append(t)

    ext._active_tasks = [t for t in ext._active_tasks if not t.done()]


def execute_step_impulse(ext, node_id, node):
    step = float(node.get("step_degrees", 90.0))
    current_pos = ext._impulse_positions.get(node_id, 0.0)
    new_pos = current_pos + step
    ext._impulse_positions[node_id] = new_pos

    ext._log(f"Step: {node_id} {current_pos} -> {new_pos} (+{step})", "ok")

    stage = omni.usd.get_context().get_stage()
    if stage:
        set_usd_attr(ext, stage, node, new_pos)

    if node_id in ext.node_labels:
        ext.node_labels[node_id].text = f"  {new_pos:.0f} deg"
        ext.node_labels[node_id].set_style({"font_size": 12, "color": CLR_GREEN})


async def execute_velocity_impulse(ext, node_id, node):
    if ext._velocity_running.get(node_id, False):
        return

    ext._velocity_running[node_id] = True
    velocity = float(node.get("target_value", 200.0))
    duration = float(node.get("impulse_duration", 1.8))

    stage = omni.usd.get_context().get_stage()
    if not stage:
        ext._velocity_running[node_id] = False
        return

    if node_id in ext.node_labels:
        ext.node_labels[node_id].text = "  SPINNING"
        ext.node_labels[node_id].set_style({"font_size": 12, "color": CLR_ORANGE})

    ext._log(f"Velocity START: {node_id} vel={velocity} dur={duration}s", "ok")
    set_usd_attr(ext, stage, node, velocity)

    try:
        await asyncio.sleep(duration)
    except asyncio.CancelledError:
        set_usd_attr(ext, stage, node, 0.0)
        ext._velocity_running[node_id] = False
        raise

    set_usd_attr(ext, stage, node, 0.0)
    ext._velocity_running[node_id] = False

    ext._log(f"Velocity STOP: {node_id}", "ok")

    if node_id in ext.node_labels:
        ext.node_labels[node_id].text = "  READY"
        ext.node_labels[node_id].set_style({"font_size": 12, "color": CLR_TEXT_DIM})


def handle_step_impulse_poll(ext, node_id, node, old_val, new_val):
    was_armed = ext._impulse_armed.get(node_id, True)

    if new_val and was_armed:
        ext._impulse_armed[node_id] = False
        execute_step_impulse(ext, node_id, node)
        ext._log(f"Step-Impuls erkannt (API): {node_id}", "ok")
    elif not new_val:
        ext._impulse_armed[node_id] = True
        if node_id in ext.node_labels:
            pos = ext._impulse_positions.get(node_id, 0.0)
            ext.node_labels[node_id].text = f"  {pos:.0f} deg"
            ext.node_labels[node_id].set_style({"font_size": 12, "color": CLR_TEXT_DIM})


def handle_velocity_impulse_poll(ext, node_id, node, old_val, new_val):
    was_armed = ext._impulse_armed.get(node_id, True)
    is_running = ext._velocity_running.get(node_id, False)

    if new_val and was_armed and not is_running:
        ext._impulse_armed[node_id] = False
        ext._log(f"Velocity-Impuls erkannt (API): {node_id}", "ok")
        t = asyncio.ensure_future(execute_velocity_impulse(ext, node_id, node))
        ext._active_tasks.append(t)
        ext._active_tasks = [t for t in ext._active_tasks if not t.done()]
    elif not new_val:
        ext._impulse_armed[node_id] = True
        if not is_running and node_id in ext.node_labels:
            ext.node_labels[node_id].text = "  READY"
            ext.node_labels[node_id].set_style({"font_size": 12, "color": CLR_TEXT_DIM})