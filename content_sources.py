"""
content_sources.py
外部資料爬取 — RSS Feed & NewsAPI
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import feedparser  # pip install feedparser

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────
# 預設 RSS 來源（保險 / 理財 / 台灣財經）
# 可在 .env 中用 RSS_FEEDS 覆蓋，逗號分隔
# ──────────────────────────────────────────

DEFAULT_RSS_FEEDS = [
    "https://www.lia-roc.org.tw/rss.xml",           # 壽險公會
    "https://money.udn.com/rss/news/1001/5590",      # 聯合新聞 - 保險理財
    "https://www.chinatimes.com/rss/finance.xml",    # 工商時報財經
    "https://ec.ltn.com.tw/rss/money.xml",           # 自由財經
    "https://news.cnyes.com/rss/cat/tw_stock_news",  # 鉅亨台股新聞
]


def fetch_rss_articles(
    feeds: Optional[list[str]] = None,
    max_age_hours: int = 24,
    max_per_feed: int = 3,
) -> list[dict]:
    """
    從 RSS Feed 拉取最新文章。
    回傳 [{"title": str, "summary": str, "url": str, "published": str}, ...]
    """
    feeds = feeds or _load_feeds_from_env()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    articles = []

    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            count = 0
            for entry in feed.entries:
                if count >= max_per_feed:
                    break

                # 解析發布時間
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    import calendar
                    ts = calendar.timegm(entry.published_parsed)
                    published = datetime.fromtimestamp(ts, tz=timezone.utc)

                # 過濾太舊的文章
                if published and published < cutoff:
                    continue

                summary = getattr(entry, "summary", "") or ""
                # 清除 HTML tags（簡單處理）
                import re
                summary = re.sub(r"<[^>]+>", "", summary).strip()[:300]

                articles.append({
                    "title": entry.get("title", ""),
                    "summary": summary,
                    "url": entry.get("link", ""),
                    "published": published.isoformat() if published else "",
                    "source": feed.feed.get("title", feed_url),
                })
                count += 1

            logger.info(f"RSS {feed_url}: 取得 {count} 篇")
        except Exception as e:
            logger.warning(f"RSS 解析失敗 {feed_url}: {e}")

    # 依時間排序（最新在前）
    articles.sort(key=lambda x: x["published"], reverse=True)
    logger.info(f"共取得 {len(articles)} 篇文章")
    return articles


def fetch_newsapi_articles(
    query: str = "保險 理財 退休",
    language: str = "zh",
    max_results: int = 10,
) -> list[dict]:
    """
    透過 NewsAPI 搜尋文章（需 NEWSAPI_KEY）。
    https://newsapi.org — 免費版 100 req/day
    """
    api_key = os.getenv("NEWSAPI_KEY")
    if not api_key:
        logger.warning("NEWSAPI_KEY 未設定，跳過 NewsAPI")
        return []

    params = {
        "q": query,
        "language": language,
        "sortBy": "publishedAt",
        "pageSize": max_results,
        "apiKey": api_key,
    }

    try:
        resp = httpx.get(
            "https://newsapi.org/v2/everything",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        articles = []
        for item in data.get("articles", []):
            articles.append({
                "title": item.get("title", ""),
                "summary": (item.get("description") or "")[:300],
                "url": item.get("url", ""),
                "published": item.get("publishedAt", ""),
                "source": item.get("source", {}).get("name", ""),
            })
        return articles
    except Exception as e:
        logger.error(f"NewsAPI 錯誤: {e}")
        return []


def get_daily_articles(max_total: int = 8) -> list[dict]:
    """
    整合 RSS + NewsAPI，回傳今日可用文章（去重）
    """
    rss = fetch_rss_articles()
    news = fetch_newsapi_articles()

    combined = rss + news

    # 依 URL 去重
    seen_urls = set()
    unique = []
    for a in combined:
        if a["url"] not in seen_urls:
            seen_urls.add(a["url"])
            unique.append(a)

    return unique[:max_total]


# ──────────────────────────────────────────
# 工具
# ──────────────────────────────────────────

def _load_feeds_from_env() -> list[str]:
    env_feeds = os.getenv("RSS_FEEDS", "")
    if env_feeds:
        return [f.strip() for f in env_feeds.split(",") if f.strip()]
    return DEFAULT_RSS_FEEDS
