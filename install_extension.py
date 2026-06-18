from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_USER_EXTENSION_ROOT = Path.home() / "AppData" / "Roaming" / "NVIDIA Corporation" / "Omniverse" / "Extensions"

IGNORE_PATTERNS = [
    ".venv",
    ".git",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "documentation.pdf",
    "documentation.aux",
    "documentation.log",
    "documentation.out",
    "documentation.fls",
    "documentation.fdb_latexmk",
    "documentation.synctex.gz",
]


def copy_extension(src: Path, dest: Path) -> None:
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"Extension-Ordner nicht gefunden: {src}")

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*IGNORE_PATTERNS))


def format_install_command(extension_path: Path) -> str:
    return (
        f"# In PowerShell:\n"
        f"$env:OMNI_KIT_EXTENSION_PATH=\"{extension_path}\"\n"
        f"Start-Process -FilePath \"<IsaacSimExecutable>\" -ArgumentList \"--extensions {extension_path}\""
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Installiere die Dobot-Extension in Isaac Sim.")
    parser.add_argument(
        "--dest",
        default=str(DEFAULT_USER_EXTENSION_ROOT),
        help="Zielordner für die Extension im Roaming-Verzeichnis von Omniverse.",
    )
    parser.add_argument(
        "--name",
        default=ROOT.name,
        help="Name des Extension-Zielordners.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Zeige, was gemacht würde, ohne Dateien zu kopieren.",
    )

    args = parser.parse_args()
    dest_root = Path(args.dest).expanduser().resolve()
    target = dest_root / args.name

    print(f"Quellordner: {ROOT}")
    print(f"Zielordner: {target}")

    if args.dry_run:
        print("Dry run: keine Dateien werden kopiert.")
    else:
        dest_root.mkdir(parents=True, exist_ok=True)
        copy_extension(ROOT, target)
        print(f"Extension erfolgreich installiert nach: {target}")

    print("\nNächste Schritte:")
    print("1. Isaac Sim neu starten.")
    print("2. Falls Isaac Sim die Extension nicht automatisch findet, starte Isaac Sim aus einer PowerShell-Session mit:")
    print(format_install_command(target))
    print("3. Oder kopiere den Ordner direkt in deinen Benutzer-Extensions-Pfad.")


if __name__ == "__main__":
    main()
