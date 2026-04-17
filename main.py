"""
main.py
FastAPI 主程式 — 含主動海巡功能
"""

import os
import uuid
import logging
import random
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from threads_client import ThreadsClient
from ai_generator import generate_post_drafts, generate_reply, generate_daily_topics, generate_proactive_reply
from notifier import notify_drafts_for_approval, notify_error, notify_reply_for_approval, send_telegram
from threads_scraper import search_threads_by_keyword_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
pending_jobs: dict[str, dict] = {}
pending_replies: dict[str, dict] = {}
processed_reply_ids: set = set()
processed_proactive_ids: set = set()

SEARCH_KEYWORDS = [
    "保險", "壽險", "醫療險", "遺產稅", "節稅", "保費",
    "保單", "理賠", "投資型保單", "重大傷病", "實支實付",
    "保障規劃", "退休規劃", "保險稅務"
]

# 每日海巡配額
PATROL_SCHEDULE = {
    "morning": {"hour": "7-9", "count": 6},
    "noon":    {"hour": "12-13", "count": 7},
    "evening": {"hour": "20-23", "count": 8},
}

# 當日已發出的主動回覆數
daily_proactive_count = {"morning": 0, "noon": 0, "evening": 0, "date": ""}


def get_client() -> ThreadsClient:
    return ThreadsClient(
        access_token=os.environ["THREADS_ACCESS_TOKEN"],
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
    if daily_proactive_count["date"] != today:
        daily_proactive_count.update({"morning": 0, "noon": 0, "evening": 0, "date": today})


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(daily_draft_job, CronTrigger(hour=8, minute=0, timezone="Asia/Taipei"), id="daily_draft", replace_existing=True)
    scheduler.add_job(poll_replies_job, IntervalTrigger(minutes=2), id="poll_replies", replace_existing=True)
    scheduler.add_job(proactive_patrol_job, IntervalTrigger(minutes=15), id="proactive_patrol", replace_existing=True)
    scheduler.add_job(refresh_token_job, CronTrigger(month="*/2", day="1", hour=3, minute=0, timezone="Asia/Taipei"), id="token_refresh", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler 啟動")
    yield
    scheduler.shutdown()


app = FastAPI(title="Threads AI Bot", lifespan=lifespan)


async def daily_draft_job():
    logger.info("執行每日草稿任務")
    try:
        articles = generate_daily_topics()
        drafts = generate_post_drafts(articles, count=3)
        job_id = str(uuid.uuid4())[:8]
        pending_jobs[job_id] = {"drafts": drafts, "status": "pending"}
        notify_drafts_for_approval(drafts, job_id=job_id)
    except Exception as e:
        logger.error(f"每日草稿任務失敗: {e}")
        notify_error(str(e))


async def poll_replies_job():
    """每 2 分鐘輪詢自己貼文的留言"""
    logger.info("輪詢留言中...")
    client = get_client()
    try:
        posts = client.get_my_posts(limit=5)
        for post in posts:
            replies = client.get_replies(post.id, limit=10)
            for reply in replies:
                if reply.id in processed_reply_ids:
                    continue
                processed_reply_ids.add(reply.id)
                if not reply.text:
                    continue
                logger.info(f"發現新留言: @{reply.username}: {reply.text[:50]}")
                reply_text = generate_reply(
                    post_text=post.text,
                    comment_text=reply.text,
                    commenter_username=reply.username,
                )
                reply_job_id = str(uuid.uuid4())[:8]
                pending_replies[reply_job_id] = {
                    "reply_id": reply.id,
                    "reply_text": reply_text,
                    "commenter": reply.username,
                    "comment_text": reply.text,
                    "post_text": post.text,
                    "status": "pending",
                }
                notify_reply_for_approval(reply_job_id, reply.username, reply.text, reply_text, post.text)
    except Exception as e:
        logger.error(f"輪詢留言失敗: {e}")
    finally:
        client.close()


async def proactive_patrol_job(force: bool = False):
    """每 15 分鐘執行一次，在對應時段主動回覆保險相關貼文。force=True 可跳過時段限制。"""
    reset_daily_count()
    session = get_current_session()
    if not session:
        if not force:
            return
        session = "morning"  # 強制執行時使用 morning 配額

    quota = PATROL_SCHEDULE[session]["count"]
    used = daily_proactive_count[session]
    if used >= quota:
        logger.info(f"[海巡] {session} 時段配額已用完 ({used}/{quota})")
        return

    # 每次最多回覆 2 則，避免短時間大量操作
    batch = min(2, quota - used)
    keyword = random.choice(SEARCH_KEYWORDS)
    logger.info(f"[海巡] 搜尋關鍵字：{keyword}，本批次：{batch} 則")

    # 用 Playwright 爬蟲搜尋他人公開貼文
    try:
        results = await search_threads_by_keyword_async(keyword=keyword, limit=20)
    except Exception as e:
        logger.error(f"[海巡] 爬蟲搜尋失敗: {e}")
        return

    if not results:
        logger.info(f"[海巡] 關鍵字「{keyword}」無搜尋結果")
        return

    random.shuffle(results)
    client = get_client()
    replied = 0
    try:
        for post in results:
            if replied >= batch:
                break
            if post.shortcode in processed_proactive_ids:
                continue
            if not post.text or len(post.text) < 20:
                continue

            processed_proactive_ids.add(post.shortcode)

            reply_text = generate_proactive_reply(
                post_text=post.text,
                keyword=keyword,
            )

            if not reply_text:
                continue

            try:
                new_reply_id = client.reply_to_comment(reply_id=post.id, text=reply_text)
                replied += 1
                daily_proactive_count[session] += 1
                logger.info(f"[海巡] 已回覆 @{post.username} (shortcode={post.shortcode})")

                send_telegram(
                    f"🔍 海巡回覆通知\n"
                    f"關鍵字：{keyword}\n"
                    f"@{post.username}：{post.text[:80]}...\n"
                    f"─────────────\n"
                    f"回覆內容：\n{reply_text}"
                )
            except Exception as e:
                logger.error(f"[海巡] 回覆失敗 (shortcode={post.shortcode}): {e}")

    except Exception as e:
        logger.error(f"[海巡] 執行失敗: {e}")
    finally:
        client.close()


async def refresh_token_job():
    try:
        client = get_client()
        result = client.refresh_long_lived_token(
            app_secret=os.environ["THREADS_APP_SECRET"]
        )
        os.environ["THREADS_ACCESS_TOKEN"] = result["access_token"]
        client.close()
    except Exception as e:
        notify_error(f"Token 刷新失敗: {e}")


@app.post("/approve-reply/{reply_job_id}/{action}")
async def approve_reply(reply_job_id: str, action: str):
    job = pending_replies.get(reply_job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到此留言任務")
    if job["status"] != "pending":
        raise HTTPException(status_code=409, detail="此任務已處理")
    if action == "skip":
        job["status"] = "skipped"
        return {"status": "skipped"}
    client = get_client()
    try:
        new_reply_id = client.reply_to_comment(reply_id=job["reply_id"], text=job["reply_text"])
        job["status"] = "replied"
        return {"status": "replied", "reply_id": new_reply_id}
    except Exception as e:
        job["status"] = "error"
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


@app.post("/approve/{job_id}/{choice}")
async def approve_draft(job_id: str, choice: str):
    job = pending_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到此草稿任務")
    if job["status"] != "pending":
        raise HTTPException(status_code=409, detail="此任務已處理")
    if choice == "skip":
        job["status"] = "skipped"
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
        job["status"] = "published"
        job["post_id"] = post_id
        return {"status": "published", "post_id": post_id}
    except Exception as e:
        job["status"] = "error"
        notify_error(f"發文失敗: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


@app.post("/webhook/telegram")
async def telegram_message_handler(request: Request):
    payload = await request.json()
    text = payload.get("message", {}).get("text", "").strip()
    import re
    m = re.match(r"選\s*([123])\s+(\w+)", text)
    if m:
        result = await approve_draft(job_id=m.group(2), choice=m.group(1))
        if result.get("status") == "published":
            send_telegram(f"✅ 已發文！post_id: {result.get('post_id')}")
        return JSONResponse({"ok": True})
    m2 = re.match(r"跳過\s+(\w+)", text)
    if m2:
        await approve_draft(job_id=m2.group(1), choice="skip")
        send_telegram("⏭ 今日發文已跳過")
        return JSONResponse({"ok": True})
    m3 = re.match(r"回覆\s+(\w+)", text)
    if m3:
        result = await approve_reply(reply_job_id=m3.group(1), action="send")
        if result.get("status") == "replied":
            send_telegram("✅ 回覆已發出")
        return JSONResponse({"ok": True})
    m4 = re.match(r"略過\s+(\w+)", text)
    if m4:
        await approve_reply(reply_job_id=m4.group(1), action="skip")
        send_telegram("⏭ 已略過此則留言")
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": True})


@app.post("/admin/trigger-draft")
async def manual_trigger():
    await daily_draft_job()
    return {"status": "triggered"}


@app.post("/admin/trigger-poll")
async def manual_poll():
    await poll_replies_job()
    return {"status": "polled"}


@app.post("/admin/trigger-patrol")
async def manual_patrol():
    await proactive_patrol_job(force=True)
    return {"status": "patrolled"}


@app.get("/admin/patrol-stats")
async def patrol_stats():
    return {"daily_count": daily_proactive_count}


@app.get("/admin/pending-jobs")
async def list_pending_jobs():
    return {jid: {"status": j["status"], "draft_count": len(j.get("drafts", []))} for jid, j in pending_jobs.items()}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/control", response_class=HTMLResponse)
async def control_panel():
    return HTMLResponse(content=open("/app/control.html", "r", encoding="utf-8").read())
