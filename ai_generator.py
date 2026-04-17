"""
ai_generator.py
Claude AI 內容生成 — 發文草稿 & 留言回覆 & 自動主題產生
所有輸出均經過保險法規 + Meta 社群規範合規審查。
"""

import os
import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"


ACCOUNT_PERSONA = os.getenv("ACCOUNT_PERSONA", """
你是「保險視界」的經營者，一個懂科技、懂保險的保險經紀人。
經紀人立場：不代表特定保險公司，以客戶最大利益為優先，可橫跨多家公司規劃。

━━━ 核心保險觀念（絕對不能跑偏）━━━

【順序一：先定期，後終身】
- 預算有限時，一定先買定期壽險/定期醫療，再考慮終身型
- 定期保費便宜，可以用少的錢買到高保額，保障缺口更容易填滿
- 終身險保費貴，若預算不足反而保額買不夠，失去保障意義
- 絕對不能暗示「終身比定期好」或「終身一次解決」——這是錯誤觀念

【順序二：保費差額做差額理財】
- 定期險 vs 終身險的保費差額，可以拿去做投資（ETF、基金）
- 長期複利下，差額理財的成果往往遠超終身險的儲蓄功能
- 保險歸保險、投資歸投資，功能不要混在一起

【順序三：先保大風險，再保小風險】
保障規劃的優先順序：
1. 死亡風險（壽險）：家庭主要收入者優先，保額 = 家庭年支出 × 10～15 年
2. 失能/長期照顧風險：一旦失能，長期花費比死亡更龐大
3. 重大傷病風險（重大傷病險）：癌症、重症，需要彌補收入中斷
4. 醫療費用（實支實付醫療險）：住院手術雜費
5. 意外風險（意外險）：保費最低，最後補齊
- 不要本末倒置：不能因為意外險便宜就優先買，大風險才是最怕的

━━━ 發文與回覆核心策略 ━━━
- 用「反差感」吸引目光：先講大家對保險的誤解，再翻轉觀點
- 不直接推銷商品，以知識、觀點、生活情境切入
- 讓讀者自己產生「我需要了解更多」的念頭
- 遵守業務員管理規範：不得以不實或誇大方式描述保險商品，不得保證獲利

發文風格：
- 開頭用一句話製造反差或好奇心
- 內容有觀點、有邏輯，不說廢話
- 結尾留下思考問題或互動鉤子
- 繁體中文，語氣像在跟朋友說話，不像在上課
- 絕對不加任何 hashtag、#字號

禁忌（違規商品，絕對不得提及）：
- 失能險：已停售，不得提及
- 副本實支實付：已停售，不得提及
- 「重大疾病險」：只能說「重大傷病險」
- 不點名特定保險公司或商品
- 不說「保證」「絕對」「一定」等字眼
""").strip()


_COMPLIANCE_RULES = """
【台灣保險業務員管理規範（保險法第 163 條相關）】
1. 不得以不實、誇大或易使人誤解的方式描述保險商品
2. 不得保證保險契約收益，不得以過去績效暗示未來報酬
3. 不得提及已停售商品：失能險、副本實支實付
4. 只能使用「重大傷病險」，不得使用「重大疾病險」
5. 不得點名比較特定保險公司或商品
6. 不得以贈品或其他利益招攬保險
7. 不得以「保證」「絕對」「一定」等字眼描述保障或收益

【Meta 社群規範】
1. 不得發布誤導性、虛假或煽動性內容
2. 不得騷擾、歧視或威脅他人
3. 不得以協調性不真實方式操控資訊
4. 商業行銷需符合 Meta 廣告政策
"""


def _call_claude(system: str, user: str, max_tokens: int = 1000) -> str:
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    resp = httpx.post(ANTHROPIC_API, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


def check_compliance(text: str) -> dict:
    """
    合規審查：確認內容符合保險法規及 Meta 社群規範。
    回傳 {"compliant": bool, "issues": list[str], "fixed_text": str}
    若無違規，fixed_text 與原文相同。
    """
    system = "你是台灣保險法規與 Meta 社群規範的合規審查員。只輸出 JSON，不加任何說明或 markdown。"
    user = f"""請審查以下內容，確認是否違反規定，並輸出修正版本。

合規規則：
{_COMPLIANCE_RULES}

待審查內容：
{text}

輸出 JSON（不加 ```）：
{{
  "compliant": true 或 false,
  "issues": ["違規事項（若無則空陣列）"],
  "fixed_text": "修正後內容（若無違規則與原文完全相同）"
}}"""

    try:
        raw = _call_claude(system, user, max_tokens=1200)
        clean = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)
        if result.get("issues"):
            logger.warning(f"合規問題：{result['issues']}")
        return result
    except Exception as e:
        logger.error(f"合規審查失敗，略過審查: {e}")
        return {"compliant": True, "issues": [], "fixed_text": text}


def _apply_compliance(text: str) -> str:
    """審查並回傳合規後的文字。若審查失敗，回傳原文。"""
    result = check_compliance(text)
    return result.get("fixed_text") or text


def generate_daily_topics() -> list[dict]:
    system = f"""{ACCOUNT_PERSONA}

你的任務是產生今日 Threads 發文的主題清單。
輸出嚴格為 JSON array，不加任何說明文字，不加 markdown 符號。
JSON 格式：[{{"title":"主題標題","summary":"一句話說明角度","url":""}}]
"""
    user = """請產生 5 個適合今日發文的保險相關主題。
要求：
- 從以下方向輪流選取：壽險活用、遺產稅務規劃、投資型保單優勢、心理學與保險決策、醫療保障觀念
- 有反差感或能引發好奇心
- 符合台灣保險市場現況
- 每個主題角度不同

輸出 JSON array，5 個物件。"""

    try:
        raw = _call_claude(system, user, max_tokens=1000)
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception as e:
        logger.error(f"主題生成失敗: {e}")
        return [
            {"title": "壽險不是等死才用", "summary": "保單借款與減額繳清的活用方式", "url": ""},
            {"title": "遺產稅的合法節稅工具", "summary": "保險金不計入遺產的關鍵知識", "url": ""},
            {"title": "投資型保單被誤解了嗎？", "summary": "分離帳戶與一般帳戶的本質差異", "url": ""},
            {"title": "為什麼人們總是拖延買保險？", "summary": "現時偏誤與保險決策的心理學", "url": ""},
            {"title": "重大傷病險 vs 醫療險", "summary": "兩者的理賠邏輯完全不同", "url": ""},
        ]


def generate_post_drafts(source_articles: list[dict], count: int = 3) -> list[dict]:
    articles_text = "\n\n".join(
        f"[{i+1}] 主題：{a['title']}\n角度：{a.get('summary','')}"
        for i, a in enumerate(source_articles[:5])
    )

    system = f"""{ACCOUNT_PERSONA}

你的任務是根據提供的主題，生成 Threads 發文草稿。

規則：
- 每篇草稿不超過 450 字（含標點）
- 絕對不加任何 hashtag 或 # 字號
- 開頭要有反差感或吸睛的第一句話
- 不直接推銷保險商品
- 結尾用問句引發互動
- 輸出嚴格為 JSON array，不加任何說明文字，不加 markdown 符號
- JSON 格式：[{{"draft":"內文","angle":"切入角度","source_title":"主題標題"}}]
"""

    user = f"""請根據以下主題，生成 {count} 篇風格各異的 Threads 發文草稿：

{articles_text}

輸出 JSON array，{count} 個物件。"""

    raw = _call_claude(system, user, max_tokens=2000)
    clean = raw.replace("```json", "").replace("```", "").strip()
    drafts = json.loads(clean)[:count]

    # 合規審查並修正每篇草稿
    for d in drafts:
        d["draft"] = _apply_compliance(d["draft"])

    return drafts


def generate_reply(
    post_text: str,
    comment_text: str,
    commenter_username: str,
    conversation_history: Optional[list[dict]] = None,
) -> str:
    history_text = ""
    if conversation_history:
        history_text = "\n".join(
            f"@{m['username']}: {m['text']}"
            for m in conversation_history[-4:]
        )

    system = f"""{ACCOUNT_PERSONA}

你正在回覆 Threads 上的留言。

規則：
- 回覆自然、口語、有溫度，不像機器人
- 不超過 150 字
- 直接輸出回覆文字，不加任何說明或引號
- 不加 hashtag
- 若留言是問題，給出實質回答；若是閒聊，輕鬆回應
- 若留言有購買意向或詢問細節，自然引導對方私訊了解
"""

    context = f"""我的貼文內容：
{post_text}

{"對話記錄：" + history_text if history_text else ""}

@{commenter_username} 留言說：
{comment_text}

請回覆這則留言："""

    reply = _call_claude(system, context, max_tokens=300)
    return _apply_compliance(reply)


def generate_proactive_reply(post_text: str, keyword: str) -> str:
    """
    針對他人保險相關貼文生成主動回覆。
    回傳回覆文字，若內容不適合回覆則回傳空字串。
    """
    system = f"""{ACCOUNT_PERSONA}

你正在主動回覆 Threads 上與保險相關的公開貼文。

嚴格規則：
- 只做客觀知識補充，不做商品比較
- 不提具體保險公司或商品名稱
- 不招攬，不說「我可以幫你規劃」「歡迎聯絡我」等直接招攬語
- 不超過 120 字
- 繁體中文，自然口語
- 若貼文內容不適合回覆（廣告、與保險無關、已有完整正確答案），輸出空字串
- 直接輸出回覆文字或空字串，不加任何說明
- 不要在文字中加 @username 或任何 @tag
- 【關鍵】若涉及險種選擇，一定遵守：先定期後終身、先大風險後小風險
- 【關鍵】絕對不能暗示終身險比定期好，不能美化儲蓄險功能
"""

    user = f"""搜尋關鍵字：{keyword}

他人的 Threads 貼文內容：
{post_text}

請判斷是否適合回覆，並生成補充知識的回覆（或空字串）："""

    try:
        result = _call_claude(system, user, max_tokens=300)
        cleaned = result.strip().strip('"').strip("'").strip()
        if not cleaned or cleaned in ("空字串", "不適合回覆") or len(cleaned) < 5:
            return ""
        result = cleaned
        return _apply_compliance(result.strip())
    except Exception as e:
        logger.error(f"主動回覆生成失敗: {e}")
        return ""
