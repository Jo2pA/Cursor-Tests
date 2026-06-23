# News Digest

シリコン/オープンソースEDA系ニュースの日次自動ダイジェスト。
**GitHub Actions の cron が発火源**なので、アシスタントがオンラインかどうかと
無関係に毎朝回る。状態（既読ID）は `manifest.json` にコミットされて永続化される。

## 構成

| ファイル | 役割 |
| --- | --- |
| `config.yaml` | ソース定義（フィードURL / 取得方式） |
| `digest.py` | 取得・差分検知・整形・メール送信 |
| `manifest.json` | 既読IDの状態（自動コミットされる。手で編集しない） |
| `requirements.txt` | 依存 |
| `../.github/workflows/digest.yml` | cron（07:00 JST）と手動実行 |

## ソースと取得方式

- **RSS/Atom**: Zero to ASIC / Electronics Weekly / Hackster.io / FOSSi(GitHub org Atom)
- **HTML表スクレイプ**: Tiny Tapeout シャトル一覧（RSSがないため表の差分を検知。
  行テキスト全体をハッシュ化するので、新規シャトル追加だけでなくステータス変化も拾う）

`config.yaml` の `verified: false` は未裏取りのフィードURL。初回の dry-run ログ
（`[ ok ] / [FAIL]`）でどのフィードが解決したか確認し、必要なら差し替える。

## セットアップ

### 1. メール送信用シークレットを登録

リポジトリの Settings → Secrets and variables → Actions に以下を登録:

| Secret | 例 | 必須 |
| --- | --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` | ✓ |
| `SMTP_PORT` | `587` | 任意（既定587） |
| `SMTP_USERNAME` | `you@gmail.com` | ✓ |
| `SMTP_PASSWORD` | アプリパスワード | ✓ |
| `MAIL_FROM` | `you@gmail.com` | 任意（既定=USERNAME） |
| `MAIL_TO` | `a@x.com,b@y.com` | ✓（カンマ区切り可） |

> Gmail を使う場合は2段階認証＋「アプリパスワード」を発行して `SMTP_PASSWORD` に。

### 2. ベースライン作成（初回の巨大メールを防ぐ）

Actions → News Digest → Run workflow → mode = **init** を1回実行。
現在の全項目が「既読」として記録され、翌朝以降は**新着のみ**が届く。

### 3. 動作確認

mode = **dry-run** を実行するとメールを送らずに、解決したフィードと
検出した新着が Actions ログに出る。

## ローカル実行

```bash
cd news_digest
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python digest.py --dry-run      # 送信せず標準出力に
python digest.py --init         # 既読ベースライン作成
python digest.py                # 新着をメール送信（要シークレット環境変数）
```

## チューニング

- ソース追加: `config.yaml` の `sources:` に1ブロック足すだけ（`type: rss` か `html_table`）。
- 配信時刻: `digest.yml` の cron を変更（UTC 指定）。
- 1通あたりの最大件数: `config.yaml` の `mail.max_items`。
