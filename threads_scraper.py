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

    @property
    def id(self) -> str:
        return _shortcode_to_id(self.shortcode)


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

        try:
            if not await _ensure_logged_in(page, context):
                return []

            encoded = urllib.parse.quote(keyword)
            search_url = f"https://www.threads.com/search?q={encoded}&serp_type=default"
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

                    # 用 JavaScript 往上遍歷找貼文文字和帳號
                    result = await link_el.evaluate("""el => {
                        let node = el;
                        let text = '';
                        let username = '';
                        for (let i = 0; i < 15; i++) {
                            node = node.parentElement;
                            if (!node) break;
                            const t = (node.innerText || '').trim();
                            if (t.length > 50) { text = t.slice(0, 500); break; }
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
                        return {text: text, username: username};
                    }""")

                    raw_text = (result.get("text") or "").strip()
                    username = (result.get("username") or "").strip()

                    # 去除開頭的帳號名稱、時間戳、hashtag 等短行雜訊
                    lines = raw_text.splitlines()
                    clean_lines = []
                    skipping = True
                    for line in lines:
                        s = line.strip()
                        if skipping and (not s or len(s) < 15 or re.match(r"^\d+[hdw]$|^@?\w{1,20}$", s)):
                            continue
                        skipping = False
                        clean_lines.append(s)
                    text = "\n".join(clean_lines).strip() or raw_text

                    if not text or len(text) < 20:
                        continue
                    if username.lower() == my_username:
                        continue

                    posts.append(ScrapedPost(shortcode=shortcode, text=text, username=username))
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
