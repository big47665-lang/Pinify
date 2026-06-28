"""
Pinify Bot v6
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Changes from v3:
  • FIXED: Stars invoice now sends a real payment button (pay=True)
  • NEW: OWNER_ID env var — owner has permanent ProPin, never expires
  • NEW: /giftpro @username or /giftpro <user_id> — owner can gift ProPin
  • NEW: /admin panel for owner — list pro users, revoke, check stats
"""

import os, logging, sqlite3, random, asyncio, re, json, time
import urllib.request, urllib.parse, concurrent.futures
from datetime import datetime, timezone

import anthropic

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, InputMediaPhoto, LabeledPrice,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, PreCheckoutQueryHandler,
    ContextTypes, filters,
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────────────────────────────────────
# Config  (set these as Railway env vars)
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OWNER_ID      = os.getenv("OWNER_ID", "").strip()  # your Telegram numeric user ID
DB_PATH       = "pinify.db"

FREE_DAILY_LIMIT  = 15
FREE_PHOTO_COUNT  = 4
PRO_PHOTO_COUNT   = 5
PRO_STARS_PRICE   = 60
PRO_DURATION_DAYS = 30
BADGE_PRO         = "🌟"

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
            pro_gifted_by  TEXT DEFAULT '',
            total_searches INTEGER DEFAULT 0,
            updated_at     INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    return conn

DB = _db()

def _get_row(user_id: str) -> dict:
    cur = DB.execute("SELECT * FROM user_profile WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        DB.execute("INSERT INTO user_profile (user_id) VALUES (?)", (user_id,))
        DB.commit()
        cur = DB.execute("SELECT * FROM user_profile WHERE user_id=?", (user_id,))
        row = cur.fetchone()
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))

def _set(user_id: str, **kwargs):
    sets = ", ".join(f"{k}=?" for k in kwargs)
    DB.execute(f"UPDATE user_profile SET {sets} WHERE user_id=?", (*kwargs.values(), user_id))
    DB.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Owner helpers
# ─────────────────────────────────────────────────────────────────────────────
def is_owner(user_id: str) -> bool:
    return bool(OWNER_ID) and str(user_id) == str(OWNER_ID)

# ─────────────────────────────────────────────────────────────────────────────
# Subscription
# ─────────────────────────────────────────────────────────────────────────────
def is_pro(user_id: str) -> bool:
    """Owner always has pro. Others check expiry."""
    if is_owner(user_id):
        return True
    row = _get_row(user_id)
    if not row["is_pro"]:
        return False
    if int(time.time()) > row["pro_until"]:
        _set(user_id, is_pro=0, pro_until=0)
        return False
    return True

def activate_pro(user_id: str, gifted_by: str = ""):
    now  = int(time.time())
    row  = _get_row(user_id)
    base = max(now, row["pro_until"])
    _set(user_id,
         is_pro=1,
         pro_until=base + PRO_DURATION_DAYS * 86400,
         pro_gifted_by=gifted_by)

def revoke_pro(user_id: str):
    _set(user_id, is_pro=0, pro_until=0, pro_gifted_by="")

def pro_until_str(user_id: str) -> str:
    if is_owner(user_id):
        return "∞ (owner)"
    row = _get_row(user_id)
    if not row["is_pro"]:
        return ""
    return datetime.fromtimestamp(row["pro_until"], tz=timezone.utc).strftime("%b %d, %Y")

def check_and_increment_usage(user_id: str) -> tuple[bool, int]:
    if is_pro(user_id):
        if not is_owner(user_id):
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
    return {"history": json.loads(row["search_history"]),
            "taste_tags": json.loads(row["taste_tags"])}

def update_profile(user_id: str, query: str, new_tags: list[str]):
    p = get_profile(user_id)
    history = (p["history"] + [query])[-30:]
    merged  = list(dict.fromkeys(new_tags + p["taste_tags"]))[:40]
    _set(user_id, search_history=json.dumps(history),
         taste_tags=json.dumps(merged), updated_at=int(time.time()))

def reset_profile(user_id: str):
    DB.execute("DELETE FROM sent_images WHERE user_id=?", (user_id,))
    DB.execute("""UPDATE user_profile SET search_history='[]', taste_tags='[]',
        daily_count=0, day_stamp='', total_searches=0 WHERE user_id=?""", (user_id,))
    DB.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Claude AI — query expansion
# ─────────────────────────────────────────────────────────────────────────────
def ai_expand_query(raw_query: str, profile: dict, pro: bool) -> dict:
    if not ai:
        return _simple_expand(raw_query, pro)
    n  = 6 if pro else 4
    hs = ", ".join(profile["history"][-12:]) or "none yet"
    ts = ", ".join(profile["taste_tags"][:25]) or "none yet"
    prompt = f"""You are the AI search engine inside Pinify, a Pinterest bot.

User query: "{raw_query}"
Recent searches: {hs}
Taste profile tags: {ts}
Tier: {"ProPin (premium)" if pro else "Free"}

Generate {n} Pinterest search sub-queries following these CRITICAL ACCURACY RULES:
1. NEVER change the core subject. Every sub-query must still be exactly about what the user asked.
2. Only vary: aesthetic adjectives, synonyms, visual descriptors, medium, or mood words that still apply.
3. Keep the original keywords in most sub-queries. Exact phrasing works best on Pinterest.
4. If the query is a niche aesthetic (frutiger aero, dark academia, cottagecore, etc.) keep it verbatim in every sub-query.
5. {"ProPin: add 1-2 niche sub-queries using highly specific terminology real pinners use as board names or pin titles." if pro else "Stay close to the original — do not drift into related but different topics."}

GOOD example for 'frutiger aero night': ["frutiger aero night", "frutiger aero night aesthetic", "frutiger aero dark night", "frutiger aero night wallpaper"]
BAD example — NEVER do this: ["Y2K night aesthetic", "retro digital dark art", "2000s computer vibes"]

Personalise the sub-queries to their taste profile only if it doesn't change the subject.
Extract 3-6 short taste tags from this search. Write a 2-5 word display theme label.

Respond ONLY with valid JSON (no markdown):
{{"sub_queries":["..."],"tags":["..."],"display_theme":"..."}}"""
    try:
        resp = ai.messages.create(
            model="claude-haiku-4-5", max_tokens=500,
            messages=[{"role": "user", "content": prompt}])
        text = re.sub(r"^```[a-z]*\n?|```$", "",
                      resp.content[0].text.strip(), flags=re.MULTILINE).strip()
        d = json.loads(text)
        return {"sub_queries": d.get("sub_queries", [raw_query]),
                "tags": d.get("tags", []),
                "display_theme": d.get("display_theme", raw_query)}
    except Exception as e:
        log.warning("Claude expand failed: %s", e)
        return _simple_expand(raw_query, pro)

def _simple_expand(q: str, pro: bool) -> dict:
    b = q.strip()
    s = [f"{b} aesthetic", f"{b} pinterest", f"{b} inspo", f"{b} mood board"]
    if pro: s += [f"{b} photography", f"{b} editorial"]
    return {"sub_queries": s, "tags": [b], "display_theme": b}

# ─────────────────────────────────────────────────────────────────────────────
# Scraper — 4-layer Pinterest-accurate approach
# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: Pinterest's own internal BaseSearchResource API (exact browser match)
# Layer 2: Pinterest search page HTML — parse the embedded JSON state blob
# Layer 3: Google Images filtered to i.pinimg.com CDN
# Layer 4: DuckDuckGo fallback
# ─────────────────────────────────────────────────────────────────────────────

PIN_RE   = re.compile(r'https://i\.pinimg\.com/[^\s"\'\\><]+\.(?:jpg|jpeg|png|webp)', re.I)
PIN_ORIG = re.compile(r'https://i\.pinimg\.com/originals/[^\s"\'\\><]+\.(?:jpg|jpeg|png|webp)', re.I)

HDR_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "DNT": "1",
    "Connection": "keep-alive",
}
HDR_XHR = {
    **HDR_BROWSER,
    "Accept": "application/json, text/javascript, */*, q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.pinterest.com/",
}

def _up(u):
    return re.sub(r'/\d+x(?:/|\.)', '/736x/', u)

def _decompress(raw):
    import gzip
    try: return gzip.decompress(raw)
    except: return raw

def _extract_pinimg(text):
    originals = PIN_ORIG.findall(text)
    all_urls  = PIN_RE.findall(text)
    seen, out = set(), []
    for u in originals + all_urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def _layer1_pinterest_api(query):
    """Pinterest's own internal search API — same endpoint the website calls."""
    try:
        options = json.dumps({
            "options": {
                "query": query,
                "scope": "pins",
                "no_fetch_context_on_resource": False,
                "page_size": 50,
                "isPrefetch": False,
            },
            "context": {}
        })
        params = urllib.parse.urlencode({
            "source_url": "/search/pins/?q=" + urllib.parse.quote(query) + "&rs=typed",
            "data": options,
        })
        url = "https://www.pinterest.com/resource/BaseSearchResource/get/?" + params
        req = urllib.request.Request(url, headers=HDR_XHR)
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(_decompress(r.read()).decode("utf-8", errors="ignore"))
        results = (data.get("resource_response", {})
                       .get("data", {})
                       .get("results", []))
        urls = []
        for pin in results:
            images = pin.get("images", {})
            for size in ("orig", "736x", "564x", "474x"):
                img = images.get(size, {})
                u   = img.get("url", "")
                if u and "pinimg.com" in u:
                    urls.append(u); break
        log.info("L1 Pinterest API: %d urls for '%s'", len(urls), query)
        return urls
    except Exception as e:
        log.debug("L1 failed '%s': %s", query, e); return []

def _layer2_pinterest_html(query):
    """Fetch Pinterest search page and parse embedded JSON state blobs."""
    try:
        url = "https://www.pinterest.com/search/pins/?q=" + urllib.parse.quote(query) + "&rs=typed"
        req = urllib.request.Request(url, headers=HDR_BROWSER)
        with urllib.request.urlopen(req, timeout=14) as r:
            html = _decompress(r.read()).decode("utf-8", errors="ignore")
        urls = []
        for pat in (
            r'__PWS_INITIAL_PROPS__\s*=\s*(\{.+?\})\s*</script>',
            r'__PWS_DATA__\s*=\s*(\{.+?\})\s*</script>',
            r'"resource_response"\s*:\s*(\{.+?"results".+?\})\s*[,}]',
        ):
            for m in re.finditer(pat, html, re.DOTALL):
                try: urls += _extract_pinimg(m.group(1))
                except: pass
        if not urls:
            urls = _extract_pinimg(html)
        log.info("L2 Pinterest HTML: %d urls for '%s'", len(urls), query)
        return list(dict.fromkeys(urls))
    except Exception as e:
        log.debug("L2 failed '%s': %s", query, e); return []

def _layer3_google(query):
    """Google Images search filtered to Pinterest CDN — more accurate than DDG."""
    try:
        params = urllib.parse.urlencode({
            "q":   query + " site:pinterest.com",
            "tbm": "isch", "hl": "en", "gl": "us",
        })
        req = urllib.request.Request(
            "https://www.google.com/search?" + params,
            headers={**HDR_BROWSER, "Referer": "https://www.google.com/"})
        with urllib.request.urlopen(req, timeout=12) as r:
            html = _decompress(r.read()).decode("utf-8", errors="ignore")
        urls = []
        for m in re.finditer(r'AF_initDataCallback\(\{.*?data:(\[\[.*?\]\]).*?\}\)', html, re.DOTALL):
            try: urls += _extract_pinimg(m.group(1))
            except: pass
        if not urls:
            urls = _extract_pinimg(html)
        log.info("L3 Google images: %d urls for '%s'", len(urls), query)
        return list(dict.fromkeys(urls))
    except Exception as e:
        log.debug("L3 failed '%s': %s", query, e); return []

def _layer4_ddg(query):
    """DuckDuckGo image search — last resort fallback."""
    try:
        req = urllib.request.Request(
            "https://duckduckgo.com/?q=" + urllib.parse.quote(query + " site:pinterest.com") + "&iax=images&ia=images",
            headers=HDR_BROWSER)
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")
        m = re.search(r'vqd=(["\'\'])([^\"\']+)\1', html) or re.search(r'vqd=([\d\-]+)', html)
        if not m: return _extract_pinimg(html)
        vqd = m.group(2) if m.lastindex == 2 else m.group(1)
        p = urllib.parse.urlencode({"l":"us-en","o":"json",
            "q": query + " site:pinterest.com","vqd":vqd,"f":",,,,,","p":"1"})
        req2 = urllib.request.Request("https://duckduckgo.com/i.js?" + p,
            headers={**HDR_BROWSER, "Referer": "https://duckduckgo.com/"})
        time.sleep(0.2)
        with urllib.request.urlopen(req2, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8","ignore"))
        urls = []
        for item in data.get("results", []):
            for k in ("image","thumbnail"):
                v = item.get(k,"")
                if v and "pinimg.com" in v: urls.append(_up(v)); break
        log.info("L4 DDG: %d urls for '%s'", len(urls), query)
        return urls
    except Exception as e:
        log.debug("L4 failed '%s': %s", query, e); return []

def scrape_one(query):
    urls = _layer1_pinterest_api(query)
    if len(urls) < 8: urls += _layer2_pinterest_html(query)
    if len(urls) < 8: urls += _layer3_google(query)
    if len(urls) < 8: urls += _layer4_ddg(query)
    return list(dict.fromkeys(_up(u) for u in urls))

def scrape_all(sub_queries):
    results = [[] for _ in sub_queries]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(scrape_one, q): i for i, q in enumerate(sub_queries)}
        for f in concurrent.futures.as_completed(futs):
            try: results[futs[f]] = f.result()
            except Exception as e: log.warning("scrape_all error: %s", e)
    out = []
    for i in range(max((len(r) for r in results), default=0)):
        for r in results:
            if i < len(r): out.append(r[i])
    return list(dict.fromkeys(out))
def pick_fresh(uid, key, urls, count):
    random.shuffle(urls)
    chosen = []
    for url in urls:
        if len(chosen) >= count: break
        if not DB.execute("SELECT 1 FROM sent_images WHERE user_id=? AND query=? AND url=?",
                          (uid,key,url)).fetchone():
            chosen.append(url)
    return chosen

def mark_sent(uid, key, urls):
    for url in urls:
        try: DB.execute("INSERT INTO sent_images VALUES (?,?,?)",(uid,key,url))
        except sqlite3.IntegrityError: pass
    DB.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def detect_pinme(text):
    m = re.match(r"^(?:pinme|پینمی)\s+(.+)$", text.strip(), re.I|re.DOTALL)
    return m.group(1).strip() if m else None

def is_fa(text): return bool(re.search(r"[\u0600-\u06FF]", text))

# ─────────────────────────────────────────────────────────────────────────────
# Stars invoice  ← THE FIX: reply_markup with pay=True button
# ─────────────────────────────────────────────────────────────────────────────
async def send_invoice(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    """Send the Stars payment invoice with a proper Pay button."""
    pay_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⭐ Pay {PRO_STARS_PRICE} Stars", pay=True)
    ]])
    await ctx.bot.send_invoice(
        chat_id       = chat_id,
        title         = f"{BADGE_PRO} Pinify ProPin — 30 Days",
        description   = (
            "✅ Unlimited daily searches\n"
            "✅ 5 photos per search (vs 4)\n"
            "✅ Deeper AI personalisation\n"
            f"✅ {BADGE_PRO} ProPin badge & tag\n"
            "✅ 30 days (stackable)"
        ),
        payload       = "propinsub_30d",
        currency      = "XTR",
        prices        = [LabeledPrice(label=f"{BADGE_PRO} ProPin 30 Days",
                                      amount=PRO_STARS_PRICE)],
        reply_markup  = pay_kb,
    )

# ─────────────────────────────────────────────────────────────────────────────
# /whoami  — debug: shows your user ID and owner status
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = str(update.effective_user.id)
    name = update.effective_user.first_name or "?"
    owner_id_loaded = repr(OWNER_ID)
    match = is_owner(uid)
    lines = [
        "🔍 *Debug Info*",
        "",
        f"Your user ID: `{uid}`",
        f"Your name: {name}",
        "OWNER_ID in bot: `" + owner_id_loaded + "`",
        f"Owner match: `{match}`",
        f"Is pro: `{is_pro(uid)}`",
    ]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    me  = (await ctx.bot.get_me()).username
    uid = str(update.effective_user.id)
    _get_row(uid)
    pro   = is_pro(uid)
    owner = is_owner(uid)

    # Deep link: /start buypro  → immediately show invoice
    if ctx.args and ctx.args[0] == "buypro" and not pro and not owner:
        await update.message.reply_text(
            "⭐ *Get " + BADGE_PRO + " ProPin!*\n\nTap below to pay with Telegram Stars:",
            parse_mode=ParseMode.MARKDOWN)
        await send_invoice(update.message.chat_id, ctx)
        return
    badge = f"{BADGE_PRO} " if pro else ""
    tier  = ("Owner " + BADGE_PRO) if owner else ("ProPin " + BADGE_PRO if pro else "Free")

    kb = [
        [InlineKeyboardButton("➕ Add to a group", url=f"https://t.me/{me}?startgroup=true")],
        [InlineKeyboardButton("🎨 My Profile", callback_data="profile"),
         InlineKeyboardButton("⭐ Go ProPin", callback_data="buy_pro")],
    ]
    if owner:
        kb.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin")])

    await update.message.reply_text(
        f"📌 *Welcome to Pinify!* {badge}\n"
        f"_Your tier: {tier}_\n\n"
        "Find Pinterest photos for any vibe — I learn your taste over time.\n\n"
        "🇬🇧 `pinme <anything>`\n"
        "🇮🇷 `پینمی <هر چیزی>`\n\n"
        f"*Free:* {FREE_DAILY_LIMIT} searches/day · 4 photos\n"
        f"*{BADGE_PRO} ProPin:* Unlimited · 5 photos · Deeper AI · Badge",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb))

# ─────────────────────────────────────────────────────────────────────────────
# /profile
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = str(update.effective_user.id)
    await _send_profile(update.message.chat_id, uid, ctx, send_new=True)

async def _send_profile(chat_id, uid, ctx, send_new=False, edit_msg=None):
    pro   = is_pro(uid)
    owner = is_owner(uid)
    row   = _get_row(uid)
    prof  = get_profile(uid)

    badge    = f"{BADGE_PRO} " if pro else ""
    if owner:
        tier_txt = f"👑 *Owner* {BADGE_PRO}  •  permanent"
    elif pro:
        tier_txt = f"{BADGE_PRO} *ProPin*  •  expires {pro_until_str(uid)}"
        if row["pro_gifted_by"]:
            tier_txt += f"\n_Gifted by owner_"
    else:
        tier_txt = "*Free*"

    remaining = usage_remaining(uid)
    total     = row["total_searches"]
    used_today = FREE_DAILY_LIMIT - remaining if not pro else "∞"

    h_txt = " · ".join(f"`{h}`" for h in prof["history"][-8:]) or "_none yet_"
    t_txt = "  ".join(f"#{t}" for t in prof["taste_tags"][:20]) or "_none yet_"

    msg = (
        f"🎨 *{badge}Your Pinify Profile*\n\n"
        f"Tier: {tier_txt}\n"
        f"Searches today: `{used_today}` / {'∞' if pro else FREE_DAILY_LIMIT}\n"
        f"Total searches: `{total}`\n\n"
        f"*Recent searches:*\n{h_txt}\n\n"
        f"*Your aesthetic tags:*\n{t_txt}"
    )
    kb = [
        [InlineKeyboardButton("⭐ Go ProPin", callback_data="buy_pro"),
         InlineKeyboardButton("🔄 Reset", callback_data="reset_confirm")],
        [InlineKeyboardButton("« Back", callback_data="back_start")],
    ]
    if is_owner(uid):
        kb.insert(0, [InlineKeyboardButton("🛠 Admin Panel", callback_data="admin")])

    markup = InlineKeyboardMarkup(kb)
    if send_new:
        await ctx.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
    elif edit_msg:
        await edit_msg.edit_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)

# ─────────────────────────────────────────────────────────────────────────────
# /upgrade
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_upgrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if is_owner(uid):
        await update.message.reply_text("👑 You're the owner — ProPin is permanent for you!")
        return
    if is_pro(uid):
        await update.message.reply_text(
            f"✅ You're already *{BADGE_PRO} ProPin* until *{pro_until_str(uid)}*!\n"
            "Paying again adds 30 more days on top.",
            parse_mode=ParseMode.MARKDOWN)
    await send_invoice(update.message.chat_id, ctx)

# ─────────────────────────────────────────────────────────────────────────────
# /giftpro  (owner only)
# Usage: /giftpro @username   OR   /giftpro 123456789   OR reply to a message
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_giftpro(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not is_owner(uid):
        await update.message.reply_text("❌ Only the bot owner can gift ProPin.")
        return

    target_id   = None
    target_name = None

    # Case 1: reply to someone's message
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        u = update.message.reply_to_message.from_user
        target_id   = str(u.id)
        target_name = u.first_name

    # Case 2: /giftpro <user_id>  (numeric)
    elif ctx.args and ctx.args[0].lstrip("-").isdigit():
        target_id   = ctx.args[0]
        target_name = f"User {target_id}"

    # Case 3: /giftpro @username  (bot must have seen this user before)
    elif ctx.args and ctx.args[0].startswith("@"):
        await update.message.reply_text(
            "⚠️ Telegram doesn't let bots look up users by @username.\n\n"
            "Please either:\n"
            "• Forward a message from that person and reply to it with `/giftpro`\n"
            "• Use their numeric user ID: `/giftpro 123456789`\n\n"
            "_Tip: they can find their ID by messaging @userinfobot_",
            parse_mode=ParseMode.MARKDOWN)
        return
    else:
        await update.message.reply_text(
            "Usage:\n"
            "• Reply to their message: `/giftpro`\n"
            "• By user ID: `/giftpro 123456789`",
            parse_mode=ParseMode.MARKDOWN)
        return

    _get_row(target_id)  # ensure row exists
    activate_pro(target_id, gifted_by=uid)
    until = pro_until_str(target_id)

    await update.message.reply_text(
        f"🎁 *ProPin gifted!*\n\n"
        f"User: `{target_id}` ({target_name})\n"
        f"Active until: *{until}*",
        parse_mode=ParseMode.MARKDOWN)

    # Notify the recipient if possible
    try:
        await ctx.bot.send_message(
            chat_id=int(target_id),
            text=(
                f"🎁 *You received a ProPin gift!*\n\n"
                f"The bot owner has given you {BADGE_PRO} *ProPin* access.\n"
                f"Active until: *{until}*\n\n"
                "Enjoy unlimited searches, 5 photos per search, and deeper AI! 🌸"
            ),
            parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text(
            "_Note: couldn't notify the user (they may not have started the bot yet)._",
            parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────────────────────
# /revokepro  (owner only)
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_revokepro(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not is_owner(uid):
        await update.message.reply_text("❌ Owner only.")
        return
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: `/revokepro <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    target = ctx.args[0]
    revoke_pro(target)
    await update.message.reply_text(f"✅ ProPin revoked for `{target}`.", parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────────────────────
# /admin  (owner only)
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not is_owner(uid):
        await update.message.reply_text("❌ Owner only.")
        return
    await _send_admin(update.message.chat_id, ctx)

async def _send_admin(chat_id, ctx, edit_msg=None):
    now = int(time.time())
    pro_rows = DB.execute(
        "SELECT user_id, pro_until, pro_gifted_by, total_searches FROM user_profile WHERE is_pro=1"
    ).fetchall()
    total_users = DB.execute("SELECT COUNT(*) FROM user_profile").fetchone()[0]
    total_searches = DB.execute("SELECT SUM(total_searches) FROM user_profile").fetchone()[0] or 0

    lines = [f"🛠 *Pinify Admin Panel*\n",
             f"Total users: `{total_users}`",
             f"Total searches: `{total_searches}`",
             f"Active ProPin users: `{len(pro_rows)}`\n",
             "*ProPin users:*"]
    for row in pro_rows:
        uid2, until, gifted_by, searches = row
        until_dt = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%b %d, %Y")
        gift_note = " _(gifted)_" if gifted_by else ""
        lines.append(f"• `{uid2}` — until {until_dt}{gift_note} — {searches} searches")

    if not pro_rows:
        lines.append("_None yet_")

    msg = "\n".join(lines)
    kb  = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="back_start")]])

    if edit_msg:
        await edit_msg.edit_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await ctx.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ─────────────────────────────────────────────────────────────────────────────
# Payment handlers
# ─────────────────────────────────────────────────────────────────────────────
async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    await q.answer(ok=(q.invoice_payload == "propinsub_30d"),
                   error_message="Unknown product." if q.invoice_payload != "propinsub_30d" else "")

async def successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    activate_pro(uid)
    until = pro_until_str(uid)
    await update.message.reply_text(
        f"🎉 *Welcome to {BADGE_PRO} ProPin!*\n\n"
        f"Active until *{until}*.\n\n"
        "• Unlimited searches\n• 5 photos per search\n"
        "• Deeper AI\n• Your ProPin badge\n\n"
        "Try it: `pinme dark academia`",
        parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────────────────────
# Callback buttons
# ─────────────────────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)
    me  = (await ctx.bot.get_me()).username

    if q.data == "buy_pro":
        if is_owner(uid):
            await q.answer("👑 You're the owner — ProPin is permanent!", show_alert=True)
            return
        chat_type = q.message.chat.type
        if chat_type != "private":
            # Stars invoices only work in private DM chats
            me2 = (await ctx.bot.get_me()).username
            await q.answer("Payment only works in private chat!", show_alert=True)
            await q.message.reply_text(
                f"⭐ To buy ProPin, open a private chat with me:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💳 Pay in private chat",
                                         url=f"https://t.me/{me2}?start=buypro")
                ]]))
            return
        await q.message.reply_text("⬇️ Tap the button below to pay with Stars:")
        await send_invoice(q.message.chat_id, ctx)

    elif q.data == "profile":
        await _send_profile(q.message.chat_id, uid, ctx, edit_msg=q.message)

    elif q.data == "admin":
        if is_owner(uid):
            await _send_admin(q.message.chat_id, ctx, edit_msg=q.message)
        else:
            await q.answer("❌ Owner only.", show_alert=True)

    elif q.data == "reset_confirm":
        await q.edit_message_text(
            "⚠️ *Reset profile?*\n\nClears taste history and sent-image memory. Cannot be undone.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, reset", callback_data="reset_do"),
                InlineKeyboardButton("❌ Cancel", callback_data="back_start"),
            ]]))

    elif q.data == "reset_do":
        reset_profile(uid)
        await q.edit_message_text("✅ *Profile reset!* Fresh start.", parse_mode=ParseMode.MARKDOWN)

    elif q.data == "back_start":
        pro   = is_pro(uid)
        owner = is_owner(uid)
        badge = f"{BADGE_PRO} " if pro else ""
        tier  = ("Owner " + BADGE_PRO) if owner else ("ProPin " + BADGE_PRO if pro else "Free")
        kb = [
            [InlineKeyboardButton("➕ Add to a group", url=f"https://t.me/{me}?startgroup=true")],
            [InlineKeyboardButton("🎨 My Profile", callback_data="profile"),
             InlineKeyboardButton("⭐ Go ProPin", callback_data="buy_pro")],
        ]
        if owner:
            kb.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin")])
        await q.edit_message_text(
            f"📌 *Pinify* {badge}| _Tier: {tier}_\n\n"
            "Type `pinme <anything>` to search Pinterest.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb))

# ─────────────────────────────────────────────────────────────────────────────
# Core search
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

    # Usage check
    allowed, remaining = check_and_increment_usage(uid)
    if not allowed:
        txt = (
            f"🚫 *به محدودیت روزانه رسیدید!*\n\nکاربران رایگان روزانه {FREE_DAILY_LIMIT} جستجو دارند.\n\n"
            f"برای جستجوی نامحدود {BADGE_PRO} *ProPin* تهیه کنید:"
        ) if fa else (
            f"🚫 *Daily limit reached!*\n\nFree users get {FREE_DAILY_LIMIT} searches/day.\n\n"
            f"Upgrade to {BADGE_PRO} *ProPin* for unlimited:"
        )
        await message.reply_text(txt, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"⭐ Get ProPin — {PRO_STARS_PRICE} Stars", callback_data="buy_pro")
            ]]))
        return

    warn_msg = None
    if not pro and remaining <= 3:
        warn_msg = (
            f"⚠️ _{remaining} جستجوی رایگان امروز باقی مانده._" if fa
            else f"⚠️ _{remaining} free search{'es' if remaining != 1 else ''} left today._"
        )

    badge_pfx = f"{BADGE_PRO} " if pro else ""
    ack = await message.reply_text(
        f"{badge_pfx}🔍 در حال یافتن تصاویر برای «{raw_query}»..." if fa
        else f"{badge_pfx}🔍 Finding images for *{raw_query}*…",
        parse_mode=ParseMode.MARKDOWN)

    profile   = get_profile(uid)
    expansion = await asyncio.to_thread(ai_expand_query, raw_query, profile, pro)
    sub_q     = expansion["sub_queries"]
    new_tags  = expansion["tags"]
    theme     = expansion["display_theme"]
    log.info("uid=%s pro=%s query='%s' subs=%s", uid, pro, raw_query, sub_q)

    # Always include the exact original query as a sub-query — this is what
    # Pinterest's own search engine gets and produces the most accurate results.
    exact_queries = [raw_query] + [q for q in sub_q if q.lower() != raw_query.lower()]
    all_urls = await asyncio.to_thread(scrape_all, exact_queries)
    log.info("Found %d URLs for '%s'", len(all_urls), raw_query)

    if not all_urls:
        await ack.edit_text(
            "😕 هیچ تصویری پیدا نشد. کلمات دیگری امتحان کنید." if fa
            else "😕 No images found. Try different keywords!")
        return

    photo_count = PRO_PHOTO_COUNT if pro else FREE_PHOTO_COUNT
    chosen      = pick_fresh(uid, base_key, all_urls, photo_count)

    if not chosen:
        await ack.edit_text(
            "🔄 تمام تصاویر این موضوع قبلاً ارسال شده‌اند!" if fa
            else "🔄 You've seen all images for this topic — try a slightly different search.")
        return

    mark_sent(uid, base_key, chosen)
    update_profile(uid, raw_query, new_tags)

    # Delete the "searching..." ack message right before photos arrive
    try: await ack.delete()
    except: pass

    # Always use the user's exact original query in the caption — never the AI
    # display_theme, which can misspell niche terms like "frutiger aero".
    tier_tag = f"{BADGE_PRO} ProPin" if pro else "Pinify"
    caption  = f"📌 {raw_query}  •  {tier_tag}"
    if fa:
        caption = f"📌 {raw_query}  •  {'پرو‌پین ' + BADGE_PRO if pro else 'پینیفای'}"

    # Send as media group. For group chats Telegram is stricter about URLs,
    # so we try the group send first, then fall back to individual photos
    # only for URLs that actually fail — using a per-URL sent tracker to
    # guarantee no photo is ever sent twice.
    sent_urls: set = set()

    async def _send_photo(url: str, cap=None):
        if url in sent_urls:
            return True
        try:
            await message.reply_photo(photo=url, caption=cap)
            sent_urls.add(url)
            return True
        except Exception as e:
            log.debug("Photo failed (%s): %s", url[:60], e)
            return False

    try:
        media    = [InputMediaPhoto(media=url) for url in chosen]
        media[0] = InputMediaPhoto(media=chosen[0], caption=caption)
        await message.reply_media_group(media=media)
        # Mark all as sent so fallback never re-sends them
        sent_urls.update(chosen)
    except Exception as exc:
        log.warning("Media group failed (%s), falling back per-photo", exc)
        first = True
        for url in chosen:
            ok = await _send_photo(url, caption if first else None)
            if ok:
                first = False

    if warn_msg:
        await message.reply_text(
            warn_msg + "\n\n👉 /upgrade",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"⭐ ProPin — {PRO_STARS_PRICE} Stars", callback_data="buy_pro")
            ]]))

# ─────────────────────────────────────────────────────────────────────────────
# Boot
# ─────────────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    cmds = [
        BotCommand("start",      "Welcome & tier info"),
        BotCommand("profile",    "Your taste profile & usage"),
        BotCommand("upgrade",    f"Get {BADGE_PRO} ProPin"),
        BotCommand("giftpro",    "Gift ProPin to a user (owner only)"),
        BotCommand("revokepro",  "Revoke ProPin from a user (owner only)"),
        BotCommand("admin",      "Admin panel (owner only)"),
    ]
    await app.bot.set_my_commands(cmds)
    log.info("Pinify v7 ready 🌸")
    log.info("OWNER_ID loaded: '%s' (len=%d)", OWNER_ID, len(OWNER_ID))

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN env var!")
    if not ANTHROPIC_KEY:
        log.warning("No ANTHROPIC_API_KEY set.")
    if not OWNER_ID:
        log.warning("No OWNER_ID set — owner features disabled.")
    else:
        log.info("Owner ID: %s", OWNER_ID)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("whoami",    cmd_whoami))
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("profile",   cmd_profile))
    app.add_handler(CommandHandler("upgrade",   cmd_upgrade))
    app.add_handler(CommandHandler("giftpro",   cmd_giftpro))
    app.add_handler(CommandHandler("revokepro", cmd_revokepro))
    app.add_handler(CommandHandler("admin",     cmd_admin))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pinme))

    log.info("🌸 Pinify v7 running")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
