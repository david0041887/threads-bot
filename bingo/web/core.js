/* 賓果攻略選號核心（與 bingo_strategy.py 同一套邏輯） */
(function (root) {
  "use strict";
  const BET = 25;
  const NUMS = Array.from({ length: 80 }, (_, i) => i + 1);
  const PAYOUT = {
    1: { 1: 50 }, 2: { 2: 75, 1: 25 }, 3: { 3: 500, 2: 50 }, 4: { 4: 1000, 3: 100, 2: 25 },
    5: { 5: 7500, 4: 500, 3: 50 }, 6: { 6: 25000, 5: 1000, 4: 200, 3: 25 },
    7: { 7: 80000, 6: 3000, 5: 300, 4: 50, 3: 25 },
    8: { 8: 500000, 7: 20000, 6: 1000, 5: 200, 4: 25, 0: 25 },
    9: { 9: 1000000, 8: 100000, 7: 3000, 6: 500, 5: 100, 4: 25, 0: 25 },
    10: { 10: 5000000, 9: 250000, 8: 25000, 7: 2500, 6: 250, 5: 25, 0: 25 },
  };
  const SUPER_PAYOUT = 1200, BS_PAYOUT = 150, SUPER_BONUS = 1500, BS_BONUS = 175;
  const BONUS_BASIC = { 1: { 1: 75 }, 2: { 2: 150 }, 3: { 3: 1000 }, 4: { 4: 2000, 3: 150 },
    5: { 5: 10000, 4: 600 }, 6: { 6: 50000, 5: 1200 } };
  const PROMOS = [
    { start: "2026-02-27", end: "2026-03-03", name: "2026 元宵加碼", basic: BONUS_BASIC, sup: null, bs: null },
    { start: "2026-06-05", end: "2026-07-05", name: "2026 端午加碼", basic: null, sup: SUPER_BONUS, bs: BS_BONUS },
    { start: "2026-09-25", end: "2026-10-11", name: "2026 中秋加碼", basic: null, sup: SUPER_BONUS, bs: BS_BONUS },
    { start: "2026-10-08", end: "2026-10-09", name: "2026 中秋快閃", basic: BONUS_BASIC, sup: SUPER_BONUS, bs: BS_BONUS },
  ];
  const cfg = { bonus: "auto" }; // auto 依日期；on 全部加碼；off 不加碼

  // ───────── 基本 ─────────
  function mkDraw(term, date, nums, sup) {
    const has = new Uint8Array(82);
    for (const n of nums) has[n] = 1;
    return { term, date: date || "", nums, sup: sup || 0, has };
  }
  const bigSmall = (d) => { let b = 0; for (const n of d.nums) if (n >= 41) b++; return b >= 13 ? "大" : b <= 7 ? "小" : "－"; };
  const oddEven = (d) => { let o = 0; for (const n of d.nums) o += n & 1; return o >= 13 ? "單" : o <= 7 ? "雙" : "－"; };
  const mean = (xs) => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : NaN;
  const pad2 = (n) => String(n).padStart(2, "0");
  function addDays(ds, n) {
    const [y, m, d] = ds.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
  }
  const validDate = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s || "");
  function todayTPE() {
    try { return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Taipei" }).format(new Date()); }
    catch (e) { return new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10); }
  }

  // ───────── 匯入解析（CSV／TXT／網頁貼上） ─────────
  const DATE_RE = /(\d{2,4})[-/.](\d{1,2})[-/.](\d{1,2})/;
  function normDate(s) {
    const m = DATE_RE.exec(s || "");
    if (!m) return "";
    let y = +m[1]; const mo = +m[2], d = +m[3];
    if (y < 1911) y += 1911;
    return `${String(y).padStart(4, "0")}-${pad2(mo)}-${pad2(d)}`;
  }
  function intsIn(cell) {
    const parts = String(cell).replace(/、/g, " ").trim().split(/\s+/).filter(Boolean);
    return parts.length && parts.every((p) => /^\d+$/.test(p)) ? parts.map(Number) : [];
  }
  const toInt = (x) => (/^\s*[+-]?\d+\s*$/.test(String(x ?? "")) ? parseInt(x, 10) : null);
  function splitCSV(line, delim) {
    const out = []; let cur = "", q = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (q) {
        if (ch === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += ch;
      } else if (ch === '"') q = true;
      else if (ch === delim) { out.push(cur); cur = ""; } else cur += ch;
    }
    out.push(cur);
    return out;
  }
  function parseHistory(text) {
    text = String(text || "").replace(/^﻿/, "")
      .replace(/[０-９]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0));
    const lines = text.split(/\r?\n/).filter((l) => l.trim());
    if (!lines.length) return { draws: [], bad: 0 };
    let rows;
    if (lines[0].includes(",")) rows = lines.map((l) => splitCSV(l, ","));
    else if (lines[0].includes("\t")) rows = lines.map((l) => splitCSV(l, "\t"));
    else rows = lines.map((l) => l.trim().split(/\s+/));
    const first = rows[0].map((c) => c.trim());
    const hasHeader = first.flatMap(intsIn).filter((x) => x >= 1 && x <= 80).length < 20;
    let iTerm = null, iDate = null, iSuper = null, numCols = null;
    if (hasHeader) {
      const h = first.map((c) => c.toLowerCase());
      const find = (keys, exclude = []) => {
        for (const k of keys) for (let i = 0; i < h.length; i++) if (!exclude.includes(i) && h[i].includes(k)) return i;
        return null;
      };
      iDate = find(["開獎日期", "日期", "date"]);
      iSuper = find(["超級", "super", "bulleye"]);
      iTerm = find(["期別", "期數", "期號", "term", "period", "issue", "期"], [iDate, iSuper]);
      numCols = h.map((c, i) => i).filter((i) => i !== iTerm && i !== iDate && i !== iSuper &&
        /獎號|號碼|^n\d+$|^no\.?\s*\d+$|^\d+$|^ball/.test(h[i]));
      if (numCols.length < 20) numCols = null;
      rows = rows.slice(1);
    }
    const out = []; let bad = 0;
    rows.forEach((r, idx) => {
      const cells = r.map((c) => c.trim());
      let term, dt, sup, nums;
      if (hasHeader) {
        const get = (i) => (i !== null && i < cells.length ? cells[i] : "");
        term = toInt(get(iTerm)); dt = normDate(get(iDate)); sup = toInt(get(iSuper));
        const cols = numCols || cells.map((c, i) => i).filter((i) => i !== iTerm && i !== iDate && i !== iSuper);
        nums = cols.filter((i) => i < cells.length).flatMap((i) => intsIn(cells[i])).filter((x) => x >= 1 && x <= 80);
      } else {
        const dc = cells.find((c) => DATE_RE.test(c));
        dt = dc ? normDate(dc) : "";
        let ints = cells.filter((c) => !DATE_RE.test(c)).flatMap(intsIn);
        const ti = ints.findIndex((x) => x >= 100000);
        term = ti >= 0 ? ints[ti] : null;
        if (ti >= 0) ints = ints.slice(ti + 1);
        nums = ints.filter((x) => x >= 1 && x <= 80);
        sup = null;
      }
      if (nums.length < 20 || new Set(nums.slice(0, 20)).size < 20) { bad++; return; }
      if (!sup && nums.length > 20) sup = nums[20];
      nums = nums.slice(0, 20);
      const sorted = nums.slice().sort((a, b) => a - b);
      if (!sup || sup < 1 || sup > 80) sup = nums.some((n, i) => n !== sorted[i]) ? nums[19] : 0;
      out.push(mkDraw(term !== null ? term : idx + 1, dt, nums, sup));
    });
    const m = new Map();
    for (const d of out) m.set(d.term, d);
    return { draws: [...m.keys()].sort((a, b) => a - b).map((k) => m.get(k)), bad };
  }
  function mergeDraws(old, incoming) {
    // 沒有期別的資料（例如只貼 20 個號碼）接在最後一期之後
    if (old.length && incoming.length && incoming.every((d) => d.term < 100000)) {
      const last = old[old.length - 1];
      incoming = incoming.map((d, i) => mkDraw(last.term + 1 + i, d.date || todayTPE(), d.nums, d.sup));
    }
    const m = new Map();
    for (const d of old) m.set(d.term, d);
    for (const d of incoming) m.set(d.term, d);
    return [...m.keys()].sort((a, b) => a - b).map((k) => m.get(k));
  }
  function toCSV(draws) {
    const head = ["term", "date"].concat(NUMS.slice(0, 20).map((i) => "n" + i), ["super"]).join(",");
    return [head].concat(draws.map((d) => [d.term, d.date, ...d.nums.map(pad2), pad2(d.sup)].join(","))).join("\n");
  }
  // 雲端儲存：每天一份文件
  function packDay(draws) {
    return { date: draws[0].date, terms: draws.map((d) => d.term),
      d: draws.map((d) => d.nums.map(pad2).join("") + pad2(d.sup)).join(""), v: 1 };
  }
  function unpackDay(body) {
    const out = [];
    if (!body || !Array.isArray(body.terms) || typeof body.d !== "string") return out;
    body.terms.forEach((term, i) => {
      const chunk = body.d.slice(i * 42, i * 42 + 42);
      if (chunk.length !== 42) return;
      const xs = []; for (let j = 0; j < 42; j += 2) xs.push(+chunk.slice(j, j + 2));
      const nums = xs.slice(0, 20);
      if (nums.every((n) => n >= 1 && n <= 80) && new Set(nums).size === 20) out.push(mkDraw(+term, body.date || "", nums, xs[20]));
    });
    return out;
  }

  // ───────── 歷史統計 ─────────
  function bisectLeft(a, x) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < x) lo = m + 1; else hi = m; } return lo; }
  class Hist {
    constructor(draws) {
      this.draws = draws; this.T = draws.length;
      this.bs = draws.map(bigSmall);
      this.apps = Array.from({ length: 81 }, () => []);
      draws.forEach((d, i) => { for (const n of d.nums) this.apps[n].push(i); });
      this.slot = []; this.at = new Map();
      let prev = null, k = 0;
      draws.forEach((d, i) => { k = d.date === prev ? k + 1 : 0; prev = d.date; this.slot.push(k); this.at.set(d.date + "|" + k, i); });
      this._t = null; this._m = new Map();
    }
    memo(t, key, fn) {
      if (t !== this._t) { this._t = t; this._m = new Map(); }
      if (!this._m.has(key)) this._m.set(key, fn());
      return this._m.get(key);
    }
    freq(t, N) {
      return this.memo(t, "f" + N, () => {
        const c = new Int32Array(81);
        for (let i = Math.max(0, t - N); i < t; i++) for (const n of this.draws[i].nums) c[n]++;
        return c;
      });
    }
    gaps(t) {
      return this.memo(t, "g", () => {
        const g = new Int32Array(81);
        for (let n = 1; n <= 80; n++) { const a = this.apps[n]; const i = bisectLeft(a, t); g[n] = i ? t - 1 - a[i - 1] : t; }
        return g;
      });
    }
    streaks(t) {
      return this.memo(t, "s", () => {
        const out = new Int32Array(81);
        if (t < 1) return out;
        for (const n of this.draws[t - 1].nums) { let s = 1; while (t - 1 - s >= 0 && this.draws[t - 1 - s].has[n]) s++; out[n] = s; }
        return out;
      });
    }
    pairRep(t, N) {
      return this.memo(t, "p" + N, () => {
        const c = new Int32Array(81);
        for (let i = Math.max(1, t - N); i < t; i++) for (const n of this.draws[i].nums) if (this.draws[i - 1].has[n]) c[n]++;
        return c;
      });
    }
    repCount(t) {
      if (t < 2) return 99;
      let c = 0; for (const n of this.draws[t - 1].nums) if (this.draws[t - 2].has[n]) c++;
      return c;
    }
    slotOf(t) {
      if (t < this.T) return [this.draws[t].date, this.slot[t]];
      let d = this.draws[this.T - 1].date, s = this.slot[this.T - 1] + 1;
      if (s >= 203 && validDate(d)) { d = addDays(d, 1); s = 0; }
      return [d, s];
    }
    sameSlot(t, days = 7) {
      const [d, s] = this.slotOf(t);
      if (!validDate(d)) return [];
      const out = [];
      for (let i = 1; i <= days; i++) { const j = this.at.get(addDays(d, -i) + "|" + s); if (j !== undefined) out.push(this.bs[j]); }
      return out;
    }
  }

  // ───────── 選號策略（出處＝Threads 攻略版本與條目） ─────────
  const STRATS = {};
  const ORDER = [];
  function strategy(key, name, src, desc, params, fn) { STRATS[key] = { key, name, src, desc, params, fn }; ORDER.push(key); }
  const scores = (f) => { const s = new Float64Array(81); for (let n = 1; n <= 80; n++) s[n] = f(n); return s; };

  strategy("repeat", "連莊號", "一版1、四版3、二版10", "上期號碼中「連開2~3次」優先、排除已連開≥4次；同級再比近20期連莊次數",
    { N: 20, max_streak: 3 }, (H, t, P) => {
      const st = H.streaks(t), pr = H.pairRep(t, P.N), c = H.freq(t, P.N);
      const tier = (n) => { const s = st[n]; return s > P.max_streak ? -1 : s >= 2 ? 2 : s === 1 ? 1 : 0; };
      return scores((n) => tier(n) * 1000 + pr[n] * 10 + c[n]);
    });
  strategy("hot", "熱門前十", "一版2", "近20期開出次數最多的號碼（貼文：前十熱號下期常開 3 顆）", { N: 20 },
    (H, t, P) => H.freq(t, P.N));
  strategy("direct", "直攻上期", "一版3", "上期與前一期重複少於 2 顆才下注，直接選上期號碼（建議 4~5 星）", { max_rep: 1, N: 20 },
    (H, t, P) => {
      if (H.repCount(t) > P.max_rep) return null;
      const last = H.draws[t - 1].has, c = H.freq(t, P.N);
      return scores((n) => (last[n] ? 1000 : 0) + c[n]);
    });
  strategy("tail", "冷尾數同尾組", "二版1、四版7、一版4", "近5期開最少的尾數，選同尾號碼（如 21.31.41.51.61）", { N: 5, M: 30 },
    (H, t, P) => {
      const rc = H.freq(t, P.N), c = H.freq(t, P.M), tail = new Array(10).fill(0);
      for (let n = 1; n <= 80; n++) tail[n % 10] += rc[n];
      const rank = new Array(10);
      [0, 1, 2, 3, 4, 5, 6, 7, 8, 9].sort((a, b) => (tail[a] - tail[b]) || (a - b)).forEach((d, i) => { rank[d] = i; });
      return scores((n) => (10 - rank[n % 10]) * 1000 + c[n]);
    });
  strategy("combo", "自選組合", "二版2、3、5、6、7、10", "2熱1冷（冷＝遺漏≥10期）、大小拆散、不連號、排除連開≥4", { N: 20, cold_gap: 10 },
    (H, t, P, k) => {
      const c = H.freq(t, P.N), g = H.gaps(t), st = H.streaks(t);
      const nCold = k === 1 ? 0 : Math.max(1, Math.round(k / 3));
      let prevBig = 0; for (const n of H.draws[t - 1].nums) if (n >= 41) prevBig++;
      const nBig = Math.floor(k / 2) + (prevBig < 10 ? k % 2 : 0);
      const hot = NUMS.slice().sort((a, b) => (c[b] - c[a]) || (a - b)).filter((n) => st[n] < 4);
      const cold = NUMS.slice().sort((a, b) => (g[b] - g[a]) || (a - b)).filter((n) => g[n] >= P.cold_gap);
      const picks = [];
      const ok = (n, quota) => {
        if (picks.includes(n) || picks.some((p) => Math.abs(n - p) === 1)) return false;
        if (quota) {
          const nb = picks.filter((p) => p >= 41).length;
          if ((n >= 41 && nb >= nBig) || (n <= 40 && picks.length - nb >= k - nBig)) return false;
        }
        return true;
      };
      for (const n of cold) { if (picks.length >= nCold) break; if (ok(n, true)) picks.push(n); }
      for (const quota of [true, false]) for (const n of hot) { if (picks.length >= k) break; if (ok(n, quota)) picks.push(n); }
      return picks;
    });
  strategy("outside", "非上期熱門", "四版6、二版8", "近20期熱門、但不在上期 20 個號碼內（四版：三星 2 倍撿零錢）", { N: 20, min_rep: 0 },
    (H, t, P) => {
      if (P.min_rep && H.repCount(t) < P.min_rep) return null;
      const last = H.draws[t - 1].has, c = H.freq(t, P.N);
      return scores((n) => (last[n] ? -1 : c[n]));
    });
  strategy("neighbor", "隔壁號", "四版1、2", "上期號碼的隔壁（±1）且不在上期，兩邊都相鄰的優先", { N: 20 },
    (H, t, P) => {
      const last = H.draws[t - 1].has, c = H.freq(t, P.N);
      return scores((n) => (last[n - 1] + last[n + 1]) * 1000 + (last[n] ? 0 : 100) + c[n]);
    });
  strategy("cold", "10期未開", "二版7", "遺漏 10 期以上的號碼，越久沒開越優先", {}, (H, t) => H.gaps(t));
  function mulberry32(a) { return function () { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
  function sample(rnd, k) { const a = NUMS.slice(); for (let i = 0; i < k; i++) { const j = i + Math.floor(rnd() * (80 - i)); [a[i], a[j]] = [a[j], a[i]]; } return a.slice(0, k); }
  strategy("qp_nb", "電選隔壁", "四版1", "電腦隨機選號後，改買每個號碼的隔壁號（+1）", { seed: 0 },
    (H, t, P, k) => sample(mulberry32(t * 7919 + P.seed), k).map((n) => (n % 80) + 1));
  strategy("random", "電選(隨機)", "對照組", "純隨機＝電腦選號，用來比較策略有沒有比亂選好", { seed: 1 },
    (H, t, P, k) => sample(mulberry32(t * 7919 + P.seed), k));
  function rank01(sc) {
    const v = (n) => sc[n] || 0;
    const order = NUMS.slice().sort((a, b) => (v(a) - v(b)) || (a - b));
    const out = new Float64Array(81); let i = 0;
    while (i < 80) {
      let j = i; const x = v(order[i]);
      while (j + 1 < 80 && v(order[j + 1]) === x) j++;
      for (let q = i; q <= j; q++) out[order[q]] = (i + j) / 2 / 79;
      i = j + 1;
    }
    return out;
  }
  strategy("mix", "綜合加權", "整合", "連莊、熱門、隔壁、冷尾數、10期未開的排名加權投票",
    { weights: { repeat: 1, hot: 1, neighbor: 0.5, tail: 0.5, cold: 0.5 } }, (H, t, P, k) => {
      const tot = new Float64Array(81);
      for (const [key, w] of Object.entries(P.weights)) {
        if (!w) continue;
        const st = STRATS[key]; let res = st.fn(H, t, st.params, k);
        if (res == null) continue;
        if (Array.isArray(res)) { const sc = new Float64Array(81); res.forEach((n, i) => { sc[n] = res.length - i; }); res = sc; }
        const r = rank01(res);
        for (let n = 1; n <= 80; n++) tot[n] += w * r[n];
      }
      return tot;
    });
  strategy("super", "超級獎號", "三版1~7", "近45期超級獎號：排除上期與±2、距離>45；依近3期大小平衡選邊；重複開過的優先", { N: 45, near: 2, max_dist: 45 },
    (H, t, P) => {
      const sup = [];
      for (let i = Math.max(0, t - P.N); i < t; i++) if (H.draws[i].sup) sup.push([i, H.draws[i].sup]);
      if (!sup.length) return null;
      const last = sup[sup.length - 1][1];
      const cnt = new Int32Array(81), seen = new Int32Array(81).fill(-1);
      for (const [i, s] of sup) { cnt[s]++; seen[s] = i; }
      const r3 = sup.slice(-3).map((x) => x[1]);
      const wantBig = r3.filter((s) => s >= 41).length * 2 < r3.length;
      const ok = NUMS.filter((n) => { const d = Math.abs(n - last); return P.near < d && d <= P.max_dist && (n >= 41) === wantBig; });
      let first = ok.filter((n) => cnt[n] >= 2);
      if (!first.length) first = ok.filter((n) => cnt[n] === 0);
      first.sort((a, b) => (cnt[b] - cnt[a]) || (seen[b] - seen[a]) || (a - b));
      const fs = new Set(first);
      return first.concat(ok.filter((n) => !fs.has(n)).sort((a, b) => (cnt[b] - cnt[a]) || (a - b)));
    });
  const PICK_KEYS = ["repeat", "hot", "direct", "tail", "combo", "outside", "neighbor", "cold", "qp_nb", "mix"];

  function pick(H, t, key, k) {
    const st = STRATS[key];
    const res = st.fn(H, t, st.params, k);
    if (res == null) return null;
    const rec = H.freq(t, 10);
    if (Array.isArray(res)) {
      const out = [...new Set(res)].slice(0, k);
      if (out.length < k) for (const n of NUMS.slice().sort((a, b) => (rec[b] - rec[a]) || (a - b))) { if (out.length >= k) break; if (!out.includes(n)) out.push(n); }
      return out;
    }
    const v = (n) => res[n] || 0;
    return NUMS.slice().sort((a, b) => (v(b) - v(a)) || (rec[b] - rec[a]) || (a - b)).slice(0, k);
  }

  // ───────── 猜大小（一版5、二版大小選） ─────────
  function bsSignal(H, t, balWin = 24, balTh = 0.52, hotN = 20) {
    const votes = [];
    let last = null;
    for (let i = t - 1; i > Math.max(-1, t - 400); i--) if (H.bs[i] !== "－") { last = H.bs[i]; break; }
    if (last) votes.push(["小後接大", last === "小" ? "大" : "小"]);
    let tot = 0, sm = 0;
    for (let i = Math.max(0, t - balWin); i < t; i++) for (const n of H.draws[i].nums) { tot++; if (n <= 40) sm++; }
    if (tot) { const r = sm / tot; if (r >= balTh) votes.push(["2小時平衡", "大"]); else if (r <= 1 - balTh) votes.push(["2小時平衡", "小"]); }
    const c = H.freq(t, hotN);
    const b = NUMS.slice().sort((x, y) => (c[y] - c[x]) || (x - y)).slice(0, 10).filter((n) => n >= 41).length;
    if (b !== 5) votes.push(["前十熱號", b > 5 ? "大" : "小"]);
    let pb = 0; for (const n of H.draws[t - 1].nums) if (n >= 41) pb++;
    if (pb !== 10) votes.push(["上期反向", pb > 10 ? "小" : "大"]);
    const nb = votes.filter((v) => v[1] === "大").length, ns = votes.length - nb;
    const dir = nb > ns ? "大" : ns > nb ? "小" : (votes.length ? votes[0][1] : "－");
    let drought = 0;
    for (let i = t - 1; i >= 0; i--) { if (H.bs[i] !== "－") break; drought++; }
    const slot = H.sameSlot(t);
    return { dir, votes, drought, slot, slotOk: !slot.length || slot.some((x) => x !== "－") };
  }

  // ───────── 機率／獎金／加碼 ─────────
  function comb(n, k) { if (k < 0 || k > n) return 0; let r = 1; for (let i = 1; i <= k; i++) r = (r * (n - k + i)) / i; return r; }
  function hg(N, K, n, x) { if (x < 0 || x > n || x > K || n - x > N - K) return 0; return (comb(K, x) * comb(N - K, n - x)) / comb(N, n); }
  function table(k, bonus) { return Object.assign({}, PAYOUT[k], bonus ? BONUS_BASIC[k] || {} : {}); }
  function theory(k, bonus = false) {
    const tb = table(k, bonus); let pwin = 0, ev = 0;
    for (const h of Object.keys(tb)) { const p = hg(80, 20, k, +h); pwin += p; ev += p * tb[h]; }
    return [pwin, ev / BET];
  }
  let P_BIG = 0; for (let x = 13; x <= 20; x++) P_BIG += hg(80, 40, 20, x);
  function pTailGe5() {
    let poly = [1n]; const term = [0, 1, 2, 3, 4].map((j) => BigInt(Math.round(comb(8, j))));
    for (let r = 0; r < 10; r++) {
      const nw = new Array(poly.length + 4).fill(0n);
      poly.forEach((a, i) => term.forEach((b, j) => { nw[i + j] += a * b; }));
      poly = nw;
    }
    let c8020 = 1n; for (let i = 1n; i <= 20n; i++) c8020 = (c8020 * (60n + i)) / i;
    return 1 - Number(poly[20]) / Number(c8020);
  }
  const promosOn = (d) => (d ? PROMOS.filter((p) => p.start <= d && d <= p.end) : []);
  function prize(k, h, d) {
    const base = PAYOUT[k][h] || 0;
    if (cfg.bonus === "off") return base;
    if (cfg.bonus === "on") return (BONUS_BASIC[k] || {})[h] || base;
    let v = base;
    for (const p of promosOn(d)) if (p.basic) v = (p.basic[k] || {})[h] || v;
    return v;
  }
  function superPrize(d) {
    if (cfg.bonus !== "auto") return cfg.bonus === "on" ? SUPER_BONUS : SUPER_PAYOUT;
    return Math.max(SUPER_PAYOUT, ...promosOn(d).map((p) => p.sup || 0));
  }
  function bsPrize(d) {
    if (cfg.bonus !== "auto") return cfg.bonus === "on" ? BS_BONUS : BS_PAYOUT;
    return Math.max(BS_PAYOUT, ...promosOn(d).map((p) => p.bs || 0));
  }
  function promoStatus(d) {
    const now = promosOn(d).map((p) => ({ name: p.name, start: p.start, end: p.end, basic: !!p.basic, sup: p.sup, bs: p.bs }));
    let next = null;
    if (validDate(d)) {
      const lim = addDays(d, 30);
      const c = PROMOS.filter((p) => d < p.start && p.start <= lim).sort((a, b) => (a.start < b.start ? -1 : 1));
      if (c.length) next = { name: c[0].name, start: c[0].start, end: c[0].end, basic: !!c[0].basic, sup: c[0].sup, bs: c[0].bs };
    }
    return { now, next };
  }

  // ───────── 回測 ─────────
  function backtest(H, k, keys, last = 2000, warmup = 60) {
    const t0 = Math.max(warmup, H.T - last), n = H.T - t0;
    if (n <= 0) return null;
    const acc = {}; for (const key of keys) acc[key] = { dist: {}, m: 0, paid: 0, wins: 0 };
    const sup = { super: 0, random: 0 }; let supN = 0, bsOk = 0, bsN = 0, nb = 0, ns = 0;
    for (let t = t0; t < H.T; t++) {
      const D = H.draws[t];
      if (promosOn(D.date).some((p) => p.basic)) nb++;
      if (promosOn(D.date).some((p) => p.sup)) ns++;
      for (const key of keys) {
        const ps = pick(H, t, key, k);
        if (!ps) continue;
        let h = 0; for (const x of ps) h += D.has[x];
        const v = prize(k, h, D.date), a = acc[key];
        a.dist[h] = (a.dist[h] || 0) + 1; a.m++; a.paid += v; a.wins += v > 0 ? 1 : 0;
      }
      if (D.sup) {
        supN++;
        for (const key of ["super", "random"]) { const ps = pick(H, t, key, 2); if (ps && ps.includes(D.sup)) sup[key]++; }
      }
      if (H.bs[t] !== "－") { const d = bsSignal(H, t).dir; if (d !== "－") { bsN++; if (d === H.bs[t]) bsOk++; } }
    }
    const var1 = (k * 0.25 * 0.75 * (80 - k)) / 79;
    const rows = keys.map((key) => {
      const a = acc[key];
      if (!a.m) return { key, name: STRATS[key].name, m: 0 };
      let s = 0; for (const h in a.dist) s += h * a.dist[h];
      const avg = s / a.m;
      return { key, name: STRATS[key].name, m: a.m, avg, z: (avg - k / 4) / Math.sqrt(var1 / a.m), win: a.wins / a.m, roi: a.paid / (BET * a.m), dist: a.dist };
    });
    return { k, t0, n, from: H.draws[t0].term, to: H.draws[H.T - 1].term, theory: theory(k, cfg.bonus === "on"),
      rows, sup: { n: supN, super: supN ? sup.super / supN : 0, random: supN ? sup.random / supN : 0 },
      bs: { n: bsN, acc: bsN ? bsOk / bsN : 0 }, promoDraws: { basic: nb, sup: ns } };
  }

  // ───────── 追號方案 ─────────
  const PLANS = [
    { name: "3星4倍 追5期", src: "一版", kind: "num", k: 3, mult: 4, periods: 5 },
    { name: "3星4倍 追8期", src: "一版", kind: "num", k: 3, mult: 4, periods: 8 },
    { name: "3星4倍 追10期（傳說）", src: "一版", kind: "num", k: 3, mult: 4, periods: 10 },
    { name: "4星3倍 追8期", src: "一版", kind: "num", k: 4, mult: 3, periods: 8 },
    { name: "1星10倍 追4期", src: "一版", kind: "num", k: 1, mult: 10, periods: 4 },
    { name: "2星10倍 追4期", src: "四版9", kind: "num", k: 2, mult: 10, periods: 4 },
    { name: "3星2倍 非上期熱門", src: "四版6", kind: "outside", k: 3, mult: 2, periods: 1 },
    { name: "超級獎號2碼 2倍 追3期", src: "三版6", kind: "super", k: 2, mult: 2, periods: 3 },
    { name: "猜大小6倍 追4期", src: "二版大小4、6", kind: "bs", k: 1, mult: 6, periods: 4 },
  ];
  function runPlan(H, plan, key, t0, stop) {
    const out = []; let t = t0; const rnd = mulberry32(7);
    const { kind, k, mult, periods } = plan;
    while (t + periods <= H.T) {
      let target = null, ps = null;
      if (kind === "bs") {
        const sig = bsSignal(H, t);
        target = key === "random" ? (rnd() < 0.5 ? "大" : "小") : sig.dir;
        if (sig.drought < 15 || !sig.slotOk || target === "－") { t++; continue; }
      } else {
        ps = pick(H, t, key, k);
        if (!ps) { t++; continue; }
      }
      let cost = 0, pay = 0, wins = 0, j = 0;
      for (; j < periods; j++) {
        const i = t + j, D = H.draws[i];
        let p, c;
        if (kind === "bs") { p = H.bs[i] === target ? bsPrize(D.date) : 0; c = BET; }
        else if (kind === "super") { p = ps.includes(D.sup) ? superPrize(D.date) : 0; c = BET * ps.length; }
        else { let h = 0; for (const x of ps) h += D.has[x]; p = prize(k, h, D.date); c = BET; }
        cost += c * mult; pay += p * mult; if (p > 0) wins++;
        if (stop && p > 0) { j++; break; }
      }
      out.push([cost, pay, wins]);
      t += stop ? j : periods;
    }
    return out;
  }
  function plans(H, keys, last = 2000, warmup = 60, stop = false) {
    const t0 = Math.max(warmup, H.T - last), on = cfg.bonus === "on";
    return { t0, n: H.T - t0, from: H.draws[t0] ? H.draws[t0].term : null, to: H.draws[H.T - 1].term, stop, rows: PLANS.map((pl) => {
      let ks, pwin, roi;
      if (pl.kind === "num") { ks = keys; [pwin, roi] = theory(pl.k, on); }
      else if (pl.kind === "outside") { ks = ["outside", "random"]; [pwin, roi] = theory(pl.k, on); }
      else if (pl.kind === "super") { ks = ["super", "random"]; pwin = pl.k / 80; roi = (on ? SUPER_BONUS : SUPER_PAYOUT) / BET / 80; }
      else { ks = ["bs", "random"]; pwin = P_BIG; roi = ((on ? BS_BONUS : BS_PAYOUT) / BET) * P_BIG; }
      const res = ks.map((key) => {
        const r = runPlan(H, pl, key, t0, stop);
        const cost = r.reduce((a, x) => a + x[0], 0), pay = r.reduce((a, x) => a + x[1], 0);
        return { key, name: key === "bs" ? "貼文訊號" : STRATS[key].name, rounds: r.length,
          anyWin: r.length ? mean(r.map((x) => (x[2] > 0 ? 1 : 0))) : 0, profit: r.length ? mean(r.map((x) => (x[1] > x[0] ? 1 : 0))) : 0,
          avgCost: r.length ? cost / r.length : 0, avgPay: r.length ? pay / r.length : 0, roi: cost ? pay / cost : 0 };
      });
      return { plan: pl, res, theory: { anyWin: 1 - Math.pow(1 - pwin, pl.periods), roi } };
    }) };
  }

  // ───────── 貼文說法檢驗 ─────────
  function verify(H) {
    const T = H.T, D = H.draws, R = H.bs, rows = [];
    const row = (src, claim, real, theo) => rows.push({ src, claim, real, theo });
    const pct = (x, d = 1) => (isFinite(x) ? (x * 100).toFixed(d) + "%" : "－");
    const inter = (a, b) => { let c = 0; for (const n of a.nums) c += b.has[n]; return c; };
    const reps = []; for (let i = 1; i < T; i++) reps.push(inter(D[i], D[i - 1]));
    let th35 = 0; for (let x = 3; x <= 5; x++) th35 += hg(80, 20, 20, x);
    row("一版1", "每期會開 3~5 顆上期號碼", `${pct(mean(reps.map((r) => (r >= 3 && r <= 5 ? 1 : 0))))}（平均 ${mean(reps).toFixed(2)} 顆）`, `${pct(th35)}（平均 5.00 顆）`);
    const hh = [];
    for (let i = 20; i < T; i++) { const c = H.freq(i, 20); const top = NUMS.slice().sort((a, b) => (c[b] - c[a]) || (a - b)).slice(0, 10); let h = 0; for (const n of top) h += D[i].has[n]; hh.push(h); }
    let th3 = 0; for (let x = 3; x <= 10; x++) th3 += hg(80, 20, 10, x);
    row("一版2", "近 20 期熱門前十，下期約開 3 顆", `平均 ${mean(hh).toFixed(2)} 顆，≥3 顆 ${pct(mean(hh.map((h) => (h >= 3 ? 1 : 0))))}`, `平均 2.50 顆，≥3 顆 ${pct(th3)}`);
    const trig = []; for (let i = 20; i < T; i++) if (H.repCount(i) <= 1) trig.push(i);
    row("一版3", "上期重複不到 2 顆 → 直攻上期號碼 4 星大部分會中",
      trig.length ? `觸發 ${trig.length} 次，4 星中獎 ${pct(mean(trig.map((i) => { const ps = pick(H, i, "direct", 4); let h = 0; for (const n of ps) h += D[i].has[n]; return h >= 2 ? 1 : 0; })))}` : "期間內沒觸發",
      `4 星中獎 ${pct(theory(4)[0])}`);
    const t5 = D.map((d) => { const c = new Array(10).fill(0); for (const n of d.nums) c[n % 10]++; return Math.max(...c) >= 5 ? 1 : 0; });
    row("一版4", "每小時約 5 次某尾數一次開 5~6 顆", `每小時 ${(12 * mean(t5)).toFixed(1)} 次`, `每小時 ${(12 * pTailGe5()).toFixed(1)} 次`);
    const small = D.map((d) => d.nums.filter((n) => n <= 40).length);
    let blocks = 0, bigN = 0, totN = 0;
    for (let i = 24; i < T - 12; i += 12) {
      let s = 0; for (let j = i - 24; j < i; j++) s += small[j];
      if (s / 480 >= 0.52) { blocks++; for (let j = i; j < i + 12; j++) if (R[j] !== "－") { totN++; if (R[j] === "大") bigN++; } }
    }
    row("一版5", "近 2 小時小號特別多（≥52%）→ 下一小時開大", `${blocks} 次，下一小時大小結果「大」佔 ${totN ? pct(bigN / totN) : "－"}`, "50.0%");
    const afterBig = []; for (let i = 1; i < T; i++) if (20 - small[i - 1] > 10) afterBig.push(20 - small[i]);
    row("二版6", "前一期開大（大號超過 10 顆）→ 下期偏小", `下期平均大號 ${mean(afterBig).toFixed(2)} 顆（${afterBig.length} 次）`, "10.00 顆");
    const cur = new Int32Array(81), cont = {}, base = {};
    for (let i = 0; i < T - 1; i++) {
      for (let n = 1; n <= 80; n++) cur[n] = D[i].has[n] ? cur[n] + 1 : 0;
      for (const n of D[i].nums) { base[cur[n]] = (base[cur[n]] || 0) + 1; cont[cur[n]] = (cont[cur[n]] || 0) + D[i + 1].has[n]; }
    }
    row("二版10", "連莊球號通常不超過 4 次（連開 4 次後第 5 次再開）", `${base[4] ? pct(cont[4] / base[4]) : "－"}（${base[4] || 0} 次）`, "25.0%");
    const seq = R.filter((r) => r !== "－"); const afterSmall = [];
    for (let i = 0; i + 1 < seq.length; i++) if (seq[i] === "小") afterSmall.push(seq[i + 1]);
    row("大小1", "開「小」之後，下一次大小結果是「大」", `${afterSmall.length ? pct(afterSmall.filter((x) => x === "大").length / afterSmall.length) : "－"}（${afterSmall.length} 次）`, "50.0%");
    let hit = 0, n15 = 0, run = 0;
    for (let i = 0; i < T - 2; i++) {
      if (run === 15) { n15++; if (R[i] !== "－" || R[i + 1] !== "－" || R[i + 2] !== "－") hit++; }
      run = R[i] === "－" ? run + 1 : 0;
    }
    row("大小6", "15 期沒開大小 → 接下來 3 期內會開", `${n15 ? pct(hit / n15) : "－"}（${n15} 次）`, pct(1 - Math.pow(1 - 2 * P_BIG, 3)));
    const sup = D.filter((d) => d.sup).map((d) => d.sup);
    if (sup.length > 50) {
      const pairs = []; for (let i = 0; i + 1 < sup.length; i++) pairs.push([sup[i], sup[i + 1]]);
      row("三版3", "超級獎號不會連續開同號", `連開 ${pct(mean(pairs.map(([x, y]) => (x === y ? 1 : 0))), 2)}`, "連開 1.25%");
      row("三版4", "超級獎號前後期距離通常不超過 40~50（超過 45 的比例）", pct(mean(pairs.map(([x, y]) => (Math.abs(x - y) > 45 ? 1 : 0)))), pct(1190 / 6400));
      row("三版5", "超級獎號不太開在上期隔壁（±1~2）", pct(mean(pairs.map(([x, y]) => { const d = Math.abs(x - y); return d >= 1 && d <= 2 ? 1 : 0; }))), pct(314 / 6400));
      const w4 = []; for (let i = 0; i + 3 < sup.length; i++) { const w = sup.slice(i, i + 4); w4.push(w.every((x) => x >= 41) || w.every((x) => x <= 40) ? 1 : 0); }
      row("三版2", "超級獎號大小恰恰（連 4 期全大或全小的比例）", pct(mean(w4)), "12.5%");
      const bal = []; for (let i = 3; i < sup.length; i++) bal.push((sup.slice(i - 3, i).filter((x) => x >= 41).length * 2 < 3) === (sup[i] >= 41) ? 1 : 0);
      row("三版2", "用近 3 期大小平衡猜下一期超級獎號大小", `猜中 ${pct(mean(bal))}`, "50.0%");
      let got = 0, exp = 0;
      for (let i = 45; i < sup.length; i++) {
        const cnt = new Int32Array(81); for (let j = i - 45; j < i; j++) cnt[sup[j]]++;
        let cand = NUMS.filter((n) => cnt[n] >= 2); if (!cand.length) cand = NUMS.filter((n) => cnt[n] === 0);
        if (cand.includes(sup[i])) got++;
        exp += cand.length / 80;
      }
      row("三版1、7", "近 45 期重複開過的超級獎號比較會再開", `命中 ${got} 次／隨機應中 ${exp.toFixed(1)} 次（${(got / exp).toFixed(2)} 倍）`, "1.00 倍");
    }
    return rows;
  }

  // ───────── 回收率（平常 vs 加碼） ─────────
  function evTable() {
    const stars = [];
    for (let k = 1; k <= 10; k++) {
      const [pw, ev0] = theory(k), ev1 = theory(k, true)[1];
      const items = Object.entries(BONUS_BASIC[k] || {}).sort((a, b) => b[0] - a[0]).map(([h, v]) => ({ h: +h, from: PAYOUT[k][h], to: v }));
      stars.push({ k, pwin: pw, ev0, ev1, items });
    }
    const chase = [["3星4倍 追10期", 3, 4, 10], ["3星4倍 追8期", 3, 4, 8], ["3星4倍 追5期", 3, 4, 5], ["4星3倍 追8期", 4, 3, 8], ["1星10倍 追4期", 1, 10, 4], ["2星10倍 追4期", 2, 10, 4]]
      .map(([name, k, m, n]) => {
        const cost = BET * m * n;
        return { name, cost, anyWin: 1 - Math.pow(1 - theory(k)[0], n), top: `中${k}個 ${(PAYOUT[k][k] * m).toLocaleString()}→${(BONUS_BASIC[k][k] * m).toLocaleString()}`,
          back0: cost * theory(k)[1], back1: cost * theory(k, true)[1] };
      });
    chase.push({ name: "超級獎號2碼 2倍 追3期", cost: 300, anyWin: 1 - Math.pow(1 - 2 / 80, 3), top: `${(SUPER_PAYOUT * 2).toLocaleString()}→${(SUPER_BONUS * 2).toLocaleString()}`, back0: 300 * SUPER_PAYOUT / BET / 80, back1: 300 * SUPER_BONUS / BET / 80 });
    chase.push({ name: "猜大小6倍 追4期", cost: 600, anyWin: 1 - Math.pow(1 - P_BIG, 4), top: `${(BS_PAYOUT * 6).toLocaleString()}→${(BS_BONUS * 6).toLocaleString()}`, back0: 600 * BS_PAYOUT / BET * P_BIG, back1: 600 * BS_BONUS / BET * P_BIG });
    return { stars, sup: { p: 1 / 80, ev0: SUPER_PAYOUT / BET / 80, ev1: SUPER_BONUS / BET / 80 }, bs: { p: P_BIG, ev0: BS_PAYOUT / BET * P_BIG, ev1: BS_BONUS / BET * P_BIG }, chase };
  }

  // 範例資料（純隨機模擬）
  function sampleDraws(days = 3, seed = 20261003) {
    const rnd = mulberry32(seed), out = []; let term = 115000001;
    const end = todayTPE();
    for (let i = days - 1; i >= 0; i--) {
      const ds = addDays(end, -i);
      for (let s = 0; s < 203; s++) { const nums = sample(rnd, 20); out.push(mkDraw(term++, ds, nums, nums[19])); }
    }
    return out;
  }

  root.Bingo = { BET, NUMS, PAYOUT, BONUS_BASIC, SUPER_PAYOUT, BS_PAYOUT, SUPER_BONUS, BS_BONUS, PROMOS, cfg, P_BIG,
    mkDraw, bigSmall, oddEven, parseHistory, mergeDraws, toCSV, packDay, unpackDay, Hist, STRATS, ORDER, PICK_KEYS, pick,
    bsSignal, theory, hg, pTailGe5, prize, superPrize, bsPrize, promosOn, promoStatus, backtest, plans, PLANS, verify, evTable,
    sampleDraws, todayTPE, addDays, pad2, mean };
})(typeof module !== "undefined" ? module.exports : window);
