#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Build List (single-table schema) for scripts_done.
- one table only
- hotness metric (comment velocity per day)
- categories configurable via env JSON/CSV
"""

from __future__ import annotations
import json
import re
import sys
import time
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Tuple
from zoneinfo import ZoneInfo

from tqdm import tqdm
from dateutil import parser as dtparser
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# Allow running from scripts_done/ directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import CFG
from core.env_loader import env_str

# =========================================================
# 設定（ここだけ変えればOK）
# =========================================================
DB_PATH = CFG.DB_PATH
if not DB_PATH:
    raise SystemExit("DB_PATH が未設定です（girlsChannel.env を確認）")

TABLE_NAME = CFG.ITEMS_DO_TABLE or "items_do"

PAGE_FROM = int(env_str("PAGE_FROM", "1"))
PAGE_TO = int(env_str("PAGE_TO", "15"))

TARGET_NEW_COUNT = int(env_str("TARGET_NEW_COUNT", "800"))
MIN_COMMENTS = int(env_str("MIN_COMMENTS", "800"))
UPDATE_EXISTING = env_str("UPDATE_EXISTING", "false").lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)

HEADLESS = env_str("HEADLESS", "true").lower() in ("1", "true", "yes", "y", "on")
SLEEP_SEC = float(env_str("SLEEP_SEC", "0.6"))
TIMEOUT_MS = int(env_str("TIMEOUT_MS", "30000"))

ECHO_EACH_SAVE = env_str("ECHO_EACH_SAVE", "false").lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)
EARLY_STOP_PAGES = int(env_str("EARLY_STOP_PAGES", "2"))

ENABLE_POST_TITLE = env_str("ENABLE_POST_TITLE", "true").lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)

ENABLE_FIRST_POST = env_str("ENABLE_FIRST_POST", "true").lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)

HOTNESS_ENABLE = env_str("HOTNESS_ENABLE", "true").lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)
HOT_SCORE_CATEGORY_MULTIPLIER_DEFAULT = 1.0
HOT_SCORE_CATEGORY_MULTIPLIERS: Dict[str, float] = {
    "ゴシップ": 1.2,
}

DETAIL_TIMEOUT_MS = int(env_str("DETAIL_TIMEOUT_MS", "12000"))
DETAIL_TEXT_TIMEOUT_MS = int(env_str("DETAIL_TEXT_TIMEOUT_MS", "3000"))
DETAIL_SLEEP_SEC = float(env_str("DETAIL_SLEEP_SEC", "0.05"))

ENABLE_EXCLUDE_BADWORDS = env_str("ENABLE_EXCLUDE_BADWORDS", "true").lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)
BADWORDS_NORMALIZE = env_str("BADWORDS_NORMALIZE", "true").lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)
BADWORDS_DEFAULT = [
    "殺",
    "死",
    "亡",
    "自殺",
    "他殺",
    "事故死",
    "ころす",
    "殺す",
    "死ね",
    "氏ね",
    "しぬ",
    "ﾀﾋ",
    "タヒ",
    "レイプ",
    "ﾚｲﾌﾟ",
    "売春",
    "Part",
    "PART",
    "語ろう",
    "語りたい",
    "語りましょう",
    "アンチ厳禁",
    "ファントピ",
    "トピ",
    "結婚を発表",
    "妊娠",
    "ガルちゃん",
    "ｶﾞﾙ",
    "一周忌",
    "と思う芸能人",
    "と思う有名人",
    "地震",
]
_bw_csv = env_str("BADWORDS_CSV", "").strip()
BADWORDS = (
    [w.strip() for w in _bw_csv.split(",") if w.strip()]
    if _bw_csv
    else BADWORDS_DEFAULT
)


@dataclass(frozen=True)
class CategoryConfig:
    name: str
    base_url: str
    params: str


def _parse_categories() -> List[CategoryConfig]:
    raw_json = env_str("CATEGORIES_JSON", "").strip()
    if raw_json:
        try:
            data = json.loads(raw_json)
            out: List[CategoryConfig] = []
            for row in data:
                out.append(
                    CategoryConfig(
                        name=str(row["name"]),
                        base_url=str(row["base_url"]),
                        params=str(row.get("params", "")),
                    )
                )
            if out:
                return out
        except Exception:
            pass

    raw_csv = env_str("CATEGORIES_CSV", "").strip()
    if raw_csv:
        out: List[CategoryConfig] = []
        for part in raw_csv.split(";"):
            part = part.strip()
            if not part:
                continue
            pieces = [p.strip() for p in part.split("|")]
            if len(pieces) < 2:
                continue
            name = pieces[0]
            base_url = pieces[1]
            params = pieces[2] if len(pieces) >= 3 else ""
            out.append(CategoryConfig(name=name, base_url=base_url, params=params))
        if out:
            return out

    return [
        CategoryConfig(
            name="ゴシップ",
            base_url="https://girlschannel.net/topics/category/gossip",
            params="?sort=comment&date=m",
        ),
        CategoryConfig(
            name="ニュース",
            base_url="https://girlschannel.net/topics/category/news",
            params="?sort=comment&date=m",
        ),
        # CategoryConfig(
        #     name="政治経済",
        #     base_url="https://girlschannel.net/topics/category/politics",
        #     params="?sort=comment&date=m",
        # ),
    ]


CATEGORIES: List[CategoryConfig] = _parse_categories()

# =========================================================
# DDL (single table)
# =========================================================
DDL_ITEMS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
  id TEXT PRIMARY KEY,
  skip INTEGER NOT NULL DEFAULT 0,
  hot_score REAL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  comments_count INTEGER NOT NULL,
  first_post_at TEXT,
  last_post_at TEXT NOT NULL,
  list_add_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_skip ON {TABLE_NAME}(skip);
CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_category ON {TABLE_NAME}(category);
CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_last_post_at ON {TABLE_NAME}(last_post_at);
CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_comments ON {TABLE_NAME}(comments_count);
CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_hot ON {TABLE_NAME}(hot_score DESC, list_add_at DESC);
"""

RE_TOPIC_HREF = re.compile(r"/topics/(\d+)/")
RE_FIRSTPOST_ANY = re.compile(r"(\d{4})/(\d{2})/(\d{2}).*?(\d{2}):(\d{2}):(\d{2})")


def now_jst_str() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d-%H:%M:%S")


def _pick_expr(cols: set[str], candidates: List[str], default_expr: str) -> str:
    for c in candidates:
        if c in cols:
            return c
    return default_expr


def ensure_run_history_table(con: sqlite3.Connection) -> None:
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
    con.execute(f"ALTER TABLE run_history RENAME TO {old}")
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
    steps_expr = _pick_expr(old_cols, ["steps"], "'build_list'")
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


def record_run_history(
    db_path: Path, *, steps: str, start_at: str, end_at: str, sec: float, error: str
) -> None:
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db_path), timeout=30)
        try:
            ensure_run_history_table(con)
            con.execute(
                """
                INSERT INTO run_history (steps, start_at, end_at, sec, error)
                VALUES (?, ?, ?, ?, ?)
                """,
                (steps, start_at, end_at, round(max(0.0, float(sec)), 2), error or None),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _ensure_items_do_column_order(con: sqlite3.Connection) -> None:
    expected = [
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
    cur = con.execute(f"PRAGMA table_info({TABLE_NAME})")
    cols = [str(r[1]) for r in cur.fetchall()]
    if cols == expected:
        return
    if not cols:
        return

    tmp = f"{TABLE_NAME}__rebuild"
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
    common = [c for c in expected if c in cols]
    if common:
        sel = ", ".join(common)
        con.execute(f"INSERT OR REPLACE INTO {tmp} ({sel}) SELECT {sel} FROM {TABLE_NAME}")
    con.execute(f"DROP TABLE {TABLE_NAME}")
    con.execute(f"ALTER TABLE {tmp} RENAME TO {TABLE_NAME}")
    con.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_skip ON {TABLE_NAME}(skip)")
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_category ON {TABLE_NAME}(category)"
    )
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_last_post_at ON {TABLE_NAME}(last_post_at)"
    )
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_comments ON {TABLE_NAME}(comments_count)"
    )
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_hot ON {TABLE_NAME}(hot_score DESC, list_add_at DESC)"
    )


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.executescript(DDL_ITEMS)
    _ensure_items_do_column_order(con)
    con.create_function(
        "category_multiplier",
        1,
        lambda category: float(
            HOT_SCORE_CATEGORY_MULTIPLIERS.get(
                str(category or ""), HOT_SCORE_CATEGORY_MULTIPLIER_DEFAULT
            )
        ),
    )
    con.commit()
    return con


def exists_id(con: sqlite3.Connection, tid: str) -> bool:
    row = con.execute(
        f"SELECT 1 FROM {TABLE_NAME} WHERE id=? LIMIT 1", (tid,)
    ).fetchone()
    return row is not None


def _normalize_for_badword_check(s: str) -> str:
    t = s or ""
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\s+", "", t)
    return t.lower()


def contains_badword(text: str) -> bool:
    if not ENABLE_EXCLUDE_BADWORDS:
        return False
    t = _normalize_for_badword_check(text) if BADWORDS_NORMALIZE else (text or "")
    for w in BADWORDS:
        ww = _normalize_for_badword_check(w) if BADWORDS_NORMALIZE else w
        if ww and ww in t:
            return True
    return False


def upsert_item(con: sqlite3.Connection, row: Dict[str, Any]) -> None:
    sql = f"""
    INSERT INTO {TABLE_NAME} (
      id, skip, hot_score, category, title, comments_count,
      first_post_at, last_post_at, list_add_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
      skip = CASE WHEN {TABLE_NAME}.skip = 1 THEN 1 ELSE excluded.skip END,
      category=excluded.category,
      title=excluded.title,
      first_post_at=COALESCE({TABLE_NAME}.first_post_at, excluded.first_post_at),
      last_post_at=excluded.last_post_at,
      comments_count=excluded.comments_count,
      hot_score=excluded.hot_score
    """
    con.execute(
        sql,
        (
            row["id"],
            int(row.get("skip", 0)),
            row.get("hot_score"),
            row["category"],
            row["title"],
            int(row["comments_count"]),
            row.get("first_post_at"),
            row["last_post_at"],
            row["list_add_at"],
        ),
    )


def digits_only_int(s: str) -> int:
    nums = re.findall(r"\d+", (s or "").replace(",", ""))
    return int("".join(nums)) if nums else 0


def normalize_list_datetime(raw: str) -> str:
    txt = (raw or "").strip()
    if not txt:
        return "1970/01/01-00:00:00"
    try:
        dt = dtparser.parse(txt, fuzzy=True)
        return dt.strftime("%Y/%m/%d-%H:%M:%S")
    except Exception:
        return txt


def parse_first_post_from_text(body_text: str) -> str | None:
    t = (body_text or "").replace("\r\n", "\n").replace("\r", "\n")
    m = RE_FIRSTPOST_ANY.search(t)
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    return f"{y}/{mo}/{d}-{hh}:{mm}:{ss}"


def fetch_first_post_via_comment1(detail_page, topic_id: str) -> str | None:
    url = f"https://girlschannel.net/comment/{topic_id}/1/"
    try:
        resp = detail_page.goto(url, wait_until="domcontentloaded", timeout=DETAIL_TIMEOUT_MS)
        status = resp.status if resp else None
        if not resp or (status and status >= 400):
            return None
    except PWTimeoutError:
        return None

    try:
        body_txt = detail_page.locator("body").inner_text(timeout=DETAIL_TEXT_TIMEOUT_MS)
    except Exception:
        return None

    return parse_first_post_from_text(body_txt)


def recompute_hot_scores(con: sqlite3.Connection) -> None:
    con.execute(
        f"""
        UPDATE {TABLE_NAME}
           SET
             hot_score =
               CASE
                 WHEN first_post_at IS NULL OR first_post_at='' THEN NULL
                 WHEN last_post_at  IS NULL OR last_post_at ='' THEN NULL
                 ELSE
                   CASE
                     WHEN (
                       (julianday(date(REPLACE(substr(last_post_at,1,10),'/','-'))) -
                        julianday(date(REPLACE(substr(first_post_at,1,10),'/','-')))) + 1
                     ) <= 0 THEN NULL
                     ELSE ROUND(
                       (
                         (comments_count * 1.0) /
                         ((julianday(date(REPLACE(substr(last_post_at,1,10),'/','-'))) -
                           julianday(date(REPLACE(substr(first_post_at,1,10),'/','-')))) + 1)
                       ) * category_multiplier(category),
                       2
                     )
                   END
               END
        """
    )
    con.commit()


def build_page_url(cfg: CategoryConfig, page_no: int) -> str:
    return f"{cfg.base_url}/{page_no}/" + (cfg.params or "")


def build_post_title(title: str) -> str:
    raw = (title or "").strip()
    core = re.sub(r"【[^】]{1,30}】", "", raw).strip()
    core = re.sub(r"\b(PART|Part|part)\s*\d+\b", "", core).strip()
    core = re.sub(r"[#＃]\s*\d+\b", "", core).strip()
    core = re.sub(r"(第\s*\d+\s*(回|弾))", "", core).strip()
    core = re.sub(r"(パート|Part|PART)\s*\d+\s*$", "", core).strip()
    core = re.sub(r"\s{2,}", " ", core).strip(" 　-–—_:：")
    return core if core else raw


def main() -> int:
    run_start = now_jst_str()
    t0 = time.time()
    run_error = ""
    try:
        if TARGET_NEW_COUNT <= 0:
            raise SystemExit("TARGET_NEW_COUNT は 1以上にしてください")
        if PAGE_FROM <= 0 or PAGE_TO <= 0 or PAGE_TO < PAGE_FROM:
            raise SystemExit("PAGE_FROM/PAGE_TO の指定が不正です")
        if MIN_COMMENTS < 0:
            raise SystemExit("MIN_COMMENTS は 0以上にしてください")
        if not CATEGORIES:
            raise SystemExit("CATEGORIES が空です")

        con = connect(DB_PATH)

        saved = 0
        pages_done = 0
        seen = 0
        skipped_exists = 0
        skipped_under_min = 0
        skipped_badword = 0
        failed_item = 0
        failed_page = 0

        new_ids: List[str] = []
        first_post_filled = 0
        first_post_failed = 0

        print(f"[INFO] DB: {DB_PATH}")
        print(f"[INFO] table: {TABLE_NAME}")
        print(f"[INFO] page_from..to: {PAGE_FROM}..{PAGE_TO}")
        print(f"[INFO] target_save(total): {TARGET_NEW_COUNT}")
        print(f"[INFO] min_comments: {MIN_COMMENTS}")
        print(f"[INFO] update_existing: {UPDATE_EXISTING}")
        print(f"[INFO] categories: {', '.join([c.name for c in CATEGORIES])}")
        for c in CATEGORIES:
            print(f"  - {c.name}: {c.base_url}/?{(c.params or '').lstrip('?')}")
        print(f"[INFO] early_stop_pages(per_category): {EARLY_STOP_PAGES}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            context = browser.new_context(locale="ja-JP")
            page = context.new_page()
            detail_page = context.new_page()

            try:
                pbar = tqdm(total=TARGET_NEW_COUNT, desc="saved")

                for cfg in CATEGORIES:
                    if saved >= TARGET_NEW_COUNT:
                        break

                    print(
                        f"\n[CATEGORY] {cfg.name}  base={cfg.base_url}  params={cfg.params}"
                    )
                    consecutive_no_save_pages = 0

                    for page_no in range(PAGE_FROM, PAGE_TO + 1):
                        if saved >= TARGET_NEW_COUNT:
                            break

                        pages_done += 1
                        url = build_page_url(cfg, page_no)

                        try:
                            resp = page.goto(
                                url, wait_until="domcontentloaded", timeout=TIMEOUT_MS
                            )
                            status = resp.status if resp else None
                            if (not resp) or (status and status >= 400):
                                failed_page += 1
                                print(
                                    f"[PAGE_FAIL] cat={cfg.name} page={page_no} status={status} url={url}"
                                )
                                time.sleep(SLEEP_SEC)
                                continue
                        except PWTimeoutError:
                            failed_page += 1
                            print(
                                f"[PAGE_TIMEOUT] cat={cfg.name} page={page_no} url={url}"
                            )
                            time.sleep(SLEEP_SEC)
                            continue

                        li_locator = page.locator(
                            "xpath=/html/body/div[1]/div[1]/div[1]/ul[2]/li"
                        )
                        li_count = li_locator.count()
                        if li_count == 0:
                            print(f"[NO_ITEMS] cat={cfg.name} page={page_no} url={url}")
                            break

                        page_rows: List[Tuple[int, Dict[str, Any], int]] = []

                        for idx in range(1, li_count + 1):
                            seen += 1

                            a = page.locator(
                                f"xpath=/html/body/div[1]/div[1]/div[1]/ul[2]/li[{idx}]/a"
                            ).first
                            href = a.get_attribute("href") or ""
                            m = RE_TOPIC_HREF.search(href)
                            if not m:
                                failed_item += 1
                                continue
                            tid = m.group(1)

                            try:
                                comments_raw = (
                                    page.locator(
                                        f"xpath=/html/body/div[1]/div[1]/div[1]/ul[2]/li[{idx}]/a/div/p/span[2]"
                                    )
                                    .first.inner_text(timeout=5000)
                                    .strip()
                                )
                                post_raw = (
                                    page.locator(
                                        f"xpath=/html/body/div[1]/div[1]/div[1]/ul[2]/li[{idx}]/a/div/p/span[3]"
                                    )
                                    .first.inner_text(timeout=5000)
                                    .strip()
                                )
                                title = (
                                    page.locator(
                                        f"xpath=/html/body/div[1]/div[1]/div[1]/ul[2]/li[{idx}]/a/p"
                                    )
                                    .first.inner_text(timeout=5000)
                                    .strip()
                                )
                            except PWTimeoutError:
                                failed_item += 1
                                continue

                            comments_count = digits_only_int(comments_raw)
                            if comments_count < MIN_COMMENTS:
                                skipped_under_min += 1
                                continue

                            badword_flag = 1 if contains_badword(title) else 0
                            if badword_flag:
                                skipped_badword += 1

                            if (not UPDATE_EXISTING) and exists_id(con, tid):
                                skipped_exists += 1
                                continue

                            last_post_at = normalize_list_datetime(post_raw)
                            now_ts = now_jst_str()
                            row: Dict[str, Any] = {
                                "id": tid,
                                "skip": badword_flag,
                                "hot_score": None,
                                "category": cfg.name,
                                "title": title,
                                "comments_count": comments_count,
                                "first_post_at": None,
                                "last_post_at": last_post_at,
                                "list_add_at": now_ts,
                            }

                            page_rows.append((idx, row, badword_flag))

                        page_rows.sort(
                            key=lambda t: (
                                -int(t[1]["comments_count"]),
                                str(t[1]["last_post_at"]),
                            )
                        )

                        page_saved = 0
                        for orig_idx, row, badword_flag in page_rows:
                            if saved >= TARGET_NEW_COUNT:
                                break

                            existed = exists_id(con, row["id"])
                            upsert_item(con, row)
                            con.commit()

                            if not existed:
                                new_ids.append(row["id"])

                            saved += 1
                            page_saved += 1
                            pbar.update(1)

                            if ECHO_EACH_SAVE:
                                suffix = " skip=1" if badword_flag else ""
                                print(
                                    f"[OK] cat={cfg.name} page={page_no} li={orig_idx} saved={saved} id={row['id']} "
                                    f"post={row['last_post_at']} c={row['comments_count']} "
                                    f"title={row['title'][:60]}{suffix}"
                                )

                        if page_saved == 0:
                            consecutive_no_save_pages += 1
                            print(
                                f"[NO_SAVE] cat={cfg.name} page={page_no} consecutive={consecutive_no_save_pages} "
                                f"(under_min_total={skipped_under_min}, exists_total={skipped_exists}, failed_total={failed_item})"
                            )
                            if (
                                EARLY_STOP_PAGES > 0
                                and consecutive_no_save_pages >= EARLY_STOP_PAGES
                            ):
                                print(
                                    "[EARLY_STOP] no saved items for consecutive pages (this category) -> stop this category"
                                )
                                break
                        else:
                            consecutive_no_save_pages = 0

                        time.sleep(SLEEP_SEC)

                pbar.close()

                if ENABLE_FIRST_POST and new_ids:
                    print("\n[STEP] fetch first_post (newly inserted only)")
                    for tid in new_ids:
                        fp = fetch_first_post_via_comment1(detail_page, tid)
                        if fp:
                            con.execute(
                                f"UPDATE {TABLE_NAME} SET first_post_at = COALESCE(first_post_at, ?) WHERE id=?",
                                (fp, tid),
                            )
                            con.commit()
                            first_post_filled += 1
                        else:
                            first_post_failed += 1
                        if DETAIL_SLEEP_SEC > 0:
                            time.sleep(DETAIL_SLEEP_SEC)

                if HOTNESS_ENABLE:
                    print("\n[STEP] recompute hotness")
                    recompute_hot_scores(con)
            finally:
                con.close()
                context.close()
                browser.close()

        print("\n[SUMMARY]")
        print(f"  saved={saved} target={TARGET_NEW_COUNT}")
        print(
            f"  pages_done={pages_done} range={PAGE_FROM}..{PAGE_TO}  categories={len(CATEGORIES)}"
        )
        print(
            f"  seen={seen} under_min={skipped_under_min} skipped_exists={skipped_exists} failed_item={failed_item} failed_page={failed_page}"
        )
        print(f"  skipped_badword={skipped_badword}")
        if ENABLE_FIRST_POST:
            print(f"  first_post filled={first_post_filled} failed={first_post_failed}")
        return 0
    except Exception as e:
        run_error = f"{type(e).__name__}: {e}"
        raise
    finally:
        record_run_history(
            DB_PATH,
            steps="build_list",
            start_at=run_start,
            end_at=now_jst_str(),
            sec=time.time() - t0,
            error=run_error,
        )


if __name__ == "__main__":
    raise SystemExit(main())
