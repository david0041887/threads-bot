"""
models.py
海巡資料結構。

從 threads_scraper.py 抽出來的理由：海巡改由本機 worker 執行後，Railway 端只
負責派工與收結果，需要能還原 ScrapedPost 卻不該為此 import 整個 threads_scraper
（那會連 playwright 一起拉進來）。放在這裡讓兩端共用同一份定義，欄位改了不會
只改到一邊——序列化兩端不同步的 bug 特別難查，因為它只在跨機器時才現形。
"""

from dataclasses import dataclass, asdict, fields

# Threads/Instagram shortcode 字母表（與 Instagram 相同）
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def shortcode_to_id(shortcode: str) -> str:
    n = 0
    for ch in shortcode:
        if ch in ALPHABET:
            n = n * 64 + ALPHABET.index(ch)
    return str(n)


@dataclass
class ScrapedPost:
    shortcode: str
    text: str
    username: str
    media_id: str = ""
    age_hours: int = 9999  # 貼文年齡（小時），9999=無法判斷
    like_count: int = 0    # 0 = 未知（API 未回傳）
    age_source: str = "無"  # 時間來源：DOM / API / 文字 / 無（診斷用）
    is_reply: bool = False  # 是否為別人貼文底下的留言（True 則海巡跳過）
    image_urls: list[str] | None = None  # 貼文內容圖片；交給多模態模型讀取，最多 4 張
    image_data: list[dict] | None = None  # 本機瀏覽器擷取後的 JPEG base64；避免 CDN 禁止模型抓取

    @property
    def id(self) -> str:
        return self.media_id or shortcode_to_id(self.shortcode)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScrapedPost":
        """只取認得的欄位。兩端版本不同步時（本機 worker 沒跟著更新），
        寧可掉一個新欄位也不要整批結果因為 TypeError 全滅。"""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
