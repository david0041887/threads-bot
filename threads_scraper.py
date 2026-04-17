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
    # 直接進 /login，若已登入會被重導到首頁
    await page.goto("https://www.threads.net/login", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    logger.info(f"[海巡] /login 目前 URL: {page.url}")

    # 若被重導離開 login 頁面，表示 cookies 有效，已登入
    if "login" not in page.url.lower():
        logger.info("[海巡] cookies 有效，已登入")
        return True

    # 需要重新登入
    username = os.environ.get("THREADS_USERNAME", "")
    password = os.environ.get("THREADS_PASSWORD", "")
    if not username or not password:
        logger.error("[海巡] THREADS_USERNAME / THREADS_PASSWORD 未設定")
        return False

    logger.info(f"[海巡] 嘗試登入帳號: {username}")
    try:
        await page.wait_for_selector('input[autocomplete="username"]', timeout=15000)
        await page.fill('input[autocomplete="username"]', username)
        await page.fill('input[type="password"]', password)
        await page.press('input[type="password"]', "Enter")
        await page.wait_for_url(re.compile(r"threads\.net(?!/login)"), timeout=30000)
        Path(COOKIES_FILE).write_text(json.dumps(await context.cookies()))
        logger.info("[海巡] 登入成功，已儲存 cookies")
        return True
    except Exception as e:
        logger.error(f"[海巡] 登入失敗: {e}")
        return False


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
            search_url = f"https://www.threads.net/search?q={encoded}&serp_type=default"
            logger.info(f"[海巡] 前往搜尋頁面: {search_url}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            logger.info(f"[海巡] 目前 URL: {page.url}")
            await asyncio.sleep(3)

            try:
                await page.wait_for_selector("article", timeout=15000)
            except Exception:
                logger.warning(f"[海巡] 搜尋「{keyword}」未找到 article 元素，目前 URL: {page.url}")
                # 嘗試截圖頁面 title 做診斷
                title = await page.title()
                logger.warning(f"[海巡] 頁面 title: {title}")
                return []

            # 往下滾動以載入更多
            for _ in range(2):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(1.5)

            my_username = os.environ.get("THREADS_USERNAME", "").lower()
            articles = await page.query_selector_all("article")
            logger.info(f"[海巡] 找到 {len(articles)} 個 article 元素")

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
