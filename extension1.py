import omni.ext
import omni.ui as ui
import omni.usd
import os
import json
import asyncio
import aiohttp

class MyExtension(omni.ext.IExt):
    def on_startup(self, ext_id):
        print("[MazeRunner] Startup gestartet...")
        
        ext_path = omni.kit.app.get_app().get_extension_manager().get_extension_path(ext_id)
        self.json_path = os.path.join(ext_path, "omni", "mazerunner", "API", "nodes_db.json")
        
        self.api_url_get = "https://digitaltwinservice.de/api/Database/GetValue"
        self.api_url_set = "https://digitaltwinservice.de/api/Database/SetValue"
        self.api_key = "2b56f658-b11f-4067-9537-631bf27a30f0"
        
        self._window = ui.Window("Maze Runner - Web-API Control-Center", width=850, height=450)
        self._window.deferred_dock_in("Property")

        self.node_labels = {}
        self.node_values = {}
        self.nodes = []
        self._is_running = True 
        self._update_task = None

        with self._window.frame:
            with ui.VStack(spacing=5, m=10):
                with ui.HStack(height=35):
                    ui.Label("Web-API Control-Center", style={"font_size": 18, "color": 0xFF00BFFF})
                    ui.Spacer()
                    ui.Button("REFRESH JSON", width=120, height=30, clicked_fn=self.load_nodes_from_json)
                
                ui.Separator(height=10)
                
                with ui.HStack(height=20):
                    ui.Label("Variable", width=150, style={"color": 0xFFAAAAAA})
                    ui.Label("Node-ID", width=150, style={"color": 0xFFAAAAAA})
                    ui.Label("Zustand", width=100, style={"color": 0xFFAAAAAA})
                    ui.Spacer(width=50)
                    ui.Label("Aktion", width=120, style={"color": 0xFFAAAAAA})

                ui.Separator(height=2)

                with ui.ScrollingFrame():
                    self._list_container = ui.VStack(spacing=8)

        self.load_nodes_from_json()
        self._update_task = asyncio.ensure_future(self.auto_update_loop())

    def load_nodes_from_json(self):
        if not hasattr(self, "_list_container"): return
        self._list_container.clear()
        
        if not os.path.exists(self.json_path):
            print(f"[MazeRunner] FEHLER: Datei nicht gefunden: {self.json_path}")
            return
            
        try:
            with open(self.json_path, 'r') as f:
                self.nodes = json.load(f).get("nodes", [])
                print(f"[MazeRunner] {len(self.nodes)} Nodes geladen.")
        except Exception as e:
            print(f"[MazeRunner] JSON Error: {e}")
            return

        with self._list_container:
            for node in self.nodes:
                node_id = node.get('node_id', '')
                with ui.HStack(height=30):
                    ui.Label(str(node.get('display_name', 'Unknown')), width=150)
                    ui.Label(str(node_id), width=150, style={"color": 0xFFBBBBBB})
                    
                    lbl = ui.Label("WAITING...", width=100)
                    self.node_labels[node_id] = lbl
                    
                    ui.Spacer(width=50)
                    ui.Button("TOGGLE", width=120, height=24, 
                              clicked_fn=lambda n=node_id: self.on_control_clicked(n))

    def on_control_clicked(self, node_id):
        current_val = self.node_values.get(node_id, False)
        new_val = not current_val if isinstance(current_val, bool) else 0.0
        
        if node_id in self.node_labels:
            self.node_values[node_id] = new_val 
            self.node_labels[node_id].text = f"{new_val} (sending...)"
            self.node_labels[node_id].set_style({"color": 0xFFFFFF00}) # Gelb

        asyncio.ensure_future(self.send_api_update(node_id, new_val))

    async def send_api_update(self, node_id, value):
        headers = {"X-API-KEY": self.api_key, "accept": "application/json"}
        val_to_send = str(value).lower() if isinstance(value, bool) else str(value)
        params = {"NodeName": node_id, "Value": val_to_send, "user": "admin", "apiKey": self.api_key}
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(self.api_url_set, params=params, headers=headers, timeout=5, ssl=False) as resp:
                    if resp.status == 200:
                        # ERFOLG: (sending...) entfernen und Farbe anpassen
                        if node_id in self.node_labels:
                            self.node_labels[node_id].text = str(value)
                            color = 0xFF00FF00 if value is True else 0xFFFFFFFF
                            self.node_labels[node_id].set_style({"color": color})
                    else:
                        if node_id in self.node_labels:
                            self.node_labels[node_id].text = "SPS ERROR"
                            self.node_labels[node_id].set_style({"color": 0xFF0000FF})
            except Exception as e:
                print(f"[MazeRunner] Fehler bei SetValue: {e}")

    async def auto_update_loop(self):
        while self._is_running:
            try:
                async with aiohttp.ClientSession() as session:
                    while self._is_running:
                        await self.update_via_web_api(session)
                        await asyncio.sleep(0.01) 
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[MazeRunner] Loop Fehler: {e}")
                await asyncio.sleep(0.5)

    async def update_via_web_api(self, session):
        headers = {"X-API-KEY": self.api_key, "accept": "application/json"}
        stage = omni.usd.get_context().get_stage()

        for node in self.nodes:
            if not self._is_running: break
            node_id = node.get('node_id')
            label = self.node_labels.get(node_id)
            if not label: continue

            params = {"NodeName": node_id, "useHistoricalData": "false", "user": "admin", "apiKey": self.api_key}

            try:
                async with session.get(self.api_url_get, params=params, headers=headers, timeout=5, ssl=False) as resp:
                    if resp.status == 200:
                        data = await resp.text()
                        val_str = data.replace('"', '').strip()
                        
                        if val_str.lower() == "true": val = True
                        elif val_str.lower() == "false": val = False
                        else:
                            try: val = float(val_str)
                            except: val = val_str

                        if "sending" not in label.text:
                            self.node_values[node_id] = val
                            label.text = str(val)
                            label.set_style({"color": 0xFF00FF00 if val is True else 0xFFFFFFFF})

                        p_path = node.get("prim_path")
                        if p_path and stage:
                            prim = stage.GetPrimAtPath(p_path)
                            if prim and prim.IsValid():
                                attr = prim.GetAttribute(node.get("attribute", "xformOp:translate"))
                                if attr:
                                    mult = 1.0 if (val is True or val == "1.0" or val == 1.0) else 0.0
                                    attr.Set(node.get("target_value", 1.0) * mult)
                    else:
                        if "sending" not in label.text:
                            label.text = f"HTTP {resp.status}"
            except Exception:
                if "sending" not in label.text:
                    label.text = "OFFLINE"

    def on_shutdown(self):
        print("[MazeRunner] Shutdown...")
        self._is_running = False
        if self._update_task:
            self._update_task.cancel()
        
        self.node_labels = {}
        self.node_values = {}
        self.nodes = []
        
        if self._window:
            self._window.destroy()
            self._window = None