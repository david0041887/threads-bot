"""
cookie_store.py
Threads 登入 cookie 的持久化層。

## 為什麼需要這一層

Meta 在瀏覽期間會不斷 rotate session cookie —— 伺服器用 Set-Cookie 換掉
sessionid / csrftoken / rur 等值，瀏覽器必須帶著「最新那份」回去。

舊版沒有這層：每次啟動都從 THREADS_COOKIES 環境變數載入同一份靜態 cookie，
瀏覽器在這次 session 收到的新值，在 browser.close() 時連同 context 一起蒸發，
下次啟動又把當初匯出的舊值送回去。

對 Meta 而言，「一份 cookie 被反覆從全新 session 重放，且永遠不接受伺服器發的
新值」正是 session 遭竊的特徵 —— 我們等於每小時對它揮一次紅旗。這是 cookie
一兩天就被全域作廢的主因之一（另一主因是 IP 與 cookie 出身地不符，那個要靠
把海巡搬到本機解，不在本模組範圍）。

## 現在的模型

- 環境變數 THREADS_COOKIES 只是**種子**：第一次啟動、或使用者手動換新時才採用。
- 實際使用的是存在 Volume（state.kv，Railway 掛在 /app/data）裡的版本，
  每次瀏覽器關閉前把 context.cookies() 整份回寫，所以永遠帶最新的 rotate 結果。
- 使用者在 Railway 貼上新的 THREADS_COOKIES 時，種子指紋改變 → 自動改用新種子。
  這條不能省：否則手動換 cookie 會被舊快照蓋掉，換幾次都沒用，而且失敗方式是
  靜默的（看起來有載入 cookie，就是登不進去）。
"""

import os
import json
import time
import hashlib
import logging

import state

logger = logging.getLogger(__name__)

_KV_KEY = "threads_cookie_jar"

# 只保留這些網域的 cookie。context.cookies() 會把造訪過的第三方網域也一起吐出來，
# 存進 KV 只是白佔空間，還會讓下次 add_cookies 變慢。
_KEEP_DOMAINS = ("threads.com", "threads.net", "instagram.com", "facebook.com")

# session 的身分核心。指紋只取這幾個值，其餘 cookie（語言、實驗分組）換來換去
# 不代表使用者換了新登入，不該觸發種子切換。
_IDENTITY_COOKIES = ("sessionid", "ds_user_id")


def _normalize(raw: list) -> list[dict]:
    """把 Cookie-Editor / Playwright 兩種格式統一成 Playwright add_cookies 吃的格式。"""
    out = []
    for c in raw:
        if not c.get("name"):
            continue
        pw = {
            "name": c["name"],
            "value": c.get("value", ""),
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
        # sameSite 可能是 None、"no_restriction"、"lax"…… Playwright 只吃三個值
        ss = c.get("sameSite") or "Lax"
        pw["sameSite"] = {
            "no_restriction": "None", "none": "None",
            "lax": "Lax", "strict": "Strict", "unspecified": "Lax",
        }.get(str(ss).lower(), "Lax")
        out.append(pw)
    return out


def _fingerprint(cookies: list[dict]) -> str:
    """對身分 cookie 取指紋。用 hash 是為了能寫進 log 而不外洩 sessionid 本身。"""
    parts = []
    for name in _IDENTITY_COOKIES:
        val = next((c["value"] for c in cookies if c.get("name") == name), "")
        parts.append(f"{name}={val}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def _seed_cookies() -> list[dict]:
    raw = os.environ.get("THREADS_COOKIES", "")
    if not raw:
        return []
    try:
        return _normalize(json.loads(raw))
    except Exception as e:
        logger.warning(f"[cookie] THREADS_COOKIES 解析失敗: {e}")
        return []


def _write(cookies: list[dict], seed_fp: str, source: str,
           rotations: int = 0, seed_at: int | None = None) -> None:
    """seed_at＝這份 session 是什麼時候啟用的。跟 saved_at 不同：saved_at 每次
    海巡都會刷新，看不出「這份 cookie 活了幾天」，而那正是我們要觀測的數字。"""
    state.set_kv(_KV_KEY, {
        "cookies": cookies,
        "seed_fp": seed_fp,
        "saved_at": int(time.time()),
        "seed_at": seed_at or int(time.time()),
        "source": source,
        "rotations": rotations,
    })


def load() -> list[dict]:
    """取得目前該用的 cookie（Playwright 格式）。空 list 代表沒有任何可用 cookie。"""
    seed = _seed_cookies()
    seed_fp = _fingerprint(seed) if seed else ""
    jar = state.get_kv(_KV_KEY, None)

    if not jar or not jar.get("cookies"):
        if seed:
            _write(seed, seed_fp, "seed")
            logger.info(f"[cookie] 首次啟用種子 cookie（{len(seed)} 個，指紋 {seed_fp}）")
        else:
            logger.error("[cookie] 沒有任何 cookie：THREADS_COOKIES 未設定且 Volume 內無存檔")
        return seed

    # 使用者換了新的 THREADS_COOKIES → 種子優先，並重置存檔
    if seed and jar.get("seed_fp") != seed_fp:
        _write(seed, seed_fp, "seed")
        logger.info(f"[cookie] 偵測到新的 THREADS_COOKIES（指紋 {jar.get('seed_fp')} → {seed_fp}），改用新種子")
        return seed

    age_h = (time.time() - jar.get("saved_at", 0)) / 3600
    logger.info(
        f"[cookie] 沿用 Volume 存檔（{len(jar['cookies'])} 個，指紋 {jar.get('seed_fp')}，"
        f"{age_h:.1f} 小時前更新，已 rotate {jar.get('rotations', 0)} 次）"
    )
    return jar["cookies"]


async def apply(context) -> int:
    """把目前該用的 cookie 灌進 Playwright context，回傳筆數。

    persistent profile（BROWSER_USER_DATA_DIR）自己就帶著上次的登入狀態，而且
    多半比我們的存檔新。這種情況灌種子只會把「還活著的 session」換成「已經死的
    那份」，而且失敗方式是靜默的——看起來一切正常，就是登不進去。所以只在
    context 裡沒有可用 session 時才灌。
    """
    try:
        existing = await context.cookies()
        if any(c.get("name") == "sessionid" and c.get("value") for c in existing):
            logger.info("[cookie] context 已自帶 session（persistent profile），不覆蓋")
            return 0
    except Exception:
        pass

    cookies = load()
    if not cookies:
        return 0
    try:
        await context.add_cookies(cookies)
        return len(cookies)
    except Exception as e:
        logger.warning(f"[cookie] add_cookies 失敗: {e}")
        return 0


async def save_from(context) -> int:
    """瀏覽器關閉前呼叫：把這次 session 拿到的最新 cookie 整份回寫。

    這是整個模組存在的理由 —— 不呼叫這行，等於回到舊版每次重放舊 cookie。
    失敗一律吞掉：cookie 沒存回頂多下次用舊的，不值得讓一次海巡整個失敗。
    """
    try:
        fresh = [
            c for c in await context.cookies()
            if any(d in (c.get("domain") or "") for d in _KEEP_DOMAINS)
        ]
        if not fresh:
            return 0
        # 若這次 session 連身分 cookie 都沒了（被登出），不要用空殼覆蓋掉還能用的存檔
        if not any(c.get("name") == "sessionid" and c.get("value") for c in fresh):
            logger.warning("[cookie] 本次 session 已無 sessionid，跳過回寫以保留既有存檔")
            return 0

        jar = state.get_kv(_KV_KEY, None) or {}
        prev = jar.get("cookies") or []
        rotations = jar.get("rotations", 0)
        changed = _diff_names(prev, fresh)
        if changed:
            rotations += 1
            logger.info(f"[cookie] 伺服器 rotate 了 {', '.join(changed)}，已回寫（累計 {rotations} 次）")
        _write(_normalize(fresh), jar.get("seed_fp") or _fingerprint(fresh), "rotated",
               rotations, seed_at=jar.get("seed_at"))
        return len(fresh)
    except Exception as e:
        logger.warning(f"[cookie] 回寫失敗（不影響本次任務）: {e}")
        return 0


def _diff_names(prev: list[dict], now: list[dict]) -> list[str]:
    """回傳有變動的 cookie 名稱。

    要比 value 也要比 expires：Meta 續期 session 時常常只延長 sessionid 的到期日、
    值不動（實測一次海巡把名目效期從 360 天推到 365 天）。只比 value 會把這種
    「伺服器認可並續期」的訊號整個漏掉，rotate 次數就永遠是 0，看起來像沒作用。
    """
    old = {c.get("name"): (c.get("value"), c.get("expires")) for c in prev}
    return sorted(
        c["name"] for c in now
        if c.get("name") in old and old[c["name"]] != (c.get("value"), c.get("expires"))
    )


def expected_user_id() -> str:
    return os.environ.get("EXPECTED_DS_USER_ID", "").strip()


def current_user_id(cookies: list[dict] | None = None) -> str:
    """目前這份 cookie 屬於哪個帳號（ds_user_id）。"""
    if cookies is None:
        jar = state.get_kv(_KV_KEY, None) or {}
        cookies = jar.get("cookies") or []
    return next((c.get("value", "") for c in cookies if c.get("name") == "ds_user_id"), "")


def account_mismatch(cookies: list[dict] | None = None) -> str:
    """登入到錯帳號時回傳說明字串，正確或未設定預期值時回空字串。

    這個檢查存在的理由：使用者平常用的是另一個個人帳號，瀏覽器會自動帶入，
    已經匯錯／登錯兩次。錯帳號的失敗方式很惡劣——一切看起來都正常運作，
    海巡照跑照推播，只是行為全記在錯的帳號上，而且要很久才會發現。
    """
    want = expected_user_id()
    if not want:
        return ""
    got = current_user_id(cookies)
    if got and got != want:
        return f"登入的是帳號 {got}，但預期是 {want}"
    return ""


def status() -> dict:
    """給 Telegram /status 用的健康摘要。"""
    jar = state.get_kv(_KV_KEY, None)
    if not jar or not jar.get("cookies"):
        # 存檔要等第一次跑瀏覽器才寫入。這時候如果報「未設定」會害人跑去
        # Railway 檢查一個其實填好的環境變數，所以兩種空狀態要分清楚。
        seed = _seed_cookies()
        if seed:
            return {"ok": True, "text": f"種子已就緒（{len(seed)} 個），尚未初始化 —— 第一次海巡後才會有存檔"}
        return {"ok": False, "text": "無 cookie：THREADS_COOKIES 未設定，且 Volume 內無存檔"}
    age_h = (time.time() - jar.get("saved_at", 0)) / 3600
    sid = next((c for c in jar["cookies"] if c.get("name") == "sessionid"), None)
    exp = sid.get("expires") if sid else None
    exp_txt = ""
    if exp:
        days = (exp - time.time()) / 86400
        exp_txt = f"，sessionid 名目效期剩 {days:.0f} 天"
    alive_d = (time.time() - jar.get("seed_at", jar.get("saved_at", 0))) / 86400
    uid = next((c.get("value") for c in jar["cookies"] if c.get("name") == "ds_user_id"), "?")
    return {
        "ok": True,
        "text": (
            f"cookie {len(jar['cookies'])} 個｜帳號 {uid}｜這份 session 已存活 {alive_d:.1f} 天｜"
            f"來源 {jar.get('source')}｜{age_h:.1f} 小時前更新｜"
            f"已 rotate {jar.get('rotations', 0)} 次{exp_txt}"
        ),
        "names": sorted(c.get("name", "") for c in jar["cookies"]),
        "alive_days": round(alive_d, 2),
        "rotations": jar.get("rotations", 0),
        "ds_user_id": uid,
    }
