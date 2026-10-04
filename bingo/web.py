"""賓果攻略選號台（網頁版）：掛在 bot 的 FastAPI 上，由伺服器抓台彩開獎資料，手機在外面也能用。

路由
  GET /bingo                  選號網頁
  GET /bingo/api/draws?days=7 近 N 天開獎資料（快取，背景每分鐘更新）
  GET /bingo/api/status       抓取狀態（除錯用）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import bingo_strategy as bs

log = logging.getLogger(__name__)
router = APIRouter()

WEB_DIR = Path(__file__).resolve().parent / "web"
CACHE_PATH = Path(os.environ.get("BINGO_CACHE", "data/bingo_history.csv"))
DRAWS_PER_DAY = 203
MAX_DAYS = 14
IDLE_STOP_SEC = 30 * 60   # 30 分鐘沒人看就停止背景抓取，下次有人打開再啟動
NO_STORE = {"Cache-Control": "no-store"}

_draws: dict[int, bs.Draw] = {}
_state = {"loaded": False, "last_fetch": None, "last_ok": None, "last_error": None, "window": 7, "last_seen": 0.0}
_task: asyncio.Task | None = None


def _now_tpe() -> datetime:
    return datetime.now(bs.TPE)


def _load_cache() -> None:
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        if CACHE_PATH.exists():
            for d in bs.load_history(str(CACHE_PATH)):
                _draws[d.term] = d
    except Exception as e:  # 快取壞掉就重抓
        log.warning("bingo cache load failed: %s", e)


def _save_cache(draws: list) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        bs.save_db(draws, str(CACHE_PATH))
    except Exception as e:
        log.warning("bingo cache save failed: %s", e)


def _fetch_days(days: list) -> tuple:
    """在執行緒裡跑（阻塞 I/O）；只回傳結果，不動共用資料"""
    got, errors = [], []
    for day in days:
        try:
            for r in bs.fetch_day(day):
                got.append(r if r.date else bs.Draw(r.term, day.isoformat(), r.nums, r.super_no))
        except Exception as e:
            errors.append(f"{day}: {type(e).__name__}: {e}")
    return got, errors


def _missing_days(window: int) -> list:
    """今天一定重抓；過去日期不滿 203 期才補，每輪最多補 3 天"""
    today = _now_tpe().date()
    counts = Counter(d.date for d in _draws.values())
    past = [today - timedelta(days=i) for i in range(1, window)]
    return [today] + [d for d in past if counts.get(d.isoformat(), 0) < DRAWS_PER_DAY][:3]


async def _refresh_once() -> None:
    _load_cache()
    days = _missing_days(_state["window"])
    got, errors = await asyncio.wait_for(asyncio.to_thread(_fetch_days, days), timeout=180)
    for r in got:
        _draws[r.term] = r
    _state["last_fetch"] = _now_tpe().isoformat(timespec="seconds")
    if got:
        _state["last_ok"] = _state["last_fetch"]
        keep = (_now_tpe().date() - timedelta(days=MAX_DAYS)).isoformat()
        for term in [t for t, d in _draws.items() if d.date and d.date < keep]:
            del _draws[term]
        await asyncio.to_thread(_save_cache, [_draws[k] for k in sorted(_draws)])
    if errors:
        _state["last_error"] = "；".join(errors)
    elif not got and not _draws:
        _state["last_error"] = "台彩沒有回傳任何一期（可能 API 格式不同，請看 /bingo/api/status 的 api_debug）"
    else:
        _state["last_error"] = None


def _sleep_sec() -> int:
    h = _now_tpe().hour
    return 60 if 7 <= h <= 23 else 600   # 開獎時段 07:05~23:55 每分鐘，其他時間 10 分鐘


async def _loop() -> None:
    global _task
    try:
        while time.time() - _state["last_seen"] < IDLE_STOP_SEC:
            try:
                await _refresh_once()
            except Exception as e:
                _state["last_error"] = f"{type(e).__name__}: {e}"
                log.warning("bingo refresh failed: %s", e)
            await asyncio.sleep(_sleep_sec())
    finally:
        _task = None


def _touch(days: int = 7) -> None:
    """記錄有人在用，必要時啟動背景抓取"""
    global _task
    _state["last_seen"] = time.time()
    _state["window"] = max(2, min(MAX_DAYS, days))
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())


def _page() -> str:
    page = (WEB_DIR / "page.html").read_text(encoding="utf-8")
    core = (WEB_DIR / "core.js").read_text(encoding="utf-8")
    head = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<meta name="robots" content="noindex"><meta name="theme-color" content="#d42a3f">'
            '<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="賓果選號">'
            '<link rel="manifest" href="/bingo/manifest.webmanifest"><link rel="icon" href="/bingo/icon.svg" type="image/svg+xml">'
            '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
            'body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>'
            '<script>window.BINGO_MODE="server"</script></head><body>')
    return head + page.replace("/*__CORE__*/", core) + "</body></html>"


@router.get("/bingo", response_class=HTMLResponse)
async def bingo_page():
    _touch()
    return HTMLResponse(_page(), headers=NO_STORE)


@router.get("/bingo/api/draws")
async def bingo_draws(days: int = Query(7, ge=1, le=MAX_DAYS)):
    _touch(days)
    _load_cache()
    since = (_now_tpe().date() - timedelta(days=days - 1)).isoformat()
    rows = [[d.term, d.date, "".join(f"{n:02d}" for n in d.nums) + f"{d.super_no:02d}"]
            for k, d in sorted(_draws.items()) if not d.date or d.date >= since]
    return JSONResponse({"updated": _state["last_ok"], "checked": _state["last_fetch"], "error": _state["last_error"],
                         "fetching": _state["last_fetch"] is None, "count": len(rows), "draws": rows}, headers=NO_STORE)


@router.get("/bingo/api/status")
async def bingo_status():
    _touch()
    counts = Counter(d.date for d in _draws.values())
    debug = None
    dbg = Path(bs.HERE) / "bingo_api_debug.json"
    if dbg.exists():
        try:
            debug = dbg.read_text(encoding="utf-8")[:2000]
        except Exception:
            debug = None
    return JSONResponse({"running": _task is not None and not _task.done(), **{k: v for k, v in _state.items() if k != "loaded"},
                         "count": len(_draws), "days": dict(sorted(counts.items())), "api_debug": debug}, headers=NO_STORE)


@router.get("/bingo/manifest.webmanifest")
async def bingo_manifest():
    body = {"name": "賓果攻略選號台", "short_name": "賓果選號", "start_url": "/bingo", "scope": "/bingo",
            "display": "standalone", "background_color": "#f2f4f8", "theme_color": "#d42a3f",
            "icons": [{"src": "/bingo/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}
    return Response(json.dumps(body, ensure_ascii=False), media_type="application/manifest+json")


@router.get("/bingo/icon.svg")
async def bingo_icon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#d42a3f"/>'
           '<circle cx="32" cy="32" r="19" fill="#fff"/><text x="32" y="40" font-family="Arial,sans-serif" font-size="22" '
           'font-weight="700" text-anchor="middle" fill="#d42a3f">80</text></svg>')
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})
