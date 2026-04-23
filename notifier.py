"""
notifier.py
推播審核通知 — 支援 Telegram Bot
"""

import os
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def send_telegram(message: str, chat_id: str = "") -> Optional[int]:
    """發送 Telegram 訊息，回傳 message_id（失敗回傳 None）。"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未設定")
        return None
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("result", {}).get("message_id")
    except Exception as e:
        logger.error(f"Telegram 發送失敗: {e}")
        return None


def notify_drafts_for_approval(drafts: list[dict], job_id: str) -> Optional[int]:
    """發送草稿審核通知，回傳 Telegram message_id 供後續回覆對應。"""
    lines = [f"📋 今日發文草稿\n"]
    for i, d in enumerate(drafts, 1):
        angle = d.get("angle", "")
        draft = d.get("draft", "")
        source = d.get("source_title", "")
        lines.append(f"【{i}】{angle}")
        if source:
            lines.append(f"主題：{source}")
        lines.append("─" * 20)
        lines.append(draft)
        lines.append("")
    lines.append(f"💬 直接回覆此訊息：「1」「2」「3」選草稿，「跳過」略過")
    lines.append(f"[job:{job_id}]")
    return send_telegram("\n".join(lines))


def notify_reply_for_approval(reply_job_id: str, commenter: str, comment_text: str, reply_text: str, post_preview: str) -> Optional[int]:
    """發送留言回覆審核通知，回傳 Telegram message_id 供後續回覆對應。"""
    message = (
        f"💬 新留言審核\n"
        f"貼文：{post_preview[:60]}...\n"
        f"@{commenter}：{comment_text[:100]}\n"
        f"─────────────\n"
        f"AI 回覆：\n{reply_text}\n"
        f"─────────────\n"
        f"直接回覆「發出」送出，「略過」跳過\n"
        f"[reply_job:{reply_job_id}]"
    )
    return send_telegram(message)


def notify_error(error_msg: str) -> Optional[int]:
    return send_telegram(f"🚨 Threads Bot 錯誤\n{error_msg}")


def notify_auto_reply(post_preview: str, commenter: str, comment: str, reply: str, post_id: str) -> Optional[int]:
    message = (
        f"🤖 自動回覆通知\n"
        f"貼文：{post_preview[:60]}...\n"
        f"留言者：@{commenter}\n"
        f"留言：{comment[:80]}\n"
        f"─────────────\n"
        f"AI 回覆：{reply}"
    )
    return send_telegram(message)
