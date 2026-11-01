#!/usr/bin/env python3
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from rctf_monitor.app import entrypoint

raise SystemExit(entrypoint())
