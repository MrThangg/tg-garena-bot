#!/usr/bin/env python3
# tg_garena_final.py

import os, json, asyncio, logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, List

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------- CONFIG ----------------
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")  # đọc từ biến môi trường
DATA_FILE = "data.json"
LOG_DIR = "logs"

# Timezone VN với fallback nếu Render/Linux thiếu tzdata
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    from datetime import timedelta, timezone
    TZ = timezone(timedelta(hours=7))

# ----------------------------------------

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {
            "chats": {},
            "api": {"url": "", "token": ""},
            "last_seen_unlocked": {},
            "include_raw": False
        }
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"chats": {}, "api": {"url": "", "token": ""}, "last_seen_unlocked": {}, "include_raw": False}

def save_data(d: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

async def api_check_unlocked(session: aiohttp.ClientSession, base_url: str, bearer: str, account: str) -> Dict[str, Any]:
    out = {"ok": False, "unlocked": False, "raw": None, "status": None}
    if not base_url or not bearer:
        out["status"] = "not-configured"
        return out
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    body = {"account": account}
    try:
        async with session.post(base_url, json=body, headers=headers, timeout=20) as resp:
            text = await resp.text()
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            out["status"] = resp.status
            out["raw"] = parsed or text
            out["ok"] = resp.status < 400
            if isinstance(parsed, dict):
                if "unlocked" in parsed:
                    out["unlocked"] = bool(parsed["unlocked"])
                elif parsed.get("status") in ("ok", "success"):
                    inner = parsed.get("data") or {}
                    if isinstance(inner, dict) and "unlocked" in inner:
                        out["unlocked"] = bool(inner["unlocked"])
            return out
    except Exception as e:
        out["status"] = "error"
        out["raw"] = str(e)
        return out

def format_notification(account: str, unlocked: bool, check_dt: datetime) -> str:
    iso_ts = check_dt.strftime("%Y-%m-%d %H:%M:%S")
    ddmmyy = check_dt.strftime("%d/%m/%Y %H:%M:%S")
    status_text = "Tài khoản đã mở khoá" if unlocked else "Tài khoản bị cấm"
    msg = (
        "🔔 *THÔNG BÁO*\n"
        "📝 *Nội dung:* 🔎 *KIỂM TRA GARENA*\n"
        f"📛 *Tên tài khoản:* `{account}`\n"
        f"📌 *Trạng thái:* *{status_text}*\n"
        f"⏱️ `{iso_ts}`\n"
        f"🕒 *Thời gian:* {ddmmyy}\n"
    )
    return msg

# --- Telegram command handlers ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot kiểm tra Garena — lệnh:\n"
        "/add <account>\n/remove <account>\n/list\n/interval <phút>\n"
        "/setapi <url>\n/settoken <bearer_token>\n/testnotify <account>"
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Dùng: /add <account>")
    acc = context.args[0].strip()
    chat = str(update.effective_chat.id)
    d = load_data()
    d["chats"].setdefault(chat, {"accounts": [], "interval_min": 5})["accounts"].append(acc)
    save_data(d)
    await update.message.reply_text(f"Đã thêm: {acc}")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = str(update.effective_chat.id)
    d = load_data()
    cfg = d["chats"].get(chat, {"accounts": [], "interval_min": 5})
    rows = "\n".join(f"- {a}" for a in cfg["accounts"]) or "- (trống)"
    api_url = d.get("api", {}).get("url") or "(chưa đặt)"
    await update.message.reply_text(f"Đang theo dõi:\n{rows}\nChu kỳ: {cfg['interval_min']} phút\nAPI: {api_url}")

async def cmd_setapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Dùng: /setapi <url>")
    url = context.args[0].strip()
    d = load_data()
    d["api"]["url"] = url
    save_data(d)
    await update.message.reply_text(f"Đã lưu API URL: {url}")

async def cmd_settoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Dùng: /settoken <token>")
    tok = context.args[0].strip()
    d = load_data()
    d["api"]["token"] = tok
    save_data(d)
    await update.message.reply_text("Đã lưu Bearer token.")

async def cmd_testnotify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Dùng: /testnotify <account>")
    acc = context.args[0].strip()
    now = datetime.now(TZ)
    msg = format_notification(acc, True, now)
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- periodic job ---
async def periodic_check(app: Application):
    d = load_data()
    api = d.get("api", {})
    base_url, bearer = api.get("url"), api.get("token")
    if not base_url or not bearer: return
    async with aiohttp.ClientSession() as session:
        for chat_id, cfg in d["chats"].items():
            for acc in cfg.get("accounts", []):
                res = await api_check_unlocked(session, base_url, bearer, acc)
                if res.get("unlocked"):
                    check_time = datetime.now(TZ)
                    txt = format_notification(acc, True, check_time)
                    await app.bot.send_message(chat_id=int(chat_id), text=txt, parse_mode="Markdown")

async def on_startup(app: Application):
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(periodic_check, IntervalTrigger(minutes=1), args=[app], id="periodic_check", replace_existing=True)
    scheduler.start()
    logging.info("Scheduler started.")

def main():
    token = TG_BOT_TOKEN
    if not token:
        raise RuntimeError("TG_BOT_TOKEN không được thiết lập.")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("setapi", cmd_setapi))
    app.add_handler(CommandHandler("settoken", cmd_settoken))
    app.add_handler(CommandHandler("testnotify", cmd_testnotify))
    app.post_init = on_startup
    app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    main()
