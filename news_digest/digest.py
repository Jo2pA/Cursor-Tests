#!/usr/bin/env python3
"""Silicon news digest.

冪等な日次ダイジェスト生成器。各ソース（RSS / HTML表スクレイプ）を取得し、
manifest.json に記録済みの項目と差分を取り、新着だけをメール配信する。

モード:
  (default)     新着を検出 → メール送信 → manifest 更新（commit 対象）
  --dry-run     新着を検出 → 標準出力に表示。メール送信せず manifest も更新しない
  --init        現在の全項目を「既読」として manifest に記録（配信はしない）。
                初回に巨大なメールが飛ぶのを防ぐベースライン作成用。

manifest.json がリポジトリにコミットされることで、実行間の状態（既読ID）が
GitHub 上に永続化され、ワークフローが私（アシスタント）に依存せず冪等に回る。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.yaml"
MANIFEST_PATH = HERE / "manifest.json"

# サイトによってはボットを 403 で弾くため、ブラウザ風の UA を名乗る。
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 SiliconDigest/1.0"
)
REQUEST_TIMEOUT = 30
# manifest 1ソースあたりに保持する既読IDの上限（無制限肥大化を防ぐ）。
MAX_SEEN_PER_SOURCE = 1000


@dataclass
class Item:
    source_id: str
    source_name: str
    uid: str
    title: str
    url: str
    summary: str = ""
    published: str = ""


@dataclass
class FetchResult:
    source_id: str
    source_name: str
    items: list[Item] = field(default_factory=list)
    error: str | None = None


def _http_get(url: str) -> requests.Response:
    resp = requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    return resp


def _hash(*parts: str) -> str:
    return hashlib.sha256(" ".join(parts).encode("utf-8")).hexdigest()[:16]


def fetch_rss(source: dict) -> FetchResult:
    sid, name, url = source["id"], source["name"], source["url"]
    result = FetchResult(sid, name)
    try:
        raw = _http_get(url).content
        feed = feedparser.parse(raw)
        if feed.bozo and not feed.entries:
            raise ValueError(f"feed parse error: {feed.bozo_exception!r}")
        for entry in feed.entries:
            link = entry.get("link", url)
            uid = entry.get("id") or link or _hash(sid, entry.get("title", ""))
            result.items.append(
                Item(
                    source_id=sid,
                    source_name=name,
                    uid=str(uid),
                    title=entry.get("title", "(no title)").strip(),
                    url=link,
                    summary=_clean_summary(entry.get("summary", "")),
                    published=entry.get("published", entry.get("updated", "")),
                )
            )
    except Exception as exc:  # 1ソースの失敗で全体を落とさない
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def fetch_html_table(source: dict) -> FetchResult:
    """RSSのないページの表をスクレイプし、各行を1項目として扱う。

    行テキスト全体をハッシュ化するので、新規行の追加だけでなく
    既存行のステータス変化も「新着（更新）」として検知される。
    """
    sid, name, url = source["id"], source["name"], source["url"]
    result = FetchResult(sid, name)
    try:
        html = _http_get(url).text
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table tr")
        for row in rows:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if not cells:
                continue
            # ヘッダー行らしきものはスキップ
            if row.find("th") and not row.find("td"):
                continue
            text = " | ".join(cells)
            link = url
            anchor = row.find("a", href=True)
            if anchor:
                link = requests.compat.urljoin(url, anchor["href"])
            result.items.append(
                Item(
                    source_id=sid,
                    source_name=name,
                    uid=_hash(sid, text),
                    title=text,
                    url=link,
                )
            )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _clean_summary(html: str, limit: int = 280) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    return text[:limit] + ("…" if len(text) > limit else "")


FETCHERS = {"rss": fetch_rss, "html_table": fetch_html_table}


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {"version": 1, "seen": {}}


def save_manifest(manifest: dict) -> None:
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def collect(config: dict) -> list[FetchResult]:
    results = []
    for source in config.get("sources", []):
        fetcher = FETCHERS.get(source.get("type"))
        if fetcher is None:
            results.append(
                FetchResult(
                    source["id"],
                    source.get("name", source["id"]),
                    error=f"unknown source type: {source.get('type')!r}",
                )
            )
            continue
        results.append(fetcher(source))
    return results


def diff_new(results: list[FetchResult], manifest: dict) -> dict[str, list[Item]]:
    seen = manifest.get("seen", {})
    new_by_source: dict[str, list[Item]] = {}
    for res in results:
        if res.error:
            continue
        known = set(seen.get(res.source_id, []))
        fresh = [it for it in res.items if it.uid not in known]
        if fresh:
            new_by_source[res.source_id] = fresh
    return new_by_source


def update_seen(results: list[FetchResult], manifest: dict) -> None:
    seen = manifest.setdefault("seen", {})
    for res in results:
        if res.error:
            continue  # 取得失敗時は既読集合を壊さない
        current = [it.uid for it in res.items]
        merged = list(dict.fromkeys(current + seen.get(res.source_id, [])))
        seen[res.source_id] = merged[:MAX_SEEN_PER_SOURCE]


def render(new_by_source: dict[str, list[Item]], config: dict) -> tuple[str, str]:
    max_items = config.get("mail", {}).get("max_items", 60)
    total = sum(len(v) for v in new_by_source.values())
    date_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

    text_lines = [f"Silicon Digest — {date_str}", f"新着 {total} 件", ""]
    html_parts = [
        "<html><body style='font-family:sans-serif;line-height:1.5'>",
        f"<h2>Silicon Digest — {date_str}</h2>",
        f"<p>新着 {total} 件</p>",
    ]

    shown = 0
    for sid, items in new_by_source.items():
        name = items[0].source_name
        text_lines.append(f"## {name} ({len(items)})")
        html_parts.append(f"<h3>{name} <small>({len(items)})</small></h3><ul>")
        for it in items:
            if shown >= max_items:
                break
            shown += 1
            meta = f" — {it.published}" if it.published else ""
            text_lines.append(f"- {it.title}{meta}\n  {it.url}")
            summary = f"<br><span style='color:#555'>{it.summary}</span>" if it.summary else ""
            html_parts.append(
                f"<li><a href='{it.url}'>{it.title}</a>"
                f"<small style='color:#888'>{meta}</small>{summary}</li>"
            )
        text_lines.append("")
        html_parts.append("</ul>")
        if shown >= max_items:
            text_lines.append(f"(残りは max_items={max_items} のため省略)")
            html_parts.append(f"<p><em>残りは max_items={max_items} のため省略</em></p>")
            break

    html_parts.append("</body></html>")
    return "\n".join(text_lines), "\n".join(html_parts)


def send_email(subject: str, text_body: str, html_body: str) -> None:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    mail_from = os.environ.get("MAIL_FROM", user or "")
    mail_to = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]

    missing = [k for k, v in {
        "SMTP_HOST": host, "SMTP_USERNAME": user,
        "SMTP_PASSWORD": password, "MAIL_TO": mail_to,
    }.items() if not v]
    if missing:
        raise RuntimeError(
            "メール送信に必要なシークレットが未設定です: " + ", ".join(missing)
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(mail_to)
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=REQUEST_TIMEOUT) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(mail_from, mail_to, msg.as_string())


def log_fetch_summary(results: list[FetchResult]) -> None:
    print("=== fetch summary ===", file=sys.stderr)
    for res in results:
        if res.error:
            print(f"  [FAIL] {res.source_id}: {res.error}", file=sys.stderr)
        else:
            print(f"  [ ok ] {res.source_id}: {len(res.items)} items", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Silicon news digest")
    parser.add_argument("--dry-run", action="store_true",
                        help="検出だけ。メール送信も manifest 更新もしない")
    parser.add_argument("--init", action="store_true",
                        help="現在の全項目を既読化（配信なし・ベースライン作成）")
    args = parser.parse_args()

    config = load_config()
    manifest = load_manifest()
    results = collect(config)
    log_fetch_summary(results)

    if args.init:
        update_seen(results, manifest)
        save_manifest(manifest)
        total = sum(len(r.items) for r in results if not r.error)
        print(f"init: {total} 件を既読として記録しました。")
        return 0

    new_by_source = diff_new(results, manifest)
    total_new = sum(len(v) for v in new_by_source.values())
    text_body, html_body = render(new_by_source, config)

    if args.dry_run:
        print(text_body)
        print(f"\n[dry-run] 新着 {total_new} 件（メール送信・manifest更新なし）",
              file=sys.stderr)
        return 0

    if total_new == 0:
        print("新着なし。メール送信せず manifest のみ更新。", file=sys.stderr)
        update_seen(results, manifest)
        save_manifest(manifest)
        return 0

    prefix = config.get("mail", {}).get("subject_prefix", "[Digest]")
    date_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    subject = f"{prefix} {date_str} — 新着 {total_new} 件"
    send_email(subject, text_body, html_body)
    print(f"メール送信完了: {subject}", file=sys.stderr)

    update_seen(results, manifest)
    save_manifest(manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
