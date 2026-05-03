# omni/mazerunner/ui_builder.py
import omni.ui as ui
from .constants import (
    CLR_BG_DARK, CLR_BG_MID, CLR_BG_ROW_A, CLR_BG_ROW_B,
    CLR_BG_HEADER, CLR_ACCENT, CLR_GREEN, CLR_RED,
    CLR_TEXT, CLR_TEXT_DIM, CLR_TEXT_FAINT, CLR_BORDER,
)

class UIBuilder:
    """Kapselt den kompletten UI‑Aufbau.  Die Klasse hält nur Referenzen
    auf Widgets, die von MyExtension benötigt werden."""
    def __init__(self, ext):
        self.ext = ext          # Referenz auf MyExtension (für Callbacks)
        self._build()

    def _build(self):
        ext = self.ext
        # -----------------------------------------------------------------
        # Hauptfenster
        # -----------------------------------------------------------------
        ext._window = ui.Window("Maze Runner", width=960, height=520)
        ext._window.deferred_dock_in("Property")

        with ext._window.frame:
            with ui.VStack(spacing=0):
                self._build_header()
                self._build_toolbar()
                self._build_table_header()
                self._build_node_list()
                self._build_log_section()

    # -----------------------------------------------------------------
    # Einzelne UI‑Blöcke (je ein kleiner Helper)
    # -----------------------------------------------------------------
    def _build_header(self):
        ext = self.ext
        with ui.ZStack(height=50):
            ui.Rectangle(style={"background_color": CLR_BG_DARK})
            with ui.HStack():
                ui.Spacer(width=14)
                with ui.VStack(spacing=1):
                    ui.Spacer(height=9)
                    ui.Label("MAZE RUNNER",
                             style={"font_size": 17, "color": CLR_TEXT},
                             height=22)
                    ui.Label("MQTT Control Center",
                             style={"font_size": 11, "color": CLR_TEXT_DIM},
                             height=14)
                ui.Spacer()
                with ui.VStack(width=180, spacing=1):
                    ui.Spacer(height=10)
                    ext._status_label = ui.Label(
                        "SIM-Modus aktiv",
                        style={"font_size": 11, "color": CLR_GREEN},
                        height=16,
                        alignment=ui.Alignment.RIGHT,
                    )
                    ext._poll_label = ui.Label(
                        "MQTT: --",
                        style={"font_size": 10, "color": CLR_TEXT_FAINT},
                        height=14,
                        alignment=ui.Alignment.RIGHT,
                    )
                ui.Spacer(width=14)

        ui.Line(style={"color": CLR_BORDER}, height=1)

    def _build_toolbar(self):
        ext = self.ext
        with ui.ZStack(height=34):
            ui.Rectangle(style={"background_color": CLR_BG_MID})
            with ui.HStack(spacing=6):
                ui.Spacer(width=10)

                btn_ref = ui.Button("Refresh JSON", width=120, height=24)
                btn_ref.set_style({
                    "background_color": 0xFF1A2E48,
                    "border_radius": 3,
                    "font_size": 11,
                    "color": CLR_TEXT,
                })
                btn_ref.set_clicked_fn(ext.load_nodes_from_json)

                btn_rst = ui.Button("Restart Extension", width=130, height=24)
                btn_rst.set_style({
                    "background_color": 0xFF2A1520,
                    "border_radius": 3,
                    "font_size": 11,
                    "color": CLR_RED,
                })
                btn_rst.set_clicked_fn(ext._restart_extension)

                ext._sim_btn = ui.Button("→ LIVE", width=90, height=24)
                ext._sim_btn.set_style({
                    "background_color": 0xFF0D2A0D,
                    "border_radius": 3,
                    "font_size": 11,
                    "color": CLR_GREEN,
                })
                ext._sim_btn.set_clicked_fn(ext._toggle_sim_mode)

                ext._btn_gesamt = ui.Button("▶ Gesamtprozess", width=140, height=24)
                ext._btn_gesamt.set_style({
                    "background_color": 0xFF1A1A40,
                    "border_radius": 3,
                    "font_size": 11,
                    "color": CLR_ACCENT,
                })
                ext._btn_gesamt.set_clicked_fn(ext._start_gesamtprozess)
                ext._btn_gesamt.visible = False

                ui.Spacer()
                ext._node_count_label = ui.Label(
                    "",
                    style={"font_size": 10, "color": CLR_TEXT_FAINT},
                    width=70,
                    alignment=ui.Alignment.RIGHT,
                )
                ui.Spacer(width=14)

        ui.Line(style={"color": CLR_BORDER}, height=1)

    def _build_table_header(self):
        with ui.ZStack(height=22):
            ui.Rectangle(style={"background_color": CLR_BG_HEADER})
            with ui.HStack():
                ui.Spacer(width=14)
                ui.Label("Node",   style={"font_size": 10, "color": CLR_TEXT_FAINT}, width=180)
                ui.Label("Mode",   style={"font_size": 10, "color": CLR_TEXT_FAINT}, width=90)
                ui.Label("Status", style={"font_size": 10, "color": CLR_TEXT_FAINT}, width=100)
                ui.Spacer()
                ui.Label("Action", style={"font_size": 10, "color": CLR_TEXT_FAINT},
                         width=80, alignment=ui.Alignment.CENTER)
                ui.Spacer(width=14)

        ui.Line(style={"color": CLR_BORDER}, height=1)

    def _build_node_list(self):
        ext = self.ext
        with ui.ScrollingFrame(style={"background_color": CLR_BG_MID}):
            ext._list_container = ui.VStack(spacing=0)

    def _build_log_section(self):
        ext = self.ext
        # Header
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
                    "color": CLR_TEXT_FAINT,
                })
                btn_clr.set_clicked_fn(ext._clear_log)
                ui.Spacer(width=14)

        # Log‑Container
        with ui.ScrollingFrame(height=110, style={"background_color": 0xFF080C14}):
            ext._log_container = ui.VStack(spacing=0)