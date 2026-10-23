"""ClaimFlow AI Telegram bot: employee intake channel + manager approval console.

Single-account mode: if MANAGER_CHAT_ID is empty, manager approval cards are sent to the
same chat that submitted the claim, so one Telegram account can demo both roles.

Run:  python -m app.bot
"""
import asyncio
import html
import json
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from app import config, db, pipeline
from app.config import load_policy, reload_policy
from app.extractor import chaos_enabled, set_chaos
from app.logger import get_logger
from app.models import Status

log = get_logger(stage="bot")

PURPOSES = ["🤝 Client meeting", "✈️ Business travel", "🎤 Conference", "🍽️ Team event", "📦 Office purchase"]
STAGES = ["Intake", "Duplicate check", "AI extraction", "Validation", "Policy & risk", "Decision"]

STATUS_BADGE = {
    "AUTO_APPROVED": "✅ AUTO-APPROVED", "APPROVED": "✅ APPROVED BY MANAGER",
    "PENDING_APPROVAL": "🟡 PENDING MANAGER APPROVAL", "NEEDS_REVIEW": "🔍 SENT TO MANUAL REVIEW",
    "AUTO_REJECTED": "⛔ AUTO-REJECTED", "REJECTED": "❌ REJECTED BY MANAGER",
    "DUPLICATE": "🚫 DUPLICATE BLOCKED", "RETRY_PENDING": "🔁 QUEUED FOR RETRY",
    "FAILED": "⚠️ FAILED", "PROCESSING": "⏳ PROCESSING",
}
SEV_ICON = {"hard": "⛔", "warn": "⚠️", "info": "ℹ️"}
LINE = "━━━━━━━━━━━━━━━━━━"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def e(x) -> str:
    return html.escape(str(x)) if x is not None else "-"


def risk_bar(score) -> str:
    score = score or 0
    filled = round(score / 10)
    return ("🟥" if score >= 70 else "🟨" if score >= 30 else "🟩") * filled + "⬜" * (10 - filled)


def claim_card(c: dict, title: str = None) -> str:
    amount = f"{c['currency'] or 'INR'} {c['total_amount']:,.2f}" if c.get("total_amount") is not None else "-"
    parts = [f"🧾 <b>{e(c['id'])}</b> · <b>{STATUS_BADGE.get(c['status'], c['status'])}</b>"]
    if title:
        parts.insert(0, title)
    parts.append(LINE)
    if c.get("vendor") or c.get("total_amount") is not None:
        parts += [f"🏪 <b>Vendor:</b> {e(c.get('vendor'))}", f"📅 <b>Date:</b> {e(c.get('receipt_date'))}",
                  f"💰 <b>Amount:</b> {e(amount)}", f"🏷 <b>Category:</b> {e(c.get('category'))}"]
    parts.append(f"🎯 <b>Purpose:</b> {e(c.get('purpose'))}")
    if c.get("confidence") is not None:
        parts.append(f"🤖 <b>AI confidence:</b> {c['confidence']:.0%}")
    if c.get("risk_score") is not None:
        parts.append(f"📊 <b>Risk:</b> {c['risk_score']}/100 {risk_bar(c['risk_score'])}")
    flags = json.loads(c.get("flags_json") or "[]")
    shown = [f for f in flags if f["code"] != "AI_CONFIDENCE"]
    if shown:
        parts += [LINE, "🚩 <b>Policy & fraud checks</b>"]
        parts += [f"{SEV_ICON.get(f['severity'], '•')} <code>{e(f['code'])}</code> {e(f['message'])}" for f in shown]
    parts += [LINE, f"📝 {e(c.get('reason'))}"]
    if c.get("processing_ms"):
        parts.append(f"⏱ Processed in {c['processing_ms'] / 1000:.1f}s")
    return "\n".join(parts)


def progress_text(claim_label: str, stage: int, label: str, t0: float) -> str:
    rows = []
    for i, name in enumerate(STAGES, start=1):
        icon = "✅" if i < stage else "⏳" if i == stage else "▫️"
        rows.append(f"{icon} {i}. {name}")
    return (f"⚙️ <b>Processing {e(claim_label)}</b>\n{LINE}\n" + "\n".join(rows) +
            f"\n{LINE}\n<i>{e(label)}…</i>  ({time.perf_counter() - t0:.1f}s)")


def approval_keyboard(claim_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"approve:{claim_id}"),
                                  InlineKeyboardButton("❌ Reject", callback_data=f"reject:{claim_id}")]])


def manager_chat(claim: dict) -> str:
    return config.MANAGER_CHAT_ID or claim["chat_id"]


# ---------------------------------------------------------------- commands
def intro_text(first_name: str) -> str:
    mode = "single-account demo mode (you are employee + manager)" if not config.MANAGER_CHAT_ID else "manager chat configured"
    return (
        f"👋 Hi <b>{e(first_name)}</b>, welcome to <b>ClaimFlow AI</b>.\n"
        f"Intelligent expense-claim automation for <i>{e(load_policy()['company'])}</i>.\n\n"
        "📸 <b>Send a photo of a receipt</b> to file a claim. I will:\n"
        "1️⃣ read it with AI vision\n2️⃣ check for duplicates and fraud\n3️⃣ apply the company expense policy\n"
        "4️⃣ auto-approve it or route it to your manager\n5️⃣ email you a PDF voucher\n\n"
        "<b>Commands</b>\n/myclaims · your recent claims\n/status &lt;ID&gt; · claim details + audit trail\n"
        "/queue · manager approval queue\n/policy · current expense policy\n/stats · system statistics\n"
        "/email · set or change your notification email\n"
        "/chaos on|off · simulate an AI outage (resilience demo)\n\n"
        f"<i>Mode: {mode}</i>")


EMAIL_KB = InlineKeyboardMarkup([[InlineKeyboardButton("📧 Add my email", callback_data="email:add"),
                                  InlineKeyboardButton("Skip for now ➜", callback_data="email:skip")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, chat = update.effective_user, update.effective_chat
    log.info(f"/start from {user.full_name} (chat {chat.id})")
    await update.message.reply_html(intro_text(user.first_name))
    profile = db.get_user(chat.id)
    if profile and profile.get("email"):
        await update.message.reply_html(f"📧 Decision emails go to <b>{e(profile['email'])}</b> (change with /email).\n"
                                        "Send a receipt photo whenever you're ready.")
    elif not (profile and profile.get("email_asked")):
        await update.message.reply_html(
            "📧 <b>Optional:</b> want the decision and PDF voucher emailed to you as well?\n"
            "You can skip this and go straight to sending receipts.", reply_markup=EMAIL_KB)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(intro_text(update.effective_user.first_name))


async def email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if context.args and context.args[0].lower() in ("off", "remove", "none"):
        db.upsert_user(chat.id, update.effective_user.full_name, email=None, email_asked=1)
        context.user_data.pop("awaiting_email", None)
        await update.message.reply_text("🔕 Email removed. You will keep getting updates here in Telegram.")
        return
    context.user_data["awaiting_email"] = True
    current = (db.get_user(chat.id) or {}).get("email")
    await update.message.reply_html((f"Current email: <b>{e(current)}</b>\n" if current else "") +
                                    "✏️ Type the email address for decision emails (or /email off to remove).")


async def on_email_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "email:add":
        context.user_data["awaiting_email"] = True
        await q.edit_message_text("✏️ Type your email address:")
    else:
        db.upsert_user(update.effective_chat.id, update.effective_user.full_name, email_asked=1)
        await q.edit_message_text("👍 Skipped. You can add one any time with /email.\n"
                                  "📸 Send a photo of a receipt to file your first claim.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if context.user_data.get("awaiting_email"):
        if not EMAIL_RE.match(text):
            await update.message.reply_text("⚠️ That doesn't look like an email address. Please try again, or /email off.")
            return
        context.user_data.pop("awaiting_email", None)
        db.upsert_user(update.effective_chat.id, update.effective_user.full_name, email=text.lower(), email_asked=1)
        db.audit(None, update.effective_user.full_name, "EMAIL_REGISTERED", details={"email": text.lower()})
        log.info(f"{update.effective_user.full_name} registered a notification email")
        await update.message.reply_html(f"✅ Saved. Decision emails + PDF vouchers will go to <b>{e(text.lower())}</b>.\n"
                                        "📸 Now send a photo of a receipt to file a claim.")
        return
    await update.message.reply_text("📸 Send a photo of a receipt to file a claim, or /help to see what I can do.")


async def policy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = load_policy()
    limits = "\n".join(f"  • {k}: ₹{v:,}" for k, v in p["category_limits"].items())
    await update.message.reply_html(
        f"📜 <b>Expense policy</b> (config/policy.yaml)\n{LINE}\n"
        f"Auto-approve up to: ₹{p['auto_approve_limit']:,} (risk &lt; {p['routing']['auto_approve_max_risk']})\n"
        f"Max receipt age: {p['max_receipt_age_days']} days\nMin AI confidence: {p['min_ai_confidence']:.0%}\n"
        f"Category limits:\n{limits}\nHard limit: {p['hard_limit_multiplier']}x category limit\n"
        f"Non-reimbursable: {', '.join(p['prohibited_keywords'])}")


async def reload_policy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reload_policy()
    log.info("Policy reloaded from YAML")
    db.audit(None, update.effective_user.full_name, "POLICY_RELOADED")
    await update.message.reply_text("🔄 Policy reloaded from config/policy.yaml")


async def myclaims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.claims_for_chat(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("You have no claims yet. Send a receipt photo to start.")
        return
    lines = [f"<code>{r['id']}</code> {STATUS_BADGE.get(r['status'], r['status'])}\n"
             f"     {e(r['vendor'])} · {r['currency'] or ''} {r['total_amount'] or 0:,.0f}" for r in rows]
    await update.message.reply_html("🗂 <b>Your recent claims</b>\n" + LINE + "\n" + "\n".join(lines))


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /status CLM-2026-0001")
        return
    c = db.get_claim(context.args[0].upper())
    if not c:
        await update.message.reply_text("Claim not found.")
        return
    trail = "\n".join(f"<code>{a['ts'][11:19]}</code> {e(a['actor'])}: {e(a['action'])}"
                      + (f" → {e(a['to_status'])}" if a["to_status"] else "") for a in db.audit_trail(c["id"]))
    await update.message.reply_html(claim_card(c) + f"\n{LINE}\n🧾 <b>Audit trail</b>\n{trail}")


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.claims_by_status(Status.PENDING_APPROVAL.value, Status.NEEDS_REVIEW.value, Status.RETRY_PENDING.value)
    if not rows:
        await update.message.reply_text("🎉 Approval queue is empty.")
        return
    await update.message.reply_html(f"🛡️ <b>MANAGER CONSOLE</b> · {len(rows)} claim(s) waiting")
    for c in rows:
        kb = approval_keyboard(c["id"]) if c["status"] != Status.RETRY_PENDING.value else None
        await update.message.reply_html(claim_card(c), reply_markup=kb)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db.connect() as conn:
        by = dict(conn.execute("SELECT status, COUNT(*) FROM claims GROUP BY status").fetchall())
        approved = conn.execute("SELECT COALESCE(SUM(total_amount),0) FROM claims "
                                "WHERE status IN ('AUTO_APPROVED','APPROVED')").fetchone()[0]
        avg_ms = conn.execute("SELECT AVG(processing_ms) FROM claims WHERE processing_ms IS NOT NULL").fetchone()[0]
        api = conn.execute("SELECT COUNT(*), SUM(success) FROM api_metrics WHERE service='gemini'").fetchone()
    total = sum(by.values())
    auto = by.get("AUTO_APPROVED", 0) + by.get("AUTO_REJECTED", 0) + by.get("DUPLICATE", 0)
    rate = f"{(api[1] or 0) / api[0]:.0%}" if api[0] else "-"
    await update.message.reply_html(
        f"📈 <b>ClaimFlow AI statistics</b>\n{LINE}\nTotal claims: <b>{total}</b>\n"
        + "\n".join(f"  {STATUS_BADGE.get(k, k)}: {v}" for k, v in by.items())
        + f"\nApproved value: <b>₹{approved:,.0f}</b>\nStraight-through (no human): <b>{auto / total:.0%}</b>\n"
          f"Avg processing time: <b>{(avg_ms or 0) / 1000:.1f}s</b>\nAI API success rate: <b>{rate}</b>"
        if total else "No claims yet.")


async def chaos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "off"):
        set_chaos(arg == "on")
        db.audit(None, update.effective_user.full_name, f"CHAOS_{arg.upper()}")
        log.warning(f"AI outage simulation turned {arg.upper()}")
    state = "🔴 ON - every AI call will fail (503)" if chaos_enabled() else "🟢 OFF - AI service healthy"
    await update.message.reply_html(f"🧪 <b>Chaos mode:</b> {state}\nUse /chaos on or /chaos off")


# ---------------------------------------------------------------- receipt intake
async def on_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo:
        file = await msg.photo[-1].get_file()
        mime = "image/jpeg"
    else:
        doc = msg.document
        if not (doc.mime_type or "").startswith("image/"):
            await msg.reply_text("Please send the receipt as an image (JPG/PNG).")
            return
        file = await doc.get_file()
        mime = doc.mime_type
    data = bytes(await file.download_as_bytearray())
    log.info(f"Receipt image received from {update.effective_user.full_name} ({len(data) / 1024:.0f} KB)")
    if msg.caption:
        await run_pipeline(update, context, data, mime, msg.caption.strip())
        return
    context.user_data["pending"] = {"bytes": data, "mime": mime}
    kb = [[InlineKeyboardButton(p, callback_data=f"purpose:{i}")] for i, p in enumerate(PURPOSES)]
    await msg.reply_text("📎 Got it! What was this expense for?", reply_markup=InlineKeyboardMarkup(kb))


async def on_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pending = context.user_data.pop("pending", None)
    if not pending:
        await q.edit_message_text("This request expired. Please send the receipt again.")
        return
    purpose = PURPOSES[int(q.data.split(":")[1])]
    await q.edit_message_text(f"🎯 Purpose: {purpose}")
    await run_pipeline(update, context, pending["bytes"], pending["mime"], purpose)


async def run_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE, data: bytes, mime: str, purpose: str):
    chat = update.effective_chat
    user = update.effective_user
    t0 = time.perf_counter()
    status_msg = await context.bot.send_message(chat.id, progress_text("new claim", 1, "Starting", t0),
                                                parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()

    def progress(stage: int, label: str):
        fut = asyncio.run_coroutine_threadsafe(
            status_msg.edit_text(progress_text("claim", stage, label, t0), parse_mode=ParseMode.HTML), loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass

    result = await asyncio.to_thread(pipeline.submit_claim, data, chat.id, user.full_name, purpose, mime, progress)
    await deliver_result(context, result.claim_id, status_msg=status_msg)


async def deliver_result(context: ContextTypes.DEFAULT_TYPE, claim_id: str, status_msg=None):
    """Shows the outcome to the employee and, if needed, sends the manager approval card."""
    c = db.get_claim(claim_id)
    text = claim_card(c)
    if c["status"] == Status.NEEDS_REVIEW.value:
        text += "\n\n📸 <b>Tip:</b> please re-send a clearer, well-lit photo of the full receipt."
    elif c["status"] == Status.RETRY_PENDING.value:
        text += ("\n\n🔁 The AI service is temporarily unavailable. Your claim is <b>safely queued</b> "
                 "and will be processed automatically - no need to resubmit.")
    elif c["status"] == Status.DUPLICATE.value:
        text += f"\n\n🔎 Original claim: /status {c['duplicate_of']}"
    elif c["status"] == Status.PENDING_APPROVAL.value:
        text += "\n\n📨 Sent to your manager for approval."
    if c["status"] in (Status.AUTO_APPROVED.value, Status.AUTO_REJECTED.value, Status.DUPLICATE.value):
        text += "\n📧 Email + PDF voucher sent."

    if status_msg:
        await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(c["chat_id"], text, parse_mode=ParseMode.HTML)

    if c["status"] in (Status.PENDING_APPROVAL.value, Status.NEEDS_REVIEW.value):
        await send_manager_card(context, c)


async def send_manager_card(context: ContextTypes.DEFAULT_TYPE, c: dict):
    title = ("🛡️ <b>MANAGER CONSOLE</b> · Approval required" if c["status"] == Status.PENDING_APPROVAL.value
             else "🛡️ <b>MANAGER CONSOLE</b> · Manual review required")
    caption = title + f"\n👤 Submitted by: {e(c['employee_name'])}\n" + claim_card(c)
    if len(caption) > 1024:
        caption = caption[:1000] + "…"
    with open(c["image_path"], "rb") as img:
        await context.bot.send_photo(manager_chat(c), img, caption=caption, parse_mode=ParseMode.HTML,
                                     reply_markup=approval_keyboard(c["id"]))
    get_logger(c["id"], "approval").info(f"Manager approval card sent to chat {manager_chat(c)}")


async def on_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, claim_id = q.data.split(":", 1)
    actor = f"{update.effective_user.full_name} (Manager)"
    try:
        c = await asyncio.to_thread(pipeline.manager_decision, claim_id, action == "approve", actor)
    except ValueError as err:
        await q.answer(f"Already processed: {err}", show_alert=True)
        return
    await q.answer("Approved ✅" if action == "approve" else "Rejected ❌")
    stamp = (f"\n{LINE}\n<b>{'✅ APPROVED' if action == 'approve' else '❌ REJECTED'}</b> by {e(actor)} "
             f"at {c['updated_at'][11:19]}\n📧 Email + PDF voucher sent")
    try:
        if q.message.photo:
            cap = (q.message.caption_html or "")[:1024 - len(stamp)] + stamp
            await q.edit_message_caption(cap, parse_mode=ParseMode.HTML)
        else:
            await q.edit_message_text((q.message.text_html or "") + stamp, parse_mode=ParseMode.HTML)
    except Exception as err:
        log.warning(f"Could not update manager card: {err}")
    await context.bot.send_message(c["chat_id"], "📣 <b>Update on your claim</b>\n" + claim_card(c),
                                   parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------- background worker
async def retry_worker(context: ContextTypes.DEFAULT_TYPE):
    if not db.claims_by_status(Status.RETRY_PENDING.value):
        return
    results = await asyncio.to_thread(pipeline.process_retry_queue)
    for r in results:
        if r.status != Status.RETRY_PENDING:
            c = db.get_claim(r.claim_id)
            await context.bot.send_message(c["chat_id"], "♻️ <b>AI service recovered - queued claim processed</b>",
                                           parse_mode=ParseMode.HTML)
            await deliver_result(context, r.claim_id)


HEARTBEAT = config.DATA_DIR / "bot_heartbeat.txt"


async def heartbeat(context: ContextTypes.DEFAULT_TYPE):
    """Lets the dashboard show whether the automation worker is online."""
    HEARTBEAT.write_text(str(time.time()))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.opt(exception=context.error).error(f"Unhandled bot error: {context.error}")
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(update.effective_chat.id, "⚠️ Something went wrong; it has been logged.")
        except Exception:
            pass


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing in .env")
    db.init_db()
    app = (Application.builder().token(config.TELEGRAM_BOT_TOKEN)
           .connect_timeout(20).read_timeout(30).write_timeout(30).build())
    for name, fn in [("start", start), ("help", help_cmd), ("policy", policy_cmd), ("reloadpolicy", reload_policy_cmd),
                     ("myclaims", myclaims), ("status", status_cmd), ("queue", queue_cmd), ("stats", stats_cmd),
                     ("chaos", chaos_cmd), ("email", email_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_receipt))
    app.add_handler(CallbackQueryHandler(on_purpose, pattern=r"^purpose:"))
    app.add_handler(CallbackQueryHandler(on_email_choice, pattern=r"^email:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_decision, pattern=r"^(approve|reject):"))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(retry_worker, interval=config.RETRY_QUEUE_INTERVAL_SEC, first=10)
    app.job_queue.run_repeating(heartbeat, interval=15, first=1)
    log.info(f"ClaimFlow AI bot started | model={config.GEMINI_MODEL} | "
             f"manager={'same chat (single-account mode)' if not config.MANAGER_CHAT_ID else config.MANAGER_CHAT_ID}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
