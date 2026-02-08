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

## CI（簡易）
```bash
make ci
```
