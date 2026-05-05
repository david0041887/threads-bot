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
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

# Ensure Playwright finds Chromium in the app directory (Railway build artifact)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/.playwright-browsers")

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

COOKIES_FILE = "threads_cookies.json"
# Threads/Instagram shortcode 字母表（與 Instagram 相同）
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _shortcode_to_id(shortcode: str) -> str:
    n = 0
    for ch in shortcode:
        if ch in _ALPHABET:
            n = n * 64 + _ALPHABET.index(ch)
    return str(n)


@dataclass
class ScrapedPost:
    shortcode: str
    text: str
    username: str
    media_id: str = ""
    age_hours: int = 9999  # 貼文年齡（小時），9999=無法判斷

    @property
    def id(self) -> str:
        return self.media_id or _shortcode_to_id(self.shortcode)


def _parse_post_text(raw_text: str) -> tuple[str, int]:
    """清理貼文文字，同時萃取時間戳轉為小時數。回傳 (clean_text, age_hours)。"""
    lines = raw_text.splitlines()
    clean_lines = []
    age_hours = 9999
    skipping = True
    for line in lines:
        s = line.strip()
        if skipping:
            if not s:
                continue
            ts = re.match(r'^(\d+)([mhdw])$', s)
            if ts:
                n, unit = int(ts.group(1)), ts.group(2)
                if unit == 'm':
                    age_hours = 1  # 幾分鐘內的貼文視為 1 小時
                elif unit == 'h':
                    age_hours = n
                elif unit == 'd':
                    age_hours = n * 24
                else:
                    age_hours = n * 168
                continue
            if len(s) < 15 or re.match(r'^@?\w{1,20}$', s):
                continue
            skipping = False
        clean_lines.append(s)
    return ("\n".join(clean_lines).strip() or raw_text), age_hours


async def _ensure_logged_in(page, context) -> bool:
    # 優先使用 THREADS_COOKIES 環境變數（從真實瀏覽器匯出）
    cookies_env = os.environ.get("THREADS_COOKIES", "")
    if cookies_env:
        try:
            raw = json.loads(cookies_env)
            pw_cookies = []
            for c in raw:
                pw = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", ".threads.com"),
                    "path": c.get("path", "/"),
                }
                # Cookie-Editor 用 expirationDate，Playwright 用 expires
                exp = c.get("expirationDate") or c.get("expires")
                if exp and exp > 0:
                    pw["expires"] = int(exp)
                if "httpOnly" in c:
                    pw["httpOnly"] = bool(c["httpOnly"])
                if "secure" in c:
                    pw["secure"] = bool(c["secure"])
                # 修正 sameSite 值（可能為 None）
                ss = c.get("sameSite") or "Lax"
                pw["sameSite"] = {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}.get(ss.lower(), "Lax")
                pw_cookies.append(pw)
            await context.add_cookies(pw_cookies)
            logger.info(f"[海巡] 已從環境變數載入 {len(pw_cookies)} 個 cookies")
        except Exception as e:
            logger.warning(f"[海巡] THREADS_COOKIES 解析失敗: {e}")

    # 前往首頁確認登入狀態
    await page.goto("https://www.threads.com/", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    current_url = page.url
    logger.info(f"[海巡] threads.com 目前 URL: {current_url}")

    if "login" in current_url.lower() or "challenge" in current_url.lower():
        logger.warning(f"[海巡] 未登入（URL: {current_url}），嘗試帳密登入")
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
            final_url = page.url
            logger.info(f"[海巡] 登入後 URL: {final_url}")
            if "challenge" in final_url or "login" in final_url:
                logger.error("[海巡] 登入被 Instagram 安全驗證擋住，需手動提供 THREADS_COOKIES")
                return False
            Path(COOKIES_FILE).write_text(json.dumps(await context.cookies()))
            logger.info("[海巡] 登入成功")
            return True
        except Exception as e:
            logger.error(f"[海巡] 登入失敗: {e}")
            return False

    logger.info("[海巡] 已登入")
    return True


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


async def search_threads_by_keyword_async(keyword: str, limit: int = 20) -> list[ScrapedPost]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        cookies_path = Path(COOKIES_FILE)
        if cookies_path.exists():
            try:
                await context.add_cookies(json.loads(cookies_path.read_text()))
            except Exception:
                pass

        page = await context.new_page()
        posts: list[ScrapedPost] = []
        # shortcode → 真實 media_id（從 API 回應攔截）
        api_pk_map: dict[str, str] = {}

        async def _capture_pk(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct and "javascript" not in ct:
                    return
                body = await response.text()
                # 只處理包含 post 資料特徵的回應
                has_id = '"pk"' in body or ('"id"' in body and len(body) > 500)
                has_code = '"code"' in body or '"shortcode"' in body or '/post/' in body
                if has_id and has_code:
                    found = _extract_pk_map(body)
                    if found:
                        logger.debug(f"[海巡] 從 {response.url[:60]} 捕捉到 {len(found)} 個 pk")
                        api_pk_map.update(found)
            except Exception:
                pass

        page.on("response", _capture_pk)

        try:
            if not await _ensure_logged_in(page, context):
                return []

            encoded = urllib.parse.quote(keyword)
            search_url = f"https://www.threads.com/search?q={encoded}&serp_type=recent"
            logger.info(f"[海巡] 前往搜尋頁面: {search_url}")
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

            logger.info(f"[海巡] API 攔截到 {len(api_pk_map)} 個 pk 對應")
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

                    # 用 JavaScript 往上遍歷找貼文文字、帳號、media ID
                    result = await link_el.evaluate("""el => {
                        let node = el;
                        let text = '';
                        let username = '';
                        let mediaId = '';
                        for (let i = 0; i < 15; i++) {
                            node = node.parentElement;
                            if (!node) break;
                            const t = (node.innerText || '').trim();
                            // 嘗試抓 data-id / data-post-id 等屬性
                            const did = node.getAttribute('data-id') || node.getAttribute('data-post-id') || node.getAttribute('data-media-id');
                            if (did && /^\\d{10,}$/.test(did)) mediaId = did;
                            if (!text && t.length > 50) text = t.slice(0, 500);
                        }
                        node = el;
                        for (let i = 0; i < 15; i++) {
                            node = node.parentElement;
                            if (!node) break;
                            const uLink = node.querySelector('a[href^="/@"]');
                            if (uLink) {
                                username = (uLink.getAttribute('href') || '').replace(/^\\/@/, '').split('/')[0];
                                break;
                            }
                        }
                        return {text: text, username: username, mediaId: mediaId};
                    }""")

                    raw_text = (result.get("text") or "").strip()
                    username = (result.get("username") or "").strip()
                    media_id = (
                        api_pk_map.get(shortcode)
                        or (result.get("mediaId") or "").strip()
                        or _shortcode_to_id(shortcode)
                    )
                    logger.debug(f"[海巡] shortcode={shortcode} media_id={media_id} (api_map={bool(api_pk_map.get(shortcode))})")

                    text, age_hours = _parse_post_text(raw_text)

                    if not text or len(text) < 20:
                        continue
                    if username.lower() == my_username:
                        continue

                    posts.append(ScrapedPost(shortcode=shortcode, text=text, username=username, media_id=media_id, age_hours=age_hours))
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
            await browser.close()


async def _ui_reply_to_post(page, post_url: str, reply_text: str) -> bool:
    """
    使用 Playwright 瀏覽器 UI 在 Threads 串文底下回覆。
    回傳 True 表示成功送出。
    """
    try:
        logger.info(f"[海巡-UI] 前往貼文: {post_url}")
        await page.goto(post_url, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(3)

        # 確認頁面已載入（找到貼文容器）
        try:
            await page.wait_for_selector('a[href*="/post/"]', timeout=10000)
        except Exception:
            logger.warning(f"[海巡-UI] 頁面未正確載入: {post_url}")
            return False

        # 找回覆按鈕：Threads UI 的 svg aria-label 或 data-testid
        reply_btn = None
        for sel in [
            'svg[aria-label="Reply"]',
            'svg[aria-label="回覆"]',
            '[data-testid="reply-button"]',
            'div[role="button"] svg[aria-label*="eply"]',
        ]:
            reply_btn = await page.query_selector(sel)
            if reply_btn:
                break

        if not reply_btn:
            # 嘗試找第一個 article 內的 svg 按鈕群組的第一個 button
            reply_btn = await page.query_selector('article div[role="button"]:first-of-type')

        if not reply_btn:
            logger.warning("[海巡-UI] 找不到回覆按鈕")
            return False

        await reply_btn.click()
        await asyncio.sleep(1.5)

        # 找回覆輸入框（contenteditable 或 textarea）
        input_box = None
        for sel in [
            '[role="textbox"][aria-label*="eply"]',
            '[role="textbox"][aria-label*="覆"]',
            '[contenteditable="true"]',
            'textarea',
        ]:
            input_box = await page.query_selector(sel)
            if input_box:
                break

        if not input_box:
            logger.warning("[海巡-UI] 找不到輸入框")
            return False

        await input_box.click()
        await asyncio.sleep(0.5)
        await input_box.type(reply_text, delay=30)
        await asyncio.sleep(0.5)

        # 找送出按鈕
        post_btn = None
        for sel in [
            'div[role="button"][aria-label*="Post"]',
            'div[role="button"][aria-label*="送出"]',
            'button[type="submit"]',
            # 備用：找含「Post」文字的按鈕
        ]:
            post_btn = await page.query_selector(sel)
            if post_btn:
                break

        if not post_btn:
            # 備用：用 Ctrl+Enter 送出
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
    reply_tasks: list[dict],  # [{"shortcode": ..., "username": ..., "text": ...}]
    skip_search: bool = False,  # True = 跳過搜尋階段，只做 UI 回覆（Phase 2 用）
) -> dict:
    """
    在同一個登入瀏覽器 session 中：先搜尋關鍵字貼文，再對指定貼文用 UI 回覆。
    reply_tasks: 要回覆的貼文清單，每筆含 shortcode, username, reply_text
    skip_search: Phase 2 傳 True，跳過搜尋直接回覆，省一次 Threads 請求
    回傳 {"posts": [...], "replied": [shortcode, ...], "failed": [shortcode, ...]}
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        # 載入 cookies
        cookies_env = os.environ.get("THREADS_COOKIES", "")
        if cookies_env:
            try:
                raw = json.loads(cookies_env)
                pw_cookies = []
                for c in raw:
                    pw = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".threads.com"),
                        "path": c.get("path", "/"),
                    }
                    exp = c.get("expirationDate") or c.get("expires")
                    if exp and exp > 0:
                        pw["expires"] = int(exp)
                    if "httpOnly" in c:
                        pw["httpOnly"] = bool(c["httpOnly"])
                    if "secure" in c:
                        pw["secure"] = bool(c["secure"])
                    ss = c.get("sameSite") or "Lax"
                    pw["sameSite"] = {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}.get(ss.lower(), "Lax")
                    pw_cookies.append(pw)
                await context.add_cookies(pw_cookies)
            except Exception as e:
                logger.warning(f"[海巡] cookies 載入失敗: {e}")

        page = await context.new_page()
        replied = []
        failed = []
        posts = []

        try:
            # 確認登入
            await page.goto("https://www.threads.com/", wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(2)
            if "login" in page.url.lower():
                logger.error("[海巡] 未登入，無法執行")
                return {"posts": [], "replied": [], "failed": [t["shortcode"] for t in reply_tasks]}

            # 搜尋（Phase 2 傳 skip_search=True 時跳過）
            if not skip_search:
                api_pk_map: dict[str, str] = {}

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
                    except Exception:
                        pass

                page.on("response", _capture_pk)

                encoded = urllib.parse.quote(keyword)
                search_url = f"https://www.threads.com/search?q={encoded}&serp_type=recent"
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
                        result = await link_el.evaluate("""el => {
                            let node = el, text = '', username = '', mediaId = '';
                            for (let i = 0; i < 15; i++) {
                                node = node.parentElement; if (!node) break;
                                const t = (node.innerText||'').trim();
                                const did = node.getAttribute('data-id')||node.getAttribute('data-post-id')||node.getAttribute('data-media-id');
                                if (did && /^\\d{10,}$/.test(did)) mediaId = did;
                                if (!text && t.length > 50) text = t.slice(0,500);
                            }
                            node = el;
                            for (let i = 0; i < 15; i++) {
                                node = node.parentElement; if (!node) break;
                                const uLink = node.querySelector('a[href^="/@"]');
                                if (uLink) { username=(uLink.getAttribute('href')||'').replace(/^\\/@/,'').split('/')[0]; break; }
                            }
                            return {text,username,mediaId};
                        }""")
                        raw_text = (result.get("text") or "").strip()
                        username = (result.get("username") or "").strip()
                        media_id = api_pk_map.get(sc) or (result.get("mediaId") or "").strip() or _shortcode_to_id(sc)
                        text, age_hours = _parse_post_text(raw_text)
                        if not text or len(text) < 20 or username.lower() == my_username:
                            continue
                        posts.append(ScrapedPost(shortcode=sc, text=text, username=username, media_id=media_id, age_hours=age_hours))
                    except Exception:
                        pass

                logger.info(f"[海巡] 找到 {len(posts)} 篇貼文")

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
            await browser.close()

        return {"posts": posts, "replied": replied, "failed": failed}
