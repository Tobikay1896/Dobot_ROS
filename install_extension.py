"""
install_extension.py
====================
Hilfsskript zum Installieren der Dobot-Extension in den Omniverse-Extensions-Ordner.

WANN WIRD DIESES SKRIPT BENÖTIGT?
----------------------------------
Isaac Sim kennt zwei Wege, eine Extension zu finden:

  1. Suchpfad im Extension-Manager:
     Im Extension-Manager (Fenster → Extensions → Zahnrad-Symbol → Paths)
     kann ein zusätzlicher Ordner eingetragen werden. Liegt der
     Quellordner dort direkt, reicht das für die Entwicklung aus.

  2. Roaming-Extensions-Verzeichnis (Standard-Installationspfad):
     %APPDATA%\\NVIDIA Corporation\\Omniverse\\Extensions
     Dieses Verzeichnis wird von Isaac Sim beim Start automatisch
     durchsucht. Wer die Extension auf einem anderen Rechner betreiben
     oder dauerhaft ohne manuellen Pfadeintrag verwenden möchte,
     kopiert den Ordner dorthin – genau das erledigt dieses Skript.

WANN IST DAS SKRIPT NICHT NÖTIG?
----------------------------------
Wer den Quellordner direkt im Extension-Manager als Suchpfad einträgt,
muss nichts kopieren. Das Skript ist vor allem nützlich für:
  - Deployment auf einem Produktionsrechner ohne Entwicklungsumgebung
  - Saubere Installation ohne Git-Artefakte, __pycache__ usw.
  - Schnelle Ersteinrichtung auf einem neuen System

VERWENDUNG:
-----------
  # Standard-Installation in das Omniverse-Roaming-Verzeichnis:
  python install_extension.py

  # Eigenes Zielverzeichnis und eigener Ordnername:
  python install_extension.py --dest "C:\\MeinPfad" --name dobot_ros

  # Vorschau: zeigt Quelle und Ziel, kopiert aber nichts:
  python install_extension.py --dry-run
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

# Absoluter Pfad des Ordners, in dem dieses Skript liegt (= Extension-Root).
ROOT = Path(__file__).resolve().parent

# Standard-Zielverzeichnis: der Omniverse-Roaming-Extensions-Ordner des
# aktuell angemeldeten Windows-Nutzers. Isaac Sim durchsucht diesen Ordner
# beim Start automatisch nach installierten Extensions.
DEFAULT_USER_EXTENSION_ROOT = (
    Path.home()
    / "AppData"
    / "Roaming"
    / "NVIDIA Corporation"
    / "Omniverse"
    / "Extensions"
)

# Dateimuster, die beim Kopieren ausgeschlossen werden.
# So landet kein Entwicklungs-Ballast (Cache, Git-History, Build-Artefakte)
# im Installationsziel.
IGNORE_PATTERNS = [
    ".venv",           # lokale Python-Entwicklungsumgebung
    ".git",            # Git-Versionsverwaltung
    "__pycache__",     # Python-Bytecode-Cache
    "*.pyc",           # kompilierte Python-Dateien
    "*.pyo",
    "*.pyd",
    "documentation.pdf",          # LaTeX-Ausgabe
    "documentation.aux",
    "documentation.log",
    "documentation.out",
    "documentation.fls",
    "documentation.fdb_latexmk",
    "documentation.synctex.gz",
]


def copy_extension(src: Path, dest: Path) -> None:
    """Kopiert den Extension-Ordner von src nach dest.

    Ein bereits vorhandenes Zielverzeichnis wird vollständig ersetzt,
    damit veraltete Dateien nicht liegenbleiben.
    IGNORE_PATTERNS filtert Entwicklungs-Artefakte heraus.
    """
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"Extension-Ordner nicht gefunden: {src}")

    if dest.exists():
        shutil.rmtree(dest)   # altes Installationsverzeichnis komplett löschen

    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*IGNORE_PATTERNS))


def format_install_command(extension_path: Path) -> str:
    """Gibt einen PowerShell-Befehl zurück, mit dem Isaac Sim den
    Installationspfad als Extension-Suchpfad übergeben bekommt –
    als Alternative zum Eintrag im Extension-Manager-UI."""
    return (
        f"# In PowerShell:\n"
        f"$env:OMNI_KIT_EXTENSION_PATH=\"{extension_path}\"\n"
        f"Start-Process -FilePath \"<IsaacSimExecutable>\""
        f" -ArgumentList \"--extensions {extension_path}\""
    )


def main() -> None:
    """Kommandozeilen-Einstiegspunkt: Parameter parsen, Ziel berechnen,
    kopieren (oder Dry-Run ausgeben) und Nächste-Schritte-Hinweis drucken."""
    parser = argparse.ArgumentParser(
        description="Installiere die Dobot-Extension in den Omniverse-Extensions-Ordner."
    )
    parser.add_argument(
        "--dest",
        default=str(DEFAULT_USER_EXTENSION_ROOT),
        help="Zielordner (Standard: Omniverse-Roaming-Extensions-Verzeichnis).",
    )
    parser.add_argument(
        "--name",
        default=ROOT.name,   # übernimmt den Namen des Quellordners
        help="Name des Extension-Zielordners (Standard: Name des Quellordners).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nur Ausgabe – keine Dateien werden tatsächlich kopiert.",
    )

    args = parser.parse_args()
    dest_root = Path(args.dest).expanduser().resolve()
    target = dest_root / args.name   # vollständiger Zielpfad

    print(f"Quellordner : {ROOT}")
    print(f"Zielordner  : {target}")

    if args.dry_run:
        print("Dry run – keine Dateien wurden kopiert.")
    else:
        dest_root.mkdir(parents=True, exist_ok=True)   # Zielordner anlegen falls nicht vorhanden
        copy_extension(ROOT, target)
        print(f"Extension erfolgreich installiert nach: {target}")

    print("\nNächste Schritte:")
    print("1. Isaac Sim neu starten.")
    print(
        "2. Falls Isaac Sim die Extension nicht automatisch findet, "
        "starte Isaac Sim aus einer PowerShell-Session mit:"
    )
    print(format_install_command(target))
    print("3. Oder trage den Ordner direkt im Extension-Manager als Suchpfad ein.")


if __name__ == "__main__":
    main()
