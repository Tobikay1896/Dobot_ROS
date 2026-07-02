# calibrate_j1_offset.py -- Isaac Sim Script Editor
# Anleitung:
#   1. Extension verbunden (Connect geklickt)
#   2. Dobot physisch exakt nach vorne ausrichten
#   3. Dieses Script im Script Editor ausfuehren
#   4. Ausgegebenen Wert als J1_YAW_OFFSET_DEG in extension.py eintragen

import gc

def calibrate():
    # Laufende DobotBridgeExtension-Instanz ueber Garbage Collector finden
    try:
        from dobot_ros.extension import DobotBridgeExtension
    except ImportError:
        print("[Fehler] Modul dobot_ros nicht importierbar.")
        print("         Stelle sicher, dass die Extension aktiv ist.")
        return

    # Alle Instanzen durchsuchen, erste mit gueltigem _ros_node nehmen
    node = None
    for obj in gc.get_objects():
        if isinstance(obj, DobotBridgeExtension):
            candidate = getattr(obj, "_ros_node", None)
            if candidate is not None:
                node = candidate
                break

    if node is None:
        print("[Fehler] Keine verbundene DobotBridgeExtension gefunden.")
        print("         -> Bitte zuerst 'Connect' klicken.")
        return

    device = getattr(node, "device", None)
    if device is None:
        print("[Fehler] Kein Dobot-Device (COM-Port nicht verbunden).")
        return

    try:
        pose = device.pose()
    except Exception as e:
        print("[Fehler] pose() fehlgeschlagen:", e)
        return

    if not pose or len(pose) < 8:
        print("[Fehler] Ungueltige Pose-Antwort.")
        return

    x, y, z, r = pose[0], pose[1], pose[2], pose[3]
    j1, j2, j3, j4 = pose[4], pose[5], pose[6], pose[7]

    # Wenn der Roboter jetzt nach vorne zeigt, soll joint_1 in Sim = 0 sein.
    # j1_sim = j1_real + offset = 0  =>  offset = -j1_real
    offset = -j1

    print("=" * 52)
    print("  J1-OFFSET KALIBRIERUNG")
    print("=" * 52)
    print("  Pos:    x={:.1f}  y={:.1f}  z={:.1f}  r={:.1f}".format(x, y, z, r))
    print("  Joints: j1={:.2f}  j2={:.2f}  j3={:.2f}  j4={:.2f}".format(j1, j2, j3, j4))
    print()
    print("  Offset:  {:.2f} Grad".format(offset))
    print("  Probe:   {:.2f} + ({:.2f}) = {:.2f} (soll 0)".format(j1, offset, j1 + offset))
    print()
    print("  In extension.py eintragen:")
    print("    J1_YAW_OFFSET_DEG = {:.1f}".format(offset))
    print("=" * 52)

calibrate()
