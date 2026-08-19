"""
local_worker.py — 在家裡筆電上執行 Threads 瀏覽器動作的常駐 worker。

## 這支程式解決什麼

Threads 的 session cookie 誕生在這台筆電的 Chrome、台灣住宅 IP。Railway 在美國的
資料中心 IP 上每小時重放同一份 cookie，Meta 會把它當成 session 遭竊而全域作廢
（實測：排程關閉時同一份 cookie 撐約四週，開成 10 分鐘一次只撐 13 小時）。

所以：Railway 仍然負責排程、去重、AI 判斷、Telegram 推播與發文（官方 API），
只有「開瀏覽器」這件事回到這台筆電執行 —— IP 與 cookie 同源，頻率就不再是限制。

## 怎麼跑

1. 複製 .env.worker.example 成 .env.worker，填 BOT_URL / BROWSER_WORKER_TOKEN /
   THREADS_COOKIES（第一次的種子，之後由 cookie_store 自動維護 rotate 後的版本）
2. 雙擊 start_worker.bat（或 python local_worker.py）
3. Railway 那邊把 BROWSER_WORKER_MODE 設成 remote

筆電關機時：任務留在 Railway 佇列，6 小時後自動清掉。海巡會少跑幾次，
但 cookie 不會因此被作廢 —— 這個取捨是刻意的。
"""

import os
import sys
import time
import asyncio
import logging
import platform
from pathlib import Path

# ── 先載入 .env.worker，再 import 任何會讀環境變數的模組 ──────────
def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # 值可能是整包 JSON（THREADS_COOKIES），只剝掉外層引號，內容原封不動
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


_HERE = Path(__file__).resolve().parent
_load_env_file(_HERE / ".env.worker")

# 本機的 state.db 預設放在專案旁邊；不設會沿用 Railway 的 /app/data 路徑而失敗
os.environ.setdefault("STATE_DB_PATH", str(_HERE / "worker_data" / "state.db"))
# 本機有桌面，預設用有頭瀏覽器（指紋更像真人；要背景跑就設 SCRAPER_HEADLESS=1）
os.environ.setdefault("SCRAPER_HEADLESS", "0")

import httpx  # noqa: E402
import state  # noqa: E402
import cookie_store  # noqa: E402
from threads_scraper import search_and_reply_async  # noqa: E402

# Windows 主控台預設 cp950，log 裡的中文標點與符號會讓整個 worker 死在 print 上
# （UnicodeEncodeError）。errors="replace" 保證顯示不了的字只是變成問號，不會中斷任務。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%m-%d %H:%M:%S",
)
logger = logging.getLogger("worker")

BOT_URL = (os.environ.get("BOT_URL", "") or "").rstrip("/")
TOKEN = os.environ.get("BROWSER_WORKER_TOKEN", "")
WORKER_NAME = os.environ.get("WORKER_NAME", "") or f"laptop-{platform.node()}"
POLL_INTERVAL_S = int(os.environ.get("WORKER_POLL_SECONDS", "30"))

# 連不上 Railway 時的退避上限。網路斷了就每 5 分鐘試一次，不要洗版 log
_BACKOFF_MAX_S = 300


async def _claim(client: httpx.AsyncClient) -> dict | None:
    r = await client.post(
        f"{BOT_URL}/worker/claim",
        headers={"X-Worker-Token": TOKEN},
        json={"worker": WORKER_NAME},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("task")


async def _report(client: httpx.AsyncClient, task_id: str, ok: bool, result: dict) -> None:
    r = await client.post(
        f"{BOT_URL}/worker/result",
        headers={"X-Worker-Token": TOKEN},
        json={"worker": WORKER_NAME, "task_id": task_id, "ok": ok, "result": result},
        timeout=60,
    )
    r.raise_for_status()


async def _run_task(payload: dict) -> dict:
    """執行一筆任務。回傳可 JSON 序列化的結果（ScrapedPost 轉成 dict）。"""
    keyword = payload.get("keyword", "")
    reply_tasks = payload.get("reply_tasks") or []
    logger.info(f"▶ 執行：關鍵字「{keyword}」、模式 {payload.get('search_mode')}、{len(reply_tasks)} 則回覆")
    out = await search_and_reply_async(
        keyword=keyword,
        reply_tasks=reply_tasks,
        skip_search=bool(payload.get("skip_search")),
        search_mode=payload.get("search_mode", "recent"),
    )
    posts = [p.to_dict() for p in out.get("posts", [])]
    logger.info(f"◀ 完成：{len(posts)} 篇貼文、回覆成功 {len(out.get('replied', []))}")
    return {
        "posts": posts,
        "replied": out.get("replied", []),
        "failed": out.get("failed", []),
        **({"error": out["error"]} if out.get("error") else {}),
    }


async def main() -> None:
    if not BOT_URL or not TOKEN:
        logger.error("BOT_URL 或 BROWSER_WORKER_TOKEN 未設定（檢查 .env.worker）")
        sys.exit(1)

    state.init_db()
    logger.info(f"worker「{WORKER_NAME}」啟動 → {BOT_URL}")
    logger.info(f"state: {os.environ['STATE_DB_PATH']}")
    logger.info(f"cookie: {cookie_store.status()['text']}")
    logger.info(f"瀏覽器：{'有頭' if os.environ.get('SCRAPER_HEADLESS') == '0' else 'headless'}｜每 {POLL_INTERVAL_S} 秒領工一次")

    backoff = 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                task = await _claim(client)
                backoff = 0
            except Exception as e:
                backoff = min(_BACKOFF_MAX_S, max(POLL_INTERVAL_S, backoff * 2 or POLL_INTERVAL_S))
                logger.warning(f"領工失敗（{type(e).__name__}: {e}），{backoff}s 後重試")
                await asyncio.sleep(backoff)
                continue

            if not task:
                await asyncio.sleep(POLL_INTERVAL_S)
                continue

            task_id = task["task_id"]
            try:
                result = await _run_task(task["payload"])
                await _report(client, task_id, True, result)
            except Exception as e:
                logger.error(f"任務 {task_id} 失敗: {type(e).__name__}: {e}", exc_info=True)
                try:
                    # 交件本身也可能失敗（例如剛好斷網）。交不出去就讓它逾時，
                    # Railway 端有 timeout 兜底，不會卡住排程。
                    await _report(client, task_id, False, {"error": f"{type(e).__name__}: {e}"})
                except Exception:
                    logger.error("連失敗回報都送不出去，交給 Railway 端逾時處理")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("已停止")
