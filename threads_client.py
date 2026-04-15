"""
threads_client.py
Threads API 封裝 — 發文、回覆、讀取留言
Meta Threads API base: https://graph.threads.net/v1.0/
"""

import os
import time
import logging
from typing import Optional
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

THREADS_API_BASE = "https://graph.threads.net/v1.0"


# ──────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────

@dataclass
class ThreadsPost:
    id: str
    text: str
    timestamp: str
    permalink: str = ""
    media_type: str = "TEXT_POST"


@dataclass
class ThreadsReply:
    id: str
    text: str
    timestamp: str
    username: str = ""
    is_reply: bool = True


# ──────────────────────────────────────────
# Client
# ──────────────────────────────────────────

class ThreadsClient:
    """
    使用方式：
        client = ThreadsClient(
            access_token=os.getenv("THREADS_ACCESS_TOKEN"),
            user_id=os.getenv("THREADS_USER_ID"),
        )
    """

    def __init__(self, access_token: str, user_id: str, timeout: int = 30):
        self.access_token = access_token
        self.user_id = user_id
        self.timeout = timeout
        self._http = httpx.Client(timeout=timeout)

    def _get(self, path: str, params: dict = None) -> dict:
        params = params or {}
        params["access_token"] = self.access_token
        url = f"{THREADS_API_BASE}/{path}"
        resp = self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict = None) -> dict:
        data = data or {}
        data["access_token"] = self.access_token
        url = f"{THREADS_API_BASE}/{path}"
        resp = self._http.post(url, data=data)
        resp.raise_for_status()
        return resp.json()

    # ── 發文 ──────────────────────────────

    def create_post(
        self,
        text: str,
        reply_control: str = "everyone",
        reply_to_id: Optional[str] = None,
    ) -> str:
        """
        發一篇文章或回覆留言。
        reply_control: everyone | accounts_you_follow | mentioned_only | followers_only
        reply_to_id:   若填入，代表回覆特定留言（自動回覆功能使用）
        回傳 post_id (str)
        """
        # Step 1: 建立 media container
        container_payload = {
            "media_type": "TEXT",
            "text": text,
            "reply_control": reply_control,
        }
        if reply_to_id:
            container_payload["reply_to_id"] = reply_to_id

        container = self._post(f"{self.user_id}/threads", container_payload)
        container_id = container.get("id")
        if not container_id:
            raise ValueError(f"建立 container 失敗: {container}")

        logger.info(f"Container 建立成功: {container_id}")

        # Step 2: 等待 server 處理（Meta 建議平均 30 秒）
        time.sleep(31)

        # Step 3: 發布
        result = self._post(
            f"{self.user_id}/threads_publish",
            {"creation_id": container_id},
        )
        post_id = result.get("id")
        if not post_id:
            raise ValueError(f"發布失敗: {result}")

        logger.info(f"發文成功: post_id={post_id}")
        return post_id

    def reply_to_comment(self, reply_id: str, text: str) -> str:
        """回覆特定留言，回傳新 reply post_id"""
        return self.create_post(text=text, reply_to_id=reply_id)

    # ── 讀取留言 ──────────────────────────

    def get_replies(self, post_id: str, limit: int = 20) -> list[ThreadsReply]:
        """
        取得某篇貼文的所有頂層留言。
        回傳 List[ThreadsReply]
        """
        fields = "id,text,timestamp,username,is_reply,hide_status"
        data = self._get(
            f"{post_id}/replies",
            {"fields": fields, "limit": limit},
        )
        replies = []
        for item in data.get("data", []):
            replies.append(
                ThreadsReply(
                    id=item["id"],
                    text=item.get("text", ""),
                    timestamp=item.get("timestamp", ""),
                    username=item.get("username", ""),
                    is_reply=item.get("is_reply", True),
                )
            )
        return replies

    def get_conversation(self, post_id: str) -> list[ThreadsReply]:
        """取得完整對話串（含巢狀回覆）"""
        fields = "id,text,timestamp,username,is_reply,replied_to,has_replies"
        data = self._get(
            f"{post_id}/conversation",
            {"fields": fields},
        )
        replies = []
        for item in data.get("data", []):
            replies.append(
                ThreadsReply(
                    id=item["id"],
                    text=item.get("text", ""),
                    timestamp=item.get("timestamp", ""),
                    username=item.get("username", ""),
                )
            )
        return replies

    # ── 讀取自己的貼文 ─────────────────────

    def get_my_posts(self, limit: int = 10) -> list[ThreadsPost]:
        """取得自己最新的 N 篇貼文"""
        fields = "id,text,timestamp,permalink,media_type"
        data = self._get(
            f"{self.user_id}/threads",
            {"fields": fields, "limit": limit},
        )
        posts = []
        for item in data.get("data", []):
            posts.append(
                ThreadsPost(
                    id=item["id"],
                    text=item.get("text", ""),
                    timestamp=item.get("timestamp", ""),
                    permalink=item.get("permalink", ""),
                    media_type=item.get("media_type", "TEXT_POST"),
                )
            )
        return posts

    # ── 管理留言 ──────────────────────────

    def hide_reply(self, reply_id: str, hide: bool = True) -> bool:
        """隱藏 / 取消隱藏某則留言"""
        result = self._post(
            f"{reply_id}/manage_reply",
            {"hide": str(hide).lower()},
        )
        return result.get("success", False)

    # ── Token 管理 ─────────────────────────

    def refresh_long_lived_token(self, app_secret: str) -> dict:
        """
        刷新 Long-lived token（每 60 天需更新一次）
        回傳 {"access_token": "...", "expires_in": 5183944}
        """
        params = {
            "grant_type": "th_refresh_token",
            "access_token": self.access_token,
        }
        resp = self._http.get(
            f"{THREADS_API_BASE}/refresh_access_token",
            params=params,
        )
        resp.raise_for_status()
        result = resp.json()
        # 更新 client 內的 token
        self.access_token = result["access_token"]
        logger.info("Token 刷新成功")
        return result



    def search_posts(self, keyword: str, limit: int = 20) -> list:
        """搜尋含特定關鍵字的公開貼文"""
        fields = "id,text,timestamp,username,permalink"
        try:
            data = self._get(
                "threads/search",
                {"q": keyword, "fields": fields, "limit": limit},
            )
            posts = []
            for item in data.get("data", []):
                if item.get("text"):
                    posts.append(ThreadsPost(
                        id=item["id"],
                        text=item.get("text", ""),
                        timestamp=item.get("timestamp", ""),
                        permalink=item.get("permalink", ""),
                    ))
            return posts
        except Exception as e:
            logger.warning(f"搜尋失敗: {e}")
            return []

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ──────────────────────────────────────────
# 快速測試（直接執行此檔案時）
# ──────────────────────────────────────────

if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)

    client = ThreadsClient(
        access_token=os.environ["THREADS_ACCESS_TOKEN"],
        user_id=os.environ["THREADS_USER_ID"],
    )

    # 測試：取得最新 3 篇貼文
    posts = client.get_my_posts(limit=3)
    print("=== 最新貼文 ===")
    for p in posts:
        print(f"  [{p.timestamp}] {p.text[:50]}...")

    # 測試：取得第一篇貼文的留言
    if posts:
        replies = client.get_replies(posts[0].id)
        print(f"\n=== 留言 ({len(replies)} 則) ===")
        for r in replies:
            print(f"  @{r.username}: {r.text[:60]}")
