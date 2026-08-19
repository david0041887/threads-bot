"""
remote_browser.py
把需要登入瀏覽器的動作（海巡搜尋、UI 回覆）委派給本機 worker 執行。

## 為什麼

Threads 的 session cookie 誕生在使用者家裡的 Chrome、住宅 IP、台灣。拿到 Railway
的資料中心 IP 上重複使用，對 Meta 而言是「同一個帳號的 session 每小時從地球另一端
出現一次」——實測 cookie 撐不過一兩天，而排程關閉時同一份 cookie 可以撐四週。
把瀏覽器動作搬回本機（IP 與 cookie 同源）才是根治，調頻率只是延後死亡時間。

## 分工

- Railway：排程、去重、AI 判斷、Telegram 推播、發文（官方 API，穩，不搬）
- 本機筆電：只做「用已登入的瀏覽器抓資料 / 送出留言」

## 為什麼是本機來領工，而不是 Railway 呼叫本機

反向（Railway → 本機）需要固定位址、打洞或常駐 VPN，家用網路一換 IP 就斷。
本機主動 poll 只需要一條 outbound HTTPS，筆電關機時任務就留在佇列自然過期。

本模組是 Railway 端的代理，介面與 threads_scraper.search_and_reply_async 完全一致，
所以 main.py 只要換一個 import 來源，海巡的後續流程一行都不必動。
"""

import os
import time
import uuid
import asyncio
import logging

import state
from models import ScrapedPost

logger = logging.getLogger(__name__)

# 本機 worker 最久允許跑多久。海巡一次含捲動、is_reply 判定與可能的 UI 回覆，
# 正常兩三分鐘；給 8 分鐘的餘裕，逾時就當本機沒開。
TASK_TIMEOUT_S = int(os.environ.get("BROWSER_WORKER_TIMEOUT", "480"))
_POLL_S = 3

# 本機沒回應時要不要退回 Railway 自己爬。預設關閉：那條路正是殺 cookie 的路，
# 「今天少一次海巡」遠比「cookie 又被作廢、接下來幾天全停」便宜。
FALLBACK_LOCAL = os.environ.get("BROWSER_WORKER_FALLBACK_LOCAL", "").lower() in ("1", "true", "yes")

# worker 幾分鐘內回報過就算在線
WORKER_ONLINE_WINDOW_S = 300


def worker_online() -> bool:
    seen = state.worker_last_seen()
    if not seen:
        return False
    return (time.time() - seen.get("at", 0)) < WORKER_ONLINE_WINDOW_S


def worker_status_text() -> str:
    seen = state.worker_last_seen()
    if not seen:
        return "本機 worker：從未連線"
    ago = (time.time() - seen.get("at", 0)) / 60
    mark = "🟢 在線" if worker_online() else "🔴 離線"
    return f"本機 worker：{mark}（{seen.get('worker', '?')}，{ago:.0f} 分鐘前回報）"


async def search_and_reply_async(
    keyword: str,
    reply_tasks: list[dict],
    skip_search: bool = False,
    search_mode: str = "recent",
) -> dict:
    """介面與 threads_scraper 的同名函式一致，實際工作交給本機 worker。"""
    if not worker_online():
        logger.warning("[派工] 本機 worker 離線")
        if FALLBACK_LOCAL:
            logger.warning("[派工] 退回 Railway 自行執行（會消耗 cookie 壽命）")
            return await _run_locally(keyword, reply_tasks, skip_search, search_mode)
        return {"posts": [], "replied": [], "failed": [t.get("shortcode") for t in reply_tasks],
                "error": "worker_offline"}

    task_id = uuid.uuid4().hex[:16]
    state.enqueue_browser_task(task_id, {
        "action": "search_and_reply",
        "keyword": keyword,
        "reply_tasks": reply_tasks,
        "skip_search": skip_search,
        "search_mode": search_mode,
    })
    logger.info(f"[派工] {task_id} 已排入佇列（關鍵字「{keyword}」，{len(reply_tasks)} 則回覆）")

    deadline = time.time() + TASK_TIMEOUT_S
    while time.time() < deadline:
        await asyncio.sleep(_POLL_S)
        task = state.get_browser_task(task_id)
        if not task or task["status"] in ("pending", "claimed"):
            continue
        result = task.get("result") or {}
        if task["status"] == "failed":
            logger.error(f"[派工] {task_id} 本機執行失敗：{result.get('error')}")
            return {"posts": [], "replied": [], "failed": [t.get("shortcode") for t in reply_tasks],
                    "error": result.get("error", "worker_failed")}
        posts = [ScrapedPost.from_dict(d) for d in result.get("posts", [])]
        logger.info(f"[派工] {task_id} 完成，{len(posts)} 篇貼文")
        return {
            "posts": posts,
            "replied": result.get("replied", []),
            "failed": result.get("failed", []),
            **({"error": result["error"]} if result.get("error") else {}),
        }

    logger.error(f"[派工] {task_id} 逾時（{TASK_TIMEOUT_S}s），本機可能中途斷線")
    return {"posts": [], "replied": [], "failed": [t.get("shortcode") for t in reply_tasks],
            "error": "worker_timeout"}


async def _run_locally(keyword, reply_tasks, skip_search, search_mode) -> dict:
    """退路：在 Railway 容器內直接跑 Playwright（舊行為）。"""
    from threads_scraper import search_and_reply_async as _local
    return await _local(keyword=keyword, reply_tasks=reply_tasks,
                        skip_search=skip_search, search_mode=search_mode)
