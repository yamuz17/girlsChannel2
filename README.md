# girlsChannel2

ガールズチャンネルのトピック取得 → 画像生成 → 音声生成 → サムネ/preview → 動画組み立てを、
SQLite のキューで自動実行するスクリプト群です。

## セットアップ
```bash
./scripts/setup.sh
```

## 実行方法（例）
```bash
source .venv/bin/activate
python scripts_done/build_list.py
python scripts_done/run_pipeline.py --steps list,pipeline --runs 1 --until 99
python scripts_parts/post_upload.py --limit 1
```

## 注意
- 生成物（動画・音声・画像）、DB、ログ、`.env`、トークン/認証情報は Git 管理しません。
- `ENGINE_URL` は `girlsChannel.env` で指定してください。

## 検証中
- `script_test/build_list.py` にて新スキーマ検証中
- テーブル構成: 単一テーブル（検証中、他スクリプトは未対応）
- カラム: `id, skip, stage, category, title, post_title, keywords, first_post_at, last_post_at, comments_count, url, hot_score_h, hot_score_d, list_add_at, video_created_at, upload_youtube_at, upload_tiktok_at`
- 時刻形式: `yyyy/mm/dd-hh:mm:ss`
- `skip`: 禁止ワードまたは手動で `1`（処理対象外）
- `stage`: 検証中のステージ案（0/10/20/30/40/50/60/70/80/90）
- カテゴリ可変: `CATEGORIES_JSON` / `CATEGORIES_CSV`
- “今何が熱いか” 指標: `first_post_at` 取得 + `hot_score_h` / `hot_score_d`
- 禁止ワード検出: タイトルに該当時 `skip=1`（保存は継続）
- テストDB: `DB_PATH_TEST`（既定は本番DB名 + `Test`）

## CI（簡易）
```bash
make ci
```
