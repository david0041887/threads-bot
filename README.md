# Threads AI Bot — 部署說明

## 專案結構

```
threads-ai-bot/
├── main.py              # FastAPI 主程式（Webhook + 排程 + 審核流程）
├── threads_client.py    # Threads API 封裝
├── ai_generator.py      # Claude 內容生成（發文草稿 + 留言回覆）
├── content_sources.py   # RSS / NewsAPI 爬取
├── notifier.py          # LINE / Telegram 推播
├── requirements.txt
├── .env.example         # 環境變數範本
└── .env                 # 實際設定（不要 commit）
```

---

## Step 1 — Meta Developer App 申請

1. 前往 https://developers.facebook.com → 建立新 App（選 Other > Business）
2. 新增產品：**Threads API**
3. Settings > Basic → 記下 `App Secret`
4. Threads API → Permissions，申請以下權限：
   - `threads_basic`
   - `threads_content_publish`
   - `threads_manage_replies`
   - `threads_read_replies`
5. 取得你的 **User ID**（數字）：
   ```
   GET https://graph.threads.net/v1.0/me?fields=id,username&access_token=YOUR_TOKEN
   ```
6. 換取 Long-lived token（60 天）：
   ```
   GET https://graph.threads.net/v1.0/access_token
     ?grant_type=th_exchange_token
     &client_id={app-id}
     &client_secret={app-secret}
     &access_token={short-lived-token}
   ```

---

## Step 2 — Telegram Bot 設定

1. 傳訊息給 @BotFather → `/newbot` → 取得 `BOT_TOKEN`
2. 傳任意訊息給你的 bot，再打開：
   ```
   https://api.telegram.org/bot{BOT_TOKEN}/getUpdates
   ```
   取得 `chat_id`（你的數字 ID）
3. 設定 Telegram Webhook（讓 bot 收到你的訊息）：
   ```
   POST https://api.telegram.org/bot{BOT_TOKEN}/setWebhook
   {"url": "https://your-domain.com/webhook/telegram"}
   ```

---

## Step 3 — 本地啟動

```bash
# 安裝依賴
pip install -r requirements.txt

# 設定環境變數
cp .env.example .env
# 編輯 .env 填入所有 token

# 啟動
uvicorn main:app --reload --port 8000

# 用 ngrok 暫時對外（設定 Webhook 時使用）
ngrok http 8000
```

---

## Step 4 — 設定 Threads Webhook

1. 打開 Meta Developer Console → Threads API → Webhooks
2. Callback URL：`https://your-domain.com/webhook/threads`
3. Verify Token：填入 `.env` 的 `WEBHOOK_VERIFY_TOKEN`
4. 訂閱 `replies` 事件
5. 確認後 Meta 會呼叫 GET /webhook/threads 驗證

---

## Step 5 — 部署（Render 免費方案）

1. 新增 `render.yaml`：
   ```yaml
   services:
     - type: web
       name: threads-ai-bot
       env: python
       buildCommand: pip install -r requirements.txt
       startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT
   ```
2. 推上 GitHub → Render 連接 repo
3. 在 Render Dashboard 設定所有環境變數

---

## 每日使用流程

```
08:00  → Bot 自動爬新聞 + 生成 3 篇草稿
         → Telegram 推播給你

你     → 傳「選 2 abc123」確認發文（job_id 在通知裡）
         或傳「跳過 abc123」取消

有人留言 → Webhook 觸發 → AI 自動生成回覆並發出
         → Telegram 通知你（回覆內容 log）
```

---

## 注意事項

- Long-lived Token 每 60 天過期，系統每 50 天自動刷新
- 每日發文上限 250 篇（回覆不計）
- `pending_jobs` 目前用記憶體暫存，重啟後清空
  - 生產環境建議改為 Redis 或 SQLite

---

## 常用指令

```bash
# 手動觸發草稿任務（測試）
curl -X POST http://localhost:8000/admin/trigger-draft

# 查看待處理草稿
curl http://localhost:8000/admin/pending-jobs

# 手動確認發文
curl -X POST http://localhost:8000/approve/{job_id}/1

# 健康檢查
curl http://localhost:8000/health
```
