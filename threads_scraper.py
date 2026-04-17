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
    await page.goto("https://www.threads.net/", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)

    if "login" in page.url.lower():
        username = os.environ.get("THREADS_USERNAME", "")
        password = os.environ.get("THREADS_PASSWORD", "")
        if not username or not password:
            logger.error("THREADS_USERNAME / THREADS_PASSWORD 未設定，無法登入爬蟲")
            return False
        try:
            await page.goto("https://www.threads.net/login", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_selector('input[autocomplete="username"]', timeout=15000)
            await page.fill('input[autocomplete="username"]', username)
            await page.fill('input[type="password"]', password)
            await page.press('input[type="password"]', "Enter")
            await page.wait_for_url(re.compile(r"threads\.net(?!/login)"), timeout=30000)
            Path(COOKIES_FILE).write_text(json.dumps(await context.cookies()))
            logger.info("Threads 登入成功，已儲存 cookies")
            return True
        except Exception as e:
            logger.error(f"Threads 登入失敗: {e}")
            return False

    return True


async def search_threads_by_keyword_async(keyword: str, limit: int = 20) -> list[ScrapedPost]:
    from playwright.async_api import async_playwright

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
            await page.goto(
                f"https://www.threads.net/search?q={encoded}&serp_type=default",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(3)

            try:
                await page.wait_for_selector("article", timeout=15000)
            except Exception:
                logger.warning(f"海巡搜尋「{keyword}」未找到任何貼文")
                return []

            # 往下滾動以載入更多
            for _ in range(2):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(1.5)

            my_username = os.environ.get("THREADS_USERNAME", "").lower()
            articles = await page.query_selector_all("article")

            for article in articles[:limit]:
                try:
                    link_el = await article.query_selector('a[href*="/post/"]')
                    if not link_el:
                        continue
                    href = await link_el.get_attribute("href") or ""
                    match = re.search(r"/post/([A-Za-z0-9_-]+)", href)
                    if not match:
                        continue
                    shortcode = match.group(1)

                    spans = await article.query_selector_all('span[dir="auto"]')
                    text = " ".join(
                        t for el in spans
                        if len(t := (await el.inner_text()).strip()) > 10
                    )
                    if not text or len(text) < 20:
                        continue

                    user_el = await article.query_selector('a[href^="/@"]')
                    username = ""
                    if user_el:
                        user_href = await user_el.get_attribute("href") or ""
                        username = user_href.lstrip("/@")

                    if username.lower() == my_username:
                        continue

                    posts.append(ScrapedPost(shortcode=shortcode, text=text, username=username))

                except Exception as e:
                    logger.debug(f"解析貼文失敗: {e}")

            logger.info(f"海巡「{keyword}」完成，共 {len(posts)} 篇")
            return posts

        except Exception as e:
            logger.error(f"海巡搜尋失敗: {e}")
            return []
        finally:
            await browser.close()
