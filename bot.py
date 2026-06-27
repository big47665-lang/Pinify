"""
Pinify Bot — Pinterest image search for Telegram
Supports English and Persian | /pinme & /پینمی commands
"""

import os
import logging
import sqlite3
import random
import asyncio
import re
import json
import time
import urllib.request
import urllib.parse
from urllib.error import URLError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DB_PATH   = "pinify.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("pinify")


# ── Database ──────────────────────────────────────────────────────────────────
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sent_images (
            chat_id   TEXT NOT NULL,
            query     TEXT NOT NULL,
            image_url TEXT NOT NULL,
            PRIMARY KEY (chat_id, query, image_url)
        )"""
    )
    conn.commit()
    return conn


def already_sent(conn, chat_id: str, query: str, url: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sent_images WHERE chat_id=? AND query=? AND image_url=?",
        (chat_id, query, url),
    )
    return cur.fetchone() is not None


def mark_sent(conn, chat_id: str, query: str, url: str) -> None:
    try:
        conn.execute(
            "INSERT INTO sent_images (chat_id, query, image_url) VALUES (?,?,?)",
            (chat_id, query, url),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # already there


# ── Image Scraper (DuckDuckGo → Pinterest CDN) ────────────────────────────────
# Pinterest blocks direct API scraping from cloud IPs.
# Instead we use DuckDuckGo's image search filtered to pinterest.com,
# then extract the direct i.pinimg.com CDN URLs — always publicly accessible.

DDG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "DNT": "1",
}

# i.pinimg.com is Pinterest's image CDN — direct image links, no auth needed
PINIMG_RE = re.compile(r'https://i\.pinimg\.com/[^\s"\'\\>]+\.(?:jpg|jpeg|png|webp)', re.IGNORECASE)


def _ddg_vqd(query: str) -> str:
    """Get the DuckDuckGo vqd token required for image search."""
    url = "https://duckduckgo.com/?q=" + urllib.parse.quote(query) + "&iax=images&ia=images"
    req = urllib.request.Request(url, headers=DDG_HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    m = re.search(r'vqd=(["\'])([^"\']+)\1', html)
    if not m:
        m = re.search(r'vqd=([\d-]+)', html)
        return m.group(1) if m else ""
    return m.group(2)


def search_pinterest(query: str, page_size: int = 80) -> list[str]:
    """
    Search DuckDuckGo images filtered to site:pinterest.com,
    extract i.pinimg.com CDN URLs and return them.
    """
    pinquery = f"{query} site:pinterest.com"

    try:
        vqd = _ddg_vqd(pinquery)
    except Exception as exc:
        log.error("DDG vqd fetch failed: %s", exc)
        return _fallback_search(query)

    if not vqd:
        log.warning("No vqd token found, trying fallback")
        return _fallback_search(query)

    params = urllib.parse.urlencode({
        "l": "us-en",
        "o": "json",
        "q": pinquery,
        "vqd": vqd,
        "f": ",,,,,",
        "p": "1",
        "v7exp": "a",
    })
    api_url = "https://duckduckgo.com/i.js?" + params
    headers = {**DDG_HEADERS, "Referer": "https://duckduckgo.com/"}

    try:
        time.sleep(0.5)  # be polite
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        urls: list[str] = []
        for item in data.get("results", []):
            # DDG gives us the Pinterest page URL and a thumbnail
            # The thumbnail IS the i.pinimg.com CDN link — grab it
            img = item.get("image", "")
            thumb = item.get("thumbnail", "")
            for candidate in (img, thumb):
                if candidate and "pinimg.com" in candidate:
                    # Upgrade to 736x (high res) if possible
                    upgraded = re.sub(r'/\d+x/', '/736x/', candidate)
                    urls.append(upgraded)
                    break
            else:
                # fallback: scan the page URL for embedded image refs
                page = item.get("url", "")
                if "pinterest" in page and img.startswith("http"):
                    urls.append(img)

        log.info("DDG search for '%s' returned %d images", query, len(urls))
        return list(dict.fromkeys(urls))  # dedupe, preserve order

    except Exception as exc:
        log.error("DDG image search failed: %s", exc)
        return _fallback_search(query)


def _fallback_search(query: str) -> list[str]:
    """
    Last-resort: scrape DuckDuckGo HTML search results page and
    extract any i.pinimg.com URLs found in the raw HTML.
    """
    log.info("Running HTML fallback scraper for '%s'", query)
    url = (
        "https://html.duckduckgo.com/html/?q="
        + urllib.parse.quote(f"{query} site:pinterest.com")
    )
    try:
        req = urllib.request.Request(url, headers=DDG_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        urls = PINIMG_RE.findall(html)
        # Upgrade resolution
        upgraded = [re.sub(r'/\d+x/', '/736x/', u) for u in urls]
        unique = list(dict.fromkeys(upgraded))
        log.info("HTML fallback found %d images", len(unique))
        return unique
    except Exception as exc:
        log.error("HTML fallback also failed: %s", exc)
        return []


def pick_fresh_images(
    conn, chat_id: str, query: str, all_urls: list[str], count: int = 4
) -> list[str]:
    """Return *count* URLs the chat hasn't seen yet for this query."""
    random.shuffle(all_urls)
    chosen: list[str] = []
    for url in all_urls:
        if len(chosen) >= count:
            break
        if not already_sent(conn, chat_id, query, url):
            chosen.append(url)
    return chosen


# ── Text helpers ──────────────────────────────────────────────────────────────
def normalize_query(text: str) -> str:
    """Lower-case and strip the query."""
    return text.strip().lower()


def detect_pinme(text: str) -> str | None:
    """
    Return the search query if the message starts with pinme or پینمی,
    else return None.
    """
    text = text.strip()
    pattern = re.compile(
        r"^(?:pinme|پینمی)\s+(.+)$",
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.match(text)
    return m.group(1).strip() if m else None


# ── Telegram Handlers ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    bot_username = (await ctx.bot.get_me()).username
    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Add Pinify to a group",
                url=f"https://t.me/{bot_username}?startgroup=true",
            )
        ],
        [
            InlineKeyboardButton(
                "📌 How to use",
                callback_data="help",
            )
        ],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    msg = (
        "📌 *Welcome to Pinify!*\n\n"
        "I search Pinterest and bring you 4 fresh photos for any vibe.\n\n"
        "🇬🇧 *English:*\n"
        "`pinme <your topic>`\n"
        "_Example:_ `pinme dark academia aesthetic`\n\n"
        "🇮🇷 *فارسی:*\n"
        "`پینمی <موضوع شما>`\n"
        "_مثال:_ `پینمی گل‌های زیبا`\n\n"
        "✨ Every search gives different photos — no repeats!"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    bot_username = (await ctx.bot.get_me()).username
    keyboard = [[
        InlineKeyboardButton(
            "➕ Add Pinify to a group",
            url=f"https://t.me/{bot_username}?startgroup=true",
        )
    ]]
    markup = InlineKeyboardMarkup(keyboard)

    msg = (
        "📌 *Pinify — Help*\n\n"
        "*Commands:*\n"
        "• `pinme <topic>` — search in English\n"
        "• `پینمی <موضوع>` — جستجو به فارسی\n\n"
        "*Examples / مثال‌ها:*\n"
        "`pinme cozy winter bedroom`\n"
        "`pinme minimalist workspace`\n"
        "`پینمی طبیعت پاییزی`\n"
        "`پینمی دکوراسیون مدرن`\n\n"
        "*Notes:*\n"
        "• I send 4 images per request 🖼\n"
        "• Same query → always fresh photos, never duplicates ✅\n"
        "• Works in groups too! Add me with the button below 👇"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


async def handle_pinme(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Central handler: fires when a message contains pinme / پینمی."""
    message = update.message or update.channel_post
    if not message or not message.text:
        return

    raw_query = detect_pinme(message.text)
    if raw_query is None:
        return  # not a pinme trigger

    query      = normalize_query(raw_query)
    chat_id    = str(message.chat_id)
    lang_fa    = bool(re.search(r"[\u0600-\u06FF]", message.text))  # has Persian chars?

    # Acknowledge
    if lang_fa:
        ack = await message.reply_text(f"🔍 در حال جستجو برای «{raw_query}» در پینترست...")
    else:
        ack = await message.reply_text(f"🔍 Searching Pinterest for *{raw_query}*…", parse_mode=ParseMode.MARKDOWN)

    # Search
    conn     = db_connect()
    all_urls = await asyncio.to_thread(search_pinterest, query, 80)

    if not all_urls:
        await ack.edit_text(
            "😕 هیچ تصویری پیدا نشد. لطفاً کلمات دیگری امتحان کنید." if lang_fa
            else "😕 No images found. Try different keywords!"
        )
        conn.close()
        return

    chosen = pick_fresh_images(conn, chat_id, query, all_urls, count=4)

    if not chosen:
        await ack.edit_text(
            "🔄 تمام تصاویر این موضوع قبلاً ارسال شده‌اند. لطفاً دوباره امتحان کنید تا تصاویر جدید بیایند." if lang_fa
            else "🔄 You've seen all available images for this topic! Try again later or use a different query."
        )
        conn.close()
        return

    # Mark as sent BEFORE sending (prevents race conditions)
    for url in chosen:
        mark_sent(conn, chat_id, query, url)
    conn.close()

    # Delete ack and send media group
    await ack.delete()

    from telegram import InputMediaPhoto
    media = [InputMediaPhoto(media=url) for url in chosen]
    caption = f"📌 {raw_query}" + (" • پینیفای" if lang_fa else " • Pinify")
    media[0] = InputMediaPhoto(media=chosen[0], caption=caption)

    try:
        await message.reply_media_group(media=media)
    except Exception as exc:
        log.error("Media group send failed: %s", exc)
        # Fallback: send individually
        for i, url in enumerate(chosen):
            try:
                cap = caption if i == 0 else None
                await message.reply_photo(photo=url, caption=cap)
            except Exception as e2:
                log.error("Single photo send failed: %s", e2)


async def post_init(application: Application) -> None:
    """Set bot commands visible in Telegram menu."""
    await application.bot.set_my_commands([
        BotCommand("start",  "Welcome & add to group"),
        BotCommand("help",   "How to use Pinify"),
    ])
    log.info("Bot commands set.")


# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Set the BOT_TOKEN environment variable before running!\n"
            "  export BOT_TOKEN=123456:ABC-..."
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))

    # Message handler — catches any text containing pinme/پینمی (groups + private)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_pinme,
        )
    )

    log.info("🌸 Pinify is running — press Ctrl+C to stop")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
