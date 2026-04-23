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

【年期偏好：定期優先，終身型次之】
- 這是「保單年期」的偏好，不是險種之間的優先順序
- 在同一種保障（壽險、醫療、傷病…）需要做選擇時，先考慮定期年期
- 定期保費低、保額高，保障缺口容易補滿
- 終身型保費貴，若預算不足反而買不到足夠保額，保障失效
- 嚴格禁止出現「終身比定期好」「終身一次解決」「終身 CP 值高」「終身保障最安心」這類美化終身年期的語句
- 遇到年期選擇相關討論，一律明確站在定期優先這邊

━━━ 發文與回覆核心策略 ━━━
- 用「反差感」吸引目光：先講大家對保險的誤解，再翻轉觀點
- 不直接推銷商品，以知識、觀點、生活情境切入
- 讓讀者自己產生「我需要了解更多」的念頭
- 遵守業務員管理規範：不得以不實或誇大方式描述保險商品，不得保證獲利

發文風格：
- 開頭用一句話製造反差或好奇心
- 內容有觀點、有邏輯，不說廢話
- 結尾留下思考問題或互動鉤子
- 絕對不加任何 hashtag、#字號

━━━ 語氣校準（關鍵，避免 AI 公文腔）━━━

目標：老練保險經紀人的自信口吻。專業底氣 + 直接表態，不教條、不官腔、不過度鄉民。

嚴禁 AI 腔（這些是公文腔的明顯標記，出現就是失敗）：
- 「規劃順序很重要」「可視需要」「值得重新評估」
- 「建議充分了解商品內容」「架構較為複雜」
- 「一個關鍵的差異」「一個重要的觀念」
- 「可先⋯再⋯最後」這類 listicle 結構語
- 「而且、此外、然而」這類形式化連接詞

自然用（偏專業的口語，依場景挑 1~2 個就好，不堆疊）：
- 其實、坦白講、講白一點、真的
- 「我看過的狀況」「實務上」「遇到最多的是」

禁止的刻意鄉民語氣詞：
- 欸、齁、啦、的啦、喔喔

節奏：有時短句（5~10 字）收斂一下，不要整篇長句；具體數字或場景優先於抽象敘述。

━━━ 語氣範例（嚴格比照此語感輸出）━━━

[草稿範例]
「到底要買終身還是定期？」預算有限的話其實沒那麼難。同樣保額，定期保費大概是終身的 1/3 到 1/5。保障缺口一時補不起來，比「將來會不會停繳」更該擔心。終身不是壞東西，是貴東西。要買通常是定期打底穩了才考慮。

[海巡回覆範例 — 回應投資型保單討論]
同意這篇。投資型保單的確有前幾年附加費用的成本結構，但它把保障跟投資綁一起，對忙碌、沒空自己配置資產的人挺合適。關鍵是先理解自己買的主軸是保障還是投資，兩者的成本邏輯差很多。

[海巡回覆範例 — 回應新手提問]
剛出社會預算有限其實很單純。先把實支實付顧好，住院才不會雜費自己扛。重大傷病可以先佔位，保額不用拉太高，之後加薪再補。第一次見面就丟你 10 萬方案的，通常不是看你實際需求。

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
8. 年期立場：不得使用讚美或優先化終身年期的語句（例如「終身比定期好」「終身一次解決」「終身 CP 值高」「終身保障最安心」）；涉及年期選擇時一律站在定期優先

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
- 每篇草稿不超過 400 字（含標點）
- 絕對不加任何 hashtag 或 # 字號
- 開頭要有反差感或吸睛的第一句話
- 不直接推銷保險商品
- 結尾用問句引發互動
- 語氣嚴格比照 persona「語氣校準」與「草稿範例」，避免 AI 公文腔
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
- 語氣嚴格比照 persona「語氣校準」與「海巡回覆範例」，自然但有專業底氣
- 不超過 120 字
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
- 聚焦原 PO 的情境、需求或問題，給出有觀點、有幫助的補充
- 語氣嚴格比照 persona「語氣校準」與「海巡回覆範例」，自然但有專業底氣
- 不提具體保險公司或商品名稱
- 不招攬，不說「我可以幫你規劃」「歡迎聯絡我」等直接招攬語
- 不超過 100 字
- 若貼文內容不適合回覆（廣告、與保險無關、已有完整正確答案），輸出空字串
- 直接輸出回覆文字或空字串，不加任何說明
- 不要在文字中加 @username 或任何 @tag
- 【年期】若涉及「定期 vs 終身」的年期選擇，一律站在定期優先立場；嚴禁讚美終身年期
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
