#!/usr/bin/env python3
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from rctf_monitor.installer import main

try:
    raise SystemExit(main())
except (OSError, ValueError, RuntimeError) as error:
    print(f"설치 실패: {error}", file=sys.stderr)
    raise SystemExit(1)
