"""Build-only helper packaging; normal installation uses install.py."""

from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        destination = Path(self.build_lib) / "rctf_monitor"
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("22.sh", "25.sh"):
            shutil.copy2(Path(__file__).parent / "helpers" / name, destination / name)

    def get_outputs(self, include_bytecode=1):
        return super().get_outputs(include_bytecode) + [
            str(Path(self.build_lib) / "rctf_monitor" / name)
            for name in ("22.sh", "25.sh")
        ]


setup(cmdclass={"build_py": BuildPy})
