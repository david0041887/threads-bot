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

from threads_client import ThreadsClient
from ai_generator import generate_post_drafts, generate_reply, generate_daily_topics, generate_proactive_reply
from notifier import notify_drafts_for_approval, notify_error, notify_reply_for_approval, send_telegram
try:
    from threads_scraper import search_threads_by_keyword_async, search_and_reply_async
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright 未安裝，海巡功能停用")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
pending_jobs: dict[str, dict] = {}      # 啟動後從磁碟載入
pending_replies: dict[str, dict] = {}   # 啟動後從磁碟載入
processed_proactive_ids: set = set()

# Telegram message_id → {"type": "draft"|"reply", "job_id": str}
# 用於直接回覆 TG 訊息審核，無需輸入 job_id
tg_msg_to_job: dict[int, dict] = {}

_PROCESSED_FILE = Path("processed_reply_ids.json")
_PENDING_JOBS_FILE = Path("pending_jobs.json")
_PENDING_REPLIES_FILE = Path("pending_replies.json")

def _load_processed_ids() -> set:
    try:
        return set(json.loads(_PROCESSED_FILE.read_text()))
    except Exception:
        return set()

def _save_processed_ids(ids: set):
    try:
        _PROCESSED_FILE.write_text(json.dumps(list(ids)))
    except Exception as e:
        logger.warning(f"無法儲存已處理留言 ID: {e}")

def _load_pending_jobs() -> dict:
    try:
        return json.loads(_PENDING_JOBS_FILE.read_text())
    except Exception:
        return {}

def _save_pending_jobs():
    try:
        _PENDING_JOBS_FILE.write_text(json.dumps(pending_jobs, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"無法儲存 pending_jobs: {e}")

def _load_pending_replies() -> dict:
    try:
        return json.loads(_PENDING_REPLIES_FILE.read_text())
    except Exception:
        return {}

def _save_pending_replies():
    try:
        _PENDING_REPLIES_FILE.write_text(json.dumps(pending_replies, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"無法儲存 pending_replies: {e}")

processed_reply_ids: set = _load_processed_ids()
pending_jobs.update(_load_pending_jobs())
pending_replies.update(_load_pending_replies())

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
    _ensure_chromium()
    scheduler.add_job(daily_draft_job, CronTrigger(hour=8, minute=0, timezone="Asia/Taipei"), id="daily_draft", replace_existing=True)
    # poll_replies 暫停（待確認 UI 回覆正常後啟用）
    # scheduler.add_job(poll_replies_job, IntervalTrigger(minutes=2), id="poll_replies", replace_existing=True)
    # 海巡暫停（UI 回覆測試中）
    # scheduler.add_job(proactive_patrol_job, IntervalTrigger(minutes=15), id="proactive_patrol", replace_existing=True)
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
        _save_pending_jobs()
        msg_id = notify_drafts_for_approval(drafts, job_id=job_id)
        if msg_id:
            tg_msg_to_job[msg_id] = {"type": "draft", "job_id": job_id}
    except Exception as e:
        logger.error(f"每日草稿任務失敗: {e}")
        notify_error(str(e))


async def poll_replies_job():
    """每 2 分鐘輪詢自己貼文的留言"""
    logger.info("輪詢留言中...")
    client = get_client()
    my_username = os.environ.get("THREADS_USERNAME", "").lower()
    try:
        posts = client.get_my_posts(limit=5)
        for post in posts:
            replies = client.get_replies(post.id, limit=10)
            for reply in replies:
                if reply.id in processed_reply_ids:
                    continue
                # 若我們帳號已回覆過此留言，直接標記處理並跳過
                if my_username:
                    sub_replies = client.get_sub_replies(reply.id)
                    if any(sr.username.lower() == my_username for sr in sub_replies):
                        processed_reply_ids.add(reply.id)
                        _save_processed_ids(processed_reply_ids)
                        logger.info(f"已跳過已回覆留言 @{reply.username}: {reply.text[:30]}")
                        continue
                processed_reply_ids.add(reply.id)
                _save_processed_ids(processed_reply_ids)
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
                _save_pending_replies()
                msg_id = notify_reply_for_approval(reply_job_id, reply.username, reply.text, reply_text, post.text)
                if msg_id:
                    tg_msg_to_job[msg_id] = {"type": "reply", "job_id": reply_job_id}
    except Exception as e:
        logger.error(f"輪詢留言失敗: {e}")
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
    used = daily_proactive_count[session]
    if used >= quota:
        logger.info(f"[海巡] {session} 時段配額已用完 ({used}/{quota})")
        return

    batch = min(2, quota - used)
    keyword = random.choice(SEARCH_KEYWORDS)
    logger.info(f"[海巡] 搜尋關鍵字：{keyword}，本批次：{batch} 則")

    if not PLAYWRIGHT_AVAILABLE:
        logger.warning("[海巡] Playwright 不可用，跳過本次海巡")
        return

    # Phase 1：一個 browser session 完成搜尋 + Claude 生成 + UI 回覆
    try:
        # search_and_reply_async 會先搜尋並回傳貼文清單；reply_tasks 初始為空
        search_result = await search_and_reply_async(keyword=keyword, reply_tasks=[])
        results = search_result.get("posts", [])
    except Exception as e:
        logger.error(f"[海巡] 爬蟲搜尋失敗: {e}")
        return

    if not results:
        logger.info(f"[海巡] 關鍵字「{keyword}」無搜尋結果")
        return

    random.shuffle(results)

    reply_tasks = []
    for post in results:
        if len(reply_tasks) >= batch:
            break
        if post.shortcode in processed_proactive_ids:
            continue
        if not post.text or len(post.text) < 20:
            continue
        reply_text = generate_proactive_reply(post_text=post.text, keyword=keyword)
        logger.info(f"[海巡] @{post.username} reply_len={len(reply_text)} preview={reply_text[:40]!r}")
        if not reply_text:
            continue
        processed_proactive_ids.add(post.shortcode)
        reply_tasks.append({
            "shortcode": post.shortcode,
            "username": post.username,
            "text": post.text,
            "reply_text": reply_text,
        })

    if not reply_tasks:
        logger.info("[海巡] 無合適貼文可回覆")
        return

    # Phase 2：用新的 browser session 做 UI 回覆（帶 reply_tasks）
    try:
        result = await search_and_reply_async(keyword=keyword, reply_tasks=reply_tasks)
        for sc in result.get("replied", []):
            daily_proactive_count[session] += 1
            task = next((t for t in reply_tasks if t["shortcode"] == sc), {})
            logger.info(f"[海巡] UI 回覆成功 @{task.get('username')} shortcode={sc}")
            send_telegram(
                f"🔍 海巡回覆通知\n"
                f"關鍵字：{keyword}\n"
                f"@{task.get('username')}：{task.get('text','')[:80]}...\n"
                f"─────────────\n"
                f"回覆內容：\n{task.get('reply_text','')}"
            )
        for sc in result.get("failed", []):
            task = next((t for t in reply_tasks if t["shortcode"] == sc), {})
            logger.warning(f"[海巡] UI 回覆失敗 @{task.get('username')} shortcode={sc}")
    except Exception as e:
        logger.error(f"[海巡] search_and_reply 失敗: {e}")


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
        _save_pending_replies()
        return {"status": "skipped"}
    client = get_client()
    try:
        new_reply_id = client.reply_to_comment(reply_id=job["reply_id"], text=job["reply_text"])
        job["status"] = "replied"
        _save_pending_replies()
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
        _save_pending_jobs()
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
        _save_pending_jobs()
        return {"status": "published", "post_id": post_id}
    except Exception as e:
        # 刻意不更改 status，保持 pending，讓用戶可以重試
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


def _parse_draft_from_tg_msg(tg_text: str, choice: int) -> str:
    """從 TG 草稿審核訊息解析出指定編號的草稿內容（伺服器重啟後 job 消失時使用）。"""
    import re as _re2
    SEP = "─" * 20
    parts = _re2.split(r'【\d+】', tg_text)
    if len(parts) <= choice:
        return ""
    section = parts[choice]
    if SEP in section:
        after = section.split(SEP, 1)[1]
        lines = after.strip().split("\n")
        draft_lines = []
        for line in lines:
            if line.startswith("💬") or line.startswith("[job:"):
                break
            draft_lines.append(line)
        return "\n".join(draft_lines).strip()
    return ""


@app.post("/webhook/telegram")
async def telegram_message_handler(request: Request):
    import asyncio, re as _re
    payload = await request.json()
    msg = payload.get("message", {})
    text = msg.get("text", "").strip()

    # ── 直接回覆 TG 訊息審核 ─────────────────────────
    replied_msg = msg.get("reply_to_message", {})
    replied_msg_id = replied_msg.get("message_id")

    # 優先從記憶體 map 取，fallback 從訊息文字解析 job_id（重啟後仍可用）
    job_info = tg_msg_to_job.get(replied_msg_id) if replied_msg_id else None
    if not job_info and replied_msg:
        replied_text = replied_msg.get("text", "")
        draft_m = _re.search(r'\[job:(\w+)\]', replied_text)
        reply_m = _re.search(r'\[reply_job:(\w+)\]', replied_text)
        if draft_m:
            job_info = {"type": "draft", "job_id": draft_m.group(1)}
        elif reply_m:
            job_info = {"type": "reply", "job_id": reply_m.group(1)}

    if job_info:
        cmd = text.strip()

        if job_info["type"] == "draft":
            if cmd in ("跳過", "skip"):
                try:
                    await approve_draft(job_id=job_info["job_id"], choice="skip")
                except Exception:
                    pass
                send_telegram("⏭ 今日發文已跳過")
            elif cmd in ("1", "2", "3"):
                try:
                    result = await approve_draft(job_id=job_info["job_id"], choice=cmd)
                    if result.get("status") == "published":
                        send_telegram(f"✅ 草稿 {cmd} 已發出！post_id: {result.get('post_id')}")
                    else:
                        send_telegram(f"❌ 發文失敗：{result}")
                except HTTPException as e:
                    if e.status_code == 404:
                        # 伺服器重啟後 job 消失，從 TG 訊息文字直接解析草稿並發文
                        draft_text = _parse_draft_from_tg_msg(
                            replied_msg.get("text", ""), int(cmd)
                        )
                        if draft_text:
                            client = get_client()
                            try:
                                post_id = client.create_post(text=draft_text)
                                send_telegram(f"✅ 草稿 {cmd} 已發出！post_id: {post_id}")
                            except Exception as post_err:
                                send_telegram(f"❌ 發文失敗：{post_err}")
                            finally:
                                client.close()
                        else:
                            send_telegram("❌ 找不到草稿內容，請重新觸發草稿（/admin/trigger-draft）")
                    elif e.status_code == 500:
                        send_telegram(f"❌ Threads 伺服器錯誤，草稿仍保留\n請重新回覆「{cmd}」重試")
                    else:
                        send_telegram(f"❌ 發文失敗：{e.detail}")
                except Exception as e:
                    send_telegram(f"❌ 發文失敗：{e}")
            else:
                send_telegram("❓ 請回覆「1」「2」「3」選草稿，或「跳過」略過")
            return JSONResponse({"ok": True})

        if job_info["type"] == "reply":
            if cmd in ("略過", "跳過", "skip"):
                try:
                    await approve_reply(reply_job_id=job_info["job_id"], action="skip")
                except Exception:
                    pass
                send_telegram("⏭ 已略過此則留言")
            else:
                try:
                    result = await approve_reply(reply_job_id=job_info["job_id"], action="send")
                    if result.get("status") == "replied":
                        send_telegram("✅ 回覆已發出")
                    else:
                        send_telegram(f"❌ 回覆失敗：{result}")
                except HTTPException as e:
                    send_telegram(f"❌ 回覆任務已過期（伺服器重啟導致），請等待下次留言輪詢重新產生")
                except Exception as e:
                    send_telegram(f"❌ 回覆失敗：{e}")
            return JSONResponse({"ok": True})

    # ── 草稿觸發 ──────────────────────────────────────
    if text == "觸發草稿":
        send_telegram("⏳ 正在產生草稿，請稍候...")
        try:
            await daily_draft_job()
        except Exception as e:
            send_telegram(f"❌ 草稿產生失敗：{e}")
        return JSONResponse({"ok": True})

    # ── 海巡控制 ──────────────────────────────────────
    if text == "海巡暫停":
        try:
            scheduler.remove_job("proactive_patrol")
        except Exception:
            pass
        try:
            scheduler.remove_job("poll_replies")
        except Exception:
            pass
        send_telegram("⏸ 海巡與留言回覆已暫停")
        return JSONResponse({"ok": True})

    if text == "海巡繼續":
        try:
            scheduler.add_job(poll_replies_job, IntervalTrigger(minutes=2), id="poll_replies", replace_existing=True)
            scheduler.add_job(proactive_patrol_job, IntervalTrigger(minutes=15), id="proactive_patrol", replace_existing=True)
            send_telegram("▶️ 海巡與留言回覆已恢復（每 15 分鐘海巡一次）")
        except Exception as e:
            send_telegram(f"❌ 恢復失敗: {e}")
        return JSONResponse({"ok": True})

    # ── 草稿審核（舊式：選 1 job_id）──────────────────
    m = _re.match(r"選\s*([123])\s+(\w+)", text)
    if m:
        try:
            result = await approve_draft(job_id=m.group(2), choice=m.group(1))
            if result.get("status") == "published":
                send_telegram(f"✅ 已發文！post_id: {result.get('post_id')}")
        except Exception as e:
            send_telegram(f"❌ 發文失敗：{e}")
        return JSONResponse({"ok": True})
    m2 = _re.match(r"跳過\s+(\w+)", text)
    if m2:
        try:
            await approve_draft(job_id=m2.group(1), choice="skip")
        except Exception:
            pass
        send_telegram("⏭ 今日發文已跳過")
        return JSONResponse({"ok": True})

    # ── 留言回覆審核（舊式：回覆 job_id）─────────────
    m3 = _re.match(r"回覆\s+(\w+)", text)
    if m3:
        try:
            result = await approve_reply(reply_job_id=m3.group(1), action="send")
            if result.get("status") == "replied":
                send_telegram("✅ 回覆已發出")
        except Exception as e:
            send_telegram(f"❌ 回覆失敗：{e}")
        return JSONResponse({"ok": True})
    m4 = _re.match(r"略過\s+(\w+)", text)
    if m4:
        try:
            await approve_reply(reply_job_id=m4.group(1), action="skip")
        except Exception:
            pass
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
    """非阻塞觸發海巡，結果透過 Telegram 通知。"""
    import asyncio
    asyncio.create_task(proactive_patrol_job(force=True))
    return {"status": "started", "message": "海巡已在背景執行，結果將透過 Telegram 通知"}


@app.get("/admin/patrol-stats")
async def patrol_stats():
    return {"daily_count": daily_proactive_count}


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
    return {jid: {"status": j["status"], "draft_count": len(j.get("drafts", []))} for jid, j in pending_jobs.items()}


_DEBUG_SCREENSHOT_PATH = "/tmp/ui_debug.png"


@app.post("/admin/test-ui-reply")
async def test_ui_reply(request: Request):
    """
    測試 UI 回覆功能。
    Body: {"post_url": "...", "reply_text": "...", "dry_run": true}
    dry_run=true (預設) 只截圖，不實際送出回覆。
    截圖儲存於 /tmp/ui_debug.png，可透過 GET /admin/debug-screenshot 查看。
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {"error": "Playwright 不可用"}
    body = await request.json()
    post_url = body.get("post_url", "")
    reply_text = body.get("reply_text", "（測試用，請忽略）")
    dry_run = body.get("dry_run", True)
    if not post_url:
        return {"error": "缺少 post_url"}

    import os as _os
    from playwright.async_api import async_playwright
    from threads_scraper import _ui_reply_to_post
    import json as _json, asyncio as _asyncio

    steps = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
            )
            cookies_env = _os.environ.get("THREADS_COOKIES", "")
            if cookies_env:
                raw = _json.loads(cookies_env)
                pw_cookies = []
                for c in raw:
                    pw = {"name": c["name"], "value": c["value"],
                          "domain": c.get("domain", ".threads.com"), "path": c.get("path", "/")}
                    exp = c.get("expirationDate") or c.get("expires")
                    if exp and exp > 0:
                        pw["expires"] = int(exp)
                    ss = c.get("sameSite") or "Lax"
                    pw["sameSite"] = {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}.get(ss.lower(), "Lax")
                    pw_cookies.append(pw)
                await context.add_cookies(pw_cookies)
                steps.append(f"載入 {len(pw_cookies)} 個 cookies")

            page = await context.new_page()
            await page.goto("https://www.threads.com/", wait_until="domcontentloaded", timeout=25000)
            await _asyncio.sleep(2)
            logged_in = "login" not in page.url.lower()
            steps.append(f"登入: {'已登入' if logged_in else '未登入'} ({page.url})")

            if dry_run:
                await page.goto(post_url, wait_until="domcontentloaded", timeout=25000)
                await _asyncio.sleep(3)
                await page.screenshot(path=_DEBUG_SCREENSHOT_PATH, full_page=False)
                steps.append(f"dry_run 截圖完成 → GET /admin/debug-screenshot 查看")
                result = None
            else:
                result = await _ui_reply_to_post(page, post_url, reply_text, screenshot_path=_DEBUG_SCREENSHOT_PATH)
                steps.append(f"UI 回覆: {'成功' if result else '失敗'} → GET /admin/debug-screenshot 查看截圖")

            await browser.close()

        return {"success": result, "dry_run": dry_run, "steps": steps}
    except Exception as e:
        return {"error": str(e), "steps": steps}


@app.get("/admin/debug-screenshot")
async def debug_screenshot():
    """查看最新一次 test-ui-reply 的截圖。"""
    import os as _os
    from fastapi.responses import FileResponse
    if not _os.path.exists(_DEBUG_SCREENSHOT_PATH):
        return {"error": "截圖不存在，請先呼叫 POST /admin/test-ui-reply"}
    return FileResponse(_DEBUG_SCREENSHOT_PATH, media_type="image/png")


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
