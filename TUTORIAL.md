# Threads AI Bot 完整建置教學
## 帳號：insurance_vision_

---

## 一、系統概覽

這是一個部署在 Railway 上的 Python 機器人，功能：

1. **每日自動發文**：每天早上 8 點，Claude AI 產生 3 篇草稿發到 Telegram 審核，你直接回覆「1」「2」「3」發文，「跳過」略過
2. **留言自動回覆**：輪詢自己貼文的留言，AI 產生回覆後送 Telegram 審核，你直接回覆任意文字發送，「跳過」略過
3. **主動海巡（Proactive Patrol）**：用 Playwright 瀏覽器自動搜尋保險相關貼文，AI 判斷是否適合回覆，適合則透過 Telegram 通知你，不適合直接略過

---

## 二、架構圖

```
┌─────────────────────────────────────┐
│           Railway (Docker)          │
│                                     │
│  FastAPI app (main.py)              │
│  ├── APScheduler (排程器)           │
│  │   ├── 08:00 每日草稿任務         │
│  │   ├── 每 2 分鐘 留言輪詢         │
│  │   └── 每 15 分鐘 海巡任務        │
│  ├── Playwright (Chromium headless) │
│  │   └── 搜尋 Threads + UI 回覆    │
│  └── Threads Graph API client       │
│      └── 自己的貼文/留言 CRUD       │
└─────────────────────────────────────┘
          ↕ Telegram Bot API
┌─────────────────────────────────────┐
│        你的手機 (Telegram)          │
│  - 收到草稿審核通知                  │
│  - 直接回覆訊息控制發文              │
│  - 收到海巡回覆通知                  │
└─────────────────────────────────────┘
          ↕ Anthropic API
┌─────────────────────────────────────┐
│         Claude claude-sonnet-4-6    │
│  - 產生每日主題                      │
│  - 產生發文草稿                      │
│  - 判斷海巡貼文是否適合回覆          │
│  - 生成回覆內容                      │
└─────────────────────────────────────┘
```

---

## 三、檔案結構

```
threads_bot_complete/
├── main.py              # FastAPI 主程式、排程、Telegram webhook
├── ai_generator.py      # Claude AI 內容生成（最重要的檔案）
├── threads_client.py    # Threads Graph API 封裝
├── threads_scraper.py   # Playwright 瀏覽器操作（海巡）
├── notifier.py          # Telegram 推播通知
├── requirements.txt     # Python 套件
├── Dockerfile           # Docker 建構設定
├── railway.toml         # Railway 部署設定
└── .env.example         # 環境變數範例
```

---

## 四、從零開始建置步驟

### 步驟 1：取得 Threads Graph API 權限

1. 前往 [Meta Developers](https://developers.facebook.com) 建立 App
2. 加入「Threads API」產品
3. 取得以下資訊：
   - `THREADS_APP_ID`
   - `THREADS_APP_SECRET`
   - `THREADS_USER_ID`（你的 Threads 用戶 ID）
4. 用 OAuth 授權流程取得長效 `access_token`（長效 token 有效期 60 天，系統會每 2 個月自動刷新）

**取得 access_token 的指令：**
```bash
# 先取得短效 token
curl "https://graph.threads.net/oauth/access_token" \
  -d "client_id=你的APP_ID" \
  -d "client_secret=你的APP_SECRET" \
  -d "grant_type=authorization_code" \
  -d "redirect_uri=你的REDIRECT_URI" \
  -d "code=授權碼"

# 換成長效 token
curl "https://graph.threads.net/access_token?grant_type=th_exchange_token&client_secret=你的APP_SECRET&access_token=短效TOKEN"
```

### 步驟 2：建立 Telegram Bot

1. 在 Telegram 找 [@BotFather](https://t.me/BotFather)
2. 輸入 `/newbot`，取得 `BOT_TOKEN`
3. 隨便傳一則訊息給 bot，然後用以下網址查你的 `chat_id`：
   ```
   https://api.telegram.org/bot你的BOT_TOKEN/getUpdates
   ```
   找到 `message.chat.id` 那個數字就是你的 `TELEGRAM_CHAT_ID`

### 步驟 3：取得 Anthropic API Key

1. 前往 [console.anthropic.com](https://console.anthropic.com)
2. 建立 API Key，記下來作為 `ANTHROPIC_API_KEY`

### 步驟 4：取得 Threads 登入 Cookie（海巡用）

海巡功能用 Playwright 模擬瀏覽器登入 Threads，需要你的 cookie：

1. 電腦開 Chrome，登入 [threads.net](https://threads.net)
2. 開發者工具 → Application → Cookies → threads.net
3. 找到並複製以下 cookie（JSON 格式）：
   - `sessionid`
   - `mid`
   - `ig_did`
   - `csrftoken`
4. 格式如下，存為 `THREADS_COOKIES` 環境變數：
```json
[{"name":"sessionid","value":"你的值","domain":".threads.net"},{"name":"mid","value":"你的值","domain":".threads.net"},{"name":"csrftoken","value":"你的值","domain":".threads.net"}]
```

### 步驟 5：部署到 Railway

1. Fork 或上傳專案到 GitHub
2. 前往 [railway.app](https://railway.app) 建立新專案，連接 GitHub repo
3. 設定環境變數（Variables）：

| 變數名稱 | 說明 |
|---------|------|
| `THREADS_ACCESS_TOKEN` | Threads 長效 token |
| `THREADS_APP_ID` | Threads App ID |
| `THREADS_APP_SECRET` | Threads App Secret |
| `THREADS_USER_ID` | 你的 Threads 用戶 ID |
| `THREADS_USERNAME` | 你的 Threads 帳號（如 `insurance_vision_`） |
| `THREADS_COOKIES` | 瀏覽器 cookie JSON 字串 |
| `ANTHROPIC_API_KEY` | Claude API Key |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | 你的 Telegram Chat ID |
| `PLAYWRIGHT_BROWSERS_PATH` | 固定設為 `/app/.playwright-browsers` |

4. Railway 會自動用 Dockerfile 建構並部署

### 步驟 6：設定 Telegram Webhook

部署完成後，取得你的 Railway 網址（如 `https://your-app.railway.app`），執行：

```bash
curl "https://api.telegram.org/bot你的BOT_TOKEN/setWebhook?url=https://your-app.railway.app/webhook/telegram"
```

確認回傳 `{"ok":true}` 表示設定成功。

---

## 五、核心功能說明

### 5.1 每日發文流程

```
每天 08:00 (台北時間)
  → generate_daily_topics()  產生 5 個保險主題
  → generate_post_drafts()   產生 3 篇發文草稿
  → Telegram 推播審核通知（附 job_id）
  → 你直接回覆訊息「1」「2」「3」
  → 發文到 Threads
```

**Telegram 指令（直接回覆審核訊息）：**
- `1` / `2` / `3`：選擇並發出對應草稿
- `跳過`：略過今日發文

### 5.2 留言回覆流程

```
每 2 分鐘輪詢
  → 抓取最近 5 篇貼文的留言
  → 跳過已處理 / 自己已回覆過的
  → generate_reply()  AI 生成回覆
  → Telegram 推播審核通知
  → 你直接回覆訊息任意文字（確認發送）
  → 透過 Threads Graph API 回覆
```

**Telegram 指令（直接回覆審核訊息）：**
- 任意文字：確認發出回覆
- `跳過` / `略過`：略過此則留言

### 5.3 海巡（Proactive Patrol）流程

```
每 15 分鐘（時段內）
  → 從關鍵字清單隨機抽一個
  → Playwright 開瀏覽器搜尋 threads.net
  → 找到相關貼文（排除已處理的）
  → generate_proactive_reply()  AI 判斷是否適合回覆 + 生成內容
  → 適合：Playwright 直接在貼文下方 UI 回覆
  → 透過 Telegram 通知你（已直接發出）
  → 不適合：靜默略過
```

**海巡時間表：**
| 時段 | 時間 | 每日配額 |
|------|------|---------|
| morning | 07:00-09:00 | 6 則 |
| noon | 12:00-13:00 | 7 則 |
| evening | 20:00-23:00 | 8 則 |

**Telegram 指令（直接傳訊息）：**
- `海巡暫停`：停止海巡和留言輪詢
- `海巡繼續`：恢復海巡和留言輪詢

**搜尋關鍵字清單：**
保險、壽險、醫療險、遺產稅、節稅、保費、保單、理賠、投資型保單、重大傷病、實支實付、保障規劃、退休規劃、保險稅務

---

## 六、AI 內容策略（ai_generator.py）

### 6.1 帳號人設（ACCOUNT_PERSONA）

你是「保險視界」的經營者，懂科技、懂保險的保險經紀人。

**鐵則（任何內容都不得違反）：**

1. **險種中立原則**：不評價任何險種的好壞、不說排序、先後順序
   - ❌ 禁止：「先買實支實付，意外險保費低最後補」
   - ❌ 禁止：「X 比 Y 重要」「X 最後再補」
   - ✅ 正確：說明每個險種保障的範圍，讓讀者自己判斷

2. **投資型保單正確認識**：
   - 最大優勢是壽險成本極低（自然費率）
   - ❌ 絕對不說「保險歸保險、投資歸投資」
   - ❌ 不說「有錢人才能用」「保障缺口填滿才考慮」

3. **選業務 vs 選公司**：
   - 選好業務才是最關鍵
   - 理賠基礎是條款內容，不是公司財務大小
   - ❌ 不以保險公司財務大小作為選擇依據

4. **重大傷病險正確說法**：
   - 確診給一次性現金，怎麼用由被保人決定
   - 可用於醫療費、護理費、補收入——不限用途
   - ❌ 不說「只能作為收入補貼」

5. **意外險同等重要**：
   - 意外險不是「保費低所以最後補」的保障
   - ❌ 禁止說「意外險相對不重要」「意外險保費低最後補齊」

### 6.2 Python 層雙重過濾

即使 AI prompt 偶爾跑偏，程式碼層面還有防線：

**META_PHRASES 過濾**（輸出解釋性文字時擋住）：
- 不適合、無法回覆、沒辦法回覆、不建議回覆、此貼文、這篇貼文、理賠細節

**RANKING_PHRASES 過濾**（輸出排序語言時擋住）：
- 最後補齊、最後補充、最後才補、最後再補、不用優先、優先順序、規劃順序
- 意外險相對、意外保費相對、意外險保費低、意外險反而、意外險最後
- 排在前面、排在後面、先把、先補、倒過來、順序倒

### 6.3 不適合回覆的情況

海巡時，以下類型的貼文 AI 會自動略過（輸出空字串）：
- 廣告貼文、與保險完全無關
- 具體理賠細節詢問（哪些收據可報銷）
- 詢問推薦業務員、保經、業務人名
- 「買哪家公司比較好」「新光還是富邦」等公司比較
- 「這份規劃推薦嗎」「這樣的保單好嗎」等要求評價特定商品
- 「第一張保單買什麼」「怎麼規劃保險」等規劃建議類貼文（無法回答而不給排序）

---

## 七、Admin API 端點

部署後可透過以下 API 手動觸發（需加 Bearer Token 或直接用 Railway 的 Internal URL）：

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/admin/trigger-draft` | 手動觸發今日草稿生成 |
| POST | `/admin/trigger-poll` | 手動觸發留言輪詢 |
| POST | `/admin/trigger-patrol` | 手動觸發海巡（非阻塞，結果透過 TG 通知）|
| GET | `/admin/patrol-stats` | 查看當日海巡統計 |
| GET | `/admin/env-check` | 確認環境變數是否正確設定 |
| GET | `/admin/test-search?keyword=保險` | 測試 Playwright 搜尋功能 |
| GET | `/admin/test-login` | 測試 Playwright 登入狀態 |
| GET | `/admin/debug-screenshot` | 查看最新 debug 截圖 |

---

## 八、Dockerfile 說明

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright-browsers
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

重點：使用 Microsoft 的 Playwright Docker image，已預裝 Chromium 所需的系統相依套件。

---

## 九、資料持久化

Railway 重新部署後 in-memory 狀態會消失，系統有以下持久化機制：

| 檔案 | 內容 | 說明 |
|------|------|------|
| `pending_jobs.json` | 待審核草稿任務 | 重啟後仍可透過 TG 回覆訊息審核 |
| `pending_replies.json` | 待審核留言回覆任務 | 重啟後仍可審核 |
| `processed_reply_ids.json` | 已處理的留言 ID | 防止重複回覆 |

**TG 訊息 fallback 機制**：
- 審核通知訊息末尾附有 `[job:abc123]` 或 `[reply_job:abc123]`
- 即使系統重啟，直接回覆 TG 訊息時，程式會從訊息文字解析 job_id

---

## 十、常見問題排查

### Q: 海巡後 Telegram 沒收到通知？
- 確認 `PLAYWRIGHT_BROWSERS_PATH` 設為 `/app/.playwright-browsers`
- 確認 `THREADS_COOKIES` 格式正確（JSON array 字串）
- 查看 Railway logs：`/admin/test-login` 確認登入狀態

### Q: AI 回覆出現排序語言？
- 查看 Railway logs 確認 Python 過濾層是否有作用
- 觸發格式：`[海巡] 過濾排序語言: "意外險最後補齊..."` 表示過濾成功

### Q: TG 直接回覆沒有反應？
- 確認 webhook 是否設定正確：`https://api.telegram.org/bot你的TOKEN/getWebhookInfo`
- 確認你在 TG 是「直接回覆」那則審核通知訊息（不是新訊息）

### Q: Threads API 403 錯誤？
- Access token 可能過期，執行 `/admin/trigger-draft` 看 logs
- token 刷新排程是每 2 個月自動執行，也可手動刷新

### Q: 留言輪詢沒反應？
- `poll_replies` job 預設被暫停，需在 `main.py` lifespan 啟用，或傳 TG「海巡繼續」恢復

---

## 十一、更新部署流程

1. 修改程式碼（在本地編輯）
2. `git add . && git commit -m "更新說明"`
3. `git push origin main`
4. Railway 偵測到 push 自動重新部署（約 3-5 分鐘）
5. 部署完成後 Railway logs 會顯示 `Scheduler 啟動`

---

## 十二、費用估算（每月）

| 服務 | 費用 |
|------|------|
| Railway（Hobby 方案）| ~US$5 |
| Anthropic API（claude-sonnet-4-6）| 約 US$2-5（視海巡頻率而定）|
| Telegram Bot | 免費 |
| Threads Graph API | 免費 |
| **合計** | **約 US$7-10 / 月** |

---

*文件生成日期：2026-04-19*
*帳號：insurance_vision_*
