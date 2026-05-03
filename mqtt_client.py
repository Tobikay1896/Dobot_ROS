import threading
import time
import paho.mqtt.client as mqtt


class MQTTClient:
    """
    Wrapper um paho-mqtt.  Läuft in einem eigenen Thread,
    liefert Callbacks für Message- und Connect-Status.
    """
    def __init__(self,
                 broker,
                 port,
                 keepalive,
                 on_message_cb,
                 on_status_cb,
                 logger=print):
        self.broker = broker
        self.port = port
        self.keepalive = keepalive
        self.on_message_cb = on_message_cb
        self.on_status_cb = on_status_cb
        self._log = logger

        self._client = mqtt.Client()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self._thread = None
        self._running = False
        self._topics = []

        self._log("[MQTT] MQTTClient initialisiert", "info")

    # -----------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, rc):
        self._log(f"[MQTT] Connected (rc={rc})", "info")
        self.on_status_cb(True)
        for t in self._topics:
            client.subscribe(t)
            self._log(f"[MQTT] Subscribed to {t}", "info")

    def _on_disconnect(self, client, userdata, rc):
        self._log(f"[MQTT] Disconnected (rc={rc})", "info")
        self.on_status_cb(False)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8")
        self._log(f"[MQTT] {msg.topic} → {payload}", "log")
        self.on_message_cb(msg.topic, payload)

    # -----------------------------------------------------------------
    def start(self, topics):
        """Startet den MQTT-Thread und abonniert `topics`."""
        self._log("[MQTT] MQTT Client start wird aufgerufen", "info")
        if self._running:
            self._log("[MQTT] Already running", "info")
            return
        self._topics = topics
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._log("[MQTT] Thread gestartet", "info")

    def stop(self):
        """Beendet den Loop und wartet auf den Thread."""
        self._log("[MQTT] MQTT Client stop wird aufgerufen", "info")
        if not self._running:
            self._log("[MQTT] Nicht aktiv – stoppe nichts", "info")
            return
        self._running = False
        self._client.disconnect()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._log("[MQTT] Gestoppt", "info")

    # -----------------------------------------------------------------
    def _run(self):
        try:
            self._log("[MQTT] Versuche Verbindung zum Broker...", "info")
            self._client.connect(self.broker, self.port, self.keepalive)
            self._client.loop_start()
            self._log("[MQTT] Loop gestartet", "info")
            while self._running:
                time.sleep(0.1)  # keep thread alive
        except Exception as e:
            self._log(f"[MQTT] Fatal error: {e}", "error")
        finally:
            self._client.loop_stop()
            self._log("[MQTT] Loop gestoppt", "info")