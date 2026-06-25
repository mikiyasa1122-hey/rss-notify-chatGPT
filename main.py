import os
import re
import json
import html
import time
import hashlib
from pathlib import Path
from datetime import datetime, timezone

import feedparser
import requests
from dotenv import load_dotenv
from google import genai


load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
RSS_FEED_URLS = [
    url.strip()
    for url in os.getenv("RSS_FEED_URLS", "").split(",")
    if url.strip()
]

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "5"))

POSTED_FILE = Path("posted_items.json")


def validate_env():
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY が設定されていません。")

    if not DISCORD_WEBHOOK_URL:
        raise ValueError("DISCORD_WEBHOOK_URL が設定されていません。")

    if not RSS_FEED_URLS:
        raise ValueError("RSS_FEED_URLS が設定されていません。")


def load_posted_ids():
    if not POSTED_FILE.exists():
        return set()

    try:
        with POSTED_FILE.open("r", encoding="utf-8") as f:
            return set(json.load(f))
    except json.JSONDecodeError:
        return set()


def save_posted_ids(posted_ids):
    with POSTED_FILE.open("w", encoding="utf-8") as f:
        json.dump(list(posted_ids), f, ensure_ascii=False, indent=2)


def clean_text(text):
    if not text:
        return ""

    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def make_entry_id(entry):
    base = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def trim(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def summarize_with_gemini(client, title, article_text, link):
    prompt = f"""
以下のRSS記事を日本語で要約してください。

条件:
- 3行以内
- 重要ポイントを中心に
- 誇張しすぎない
- 最後に「注目ポイント: 〇〇」の形で1文追加

タイトル:
{title}

記事本文または概要:
{article_text}

URL:
{link}
"""

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )

    return (response.text or "").strip()


def post_to_discord(title, summary, link, feed_title):
    payload = {
        "username": "RSS要約Bot",
        "embeds": [
            {
                "title": trim(title, 250),
                "url": link,
                "description": trim(summary, 3900),
                "footer": {
                    "text": feed_title or "RSS Feed"
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
        "allowed_mentions": {
            "parse": []
        }
    }

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json=payload,
        timeout=15,
    )

    if response.status_code == 429:
        retry_after = response.json().get("retry_after", 3)
        time.sleep(float(retry_after))
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=15,
        )

    response.raise_for_status()


def fetch_entries():
    entries = []

    for feed_url in RSS_FEED_URLS:
        feed = feedparser.parse(feed_url)
        feed_title = feed.feed.get("title", feed_url)

        for entry in feed.entries[:MAX_ITEMS]:
            title = clean_text(entry.get("title", "タイトルなし"))
            link = entry.get("link", "")
            summary = clean_text(
                entry.get("summary")
                or entry.get("description")
                or ""
            )

            entries.append({
                "id": make_entry_id(entry),
                "title": title,
                "link": link,
                "summary": summary,
                "feed_title": feed_title,
            })

    return entries


def main():
    validate_env()

    client = genai.Client(api_key=GEMINI_API_KEY)
    posted_ids = load_posted_ids()

    entries = fetch_entries()
    new_entries = [
        entry for entry in entries
        if entry["id"] not in posted_ids
    ]

    if not new_entries:
        print("新しい記事はありません。")
        return

    for entry in new_entries:
        article_text = entry["summary"] or entry["title"]

        print(f"要約中: {entry['title']}")

        try:
            gemini_summary = summarize_with_gemini(
                client=client,
                title=entry["title"],
                article_text=article_text[:4000],
                link=entry["link"],
            )
        except Exception as e:
            print(f"Gemini要約に失敗しました: {e}")
            gemini_summary = "Geminiの混雑またはエラーにより、要約を取得できませんでした。記事リンクから確認してください。"

        post_to_discord(
            title=entry["title"],
            summary=gemini_summary,
            link=entry["link"],
            feed_title=entry["feed_title"],
        )

        posted_ids.add(entry["id"])
        save_posted_ids(posted_ids)

        print(f"投稿完了: {entry['title']}")

        time.sleep(1)


if __name__ == "__main__":
    main()
