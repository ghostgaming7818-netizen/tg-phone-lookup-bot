#!/usr/bin/env python3
"""
tg_phone_lookup_bot_credits.py

Styled phone-lookup bot + credit system + admin redeem codes.
"""

import os
import logging
import asyncio
import sqlite3
import secrets
import time
from datetime import datetime, date
from aiohttp import ClientSession
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("Error: BOT_TOKEN not set! Please set environment variable.")
    exit()

# Provide ADMIN Telegram numeric IDs (comma-separated env var) or edit the list below:
ADMIN_IDS_ENV = os.environ.get("ADMIN_IDS")  # e.g. "11111111,22222222"
if ADMIN_IDS_ENV:
    ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_ENV.split(",") if x.strip().isdigit()]
else:
    ADMIN_IDS = [8455153833]  # replace with your Telegram user id as admin

API_TEMPLATE = os.environ.get("API_TEMPLATE")
if not API_TEMPLATE:
    print("Error: API_TEMPLATE not set! Please set environment variable.")
    exit()


DB_FILE = "bot_data.db"
LOGFILE = "bot_activity.log"

# Credits config
DAILY_FREE_CREDITS = 30
CREDIT_COST_PER_LOOKUP = 1

# Code length: token_urlsafe(8) yields ~11-12 chars; adjust if you want shorter/longer
CODE_BYTES = 8

# ============================

# logging
logging.basicConfig(
    filename=LOGFILE,
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ========== DB helpers ==========
def init_db():
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    cur = conn.cursor()
    # users: store credits and last_topup_date (YYYY-MM-DD)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        credits INTEGER DEFAULT 0,
        last_topup_date TEXT
    )
    """)
    # codes: one-time redeem codes
    cur.execute("""
    CREATE TABLE IF NOT EXISTS codes (
        code TEXT PRIMARY KEY,
        amount INTEGER NOT NULL,
        created_by INTEGER,
        created_at TEXT,
        used_by INTEGER,
        used_at TEXT
    )
    """)
    conn.commit()
    return conn

DB = init_db()
DB_LOCK = asyncio.Lock()  # ensure async-safe db ops

def now_iso():
    return datetime.utcnow().isoformat()

# ensure user exists
def ensure_user_sync(user_id):
    cur = DB.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    r = cur.fetchone()
    if not r:
        cur.execute("INSERT INTO users (user_id, credits, last_topup_date) VALUES (?, ?, ?)",
                    (user_id, 0, None))
        DB.commit()

async def ensure_user(user_id):
    async with DB_LOCK:
        ensure_user_sync(user_id)

# top up daily free credits if not already for today
def topup_if_needed_sync(user_id):
    cur = DB.cursor()
    today = date.today().isoformat()
    cur.execute("SELECT credits, last_topup_date FROM users WHERE user_id = ?", (user_id,))
    r = cur.fetchone()
    if not r:
        # create and top up
        cur.execute("INSERT INTO users (user_id, credits, last_topup_date) VALUES (?, ?, ?)",
                    (user_id, DAILY_FREE_CREDITS, today))
        DB.commit()
        return DAILY_FREE_CREDITS
    credits, last_date = r
    if last_date != today:
        new_credits = credits + DAILY_FREE_CREDITS
        cur.execute("UPDATE users SET credits = ?, last_topup_date = ? WHERE user_id = ?",
                    (new_credits, today, user_id))
        DB.commit()
        return new_credits
    return credits

async def topup_if_needed(user_id):
    async with DB_LOCK:
        return topup_if_needed_sync(user_id)

def get_credits_sync(user_id):
    cur = DB.cursor()
    cur.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    r = cur.fetchone()
    return r[0] if r else 0

async def get_credits(user_id):
    async with DB_LOCK:
        ensure_user_sync(user_id)
        return get_credits_sync(user_id)

def change_credits_sync(user_id, delta):
    cur = DB.cursor()
    cur.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    r = cur.fetchone()
    if not r:
        cur.execute("INSERT INTO users (user_id, credits, last_topup_date) VALUES (?, ?, ?)",
                    (user_id, max(0, delta), None))
        DB.commit()
        return max(0, delta)
    new = max(0, r[0] + delta)
    cur.execute("UPDATE users SET credits = ? WHERE user_id = ?", (new, user_id))
    DB.commit()
    return new

async def change_credits(user_id, delta):
    async with DB_LOCK:
        ensure_user_sync(user_id)
        return change_credits_sync(user_id, delta)

# Code generation & redeem
def generate_code_sync(amount, created_by):
    token = secrets.token_urlsafe(CODE_BYTES)
    created_at = now_iso()
    cur = DB.cursor()
    cur.execute("INSERT INTO codes (code, amount, created_by, created_at, used_by, used_at) VALUES (?, ?, ?, ?, NULL, NULL)",
                (token, int(amount), int(created_by), created_at))
    DB.commit()
    return token

async def generate_code(amount, created_by):
    async with DB_LOCK:
        return generate_code_sync(amount, created_by)

def redeem_code_sync(code, user_id):
    cur = DB.cursor()
    cur.execute("SELECT amount, used_by FROM codes WHERE code = ?", (code,))
    r = cur.fetchone()
    if not r:
        return False, "Code not found"
    amount, used_by = r
    if used_by is not None:
        return False, "Code already used"
    used_at = now_iso()
    # mark used
    cur.execute("UPDATE codes SET used_by = ?, used_at = ? WHERE code = ?", (user_id, used_at, code))
    # add credits to user
    cur.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    rr = cur.fetchone()
    if not rr:
        cur.execute("INSERT INTO users (user_id, credits, last_topup_date) VALUES (?, ?, ?)",
                    (user_id, int(amount), None))
    else:
        newc = rr[0] + int(amount)
        cur.execute("UPDATE users SET credits = ? WHERE user_id = ?", (newc, user_id))
    DB.commit()
    return True, int(amount)

async def redeem_code(code, user_id):
    async with DB_LOCK:
        return redeem_code_sync(code, user_id)

def list_codes_sync(limit=100):
    cur = DB.cursor()
    cur.execute("SELECT code, amount, created_by, created_at, used_by, used_at FROM codes ORDER BY created_at DESC LIMIT ?", (limit,))
    return cur.fetchall()

async def list_codes(limit=100):
    async with DB_LOCK:
        return list_codes_sync(limit)

# ========== Bot formatting (same styled blocks as before) ==========

def style_record_block(rec: dict, idx: int) -> str:
    """
    Hacker-style scanning format for mobile info.
    """
    mobile = (rec.get("mobile") or "").strip()
    alt = (rec.get("alt_mobile") or "").strip()
    name = (rec.get("name") or "").strip()
    father = (rec.get("father_name") or "").strip()
    circle = (rec.get("circle") or "").strip()
    idnum = (rec.get("id_number") or "").strip()
    address = (rec.get("address") or "").strip()
    email = (rec.get("email") or "").strip()

    lines = []
    lines.append(f"⚡ SCAN INITIATED: RECORD {idx} ⚡")
    lines.append("┌─────────────────────────┐")
    if name:
        lines.append(f"│ 👤 Name         : {name.upper()}")
    if mobile:
        lines.append(f"│ 📞 Phone        : {mobile}")
    if alt:
        lines.append(f"│ 📱 Alt Phone    : {alt}")
    if father:
        lines.append(f"│ 👴 Father's Name: {father}")
    if circle:
        lines.append(f"│ 🔴 Circle       : {circle}")
    if idnum:
        lines.append(f"│ 🆔 ID           : {idnum}")
    if address:
        lines.append(f"│ 🏠 Address      : {address}")
    if email:
        lines.append(f"│ ✉️ Email        : {email}")
    lines.append("└─────────────────────────┘")
    lines.append("✅ SCAN COMPLETE")
    return "\n".join(lines)

# ========== Bot command handlers ==========

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 *Welcome to PREDATOR INFO BOT!* 🕵️‍♂️\n\n"
        "I can help you lookup mobile numbers with detailed info.\n\n"
        "💡 *How to use:*\n"
        "• `/num <number>` - Lookup a mobile number (10 or 12 digits)\n"
        "• `/credits` - Check your remaining credits\n"
        "• `/redeem <code>` - Redeem a credit code\n\n"
        "🛡️ Admins can use `/code <amount>` to generate redeem codes and `/codes` to view them.\n\n"
        "⚡ *Note:* Non-admins get *daily free credits*: {daily} per day.\n"
        "Try it now: `/num 7986782429`\n\n"
        "MADE BY @PREDATORHUNTER1"
    ).format(daily=DAILY_FREE_CREDITS)

    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def credits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await ensure_user(user_id)
    await topup_if_needed(user_id)
    credits = await get_credits(user_id)
    is_admin = user_id in ADMIN_IDS
    if is_admin:
        await update.message.reply_text("You are an *ADMIN* — unlimited usage.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Your credits: {credits} (Daily free credits: {DAILY_FREE_CREDITS})")

async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /redeem CODE")
        return
    code = context.args[0].strip()
    ok, info = await redeem_code(code, user_id)
    if ok:
        await update.message.reply_text(f"✅ Code applied. You received {info} credits.")
    else:
        await update.message.reply_text(f"❌ Redeem failed: {info}")

async def code_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin-only: /code <amount>
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Only admin can create codes.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /code <amount>")
        return
    try:
        amount = int(context.args[0])
        if amount <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Provide a positive integer amount. Example: /code 100")
        return
    token = await generate_code(amount, user_id)
    await update.message.reply_text(f"✅ Code created: `{token}`\nAmount: {amount}\nNote: one-time use only.", parse_mode="Markdown")

async def codes_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Only admin can view codes.")
        return
    rows = await list_codes(limit=200)
    if not rows:
        await update.message.reply_text("No codes found.")
        return
    lines = []
    for r in rows:
        code, amount, created_by, created_at, used_by, used_at = r
        used = f"USED by {used_by} at {used_at}" if used_by else "UNUSED"
        lines.append(f"{code} | {amount} credits | created_by:{created_by} | {used}")
    # send in chunks if long
    text = "\n".join(lines)
    for chunk in [text[i:i+3900] for i in range(0, len(text), 3900)]:
        await update.message.reply_text(chunk)

# Main lookup command with credit deduction
# Replace the existing num_cmd with this updated version
async def num_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /num 7986782429")
        return

    raw_num = context.args[0].strip()
    num_digits = "".join(ch for ch in raw_num if ch.isdigit())
    if len(num_digits) not in (10, 12):
        await update.message.reply_text("Please provide a 10 or 12 digit mobile number (e.g., 7986782429).")
        return

    user_id = update.effective_user.id
    is_admin = user_id in ADMIN_IDS

    # ensure user exist and topup if needed
    await ensure_user(user_id)
    await topup_if_needed(user_id)

    # check credits for non-admin but DO NOT deduct yet
    if not is_admin:
        credits = await get_credits(user_id)
        if credits < CREDIT_COST_PER_LOOKUP:
            await update.message.reply_text(f"❌ You have insufficient credits ({credits}). Redeem a code or wait for daily top-up.")
            return

    query_num = num_digits
    await update.message.reply_text(f"Looking up {query_num} ...")
    url = API_TEMPLATE.format(num=query_num)

    try:
        async with ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    await update.message.reply_text(f"API returned status {resp.status}.")
                    return
                data = await resp.json()

        # --- NEW: normalize/unwrap API response ---
        # If API wraps results in {"data": [...] , ...}, use that list.
        # If API returns a single dict record, convert to [dict].
        # If API returns a list already, use it.
        data_list = None
        if isinstance(data, dict):
            # prefer 'data' key if present
            if "data" in data and isinstance(data["data"], (list, dict)):
                inner = data["data"]
                if isinstance(inner, list):
                    data_list = inner
                elif isinstance(inner, dict):
                    data_list = [inner]
                else:
                    data_list = []
            else:
                # if dict looks like a single record (has mobile/name keys), wrap it
                if any(k in data for k in ["mobile", "name", "alt_mobile", "id_number", "address", "email"]):
                    data_list = [data]
                else:
                    # handle common API error structure
                    if data.get("message") == "No records found":
                        await update.message.reply_text("❌ NO RESULT FOUND")
                        return
                    if data.get("status") == "error":
                        await update.message.reply_text(f"❌ API Error: {data.get('message')}")
                        return
                    # unknown dict structure — try to be graceful
                    await update.message.reply_text("Unexpected API response structure.")
                    return
        elif isinstance(data, list):
            data_list = data
        else:
            await update.message.reply_text("Unexpected API response format.")
            return

        # filter out empty records just in case
        filtered_list = []
        for rec in data_list:
            # ensure rec is a dict
            if not isinstance(rec, dict):
                continue
            # ignore meta keys like api_owner / developer by not using them
            if any(rec.get(k) for k in ["mobile", "alt_mobile", "name", "father_name", "circle", "id_number", "address", "email"]):
                filtered_list.append(rec)

        if not filtered_list:
            await update.message.reply_text("❌ NO RESULT FOUND")
            return

        # deduct credits only now, after confirming valid result
        if not is_admin:
            await change_credits(user_id, -CREDIT_COST_PER_LOOKUP)

        # send results
        for idx, rec in enumerate(filtered_list, start=1):
            block = style_record_block(rec, idx)
            await update.message.reply_text(block)

        # footer
        if is_admin:
            await update.message.reply_text("MADE BY @PREDATORHUNTER1 (ADMIN)")
        else:
            rem = await get_credits(user_id)
            await update.message.reply_text(f"MADE BY @PREDATORHUNTER1\nRemaining credits: {rem}")

    except Exception as e:
        logger.exception("Error fetching API")
        await update.message.reply_text(f"Error calling API: {e}")

# ========== main ==========
def main():
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        print("Set BOT_TOKEN environment variable or update the script with your bot token.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("credits", credits_cmd))
    app.add_handler(CommandHandler("redeem", redeem_cmd))
    app.add_handler(CommandHandler("code", code_cmd))
    app.add_handler(CommandHandler("codes", codes_list_cmd))
    app.add_handler(CommandHandler("num", num_cmd))

    print("Bot with credits started. Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
