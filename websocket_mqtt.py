import asyncio
import struct
import websockets

# Korrektur laut Screenshot: Port 9001
MQTT_WS_URL = "ws://digitaltwinservice.de:9001/mqtt"

class WebSocketMQTTClient:
    def __init__(self, on_message_callback=None, topic_list=None):
        self.ws = None
        self.on_message = on_message_callback
        self.topics = topic_list or []
        self.connected = False
        self._running = True
        # Eindeutige Client-ID (verhindert Konflikte mit anderen Sessions)
        self.client_id = "omni_client_" + str(id(self))[-4:]

    def _mqtt_connect_packet(self):
        # Variable Header
        protocol_name = b'\x00\x04MQTT'
        protocol_level = b'\x04'  # MQTT v3.1.1
        connect_flags = b'\x02'   # Clean Session
        keepalive = struct.pack("!H", 60)
        
        variable_header = protocol_name + protocol_level + connect_flags + keepalive

        # Payload: Client ID (Präfix mit 2-Byte Länge)
        id_bytes = self.client_id.encode()
        payload = struct.pack("!H", len(id_bytes)) + id_bytes

        remaining_length = len(variable_header) + len(payload)
        
        # Fixed Header (0x10 = Connect)
        return b'\x10' + self._encode_length(remaining_length) + variable_header + payload

    def _mqtt_subscribe_packet(self, packet_id, topic):
        # Variable Header: Packet Identifier
        variable_header = struct.pack("!H", packet_id)
        
        # Payload: Topic mit Länge + QoS (0x00)
        topic_bytes = topic.encode()
        payload = struct.pack("!H", len(topic_bytes)) + topic_bytes + b"\x00"
        
        remaining_length = len(variable_header) + len(payload)
        
        # Fixed Header (0x82 = Subscribe, bit 1 must be set)
        return b'\x82' + self._encode_length(remaining_length) + variable_header + payload

    def _mqtt_ping(self):
        return b'\xC0\x00'

    def _encode_length(self, length):
        r = b""
        while True:
            encoded = length % 128
            length //= 128
            if length > 0:
                encoded |= 128
            r += struct.pack("!B", encoded)
            if length == 0:
                break
        return r

    async def connect(self):
        try:
            # Verbindungsaufbau mit dem korrekten Port
            self.ws = await websockets.connect(
                MQTT_WS_URL,
                subprotocols=["mqtt"], # Wichtig für viele WebSocket-MQTT-Broker
                ping_interval=None
            )
            
            # 1. Connect senden
            await self.ws.send(self._mqtt_connect_packet())
            
            # Kurze Pause, damit der Broker den Connect verarbeiten kann
            await asyncio.sleep(0.2)
            
            self.connected = True

            # 2. Topics abonnieren
            for i, t in enumerate(self.topics):
                await self.ws.send(self._mqtt_subscribe_packet(i+1, t))
                print(f"[WS-MQTT] Subscribing to: {t}")

            asyncio.create_task(self._mqtt_reader())
            asyncio.create_task(self._mqtt_pinger())

            print(f"[WS-MQTT] Connected as {self.client_id}")

        except Exception as e:
            print(f"[WS-MQTT] Connection error: {e}")
            self.connected = False

    async def _mqtt_reader(self):
        try:
            while self._running and self.connected:
                frame = await self.ws.recv()
                if not frame or isinstance(frame, str):
                    continue

                packet_type = frame[0] >> 4

                # PUBLISH (Typ 3)
                if packet_type == 3:
                    # Längen-Decoding
                    remaining_len, consumed = self._decode_length(frame, 1)
                    idx = 1 + consumed

                    # Topic extrahieren
                    topic_len = struct.unpack("!H", frame[idx:idx+2])[0]
                    idx += 2
                    topic = frame[idx:idx+topic_len].decode()
                    idx += topic_len

                    # Payload extrahieren (Rest des Frames)
                    payload = frame[idx:].decode('utf-8', errors='ignore')

                    if self.on_message:
                        self.on_message(topic, payload)

        except Exception as e:
            print(f"[WS-MQTT] Reader error: {e}")
            self.connected = False

    def _decode_length(self, data, start):
        m = 1
        value = 0
        pos = start
        while True:
            encoded = data[pos]
            pos += 1
            value += (encoded & 127) * m
            if (encoded & 128) == 0:
                break
            m *= 128
        return value, pos - start

    async def disconnect(self):
        self._running = False
        self.connected = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    async def _mqtt_pinger(self):
        while self._running and self.connected:
            try:
                await asyncio.sleep(30)
                if self.ws:
                    await self.ws.send(self._mqtt_ping())
            except:
                break