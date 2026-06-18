import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
tex = root / "documentation.tex"
pdf = root / "documentation.pdf"

if not shutil.which("pdflatex"):
    raise SystemExit("pdflatex wurde nicht gefunden. Bitte installieren Sie eine LaTeX-Distribution.")

cmd = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-output-directory", str(root), str(tex)]
subprocess.run(cmd, check=True)
print(f"Erzeugt: {pdf}")
