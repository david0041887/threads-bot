#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
賓果賓果（BINGO BINGO）選號策略程式 — 整合 Threads 攻略一～四版 + 台彩開獎數據
純 Python 標準函式庫，免安裝套件（Python 3.8+）

指令
  fetch     抓台彩官方開獎資料 → bingo_history.csv
  import    匯入 CSV/TXT 開獎資料（台彩歷史下載檔、自己整理的號碼都可以）
  list      列出所有策略與貼文出處
  stats     盤面統計：連莊、冷熱號、遺漏、尾數、大小、超級獎號
  pick      依貼文策略產生下一期選號 + 超級獎號 + 猜大小建議
  backtest  逐期回測各策略（每期只用之前的資料），對照電選（隨機）與理論值
  plan      回測貼文的追號方案（3星4倍、4星3倍8期、1星10倍4期、超級獎號、猜大小）
  verify    用開獎數據檢驗貼文每一條說法 vs 純隨機理論值
  ev        各玩法中獎率、回收率（平常 vs 加碼）＋貼文追號方案的成本與可領金額
  demo      沒網路時用模擬資料跑一遍

加碼：預設依開獎日期自動套用加碼獎金（--bonus auto）；--bonus on 全部當加碼算、--bonus off 全部不加碼

範例
  python bingo_strategy.py fetch --days 14
  python bingo_strategy.py pick --stars 3 --fetch
  python bingo_strategy.py backtest --stars 3 --last 2000
  python bingo_strategy.py plan --strategy repeat,mix,random
  python bingo_strategy.py verify
  python bingo_strategy.py ev
  python bingo_strategy.py plan --bonus on
  python bingo_strategy.py pick --stars 4 --set hot.N=30,super.N=50 --weights repeat=2,hot=1,cold=0
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
BS_PAYOUT = 150      # 猜大小：6 倍

# 加碼活動（依新聞報導整理，實際以台彩公告為準）
BONUS_BASIC = {1: {1: 75}, 2: {2: 150}, 3: {3: 1000}, 4: {4: 2000, 3: 150},
               5: {5: 10000, 4: 600}, 6: {6: 50000, 5: 1200}}  # 基本玩法 1~6 星 9 個獎項
SUPER_BONUS, BS_BONUS = 1500, 175  # 超級獎號 1,500；猜大小／單雙 7 倍 175
PROMOS = [  # (開始, 結束, 名稱, 基本玩法加碼, 超級獎號, 猜大小/單雙)
    ("2026-02-27", "2026-03-03", "2026 元宵加碼", BONUS_BASIC, None, None),
    ("2026-06-05", "2026-07-05", "2026 端午加碼", None, SUPER_BONUS, BS_BONUS),
    ("2026-09-25", "2026-10-11", "2026 中秋加碼", None, SUPER_BONUS, BS_BONUS),
    ("2026-10-08", "2026-10-09", "2026 中秋快閃", BONUS_BASIC, SUPER_BONUS, BS_BONUS),
]
BONUS_MODE = "auto"  # auto：依開獎日期套用；on：全部當加碼；off：全部不加碼


def promos_on(d: str) -> list:
    return [p for p in PROMOS if d and p[0] <= d <= p[1]]


def prize(k: int, h: int, d: str = "") -> int:
    """k 星中 h 個的單注獎金（d＝開獎日期，用來判斷加碼）"""
    base = PAYOUT[k].get(h, 0)
    if BONUS_MODE == "off":
        return base
    if BONUS_MODE == "on":
        return BONUS_BASIC.get(k, {}).get(h, base)
    for pm in promos_on(d):
        if pm[3]:
            base = pm[3].get(k, {}).get(h, base)
    return base


def super_prize(d: str = "") -> int:
    if BONUS_MODE != "auto":
        return SUPER_BONUS if BONUS_MODE == "on" else SUPER_PAYOUT
    return max([pm[4] for pm in promos_on(d) if pm[4]] or [SUPER_PAYOUT])


def bs_prize(d: str = "") -> int:
    if BONUS_MODE != "auto":
        return BS_BONUS if BONUS_MODE == "on" else BS_PAYOUT
    return max([pm[5] for pm in promos_on(d) if pm[5]] or [BS_PAYOUT])


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
    """猜大小：41~80 開 13 個以上＝大，01~40 開 13 個以上＝小，其他＝－（沒開）"""
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
        self.bs = [big_small(d) for d in draws]
        self.apps = [[] for _ in range(81)]  # 每個號碼出現在第幾期（索引）
        for i, s in enumerate(self.sets):
            for n in s:
                self.apps[n].append(i)
        self.slot, self.at = [], {}  # 當天第幾期（0 起算）
        prev, k = None, 0
        for i, d in enumerate(draws):
            k = k + 1 if d.date == prev else 0
            prev = d.date
            self.slot.append(k)
            self.at[(d.date, k)] = i
        self._t, self._m = None, {}

    def _memo(self, t, key, fn):
        if t != self._t:
            self._t, self._m = t, {}
        if key not in self._m:
            self._m[key] = fn()
        return self._m[key]

    def freq(self, t: int, N: int) -> Counter:
        """第 t 期之前 N 期，各號碼開出次數"""
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

    def streaks(self, t: int) -> dict:
        """上期開出的號碼，各已連開幾期"""
        def calc():
            out = {}
            for n in self.sets[t - 1]:
                s = 1
                while t - 1 - s >= 0 and n in self.sets[t - 1 - s]:
                    s += 1
                out[n] = s
            return out
        return self._memo(t, ("s",), calc)

    def pair_rep(self, t: int, N: int) -> Counter:
        """近 N 期內各號碼「連續兩期都開」的次數（連莊次數）"""
        return self._memo(t, ("p", N), lambda: Counter(chain.from_iterable(
            self.sets[i] & self.sets[i - 1] for i in range(max(1, t - N), t))))

    def rep_count(self, t: int) -> int:
        """上期與前一期重複幾顆"""
        return len(self.sets[t - 1] & self.sets[t - 2]) if t >= 2 else 99

    def slot_of(self, t: int):
        """第 t 期的（日期, 當天第幾期）；t == T 代表下一期"""
        if t < self.T:
            return self.draws[t].date, self.slot[t]
        d, s = self.draws[-1].date, self.slot[-1] + 1
        if s >= 203 and d:
            d, s = (date.fromisoformat(d) + timedelta(days=1)).isoformat(), 0
        return d, s

    def same_slot(self, t: int, days: int = 7) -> list:
        """近 days 天同一時段的猜大小結果"""
        d, s = self.slot_of(t)
        try:
            base = date.fromisoformat(d)
        except ValueError:
            return []
        out = []
        for i in range(1, days + 1):
            j = self.at.get(((base - timedelta(days=i)).isoformat(), s))
            if j is not None:
                out.append(self.bs[j])
        return out


# ───────────── 選號策略（出處＝Threads 攻略版本與條目） ─────────────
@dataclass
class Strat:
    key: str
    name: str
    src: str
    desc: str
    fn: Callable
    params: dict


STRATS: dict = {}


def strategy(key, name, src, desc, **params):
    def deco(fn):
        STRATS[key] = Strat(key, name, src, desc, fn, params)
        return fn
    return deco


# 每個策略回傳 {號碼: 分數}（高分優先）、號碼 list（依序），或 None（條件不成立＝這期不下注）
@strategy("repeat", "連莊號", "一版1、四版3、二版10",
          "上期號碼中「連開2~3次」優先、排除已連開≥4次；同級再比近 N 期連莊次數", N=20, max_streak=3)
def s_repeat(H, t, P, k):
    st, pr, c = H.streaks(t), H.pair_rep(t, P["N"]), H.freq(t, P["N"])

    def tier(n):
        s = st.get(n, 0)
        return -1 if s > P["max_streak"] else 2 if s >= 2 else 1 if s == 1 else 0
    return {n: tier(n) * 1000 + pr[n] * 10 + c[n] for n in NUMS}


@strategy("hot", "熱門前十", "一版2", "近 N 期開出次數最多的號碼（貼文：前十熱號下期常開 3 顆）", N=20)
def s_hot(H, t, P, k):
    return H.freq(t, P["N"])


@strategy("direct", "直攻上期", "一版3",
          "上期與前一期重複 < 2 顆才下注：直接選上期號碼（建議 4~5 星）", max_rep=1, N=20)
def s_direct(H, t, P, k):
    if H.rep_count(t) > P["max_rep"]:
        return None
    last, c = H.sets[t - 1], H.freq(t, P["N"])
    return {n: (1000 if n in last else 0) + c[n] for n in NUMS}


@strategy("tail", "冷尾數同尾組", "二版1、四版7、一版4",
          "近 N 期開最少的尾數，選同尾號碼（如 21.31.41.51.61）；尾數內挑近 M 期較熱的", N=5, M=30)
def s_tail(H, t, P, k):
    rc, c = H.freq(t, P["N"]), H.freq(t, P["M"])
    tail = {d: 0 for d in range(10)}
    for n in NUMS:
        tail[n % 10] += rc[n]
    rank = {d: i for i, d in enumerate(sorted(range(10), key=lambda d: (tail[d], d)))}  # 0 = 最冷
    return {n: (10 - rank[n % 10]) * 1000 + c[n] for n in NUMS}


@strategy("combo", "自選組合", "二版2、3、5、6、7、10",
          "2熱1冷（冷＝遺漏≥10期）、大小拆散、不連號、排除連開≥4；奇數星多的那顆給上期少開的那邊", N=20, cold_gap=10)
def s_combo(H, t, P, k):
    c, g, st = H.freq(t, P["N"]), H.gaps(t), H.streaks(t)
    n_cold = 0 if k == 1 else max(1, round(k / 3))
    prev_big = sum(1 for n in H.sets[t - 1] if n >= 41)
    n_big = k // 2 + (k % 2 if prev_big < 10 else 0)  # 前期開大 → 下期偏小，反之亦然
    hot = [n for n in sorted(NUMS, key=lambda n: (-c[n], n)) if st.get(n, 0) < 4]
    cold = [n for n in sorted(NUMS, key=lambda n: (-g[n], n)) if g[n] >= P["cold_gap"]]
    picks = []

    def ok(n, quota):
        if n in picks or any(abs(n - p) == 1 for p in picks):  # 不連號
            return False
        if quota:
            nb = sum(p >= 41 for p in picks)
            if (n >= 41 and nb >= n_big) or (n <= 40 and len(picks) - nb >= k - n_big):
                return False
        return True

    for n in cold:
        if len(picks) >= n_cold:
            break
        if ok(n, True):
            picks.append(n)
    for quota in (True, False):
        for n in hot:
            if len(picks) >= k:
                break
            if ok(n, quota):
                picks.append(n)
    return picks


@strategy("outside", "非上期熱門", "四版6、二版8",
          "近 N 期熱門但不在上期 20 碼內（四版：三星 2 倍撿零錢）；min_rep>0 時只在上期重複≥min_rep 顆才下注（二版8）",
          N=20, min_rep=0)
def s_outside(H, t, P, k):
    if P["min_rep"] and H.rep_count(t) < P["min_rep"]:
        return None
    last, c = H.sets[t - 1], H.freq(t, P["N"])
    return {n: -1 if n in last else c[n] for n in NUMS}


@strategy("neighbor", "隔壁號", "四版1、2", "上期號碼的隔壁（±1）且不在上期；兩邊都相鄰的優先", N=20)
def s_neighbor(H, t, P, k):
    last, c = H.sets[t - 1], H.freq(t, P["N"])
    return {n: ((n - 1 in last) + (n + 1 in last)) * 1000 + (0 if n in last else 100) + c[n] for n in NUMS}


@strategy("cold", "10期未開", "二版7", "遺漏 ≥10 期的號碼，遺漏越久越優先")
def s_cold(H, t, P, k):
    return H.gaps(t)


@strategy("qp_nb", "電選隔壁", "四版1", "電腦隨機選號後改買每個號碼的隔壁號（+1）", seed=0)
def s_qp_nb(H, t, P, k):
    r = random.Random(t * 7919 + P["seed"])
    return [n % 80 + 1 for n in r.sample(range(1, 81), k)]


@strategy("random", "電選(隨機)", "對照組", "純隨機＝電腦選號，用來比較策略有沒有比亂選好", seed=1)
def s_random(H, t, P, k):
    return random.Random(t * 7919 + P["seed"]).sample(range(1, 81), k)


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


MIX_DEFAULT = {"repeat": 1, "hot": 1, "neighbor": 0.5, "tail": 0.5, "cold": 0.5}


@strategy("mix", "綜合加權", "整合", "各策略排名加權投票（--weights 調整）", weights=dict(MIX_DEFAULT))
def s_mix(H, t, P, k):
    tot = Counter()
    for key, w in P["weights"].items():
        if not w:
            continue
        st = STRATS[key]
        res = st.fn(H, t, st.params, k)
        if res is None:
            continue
        if isinstance(res, list):
            res = {n: len(res) - i for i, n in enumerate(res)}
        for n, r in rank01(res).items():
            tot[n] += w * r
    return tot


@strategy("super", "超級獎號", "三版1~7",
          "近 N 期超級獎號：排除上期與±near、距離>max_dist；依近3期大小平衡選邊；有重複開過的優先，沒有就挑沒開過的",
          N=45, near=2, max_dist=45)
def s_super(H, t, P, k):
    sup = [(i, H.draws[i].super_no) for i in range(max(0, t - P["N"]), t) if H.draws[i].super_no]
    if not sup:
        return None
    last = sup[-1][1]
    cnt, seen_at = Counter(s for _, s in sup), {s: i for i, s in sup}
    r3 = [s for _, s in sup[-3:]]
    want_big = sum(s >= 41 for s in r3) * 2 < len(r3)  # 近 3 期大號少 → 選大（恰恰／平衡）
    ok = [n for n in NUMS if P["near"] < abs(n - last) <= P["max_dist"] and (n >= 41) == want_big]
    first = [n for n in ok if cnt[n] >= 2] or [n for n in ok if cnt[n] == 0]
    first.sort(key=lambda n: (-cnt[n], -seen_at.get(n, -1), n))
    rest = sorted((n for n in ok if n not in first), key=lambda n: (-cnt[n], n))
    return first + rest


def pick(H: Hist, t: int, key: str, k: int):
    """用第 t 期以前的資料，依策略選 k 個號碼；條件不成立回傳 None"""
    st = STRATS[key]
    res = st.fn(H, t, st.params, k)
    if res is None:
        return None
    rec = H.freq(t, 10)
    if isinstance(res, list):
        out = list(dict.fromkeys(res))[:k]
        if len(out) < k:
            out += [n for n in sorted(NUMS, key=lambda n: (-rec[n], n)) if n not in out][:k - len(out)]
        return out
    return sorted(NUMS, key=lambda n: (-res.get(n, 0), -rec[n], n))[:k]


# ───────────── 猜大小（一版5、二版大小選） ─────────────
def bs_signal(H: Hist, t: int, bal_win=24, bal_th=0.52, hot_n=20) -> dict:
    votes = []
    last = next((H.bs[i] for i in range(t - 1, max(-1, t - 400), -1) if H.bs[i] != "－"), None)
    if last:  # 二版大小1：小之後會接大（大之後接小為對稱推論）
        votes.append(("小後接大", "大" if last == "小" else "小"))
    nums = list(chain.from_iterable(H.sets[max(0, t - bal_win):t]))
    if nums:  # 一版5：近 2 小時小號特別多 → 下一小時開大（平衡原則）
        sm = sum(n <= 40 for n in nums) / len(nums)
        if sm >= bal_th:
            votes.append(("2小時平衡", "大"))
        elif sm <= 1 - bal_th:
            votes.append(("2小時平衡", "小"))
    c = H.freq(t, hot_n)  # 二版大小2：熱門前十的大小組成
    b = sum(n >= 41 for n in sorted(NUMS, key=lambda n: (-c[n], n))[:10])
    if b != 5:
        votes.append(("前十熱號", "大" if b > 5 else "小"))
    pb = sum(n >= 41 for n in H.sets[t - 1])  # 二版6：前一期開大 → 下期偏小
    if pb != 10:
        votes.append(("上期反向", "小" if pb > 10 else "大"))
    nb = sum(v == "大" for _, v in votes)
    ns = len(votes) - nb
    direction = "大" if nb > ns else "小" if ns > nb else (votes[0][1] if votes else "－")
    drought = 0
    for i in range(t - 1, -1, -1):
        if H.bs[i] != "－":
            break
        drought += 1
    slot = H.same_slot(t)
    return {"dir": direction, "votes": votes, "drought": drought, "slot": slot,
            "slot_ok": not slot or any(x != "－" for x in slot)}


# ───────────── 機率 / 期望值 ─────────────
def hg(N: int, K: int, n: int, x: int) -> float:
    """超幾何：N 個裡有 K 個中，抽 n 個剛好中 x 個的機率"""
    if x < 0 or x > n or x > K or n - x > N - K:
        return 0.0
    return math.comb(K, x) * math.comb(N - K, n - x) / math.comb(N, n)


def theory(k: int, bonus: bool = False):
    """k 星：中獎率、回收率（bonus＝用加碼獎金）"""
    table = {**PAYOUT[k], **(BONUS_BASIC.get(k, {}) if bonus else {})}
    pwin = sum(hg(80, 20, k, h) for h in table)
    ev = sum(hg(80, 20, k, h) * v for h, v in table.items()) / BET
    return pwin, ev


P_BIG = sum(hg(80, 40, 20, x) for x in range(13, 21))  # 單期開「大」的機率（開「小」相同）


def p_tail_ge5() -> float:
    """單期有某個尾數一次開 ≥5 顆的機率"""
    poly, term = [1], [math.comb(8, j) for j in range(5)]
    for _ in range(10):
        new = [0] * (len(poly) + 4)
        for i, a in enumerate(poly):
            for j, b in enumerate(term):
                new[i + j] += a * b
        poly = new
    return 1 - poly[20] / math.comb(80, 20)


# ───────────── 輸出工具 ─────────────
def fmt(ns) -> str:
    return " ".join(f"{n:02d}" for n in ns)


def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, w: int) -> str:
    return s + " " * max(0, w - _w(s))


def rpad(s: str, w: int) -> str:
    return " " * max(0, w - _w(s)) + s


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


# ───────────── 指令 ─────────────
DEFAULT_KEYS = ["repeat", "hot", "direct", "tail", "combo", "outside", "neighbor", "cold", "qp_nb", "mix"]


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
        sys.exit(f"找不到 {db}\n請先執行：python bingo_strategy.py fetch --days 14（或 import / demo）")
    draws = load_history(db)
    if len(draws) < 100:
        print(f"注意：只有 {len(draws)} 期資料，建議至少 2 天以上（每天 203 期）")
    if len(draws) < 3:
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


def cmd_list(a):
    for s in STRATS.values():
        ps = "，".join(f"{k}={v}" for k, v in s.params.items() if k != "weights")
        print(f"{pad(s.key, 9)}{pad(s.name, 14)}[{s.src}] {s.desc}" + (f"（參數 {ps}）" if ps else ""))


def cmd_stats(a):
    if getattr(a, "fetch", False):
        quick_fetch(a.db)
    H = load_hist(a.db)
    N, t, last = a.window, H.T, H.draws[-1]
    print(f"資料 {H.T} 期：{H.draws[0].term}（{H.draws[0].date}）~ {last.term}（{last.date}）")
    print(f"最新一期 {last.term}：{fmt(sorted(last.nums))}")
    rep = sorted(last.s & H.sets[-2])
    print(f"  與前期重複 {len(rep)} 顆：{fmt(rep)}（一版：通常 3~5 顆；<2 顆觸發直攻）")
    st = H.streaks(t)
    multi = sorted((n for n in st if st[n] >= 2), key=lambda n: (-st[n], n))
    print("  連莊中：" + ("  ".join(f"{n:02d}(連{st[n]})" for n in multi) or "無"))
    big = sum(n >= 41 for n in last.nums)
    print(f"  大號 {big} 顆／小號 {20 - big} 顆｜猜大小 {H.bs[-1]}｜猜單雙 {odd_even(last)}｜超級獎號 {last.super_no:02d}")
    c, g = H.freq(t, N), H.gaps(t)
    print(f"\n近 {N} 期熱門前十（理論每號 {N / 4:.1f} 次）：")
    print("  " + "  ".join(f"{n:02d}({c[n]})" for n in sorted(NUMS, key=lambda n: (-c[n], n))[:10]))
    cold = sorted((n for n in NUMS if g[n] >= 10), key=lambda n: (-g[n], n))
    print("10 期以上未開（括號＝幾期沒開）：")
    print("  " + ("  ".join(f"{n:02d}({g[n]})" for n in cold) or "無"))
    r5 = H.freq(t, 5)
    tail = Counter({d: 0 for d in range(10)})
    for n in NUMS:
        tail[n % 10] += r5[n]
    print("近 5 期尾數（由冷到熱）：" + "  ".join(f"{d}尾({v})" for d, v in sorted(tail.items(), key=lambda x: (x[1], x[0]))))
    drought = next((i for i, r in enumerate(reversed(H.bs)) if r != "－"), H.T)
    print(f"\n近 20 期猜大小：{''.join(H.bs[-20:])}（已連續 {drought} 期沒開大小）")
    print(f"近 20 期猜單雙：{''.join(odd_even(d) for d in H.draws[-20:])}")
    print(f"近 10 期超級獎號：{fmt(d.super_no for d in H.draws[-10:])}")


def cmd_pick(a):
    if getattr(a, "fetch", False):
        quick_fetch(a.db)
    H = load_hist(a.db)
    apply_overrides(a)
    keys, k, t, last = parse_keys(a.strategy, DEFAULT_KEYS), a.stars, H.T, H.draws[-1]
    rep = H.rep_count(t)
    print(f"資料 {H.T} 期｜最新 {last.term}（{last.date}）｜上期與前期重複 {rep} 顆")
    print(f"\n下一期 {last.term + 1}｜{k} 星選號")
    for key in keys:
        st = STRATS[key]
        label = pad(f"{st.name}（{st.src}）", 38)
        ps = pick(H, t, key, k)
        if ps is None:
            need = f"<{st.params['max_rep'] + 1}" if key == "direct" else f"≥{st.params.get('min_rep', 0)}"
            print(f"  {label}這期不下注（上期重複 {rep} 顆，條件 {need} 顆）")
            continue
        extra = ""
        if key == "tail":
            extra = f"  ← {'、'.join(str(d) for d in dict.fromkeys(n % 10 for n in ps))} 尾"
        elif key == "qp_nb":
            extra = f"  ← 電選 {fmt(sorted((n - 2) % 80 + 1 for n in ps))} 的隔壁，兩組一起追"
        print(f"  {label}{fmt(sorted(ps))}{extra}")

    sp = pick(H, t, "super", 2)
    if sp:
        print(f"\n超級獎號（三版）：{fmt(sp)}  → 2~3 倍連追 3 期")
        print(f"  近 5 期 {fmt(d.super_no for d in H.draws[-5:])}；已排除上期 {last.super_no:02d}、±2、距離>45；"
              f"選{'大' if sp[0] >= 41 else '小'}號邊")
        print(f"  順便丟三星（三版8）：{fmt(sorted(pick(H, t, 'super', 3)))}")
    sig = bs_signal(H, t)
    votes = "、".join(f"{n}→{v}" for n, v in sig["votes"])
    slot = "".join(sig["slot"]) or "無資料"
    go = sig["drought"] >= 15 and sig["slot_ok"]
    print(f"\n猜大小（一版5、二版）：傾向「{sig['dir']}」（{votes}）")
    print(f"  已連續 {sig['drought']} 期沒開大小｜近 7 天同時段：{slot}｜"
          + ("可進場：6 倍追 4 期" if go else "觀望（要 15 期以上沒開、且同時段本週有開過大小）"))
    print("\n加碼：" + promo_status(H.slot_of(t)[0] or datetime.now(TPE).date().isoformat()))
    print("追號參考（一版）：3星4倍追5~8期、4星3倍追8期（成本600）、1星10倍追4期（成本1000）"
          "→ 成本與可領金額見 ev，實測見 plan")


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
    paid, wins = Counter(), Counter()
    sup, sup_n, bs_ok, bs_n = Counter(), 0, 0, 0
    for t in range(t0, H.T):
        for key in keys:
            ps = pick(H, t, key, k)
            if ps is not None:
                h = len(H.sets[t].intersection(ps))
                v = prize(k, h, H.draws[t].date)
                dist[key][h] += 1
                paid[key] += v
                wins[key] += v > 0
        sn = H.draws[t].super_no
        if sn:
            sup_n += 1
            for key in ("super", "random"):
                ps = pick(H, t, key, 2)
                sup[key] += bool(ps) and sn in ps
        if H.bs[t] != "－":
            d = bs_signal(H, t)["dir"]
            if d != "－":
                bs_n += 1
                bs_ok += d == H.bs[t]
    var1 = k * 0.25 * 0.75 * (80 - k) / 79
    pwin, ev = theory(k, BONUS_MODE == "on")
    print(bonus_note(H, t0))
    print("\n" + pad("策略", 14) + rpad("下注期", 7) + rpad("平均中", 8) + rpad("z值", 7)
          + rpad("中獎率", 8) + rpad("回收率", 8) + "   中獎分布（中幾個:期數）")
    print(pad("理論(隨機)", 14) + rpad("", 7) + f"{k / 4:>8.3f}{0:>7.2f}{pwin:>8.1%}{ev:>8.1%}")
    for key in keys:
        d = dist[key]
        m = sum(d.values())
        if not m:
            print(pad(STRATS[key].name, 14) + rpad("0", 7) + "   （期間內沒觸發）")
            continue
        avg = sum(h * c for h, c in d.items()) / m
        win = wins[key] / m
        roi = paid[key] / (BET * m)
        z = (avg - k / 4) / math.sqrt(var1 / m)
        print(pad(STRATS[key].name, 14) + f"{m:>7}{avg:>8.3f}{z:>7.2f}{win:>8.1%}{roi:>8.1%}   "
              + " ".join(f"{h}:{d[h]}" for h in sorted(d)))
    if sup_n:
        print(f"\n超級獎號 2 碼（{sup_n} 期）：三版策略命中 {sup['super'] / sup_n:.2%}｜電選 {sup['random'] / sup_n:.2%}"
              f"｜理論 2.50%（回收率平常 {SUPER_PAYOUT / BET / 80:.0%}、加碼 {SUPER_BONUS / BET / 80:.0%}）")
    if bs_n:
        print(f"猜大小方向（{bs_n} 期有開大小）：猜中 {bs_ok / bs_n:.1%}｜理論 50.0%")
    print("\nz 值：|z| < 2 ＝ 跟電選（隨機）沒有顯著差異；回收率 ＝ 獎金 ÷ 投注金額")


PLANS = [  # (方案, 出處, 類型, 星數/碼數, 倍數, 期數)
    ("3星4倍 追5期", "一版", "num", 3, 4, 5),
    ("3星4倍 追8期", "一版", "num", 3, 4, 8),
    ("3星4倍 追10期(傳說)", "一版", "num", 3, 4, 10),
    ("4星3倍 追8期", "一版", "num", 4, 3, 8),
    ("1星10倍 追4期", "一版", "num", 1, 10, 4),
    ("2星10倍 追4期", "四版9", "num", 2, 10, 4),
    ("3星2倍 非上期熱門", "四版6", "outside", 3, 2, 1),
    ("超級獎號2碼 2倍 追3期", "三版6", "super", 2, 2, 3),
    ("猜大小6倍 追4期", "二版大小4、6", "bs", 1, 6, 4),
]


def run_plan(H, kind, key, k, mult, periods, t0, stop):
    out, t, rnd = [], t0, random.Random(7)
    while t + periods <= H.T:
        if kind == "bs":
            sig = bs_signal(H, t)
            target = rnd.choice("大小") if key == "random" else sig["dir"]
            if sig["drought"] < 15 or not sig["slot_ok"] or target == "－":
                t += 1
                continue
        else:
            ps = pick(H, t, key, k)
            if ps is None:
                t += 1
                continue
            ps = set(ps)
        cost = pay = wins = 0
        for j in range(periods):
            i = t + j
            d = H.draws[i].date
            if kind == "bs":
                p, c = (bs_prize(d) if H.bs[i] == target else 0), BET
            elif kind == "super":
                p, c = (super_prize(d) if H.draws[i].super_no in ps else 0), BET * len(ps)
            else:
                p, c = prize(k, len(H.sets[i] & ps), d), BET
            cost += c * mult
            pay += p * mult
            wins += p > 0
            if stop and p > 0:
                break
        out.append((cost, pay, wins))
        t += j + 1
    return out


def cmd_plan(a):
    H = load_hist(a.db)
    apply_overrides(a)
    keys = parse_keys(a.strategy, ["mix", "random"])
    t0 = max(a.warmup, H.T - a.last)
    print(f"追號方案回測：{H.draws[t0].term} ~ {H.draws[-1].term}（{H.T - t0} 期）｜"
          + ("中了就停" if a.stop else "每期都買滿") + "｜一輪結束才開下一輪")
    print(bonus_note(H, t0))
    on = BONUS_MODE == "on"
    print("\n" + pad("方案（出處）", 34) + pad("選號", 14) + rpad("輪數", 6) + rpad("至少中1次", 11)
          + rpad("賺錢輪", 8) + rpad("平均成本", 10) + rpad("平均獎金", 10) + rpad("回收率", 8))
    for name, src, kind, k, mult, periods in PLANS:
        if kind == "num":
            ks, (pwin, roi) = keys, theory(k, on)
        elif kind == "outside":
            ks, (pwin, roi) = ["outside", "random"], theory(k, on)
        elif kind == "super":
            ks, pwin, roi = ["super", "random"], k / 80, (SUPER_BONUS if on else SUPER_PAYOUT) / BET / 80
        else:
            ks, pwin, roi = ["bs", "random"], P_BIG, (BS_BONUS if on else BS_PAYOUT) / BET * P_BIG
        title = f"{name}（{src}）"
        for key in ks:
            res = run_plan(H, kind, key, k, mult, periods, t0, a.stop)
            label = "貼文訊號" if key == "bs" else STRATS[key].name
            if not res:
                print(pad(title, 34) + pad(label, 14) + rpad("0", 6))
            else:
                cost, pay = sum(r[0] for r in res), sum(r[1] for r in res)
                print(pad(title, 34) + pad(label, 14) + f"{len(res):>6}"
                      + f"{mean(r[2] > 0 for r in res):>11.1%}{mean(r[1] > r[0] for r in res):>8.1%}"
                      + f"{cost / len(res):>10.0f}{pay / len(res):>10.0f}{pay / cost:>8.1%}")
            title = ""
        print(pad("", 34) + pad("理論值", 14) + rpad("", 6) + f"{1 - (1 - pwin) ** periods:>11.1%}"
              + rpad("", 8) + rpad("", 10) + rpad("", 10) + f"{roi:>8.1%}")
    print("\n回收率 < 100% ＝ 長期會虧；每一注的期望值固定，追幾期、幾倍只改變波動，不改變回收率")


def promo_status(d: str) -> str:
    """某天的加碼狀態 + 30 天內下一檔"""
    now = promos_on(d)
    parts = []
    for pm in now:
        items = []
        if pm[3]:
            items.append("1~6星 9 獎項（3星中3 1,000、4星中4 2,000…）")
        if pm[4]:
            items.append(f"超級獎號 {pm[4]:,}")
        if pm[5]:
            items.append(f"猜大小/單雙 {pm[5]}")
        parts.append(f"{pm[2]} {pm[0][5:]}~{pm[1][5:]}：" + "、".join(items))
    txt = "；".join(parts) if parts else "目前沒有加碼"
    try:
        lim = (date.fromisoformat(d) + timedelta(days=30)).isoformat()
        nxt = [pm for pm in PROMOS if d < pm[0] <= lim]
    except ValueError:
        nxt = []
    if nxt:
        pm = min(nxt)
        txt += f"｜下一檔 {pm[2]} {pm[0][5:]}~{pm[1][5:]}" + ("（基本玩法 1~6 星加碼）" if pm[3] else "")
    return txt


def bonus_note(H: Hist, t0: int) -> str:
    if BONUS_MODE == "on":
        return "獎金：全部以加碼獎金計算（--bonus on）"
    if BONUS_MODE == "off":
        return "獎金：全部以平常獎金計算（--bonus off）"
    pms = [promos_on(H.draws[t].date) for t in range(t0, H.T)]
    nb = sum(any(pm[3] for pm in x) for x in pms)
    ns = sum(any(pm[4] for pm in x) for x in pms)
    return f"獎金：依開獎日期套用加碼（期間內 1~6 星加碼 {nb} 期、超級獎號／猜大小加碼 {ns} 期）"


def cmd_ev(a):
    print("各玩法中獎率／回收率（回收率＝長期平均每投 100 元拿回多少）\n")
    print(pad("玩法", 10) + rpad("中獎率", 9) + rpad("平常", 9) + rpad("加碼", 9) + "   加碼獎項")
    for k in range(1, 11):
        pw, ev0 = theory(k)
        _, ev1 = theory(k, True)
        b = "、".join(f"中{h} {PAYOUT[k][h]:,}→{v:,}" for h, v in sorted(BONUS_BASIC.get(k, {}).items(), reverse=True))
        print(pad(f"{k}星", 10) + f"{pw:>9.1%}{ev0:>9.1%}{ev1:>9.1%}   {b}")
    print(pad("超級獎號", 10) + f"{1 / 80:>9.2%}{SUPER_PAYOUT / BET / 80:>9.1%}{SUPER_BONUS / BET / 80:>9.1%}"
          f"   {SUPER_PAYOUT:,}→{SUPER_BONUS:,}")
    print(pad("猜大小", 10) + f"{P_BIG:>9.1%}{BS_PAYOUT / BET * P_BIG:>9.1%}{BS_BONUS / BET * P_BIG:>9.1%}"
          f"   {BS_PAYOUT}→{BS_BONUS}")
    print("\n貼文追號方案（每期都買滿）")
    print(pad("方案", 26) + rpad("成本", 7) + rpad("至少中1次", 11) + "   最高可領（平常→加碼）"
          + "   平均拿回（平常→加碼）")
    rows = [("3星4倍 追10期", 3, 4, 10), ("3星4倍 追8期", 3, 4, 8), ("3星4倍 追5期", 3, 4, 5),
            ("4星3倍 追8期", 4, 3, 8), ("1星10倍 追4期", 1, 10, 4), ("2星10倍 追4期", 2, 10, 4)]
    for name, k, m, n in rows:
        cost = BET * m * n
        top0, top1 = PAYOUT[k][k] * m, BONUS_BASIC[k][k] * m
        print(pad(name, 26) + f"{cost:>7,}{1 - (1 - theory(k)[0]) ** n:>11.1%}"
              + pad(f"   中{k}個 {top0:,}→{top1:,}/期", 25)
              + f"   {cost * theory(k)[1]:,.0f}→{cost * theory(k, True)[1]:,.0f}")
    cost = BET * 2 * 2 * 3
    print(pad("超級獎號2碼 2倍 追3期", 26) + f"{cost:>7,}{1 - (1 - 2 / 80) ** 3:>11.1%}"
          + pad(f"   {SUPER_PAYOUT * 2:,}→{SUPER_BONUS * 2:,}/期", 25)
          + f"   {cost * SUPER_PAYOUT / BET / 80:,.0f}→{cost * SUPER_BONUS / BET / 80:,.0f}")
    cost = BET * 6 * 4
    print(pad("猜大小6倍 追4期", 26) + f"{cost:>7,}{1 - (1 - P_BIG) ** 4:>11.1%}"
          + pad(f"   {BS_PAYOUT * 6:,}→{BS_BONUS * 6:,}/期", 25)
          + f"   {cost * BS_PAYOUT / BET * P_BIG:,.0f}→{cost * BS_BONUS / BET * P_BIG:,.0f}")
    print("\n今天：" + promo_status(datetime.now(TPE).date().isoformat()))
    print("加碼金額依新聞報導整理，實際以台彩公告為準；改程式開頭的 BONUS_BASIC／PROMOS 即可")


def cmd_verify(a):
    H = load_hist(a.db)
    T, S, R = H.T, H.sets, H.bs
    rows = []

    def row(src, claim, real, theo):
        rows.append((src, claim, real, theo))

    pct = lambda x: f"{x:.1%}"
    reps = [len(S[i] & S[i - 1]) for i in range(1, T)]
    row("一版1", "每期會開 3~5 顆上期號碼",
        f"{mean(3 <= r <= 5 for r in reps):.1%}（平均 {mean(reps):.2f} 顆）",
        f"{sum(hg(80, 20, 20, x) for x in range(3, 6)):.1%}（平均 5.00 顆）")

    hh = []
    for i in range(20, T):
        c = H.freq(i, 20)
        hh.append(len(S[i].intersection(sorted(NUMS, key=lambda n: (-c[n], n))[:10])))
    row("一版2", "近 20 期熱門前十，下期約開 3 顆",
        f"平均 {mean(hh):.2f} 顆，≥3 顆 {mean(h >= 3 for h in hh):.1%}",
        f"平均 2.50 顆，≥3 顆 {sum(hg(80, 20, 10, x) for x in range(3, 11)):.1%}")

    trig = [i for i in range(20, T) if H.rep_count(i) <= 1]
    real = (f"觸發 {len(trig)} 次，4 星中獎 {mean(len(S[i].intersection(pick(H, i, 'direct', 4))) >= 2 for i in trig):.1%}"
            if trig else "期間內沒觸發")
    row("一版3", "上期重複 <2 顆 → 直攻上期號碼 4 星大部分會中", real, f"4 星中獎 {pct(theory(4)[0])}")

    t5 = [max(Counter(n % 10 for n in s).values()) >= 5 for s in S]
    row("一版4", "每小時約 5 次某尾數一次開 5~6 顆", f"每小時 {12 * mean(t5):.1f} 次", f"每小時 {12 * p_tail_ge5():.1f} 次")

    small = [sum(n <= 40 for n in s) for s in S]
    blocks = big_n = tot_n = 0
    for i in range(24, T - 12, 12):
        if sum(small[i - 24:i]) / 480 >= 0.52:
            blocks += 1
            nxt = [R[j] for j in range(i, i + 12) if R[j] != "－"]
            big_n += nxt.count("大")
            tot_n += len(nxt)
    row("一版5", "近 2 小時小號特別多（≥52%）→ 下一小時開大",
        f"{blocks} 次，下一小時大小結果「大」佔 {pct(big_n / tot_n) if tot_n else '－'}", "50.0%")

    after_big = [20 - small[i] for i in range(1, T) if 20 - small[i - 1] > 10]
    row("二版6", "前一期開大（大號 >10 顆）→ 下期偏小", f"下期平均大號 {mean(after_big):.2f} 顆（{len(after_big)} 次）", "10.00 顆")

    cur, cont, base = [0] * 81, Counter(), Counter()
    for i in range(T - 1):
        for n in NUMS:
            cur[n] = cur[n] + 1 if n in S[i] else 0
        for n in S[i]:
            base[cur[n]] += 1
            cont[cur[n]] += n in S[i + 1]
    row("二版10", "連莊球號通常不超過 4 次（連開 4 次後第 5 次再開）",
        f"{pct(cont[4] / base[4]) if base[4] else '－'}（{base[4]} 次）", "25.0%")

    seq = [r for r in R if r != "－"]
    after_small = [y for x, y in zip(seq, seq[1:]) if x == "小"]
    row("大小1", "開「小」之後，下一次大小結果是「大」",
        f"{pct(after_small.count('大') / len(after_small)) if after_small else '－'}（{len(after_small)} 次）", "50.0%")

    hit = n15 = run = 0
    for i in range(T - 2):
        if run == 15:
            n15 += 1
            hit += any(R[j] != "－" for j in range(i, i + 3))
        run = run + 1 if R[i] == "－" else 0
    row("大小6", "15 期沒開大小 → 接下來 3 期內會開",
        f"{pct(hit / n15) if n15 else '－'}（{n15} 次）", pct(1 - (1 - 2 * P_BIG) ** 3))

    sup = [d.super_no for d in H.draws if d.super_no]
    if len(sup) > 50:
        pairs = list(zip(sup, sup[1:]))
        row("三版3", "超級獎號不會連續開同號", f"連開 {mean(x == y for x, y in pairs):.2%}", "連開 1.25%")
        row("三版4", "超級獎號前後期距離通常不超過 40~50（>45 的比例）",
            pct(mean(abs(x - y) > 45 for x, y in pairs)), pct(1190 / 6400))
        row("三版5", "超級獎號不太開在上期隔壁（±1~2）", pct(mean(1 <= abs(x - y) <= 2 for x, y in pairs)), pct(314 / 6400))
        row("三版2", "超級獎號大小恰恰（連 4 期全大或全小的比例）",
            pct(mean(all(x >= 41 for x in w) or all(x <= 40 for x in w) for w in zip(sup, sup[1:], sup[2:], sup[3:]))),
            "12.5%")
        row("三版2", "用近 3 期大小平衡猜下一期超級獎號大小",
            f"猜中 {mean((sum(x >= 41 for x in sup[i - 3:i]) * 2 < 3) == (sup[i] >= 41) for i in range(3, len(sup))):.1%}",
            "50.0%")
        got = exp = 0.0
        for i in range(45, len(sup)):
            cnt = Counter(sup[i - 45:i])
            cand = {n for n in NUMS if cnt[n] >= 2} or {n for n in NUMS if cnt[n] == 0}
            got += sup[i] in cand
            exp += len(cand) / 80
        row("三版1、7", "近 45 期重複開過的超級獎號較會再開", f"命中 {got:.0f} 次／隨機應中 {exp:.1f} 次（{got / exp:.2f} 倍）", "1.00 倍")

    print(f"用 {T} 期開獎數據檢驗貼文說法（{H.draws[0].term} ~ {H.draws[-1].term}）\n")
    for src, claim, real, theo in rows:
        print(f"[{src}] {claim}")
        print(f"    實際：{real}｜純隨機理論：{theo}")
    print("\n實際 ≈ 純隨機理論 → 這條說法只是機率本來的樣子，無法用來預測下一期")


def cmd_demo(a):
    rnd = random.Random(a.seed)
    path = os.path.join(HERE, "bingo_demo.csv")
    draws, term, start = [], 115_000_001, date(2026, 9, 24)
    for i in range(a.days):
        ds = (start + timedelta(days=i)).isoformat()
        for _ in range(203):
            nums = rnd.sample(range(1, 81), 20)
            draws.append(Draw(term, ds, tuple(nums), nums[-1]))
            term += 1
    save_db(draws, path)
    print(f"已產生模擬資料 {path}（{len(draws)} 期，純隨機，只用來測程式）\n")
    a.db, a.fetch, a.window, a.stop = path, False, 30, False
    for fn in (cmd_stats, cmd_pick, cmd_backtest, cmd_plan, cmd_verify):
        print("=" * 30, fn.__name__[4:], "=" * 30)
        fn(a)
        print()


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DB_PATH, help="開獎資料檔（預設 bingo_history.csv）")
    st = argparse.ArgumentParser(add_help=False)
    st.add_argument("--stars", "-k", type=int, default=3, choices=range(1, 11), metavar="1~10", help="幾星（預設 3）")
    st.add_argument("--strategy", "-s", default="", help="策略代號，逗號分隔：" + ",".join(STRATS))
    st.add_argument("--set", default="", help="調參數，例：hot.N=30,super.N=50")
    st.add_argument("--weights", default="", help="綜合加權權重，例：repeat=2,hot=1,cold=0")
    st.add_argument("--fetch", action="store_true", help="先抓今天最新開獎再算")
    rng = argparse.ArgumentParser(add_help=False)
    rng.add_argument("--last", type=int, default=2000, help="回測最近幾期（預設 2000）")
    rng.add_argument("--warmup", type=int, default=60, help="前面保留幾期當暖身資料")
    st.add_argument("--bonus", choices=["auto", "on", "off"], default="auto",
                    help="加碼獎金：auto 依日期（預設）、on 全部加碼、off 不加碼")

    p = argparse.ArgumentParser(description="賓果賓果選號策略程式（Threads 攻略一～四版）", epilog=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", parents=[common], help="抓台彩官方開獎資料")
    f.add_argument("--days", type=int, default=14, help="抓最近幾天（預設 14）")
    f.add_argument("--start", help="起始日 YYYY-MM-DD")
    f.add_argument("--end", help="結束日 YYYY-MM-DD")
    f.set_defaults(func=cmd_fetch)
    i = sub.add_parser("import", parents=[common], help="匯入 CSV/TXT 開獎資料")
    i.add_argument("file")
    i.set_defaults(func=cmd_import)
    sub.add_parser("list", help="列出所有策略與貼文出處").set_defaults(func=cmd_list)
    s = sub.add_parser("stats", parents=[common], help="盤面統計")
    s.add_argument("--window", "-n", type=int, default=20, help="熱號統計近幾期（預設 20）")
    s.add_argument("--fetch", action="store_true", help="先抓今天最新開獎")
    s.set_defaults(func=cmd_stats)
    sub.add_parser("pick", parents=[common, st], help="產生下一期選號").set_defaults(func=cmd_pick)
    sub.add_parser("backtest", parents=[common, st, rng], help="各策略歷史回測").set_defaults(func=cmd_backtest)
    pl = sub.add_parser("plan", parents=[common, st, rng], help="追號方案回測")
    pl.add_argument("--stop", action="store_true", help="中了就停（預設每期都買滿）")
    pl.set_defaults(func=cmd_plan)
    sub.add_parser("verify", parents=[common], help="用數據檢驗貼文說法").set_defaults(func=cmd_verify)
    sub.add_parser("ev", help="中獎率、回收率、追號成本（平常 vs 加碼）").set_defaults(func=cmd_ev)
    d = sub.add_parser("demo", parents=[st], help="用模擬資料跑一遍")
    d.add_argument("--days", type=int, default=10)
    d.add_argument("--seed", type=int, default=1)
    d.add_argument("--last", type=int, default=1500)
    d.add_argument("--warmup", type=int, default=60)
    d.set_defaults(func=cmd_demo)
    a = p.parse_args(argv)
    global BONUS_MODE
    BONUS_MODE = getattr(a, "bonus", "auto")
    a.func(a)


if __name__ == "__main__":
    main()
