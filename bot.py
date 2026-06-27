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


# ── Pinterest Scraper ─────────────────────────────────────────────────────────
PINTEREST_SEARCH = "https://www.pinterest.com/resource/BaseSearchResource/get/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*, q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.pinterest.com/",
}


def _extract_images_from_data(data: dict) -> list[str]:
    """Walk the Pinterest JSON and collect original image URLs."""
    urls: list[str] = []
    try:
        results = (
            data.get("resource_response", {})
            .get("data", {})
            .get("results", [])
        )
        for pin in results:
            images = pin.get("images", {})
            # prefer highest resolution
            for size in ("orig", "736x", "564x", "474x", "236x"):
                img = images.get(size, {})
                url = img.get("url", "")
                if url and url.startswith("http"):
                    urls.append(url)
                    break
    except Exception as exc:
        log.warning("Image extraction error: %s", exc)
    return urls


def search_pinterest(query: str, page_size: int = 50) -> list[str]:
    """Return a list of image URLs from Pinterest for *query*."""
    options = {
        "isPrefetch": False,
        "query": query,
        "scope": "pins",
        "no_fetch_context_on_resource": False,
        "page_size": page_size,
    }
    params = urllib.parse.urlencode(
        {"source_url": f"/search/pins/?q={urllib.parse.quote(query)}",
         "data": json.dumps({"options": options, "context": {}}),
         "_": ""}
    )
    url = f"{PINTEREST_SEARCH}?{params}"

    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            return _extract_images_from_data(data)
    except (URLError, json.JSONDecodeError) as exc:
        log.error("Pinterest fetch error: %s", exc)
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
