#!/usr/bin/env python3
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    target = Path(__file__).resolve().parents[2] / "scripts_parts" / "make_audio.py"
    runpy.run_path(str(target), run_name="__main__")
