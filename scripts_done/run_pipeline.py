#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import List, Optional
from zoneinfo import ZoneInfo

from tqdm import tqdm

# Allow running from scripts_done/ directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import CFG
from core import env_loader

# =========================================================
# 設定値（このスクリプトで使う運用変数は上部に集約）
# =========================================================

# DBパス（必須）
DB_PATH = CFG.DB_PATH
if not DB_PATH:
    raise SystemExit("[ENV] missing required key: DB_PATH")

# 入力/進捗管理テーブル名
ITEMS_DO_TABLE = CFG.ITEMS_DO_TABLE or "items_do"
ITEMS_DONE_TABLE = CFG.ITEMS_DONE_TABLE or "items_done"
YOUTUBE_HISTORY_TABLE = (
    env_loader.env_str("YOUTUBE_HISTORY_TABLE", "youtube_history").strip()
    or "youtube_history"
)

# スクリプト配置
SCRIPTS_DONE_DIR = Path(__file__).resolve().parent
SCRIPTS_PARTS_DIR = REPO_ROOT / "scripts_parts"

# 各工程スクリプト
SCRIPT_02 = SCRIPTS_PARTS_DIR / (CFG.SCRIPT_02_NAME or "fetch_data.py")
SCRIPT_03 = SCRIPTS_PARTS_DIR / (CFG.SCRIPT_03_NAME or "make_images.py")
SCRIPT_04 = SCRIPTS_PARTS_DIR / (CFG.SCRIPT_04_NAME or "make_audio.py")
SCRIPT_05 = SCRIPTS_PARTS_DIR / (CFG.SCRIPT_05_NAME or "make_preview.py")
SCRIPT_99 = SCRIPTS_PARTS_DIR / (CFG.SCRIPT_99_NAME or "assemble_video.py")

# 実行制御
RUNS_PIPELINE = CFG.RUNS_DEFAULT
STOP_ON_ERROR = CFG.STOP_ON_ERROR
SLEEP_SEC_WHEN_EMPTY = float(CFG.SLEEP_SEC_WHEN_EMPTY)
PASS_FOLDER_NAME_TO_05 = CFG.PASS_FOLDER_NAME_TO_05

# env実行パラメータ（引数ではなく env で制御）
RUN_STEPS_RAW = (env_loader.env_str("RUN_STEPS", "pipeline") or "pipeline").strip()
PIPELINE_UNTIL_RAW = (env_loader.env_str("PIPELINE_UNTIL", "99") or "99").strip()
MAX_PIPELINE_CYCLES = int(env_loader.env_int("MAX_PIPELINE_CYCLES", 0))
POST_TITLE_MAX_CHARS = int(env_loader.env_int("POST_TITLE_MAX_CHARS", 95))

# ステージ番号
STA_02 = CFG.STA_02
END_02 = CFG.END_02
STA_03 = CFG.STA_03
END_03 = CFG.END_03
STA_04 = CFG.STA_04
END_04 = CFG.END_04
STA_05 = CFG.STA_05
END_05 = CFG.END_05
STA_99 = CFG.STA_99
END_99 = CFG.END_99

# 各工程タイムアウト
TIMEOUT_02 = CFG.TIMEOUT_02
TIMEOUT_03 = CFG.TIMEOUT_03
TIMEOUT_04 = CFG.TIMEOUT_04
TIMEOUT_05 = CFG.TIMEOUT_05
TIMEOUT_99 = CFG.TIMEOUT_99

# SQLite設定
BUSY_TIMEOUT_MS = CFG.BUSY_TIMEOUT_MS
SQLITE_JOURNAL_MODE = (CFG.SQLITE_JOURNAL_MODE or "WAL").strip()
SQLITE_SYNCHRONOUS = (CFG.SQLITE_SYNCHRONOUS or "NORMAL").strip()

# ステージ実行順
PIPELINE_STAGE_ORDER = ["02", "03", "04", "05", "99"]
PIPELINE_LIMIT_TAG = "99"
PIPELINE_LIMIT_IDX = PIPELINE_STAGE_ORDER.index(PIPELINE_LIMIT_TAG)


@dataclass
class HistoryRun:
    steps: str
    start_at: str
    end_at: str = ""
    sec: float = 0.0
    error: str = ""


def now_jst_str() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d-%H:%M:%S")


def now_folder_stamp() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d-%H%M%S")


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
    if SQLITE_JOURNAL_MODE:
        con.execute(f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE};")
    if SQLITE_SYNCHRONOUS:
        con.execute(f"PRAGMA synchronous={SQLITE_SYNCHRONOUS};")
    return con


def ensure_tables(con: sqlite3.Connection) -> None:
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ITEMS_DO_TABLE} (
          id TEXT PRIMARY KEY,
          skip INTEGER NOT NULL DEFAULT 0,
          hot_score REAL,
          category TEXT NOT NULL,
          title TEXT NOT NULL,
          comments_count INTEGER NOT NULL,
          first_post_at TEXT,
          last_post_at TEXT NOT NULL,
          list_add_at TEXT NOT NULL
        )
        """
    )
    expected_items_do = [
        "id",
        "skip",
        "hot_score",
        "category",
        "title",
        "comments_count",
        "first_post_at",
        "last_post_at",
        "list_add_at",
    ]
    cols_items_do = [
        str(r[1]) for r in con.execute(f"PRAGMA table_info({ITEMS_DO_TABLE})").fetchall()
    ]
    if cols_items_do and cols_items_do != expected_items_do:
        tmp = f"{ITEMS_DO_TABLE}__rebuild"
        con.execute(f"DROP TABLE IF EXISTS {tmp}")
        con.execute(
            f"""
            CREATE TABLE {tmp} (
              id TEXT PRIMARY KEY,
              skip INTEGER NOT NULL DEFAULT 0,
              hot_score REAL,
              category TEXT NOT NULL,
              title TEXT NOT NULL,
              comments_count INTEGER NOT NULL,
              first_post_at TEXT,
              last_post_at TEXT NOT NULL,
              list_add_at TEXT NOT NULL
            )
            """
        )
        common = [c for c in expected_items_do if c in cols_items_do]
        if common:
            sel = ", ".join(common)
            con.execute(
                f"INSERT OR REPLACE INTO {tmp} ({sel}) SELECT {sel} FROM {ITEMS_DO_TABLE}"
            )
        con.execute(f"DROP TABLE {ITEMS_DO_TABLE}")
        con.execute(f"ALTER TABLE {tmp} RENAME TO {ITEMS_DO_TABLE}")

    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ITEMS_DONE_TABLE} (
          id TEXT PRIMARY KEY,
          skip INTEGER NOT NULL DEFAULT 0,
          stage INTEGER NOT NULL DEFAULT {STA_02},
          category TEXT,
          post_title TEXT,
          keywords TEXT,
          url TEXT,
          list_add_at TEXT,
          video_created_at TEXT,
          youtube_upload_at TEXT,
          youtube_publish_at TEXT,
          tiktok_upload_at TEXT,
          tiktok_publish_at TEXT,
          folder_delite_at TEXT,
          folder_name TEXT
        )
        """
    )
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {YOUTUBE_HISTORY_TABLE} (
          id TEXT PRIMARY KEY,
          youtube_video_id TEXT,
          youtube_uploaded_at TEXT,
          upload_youtube_at TEXT,
          publish_at_utc TEXT,
          publish_at_jst TEXT,
          source_table TEXT,
          first_detected_at TEXT NOT NULL,
          last_synced_at TEXT NOT NULL
        )
        """
    )

    cols = {r[1] for r in con.execute(f"PRAGMA table_info({ITEMS_DONE_TABLE})").fetchall()}
    if "upload_youtube_at" not in cols:
        con.execute(f"ALTER TABLE {ITEMS_DONE_TABLE} ADD COLUMN upload_youtube_at TEXT")
    if "upload_tiktok_at" not in cols:
        con.execute(f"ALTER TABLE {ITEMS_DONE_TABLE} ADD COLUMN upload_tiktok_at TEXT")

    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{ITEMS_DO_TABLE}_pick ON {ITEMS_DO_TABLE}(skip, hot_score DESC, list_add_at DESC, id DESC)"
    )
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{ITEMS_DONE_TABLE}_stage ON {ITEMS_DONE_TABLE}(stage, skip, id DESC)"
    )
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{YOUTUBE_HISTORY_TABLE}_video_id ON {YOUTUBE_HISTORY_TABLE}(youtube_video_id)"
    )
    con.commit()


def _pick_expr(cols: set[str], candidates: List[str], default_expr: str) -> str:
    for c in candidates:
        if c in cols:
            return c
    return default_expr


def _pick_col(cols: set[str], candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in cols:
            return c
    return None


def sync_youtube_history_from_items_done(con: sqlite3.Connection) -> int:
    cols = {str(r[1]) for r in con.execute(f"PRAGMA table_info({ITEMS_DONE_TABLE})").fetchall()}
    if "id" not in cols:
        return 0

    yt_id_col = _pick_col(
        cols,
        ["youtube_video_id", "YouTubeVideoId", "youtubeVideoId"],
    )
    uploaded_col = _pick_col(cols, ["youtube_uploaded_at"])
    upload_col = _pick_col(cols, ["upload_youtube_at", "youtube_upload_at"])
    publish_utc_col = _pick_col(cols, ["publish_at_utc"])
    publish_jst_col = _pick_col(cols, ["publish_at_jst"])
    created_col = _pick_col(cols, ["video_created_at", "list_add_at"])
    has_stage = "stage" in cols

    yt_id_expr = f"COALESCE({yt_id_col}, '')" if yt_id_col else "''"
    uploaded_expr = f"COALESCE({uploaded_col}, '')" if uploaded_col else "''"
    upload_expr = f"COALESCE({upload_col}, '')" if upload_col else "''"
    publish_utc_expr = f"COALESCE({publish_utc_col}, '')" if publish_utc_col else "''"
    publish_jst_expr = f"COALESCE({publish_jst_col}, '')" if publish_jst_col else "''"

    now = now_jst_str()
    created_expr = f"COALESCE(NULLIF({created_col},''), ?)" if created_col else "?"

    where_parts: List[str] = []
    if yt_id_col:
        where_parts.append(f"COALESCE({yt_id_col},'') != ''")
    if uploaded_col:
        where_parts.append(f"COALESCE({uploaded_col},'') != ''")
    if upload_col:
        where_parts.append(f"COALESCE({upload_col},'') != ''")
    if created_col:
        where_parts.append(f"COALESCE({created_col},'') != ''")
    if has_stage:
        where_parts.append("stage = ?")

    if not where_parts:
        return 0

    params: List[object] = [ITEMS_DONE_TABLE, now, now]
    if has_stage:
        params.append(int(END_99))

    con.execute(
        f"""
        INSERT INTO {YOUTUBE_HISTORY_TABLE} (
          id,
          youtube_video_id,
          youtube_uploaded_at,
          upload_youtube_at,
          publish_at_utc,
          publish_at_jst,
          source_table,
          first_detected_at,
          last_synced_at
        )
        SELECT
          id,
          {yt_id_expr},
          {uploaded_expr},
          {upload_expr},
          {publish_utc_expr},
          {publish_jst_expr},
          ?,
          {created_expr},
          ?
          FROM {ITEMS_DONE_TABLE}
         WHERE COALESCE(id,'') != ''
           AND ({' OR '.join(where_parts)})
        ON CONFLICT(id) DO UPDATE SET
          youtube_video_id = COALESCE(NULLIF(excluded.youtube_video_id,''), {YOUTUBE_HISTORY_TABLE}.youtube_video_id),
          youtube_uploaded_at = COALESCE(NULLIF(excluded.youtube_uploaded_at,''), {YOUTUBE_HISTORY_TABLE}.youtube_uploaded_at),
          upload_youtube_at = COALESCE(NULLIF(excluded.upload_youtube_at,''), {YOUTUBE_HISTORY_TABLE}.upload_youtube_at),
          publish_at_utc = COALESCE(NULLIF(excluded.publish_at_utc,''), {YOUTUBE_HISTORY_TABLE}.publish_at_utc),
          publish_at_jst = COALESCE(NULLIF(excluded.publish_at_jst,''), {YOUTUBE_HISTORY_TABLE}.publish_at_jst),
          source_table = COALESCE(NULLIF(excluded.source_table,''), {YOUTUBE_HISTORY_TABLE}.source_table),
          last_synced_at = excluded.last_synced_at
        """,
        tuple(params),
    )
    n = con.execute(f"SELECT COUNT(*) AS n FROM {YOUTUBE_HISTORY_TABLE}").fetchone()
    con.commit()
    return int(n["n"] or 0) if n else 0


def ensure_history_table(con: sqlite3.Connection) -> None:
    cur = con.execute("PRAGMA table_info(run_history)")
    rows = cur.fetchall()
    if not rows:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS run_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              steps TEXT NOT NULL,
              start_at TEXT NOT NULL,
              end_at TEXT NOT NULL,
              sec REAL NOT NULL,
              error TEXT
            )
            """
        )
        con.commit()
        return

    cols = {str(r[1]) for r in rows}
    expected = {"id", "steps", "start_at", "end_at", "sec", "error"}
    if cols == expected:
        return

    old = "run_history__old"
    con.execute(f"DROP TABLE IF EXISTS {old}")
    con.execute("ALTER TABLE run_history RENAME TO run_history__old")
    con.execute(
        """
        CREATE TABLE run_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          steps TEXT NOT NULL,
          start_at TEXT NOT NULL,
          end_at TEXT NOT NULL,
          sec REAL NOT NULL,
          error TEXT
        )
        """
    )
    old_cols = {str(r[1]) for r in con.execute(f"PRAGMA table_info({old})").fetchall()}
    steps_expr = _pick_expr(old_cols, ["steps"], "'create_video'")
    start_expr = _pick_expr(old_cols, ["start_at", "started_at"], "''")
    end_expr = _pick_expr(old_cols, ["end_at", "ended_at"], "''")
    sec_expr = _pick_expr(old_cols, ["sec", "duration_sec"], "0")
    err_expr = _pick_expr(old_cols, ["error", "last_error"], "NULL")
    con.execute(
        f"""
        INSERT INTO run_history (steps, start_at, end_at, sec, error)
        SELECT {steps_expr},
               {start_expr},
               {end_expr},
               ROUND(COALESCE({sec_expr}, 0), 2),
               {err_expr}
          FROM {old}
        """
    )
    con.execute(f"DROP TABLE {old}")
    con.commit()


def record_history(db_path: Path, h: HistoryRun) -> None:
    if not db_path.exists():
        return
    try:
        with connect(db_path) as con:
            ensure_history_table(con)
            con.execute(
                """
                INSERT INTO run_history (steps, start_at, end_at, sec, error)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    h.steps,
                    h.start_at,
                    h.end_at,
                    round(max(0.0, float(h.sec)), 2),
                    h.error or None,
                ),
            )
            con.commit()
    except Exception:
        pass


def build_post_title(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "").strip()).strip()
    if not s:
        return "タイトルなし"
    max_chars = POST_TITLE_MAX_CHARS if POST_TITLE_MAX_CHARS > 0 else 95
    return s[:max_chars]


def build_folder_title(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return "タイトルなし"
    return s[:10]


def _parse_steps(raw: str) -> List[str]:
    parts = [p.strip().lower() for p in (raw or "").split(",") if p.strip()]
    if not parts or "all" in parts:
        return ["pipeline"]
    return parts


def _pipeline_limit_tag(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s in ("all", "99"):
        return "99"
    if s in ("02", "03", "04", "05"):
        return s
    return "99"


def _stage_enabled(tag: str) -> bool:
    return PIPELINE_STAGE_ORDER.index(tag) <= PIPELINE_LIMIT_IDX


def _stage_tag_from_value(v: int) -> Optional[str]:
    if v == STA_02:
        return "02"
    if v == STA_03:
        return "03"
    if v == STA_04:
        return "04"
    if v == STA_05:
        return "05"
    if v == STA_99:
        return "99"
    return None


def pick_inprogress_job(
    con: sqlite3.Connection, target_ids: Optional[List[str]] = None
) -> Optional[sqlite3.Row]:
    stages_all = (STA_02, STA_03, STA_04, STA_05, STA_99)
    q = ",".join(["?"] * len(stages_all))
    id_filter_sql = ""
    params: List[object] = [int(x) for x in stages_all]
    if target_ids:
        ph = ",".join(["?"] * len(target_ids))
        id_filter_sql = f" AND id IN ({ph})"
        params.extend(target_ids)
    return con.execute(
        f"""
        SELECT id, stage
          FROM {ITEMS_DONE_TABLE}
         WHERE stage IN ({q})
           AND COALESCE(skip,0)=0
           AND COALESCE(video_created_at,'') = ''
           {id_filter_sql}
         ORDER BY stage DESC, id DESC
         LIMIT 1
        """,
        tuple(params),
    ).fetchone()


def enqueue_from_items_do(con: sqlite3.Connection, limit: int) -> List[str]:
    if limit <= 0:
        return []

    rows = con.execute(
        f"""
        SELECT d.id, d.skip, d.category, d.title, d.list_add_at
         FROM {ITEMS_DO_TABLE} d
         WHERE COALESCE(d.skip,0)=0
           AND NOT EXISTS (
               SELECT 1 FROM {ITEMS_DONE_TABLE} n WHERE n.id = d.id
           )
           AND NOT EXISTS (
               SELECT 1 FROM {YOUTUBE_HISTORY_TABLE} y WHERE y.id = d.id
           )
         ORDER BY COALESCE(d.hot_score,0) DESC,
                  d.list_add_at DESC,
                  d.id DESC
         LIMIT ?
        """,
        (int(limit),),
    ).fetchall()

    added_ids: List[str] = []
    for r in rows:
        stamp = now_folder_stamp()
        item_id = str(r["id"])
        title = str(r["title"] or "")
        post_title = build_post_title(title)
        folder_title = build_folder_title(title)
        folder_name = f"{stamp}_{item_id}_{folder_title}"
        url = f"https://girlschannel.net/topics/{item_id}/"
        cur = con.execute(
            f"""
            INSERT OR IGNORE INTO {ITEMS_DONE_TABLE} (
              id, skip, stage, category, post_title, keywords, url, list_add_at,
              folder_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                int(r["skip"] or 0),
                int(STA_02),
                str(r["category"] or ""),
                post_title,
                None,
                url,
                str(r["list_add_at"] or now_jst_str()),
                folder_name,
            ),
        )
        if cur.rowcount > 0:
            added_ids.append(item_id)
    con.commit()
    return added_ids


def run_script(script_path: Path, timeout: Optional[int], extra_args: Optional[List[str]] = None) -> None:
    if not script_path.exists():
        raise FileNotFoundError(f"script not found: {script_path}")

    cmd = [sys.executable, str(script_path)]
    if extra_args:
        cmd.extend(extra_args)

    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if cp.returncode != 0:
        out = (cp.stdout or "").strip()
        err = (cp.stderr or "").strip()
        snippet = "\n".join([x for x in [out, err] if x][-20:]) if (out or err) else "(no output)"
        raise RuntimeError(f"{script_path.name} failed (exit={cp.returncode})\n{snippet}")


def process_one_item(
    con: sqlite3.Connection, target_ids: Optional[List[str]] = None
) -> int:
    row = pick_inprogress_job(con, target_ids=target_ids)
    if row is None:
        return 0

    st = int(row["stage"])
    current_tag = _stage_tag_from_value(st)
    if current_tag is not None and not _stage_enabled(current_tag):
        return 0

    if st == STA_02 and _stage_enabled("02"):
        run_script(SCRIPT_02, TIMEOUT_02)
        return 0
    if st == STA_03 and _stage_enabled("03"):
        run_script(SCRIPT_03, TIMEOUT_03)
        return 0
    if st == STA_04 and _stage_enabled("04"):
        run_script(SCRIPT_04, TIMEOUT_04)
        return 0
    if st == STA_05 and _stage_enabled("05"):
        run_script(SCRIPT_05, TIMEOUT_05)
        return 0
    if st == STA_99 and _stage_enabled("99"):
        run_script(SCRIPT_99, TIMEOUT_99)
        return 0

    return 0


def count_completed_videos(con: sqlite3.Connection, target_ids: List[str]) -> int:
    if not target_ids:
        return 0
    ph = ",".join(["?"] * len(target_ids))
    row = con.execute(
        f"""
        SELECT COUNT(*) AS n
          FROM {ITEMS_DONE_TABLE}
         WHERE id IN ({ph})
           AND (
                 COALESCE(video_created_at,'') != ''
                 OR stage = ?
               )
        """,
        tuple(target_ids + [int(END_99)]),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def main() -> int:
    if not DB_PATH.exists():
        print(f"[ERROR] DB not found: {DB_PATH}", file=sys.stderr)
        return 2

    steps = _parse_steps(RUN_STEPS_RAW)
    limit_tag = _pipeline_limit_tag(PIPELINE_UNTIL_RAW)

    global PIPELINE_LIMIT_TAG, PIPELINE_LIMIT_IDX
    PIPELINE_LIMIT_TAG = limit_tag
    PIPELINE_LIMIT_IDX = PIPELINE_STAGE_ORDER.index(PIPELINE_LIMIT_TAG)

    target_video_count = int(RUNS_PIPELINE)
    if target_video_count <= 0:
        print("[ERROR] RUNS_DEFAULT は 1 以上にしてください", file=sys.stderr)
        return 2
    max_cycles = (
        int(MAX_PIPELINE_CYCLES)
        if int(MAX_PIPELINE_CYCLES) > 0
        else max(30, target_video_count * 30)
    )

    hist = HistoryRun(steps="create_video", start_at=now_jst_str())
    t0 = time.time()

    ok_count = 0
    err_count = 0

    try:
        with connect(DB_PATH) as con:
            ensure_tables(con)
            sync_youtube_history_from_items_done(con)
            if "pipeline" in steps:
                target_ids = enqueue_from_items_do(con, target_video_count)
                if not target_ids:
                    print("[INFO] enqueue対象がありません（items_doに新規候補なし）")
                    return 0

                pbar = tqdm(total=len(target_ids), desc="create_video", unit="video")
                done = count_completed_videos(con, target_ids)
                pbar.update(done)

                cycles = 0
                while done < len(target_ids):
                    if cycles >= max_cycles:
                        raise RuntimeError(
                            f"max_cycles超過: done={done}/{len(target_ids)} cycles={cycles}"
                        )
                    try:
                        rc = process_one_item(con, target_ids=target_ids)
                        if rc == 0:
                            ok_count += 1
                        else:
                            err_count += 1
                        if STOP_ON_ERROR and err_count > 0:
                            break
                    except Exception as e:
                        err_count += 1
                        pbar.write(f"[ERROR] {type(e).__name__}: {e}")
                        if STOP_ON_ERROR:
                            break
                    finally:
                        new_done = count_completed_videos(con, target_ids)
                        if new_done > done:
                            pbar.update(new_done - done)
                        done = new_done
                        cycles += 1
                    if SLEEP_SEC_WHEN_EMPTY > 0:
                        time.sleep(SLEEP_SEC_WHEN_EMPTY)
                pbar.close()

        return 0 if err_count == 0 else 1
    except Exception as e:
        hist.error = f"{type(e).__name__}: {e}"
        raise
    finally:
        hist.end_at = now_jst_str()
        hist.sec = max(0.0, time.time() - t0)
        record_history(DB_PATH, hist)


if __name__ == "__main__":
    raise SystemExit(main())
