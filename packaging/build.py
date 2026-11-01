"""Build release archives without generating metadata in the checkout."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    output = args.outdir.resolve()
    with tempfile.TemporaryDirectory(prefix="rctf-package-") as directory:
        stage = Path(directory)
        for name in ("pyproject.toml", "MANIFEST.in", "setup.py"):
            shutil.copy2(ROOT / "packaging" / name, stage / name)
        for name in ("README.md", "install.py", "rctf-monitor.py"):
            shutil.copy2(ROOT / name, stage / name)
        for name in ("src",):
            shutil.copytree(
                ROOT / name,
                stage / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc"),
            )
        (stage / "helpers").mkdir()
        for name in ("22.sh", "25.sh"):
            shutil.copy2(ROOT / "helpers" / name, stage / "helpers" / name)
        return subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(output)], cwd=stage
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
