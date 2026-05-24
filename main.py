"""
main.py
FastAPI 主程式 — 含主動海巡功能
"""

import os
import uuid
import json
import logging
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import state
from threads_client import ThreadsClient
from ai_generator import generate_reply, generate_daily_topics, generate_proactive_reply, generate_short_reaction
from notifier import notify_error, notify_reply_for_approval, send_telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
# 避免 httpx INFO log 把 URL + access_token 印到 Railway log 裡
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

try:
    from threads_scraper import search_threads_by_keyword_async, search_and_reply_async
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright 未安裝，海巡功能停用")

scheduler = AsyncIOScheduler()

SEARCH_KEYWORDS = [
    "保險", "壽險", "醫療險", "遺產稅", "節稅", "保費",
    "保單", "理賠", "投資型保單", "重大傷病", "實支實付",
    "保障規劃", "退休規劃", "保險稅務",
    "住院自費", "保費太貴", "買錯保險", "受益人", "理賠被拒",
    "定期壽險", "意外險", "癌症險", "長照", "失能險",
    "儲蓄險", "醫療費用", "手術費", "保險規劃", "受益人指定",
]

PATROL_MAX_AGE_HOURS = 1440  # 只回覆 60 天內的貼文（60×24）

# 每日海巡配額
PATROL_SCHEDULE = {
    "morning": {"hour": "7-9", "count": 6},
    "noon":    {"hour": "12-13", "count": 7},
    "evening": {"hour": "20-23", "count": 8},
}

_COUNTS_KEY = "daily_proactive_count"
_COUNTS_DEFAULT = {"morning": 0, "noon": 0, "evening": 0, "date": ""}


def _get_counts() -> dict:
    return state.get_kv(_COUNTS_KEY, _COUNTS_DEFAULT.copy())


def _bump_count(session: str) -> None:
    c = _get_counts()
    c[session] = c.get(session, 0) + 1
    state.set_kv(_COUNTS_KEY, c)


def _current_access_token() -> str:
    """Threads access token — prefer state.kv (survives redeploys) over env var."""
    return state.get_kv("threads_access_token") or os.environ["THREADS_ACCESS_TOKEN"]


def _notify_error_throttled(category: str, msg: str, cooldown_seconds: int = 3600) -> None:
    """notify_error 但同類別 1 小時內只送 1 次，避免每 2~15 分鐘的循環任務洗版。"""
    import time as _time
    key = f"last_error_notify_{category}"
    last = state.get_kv(key)
    now = int(_time.time())
    if last and now - last < cooldown_seconds:
        return
    state.set_kv(key, now)
    notify_error(f"[{category}] {msg}")


def get_client() -> ThreadsClient:
    return ThreadsClient(
        access_token=_current_access_token(),
        user_id=os.environ["THREADS_USER_ID"],
    )


def get_current_session() -> Optional[str]:
    """判斷現在是哪個時段"""
    from datetime import datetime
    import pytz
    tz = pytz.timezone("Asia/Taipei")
    now = datetime.now(tz)
    h = now.hour
    if 7 <= h < 9:
        return "morning"
    elif 12 <= h < 13:
        return "noon"
    elif 20 <= h < 23:
        return "evening"
    return None


def reset_daily_count():
    from datetime import datetime
    import pytz
    today = datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d")
    c = _get_counts()
    if c.get("date") != today:
        state.set_kv(_COUNTS_KEY, {"morning": 0, "noon": 0, "evening": 0, "date": today})


def _ensure_chromium():
    import subprocess, sys
    browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/app/.playwright-browsers")
    import glob as _glob
    if not _glob.glob(f"{browser_path}/**/chrome-headless-shell", recursive=True):
        logger.info(f"[Playwright] Chromium 未找到，正在安裝到 {browser_path} ...")
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            logger.info("[Playwright] Chromium 安裝完成")
        else:
            logger.error(f"[Playwright] 安裝失敗: {result.stderr[:500]}")
    else:
        logger.info(f"[Playwright] Chromium 已存在於 {browser_path}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.init_db()
    _ensure_chromium()
    scheduler.add_job(daily_draft_job, CronTrigger(hour=8, minute=0, timezone="Asia/Taipei"), args=["morning"], id="daily_draft_morning", replace_existing=True)
    scheduler.add_job(daily_draft_job, CronTrigger(hour=19, minute=0, timezone="Asia/Taipei"), args=["evening"], id="daily_draft_evening", replace_existing=True)
    scheduler.add_job(refresh_token_job, CronTrigger(month="*/2", day="1", hour=3, minute=0, timezone="Asia/Taipei"), id="token_refresh", replace_existing=True)
    scheduler.add_job(lambda: state.cleanup_old_jobs(hours=24), CronTrigger(hour=4, minute=0, timezone="Asia/Taipei"), id="state_cleanup", replace_existing=True)
    scheduler.add_job(lambda: state.cleanup_old_processed_ids(days=90), CronTrigger(day_of_week="sun", hour=4, minute=30, timezone="Asia/Taipei"), id="processed_ids_cleanup", replace_existing=True)
    # 依 state 裡的 flag 決定海巡 / 留言輪詢是否啟動（跨部署記住）
    if state.get_kv("schedulers_enabled", False):
        scheduler.add_job(poll_replies_job, IntervalTrigger(minutes=2), id="poll_replies", replace_existing=True)
        scheduler.add_job(proactive_patrol_job, IntervalTrigger(minutes=15), id="proactive_patrol", replace_existing=True)
        logger.info("[持久化] schedulers_enabled=True，海巡 / 留言輪詢已自動啟動")
    scheduler.start()
    logger.info("Scheduler 啟動")
    yield
    scheduler.shutdown()


app = FastAPI(title="Threads AI Bot", lifespan=lifespan)


def _notify_topics(topics: list[dict], slot: Optional[str] = None) -> None:
    """把主題清單格式化後發 TG。"""
    slot_label = {"morning": "早上場", "evening": "晚上場"}.get(slot or "", "")
    header = f"📋 {slot_label}今日發文主題" if slot_label else "📋 今日發文主題"
    lines = [header, ""]
    for i, t in enumerate(topics, 1):
        lines.append(f"[{i}] {t['title']}")
        if t.get("summary"):
            lines.append(f"    角度：{t['summary']}")
        lines.append("")
    send_telegram("\n".join(lines).strip())


async def daily_draft_job(slot: str = "morning"):
    logger.info(f"執行每日主題任務 [{slot}]")
    from datetime import datetime
    import pytz
    today = datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d")
    dedup_key = f"last_draft_{slot}"
    if state.get_kv(dedup_key) == today:
        logger.info(f"daily_draft_job [{slot}]: 今日 ({today}) 已產過主題，跳過")
        return
    try:
        excluded = state.get_recent_topics(limit=21)
        topics = generate_daily_topics(excluded_topics=excluded)
        state.set_kv(dedup_key, today)
        state.append_recent_topics([t["title"] for t in topics])
        _notify_topics(topics, slot=slot)
    except Exception as e:
        logger.error(f"每日主題任務 [{slot}] 失敗: {e}")
        notify_error(f"每日主題 [{slot}] 失敗：{e}")


async def poll_replies_job():
    """每 2 分鐘輪詢自己貼文的留言"""
    logger.info("輪詢留言中...")
    client = get_client()
    try:
        posts = client.get_my_posts(limit=5)
        for post in posts:
            replies = client.get_replies(post.id, limit=10)
            for reply in replies:
                if state.is_processed("reply", reply.id):
                    continue
                state.mark_processed("reply", reply.id)
                if not reply.text:
                    continue
                logger.info(f"發現新留言: @{reply.username}: {reply.text[:50]}")
                reply_text = generate_reply(
                    post_text=post.text,
                    comment_text=reply.text,
                    commenter_username=reply.username,
                )
                reply_job_id = str(uuid.uuid4())[:8]
                state.save_pending_reply(reply_job_id, {
                    "reply_id": reply.id,
                    "reply_text": reply_text,
                    "commenter": reply.username,
                    "comment_text": reply.text,
                    "post_text": post.text,
                })
                notify_reply_for_approval(reply_job_id, reply.username, reply.text, reply_text, post.text)
    except Exception as e:
        logger.error(f"輪詢留言失敗: {e}")
        _notify_error_throttled("poll_replies", str(e))
    finally:
        client.close()


async def proactive_patrol_job(force: bool = False):
    """每 15 分鐘執行一次，用瀏覽器 UI 在串文底下直接回覆。force=True 可跳過時段限制。"""
    reset_daily_count()
    session = get_current_session()
    if not session:
        if not force:
            return
        session = "morning"

    quota = PATROL_SCHEDULE[session]["count"]
    used = _get_counts().get(session, 0)
    if used >= quota and not force:
        logger.info(f"[海巡] {session} 時段配額已用完 ({used}/{quota})")
        return

    batch = 2 if force else min(2, quota - used)
    keyword = random.choice(SEARCH_KEYWORDS)
    logger.info(f"[海巡] 搜尋關鍵字：{keyword}，本批次：{batch} 則")

    if not PLAYWRIGHT_AVAILABLE:
        logger.warning("[海巡] Playwright 不可用，跳過本次海巡")
        if force:
            send_telegram("❌ 海巡失敗：Playwright 不可用")
        return

    # Phase 1：一個 browser session 完成搜尋 + Claude 生成 + UI 回覆
    try:
        # search_and_reply_async 會先搜尋並回傳貼文清單；reply_tasks 初始為空
        search_result = await search_and_reply_async(keyword=keyword, reply_tasks=[])
        results = search_result.get("posts", [])
    except Exception as e:
        logger.error(f"[海巡] 爬蟲搜尋失敗: {e}")
        _notify_error_throttled("patrol_search", str(e))
        return

    if not results:
        logger.info(f"[海巡] 關鍵字「{keyword}」無搜尋結果")
        return

    random.shuffle(results)

    reply_tasks = []
    for post in results:
        if len(reply_tasks) >= batch:
            break
        if state.is_processed("proactive", post.shortcode):
            continue
        if not post.text or len(post.text) < 20:
            continue
        if post.age_hours > PATROL_MAX_AGE_HOURS:
            logger.info(f"[海巡] 跳過過舊貼文 @{post.username} age={post.age_hours}h")
            continue
        reply_text = generate_proactive_reply(post_text=post.text, keyword=keyword)
        reply_type = "insurance"
        if not reply_text:
            reply_text = generate_short_reaction(post.text)
            reply_type = "reaction"
        logger.info(f"[海巡] @{post.username} type={reply_type} reply_len={len(reply_text)} preview={reply_text[:40]!r}")
        if not reply_text:
            continue
        state.mark_processed("proactive", post.shortcode)
        reply_tasks.append({
            "shortcode": post.shortcode,
            "username": post.username,
            "text": post.text,
            "reply_text": reply_text,
            "reply_type": reply_type,
        })

    if not reply_tasks:
        logger.info("[海巡] 無合適貼文可回覆")
        if force:
            send_telegram(f"🔍 海巡完成（關鍵字：{keyword}）\n搜到 {len(results)} 篇，無合適內容可回覆")
        return

    # Phase 2：用新的 browser session 做 UI 回覆（帶 reply_tasks，跳過搜尋）
    try:
        result = await search_and_reply_async(keyword=keyword, reply_tasks=reply_tasks, skip_search=True)
        for sc in result.get("replied", []):
            if not force:
                _bump_count(session)
            task = next((t for t in reply_tasks if t["shortcode"] == sc), {})
            logger.info(f"[海巡] UI 回覆成功 @{task.get('username')} shortcode={sc}")
            rtype = task.get("reply_type", "insurance")
            label = "🧠 知識型" if rtype == "insurance" else "👋 曝光型"
            send_telegram(
                f"🔍 海巡回覆通知 {label}\n"
                f"關鍵字：{keyword}\n"
                f"@{task.get('username')}：{task.get('text','')[:80]}...\n"
                f"─────────────\n"
                f"回覆內容：\n{task.get('reply_text','')}"
            )
        for sc in result.get("failed", []):
            task = next((t for t in reply_tasks if t["shortcode"] == sc), {})
            logger.warning(f"[海巡] UI 回覆失敗 @{task.get('username')} shortcode={sc}")
        if force:
            replied_n = len(result.get("replied", []))
            send_telegram(f"🔍 海巡完成（關鍵字：{keyword}）\n搜到 {len(results)} 篇 → 產生回覆 {len(reply_tasks)} 則 → 發出 {replied_n} 則")
    except Exception as e:
        logger.error(f"[海巡] search_and_reply 失敗: {e}")
        _notify_error_throttled("patrol_reply", str(e))
        if force:
            send_telegram(f"❌ 海巡執行失敗：{e}")


async def refresh_token_job():
    try:
        client = get_client()
        result = client.refresh_long_lived_token(
            app_secret=os.environ["THREADS_APP_SECRET"]
        )
        new_token = result["access_token"]
        # 同時寫 state.kv（跨部署保留）+ os.environ（當前 process 立即生效）
        state.set_kv("threads_access_token", new_token)
        os.environ["THREADS_ACCESS_TOKEN"] = new_token
        client.close()
        logger.info("Token 刷新成功，已寫入 state.kv")
    except Exception as e:
        notify_error(f"Token 刷新失敗: {e}")


@app.post("/approve-reply/{reply_job_id}/{action}")
async def approve_reply(reply_job_id: str, action: str):
    job = state.get_pending_reply(reply_job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到此留言任務")
    if job["status"] != "pending":
        raise HTTPException(status_code=409, detail="此任務已處理")
    if action == "skip":
        state.update_reply(reply_job_id, "skipped")
        return {"status": "skipped"}
    client = get_client()
    try:
        new_reply_id = client.reply_to_comment(reply_id=job["reply_id"], text=job["reply_text"])
        state.update_reply(reply_job_id, "replied", {"new_reply_id": new_reply_id})
        # 記住我們已回過此留言 — 下次 poll_replies_job 就不用再打 get_sub_replies
        state.mark_processed("self_replied", job["reply_id"])
        return {"status": "replied", "reply_id": new_reply_id}
    except Exception as e:
        state.update_reply(reply_job_id, "error", {"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


@app.post("/approve/{job_id}/{choice}")
async def approve_draft(job_id: str, choice: str):
    job = state.get_pending_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到此草稿任務")
    if job["status"] != "pending":
        raise HTTPException(status_code=409, detail="此任務已處理")
    if choice == "skip":
        state.update_job(job_id, "skipped")
        return {"status": "skipped"}
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(job["drafts"]):
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=400, detail="choice 需為 1~3 或 skip")
    client = get_client()
    try:
        post_id = client.create_post(text=job["drafts"][idx]["draft"])
        state.update_job(job_id, "published", {"post_id": post_id})
        return {"status": "published", "post_id": post_id}
    except Exception as e:
        state.update_job(job_id, "error", {"error": str(e)})
        notify_error(f"發文失敗: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


@app.post("/webhook/telegram")
async def telegram_message_handler(request: Request):
    import asyncio, re as _re
    payload = await request.json()
    msg = payload.get("message") or payload.get("channel_post") or {}
    text = msg.get("text", "").strip()
    _tg_chat_id = str(msg.get("chat", {}).get("id") or os.getenv("TELEGRAM_CHAT_ID", ""))
    logger.info(f"[TG webhook] 收到: {text!r} chat={_tg_chat_id}")

    # ── 狀態查詢 ───────────────────────────────────────
    if text in ("ping", "狀態", "測試"):
        enabled = state.get_kv("schedulers_enabled", False)
        patrol_status = "▶️ 運行中" if enabled else "⏸ 已暫停"
        send_telegram(f"✅ Bot 正常運作\n海巡狀態：{patrol_status}")
        return JSONResponse({"ok": True})

    # ── 手動海巡（不列入配額）─────────────────────────
    if text in ("海巡測試", "手動海巡"):
        send_telegram("🔍 手動海巡啟動，不列入時段配額，結果將通知你")
        asyncio.create_task(proactive_patrol_job(force=True))
        return JSONResponse({"ok": True})

    # ── 手動產生主題 ──────────────────────────────────
    if text in ("產生草稿", "出草稿", "手動草稿", "產主題", "出主題"):
        async def _manual_topics():
            try:
                send_telegram("✍️ 正在產生主題...")
                excluded = state.get_recent_topics(limit=21)
                topics = generate_daily_topics(excluded_topics=excluded)
                state.append_recent_topics([t["title"] for t in topics])
                _notify_topics(topics, slot=None)
            except Exception as e:
                logger.error(f"手動產生主題失敗: {e}")
                send_telegram(f"❌ 主題產生失敗：{e}")
        asyncio.create_task(_manual_topics())
        return JSONResponse({"ok": True})

    # ── 海巡控制 ──────────────────────────────────────
    if text == "海巡暫停":
        for jid in ("proactive_patrol", "poll_replies"):
            try:
                scheduler.remove_job(jid)
            except Exception:
                pass
        state.set_kv("schedulers_enabled", False)
        send_telegram("⏸ 海巡與留言回覆已暫停（狀態已保存，重啟仍生效）")
        return JSONResponse({"ok": True})

    if text == "海巡繼續":
        try:
            scheduler.add_job(poll_replies_job, IntervalTrigger(minutes=2), id="poll_replies", replace_existing=True)
            scheduler.add_job(proactive_patrol_job, IntervalTrigger(minutes=15), id="proactive_patrol", replace_existing=True)
            state.set_kv("schedulers_enabled", True)
            send_telegram("▶️ 海巡與留言回覆已恢復（狀態已保存，跨部署生效）")
        except Exception as e:
            send_telegram(f"❌ 恢復失敗: {e}")
        return JSONResponse({"ok": True})

    # ── 草稿審核 ──────────────────────────────────────
    # 長版：「選2 abc」、「2 abc」（指定 job_id）
    m = _re.match(r"^(?:選\s*)?([123])\s+(\w+)$", text)
    if m:
        result = await approve_draft(job_id=m.group(2), choice=m.group(1))
        if result.get("status") == "published":
            send_telegram(f"✅ 已發文！post_id: {result.get('post_id')}")
        return JSONResponse({"ok": True})

    # 短版：「選2」、「2」（最新一筆 pending）
    m = _re.match(r"^(?:選\s*)?([123])$", text)
    if m:
        jid = state.latest_pending_job_id()
        if not jid:
            send_telegram("⚠️ 目前沒有待審核的草稿")
            return JSONResponse({"ok": True})
        result = await approve_draft(job_id=jid, choice=m.group(1))
        if result.get("status") == "published":
            send_telegram(f"✅ 已發文！post_id: {result.get('post_id')}")
        return JSONResponse({"ok": True})

    # 跳過 長版
    m = _re.match(r"^跳過\s+(\w+)$", text)
    if m:
        await approve_draft(job_id=m.group(1), choice="skip")
        send_telegram("⏭ 今日發文已跳過")
        return JSONResponse({"ok": True})

    # 跳過 短版
    if text == "跳過":
        jid = state.latest_pending_job_id()
        if not jid:
            send_telegram("⚠️ 目前沒有待審核的草稿")
            return JSONResponse({"ok": True})
        await approve_draft(job_id=jid, choice="skip")
        send_telegram("⏭ 今日發文已跳過")
        return JSONResponse({"ok": True})

    # ── 留言回覆審核 ──────────────────────────────────
    m = _re.match(r"^回覆\s+(\w+)$", text)
    if m:
        result = await approve_reply(reply_job_id=m.group(1), action="send")
        if result.get("status") == "replied":
            send_telegram("✅ 回覆已發出")
        return JSONResponse({"ok": True})

    if text == "回覆":
        rjid = state.latest_pending_reply_id()
        if not rjid:
            send_telegram("⚠️ 目前沒有待審核的留言")
            return JSONResponse({"ok": True})
        result = await approve_reply(reply_job_id=rjid, action="send")
        if result.get("status") == "replied":
            send_telegram("✅ 回覆已發出")
        return JSONResponse({"ok": True})

    m = _re.match(r"^略過\s+(\w+)$", text)
    if m:
        await approve_reply(reply_job_id=m.group(1), action="skip")
        send_telegram("⏭ 已略過此則留言")
        return JSONResponse({"ok": True})

    if text == "略過":
        rjid = state.latest_pending_reply_id()
        if not rjid:
            send_telegram("⚠️ 目前沒有待審核的留言")
            return JSONResponse({"ok": True})
        await approve_reply(reply_job_id=rjid, action="skip")
        send_telegram("⏭ 已略過此則留言")
        return JSONResponse({"ok": True})

    return JSONResponse({"ok": True})


@app.get("/admin/test-tg")
async def test_tg_send():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    msg_id = send_telegram("🔧 TG 連線測試 OK")
    return {"token_prefix": token[:8] if token else "MISSING", "chat_id": chat_id, "message_id": msg_id, "ok": msg_id is not None}


@app.post("/admin/trigger-draft")
async def manual_trigger():
    """手動觸發主題產生，不影響排程的 dedup key。"""
    try:
        excluded = state.get_recent_topics(limit=21)
        topics = generate_daily_topics(excluded_topics=excluded)
        state.append_recent_topics([t["title"] for t in topics])
        _notify_topics(topics, slot=None)
    except Exception as e:
        notify_error(f"手動觸發主題失敗: {e}")
    return {"status": "triggered"}


@app.post("/admin/trigger-poll")
async def manual_poll():
    await poll_replies_job()
    return {"status": "polled"}


@app.post("/admin/trigger-patrol")
async def manual_patrol():
    """非阻塞觸發海巡，結果透過 Telegram 通知。"""
    import asyncio
    asyncio.create_task(proactive_patrol_job(force=True))
    return {"status": "started", "message": "海巡已在背景執行，結果將透過 Telegram 通知"}


@app.get("/admin/patrol-stats")
async def patrol_stats():
    return {"daily_count": _get_counts()}


@app.get("/admin/stats")
async def admin_stats():
    """Bot 整體狀態快照 — 給人看的儀表板。"""
    jobs_info = []
    for j in scheduler.get_jobs():
        jobs_info.append({
            "id": j.id,
            "next_run": str(j.next_run_time) if j.next_run_time else None,
        })
    return {
        "pending": {
            "jobs": state.count_pending_jobs(),
            "replies": state.count_pending_replies(),
        },
        "last_draft": {
            "morning": state.get_kv("last_draft_morning"),
            "evening": state.get_kv("last_draft_evening"),
        },
        "patrol": {
            "schedulers_enabled": state.get_kv("schedulers_enabled", False),
            "daily_count": _get_counts(),
        },
        "processed_ids": {
            "reply": state.count_processed("reply"),
            "proactive": state.count_processed("proactive"),
            "self_replied": state.count_processed("self_replied"),
        },
        "scheduler_jobs": jobs_info,
        "token_in_state_kv": bool(state.get_kv("threads_access_token")),
    }


@app.get("/admin/env-check")
async def env_check():
    keys = [k for k in os.environ if "THREAD" in k.upper() or "TELEGRAM" in k.upper() or "ANTHROPIC" in k.upper() or "PLAYWRIGHT" in k.upper()]
    return {"keys_found": sorted(keys)}


@app.get("/admin/test-search")
async def test_search(keyword: str = Query(default="保險")):
    if not PLAYWRIGHT_AVAILABLE:
        return {"error": "Playwright 不可用"}
    import io, logging as _logging
    buf = io.StringIO()
    handler = _logging.StreamHandler(buf)
    handler.setLevel(_logging.DEBUG)
    for name in ("threads_scraper", "main"):
        _logging.getLogger(name).addHandler(handler)
    try:
        results = await search_threads_by_keyword_async(keyword=keyword, limit=5)
        logs = buf.getvalue()
        return {
            "keyword": keyword,
            "count": len(results),
            "results": [{"shortcode": p.shortcode, "username": p.username, "text": p.text[:80]} for p in results],
            "logs": logs,
        }
    except Exception as e:
        return {"error": str(e), "logs": buf.getvalue()}
    finally:
        for name in ("threads_scraper", "main"):
            _logging.getLogger(name).removeHandler(handler)


@app.get("/admin/test-login")
async def test_login():
    if not PLAYWRIGHT_AVAILABLE:
        return {"error": "Playwright 不可用"}
    from playwright.async_api import async_playwright
    import asyncio
    steps = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36")
            page = await context.new_page()

            await page.goto("https://www.threads.com/login", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            url_after_goto = page.url
            steps.append(f"login URL: {url_after_goto}")

            if "login" not in url_after_goto.lower():
                steps.append("已登入（redirect 成功）")
                await browser.close()
                return {"logged_in": True, "steps": steps}

            # 嘗試填寫帳密
            username = os.environ.get("THREADS_USERNAME", "")
            password = os.environ.get("THREADS_PASSWORD", "")
            steps.append(f"username env: {'已設定' if username else '未設定'}, password env: {'已設定' if password else '未設定'}")

            try:
                await page.wait_for_selector('input[autocomplete="username"]', timeout=10000)
                steps.append("找到 username input")
                await page.fill('input[autocomplete="username"]', username)
                await page.fill('input[type="password"]', password)
                await page.press('input[type="password"]', "Enter")
                await asyncio.sleep(5)
                steps.append(f"送出後 URL: {page.url}")
                logged_in = "login" not in page.url.lower()
            except Exception as e:
                steps.append(f"填表失敗: {e}")
                logged_in = False

            await browser.close()
            return {"logged_in": logged_in, "steps": steps}
    except Exception as e:
        return {"error": str(e), "steps": steps}


@app.get("/admin/pending-jobs")
async def list_pending_jobs():
    return {jid: {"status": j["status"], "draft_count": len(j.get("drafts", []))} for jid, j in state.list_pending_jobs().items()}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/admin/test-playwright")
async def test_playwright():
    if not PLAYWRIGHT_AVAILABLE:
        return {"playwright": False, "reason": "import failed"}
    try:
        from playwright.async_api import async_playwright
        import os
        browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "not set")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            version = browser.version
            await browser.close()
        return {"playwright": True, "chromium_version": version, "browser_path": browser_path}
    except Exception as e:
        return {"playwright": False, "error": str(e), "browser_path": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "not set")}


@app.get("/control", response_class=HTMLResponse)
async def control_panel():
    return HTMLResponse(content=open("/app/control.html", "r", encoding="utf-8").read())
