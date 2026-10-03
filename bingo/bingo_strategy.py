#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
賓果賓果（BINGO BINGO）選號策略程式 — 純 Python 標準函式庫，免安裝套件（Python 3.8+）

指令
  fetch     抓台彩官方開獎資料，存成 bingo_history.csv
  import    匯入 CSV/TXT 開獎資料（台彩歷史下載檔、自己整理的號碼都可以）
  stats     冷熱號、遺漏、連莊、尾數、區間、大小單雙、超級獎號統計
  pick      產生下一期選號
  backtest  逐期回測（每期只用「當期以前」的資料），對照隨機與理論值
  demo      沒網路時用模擬資料跑一遍

範例
  python bingo_strategy.py fetch --days 14
  python bingo_strategy.py stats
  python bingo_strategy.py pick --stars 4
  python bingo_strategy.py pick --stars 3 --strategy hot,drag,mix --fetch
  python bingo_strategy.py backtest --stars 4 --last 1000
  python bingo_strategy.py pick --stars 5 --set hot.N=50,drag.W=1000 --weights hot=2,drag=1,cold=0
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import random
import re
import sys
import time
import unicodedata
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import chain
from typing import Callable

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "bingo_history.csv")
API_BASE = "https://api.taiwanlottery.com/TLCAPIWeB/Lottery"
TPE = timezone(timedelta(hours=8))
NUMS = range(1, 81)
BET = 25  # 每注 25 元

# 獎金表（1 倍、每注 25 元）：{星數: {中幾個: 獎金}}。依公開資料整理，實際以台彩公告為準（加碼期間不同）
PAYOUT = {
    1: {1: 50},
    2: {2: 75, 1: 25},
    3: {3: 500, 2: 50},
    4: {4: 1000, 3: 100, 2: 25},
    5: {5: 7500, 4: 500, 3: 50},
    6: {6: 25000, 5: 1000, 4: 200, 3: 25},
    7: {7: 80000, 6: 3000, 5: 300, 4: 50, 3: 25},
    8: {8: 500000, 7: 20000, 6: 1000, 5: 200, 4: 25, 0: 25},
    9: {9: 1000000, 8: 100000, 7: 3000, 6: 500, 5: 100, 4: 25, 0: 25},
    10: {10: 5000000, 9: 250000, 8: 25000, 7: 2500, 6: 250, 5: 25, 0: 25},
}
SUPER_PAYOUT = 1200  # 超級獎號（猜中當期第 20 個開出的號碼）


# ───────────────────────── 資料 ─────────────────────────
@dataclass
class Draw:
    term: int            # 期別
    date: str            # YYYY-MM-DD
    nums: tuple          # 20 個號碼（有開出順序時保留順序）
    super_no: int = 0    # 超級獎號（第 20 個開出號碼），0 = 未知
    s: frozenset = field(init=False, repr=False)

    def __post_init__(self):
        self.s = frozenset(self.nums)


def big_small(d: Draw) -> str:
    big = sum(1 for n in d.nums if n >= 41)
    return "大" if big >= 13 else "小" if big <= 7 else "－"


def odd_even(d: Draw) -> str:
    odd = sum(n & 1 for n in d.nums)
    return "單" if odd >= 13 else "雙" if odd <= 7 else "－"


def _to_int(x):
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return None


# ───────────── 台彩官方 API（欄位名稱自動偵測） ─────────────
TERM_KEYS = ("drawTerm", "period", "term", "drawNo", "issue")
DATE_KEYS = ("dDate", "lotteryDate", "openDate", "drawDate", "date")
SUPER_HINTS = ("bulleye", "super")


def http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def _num_list(v):
    """list 或 '01,02,…' 字串 → 不重複的 1~80 號碼 list；否則 None"""
    if isinstance(v, str):
        v = [x for x in re.split(r"[\s,;]+", v.strip()) if x]
    if isinstance(v, (list, tuple)) and len(v) >= 20:
        out = [_to_int(x) for x in v]
        if all(o is not None and 1 <= o <= 80 for o in out) and len(set(out)) == len(out):
            return out
    return None


def parse_api_record(d: dict):
    term = next((_to_int(d[k]) for k in TERM_KEYS if _to_int(d.get(k))), None)
    if not term:
        return None
    lists = [nl for nl in (_num_list(v) for v in d.values()) if nl and len(nl) == 20]
    if not lists:
        return None
    order = next((nl for nl in lists if nl != sorted(nl)), None)  # 沒排序的那組 = 開出順序
    nums = order or lists[0]
    sup = 0
    for k, v in d.items():
        x = _to_int(v)
        if any(h in k.lower() for h in SUPER_HINTS) and x and 1 <= x <= 80:
            sup = x
            break
    if not sup and order:
        sup = order[-1]
    dt = next((str(d[k])[:10].replace("/", "-") for k in DATE_KEYS if d.get(k)), "")
    return Draw(term, dt, tuple(nums), sup)


def find_records(js) -> list:
    stack = [js]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            r = parse_api_record(o)
            if r:
                return [r]
            stack.extend(o.values())
        elif isinstance(o, list):
            recs = [r for r in (parse_api_record(x) for x in o if isinstance(x, dict)) if r]
            if recs:
                return recs
            stack.extend(o)
    return []


def find_key(o, key):
    if isinstance(o, dict):
        if key in o:
            return o[key]
        o = list(o.values())
    if isinstance(o, list):
        for x in o:
            v = find_key(x, key)
            if v is not None:
                return v
    return None


def fetch_day(day: date) -> list:
    got, last_js = {}, None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        ds = day.strftime(fmt)
        for page in range(1, 16):
            url = f"{API_BASE}/BingoResult?openDate={ds}&pageNum={page}&pageSize=50"
            js = last_js = http_json(url)
            new = [r for r in find_records(js) if r.term not in got]
            for r in new:
                got[r.term] = r
            total = find_key(js, "totalSize")
            if not new or (isinstance(total, int) and len(got) >= total):
                break
            time.sleep(0.15)
        if got:
            break
    if not got and last_js is not None:
        with open(os.path.join(HERE, "bingo_api_debug.json"), "w", encoding="utf-8") as f:
            json.dump(last_js, f, ensure_ascii=False, indent=1)
    return list(got.values())


# ───────────── CSV / TXT 匯入（格式自動偵測） ─────────────
DATE_RE = re.compile(r"(\d{2,4})[-/.](\d{1,2})[-/.](\d{1,2})")
INT_RE = re.compile(r"^\d+$")


def _norm_date(s: str) -> str:
    m = DATE_RE.search(s or "")
    if not m:
        return ""
    y, mo, d = map(int, m.groups())
    if y < 1911:  # 民國年
        y += 1911
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _ints_in(cell: str) -> list:
    """'05' → [5]；'01 05 12' → [1, 5, 12]；其他 → []"""
    parts = cell.replace("、", " ").split()
    return [int(p) for p in parts] if parts and all(INT_RE.match(p) for p in parts) else []


def load_history(path: str) -> list:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp950"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    if "," in lines[0]:
        rows = list(csv.reader(lines))
    elif "\t" in lines[0]:
        rows = list(csv.reader(lines, delimiter="\t"))
    else:
        rows = [ln.split() for ln in lines]

    first = [c.strip() for c in rows[0]]
    has_header = len([x for c in first for x in _ints_in(c) if 1 <= x <= 80]) < 20
    i_term = i_date = i_super = None
    num_cols = None
    if has_header:
        h = [c.lower() for c in first]

        def find(keys, exclude=()):
            for k in keys:
                for i, c in enumerate(h):
                    if i not in exclude and k in c:
                        return i
            return None

        i_date = find(("開獎日期", "日期", "date"))
        i_super = find(("超級", "super", "bulleye"))
        i_term = find(("期別", "期數", "期號", "term", "period", "issue", "期"), exclude={i_date, i_super})
        num_cols = [i for i, c in enumerate(h) if i not in (i_term, i_date, i_super)
                    and re.search(r"獎號|號碼|^n\d+$|^no\.?\s*\d+$|^\d+$|^ball", c)]
        if len(num_cols) < 20:
            num_cols = None
        rows = rows[1:]

    out, bad = [], 0
    for idx, r in enumerate(rows):
        cells = [c.strip() for c in r]
        if has_header:
            get = lambda i: cells[i] if i is not None and i < len(cells) else ""
            term, dt, sup = _to_int(get(i_term)), _norm_date(get(i_date)), _to_int(get(i_super))
            cols = num_cols or [i for i in range(len(cells)) if i not in (i_term, i_date, i_super)]
            nums = [x for i in cols if i < len(cells) for x in _ints_in(cells[i]) if 1 <= x <= 80]
        else:
            dt = next((_norm_date(c) for c in cells if DATE_RE.search(c)), "")
            ints = [x for c in cells if not DATE_RE.search(c) for x in _ints_in(c)]
            term = next((x for x in ints if x >= 100000), None)
            if term is not None:
                ints = ints[ints.index(term) + 1:]
            nums = [x for x in ints if 1 <= x <= 80]
            sup = None
        if len(nums) < 20 or len(set(nums[:20])) < 20:
            bad += 1
            continue
        if not sup and len(nums) > 20:
            sup = nums[20]
        nums = nums[:20]
        if not sup or not 1 <= sup <= 80:
            sup = nums[-1] if nums != sorted(nums) else 0
        out.append(Draw(term if term is not None else idx + 1, dt, tuple(nums), sup))
    if bad:
        print(f"（{os.path.basename(path)}：略過 {bad} 列無法解析的資料）")
    m = {d.term: d for d in out}
    return [m[k] for k in sorted(m)]


def save_db(draws: list, path: str):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["term", "date"] + [f"n{i}" for i in range(1, 21)] + ["super"])
        for d in draws:
            w.writerow([d.term, d.date, *(f"{n:02d}" for n in d.nums), f"{d.super_no:02d}"])


def merge(old: list, new: list) -> list:
    m = {d.term: d for d in old}
    m.update({d.term: d for d in new})
    return [m[k] for k in sorted(m)]


# ───────────── 歷史統計（回測加速用） ─────────────
class Hist:
    def __init__(self, draws: list):
        self.draws = draws
        self.sets = [d.s for d in draws]
        self.T = len(draws)
        self.apps = [[] for _ in range(81)]  # 每個號碼出現在第幾期（索引）
        for i, s in enumerate(self.sets):
            for n in s:
                self.apps[n].append(i)
        self._t, self._m = None, {}

    def _memo(self, t, key, fn):
        if t != self._t:
            self._t, self._m = t, {}
        if key not in self._m:
            self._m[key] = fn()
        return self._m[key]

    def freq(self, t: int, N: int) -> Counter:
        """第 t 期之前 N 期，各號碼出現次數"""
        return self._memo(t, ("f", N), lambda: Counter(chain.from_iterable(self.sets[max(0, t - N):t])))

    def gaps(self, t: int) -> dict:
        """第 t 期之前，各號碼已連續幾期沒開（遺漏值）"""
        def calc():
            g = {}
            for n in NUMS:
                a = self.apps[n]
                i = bisect.bisect_left(a, t)
                g[n] = t - 1 - a[i - 1] if i else t
            return g
        return self._memo(t, ("g",), calc)

    def drag(self, t: int, W: int) -> Counter:
        """拖牌：上期每個號碼 i，近 W 期內「i 開出後下一期」各號碼出現機率，加總"""
        def calc():
            last, score, lo = self.sets[t - 1], Counter(), max(0, t - 1 - W)
            for i in last:
                a = self.apps[i]
                idxs = a[bisect.bisect_left(a, lo):bisect.bisect_left(a, t - 1)]
                if not idxs:
                    continue
                c = Counter(chain.from_iterable(self.sets[x + 1] for x in idxs))
                inv = 1.0 / len(idxs)
                for n, v in c.items():
                    score[n] += v * inv
            return score
        return self._memo(t, ("d", W), calc)


# ───────────── 選號策略 ─────────────
@dataclass
class Strat:
    key: str
    name: str
    desc: str
    fn: Callable
    params: dict


STRATS: dict = {}


def strategy(key, name, desc, **params):
    def deco(fn):
        STRATS[key] = Strat(key, name, desc, fn, params)
        return fn
    return deco


# 以下為通用策略（貼文策略待補）。每個策略回傳 {號碼: 分數}，分數高者優先
@strategy("hot", "熱號", "近 N 期出現次數最多", N=30)
def s_hot(H, t, P):
    return H.freq(t, P["N"])


@strategy("cold", "冷號回補", "遺漏期數最多（最久沒開）")
def s_cold(H, t, P):
    return H.gaps(t)


@strategy("repeat", "連莊號", "上期開出號碼，依近 N 期熱度排序", N=10)
def s_repeat(H, t, P):
    last, c = H.sets[t - 1], H.freq(t, P["N"])
    return {n: (1000 if n in last else 0) + c[n] for n in NUMS}


@strategy("drag", "拖牌", "上期號碼在歷史上「下一期」最常跟出的號碼（近 W 期）", W=600)
def s_drag(H, t, P):
    return H.drag(t, P["W"])


@strategy("neighbor", "鄰號(邊號)", "上期號碼 ±1 的號碼", N=20)
def s_neighbor(H, t, P):
    last, c = H.sets[t - 1], H.freq(t, P["N"])
    return {n: ((n - 1 in last) + (n + 1 in last)) * 1000 + c[n] for n in NUMS}


@strategy("tail", "熱尾數(直行)", "近 N 期最熱尾數，再挑該尾數最熱號碼", N=20)
def s_tail(H, t, P):
    c, tail = H.freq(t, P["N"]), Counter()
    for n in NUMS:
        tail[n % 10] += c[n]
    return {n: tail[n % 10] * 1000 + c[n] for n in NUMS}


@strategy("zone", "熱區(橫列)", "01-10…71-80 八區中近 N 期最熱的區", N=20)
def s_zone(H, t, P):
    c, zone = H.freq(t, P["N"]), Counter()
    for n in NUMS:
        zone[(n - 1) // 10] += c[n]
    return {n: zone[(n - 1) // 10] * 1000 + c[n] for n in NUMS}


@strategy("super", "超級獎號熱號", "近 N 期超級獎號出現次數（超級獎號玩法用）", N=400)
def s_super(H, t, P):
    return Counter(d.super_no for d in H.draws[max(0, t - P["N"]):t] if d.super_no)


@strategy("random", "隨機(對照組)", "純隨機，用來檢驗策略有沒有比亂選好", seed=0)
def s_random(H, t, P):
    r = random.Random(t * 7919 + P["seed"])
    return {n: r.random() for n in NUMS}


def rank01(sc) -> dict:
    """分數 → 0~1 排名（同分取平均名次）"""
    order = sorted(NUMS, key=lambda n: sc.get(n, 0))
    out, i = {}, 0
    while i < 80:
        j, v = i, sc.get(order[i], 0)
        while j + 1 < 80 and sc.get(order[j + 1], 0) == v:
            j += 1
        for x in order[i:j + 1]:
            out[x] = (i + j) / 2 / 79
        i = j + 1
    return out


MIX_DEFAULT = {"hot": 1, "repeat": 1, "drag": 1, "neighbor": 0.5, "cold": 0.5, "tail": 0.5, "zone": 0.5}


@strategy("mix", "綜合加權", "各策略排名加權投票（--weights 調權重）", weights=dict(MIX_DEFAULT))
def s_mix(H, t, P):
    tot = Counter()
    for key, w in P["weights"].items():
        if w:
            st = STRATS[key]
            for n, r in rank01(st.fn(H, t, st.params)).items():
                tot[n] += w * r
    return tot


def pick(H: Hist, t: int, key: str, k: int) -> list:
    """用第 t 期以前的資料，依策略選 k 個號碼"""
    st = STRATS[key]
    sc, rec = st.fn(H, t, st.params), H.freq(t, 10)
    return sorted(NUMS, key=lambda n: (-sc.get(n, 0), -rec[n], n))[:k]


# ───────────── 機率 / 期望值 ─────────────
def hyper(k: int, h: int) -> float:
    """選 k 個號碼、80 取 20，剛好中 h 個的機率"""
    return math.comb(20, h) * math.comb(60, k - h) / math.comb(80, k)


def theory(k: int):
    pwin = sum(hyper(k, h) for h in PAYOUT[k])
    ev = sum(hyper(k, h) * v for h, v in PAYOUT[k].items()) / BET
    return pwin, ev


# ───────────── 輸出工具 ─────────────
def fmt(ns) -> str:
    return " ".join(f"{n:02d}" for n in ns)


def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, w: int) -> str:
    return s + " " * max(0, w - _w(s))


def rpad(s: str, w: int) -> str:
    return " " * max(0, w - _w(s)) + s


# ───────────── 指令 ─────────────
DEFAULT_KEYS = ["hot", "cold", "repeat", "drag", "neighbor", "tail", "zone", "mix"]


def parse_keys(s: str, default: list) -> list:
    keys = [x.strip() for x in s.split(",") if x.strip()] if s else default
    bad = [k for k in keys if k not in STRATS]
    if bad:
        sys.exit(f"未知策略 {bad}；可用：{', '.join(STRATS)}")
    return keys


def apply_overrides(a):
    for item in filter(None, (getattr(a, "set", "") or "").split(",")):
        k, v = item.split("=")
        key, param = k.strip().split(".")
        STRATS[key].params[param] = int(v) if v.strip().isdigit() else float(v)
    for item in filter(None, (getattr(a, "weights", "") or "").split(",")):
        k, v = item.split("=")
        STRATS["mix"].params["weights"][k.strip()] = float(v)


def load_hist(db: str) -> Hist:
    if not os.path.exists(db):
        sys.exit(f"找不到 {db}\n請先執行：python bingo_strategy.py fetch --days 7（或 import / demo）")
    draws = load_history(db)
    if len(draws) < 100:
        print(f"注意：只有 {len(draws)} 期資料，建議至少 2 天以上（每天 203 期）")
    if len(draws) < 2:
        sys.exit("資料不足")
    return Hist(draws)


def quick_fetch(db: str):
    today = datetime.now(TPE).date()
    got = []
    for d in (today - timedelta(days=1), today):
        try:
            got += fetch_day(d)
        except Exception as e:
            print(f"抓取 {d} 失敗：{type(e).__name__}: {e}")
    if got:
        old = load_history(db) if os.path.exists(db) else []
        save_db(merge(old, got), db)


def cmd_fetch(a):
    today = datetime.now(TPE).date()
    d0 = date.fromisoformat(a.start) if a.start else today - timedelta(days=a.days - 1)
    d1 = date.fromisoformat(a.end) if a.end else today
    got, d = [], d0
    while d <= d1:
        try:
            recs = fetch_day(d)
            print(f"{d}：{len(recs)} 期")
            got += recs
        except Exception as e:
            print(f"{d}：失敗 {type(e).__name__}: {e}")
        d += timedelta(days=1)
    if not got:
        print("沒抓到資料。若有產生 bingo_api_debug.json，把內容傳給我調整解析；或改用 import 匯入 CSV。")
        return
    old = load_history(a.db) if os.path.exists(a.db) else []
    allr = merge(old, got)
    save_db(allr, a.db)
    print(f"已存 {a.db}：共 {len(allr)} 期（新增 {len(allr) - len(old)} 期）")


def cmd_import(a):
    new = load_history(a.file)
    if not new:
        sys.exit("沒有解析到任何一期，請確認每列有 20 個 1~80 的號碼")
    old = load_history(a.db) if os.path.exists(a.db) else []
    allr = merge(old, new)
    save_db(allr, a.db)
    print(f"匯入 {len(new)} 期 → {a.db}：共 {len(allr)} 期")


def cmd_stats(a):
    if getattr(a, "fetch", False):
        quick_fetch(a.db)
    H = load_hist(a.db)
    N, t, last, prev = a.window, H.T, H.draws[-1], H.draws[-2]
    print(f"資料 {H.T} 期：{H.draws[0].term}（{H.draws[0].date}）~ {last.term}（{last.date}）")
    print(f"最新一期 {last.term}：{fmt(sorted(last.nums))}")
    print(f"  超級獎號 {last.super_no:02d}｜猜大小 {big_small(last)}｜猜單雙 {odd_even(last)}")
    rep = sorted(last.s & prev.s)
    print(f"  連莊（與上期重複）{len(rep)} 個：{fmt(rep)}")
    reps = [len(H.sets[i] & H.sets[i - 1]) for i in range(max(1, t - N), t)]
    print(f"  近 {len(reps)} 期平均連莊 {sum(reps) / len(reps):.2f} 個（理論 5.00）")
    c, g = H.freq(t, N), H.gaps(t)
    print(f"\n近 {N} 期熱號（理論每號 {N / 4:.1f} 次）：")
    print("  " + "  ".join(f"{n:02d}({c[n]})" for n in sorted(NUMS, key=lambda n: (-c[n], n))[:12]))
    print(f"近 {N} 期最少：")
    print("  " + "  ".join(f"{n:02d}({c[n]})" for n in sorted(NUMS, key=lambda n: (c[n], n))[:12]))
    print("遺漏最久（冷號，括號＝幾期沒開）：")
    print("  " + "  ".join(f"{n:02d}({g[n]})" for n in sorted(NUMS, key=lambda n: (-g[n], n))[:12]))
    tail, zone = Counter(), Counter()
    for n in NUMS:
        tail[n % 10] += c[n]
        zone[(n - 1) // 10] += c[n]
    print(f"近 {N} 期尾數（理論 {N * 2}）：" + "  ".join(f"{d}尾({v})" for d, v in tail.most_common()))
    print(f"近 {N} 期區間（理論 {N * 2.5:.0f}）：" + "  ".join(f"{z * 10 + 1:02d}-{z * 10 + 10:02d}({v})" for z, v in zone.most_common()))
    print(f"近 20 期猜大小：{''.join(big_small(d) for d in H.draws[-20:])}")
    print(f"近 20 期猜單雙：{''.join(odd_even(d) for d in H.draws[-20:])}")
    sc = Counter(d.super_no for d in H.draws[-400:] if d.super_no)
    if sc:
        print(f"近 {min(400, H.T)} 期超級獎號熱號：" + "  ".join(f"{n:02d}({v})" for n, v in sc.most_common(10)))


def cmd_pick(a):
    if getattr(a, "fetch", False):
        quick_fetch(a.db)
    H = load_hist(a.db)
    apply_overrides(a)
    keys, k, t, last = parse_keys(a.strategy, DEFAULT_KEYS), a.stars, H.T, H.draws[-1]
    print(f"資料 {H.T} 期｜最新 {last.term}（{last.date}）")
    print(f"下一期 {last.term + 1}｜{k} 星選號：")
    for key in keys:
        print(f"  {pad(STRATS[key].name, 14)}{fmt(sorted(pick(H, t, key, k)))}")
    print(f"  {pad('超級獎號', 14)}{pick(H, t, 'super', 1)[0]:02d}")


def cmd_backtest(a):
    H = load_hist(a.db)
    apply_overrides(a)
    keys = parse_keys(a.strategy, DEFAULT_KEYS + ["random"])
    k = a.stars
    t0 = max(a.warmup, H.T - a.last)
    n = H.T - t0
    if n <= 0:
        sys.exit(f"資料不足：至少要 {a.warmup + 1} 期")
    print(f"回測 {k} 星：{H.draws[t0].term} ~ {H.draws[-1].term}，共 {n} 期（每期只用之前的資料選號）")
    dist = {key: Counter() for key in keys}
    sup_keys = ["super", "hot", "cold", "random"]
    sup_hit = Counter()
    sup_n = 0
    for t in range(t0, H.T):
        for key in keys:
            dist[key][len(H.sets[t].intersection(pick(H, t, key, k)))] += 1
        if H.draws[t].super_no:
            sup_n += 1
            for key in sup_keys:
                sup_hit[key] += pick(H, t, key, 1)[0] == H.draws[t].super_no
    exp_mean = k / 4
    se = math.sqrt(k * 0.25 * 0.75 * (80 - k) / 79 / n)
    pwin, ev = theory(k)
    print("\n" + pad("策略", 14) + rpad("平均中", 7) + rpad("z值", 7) + rpad("中獎率", 8) + rpad("回收率", 8) + "   中獎分布（中幾個:期數）")
    print(f"{pad('理論(隨機)', 14)}{exp_mean:>7.3f}{0:>7.2f}{pwin:>8.1%}{ev:>8.1%}")
    for key in keys:
        d = dist[key]
        mean = sum(h * c for h, c in d.items()) / n
        win = sum(c for h, c in d.items() if h in PAYOUT[k]) / n
        roi = sum(PAYOUT[k].get(h, 0) * c for h, c in d.items()) / (BET * n)
        spread = " ".join(f"{h}:{d[h]}" for h in sorted(d))
        print(f"{pad(STRATS[key].name, 14)}{mean:>7.3f}{(mean - exp_mean) / se:>7.2f}{win:>8.1%}{roi:>8.1%}   {spread}")
    if sup_n:
        print(f"\n超級獎號（{sup_n} 期，理論命中 1.25%、回收率 {SUPER_PAYOUT / BET / 80:.0%}）：")
        for key in sup_keys:
            r = sup_hit[key] / sup_n
            print(f"  {pad(STRATS[key].name, 14)}命中 {r:.2%}  回收率 {r * SUPER_PAYOUT / BET:.0%}")
    print("\nz 值：|z| < 2 代表跟隨機沒有顯著差異；回收率 = 獎金 ÷ 投注金額（未含加碼）")


def cmd_demo(a):
    rnd = random.Random(a.seed)
    path = os.path.join(HERE, "bingo_demo.csv")
    draws, term, start = [], 115_000_001, date(2026, 9, 26)
    for i in range(a.days):
        ds = (start + timedelta(days=i)).isoformat()
        for _ in range(203):
            nums = rnd.sample(range(1, 81), 20)
            draws.append(Draw(term, ds, tuple(nums), nums[-1]))
            term += 1
    save_db(draws, path)
    print(f"已產生模擬資料 {path}（{len(draws)} 期，純隨機，只用來測程式）\n")
    a.db, a.fetch, a.window = path, False, 30
    cmd_stats(a)
    print()
    cmd_pick(a)
    print()
    cmd_backtest(a)


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DB_PATH, help="開獎資料檔（預設 bingo_history.csv）")
    st = argparse.ArgumentParser(add_help=False)
    st.add_argument("--stars", "-k", type=int, default=4, choices=range(1, 11), metavar="1~10", help="幾星（預設 4）")
    st.add_argument("--strategy", "-s", default="", help="策略代號，逗號分隔：" + ",".join(STRATS))
    st.add_argument("--set", default="", help="調參數，例：hot.N=50,drag.W=1000")
    st.add_argument("--weights", default="", help="綜合加權權重，例：hot=2,drag=1,cold=0")
    st.add_argument("--fetch", action="store_true", help="先抓今天最新開獎再算")

    p = argparse.ArgumentParser(description="賓果賓果選號策略程式", epilog=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", parents=[common], help="抓台彩官方開獎資料")
    f.add_argument("--days", type=int, default=7, help="抓最近幾天（預設 7）")
    f.add_argument("--start", help="起始日 YYYY-MM-DD")
    f.add_argument("--end", help="結束日 YYYY-MM-DD")
    f.set_defaults(func=cmd_fetch)
    i = sub.add_parser("import", parents=[common], help="匯入 CSV/TXT 開獎資料")
    i.add_argument("file")
    i.set_defaults(func=cmd_import)
    s = sub.add_parser("stats", parents=[common], help="冷熱號等統計")
    s.add_argument("--window", "-n", type=int, default=30, help="統計近幾期（預設 30）")
    s.add_argument("--fetch", action="store_true", help="先抓今天最新開獎")
    s.set_defaults(func=cmd_stats)
    pk = sub.add_parser("pick", parents=[common, st], help="產生下一期選號")
    pk.set_defaults(func=cmd_pick)
    b = sub.add_parser("backtest", parents=[common, st], help="歷史回測")
    b.add_argument("--last", type=int, default=1000, help="回測最近幾期（預設 1000）")
    b.add_argument("--warmup", type=int, default=60, help="前面保留幾期當暖身資料")
    b.set_defaults(func=cmd_backtest)
    d = sub.add_parser("demo", parents=[st], help="用模擬資料跑一遍")
    d.add_argument("--days", type=int, default=5)
    d.add_argument("--seed", type=int, default=1)
    d.add_argument("--last", type=int, default=600)
    d.add_argument("--warmup", type=int, default=60)
    d.set_defaults(func=cmd_demo)
    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
