"""
Pinify Bot v3 — Pinterest-style Telegram Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tiers:
  • Free  — 15 searches/day, 4 photos, standard AI
  • ProPin — unlimited, 5 photos, deeper AI, badge 🌟, special tag

Payment: 60 Telegram Stars via native Stars invoice
"""

import os, logging, sqlite3, random, asyncio, re, json, time
import urllib.request, urllib.parse, concurrent.futures
from datetime import datetime, timezone

import anthropic

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, InputMediaPhoto,
    LabeledPrice, PreCheckoutQuery,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, PreCheckoutQueryHandler,
    MessageHandler as MH, ContextTypes, filters,
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DB_PATH       = "pinify.db"

FREE_DAILY_LIMIT   = 15
PRO_PHOTO_COUNT    = 5
FREE_PHOTO_COUNT   = 4
PRO_STARS_PRICE    = 60          # Telegram Stars
PRO_DURATION_DAYS  = 30

BADGE_FREE = ""           # no badge
BADGE_PRO  = "🌟"         # ProPin badge shown in profile & captions
TAG_FREE   = "Free"
TAG_PRO    = "ProPin"

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("pinify")
ai  = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sent_images (
            user_id TEXT, query TEXT, url TEXT,
            PRIMARY KEY (user_id, query, url)
        );
        CREATE TABLE IF NOT EXISTS user_profile (
            user_id        TEXT PRIMARY KEY,
            search_history TEXT DEFAULT '[]',
            taste_tags     TEXT DEFAULT '[]',
            daily_count    INTEGER DEFAULT 0,
            day_stamp      TEXT DEFAULT '',
            is_pro         INTEGER DEFAULT 0,
            pro_until      INTEGER DEFAULT 0,
            total_searches INTEGER DEFAULT 0,
            updated_at     INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    return conn

DB = _db()

def _get_row(user_id: str) -> dict:
    row = DB.execute("SELECT * FROM user_profile WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        DB.execute("INSERT INTO user_profile (user_id) VALUES (?)", (user_id,))
        DB.commit()
        return _get_row(user_id)
    cols = [d[0] for d in DB.execute("SELECT * FROM user_profile WHERE user_id=?", (user_id,)).description]
    return dict(zip(cols, row))

def _set(user_id: str, **kwargs):
    sets = ", ".join(f"{k}=?" for k in kwargs)
    DB.execute(f"UPDATE user_profile SET {sets} WHERE user_id=?", (*kwargs.values(), user_id))
    DB.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Subscription helpers
# ─────────────────────────────────────────────────────────────────────────────
def is_pro(user_id: str) -> bool:
    row = _get_row(user_id)
    if not row["is_pro"]:
        return False
    if int(time.time()) > row["pro_until"]:
        _set(user_id, is_pro=0, pro_until=0)   # expired
        return False
    return True

def activate_pro(user_id: str):
    now = int(time.time())
    row = _get_row(user_id)
    base = max(now, row["pro_until"])           # stack on top of existing sub
    _set(user_id, is_pro=1, pro_until=base + PRO_DURATION_DAYS * 86400)

def pro_until_str(user_id: str) -> str:
    row = _get_row(user_id)
    if not row["is_pro"]:
        return ""
    dt = datetime.fromtimestamp(row["pro_until"], tz=timezone.utc)
    return dt.strftime("%b %d, %Y")

def check_and_increment_usage(user_id: str) -> tuple[bool, int]:
    """Returns (allowed, remaining). Resets counter at midnight UTC."""
    if is_pro(user_id):
        row = _get_row(user_id)
        _set(user_id, total_searches=row["total_searches"] + 1)
        return True, 999

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row   = _get_row(user_id)

    count = row["daily_count"] if row["day_stamp"] == today else 0
    if count >= FREE_DAILY_LIMIT:
        return False, 0

    _set(user_id,
         daily_count=count + 1,
         day_stamp=today,
         total_searches=row["total_searches"] + 1)
    return True, FREE_DAILY_LIMIT - count - 1

def usage_remaining(user_id: str) -> int:
    if is_pro(user_id):
        return 999
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row   = _get_row(user_id)
    count = row["daily_count"] if row["day_stamp"] == today else 0
    return max(0, FREE_DAILY_LIMIT - count)

# ─────────────────────────────────────────────────────────────────────────────
# Taste profile
# ─────────────────────────────────────────────────────────────────────────────
def get_profile(user_id: str) -> dict:
    row = _get_row(user_id)
    return {
        "history":    json.loads(row["search_history"]),
        "taste_tags": json.loads(row["taste_tags"]),
    }

def update_profile(user_id: str, query: str, new_tags: list[str]):
    profile  = get_profile(user_id)
    history  = (profile["history"] + [query])[-30:]
    merged   = list(dict.fromkeys(new_tags + profile["taste_tags"]))[:40]
    _set(user_id,
         search_history=json.dumps(history),
         taste_tags=json.dumps(merged),
         updated_at=int(time.time()))

def reset_profile(user_id: str):
    DB.execute("DELETE FROM sent_images WHERE user_id=?", (user_id,))
    DB.execute("""UPDATE user_profile SET
        search_history='[]', taste_tags='[]',
        daily_count=0, day_stamp='', total_searches=0
        WHERE user_id=?""", (user_id,))
    DB.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Claude AI — query expansion (Pro gets deeper, more sub-queries)
# ─────────────────────────────────────────────────────────────────────────────
def ai_expand_query(raw_query: str, profile: dict, pro: bool) -> dict:
    if not ai:
        return _simple_expand(raw_query, pro)

    history_str = ", ".join(profile["history"][-12:]) or "none yet"
    tags_str    = ", ".join(profile["taste_tags"][:25]) or "none yet"
    n_queries   = 6 if pro else 4

    prompt = f"""You are the AI search engine inside Pinify, a Pinterest bot.

User query: "{raw_query}"
Recent searches: {history_str}
Taste profile tags: {tags_str}
Tier: {"ProPin (premium)" if pro else "Free"}

Generate {n_queries} diverse Pinterest-style image search sub-queries in ENGLISH.
{"Since this is a ProPin user, go deeper — use niche aesthetic terminology, sub-cultures, specific visual styles, colour palettes, lighting moods, and era references to surface the most curated, accurate images possible." if pro else "Make them specific and visual with aesthetic terminology."}

Each sub-query should approach the topic from a different angle.
Personalise based on their taste profile if available.

Also extract 4-8 short taste tags from this search.
Write a 2-5 word display theme label.

Respond ONLY with valid JSON:
{{
  "sub_queries": ["{n_queries} strings"],
  "tags": ["tag1","tag2","..."],
  "display_theme": "short vibe label"
}}"""

    try:
        resp = ai.messages.create(
            model="claude-haiku-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = re.sub(r"^```[a-z]*\n?|```$", "", resp.content[0].text.strip(), flags=re.MULTILINE).strip()
        data = json.loads(text)
        return {
            "sub_queries":    data.get("sub_queries", [raw_query]),
            "tags":           data.get("tags", []),
            "display_theme":  data.get("display_theme", raw_query),
        }
    except Exception as exc:
        log.warning("Claude expand failed: %s", exc)
        return _simple_expand(raw_query, pro)

def _simple_expand(query: str, pro: bool) -> dict:
    b = query.strip()
    base = [f"{b} aesthetic", f"{b} pinterest", f"{b} inspo", f"{b} mood board"]
    if pro:
        base += [f"{b} photography", f"{b} editorial"]
    return {"sub_queries": base, "tags": [b], "display_theme": b}

# ─────────────────────────────────────────────────────────────────────────────
# Scraper (DDG JSON → DDG HTML → Bing)
# ─────────────────────────────────────────────────────────────────────────────
HDR = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.5", "DNT": "1",
}
PINIMG_RE = re.compile(r'https://i\.pinimg\.com/[^\s"\'\\><]+\.(?:jpg|jpeg|png|webp)', re.I)

def _up(url: str) -> str:
    return re.sub(r'/\d+x(?:/|\.)', '/736x/', url)

def _ddg_vqd(q: str) -> str:
    req = urllib.request.Request(
        "https://duckduckgo.com/?q=" + urllib.parse.quote(q) + "&iax=images&ia=images", headers=HDR)
    with urllib.request.urlopen(req, timeout=10) as r:
        html = r.read().decode("utf-8", errors="ignore")
    m = re.search(r'vqd=(["\'])([^"\']+)\1', html) or re.search(r'vqd=([\d\-]+)', html)
    return (m.group(2) if m and m.lastindex == 2 else m.group(1)) if m else ""

def _ddg_json(q: str) -> list[str]:
    try:
        vqd = _ddg_vqd(f"{q} site:pinterest.com")
        if not vqd: return []
        params = urllib.parse.urlencode({"l":"us-en","o":"json","q":f"{q} site:pinterest.com","vqd":vqd,"f":",,,,,","p":"1"})
        req = urllib.request.Request("https://duckduckgo.com/i.js?" + params,
                                     headers={**HDR, "Referer":"https://duckduckgo.com/"})
        time.sleep(0.25)
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
        urls = []
        for item in data.get("results", []):
            for k in ("image", "thumbnail"):
                v = item.get(k, "")
                if v and "pinimg.com" in v:
                    urls.append(_up(v)); break
        return urls
    except Exception as e:
        log.debug("DDG JSON fail '%s': %s", q, e); return []

def _ddg_html(q: str) -> list[str]:
    try:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(f"{q} site:pinterest.com")
        with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=12) as r:
            return [_up(u) for u in PINIMG_RE.findall(r.read().decode("utf-8","ignore"))]
    except Exception as e:
        log.debug("DDG HTML fail '%s': %s", q, e); return []

def _bing(q: str) -> list[str]:
    try:
        params = urllib.parse.urlencode({"q":f"{q} site:pinterest.com","form":"HDRSC2","first":"1"})
        req = urllib.request.Request("https://www.bing.com/images/search?" + params,
            headers={**HDR,"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return [_up(u) for u in PINIMG_RE.findall(r.read().decode("utf-8","ignore"))]
    except Exception as e:
        log.debug("Bing fail '%s': %s", q, e); return []

def scrape_one(q: str) -> list[str]:
    urls = _ddg_json(q)
    if len(urls) < 6: urls += _ddg_html(q)
    if len(urls) < 6: urls += _bing(q)
    return list(dict.fromkeys(urls))

def scrape_all(sub_queries: list[str]) -> list[str]:
    results: list[list[str]] = [[] for _ in sub_queries]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(scrape_one, q): i for i, q in enumerate(sub_queries)}
        for f in concurrent.futures.as_completed(futs):
            try: results[futs[f]] = f.result()
            except: pass
    # interleave for max variety
    out = []
    for i in range(max((len(r) for r in results), default=0)):
        for r in results:
            if i < len(r): out.append(r[i])
    return list(dict.fromkeys(out))

def pick_fresh(user_id: str, key: str, urls: list[str], count: int) -> list[str]:
    random.shuffle(urls)
    chosen = []
    for url in urls:
        if len(chosen) >= count: break
        cur = DB.execute("SELECT 1 FROM sent_images WHERE user_id=? AND query=? AND url=?",
                         (user_id, key, url)).fetchone()
        if not cur:
            chosen.append(url)
    return chosen

def mark_sent(user_id: str, key: str, urls: list[str]):
    for url in urls:
        try:
            DB.execute("INSERT INTO sent_images (user_id,query,url) VALUES (?,?,?)", (user_id, key, url))
        except sqlite3.IntegrityError:
            pass
    DB.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def detect_pinme(text: str) -> str | None:
    m = re.match(r"^(?:pinme|پینمی)\s+(.+)$", text.strip(), re.I | re.DOTALL)
    return m.group(1).strip() if m else None

def is_fa(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text))

def tier_label(user_id: str, fa: bool) -> str:
    if is_pro(user_id):
        return f"{BADGE_PRO} ProPin" if not fa else f"{BADGE_PRO} پرو‌پین"
    return TAG_FREE if not fa else "رایگان"

# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    me = (await ctx.bot.get_me()).username
    uid = str(update.effective_user.id)
    _get_row(uid)  # ensure row exists
    pro = is_pro(uid)

    badge = f"{BADGE_PRO} " if pro else ""
    tier  = "ProPin" if pro else "Free"

    kb = [
        [InlineKeyboardButton("➕ Add to a group", url=f"https://t.me/{me}?startgroup=true")],
        [InlineKeyboardButton("🎨 My Profile", callback_data="profile"),
         InlineKeyboardButton("⭐ Go ProPin", callback_data="buy_pro")],
    ]
    msg = (
        f"📌 *Welcome to Pinify!* {badge}\n"
        f"_Your tier: {tier}_\n\n"
        "I find Pinterest photos for any vibe and learn your taste over time.\n\n"
        "🇬🇧 `pinme <anything>`\n"
        "🇮🇷 `پینمی <هر چیزی>`\n\n"
        f"*Free tier:* {FREE_DAILY_LIMIT} searches/day · 4 photos\n"
        f"*{BADGE_PRO} ProPin:* Unlimited · 5 photos · Deeper AI · Badge & special tag"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))

# ─────────────────────────────────────────────────────────────────────────────
# /profile
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = str(update.effective_user.id)
    pro  = is_pro(uid)
    row  = _get_row(uid)
    prof = get_profile(uid)

    badge     = f"{BADGE_PRO} " if pro else ""
    tier_txt  = f"{BADGE_PRO} *ProPin*  •  expires {pro_until_str(uid)}" if pro else "*Free*  •  /upgrade to ProPin"
    remaining = usage_remaining(uid)
    total     = row["total_searches"]

    history_txt = " · ".join(f"`{h}`" for h in prof["history"][-8:]) or "_none yet_"
    tags_txt    = "  ".join(f"#{t}" for t in prof["taste_tags"][:20]) or "_none yet_"

    msg = (
        f"🎨 *{badge}Your Pinify Profile*\n\n"
        f"Tier: {tier_txt}\n"
        f"Searches today: `{FREE_DAILY_LIMIT - remaining if not pro else total}` "
        f"{'/ ∞' if pro else f'/ {FREE_DAILY_LIMIT}'}\n"
        f"Total searches: `{total}`\n\n"
        f"*Recent searches:*\n{history_txt}\n\n"
        f"*Your aesthetic tags:*\n{tags_txt}"
    )
    kb = [
        [InlineKeyboardButton("⭐ Go ProPin", callback_data="buy_pro"),
         InlineKeyboardButton("🔄 Reset", callback_data="reset_confirm")],
        [InlineKeyboardButton("« Back", callback_data="back_start")],
    ]
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))

# ─────────────────────────────────────────────────────────────────────────────
# /upgrade  (shows Stars payment)
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_upgrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if is_pro(uid):
        until = pro_until_str(uid)
        await update.message.reply_text(
            f"✅ You're already *{BADGE_PRO} ProPin* until *{until}*!\n\n"
            "Sending another payment will extend your subscription by 30 more days.",
            parse_mode=ParseMode.MARKDOWN,
        )
    await _send_stars_invoice(update.message.chat_id, ctx)

async def _send_stars_invoice(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_invoice(
        chat_id=chat_id,
        title=f"{BADGE_PRO} Pinify ProPin — 30 Days",
        description=(
            "✅ Unlimited searches\n"
            "✅ 5 photos per search (vs 4)\n"
            "✅ Deeper AI personalisation\n"
            f"✅ {BADGE_PRO} ProPin badge & tag\n"
            "✅ 30-day subscription (stackable)"
        ),
        payload="propinsub_30d",
        currency="XTR",                     # Telegram Stars currency code
        prices=[LabeledPrice(label=f"{BADGE_PRO} ProPin 30 Days", amount=PRO_STARS_PRICE)],
        provider_token="",                  # empty = Stars payment
    )

# ─────────────────────────────────────────────────────────────────────────────
# Stars payment handlers
# ─────────────────────────────────────────────────────────────────────────────
async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q: PreCheckoutQuery = update.pre_checkout_query
    if q.invoice_payload == "propinsub_30d":
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Unknown product.")

async def successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    activate_pro(uid)
    until = pro_until_str(uid)
    await update.message.reply_text(
        f"🎉 *Welcome to {BADGE_PRO} ProPin!*\n\n"
        f"Your subscription is active until *{until}*.\n\n"
        "You now have:\n"
        "• Unlimited daily searches\n"
        "• 5 photos per search\n"
        "• Deeper AI personalization\n"
        f"• Your {BADGE_PRO} ProPin badge\n\n"
        "Try it now: `pinme dark academia`",
        parse_mode=ParseMode.MARKDOWN,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Button callbacks
# ─────────────────────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid  = str(q.from_user.id)
    pro  = is_pro(uid)
    me   = (await ctx.bot.get_me()).username

    if q.data == "buy_pro":
        await q.message.reply_text("⬇️ Here's your ProPin payment:")
        await _send_stars_invoice(q.message.chat_id, ctx)

    elif q.data == "profile":
        row  = _get_row(uid)
        prof = get_profile(uid)
        badge     = f"{BADGE_PRO} " if pro else ""
        tier_txt  = f"{BADGE_PRO} *ProPin*  •  expires {pro_until_str(uid)}" if pro else "*Free*"
        remaining = usage_remaining(uid)
        total     = row["total_searches"]
        history_txt = " · ".join(f"`{h}`" for h in prof["history"][-8:]) or "_none yet_"
        tags_txt    = "  ".join(f"#{t}" for t in prof["taste_tags"][:20]) or "_none yet_"
        msg = (
            f"🎨 *{badge}Your Pinify Profile*\n\n"
            f"Tier: {tier_txt}\n"
            f"Searches today: `{FREE_DAILY_LIMIT - remaining if not pro else total}` "
            f"{'/ ∞' if pro else f'/ {FREE_DAILY_LIMIT}'}\n"
            f"Total searches: `{total}`\n\n"
            f"*Recent searches:*\n{history_txt}\n\n"
            f"*Your aesthetic tags:*\n{tags_txt}"
        )
        await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Go ProPin", callback_data="buy_pro"),
                 InlineKeyboardButton("🔄 Reset", callback_data="reset_confirm")],
                [InlineKeyboardButton("« Back", callback_data="back_start")],
            ]))

    elif q.data == "reset_confirm":
        await q.edit_message_text(
            "⚠️ *Reset profile?*\n\nClears your taste history and sent-image memory. Cannot be undone.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, reset", callback_data="reset_do"),
                 InlineKeyboardButton("❌ Cancel", callback_data="back_start")],
            ]))

    elif q.data == "reset_do":
        reset_profile(uid)
        await q.edit_message_text("✅ *Profile reset!* Fresh start.", parse_mode=ParseMode.MARKDOWN)

    elif q.data == "back_start":
        badge    = f"{BADGE_PRO} " if pro else ""
        tier_lbl = "ProPin" if pro else "Free"
        kb = [
            [InlineKeyboardButton("➕ Add to a group", url=f"https://t.me/{me}?startgroup=true")],
            [InlineKeyboardButton("🎨 My Profile", callback_data="profile"),
             InlineKeyboardButton("⭐ Go ProPin", callback_data="buy_pro")],
        ]
        await q.edit_message_text(
            f"📌 *Pinify* {badge}| _Tier: {tier_lbl}_\n\n"
            "Type `pinme <anything>` to search Pinterest.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb))

# ─────────────────────────────────────────────────────────────────────────────
# Core search handler
# ─────────────────────────────────────────────────────────────────────────────
async def handle_pinme(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.channel_post
    if not message or not message.text:
        return

    raw_query = detect_pinme(message.text)
    if raw_query is None:
        return

    uid      = str(message.from_user.id if message.from_user else message.chat_id)
    base_key = raw_query.strip().lower()
    fa       = is_fa(message.text)
    pro      = is_pro(uid)

    # ── Usage check ──────────────────────────────────────────────────────────
    allowed, remaining = check_and_increment_usage(uid)
    if not allowed:
        low_warn = (
            "🚫 *به محدودیت روزانه رسیدید!*\n\n"
            f"کاربران رایگان روزانه {FREE_DAILY_LIMIT} جستجو دارند.\n\n"
            f"برای جستجوی نامحدود، {BADGE_PRO} *ProPin* تهیه کنید:"
        ) if fa else (
            f"🚫 *Daily limit reached!*\n\n"
            f"Free users get {FREE_DAILY_LIMIT} searches per day.\n\n"
            f"Upgrade to {BADGE_PRO} *ProPin* for unlimited searches:"
        )
        await message.reply_text(low_warn, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"⭐ Get ProPin — {PRO_STARS_PRICE} Stars", callback_data="buy_pro")
            ]]))
        return

    # Low-usage warning for free users
    warn_msg = None
    if not pro and remaining <= 3:
        warn_msg = (
            f"⚠️ _{remaining} جستجوی رایگان امروز باقی مانده._" if fa
            else f"⚠️ _{remaining} free search{'es' if remaining != 1 else ''} left today._"
        )

    # ── Acknowledge ───────────────────────────────────────────────────────────
    badge_prefix = f"{BADGE_PRO} " if pro else ""
    ack_text = (
        f"{badge_prefix}🔍 در حال یافتن تصاویر برای «{raw_query}»..." if fa
        else f"{badge_prefix}🔍 Finding images for *{raw_query}*…"
    )
    ack = await message.reply_text(ack_text, parse_mode=ParseMode.MARKDOWN)

    # ── AI expansion ──────────────────────────────────────────────────────────
    profile    = get_profile(uid)
    expansion  = await asyncio.to_thread(ai_expand_query, raw_query, profile, pro)
    sub_q      = expansion["sub_queries"]
    new_tags   = expansion["tags"]
    theme      = expansion["display_theme"]
    log.info("uid=%s pro=%s query='%s' subs=%s", uid, pro, raw_query, sub_q)

    # ── Scrape ────────────────────────────────────────────────────────────────
    all_urls = await asyncio.to_thread(scrape_all, sub_q)
    log.info("Found %d URLs total", len(all_urls))

    if not all_urls:
        await ack.edit_text(
            "😕 هیچ تصویری پیدا نشد. کلمات دیگری امتحان کنید." if fa
            else "😕 No images found. Try different keywords!"
        )
        return

    # ── Pick fresh ────────────────────────────────────────────────────────────
    photo_count = PRO_PHOTO_COUNT if pro else FREE_PHOTO_COUNT
    chosen      = pick_fresh(uid, base_key, all_urls, photo_count)

    if not chosen:
        await ack.edit_text(
            "🔄 تمام تصاویر این موضوع قبلاً ارسال شده‌اند!" if fa
            else "🔄 You've seen all images for this topic — try a slightly different search."
        )
        return

    # ── Persist ───────────────────────────────────────────────────────────────
    mark_sent(uid, base_key, chosen)
    update_profile(uid, raw_query, new_tags)

    # ── Send ──────────────────────────────────────────────────────────────────
    await ack.delete()

    tier_tag = f"{BADGE_PRO} ProPin" if pro else "Pinify"
    caption  = f"📌 {theme}  •  {tier_tag}"
    if fa:
        caption = f"📌 {theme}  •  {'پرو‌پین ' + BADGE_PRO if pro else 'پینیفای'}"

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

    # Low usage warning (after photos)
    if warn_msg:
        await message.reply_text(
            warn_msg + ("\n\n👉 /upgrade" if not fa else "\n\n👉 /upgrade"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"⭐ ProPin — {PRO_STARS_PRICE} Stars", callback_data="buy_pro")
            ]]))

# ─────────────────────────────────────────────────────────────────────────────
# Boot
# ─────────────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",   "Welcome & tier info"),
        BotCommand("profile", "Your taste profile & usage"),
        BotCommand("upgrade", f"Get {BADGE_PRO} ProPin"),
    ])
    log.info("Pinify v3 ready 🌸")

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN env var!")
    if not ANTHROPIC_KEY:
        log.warning("No ANTHROPIC_API_KEY — AI expansion disabled.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pinme))

    log.info("🌸 Pinify v3 running")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
