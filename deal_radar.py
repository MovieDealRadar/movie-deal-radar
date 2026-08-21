import os
import re
import json
import time
import html
import sqlite3
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import feedparser
import requests
from requests.exceptions import HTTPError
from bs4 import BeautifulSoup
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "deal_radar.db"

load_dotenv(APP_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

@dataclass
class DealResult:
    score: int
    verdict: str
    reason: str
    matched_title: Optional[str] = None
    price: Optional[float] = None
    label: Optional[str] = None


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_posts (
            post_id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            created_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT,
            verdict TEXT,
            created_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def clean_html(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    return html.unescape(soup.get_text(" ", strip=True))


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = text.replace("’", "'")
    text = re.sub(r"[^a-z0-9$.'\- ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_prices(text: str):
    # Typical MediaSwap pricing patterns: $45, $45.00
    vals = []
    for m in re.finditer(r"\$\s*(\d{1,4}(?:\.\d{1,2})?)", text):
        try:
            vals.append(float(m.group(1)))
        except ValueError:
            pass
    return vals


def nearest_price_for_alias(text: str, alias: str) -> Optional[float]:
    """Look near a title mention first; fall back to the first price in the listing."""
    low = normalize(text)
    a = normalize(alias)
    idx = low.find(a)
    if idx >= 0:
        window = low[max(0, idx - 80): idx + len(a) + 120]
        prices = extract_prices(window)
        if prices:
            return prices[0]
    prices = extract_prices(low)
    return prices[0] if prices else None


def phrase_present(text: str, phrases):
    return any(p in text for p in phrases)


def evaluate(post_title: str, body: str, cfg) -> DealResult:
    combined_raw = f"{post_title}\n{body}"
    text = normalize(combined_raw)

    # Ignore buying posts; this radar is looking for inventory for sale.
    if "[buying]" in post_title.lower() or re.search(r"\bbuying\b", normalize(post_title)):
        return DealResult(0, "IGNORE", "Buying post")

    # 1) Specific title rules beat everything else.
    for target in cfg.get("targets", []):
        aliases = [target.get("title", "")] + target.get("aliases", [])
        for alias in aliases:
            if alias and normalize(alias) in text:
                price = nearest_price_for_alias(combined_raw, alias)
                great = target.get("great_buy_max")
                good = target.get("good_buy_max")

                base = 78
                if price is not None and great is not None and price <= float(great):
                    return DealResult(
                        100, "🔥 GREAT BUY",
                        f"{target['title']} at ${price:.2f} ≤ your great-buy max ${float(great):.2f}",
                        target["title"], price, target.get("label")
                    )
                if price is not None and good is not None and price <= float(good):
                    return DealResult(
                        90, "✅ GOOD BUY",
                        f"{target['title']} at ${price:.2f} ≤ your good-buy max ${float(good):.2f}",
                        target["title"], price, target.get("label")
                    )
                if price is None:
                    return DealResult(
                        base, "👀 TARGET FOUND",
                        f"{target['title']} was listed, but I couldn't confidently pair a price with it",
                        target["title"], None, target.get("label")
                    )
                return DealResult(
                    55, "ℹ️ TARGET / PRICE HIGH",
                    f"{target['title']} found at about ${price:.2f}",
                    target["title"], price, target.get("label")
                )

    # 2) Generic boutique discovery mode.
    second_sight = phrase_present(text, ["second sight", "secondsight"])
    arrow = phrase_present(text, ["arrow video", "arrow films", "arrow 4k", "arrow le", "arrow limited"])
    four_k = phrase_present(text, ["4k", "uhd"])
    limited = phrase_present(text, ["limited edition", " le ", "box set", "boxset", "collector"])
    selling = phrase_present(text, ["selling", "for sale", "[selling]"])

    if not (second_sight or arrow):
        return DealResult(0, "IGNORE", "Not Arrow/Second Sight")

    score = 25
    reasons = []

    if second_sight:
        score += 25
        reasons.append("Second Sight")
    if arrow:
        score += 22
        reasons.append("Arrow")
    if four_k:
        score += 18
        reasons.append("4K/UHD")
    if limited:
        score += 18
        reasons.append("limited/collector edition")
    if selling:
        score += 5

    prices = extract_prices(combined_raw)
    price = min(prices) if prices else None
    if price is not None and price <= 40:
        score += 10
        reasons.append("low visible price")
    elif price is not None and price <= 65:
        score += 5

    if score >= 80:
        verdict = "🚨 BOUTIQUE LE LEAD"
    elif score >= int(cfg.get("minimum_generic_score", 60)):
        verdict = "👀 POSSIBLE DEAL"
    else:
        verdict = "IGNORE"

    return DealResult(min(score, 99), verdict, ", ".join(reasons), price=price,
                      label="Second Sight" if second_sight else "Arrow")


def telegram_api(method: str, payload: dict):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data)
    return data["result"]


def send_alert(post_id: str, title: str, url: str, result: DealResult):
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID in .env")

    message = (
        f"{result.verdict}\n"
        f"Score: {result.score}/100\n\n"
        f"{title}\n\n"
        f"Why: {result.reason}\n"
        f"{'Price: $' + format(result.price, '.2f') + chr(10) if result.price is not None else ''}"
        f"\n{url}"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": "🔥 Great Deal", "callback_data": f"great|{post_id}"},
            {"text": "👍 Interesting", "callback_data": f"good|{post_id}"},
            {"text": "👎 Pass", "callback_data": f"pass|{post_id}"}
        ]]
    }

    telegram_api("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": False,
        "reply_markup": keyboard
    })


def get_setting(conn, key, default="0"):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )
    conn.commit()


def process_telegram_feedback(conn):
    if not TELEGRAM_BOT_TOKEN:
        return
    offset = int(get_setting(conn, "telegram_offset", "0"))
    try:
        updates = telegram_api("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["callback_query"]})
    except Exception as e:
        logging.warning("Telegram feedback check failed: %s", e)
        return

    for update in updates:
        set_setting(conn, "telegram_offset", update["update_id"] + 1)
        q = update.get("callback_query")
        if not q:
            continue

        data = q.get("data", "")
        try:
            verdict, post_id = data.split("|", 1)
        except ValueError:
            continue

        conn.execute(
            "INSERT INTO feedback(post_id, verdict, created_at) VALUES(?,?,?)",
            (post_id, verdict, int(time.time()))
        )
        conn.commit()

        # Telegram spinner acknowledgement.
        try:
            telegram_api("answerCallbackQuery", {
                "callback_query_id": q["id"],
                "text": f"Saved: {verdict}. This will be used when tuning your deal rules."
            })
        except Exception:
            pass
        logging.info("Feedback saved: %s = %s", post_id, verdict)


def already_seen(conn, post_id):
    return conn.execute("SELECT 1 FROM seen_posts WHERE post_id=?", (post_id,)).fetchone() is not None


def mark_seen(conn, post_id, title, url):
    conn.execute(
        "INSERT OR IGNORE INTO seen_posts(post_id,title,url,created_at) VALUES(?,?,?,?)",
        (post_id, title, url, int(time.time()))
    )
    conn.commit()


def fetch_feed(feed_url):
    # Use a descriptive User-Agent and respect Reddit rate limiting.
    headers = {
        "User-Agent": "MediaDealRadar/0.2 (personal-use RSS monitor; contact: local-user)"
    }
    r = requests.get(feed_url, headers=headers, timeout=20)

    if r.status_code == 429:
        retry_after = r.headers.get("Retry-After")
        wait_for = int(retry_after) if retry_after and retry_after.isdigit() else 120
        raise RuntimeError(f"REDDIT_RATE_LIMIT:{wait_for}")

    r.raise_for_status()
    return feedparser.parse(r.content)


def bootstrap_existing(conn, cfg):
    """On first launch, mark current feed items seen so the phone doesn't get spammed."""
    if get_setting(conn, "bootstrapped", "0") == "1":
        return False
    feed = fetch_feed(cfg["reddit_feed"])
    for entry in feed.entries:
        post_id = entry.get("id") or entry.get("link")
        mark_seen(conn, post_id, entry.get("title", ""), entry.get("link", ""))
    set_setting(conn, "bootstrapped", "1")
    logging.info("First-run bootstrap complete: existing posts marked as seen.")
    return True


def run():
    cfg = load_config()
    conn = init_db()

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit(
            "\nTelegram is not configured.\n"
            "1) Copy .env.example to .env\n"
            "2) Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID\n"
        )

    bootstrapped_now = bootstrap_existing(conn, cfg)
    poll_seconds = max(60, int(cfg.get("poll_seconds", 60)))

    logging.info("Media Deal Radar running. Polling every %ss.", poll_seconds)

    # The bootstrap itself fetches Reddit once. Avoid immediately fetching it again.
    if bootstrapped_now:
        logging.info("Waiting %ss before the first live check to avoid Reddit rate limiting.", poll_seconds)
        time.sleep(poll_seconds)

    while True:
        try:
            cfg = load_config()  # hot reload config each cycle
            process_telegram_feedback(conn)
            feed = fetch_feed(cfg["reddit_feed"])

            # Reverse to process older unseen entries first.
            for entry in reversed(feed.entries):
                post_id = entry.get("id") or entry.get("link")
                title = entry.get("title", "")
                url = entry.get("link", "")
                if not post_id or already_seen(conn, post_id):
                    continue

                body = clean_html(entry.get("summary", "") or entry.get("content", [{}])[0].get("value", ""))
                result = evaluate(title, body, cfg)

                logging.info("%s | %s | score=%s", result.verdict, title, result.score)

                should_alert = result.verdict != "IGNORE"
                if should_alert:
                    send_alert(post_id, title, url, result)

                mark_seen(conn, post_id, title, url)

        except KeyboardInterrupt:
            logging.info("Stopped.")
            break
        except RuntimeError as e:
            msg = str(e)
            if msg.startswith("REDDIT_RATE_LIMIT:"):
                wait_for = int(msg.split(":", 1)[1])
                logging.warning("Reddit rate-limited the feed. Waiting %ss before trying again.", wait_for)
                time.sleep(wait_for)
                continue
            logging.exception("Cycle failed: %s", e)
        except Exception as e:
            logging.exception("Cycle failed: %s", e)

        time.sleep(poll_seconds)


if __name__ == "__main__":
    run()
