"""
Pinify Bot v2 — Pinterest-accurate image search for Telegram
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Features:
  • Claude AI expands every query semantically (like Pinterest's own engine)
  • Per-user taste profile builds over time from search history
  • Claude reads the profile and personalizes future expansions to YOUR style
  • Multi-query scraping (4 sub-queries in parallel) = way more results
  • 3-layer scraper: DDG JSON → DDG HTML → Bing fallback
  • Zero duplicate images per user per topic
  • English + Persian (فارسی)
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

import anthropic

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DB_PATH       = "pinify.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("pinify")

ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None


# ── Database ───────────────────────────────────────────────────────────────────
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sent_images (
            user_id   TEXT NOT NULL,
            query     TEXT NOT NULL,
            image_url TEXT NOT NULL,
            PRIMARY KEY (user_id, query, image_url)
        );
        CREATE TABLE IF NOT EXISTS user_profile (
            user_id        TEXT PRIMARY KEY,
            search_history TEXT NOT NULL DEFAULT '[]',
            taste_tags     TEXT NOT NULL DEFAULT '[]',
            updated_at     INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()
    return conn


DB = db_connect()


def already_sent(user_id: str, query: str, url: str) -> bool:
    cur = DB.execute(
        "SELECT 1 FROM sent_images WHERE user_id=? AND query=? AND image_url=?",
        (user_id, query, url),
    )
    return cur.fetchone() is not None


def mark_sent(user_id: str, query: str, url: str) -> None:
    try:
        DB.execute(
            "INSERT INTO sent_images (user_id, query, image_url) VALUES (?,?,?)",
            (user_id, query, url),
        )
        DB.commit()
    except sqlite3.IntegrityError:
        pass


# ── User Taste Profile ─────────────────────────────────────────────────────────
def get_profile(user_id: str) -> dict:
    row = DB.execute(
        "SELECT search_history, taste_tags FROM user_profile WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if not row:
        return {"history": [], "taste_tags": []}
    return {
        "history": json.loads(row[0]),
        "taste_tags": json.loads(row[1]),
    }


def update_profile(user_id: str, query: str, new_tags: list[str]) -> None:
    """Add this search to history and merge new taste tags."""
    profile = get_profile(user_id)

    history = profile["history"]
    history.append(query)
    history = history[-30:]  # keep last 30 searches

    # merge taste tags, most recent first, cap at 40
    existing = profile["taste_tags"]
    merged = list(dict.fromkeys(new_tags + existing))[:40]

    DB.execute(
        """INSERT INTO user_profile (user_id, search_history, taste_tags, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               search_history=excluded.search_history,
               taste_tags=excluded.taste_tags,
               updated_at=excluded.updated_at""",
        (user_id, json.dumps(history), json.dumps(merged), int(time.time())),
    )
    DB.commit()


def reset_profile(user_id: str) -> None:
    DB.execute("DELETE FROM user_profile WHERE user_id=?", (user_id,))
    DB.execute("DELETE FROM sent_images WHERE user_id=?", (user_id,))
    DB.commit()


# ── Claude AI: Query Expansion + Taste Inference ───────────────────────────────
def ai_expand_query(raw_query: str, profile: dict) -> dict:
    """
    Ask Claude to:
    1. Expand the raw query into 4 Pinterest-optimised search sub-queries
    2. Extract taste tags from this search
    3. Factor in the user's taste profile to personalise
    Returns {"sub_queries": [...], "tags": [...], "display_theme": "..."}
    Falls back to a simple expansion if AI is unavailable.
    """
    if not ai:
        return _simple_expand(raw_query)

    history_str = ", ".join(profile["history"][-10:]) if profile["history"] else "none yet"
    tags_str    = ", ".join(profile["taste_tags"][:20]) if profile["taste_tags"] else "none yet"

    prompt = f"""You are the search engine behind a Pinterest-like bot called Pinify.
A user searched for: "{raw_query}"

Their recent search history: {history_str}
Their established taste profile tags: {tags_str}

Your job:
1. Generate exactly 4 diverse Pinterest search sub-queries in ENGLISH that will find highly relevant, beautiful images for what this person wants. Make them specific and visual — like how someone would search Pinterest. Use aesthetic terminology, visual descriptors, moods. Each sub-query should approach the topic from a slightly different angle to maximise variety.
2. Extract 3-6 short taste tags from THIS search (e.g. "minimalist", "warm tones", "cottagecore") to build their taste profile.
3. Write a very short display theme label (2-5 words) summarising what vibe you're searching for.

IMPORTANT: If the user has a taste profile, subtly personalise the sub-queries to match their aesthetic. For example, if their history shows they love dark/moody aesthetics, lean darker even if their query is neutral.

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "sub_queries": ["query1", "query2", "query3", "query4"],
  "tags": ["tag1", "tag2", "tag3"],
  "display_theme": "short vibe label"
}}"""

    try:
        resp = ai.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # strip markdown fences if any
        text = re.sub(r"^```[a-z]*\n?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        data = json.loads(text)
        return {
            "sub_queries": data.get("sub_queries", [raw_query]),
            "tags":        data.get("tags", []),
            "display_theme": data.get("display_theme", raw_query),
        }
    except Exception as exc:
        log.warning("Claude expansion failed: %s — using fallback", exc)
        return _simple_expand(raw_query)


def _simple_expand(query: str) -> dict:
    """No-AI fallback: just add aesthetic modifiers."""
    base = query.strip()
    return {
        "sub_queries": [
            f"{base} aesthetic",
            f"{base} pinterest",
            f"{base} inspo",
            f"{base} mood board",
        ],
        "tags": [base],
        "display_theme": base,
    }


# ── Image Scraper (Multi-layer DDG → Bing fallback) ────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "DNT": "1",
}

PINIMG_RE = re.compile(
    r'https://i\.pinimg\.com/[^\s"\'\\><]+\.(?:jpg|jpeg|png|webp)',
    re.IGNORECASE,
)


def _upgrade_res(url: str) -> str:
    """Swap any size prefix for 736x (high res)."""
    return re.sub(r'/\d+x(?:/|\.)', '/736x/', url)


def _ddg_vqd(query: str) -> str:
    url = "https://duckduckgo.com/?q=" + urllib.parse.quote(query) + "&iax=images&ia=images"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    m = re.search(r'vqd=(["\'])([^"\']+)\1', html) or re.search(r'vqd=([\d\-]+)', html)
    if m:
        return m.group(2) if m.lastindex == 2 else m.group(1)
    return ""


def _scrape_ddg_json(query: str) -> list[str]:
    try:
        vqd = _ddg_vqd(f"{query} site:pinterest.com")
        if not vqd:
            return []
        params = urllib.parse.urlencode({
            "l": "us-en", "o": "json",
            "q": f"{query} site:pinterest.com",
            "vqd": vqd, "f": ",,,,,", "p": "1",
        })
        req = urllib.request.Request(
            "https://duckduckgo.com/i.js?" + params,
            headers={**HEADERS, "Referer": "https://duckduckgo.com/"},
        )
        time.sleep(0.3)
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        urls = []
        for item in data.get("results", []):
            for key in ("image", "thumbnail"):
                val = item.get(key, "")
                if val and "pinimg.com" in val:
                    urls.append(_upgrade_res(val))
                    break
        return urls
    except Exception as exc:
        log.debug("DDG JSON scrape failed for '%s': %s", query, exc)
        return []


def _scrape_ddg_html(query: str) -> list[str]:
    try:
        url = ("https://html.duckduckgo.com/html/?q="
               + urllib.parse.quote(f"{query} site:pinterest.com"))
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        return [_upgrade_res(u) for u in PINIMG_RE.findall(html)]
    except Exception as exc:
        log.debug("DDG HTML scrape failed for '%s': %s", query, exc)
        return []


def _scrape_bing(query: str) -> list[str]:
    """Bing image search as final fallback."""
    try:
        params = urllib.parse.urlencode({
            "q": f"{query} site:pinterest.com",
            "form": "HDRSC2", "first": "1", "tsc": "ImageHoverTitle",
        })
        url = "https://www.bing.com/images/search?" + params
        req = urllib.request.Request(url, headers={
            **HEADERS,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        return [_upgrade_res(u) for u in PINIMG_RE.findall(html)]
    except Exception as exc:
        log.debug("Bing scrape failed for '%s': %s", query, exc)
        return []


def scrape_one_query(query: str) -> list[str]:
    """Try all 3 layers for a single sub-query, return deduplicated URLs."""
    urls = _scrape_ddg_json(query)
    if len(urls) < 5:
        urls += _scrape_ddg_html(query)
    if len(urls) < 5:
        urls += _scrape_bing(query)
    return list(dict.fromkeys(urls))


def scrape_all_subqueries(sub_queries: list[str]) -> list[str]:
    """
    Run up to 4 sub-queries concurrently using threads,
    interleave results so variety is maximised.
    """
    import concurrent.futures
    results: list[list[str]] = [[] for _ in sub_queries]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(scrape_one_query, q): i for i, q in enumerate(sub_queries)}
        for future in concurrent.futures.as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                log.warning("Sub-query %d failed: %s", idx, exc)

    # interleave: take 1 from each list in rotation for variety
    interleaved: list[str] = []
    max_len = max((len(r) for r in results), default=0)
    for i in range(max_len):
        for r in results:
            if i < len(r):
                interleaved.append(r[i])

    return list(dict.fromkeys(interleaved))


def pick_fresh(user_id: str, base_query: str, all_urls: list[str], count: int = 4) -> list[str]:
    random.shuffle(all_urls)
    chosen = []
    for url in all_urls:
        if len(chosen) >= count:
            break
        if not already_sent(user_id, base_query, url):
            chosen.append(url)
    return chosen


# ── Text helpers ───────────────────────────────────────────────────────────────
def detect_pinme(text: str) -> str | None:
    m = re.match(r"^(?:pinme|پینمی)\s+(.+)$", text.strip(), re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None


def is_fa(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text))


# ── Telegram Handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    me = (await ctx.bot.get_me()).username
    keyboard = [
        [InlineKeyboardButton("➕ Add Pinify to a group", url=f"https://t.me/{me}?startgroup=true")],
        [InlineKeyboardButton("🎨 My Taste Profile", callback_data="profile")],
        [InlineKeyboardButton("🔄 Reset My Profile", callback_data="reset_confirm")],
    ]
    msg = (
        "📌 *Welcome to Pinify!*\n\n"
        "I find Pinterest photos for any vibe — and I learn your taste over time "
        "to get more accurate with every search.\n\n"
        "🇬🇧 *English:*\n"
        "`pinme <anything>`\n"
        "_Try:_ `pinme cozy bedroom` · `pinme dark aesthetic` · `pinme flowers`\n\n"
        "🇮🇷 *فارسی:*\n"
        "`پینمی <هر چیزی>`\n"
        "_مثال:_ `پینمی دکوراسیون` · `پینمی طبیعت` · `پینمی کافه`\n\n"
        "✨ The more you search, the better I know your style."
    )
    await update.message.reply_text(
        msg, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    profile = get_profile(user_id)
    me = (await ctx.bot.get_me()).username

    if not profile["history"]:
        msg = (
            "🎨 *Your Taste Profile*\n\n"
            "You haven't searched anything yet!\n"
            "Start with `pinme <topic>` and I'll learn your style."
        )
    else:
        recent = " · ".join(f"`{h}`" for h in profile["history"][-8:])
        tags   = " · ".join(f"#{t}" for t in profile["taste_tags"][:15]) or "_none yet_"
        msg = (
            f"🎨 *Your Taste Profile*\n\n"
            f"*Recent searches:*\n{recent}\n\n"
            f"*Your aesthetic tags:*\n{tags}\n\n"
            f"_The more you search, the more personalised your results become._"
        )

    keyboard = [[
        InlineKeyboardButton("➕ Add Pinify to a group", url=f"https://t.me/{me}?startgroup=true"),
        InlineKeyboardButton("🔄 Reset Profile", callback_data="reset_confirm"),
    ]]
    await update.message.reply_text(
        msg, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user_id = str(q.from_user.id)

    if q.data == "profile":
        profile = get_profile(user_id)
        if not profile["history"]:
            text = "🎨 *Your Taste Profile*\n\nNo searches yet! Try `pinme <topic>` first."
        else:
            recent = " · ".join(f"`{h}`" for h in profile["history"][-8:])
            tags   = " · ".join(f"#{t}" for t in profile["taste_tags"][:15]) or "_none yet_"
            text = (
                f"🎨 *Your Taste Profile*\n\n"
                f"*Recent searches:*\n{recent}\n\n"
                f"*Your aesthetic tags:*\n{tags}"
            )
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Reset Profile", callback_data="reset_confirm"),
                InlineKeyboardButton("« Back", callback_data="back_start"),
            ]]))

    elif q.data == "reset_confirm":
        await q.edit_message_text(
            "⚠️ *Reset your profile?*\n\nThis clears your search history, taste tags, and sent-image memory. Cannot be undone.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, reset", callback_data="reset_do"),
                InlineKeyboardButton("❌ Cancel", callback_data="back_start"),
            ]]),
        )

    elif q.data == "reset_do":
        reset_profile(user_id)
        await q.edit_message_text(
            "✅ *Profile reset!*\n\nFresh start — I'll learn your taste again from scratch.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif q.data == "back_start":
        me = (await ctx.bot.get_me()).username
        keyboard = [
            [InlineKeyboardButton("➕ Add Pinify to a group", url=f"https://t.me/{me}?startgroup=true")],
            [InlineKeyboardButton("🎨 My Taste Profile", callback_data="profile")],
            [InlineKeyboardButton("🔄 Reset My Profile", callback_data="reset_confirm")],
        ]
        await q.edit_message_text(
            "📌 *Pinify* — Type `pinme <anything>` to search Pinterest.\n\n"
            "I personalise results based on your taste — the more you search, the better I get!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def handle_pinme(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.channel_post
    if not message or not message.text:
        return

    raw_query = detect_pinme(message.text)
    if raw_query is None:
        return

    user_id  = str(message.from_user.id if message.from_user else message.chat_id)
    base_key = raw_query.strip().lower()
    fa       = is_fa(message.text)

    # ── Step 1: Acknowledge ───────────────────────────────────────────────────
    ack_text = (
        f"🔍 در حال یافتن تصاویر برای «{raw_query}»..." if fa
        else f"🔍 Finding images for *{raw_query}*…"
    )
    ack = await message.reply_text(ack_text, parse_mode=ParseMode.MARKDOWN)

    # ── Step 2: Get profile + AI expansion (in thread) ────────────────────────
    profile = get_profile(user_id)
    expansion = await asyncio.to_thread(ai_expand_query, raw_query, profile)
    sub_queries    = expansion["sub_queries"]
    new_tags       = expansion["tags"]
    display_theme  = expansion["display_theme"]

    log.info("User %s | query='%s' | sub_queries=%s | tags=%s",
             user_id, raw_query, sub_queries, new_tags)

    # ── Step 3: Scrape all sub-queries in parallel ────────────────────────────
    all_urls = await asyncio.to_thread(scrape_all_subqueries, sub_queries)
    log.info("Total URLs found: %d", len(all_urls))

    if not all_urls:
        await ack.edit_text(
            "😕 هیچ تصویری پیدا نشد. با کلمات دیگری امتحان کنید." if fa
            else "😕 No images found. Try different keywords!"
        )
        return

    # ── Step 4: Pick fresh images for this user ───────────────────────────────
    chosen = pick_fresh(user_id, base_key, all_urls, count=4)

    if not chosen:
        await ack.edit_text(
            "🔄 تمام تصاویر این موضوع قبلاً ارسال شده‌اند!" if fa
            else "🔄 You've seen all images for this topic! Try a slightly different search."
        )
        return

    # ── Step 5: Update profile ────────────────────────────────────────────────
    for url in chosen:
        mark_sent(user_id, base_key, url)
    update_profile(user_id, raw_query, new_tags)

    # ── Step 6: Send photos ───────────────────────────────────────────────────
    await ack.delete()

    caption = f"📌 {display_theme}" + (" • پینیفای" if fa else " • Pinify")
    media = [InputMediaPhoto(media=url) for url in chosen]
    media[0] = InputMediaPhoto(media=chosen[0], caption=caption)

    try:
        await message.reply_media_group(media=media)
    except Exception as exc:
        log.error("Media group failed: %s", exc)
        for i, url in enumerate(chosen):
            try:
                await message.reply_photo(photo=url, caption=caption if i == 0 else None)
            except Exception as e2:
                log.error("Single photo failed: %s", e2)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",   "Welcome & add to group"),
        BotCommand("profile", "View your taste profile"),
    ])
    log.info("Pinify v2 ready.")


# ── Entry ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set the BOT_TOKEN environment variable!")
    if not ANTHROPIC_KEY:
        log.warning("No ANTHROPIC_API_KEY — AI expansion disabled, using simple fallback.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pinme))

    log.info("🌸 Pinify v2 running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
