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
from .suction import SuctionGripper


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

        self._build_ui()
        self.load_nodes_from_json()
        self._subscribe_timeline()
        self._suction = SuctionGripper(self)

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
                        ui.Spacer(width=14)

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

        else:
            self._poll_task = asyncio.ensure_future(self.auto_update_loop())
            self._active_tasks.append(self._poll_task)

            self._sim_btn.text = "→ SIM"
            self._sim_btn.set_style({
                "background_color": 0xFF2A0D0D,
                "border_radius": 3,
                "font_size": 11,
                "color": CLR_RED,
            })
            self._set_status_text("LIVE aktiv", CLR_RED)
            self._log("→ LIVE-Modus | API-Polling gestartet", "info")

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
                self._log(f"Velocity-Impuls laeuft bereits: {node_id}", "info")
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
    # POLLING
    # =================================================================
    async def auto_update_loop(self):
        if self._sim_mode:
            return

        session = None
        try:
            session = aiohttp.ClientSession()
            self._set_status_text("Verbunden", CLR_GREEN)
            while self._is_running and not self._sim_mode:
                try:
                    self._suction.update_hold_position()
                    self._suction.wait_and_press_if_ready(
                        press_prim_path="/World/Production_Line/Presse/PrismaticJoint",
                        target_attr="drive:linear:physics:targetPosition",
                        reached_value=-0.035,
                        z_down=0.002,
                        tolerance=1e-4
                    )

                    await self._poll_all_nodes(session)
                    self._poll_count += 1
                    if hasattr(self, "_poll_label"):
                        self._poll_label.text = f"Polls: {self._poll_count}"
                    await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    raise
                except aiohttp.ClientError:
                    self._set_status_text("Getrennt", CLR_RED)
                    if self._is_running and not self._sim_mode:
                        self._log("HTTP Fehler, Retry 2s", "error")
                        await asyncio.sleep(2)
                except Exception as e:
                    if self._is_running:
                        self._log(f"Loop Fehler: {e}", "error")
                        await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            if session and not session.closed:
                await session.close()
                await asyncio.sleep(0.05)

    async def _poll_all_nodes(self, session):
        if not self._is_running or self._sim_mode:
            return

        headers = {"X-API-KEY": self.api_key, "accept": "application/json"}

        for node in self.nodes:
            if not self._is_running or self._sim_mode:
                break

            node_id = node.get("node_id")
            if not self.node_labels.get(node_id):
                continue

            mode = node.get("mode", "toggle")
            params = {"NodeName": node_id, "useHistoricalData": "false", "user": "admin", "apiKey": self.api_key}

            try:
                async with session.get(self.api_url_get, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=3), ssl=False) as resp:
                    if resp.status != 200:
                        continue

                    data = await resp.text()
                    val_str = data.replace('"', "").strip().lower()
                    val = val_str in ("true", "1")

                    old_val = self.node_values.get(node_id)
                    self.node_values[node_id] = val

                    if mode == "impulse":
                        self._handle_step_impulse_poll(node_id, node, old_val, val)
                    elif mode == "velocity_impulse":
                        self._handle_velocity_impulse_poll(node_id, node, old_val, val)
                    else:
                        if old_val != val:
                            self._set_node_display(node_id, val)
                            self._apply_usd_for_node(node_id, val)
                            self._log(f"{node_id} = {val}", "ok")

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                if self._is_running:
                    self._log(f"Poll Fehler ({node_id}): {e}", "error")

    # =================================================================
    # IMPULSE POLL HANDLERS
    # =================================================================
    def _handle_step_impulse_poll(self, node_id, node, old_val, new_val):
        was_armed = self._impulse_armed.get(node_id, True)

        if new_val and was_armed:
            self._impulse_armed[node_id] = False
            self._execute_step_impulse(node_id, node)
            self._log(f"Step-Impuls erkannt (API): {node_id}", "ok")
        elif not new_val:
            self._impulse_armed[node_id] = True
            if node_id in self.node_labels:
                pos = self._impulse_positions.get(node_id, 0.0)
                self.node_labels[node_id].text = f"  {pos:.0f} deg"
                self.node_labels[node_id].set_style({"font_size": 12, "color": CLR_TEXT_DIM})

    def _handle_velocity_impulse_poll(self, node_id, node, old_val, new_val):
        was_armed = self._impulse_armed.get(node_id, True)
        is_running = self._velocity_running.get(node_id, False)

        if new_val and was_armed and not is_running:
            self._impulse_armed[node_id] = False
            self._log(f"Velocity-Impuls erkannt (API): {node_id}", "ok")
            t = asyncio.ensure_future(self._execute_velocity_impulse(node_id, node))
            self._active_tasks.append(t)
            self._active_tasks = [t for t in self._active_tasks if not t.done()]
        elif not new_val:
            self._impulse_armed[node_id] = True
            if not is_running and node_id in self.node_labels:
                self.node_labels[node_id].text = "  READY"
                self.node_labels[node_id].set_style({"font_size": 12, "color": CLR_TEXT_DIM})

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