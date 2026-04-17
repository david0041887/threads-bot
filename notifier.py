"""
notifier.py
推播審核通知 — 支援 Telegram Bot
"""

import os
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

CONTROL_URL = "https://threads-bot-production-93cc.up.railway.app/control"


def send_telegram(message: str) -> bool:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未設定")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram 發送失敗: {e}")
        return False


def notify_drafts_for_approval(drafts: list[dict], job_id: str, channel: Optional[str] = None) -> bool:
    lines = [f"📋 今日發文草稿審核 (job: {job_id})\n"]
    for i, d in enumerate(drafts, 1):
        angle = d.get("angle", "")
        draft = d.get("draft", "")
        source = d.get("source_title", "")
        lines.append(f"[{i}] 角度：{angle}")
        if source:
            lines.append(f"來源：{source}")
        lines.append("─" * 20)
        lines.append(draft)
        lines.append("")
    lines.append(f"✅ 在控制台填入 job_id 選擇發文")
    lines.append(f"🔗 {CONTROL_URL}")
    message = "\n".join(lines)
    return send_telegram(message)


def notify_reply_for_approval(reply_job_id: str, commenter: str, comment_text: str, reply_text: str, post_preview: str) -> bool:
    message = (
        f"💬 新留言審核 (reply_job: {reply_job_id})\n"
        f"貼文：{post_preview[:60]}...\n"
        f"留言者：@{commenter}\n"
        f"留言：{comment_text}\n"
        f"─────────────\n"
        f"預計回覆：\n{reply_text}\n"
        f"─────────────\n"
        f"✅ 在控制台填入 reply_job_id 確認回覆\n"
        f"🔗 {CONTROL_URL}"
    )
    return send_telegram(message)


def notify_error(error_msg: str) -> bool:
    message = f"🚨 Threads Bot 錯誤\n{error_msg}"
    return send_telegram(message)


def notify_auto_reply(post_preview: str, commenter: str, comment: str, reply: str, post_id: str) -> bool:
    message = (
        f"🤖 自動回覆通知\n"
        f"貼文：{post_preview[:60]}...\n"
        f"留言者：@{commenter}\n"
        f"留言：{comment[:80]}\n"
        f"─────────────\n"
        f"AI 回覆：{reply}"
    )
    return send_telegram(message)
