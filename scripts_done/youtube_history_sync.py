#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

# Allow running from scripts_done/ directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import env_loader
from core.config import CFG as CORE_CFG

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


JST = ZoneInfo("Asia/Tokyo")
UTC = ZoneInfo("UTC")

# 履歴同期は読み取り専用で十分。
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

DB_PATH = CORE_CFG.DB_PATH
if not DB_PATH:
    raise SystemExit("DB_PATH が未設定です（girlsChannel.env を確認してください）")

YOUTUBE_HISTORY_TABLE = (
    env_loader.env_str("YOUTUBE_HISTORY_TABLE", "youtube_history").strip()
    or "youtube_history"
)
API_DIR = CORE_CFG.API_DIR or (CORE_CFG.BASE_OUTPUT_ROOT / "api")
CLIENT_SECRETS = API_DIR / CORE_CFG.CLIENT_JSON_NAME
TOKEN_FILE = API_DIR / CORE_CFG.TOKEN_NAME

TOPIC_URL_PAT = re.compile(r"girlschannel\.net/topics/(\d+)/?", re.IGNORECASE)


@dataclass
class VideoRow:
    video_id: str
    title: str
    description: str
    channel_id: str
    channel_title: str
    published_at_utc: str
    privacy_status: str
    view_count: Optional[int]
    topic_id: str


@dataclass
class VideoAnalytics:
    views: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None
    subscribers_gained: Optional[int] = None
    subscribers_lost: Optional[int] = None
    estimated_minutes_watched: Optional[int] = None
    average_view_duration: Optional[float] = None


def now_jst_str() -> str:
    return datetime.now(JST).strftime("%Y/%m/%d-%H:%M:%S")


def utc_to_jst_human(utc_text: str) -> str:
    s = (utc_text or "").strip()
    if not s:
        return ""
    try:
        dt_utc = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        return dt_utc.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def connect_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=60000;")
    return con


def ensure_history_table(con: sqlite3.Connection, table_name: str) -> None:
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
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
    add_cols = [
        ("youtube_title", "TEXT"),
        ("youtube_description", "TEXT"),
        ("youtube_channel_id", "TEXT"),
        ("youtube_channel_title", "TEXT"),
        ("youtube_privacy_status", "TEXT"),
        ("youtube_published_at_utc", "TEXT"),
        ("youtube_view_count", "INTEGER"),
        ("youtube_analytics_views", "INTEGER"),
        ("youtube_analytics_likes", "INTEGER"),
        ("youtube_analytics_comments", "INTEGER"),
        ("youtube_analytics_shares", "INTEGER"),
        ("youtube_analytics_subscribers_gained", "INTEGER"),
        ("youtube_analytics_subscribers_lost", "INTEGER"),
        ("youtube_analytics_estimated_minutes_watched", "INTEGER"),
        ("youtube_analytics_average_view_duration", "REAL"),
        ("youtube_analytics_last_fetched_at", "TEXT"),
    ]
    cols = {str(r[1]) for r in con.execute(f"PRAGMA table_info({table_name})").fetchall()}
    for name, ddl in add_cols:
        if name not in cols:
            con.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}")

    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table_name}_video_id ON {table_name}(youtube_video_id)"
    )
    con.commit()


def resolve_client_secrets_path(path: Path) -> Path:
    if path.exists():
        return path
    path_json = Path(str(path) + ".json")
    if path_json.exists():
        return path_json
    raise FileNotFoundError(f"client secrets not found: {path} (also tried {path_json})")


def get_authenticated_services(client_secrets_file: Path, token_file: Path) -> tuple[Any, Any]:
    client_secrets_file = resolve_client_secrets_path(client_secrets_file)
    creds: Optional[Credentials] = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        need_oauth = True
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                need_oauth = False
            except RefreshError as e:
                msg = str(e)
                if "invalid_scope" in msg and token_file.exists():
                    bak = token_file.with_name(f"{token_file.name}.invalid_scope.bak")
                    try:
                        token_file.replace(bak)
                    except Exception:
                        pass
                else:
                    raise

        if need_oauth:
            # Google may return previously granted extra scopes (e.g. youtube.upload).
            # Allow broader returned scopes as long as requested scopes are included.
            os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), SCOPES)
            oauth_kwargs: Dict[str, Any] = dict(
                port=0,
                open_browser=True,
                access_type="offline",
            )
            if not creds or not creds.refresh_token:
                oauth_kwargs["prompt"] = "consent"
            creds = flow.run_local_server(**oauth_kwargs)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    youtube = build("youtube", "v3", credentials=creds)
    youtube_analytics = build("youtubeAnalytics", "v2", credentials=creds)
    return youtube, youtube_analytics


def _extract_topic_id(title: str, description: str) -> str:
    text = f"{description}\n{title}"
    m = TOPIC_URL_PAT.search(text)
    return m.group(1) if m else ""


def fetch_uploads_playlist_info(youtube: Any) -> tuple[str, str, str]:
    resp = youtube.channels().list(part="contentDetails,snippet", mine=True, maxResults=1).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError("channels.list(mine=True) でチャンネル情報を取得できませんでした")
    item = items[0]
    uploads = (
        item.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads", "")
    )
    if not uploads:
        raise RuntimeError("uploads playlist id を取得できませんでした")
    channel_id = str(item.get("id", "") or "")
    channel_title = str(item.get("snippet", {}).get("title", "") or "")
    return uploads, channel_id, channel_title


def iter_upload_video_ids(youtube: Any, uploads_playlist_id: str, limit: int) -> Iterable[str]:
    fetched = 0
    page_token: Optional[str] = None
    while True:
        req = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token,
        )
        resp = req.execute()
        for it in resp.get("items", []):
            video_id = str(it.get("contentDetails", {}).get("videoId", "") or "").strip()
            if not video_id:
                continue
            yield video_id
            fetched += 1
            if limit > 0 and fetched >= limit:
                return
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


def fetch_video_rows(youtube: Any, video_ids: List[str]) -> List[VideoRow]:
    out: List[VideoRow] = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = (
            youtube.videos()
            .list(part="snippet,status,statistics", id=",".join(batch), maxResults=50)
            .execute()
        )
        for it in resp.get("items", []):
            snippet = it.get("snippet", {}) or {}
            status = it.get("status", {}) or {}
            statistics = it.get("statistics", {}) or {}
            vid = str(it.get("id", "") or "").strip()
            if not vid:
                continue
            title = str(snippet.get("title", "") or "")
            desc = str(snippet.get("description", "") or "")
            channel_id = str(snippet.get("channelId", "") or "")
            channel_title = str(snippet.get("channelTitle", "") or "")
            published_at_utc = str(snippet.get("publishedAt", "") or "")
            privacy_status = str(status.get("privacyStatus", "") or "")
            view_count = _to_int(statistics.get("viewCount"))
            topic_id = _extract_topic_id(title, desc)
            out.append(
                VideoRow(
                    video_id=vid,
                    title=title,
                    description=desc,
                    channel_id=channel_id,
                    channel_title=channel_title,
                    published_at_utc=published_at_utc,
                    privacy_status=privacy_status,
                    view_count=view_count,
                    topic_id=topic_id,
                )
            )
    return out


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def fetch_video_analytics(youtube_analytics: Any, video_ids: List[str]) -> Dict[str, VideoAnalytics]:
    if not video_ids:
        return {}

    out: Dict[str, VideoAnalytics] = {}
    metrics = ",".join(
        [
            "views",
            "likes",
            "comments",
            "shares",
            "subscribersGained",
            "subscribersLost",
            "estimatedMinutesWatched",
            "averageViewDuration",
        ]
    )
    end_date = datetime.now(UTC).date().isoformat()
    start_date = "2005-01-01"

    for i in range(0, len(video_ids), 100):
        batch = [v for v in video_ids[i : i + 100] if v]
        if not batch:
            continue
        resp = (
            youtube_analytics.reports()
            .query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                metrics=metrics,
                dimensions="video",
                filters=f"video=={','.join(batch)}",
                maxResults=200,
            )
            .execute()
        )
        headers = [str(h.get("name", "")) for h in resp.get("columnHeaders", [])]
        idx = {name: n for n, name in enumerate(headers)}

        for row in resp.get("rows", []) or []:
            vid = str(row[idx.get("video", -1)]) if "video" in idx else ""
            if not vid:
                continue
            out[vid] = VideoAnalytics(
                views=_to_int(row[idx["views"]]) if "views" in idx else None,
                likes=_to_int(row[idx["likes"]]) if "likes" in idx else None,
                comments=_to_int(row[idx["comments"]]) if "comments" in idx else None,
                shares=_to_int(row[idx["shares"]]) if "shares" in idx else None,
                subscribers_gained=(
                    _to_int(row[idx["subscribersGained"]]) if "subscribersGained" in idx else None
                ),
                subscribers_lost=(
                    _to_int(row[idx["subscribersLost"]]) if "subscribersLost" in idx else None
                ),
                estimated_minutes_watched=(
                    _to_int(row[idx["estimatedMinutesWatched"]])
                    if "estimatedMinutesWatched" in idx
                    else None
                ),
                average_view_duration=(
                    _to_float(row[idx["averageViewDuration"]]) if "averageViewDuration" in idx else None
                ),
            )
    return out


def upsert_history(
    con: sqlite3.Connection,
    table_name: str,
    videos: List[VideoRow],
    analytics_by_video_id: Dict[str, VideoAnalytics],
    source_table: str,
    unknown_prefix: str,
) -> Dict[str, int]:
    n_total = len(videos)
    n_saved = 0
    n_skipped = 0
    now = now_jst_str()

    for v in videos:
        analytics = analytics_by_video_id.get(v.video_id) or VideoAnalytics()
        row_id = v.topic_id or (f"{unknown_prefix}{v.video_id}" if unknown_prefix else v.video_id)
        if not row_id:
            n_skipped += 1
            continue
        views_value = analytics.views if analytics.views is not None else v.view_count

        con.execute(
            f"""
            INSERT INTO {table_name} (
              id,
              youtube_video_id,
              youtube_uploaded_at,
              upload_youtube_at,
              publish_at_utc,
              publish_at_jst,
              source_table,
              first_detected_at,
              last_synced_at,
              youtube_title,
              youtube_description,
              youtube_channel_id,
              youtube_channel_title,
              youtube_privacy_status,
              youtube_published_at_utc,
              youtube_view_count,
              youtube_analytics_views,
              youtube_analytics_likes,
              youtube_analytics_comments,
              youtube_analytics_shares,
              youtube_analytics_subscribers_gained,
              youtube_analytics_subscribers_lost,
              youtube_analytics_estimated_minutes_watched,
              youtube_analytics_average_view_duration,
              youtube_analytics_last_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              youtube_video_id = COALESCE(NULLIF(excluded.youtube_video_id,''), {table_name}.youtube_video_id),
              youtube_uploaded_at = COALESCE(NULLIF(excluded.youtube_uploaded_at,''), {table_name}.youtube_uploaded_at),
              upload_youtube_at = COALESCE(NULLIF(excluded.upload_youtube_at,''), {table_name}.upload_youtube_at),
              publish_at_utc = COALESCE(NULLIF(excluded.publish_at_utc,''), {table_name}.publish_at_utc),
              publish_at_jst = COALESCE(NULLIF(excluded.publish_at_jst,''), {table_name}.publish_at_jst),
              source_table = COALESCE(NULLIF(excluded.source_table,''), {table_name}.source_table),
              last_synced_at = excluded.last_synced_at,
              youtube_title = COALESCE(NULLIF(excluded.youtube_title,''), {table_name}.youtube_title),
              youtube_description = COALESCE(NULLIF(excluded.youtube_description,''), {table_name}.youtube_description),
              youtube_channel_id = COALESCE(NULLIF(excluded.youtube_channel_id,''), {table_name}.youtube_channel_id),
              youtube_channel_title = COALESCE(NULLIF(excluded.youtube_channel_title,''), {table_name}.youtube_channel_title),
              youtube_privacy_status = COALESCE(NULLIF(excluded.youtube_privacy_status,''), {table_name}.youtube_privacy_status),
              youtube_published_at_utc = COALESCE(NULLIF(excluded.youtube_published_at_utc,''), {table_name}.youtube_published_at_utc),
              youtube_view_count = COALESCE(excluded.youtube_view_count, {table_name}.youtube_view_count),
              youtube_analytics_views = COALESCE(excluded.youtube_analytics_views, {table_name}.youtube_analytics_views),
              youtube_analytics_likes = COALESCE(excluded.youtube_analytics_likes, {table_name}.youtube_analytics_likes),
              youtube_analytics_comments = COALESCE(excluded.youtube_analytics_comments, {table_name}.youtube_analytics_comments),
              youtube_analytics_shares = COALESCE(excluded.youtube_analytics_shares, {table_name}.youtube_analytics_shares),
              youtube_analytics_subscribers_gained = COALESCE(excluded.youtube_analytics_subscribers_gained, {table_name}.youtube_analytics_subscribers_gained),
              youtube_analytics_subscribers_lost = COALESCE(excluded.youtube_analytics_subscribers_lost, {table_name}.youtube_analytics_subscribers_lost),
              youtube_analytics_estimated_minutes_watched = COALESCE(excluded.youtube_analytics_estimated_minutes_watched, {table_name}.youtube_analytics_estimated_minutes_watched),
              youtube_analytics_average_view_duration = COALESCE(excluded.youtube_analytics_average_view_duration, {table_name}.youtube_analytics_average_view_duration),
              youtube_analytics_last_fetched_at = COALESCE(NULLIF(excluded.youtube_analytics_last_fetched_at,''), {table_name}.youtube_analytics_last_fetched_at)
            """,
            (
                row_id,
                v.video_id,
                now,
                now,
                v.published_at_utc,
                utc_to_jst_human(v.published_at_utc),
                source_table,
                now,
                now,
                v.title,
                v.description,
                v.channel_id,
                v.channel_title,
                v.privacy_status,
                v.published_at_utc,
                views_value,
                analytics.views,
                analytics.likes,
                analytics.comments,
                analytics.shares,
                analytics.subscribers_gained,
                analytics.subscribers_lost,
                analytics.estimated_minutes_watched,
                analytics.average_view_duration,
                now if v.video_id in analytics_by_video_id else "",
            ),
        )
        n_saved += 1

    con.commit()
    return {"total": n_total, "saved": n_saved, "skipped_no_topic_id": n_skipped}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="YouTube channel の動画情報を取得して youtube_history へ同期する"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="取得する動画数の上限（0以下なら全件）",
    )
    parser.add_argument(
        "--table",
        type=str,
        default=YOUTUBE_HISTORY_TABLE,
        help="同期先テーブル名",
    )
    parser.add_argument(
        "--unknown-id-prefix",
        type=str,
        default="yt_",
        help="topic id を抽出できない動画も保存する場合のID接頭辞（例: yt_）",
    )
    args = parser.parse_args()

    print(f"[CONF] DB_PATH={DB_PATH}")
    print(f"[CONF] TABLE={args.table}")
    print(f"[CONF] LIMIT={args.limit}")
    print(f"[CONF] CLIENT={CLIENT_SECRETS}")
    print(f"[CONF] TOKEN={TOKEN_FILE}")

    youtube, youtube_analytics = get_authenticated_services(CLIENT_SECRETS, TOKEN_FILE)
    uploads_playlist_id, channel_id, channel_title = fetch_uploads_playlist_info(youtube)
    print(f"[INFO] channel={channel_title} ({channel_id})")
    print(f"[INFO] uploads_playlist_id={uploads_playlist_id}")

    video_ids = list(iter_upload_video_ids(youtube, uploads_playlist_id, int(args.limit)))
    if not video_ids:
        print("[INFO] 対象動画が見つかりませんでした")
        return 0
    print(f"[INFO] fetched video_ids={len(video_ids)}")

    videos = fetch_video_rows(youtube, video_ids)
    print(f"[INFO] fetched video_rows={len(videos)}")
    analytics_by_video_id: Dict[str, VideoAnalytics] = {}
    try:
        analytics_by_video_id = fetch_video_analytics(youtube_analytics, video_ids)
        print(f"[INFO] fetched analytics_rows={len(analytics_by_video_id)}")
    except Exception as e:
        print(f"[WARN] analytics fetch failed: {e}")

    with connect_db(DB_PATH) as con:
        ensure_history_table(con, args.table)
        stats = upsert_history(
            con=con,
            table_name=args.table,
            videos=videos,
            analytics_by_video_id=analytics_by_video_id,
            source_table="youtube_api_sync",
            unknown_prefix=args.unknown_id_prefix.strip(),
        )

    print(
        "[DONE] total={total} saved={saved} skipped_no_topic_id={skipped_no_topic_id}".format(
            **stats
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
