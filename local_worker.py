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
import socket
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
# worker 專用的 Chrome profile：cookie / localStorage 由瀏覽器自己維護，
# 登入一次就長期有效，不必再匯出貼上。注意是專用目錄，不是你平常在用的 profile
# （Chrome 開著會鎖住它，Playwright 開不起來）。
os.environ.setdefault("BROWSER_USER_DATA_DIR", str(_HERE / "worker_data" / "chrome-profile"))
# 用真實 Chrome 而不是 Chromium；找不到會在啟動時自動退回（見 _pick_channel）
os.environ.setdefault("BROWSER_CHANNEL", "chrome")
# 平常背景跑，不要每小時彈一個視窗打斷工作；--login 模式會強制有頭
os.environ.setdefault("SCRAPER_HEADLESS", "1")

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

# --login 模式最多等多久讓使用者完成登入
LOGIN_WAIT_S = int(os.environ.get("LOGIN_WAIT_SECONDS", "420"))

# Local TCP port used only as a single-instance lock. Binding to loopback does
# not expose anything to the network. A second worker cannot bind the same port
# and exits before it can claim duplicate patrol jobs.
_INSTANCE_LOCK_PORT = int(os.environ.get("WORKER_INSTANCE_LOCK_PORT", "47631"))
_instance_lock = None


def _acquire_single_instance() -> None:
    global _instance_lock
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", _INSTANCE_LOCK_PORT))
        lock.listen(1)
    except OSError:
        logger.error(
            "Another local worker is already running (instance lock port %s).",
            _INSTANCE_LOCK_PORT,
        )
        raise SystemExit(0)
    _instance_lock = lock


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


async def _pick_channel() -> None:
    """確認真實 Chrome 能不能用，不能就退回 Chromium。

    不做這個檢查的話，每一筆任務都會失敗，而錯誤訊息（"Chromium distribution
    'chrome' is not found"）指向的方向很難懂——會被當成 Playwright 壞了。
    """
    import threads_scraper as ts
    if not ts._BROWSER_CHANNEL:
        logger.info("瀏覽器：Chromium")
        return
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=True, channel=ts._BROWSER_CHANNEL, args=ts._LAUNCH_ARGS)
            await b.close()
        logger.info(f"瀏覽器：真實 {ts._BROWSER_CHANNEL}")
    except Exception as e:
        logger.warning(f"找不到 {ts._BROWSER_CHANNEL}（{type(e).__name__}），改用 Chromium")
        ts._BROWSER_CHANNEL = ""


async def login_mode() -> None:
    """一次性登入：開有頭瀏覽器讓使用者自己登入，登入狀態留在專用 profile 裡。

    這是為了取代「用 Cookie-Editor 匯出 JSON 再貼進環境變數」那套流程——
    那套每次 session 被作廢都要重來一次，而且貼錯格式的失敗方式很隱晦。
    """
    import threads_scraper as ts
    ts._HEADLESS = False  # 登入一定要看得到畫面
    await _pick_channel()

    from playwright.async_api import async_playwright
    state.init_db()
    print()
    print("=" * 64)
    print("  接下來會開啟一個瀏覽器視窗（worker 專用，不是你平常那個 Chrome）")
    print("  請在裡面登入 Threads 的 insurance_vision_ 帳號")
    print("  登入完成後不用做任何事——偵測到就會自動存檔並關閉視窗")
    print("=" * 64)
    print()
    async with async_playwright() as p:
        context, closable = await ts._open_context(p)
        try:
            page = await context.new_page()
            await page.goto("https://www.threads.com/", wait_until="domcontentloaded", timeout=60000)
            # 輪詢偵測而不是等 Enter：這支程式常常是被別人（例如 Claude Code）
            # 在背景啟動的，那種情況下 stdin 直接是 EOF，input() 會立刻返回而
            # 誤判成「使用者說登入好了」。看畫面比看鍵盤可靠。
            ok = False
            warned_account = ""
            deadline = time.time() + LOGIN_WAIT_S
            while time.time() < deadline:
                try:
                    if await ts._is_logged_in(page, context):
                        mismatch = cookie_store.account_mismatch(await context.cookies())
                        if not mismatch:
                            ok = True
                            break
                        # 錯帳號不要關視窗——關掉他就得從頭再來一次。留在原地
                        # 提示切換帳號，切對了下一輪自動偵測到。
                        got = cookie_store.current_user_id(await context.cookies())
                        if got != warned_account:
                            warned_account = got
                            print()
                            print(f"⚠ 目前登入的是帳號 {got}，不是 insurance_vision_（{cookie_store.expected_user_id()}）")
                            print("  請在這個瀏覽器視窗裡切換帳號（左下角頭像 → 切換帳號），")
                            print("  或登出後用 insurance_vision_ 的 Instagram 帳密登入。")
                            print("  切好之後不用回來按任何鍵，這裡會自動偵測。")
                            print()
                except Exception:
                    pass  # 使用者正在頁面間跳轉，下一輪再看
                remain = int(deadline - time.time())
                if remain % 30 == 0:
                    logger.info(f"等待登入中…（剩 {remain // 60} 分 {remain % 60} 秒）")
                await asyncio.sleep(3)
            if ok:
                n = await cookie_store.save_from(context)
                print()
                print(f"✅ 登入成功，{n} 個 cookie 已存進 profile 與存檔")
                print(f"   profile：{os.environ['BROWSER_USER_DATA_DIR']}")
                print("   接下來雙擊 start_worker.bat 就會開始領工")
            else:
                print()
                if warned_account:
                    print(f"❌ 逾時：整段時間都停在帳號 {warned_account}，沒有切到 {cookie_store.expected_user_id()}")
                else:
                    print("❌ 逾時：仍未偵測到登入（頁面上還有登入連結、找不到發文入口）")
                print("   再跑一次 login_threads.bat")
        finally:
            await closable.close()


async def main() -> None:
    if not BOT_URL or not TOKEN:
        logger.error("BOT_URL 或 BROWSER_WORKER_TOKEN 未設定（檢查 .env.worker）")
        sys.exit(1)

    _acquire_single_instance()
    state.init_db()
    logger.info(f"worker「{WORKER_NAME}」啟動 → {BOT_URL}")
    logger.info(f"state: {os.environ['STATE_DB_PATH']}")
    logger.info(f"cookie: {cookie_store.status()['text']}")
    logger.info(f"profile: {os.environ.get('BROWSER_USER_DATA_DIR') or '（無，用臨時 context）'}")
    mismatch = cookie_store.account_mismatch()
    if mismatch:
        logger.warning(f"⚠ {mismatch} —— 海巡會用錯帳號執行，請重跑 login_threads.bat")
    await _pick_channel()
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
        asyncio.run(login_mode() if "--login" in sys.argv else main())
    except KeyboardInterrupt:
        logger.info("已停止")
