#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pipeline environment diagnostics:
- DB creation/write check
- Output directory write check
- ffmpeg availability
- key Python deps import check
- optional VOICE engine reachability
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Tuple
import shutil

# Allow running from scripts/ directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import env_loader
from config import CFG


def ok(msg: str) -> None:
    print(f"[OK]  {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def fail(msg: str, failures: List[str]) -> None:
    failures.append(msg)
    print(f"[FAIL] {msg}")


def check_import(mod_name: str, failures: List[str], optional: bool = False) -> None:
    try:
        __import__(mod_name)
        ok(f"import {mod_name}")
    except Exception as e:
        if optional:
            warn(f"import {mod_name} failed (optional): {type(e).__name__}: {e}")
        else:
            fail(f"import {mod_name} failed: {type(e).__name__}: {e}", failures)


def check_dir_writable(path: Path, label: str, failures: List[str]) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_dir = path / "_diagnose_write_test"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / "write_check.txt"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        test_dir.rmdir()
        ok(f"{label} writable: {path}")
    except Exception as e:
        fail(f"{label} not writable: {path} ({type(e).__name__}: {e})", failures)


def check_db(db_path: Path, failures: List[str]) -> None:
    if not db_path:
        fail("DB_PATH is empty", failures)
        return

    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        fail(f"DB parent dir not creatable: {db_path.parent} ({e})", failures)
        return

    try:
        con = sqlite3.connect(str(db_path), timeout=30)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
              id TEXT PRIMARY KEY,
              check_create INTEGER NOT NULL DEFAULT 0,
              check_date TEXT NOT NULL,
              post_date TEXT NOT NULL,
              comments_count INTEGER NOT NULL,
              category TEXT NOT NULL,
              title TEXT NOT NULL,
              post_title TEXT
            );
            """
        )
        con.commit()

        # lightweight write test (temp table)
        con.execute("CREATE TABLE IF NOT EXISTS _diagnose_tmp (id INTEGER);")
        con.execute("INSERT INTO _diagnose_tmp (id) VALUES (1);")
        con.execute("DELETE FROM _diagnose_tmp WHERE id=1;")
        con.commit()
        con.execute("DROP TABLE IF EXISTS _diagnose_tmp;")
        con.commit()
        con.close()
        ok(f"DB create/write check: {db_path}")
    except Exception as e:
        fail(f"DB create/write check failed: {db_path} ({type(e).__name__}: {e})", failures)


def check_ffmpeg(failures: List[str]) -> None:
    if shutil.which("ffmpeg") is None:
        fail("ffmpeg not found in PATH", failures)
    else:
        ok("ffmpeg found")

    if shutil.which("ffprobe") is None:
        warn("ffprobe not found (audio check may be skipped)")
    else:
        ok("ffprobe found")


def _voice_app_candidates() -> List[Path]:
    from os import environ

    raw = str(environ.get("VOICE_APP_CANDIDATES", "")).strip()
    if raw:
        return [Path(p.strip()) for p in raw.split(";") if p.strip()]
    return [
        Path("/Applications/VOICEVOX.app"),
        Path("/Applications/VOICEBOX.app"),
    ]


def _engine_url() -> str:
    return (
        (getattr(CFG, "ENGINE_URL", "") or "").strip()
        or env_loader.env_str("ENGINE_URL", "http://127.0.0.1:50021").strip()
        or "http://127.0.0.1:50021"
    )


def check_voice_engine(require_voice: bool, failures: List[str]) -> None:
    try:
        import requests  # type: ignore
    except Exception as e:
        if require_voice:
            fail(f"requests not available for voice check: {e}", failures)
        else:
            warn("requests not available; skip voice engine check")
        return

    engine_url = _engine_url()
    try:
        r = requests.get(f"{engine_url}/speakers", timeout=2)
        if r.status_code == 200:
            ok(f"VOICE engine reachable: {engine_url}")
        else:
            msg = f"VOICE engine not ready: {engine_url} (status={r.status_code})"
            if require_voice:
                fail(msg, failures)
            else:
                warn(msg)
    except Exception as e:
        msg = f"VOICE engine unreachable: {engine_url} ({type(e).__name__}: {e})"
        if require_voice:
            fail(msg, failures)
        else:
            warn(msg)


def check_voice_launchable(failures: List[str]) -> None:
    """
    Local engine launchability check:
    - If ENGINE_URL is localhost, verify VOICE app candidates exist.
    """
    engine_url = _engine_url().lower()
    if not (engine_url.startswith("http://127.0.0.1") or engine_url.startswith("http://localhost")):
        ok(f"VOICE launch check skipped (remote engine): {engine_url}")
        return

    candidates = _voice_app_candidates()
    existing = [p for p in candidates if p.exists()]
    if existing:
        ok(f"VOICE app found (launchable): {existing[0]}")
    else:
        fail(
            "VOICE app not found (install VOICEVOX or set VOICE_APP_CANDIDATES)",
            failures,
        )


def check_required_files(failures: List[str]) -> None:
    """
    Check common required assets in BASE_OUTPUT_ROOT.
    Missing files are warnings (not fatal) unless they are critical for current flow.
    """
    if not CFG.BASE_OUTPUT_ROOT:
        return

    base = Path(str(CFG.BASE_OUTPUT_ROOT))
    candidates = [
        ("ending image", base / "image" / "last_01.png"),
        ("ending audio", base / "last" / "last.wav"),
        ("bgm odd", base / "bgm" / "1.mp3"),
        ("bgm even", base / "bgm" / "2.mp3"),
        ("api dir", base / "api"),
    ]

    for label, path in candidates:
        if path.exists():
            ok(f"{label} exists: {path}")
        else:
            warn(f"{label} missing: {path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--require-voice",
        action="store_true",
        help="VOICE engine must be reachable (otherwise fail).",
    )
    args = ap.parse_args()

    env_loader.load_env()

    failures: List[str] = []

    print("=== girlsChannel2 diagnostics ===")
    print(f"[INFO] DB_PATH: {CFG.DB_PATH}")
    print(f"[INFO] BASE_OUTPUT_ROOT: {CFG.BASE_OUTPUT_ROOT}")

    check_import("PIL", failures)
    check_import("playwright", failures)
    check_import("requests", failures, optional=True)

    check_dir_writable(Path("/tmp"), "tmp", failures)

    if not CFG.BASE_OUTPUT_ROOT:
        fail("BASE_OUTPUT_ROOT is empty", failures)
    else:
        check_dir_writable(CFG.BASE_OUTPUT_ROOT, "output_root", failures)

    check_db(CFG.DB_PATH, failures)
    check_ffmpeg(failures)
    check_voice_engine(args.require_voice, failures)
    check_voice_launchable(failures)
    check_required_files(failures)

    print("=== summary ===")
    if failures:
        for i, msg in enumerate(failures, start=1):
            print(f"{i}. {msg}")
        print("RESULT: FAIL")
        return 1

    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
