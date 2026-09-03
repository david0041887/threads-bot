"""
threads_scraper.py
Playwright 瀏覽器自動化 — 搜尋 Threads 公開關鍵字貼文（海巡用）
Threads API 的 threads_keyword_search 需企業審核，此模組作為替代方案。
"""

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone

# Ensure Playwright finds Chromium in the app directory (Railway build artifact)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/.playwright-browsers")

from playwright.async_api import async_playwright

import cookie_store
from models import ScrapedPost, shortcode_to_id as _shortcode_to_id

logger = logging.getLogger(__name__)


# 對齊真實 Chrome 的瀏覽器環境。舊版寫 Chrome/124（落後二十幾個版本）且未設
# 語言與時區，等於自稱「兩年前的舊瀏覽器 + 英語系 UTC 使用者」——Threads 因此
# 給了不同待遇（filter=recent 被洗掉），台灣相關性也偏掉（港澳內容混入）。
# UA 版本取自 2026-07 使用者實機 Chrome 的 navigator.userAgent。
_REAL_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)
# 專用 Chrome profile 目錄。設了就用 launch_persistent_context：cookie、localStorage
# 與 IndexedDB 全部交給瀏覽器自己維護，rotate 天然正確，也不必再手動匯出貼上。
# 注意這必須是「worker 專用」的目錄，不能指向使用者平常在用的 Chrome profile
# —— Chrome 執行中會鎖住 profile，Playwright 開不起來。
_USER_DATA_DIR = os.environ.get("BROWSER_USER_DATA_DIR", "").strip()
# 設 "chrome" 可用真實 Chrome 而不是 Chromium（指紋更接近真人；Railway 容器內沒有，留空）
_BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "").strip()
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]


async def _open_context(p):
    """開一個瀏覽器 context，回傳 (context, closable)。

    抽成單一入口的理由：這個檔案有三個進入點，過去 cookie 載入與登入檢查都各自
    複製一份，改了一份另一份沒跟上。啟動方式現在有兩種，更不能再分頭寫。
    """
    if _USER_DATA_DIR:
        ctx = await p.chromium.launch_persistent_context(
            _USER_DATA_DIR,
            headless=_HEADLESS,
            args=_LAUNCH_ARGS,
            **({"channel": _BROWSER_CHANNEL} if _BROWSER_CHANNEL else {}),
            **_CONTEXT_OPTS,
        )
        # persistent context 自己就是「瀏覽器」，關它等於關瀏覽器
        return ctx, ctx
    browser = await p.chromium.launch(
        headless=_HEADLESS,
        args=_LAUNCH_ARGS,
        **({"channel": _BROWSER_CHANNEL} if _BROWSER_CHANNEL else {}),
    )
    return await browser.new_context(**_CONTEXT_OPTS), browser


# 本機 worker 可以用有頭瀏覽器（SCRAPER_HEADLESS=0）。headless Chromium 在指紋上
# 與真實瀏覽器有一票可偵測的差異，而本機反正有桌面，沒必要主動送出這個訊號。
_HEADLESS = os.environ.get("SCRAPER_HEADLESS", "1") != "0"

_CONTEXT_OPTS = {
    "user_agent": _REAL_CHROME_UA,
    "viewport": {"width": 1280, "height": 900},
    "locale": "zh-TW",
    "timezone_id": "Asia/Taipei",
}


# 從貼文連結往上遍歷 DOM，抓出貼文文字、帳號、media ID 與時間。
# 兩條搜尋路徑（search_threads_by_keyword_async / search_and_reply_async）共用同一份，
# 先前各自複製一份導致修了一邊、另一邊沒跟上。
_POST_EXTRACT_JS = r"""el => {
    let node = el, text = '', username = '', mediaId = '';
    for (let i = 0; i < 15; i++) {
        node = node.parentElement;
        if (!node) break;
        const t = (node.innerText || '').trim();
        const did = node.getAttribute('data-id') || node.getAttribute('data-post-id') || node.getAttribute('data-media-id');
        if (did && /^\d{10,}$/.test(did)) mediaId = did;
        if (!text && t.length > 50) text = t.slice(0, 500);
    }
    node = el;
    for (let i = 0; i < 15; i++) {
        node = node.parentElement;
        if (!node) break;
        const uLink = node.querySelector('a[href^="/@"]');
        if (uLink) {
            username = (uLink.getAttribute('href') || '').replace(/^\/@/, '').split('/')[0];
            break;
        }
    }
    // 抓 <time datetime> — 最可靠的時間來源。
    // 往上爬時一旦容器裡出現第二篇貼文就停止，否則會抓到隔壁貼文的時間。
    let datetime = '';
    node = el;
    for (let i = 0; i < 15; i++) {
        node = node.parentElement;
        if (!node) break;
        const codes = new Set();
        node.querySelectorAll('a[href*="/post/"]').forEach(a => {
            const mm = (a.getAttribute('href') || '').match(/\/post\/([A-Za-z0-9_-]+)/);
            if (mm) codes.add(mm[1]);
        });
        if (codes.size > 1) break;
        const timeEl = node.querySelector('time[datetime]');
        if (timeEl) { datetime = timeEl.getAttribute('datetime') || ''; break; }
    }
    // 找出只包含這一篇貼文的最大容器，再取其中的大圖；尺寸門檻會排除頭像與圖示。
    let contentNode = el.parentElement, bestNode = contentNode;
    for (let i = 0; i < 15 && contentNode; i++) {
        const codes = new Set();
        contentNode.querySelectorAll('a[href*="/post/"]').forEach(a => {
            const mm = (a.getAttribute('href') || '').match(/\/post\/([A-Za-z0-9_-]+)/);
            if (mm) codes.add(mm[1]);
        });
        if (codes.size > 1) break;
        bestNode = contentNode;
        contentNode = contentNode.parentElement;
    }
    const imageUrls = [];
    if (bestNode) {
        bestNode.querySelectorAll('img').forEach(img => {
            const src = img.currentSrc || img.src || '';
            const rect = img.getBoundingClientRect();
            const w = rect.width || 0;
            const h = rect.height || 0;
            if (/^https:\/\//.test(src) && w >= 200 && h >= 120 && !imageUrls.includes(src)) imageUrls.push(src);
        });
    }
    return {text, username, mediaId, datetime, imageUrls: imageUrls.slice(0, 4)};
}"""


async def _capture_post_images(page, image_urls: list[str] | None) -> list[dict]:
    """把貼文大圖由已登入瀏覽器截成 JPEG，避免 Claude 無法下載 Threads CDN URL。"""
    wanted = set((image_urls or [])[:4])
    if not wanted:
        return []
    captured: list[dict] = []
    try:
        for img in await page.query_selector_all("img"):
            if len(captured) >= 4:
                break
            try:
                src = await img.evaluate("el => el.currentSrc || el.src || ''")
                if src not in wanted:
                    continue
                box = await img.bounding_box()
                if not box or box["width"] < 150 or box["height"] < 100:
                    continue
                raw = await img.screenshot(type="jpeg", quality=72)
                captured.append({"media_type": "image/jpeg", "data": base64.b64encode(raw).decode("ascii")})
                wanted.remove(src)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[海巡] 擷取貼文圖片失敗：{e}")
    return captured

def _build_search_url(encoded_keyword: str, search_mode: str) -> str:
    """組搜尋網址。

    Threads 網頁版用 filter=recent 切到「最近」分頁；不帶參數則是「最相關」。
    舊版寫的 serp_type=top / serp_type=recent 是無效參數，Threads 直接忽略，
    兩種模式都落回「最相關」排序 —— 這是海巡一直推到幾十天前舊文的根因。

    結尾的斜線不可省略：/search?q=X&filter=recent 會被 301 到 /search/?q=X，
    filter 參數在導向過程中被丟掉；/search/?q=X&filter=recent 才不會被導向。
    瀏覽器上點分頁是 SPA 前端切換、不重新載入，所以看不出這個差異。
    （2026-07 在瀏覽器實地驗證過兩種寫法。）
    """
    url = f"https://www.threads.com/search/?q={encoded_keyword}"
    if search_mode != "top":
        url += "&filter=recent"
    return url


_TS_UNIT_HOURS = {"s": 1, "m": 1, "h": 1, "d": 24, "w": 168, "y": 8760}


def _ts_to_age_hours(n: int, unit: str) -> int:
    """把 Threads 的相對時間標籤（如 5h、3d）換算成小時。秒/分一律視為 1 小時。"""
    if unit in ("s", "m"):
        return 1
    return n * _TS_UNIT_HOURS[unit]


def _age_hours_from_taken_at(taken_at: int) -> int:
    """把 API 的 Unix 時間戳換算成貼文年齡（小時）。"""
    age = int((time.time() - taken_at) // 3600)
    return max(0, age)


def _age_hours_from_datetime(dt_str: str) -> int:
    """把 DOM <time datetime="..."> 的 ISO 時間換算成貼文年齡（小時）。

    這是最可靠的來源：時間直接掛在該貼文自己的 DOM 節點上。
    無法解析時回傳 9999。
    """
    if not dt_str:
        return 9999
    try:
        dt = datetime.fromisoformat(dt_str.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return max(0, int(age))
    except Exception:
        return 9999


# 貼文開頭要跳過的 metadata：時間、瀏覽數、UI 按鈕。
_TIME_LINE = re.compile(
    r'^(剛剛|just now|now|\d+\s*(?:[smhdwy]|秒|分鐘|分|小時|時|天|日|週|周|個?月|年)\s*前?)$', re.I)
_VIEW_LINE = re.compile(r'\d[\d,\.]*\s*(?:次瀏覽|瀏覽|views?)$', re.I)
_UI_LINE = re.compile(
    r'^(追蹤|已追蹤|追蹤中|Follow|Following|翻譯|查看翻譯|顯示翻譯|See translation|更多|…\s*更多|More|[·•])$', re.I)
_HANDLE_LINE = re.compile(r'^@?[a-zA-Z0-9_.]{2,30}$')  # 純 ASCII 才算 handle，不會誤傷中文短行
_CN_TIME = re.compile(r'(\d+)\s*(秒|分鐘|分|小時|時|天|日|週|周|個?月|年)')


def _cn_age_hours(n: int, unit: str) -> int:
    if unit in ("秒", "分鐘", "分"):
        return 1
    return {"小時": 1, "時": 1, "天": 24, "日": 24, "週": 168, "周": 168,
            "月": 720, "個月": 720, "年": 8760}.get(unit, 1) * n


def _is_header_meta(s: str, username: str, is_first_nonblank: bool) -> bool:
    """判斷這行是否為貼文開頭的 metadata（帳號 / 時間 / 瀏覽數 / UI 按鈕）。

    只比對已知樣式，不用「長度 < N」這種粗規則 —— 那會把「1.xxx」「自負額」這類
    合法的短正文行當成 metadata 吃掉，害整篇看起來像別人的留言片段。
    """
    if not s:
        return True
    base = s.lower().lstrip("@")
    if username:
        ul = username.lower()
        if base == ul:
            return True
        # "handle · 16分鐘" / "handle16分鐘" 同一行的情況
        if base.startswith(ul):
            rest = base[len(ul):].lstrip(" ·•|").strip()
            if rest == "" or _TIME_LINE.match(rest):
                return True
    if _TIME_LINE.match(s) or _VIEW_LINE.search(s) or _UI_LINE.match(s):
        return True
    # username 抓取失敗時的後備：僅對第一個非空行，且必須長得像純 ASCII handle
    if is_first_nonblank and not username and _HANDLE_LINE.match(s):
        return True
    return False


def _line_timestamp_age(s: str) -> int | None:
    """這行若是時間戳（純時間、或行尾「· 5h」「· 16分鐘」），回傳 age_hours，否則 None。"""
    m = re.match(r'^(\d+)\s*([smhdwy])$', s)
    if m:
        return _ts_to_age_hours(int(m.group(1)), m.group(2))
    m = re.search(r'[·•]\s*(\d+)\s*([smhdwy])\s*$', s)
    if m:
        return _ts_to_age_hours(int(m.group(1)), m.group(2))
    if re.match(r'^(剛剛|just now|now)$', s, re.I):
        return 1
    m = re.match(r'^(\d+)\s*(秒|分鐘|分|小時|時|天|日|週|周|個?月|年)\s*前?$', s)
    if m:
        return _cn_age_hours(int(m.group(1)), m.group(2))
    m = re.search(r'[·•]\s*(\d+)\s*(秒|分鐘|分|小時|時|天|日|週|周|個?月|年)\s*前?\s*$', s)
    if m:
        return _cn_age_hours(int(m.group(1)), m.group(2))
    return None


def _parse_post_text(raw_text: str, username: str = "") -> tuple[str, int]:
    """清理貼文文字，同時萃取時間戳轉為小時數。回傳 (clean_text, age_hours)。

    做法：貼文卡片的 innerText 開頭固定是 header（帳號、主題 tag、時間），時間戳
    永遠是 header 最後一個元素，其後才是正文。所以以「header 內第一個時間戳行」
    當分界，之後全部視為正文原樣保留（含條列 1./2./3. 這種短行）。
    沒有 header 時間戳時（少數卡片），退回只跳已知 metadata。
    注意：時間以 DOM <time datetime> 為主，這裡只是後備。
    """
    lines = [ln.strip() for ln in raw_text.splitlines()]
    age_hours = 9999
    boundary = None
    nonblank = 0
    for idx, s in enumerate(lines):
        if not s:
            continue
        nonblank += 1
        if nonblank > 6:  # header 很短，只在前幾個非空行裡找時間戳
            break
        a = _line_timestamp_age(s)
        if a is not None:
            age_hours = a
            boundary = idx
            break

    if boundary is not None:
        body = lines[boundary + 1:]
    else:
        # 後備：沒有 header 時間戳，只跳帳號/UI 等已知 metadata
        i = 0
        first_nb = True
        while i < len(lines):
            s = lines[i]
            fnb = first_nb and bool(s)
            if s:
                first_nb = False
            if _is_header_meta(s, username, fnb):
                i += 1
                continue
            break
        body = lines[i:]

    while body and not body[0]:
        body.pop(0)
    while body and not body[-1]:
        body.pop()
    text = "\n".join(body).strip()
    if age_hours == 9999:
        m = re.search(r'(?:^|\n)\s*(\d+)\s*([smhdwy])\s*(?:\n|$)', raw_text)
        if m:
            age_hours = _ts_to_age_hours(int(m.group(1)), m.group(2))
    return (text or raw_text.strip()), age_hours


_TS_PATTERN = re.compile(r'"taken_at"\s*:\s*(\d{10})')


def _extract_taken_at_map(json_text: str) -> dict[str, int]:
    """從 API JSON 回應中提取 shortcode → taken_at（Unix 秒）對應表。

    這是時間的可靠來源：不受版面改版影響。
    優先用正式 JSON 解析，確保 code 與 taken_at 來自「同一個物件」——
    用前後視窗的正則會抓到隔壁貼文的時間戳，導致舊文被誤判成新文。
    """
    taken_map: dict[str, int] = {}

    # 1. 正式 JSON 解析（可靠）：同一物件內同時有 code / shortcode 與 taken_at
    def _walk(node) -> None:
        if isinstance(node, dict):
            code = node.get("code") or node.get("shortcode")
            ts = node.get("taken_at")
            if isinstance(code, str) and isinstance(ts, int) and 10**9 < ts < 10**10:
                taken_map.setdefault(code, ts)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    try:
        _walk(json.loads(json_text))
    except Exception:
        pass
    if taken_map:
        return taken_map

    # 2. 後備：body 不是純 JSON（例如被 JS 包裹）時，就近找最接近的 taken_at
    try:
        for m in re.finditer(r'"(?:code|shortcode)"\s*:\s*"([A-Za-z0-9_-]{6,15})"', json_text):
            code = m.group(1)
            if code in taken_map:
                continue
            # 先往後找（JSON 慣例是 code 在前、taken_at 在後）
            fwd = _TS_PATTERN.search(json_text, m.end(), min(len(json_text), m.end() + 800))
            if fwd:
                taken_map[code] = int(fwd.group(1))
                continue
            # 再往前找「最靠近」的一個，而不是視窗內第一個
            back = list(_TS_PATTERN.finditer(json_text, max(0, m.start() - 800), m.start()))
            if back:
                taken_map[code] = int(back[-1].group(1))
    except Exception:
        pass
    return taken_map


async def _is_logged_in(page, context) -> bool:
    """判斷是否真的登入 Threads。只看伺服器渲染出來的 DOM。

    兩種看似直覺的判斷法都是假陽性，別再用：
    - **URL**：未登入時 threads.com/ 不會導向 /login，而是直接顯示公開首頁，
      所以 `"login" in current_url` 永遠是 False。
    - **cookie 有沒有 ds_user_id**：那是我們自己從 THREADS_COOKIES 塞進去的，
      當然永遠存在。它只證明 cookie 被載入，不證明伺服器認可這個 session。

    只有伺服器吐回來的 DOM 反映伺服器的認定（2026-07 實測對照真實瀏覽器與
    未登入 Playwright，兩個指標都有鑑別力）：
    - 已登入：無登入連結，且有撰寫入口／活動頁／設定頁等登入後導覽
    - 未登入：有登入連結，且沒有上述導覽
    """
    try:
        if await page.query_selector("a[href*='/login']"):
            return False
        # Threads 會不定期改動撰寫按鈕的中文（「新串文」曾改成「建立」），
        # 只押一個 aria-label 會讓已登入頁面被誤判成登出。活動與設定連結也是
        # 登入後才有的導覽，拿來做多訊號判斷，比文字文案穩定。
        authenticated_ui = await page.query_selector(
            ', '.join([
                'svg[aria-label="新串文"]',
                '[aria-label*="新串文"]',
                '[aria-label*="建立"]',
                '[aria-label*="Create"]',
                '[aria-label*="發佈"]',
                '[aria-label*="Post"]',
                'a[href*="/activity"]',
                'a[href*="/settings"]',
            ])
        )
        return authenticated_ui is not None
    except Exception as e:
        logger.warning(f"[海巡] 登入狀態判斷失敗，保守視為未登入: {e}")
        return False


async def _mark_replies(page, posts, max_age_hours: int = 200) -> int:
    """標記哪些貼文其實是「別人貼文底下的留言」（is_reply），海巡要跳過只留原創貼文。

    搜尋卡片的 DOM 分不出留言與原創（實測過），但貼文自己的頁面在登入 session 下
    帶有 "is_reply":true/false 欄位。用瀏覽器 session 內平行 fetch 各貼文頁取得（httpx
    帶 cookie 拿不到這欄位，只有瀏覽器 session 的 fetch 拿得到）。
    只查近期（max_age_hours 內）的貼文以省流量——舊文本來就會被時間篩選擋掉。
    無法判斷時保守視為非留言（不誤殺原創）。回傳被標記為留言的篇數。
    """
    items = [
        {"sc": p.shortcode, "u": p.username}
        for p in posts
        if p.shortcode and p.age_hours <= max_age_hours
    ]
    if not items:
        return 0
    try:
        result = await page.evaluate(
            """async (items) => {
                const out = {};
                let idx = 0;
                async function worker(){
                    while (idx < items.length){
                        const it = items[idx++];
                        const url = it.u
                            ? `https://www.threads.com/@${it.u}/post/${it.sc}`
                            : `https://www.threads.com/t/${it.sc}`;
                        try {
                            const r = await fetch(url, {credentials:'include'});
                            const t = await r.text();
                            const m = t.match(/"is_reply"\\s*:\\s*(true|false)/);
                            out[it.sc] = m ? (m[1] === 'true') : null;
                        } catch(e){ out[it.sc] = null; }
                    }
                }
                const CONC = Math.min(6, items.length);
                await Promise.all(Array.from({length: CONC}, worker));
                return out;
            }""",
            items,
        )
        n = 0
        for p in posts:
            if result.get(p.shortcode) is True:
                p.is_reply = True
                n += 1
        return n
    except Exception as e:
        logger.warning(f"[海巡] is_reply 標記失敗（不擋，全視為非留言）: {e}")
        return 0


async def _ensure_logged_in(page, context) -> bool:
    await cookie_store.apply(context)

    # 前往首頁確認登入狀態
    await page.goto("https://www.threads.com/", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    current_url = page.url
    logger.info(f"[海巡] threads.com 目前 URL: {current_url}")

    if await _is_logged_in(page, context):
        logger.info("[海巡] 已登入")
        return True

    logger.warning("[海巡] 未登入：THREADS_COOKIES 已失效或未設定，嘗試帳密登入")
    username = os.environ.get("THREADS_USERNAME", "")
    password = os.environ.get("THREADS_PASSWORD", "")
    if not username or not password:
        logger.error("[海巡] THREADS_USERNAME / THREADS_PASSWORD 未設定")
        return False
    try:
        await page.goto("https://www.threads.com/login", wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_selector('input[autocomplete="username"]', timeout=15000)
        await page.fill('input[autocomplete="username"]', username)
        await page.fill('input[type="password"]', password)
        await page.press('input[type="password"]', "Enter")
        await asyncio.sleep(5)
        logger.info(f"[海巡] 登入後 URL: {page.url}")
        if not await _is_logged_in(page, context):
            logger.error("[海巡] 帳密登入失敗（Instagram 多半會擋密碼登入），請更新 THREADS_COOKIES")
            return False
        await cookie_store.save_from(context)
        logger.info("[海巡] 帳密登入成功")
        return True
    except Exception as e:
        logger.error(f"[海巡] 帳密登入失敗: {e}")
        return False


def _extract_pk_map(json_text: str) -> dict[str, str]:
    """從 API JSON 回應中提取 shortcode → media_id 對應表。
    支援 Threads/Instagram API 各種欄位命名：
    - code / shortcode → pk / id
    - URL 中含有 /post/shortcode 旁邊的 id 欄位
    """
    pk_map: dict[str, str] = {}
    try:
        window = 400  # 搜尋視窗大小
        id_pattern = re.compile(r'"(?:pk|id)"\s*:\s*"?(\d{12,20})"?')
        # 1. "code":"SHORTCODE" 或 "shortcode":"SHORTCODE"
        for m in re.finditer(r'"(?:code|shortcode)"\s*:\s*"([A-Za-z0-9_-]{6,15})"', json_text):
            code = m.group(1)
            start = max(0, m.start() - window)
            end = min(len(json_text), m.end() + window)
            snippet = json_text[start:end]
            pk_match = id_pattern.search(snippet)
            if pk_match:
                pk_map[code] = pk_match.group(1)
        # 2. URL 內含 /post/SHORTCODE 旁邊的 id
        for m in re.finditer(r'/post/([A-Za-z0-9_-]{6,15})', json_text):
            code = m.group(1)
            if code in pk_map:
                continue
            start = max(0, m.start() - window)
            end = min(len(json_text), m.end() + window)
            snippet = json_text[start:end]
            pk_match = id_pattern.search(snippet)
            if pk_match:
                pk_map[code] = pk_match.group(1)
    except Exception:
        pass
    return pk_map


def _extract_like_map(json_text: str) -> dict[str, int]:
    """從 API JSON 回應中提取 shortcode → like_count 對應表。"""
    like_map: dict[str, int] = {}
    like_pattern = re.compile(r'"like_count"\s*:\s*(\d+)')
    window = 800

    for m in re.finditer(r'"(?:code|shortcode)"\s*:\s*"([A-Za-z0-9_-]{6,15})"', json_text):
        code = m.group(1)
        start = max(0, m.start() - window)
        end = min(len(json_text), m.end() + window)
        snippet = json_text[start:end]
        lm = like_pattern.search(snippet)
        if lm:
            lc = int(lm.group(1))
            like_map[code] = max(like_map.get(code, 0), lc)

    for m in re.finditer(r'/post/([A-Za-z0-9_-]{6,15})', json_text):
        code = m.group(1)
        if code in like_map:
            continue
        start = max(0, m.start() - window)
        end = min(len(json_text), m.end() + window)
        snippet = json_text[start:end]
        lm = like_pattern.search(snippet)
        if lm:
            like_map[code] = int(lm.group(1))

    return like_map


async def search_threads_by_keyword_async(keyword: str, limit: int = 20, search_mode: str = "recent") -> list[ScrapedPost]:
    """search_mode: "recent"（最新）或 "top"（熱門/爆文）"""
    async with async_playwright() as p:
        context, _closable = await _open_context(p)

        await cookie_store.apply(context)

        page = await context.new_page()
        posts: list[ScrapedPost] = []
        # shortcode → 真實 media_id（從 API 回應攔截）
        api_pk_map: dict[str, str] = {}
        api_like_map: dict[str, int] = {}
        api_taken_at_map: dict[str, int] = {}

        async def _capture_pk(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct and "javascript" not in ct:
                    return
                body = await response.text()
                has_id = '"pk"' in body or ('"id"' in body and len(body) > 500)
                has_code = '"code"' in body or '"shortcode"' in body or '/post/' in body
                if has_id and has_code:
                    found = _extract_pk_map(body)
                    if found:
                        logger.debug(f"[海巡] 從 {response.url[:60]} 捕捉到 {len(found)} 個 pk")
                        api_pk_map.update(found)
                    likes = _extract_like_map(body)
                    if likes:
                        api_like_map.update(likes)
                    taken = _extract_taken_at_map(body)
                    if taken:
                        api_taken_at_map.update(taken)
            except Exception:
                pass

        page.on("response", _capture_pk)

        try:
            if not await _ensure_logged_in(page, context):
                return []

            encoded = urllib.parse.quote(keyword)
            serp = "top" if search_mode == "top" else "recent"
            search_url = _build_search_url(encoded, search_mode)
            logger.info(f"[海巡] 前往搜尋頁面 ({serp}): {search_url}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            logger.info(f"[海巡] 目前 URL: {page.url}")
            await asyncio.sleep(3)

            # 等待貼文連結出現（Threads 可能已不用 article 標籤）
            try:
                await page.wait_for_selector('a[href*="/post/"]', timeout=15000)
            except Exception:
                title = await page.title()
                logger.warning(f"[海巡] 搜尋「{keyword}」無結果，title={title}, URL={page.url}")
                return []

            # 滾動載入更多
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(1.5)

            logger.info(
                f"[海巡] API 攔截到 {len(api_pk_map)} 個 pk 對應、"
                f"{len(api_taken_at_map)} 個 taken_at 時間戳"
            )
            my_username = os.environ.get("THREADS_USERNAME", "").lower()

            # 取得所有貼文連結，從連結往上找貼文容器
            post_links = await page.query_selector_all('a[href*="/post/"]')
            logger.info(f"[海巡] 找到 {len(post_links)} 個貼文連結")

            seen_shortcodes: set[str] = set()
            for link_el in post_links[:limit * 3]:
                try:
                    href = await link_el.get_attribute("href") or ""
                    match = re.search(r"/post/([A-Za-z0-9_-]+)", href)
                    if not match:
                        continue
                    shortcode = match.group(1)
                    if shortcode in seen_shortcodes:
                        continue
                    seen_shortcodes.add(shortcode)

                    # 用 JavaScript 往上遍歷找貼文文字、帳號、media ID、時間
                    result = await link_el.evaluate(_POST_EXTRACT_JS)

                    raw_text = (result.get("text") or "").strip()
                    username = (result.get("username") or "").strip()
                    media_id = (
                        api_pk_map.get(shortcode)
                        or (result.get("mediaId") or "").strip()
                        or _shortcode_to_id(shortcode)
                    )
                    logger.debug(f"[海巡] shortcode={shortcode} media_id={media_id} (api_map={bool(api_pk_map.get(shortcode))})")

                    # 時間來源優先序：DOM <time> > API taken_at > 文字解析
                    text, age_hours = _parse_post_text(raw_text, username)
                    source = "文字" if age_hours != 9999 else "無"
                    taken_at = api_taken_at_map.get(shortcode)
                    if taken_at:
                        age_hours = _age_hours_from_taken_at(taken_at)
                        source = "API"
                    dom_age = _age_hours_from_datetime(result.get("datetime") or "")
                    if dom_age != 9999:
                        age_hours = dom_age
                        source = "DOM"

                    image_data = await _capture_post_images(page, result.get("imageUrls") or [])
                    if (not text or len(text) < 20) and not image_data:
                        continue
                    if username.lower() == my_username:
                        continue
                    posts.append(ScrapedPost(
                        shortcode=shortcode, text=text, username=username,
                        media_id=media_id, age_hours=age_hours,
                        like_count=api_like_map.get(shortcode, 0), age_source=source,
                        image_urls=result.get("imageUrls") or [],
                        image_data=image_data,
                    ))
                    if len(posts) >= limit:
                        break

                except Exception as e:
                    logger.debug(f"解析貼文失敗: {e}")

            logger.info(f"海巡「{keyword}」完成，共 {len(posts)} 篇")
            return posts

        except Exception as e:
            logger.error(f"海巡搜尋失敗: {e}")
            return []
        finally:
            # 關閉前務必回寫：Meta 這次 session 換發的新 sessionid 只存在這個
            # context 裡，沒存回去就等於下次繼續重放已被作廢的舊值。
            await cookie_store.save_from(context)
            await _closable.close()


async def get_profile_posts_async(username: str, limit: int = 50) -> list[ScrapedPost]:
    """擷取指定帳號的公開貼文列表（需要登入 cookie）"""
    async with async_playwright() as p:
        context, _closable = await _open_context(p)
        page = await context.new_page()

        try:
            if not await _ensure_logged_in(page, context):
                logger.error("[Profile] 未登入，無法擷取")
                return []

            profile_url = f"https://www.threads.com/@{username}"
            logger.info(f"[Profile] 前往 {profile_url}")
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # 向下滾動載入更多貼文（每次滾動後等待 1.5s）
            scroll_rounds = max(5, limit // 5)
            for i in range(scroll_rounds):
                await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                await asyncio.sleep(1.5)

            my_username = os.environ.get("THREADS_USERNAME", "").lower()
            post_links = await page.query_selector_all('a[href*="/post/"]')
            logger.info(f"[Profile] 找到 {len(post_links)} 個貼文連結")

            posts: list[ScrapedPost] = []
            seen: set[str] = set()

            for link_el in post_links[:limit * 3]:
                try:
                    href = await link_el.get_attribute("href") or ""
                    m = re.search(r"/post/([A-Za-z0-9_-]+)", href)
                    if not m:
                        continue
                    sc = m.group(1)
                    if sc in seen:
                        continue
                    seen.add(sc)

                    result = await link_el.evaluate("""el => {
                        let node = el, text = '', uname = '';
                        for (let i = 0; i < 15; i++) {
                            node = node.parentElement; if (!node) break;
                            const t = (node.innerText||'').trim();
                            if (!text && t.length > 30) text = t.slice(0,800);
                        }
                        node = el;
                        for (let i = 0; i < 15; i++) {
                            node = node.parentElement; if (!node) break;
                            const u = node.querySelector('a[href^="/@"]');
                            if (u) { uname=(u.getAttribute('href')||'').replace(/^\\/@/,'').split('/')[0]; break; }
                        }
                        return {text, username: uname};
                    }""")

                    raw_text = (result.get("text") or "").strip()
                    uname = (result.get("username") or "").strip()
                    if not raw_text or len(raw_text) < 20:
                        continue
                    text, age_hours = _parse_post_text(raw_text, uname)
                    posts.append(ScrapedPost(shortcode=sc, text=text, username=uname, age_hours=age_hours))
                    if len(posts) >= limit:
                        break
                except Exception:
                    pass

            logger.info(f"[Profile] @{username} 共擷取 {len(posts)} 篇")
            return posts

        except Exception as e:
            logger.error(f"[Profile] 擷取失敗: {e}")
            return []
        finally:
            # 關閉前務必回寫：Meta 這次 session 換發的新 sessionid 只存在這個
            # context 裡，沒存回去就等於下次繼續重放已被作廢的舊值。
            await cookie_store.save_from(context)
            await _closable.close()


async def _dump_page_elements(page) -> str:
    """擷取頁面上的互動元素，用於診斷 UI 選擇器。"""
    try:
        elements = await page.evaluate("""() => {
            const els = document.querySelectorAll('[role="button"], button, [contenteditable], [aria-label], [data-testid]');
            return Array.from(els).slice(0, 30).map(el => {
                return [
                    el.tagName,
                    el.getAttribute('role') || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('data-testid') || '',
                    el.getAttribute('contenteditable') || '',
                    (el.innerText || '').replace(/\\n/g,' ').substring(0,40)
                ].filter(Boolean).join('|');
            });
        }""")
        return "\n".join(elements[:20])
    except Exception:
        return "(無法擷取)"


async def _ui_reply_to_post(page, post_url: str, reply_text: str) -> bool:
    """
    使用 Playwright 瀏覽器 UI 在 Threads 串文底下回覆。
    回傳 True 表示成功送出。
    """
    try:
        logger.info(f"[海巡-UI] 前往貼文: {post_url}")
        await page.goto(post_url, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(3)

        try:
            await page.wait_for_selector('a[href*="/post/"]', timeout=10000)
        except Exception:
            logger.warning(f"[海巡-UI] 頁面未正確載入: {post_url}")
            return False

        # ── 策略一：直接找回覆輸入框（部分 UI 不需點按鈕）──────────
        input_box = None
        for sel in [
            '[placeholder*="reply" i]',
            '[placeholder*="回覆"]',
            '[aria-label*="reply" i]',
            '[aria-label*="回覆"]',
            '[role="textbox"]',
        ]:
            input_box = await page.query_selector(sel)
            if input_box:
                logger.info(f"[海巡-UI] 直接找到輸入框: {sel}")
                break

        # ── 策略二：找並點擊回覆按鈕後等待輸入框出現 ────────────────
        if not input_box:
            reply_btn = None
            for sel in [
                'svg[aria-label="Reply"]',
                'svg[aria-label="回覆"]',
                '[aria-label="Reply"]',
                '[aria-label="回覆"]',
                '[data-testid="reply-button"]',
                '[role="button"][aria-label*="eply"]',
                '[role="button"][aria-label*="回覆"]',
                'div[role="button"] svg[aria-label*="eply"]',
                'article div[role="button"]:first-of-type',
            ]:
                reply_btn = await page.query_selector(sel)
                if reply_btn:
                    logger.info(f"[海巡-UI] 找到回覆按鈕: {sel}")
                    break

            if reply_btn:
                await reply_btn.click()
                await asyncio.sleep(2)
                for sel in [
                    '[role="textbox"]',
                    '[contenteditable="true"]',
                    'textarea',
                ]:
                    input_box = await page.query_selector(sel)
                    if input_box:
                        break

        if not input_box:
            # 記錄頁面元素供診斷
            dump = await _dump_page_elements(page)
            logger.warning(f"[海巡-UI] 找不到輸入框\n頁面元素:\n{dump}")
            # 把元素列表回傳到 TG（只在有 TELEGRAM_BOT_TOKEN 時）
            try:
                from notifier import send_telegram
                send_telegram(f"[海巡-UI] 找不到輸入框，頁面元素：\n{dump[:1000]}")
            except Exception:
                pass
            return False

        await input_box.click()
        await asyncio.sleep(0.5)
        await input_box.type(reply_text, delay=30)
        await asyncio.sleep(0.5)

        # ── 找送出按鈕 ────────────────────────────────────────────
        post_btn = None
        for sel in [
            '[aria-label="Post"]',
            '[aria-label="送出"]',
            'div[role="button"][aria-label*="Post"]',
            'div[role="button"][aria-label*="送出"]',
            'button[type="submit"]',
        ]:
            post_btn = await page.query_selector(sel)
            if post_btn:
                logger.info(f"[海巡-UI] 找到送出按鈕: {sel}")
                break

        if not post_btn:
            logger.info("[海巡-UI] 找不到送出鍵，改用 Ctrl+Enter")
            await input_box.press("Control+Enter")
        else:
            await post_btn.click()

        await asyncio.sleep(3)
        logger.info(f"[海巡-UI] 回覆送出成功: {post_url}")
        return True

    except Exception as e:
        logger.error(f"[海巡-UI] 回覆失敗: {e}")
        return False


async def search_and_reply_async(
    keyword: str,
    reply_tasks: list[dict],
    skip_search: bool = False,
    search_mode: str = "recent",  # "recent" 或 "top"
) -> dict:
    """
    在同一個登入瀏覽器 session 中：先搜尋關鍵字貼文，再對指定貼文用 UI 回覆。
    reply_tasks: 要回覆的貼文清單，每筆含 shortcode, username, reply_text
    skip_search: Phase 2 傳 True，跳過搜尋直接回覆，省一次 Threads 請求
    回傳 {"posts": [...], "replied": [shortcode, ...], "failed": [shortcode, ...]}
    """
    async with async_playwright() as p:
        context, _closable = await _open_context(p)

        await cookie_store.apply(context)

        page = await context.new_page()
        replied = []
        failed = []
        posts = []

        try:
            # 確認登入（不能用 URL 或 cookie 判斷，理由見 _is_logged_in）
            await page.goto("https://www.threads.com/", wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(2)
            if not await _is_logged_in(page, context):
                logger.error("[海巡] 未登入：THREADS_COOKIES 已失效，海巡無法運作")
                return {
                    "posts": [],
                    "replied": [],
                    "failed": [t["shortcode"] for t in reply_tasks],
                    "error": "not_logged_in",
                }

            # 搜尋（Phase 2 傳 skip_search=True 時跳過）
            if not skip_search:
                api_pk_map: dict[str, str] = {}
                api_like_map: dict[str, int] = {}
                api_taken_at_map: dict[str, int] = {}

                async def _capture_pk(response):
                    try:
                        ct = response.headers.get("content-type", "")
                        if "json" not in ct and "javascript" not in ct:
                            return
                        body = await response.text()
                        has_id = '"pk"' in body or ('"id"' in body and len(body) > 500)
                        has_code = '"code"' in body or '"shortcode"' in body or '/post/' in body
                        if has_id and has_code:
                            found = _extract_pk_map(body)
                            if found:
                                api_pk_map.update(found)
                            likes = _extract_like_map(body)
                            if likes:
                                api_like_map.update(likes)
                            taken = _extract_taken_at_map(body)
                            if taken:
                                api_taken_at_map.update(taken)
                    except Exception:
                        pass

                page.on("response", _capture_pk)

                encoded = urllib.parse.quote(keyword)
                serp = "top" if search_mode == "top" else "recent"
                search_url = _build_search_url(encoded, search_mode)
                logger.info(f"[海巡] 搜尋模式={serp}: {search_url}")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(3)

                try:
                    await page.wait_for_selector('a[href*="/post/"]', timeout=12000)
                    for _ in range(3):
                        await page.evaluate("window.scrollBy(0, window.innerHeight)")
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

                logger.info(f"[海巡] API 攔截到 {len(api_pk_map)} 個 pk")

                # 搜集貼文
                my_username = os.environ.get("THREADS_USERNAME", "").lower()
                post_links = await page.query_selector_all('a[href*="/post/"]')
                seen: set[str] = set()
                for link_el in post_links[:60]:
                    try:
                        href = await link_el.get_attribute("href") or ""
                        m = re.search(r"/post/([A-Za-z0-9_-]+)", href)
                        if not m:
                            continue
                        sc = m.group(1)
                        if sc in seen:
                            continue
                        seen.add(sc)
                        result = await link_el.evaluate(_POST_EXTRACT_JS)
                        raw_text = (result.get("text") or "").strip()
                        username = (result.get("username") or "").strip()
                        media_id = api_pk_map.get(sc) or (result.get("mediaId") or "").strip() or _shortcode_to_id(sc)
                        # 時間來源優先序：DOM <time> > API taken_at > 文字解析
                        text, age_hours = _parse_post_text(raw_text, username)
                        age_source = "文字" if age_hours != 9999 else "無"
                        taken_at = api_taken_at_map.get(sc)
                        if taken_at:
                            age_hours = _age_hours_from_taken_at(taken_at)
                            age_source = "API"
                        dom_age = _age_hours_from_datetime(result.get("datetime") or "")
                        if dom_age != 9999:
                            age_hours = dom_age
                            age_source = "DOM"
                        image_data = await _capture_post_images(page, result.get("imageUrls") or [])
                        if ((not text or len(text) < 20) and not image_data) or username.lower() == my_username:
                            continue
                        posts.append(ScrapedPost(
                            shortcode=sc, text=text, username=username,
                            media_id=media_id, age_hours=age_hours,
                            like_count=api_like_map.get(sc, 0), age_source=age_source,
                            image_urls=result.get("imageUrls") or [],
                            image_data=image_data,
                        ))
                    except Exception:
                        pass

                # 標記留言（別人貼文底下的回覆），海巡只留原創貼文
                reply_n = await _mark_replies(page, posts)

                known = [p.age_hours for p in posts if p.age_hours != 9999]
                logger.info(
                    f"[海巡] 找到 {len(posts)} 篇貼文"
                    f"（時間可判斷:{len(known)}/{len(posts)}"
                    + (f"，最新:{min(known)}h 最舊:{max(known)}h" if known else "")
                    + f"，7日內:{sum(1 for a in known if a <= 168)} 篇"
                    + f"，其中留言:{reply_n} 篇）"
                )

            # 對指定貼文做 UI 回覆
            for task in reply_tasks:
                sc = task["shortcode"]
                username = task["username"]
                reply_text = task["reply_text"]
                post_url = f"https://www.threads.com/@{username}/post/{sc}"
                ok = await _ui_reply_to_post(page, post_url, reply_text)
                if ok:
                    replied.append(sc)
                else:
                    failed.append(sc)
                await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"[海巡] search_and_reply 失敗: {e}")
        finally:
            # 關閉前務必回寫：Meta 這次 session 換發的新 sessionid 只存在這個
            # context 裡，沒存回去就等於下次繼續重放已被作廢的舊值。
            await cookie_store.save_from(context)
            await _closable.close()

        return {"posts": posts, "replied": replied, "failed": failed}
