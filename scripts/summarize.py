"""Google Gemini 무료 API로 자막을 분석해 투자 전략 리포트를 생성한다.
카드 등록 없이 https://aistudio.google.com/apikey 에서 키를 받아 GEMINI_API_KEY로 등록하면 된다.
"""
import json
import os
import time

import requests

MAX_ATTEMPTS = 4
# 429(분당 호출 한도)는 1분이 지나야 풀리므로, 총 85초까지 기다려본다.
# 짧게 포기하면 이미 유료로 가져온 자막이 그대로 버려지기 때문.
RETRY_BACKOFF_SECONDS = [5, 20, 60]
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

# 로그에 대략적인 비용을 찍기 위한 단가 (2026-09 기준, 100만 토큰당 USD)
PRICES_PER_MTOK = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
INPUT_PRICE_PER_MTOK, OUTPUT_PRICE_PER_MTOK = PRICES_PER_MTOK.get(MODEL, (1.50, 9.00))

API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
MAX_TRANSCRIPT_CHARS = 30000

SYSTEM_PROMPT = """[역할 정의]
당신은 수석 월가 투자 전략가(Chief Investment Strategist)이자 경제 분석가입니다.
제공된 영상의 트랜스크립트/내용을 바탕으로, 노이즈는 제거하고 투자 판단에 필요한 핵심 정수만 추출하여 정밀 리포트를 작성해 주세요.

[분석 요구사항]
0. key_summary (한줄 미리보기)
   - 이 영상의 핵심을 1~2문장으로 압축한 미리보기 문장. 카드 목록에서 리포트 본문을 펼치기 전에 가장 먼저 보이는 문장이므로,
     가장 중요한 종목명/수치/결론을 반드시 포함해 임팩트 있게 작성할 것 (예: "**삼성전자** 파운드리 가격 주도권 강화로 4분기
     실적 서프라이즈 기대, **분할 매수** 유효").

1. 🌐 거시경제(Macro) 및 시장 진단
   - 현재 시장의 핵심 인과관계(원인 ➔ 결과) 분석 (예: 금리, 환율, 유가, 통화정책 등)
   - 시장 참여자들이 오해하거나 선반영한 악재/호재 요소 명시
   - 현재 장세의 성격 정리 (예: 주도주 부재 박스권, 순환매 장세, 강세장 등)

2. 📊 섹터 및 종목별 상세 분석
   - [우수/주도 섹터]: 전문가가 강하게 추천하거나 수급이 쏠리는 섹터, 이유, 관련 핵심 종목
   - [관망/주의 섹터]: 조정 가능성이 있거나 리스크가 존재하는 섹터 및 종목
   - 각 종목/섹터별 핵심 모멘텀(실적 성장률, AI 수혜, 정책 수혜 등) 명확히 명시
   - 이 항목들을 leading_picks / watch_picks 배열에도 각각 {sector, tickers} 형태로 구조화해서 중복으로 채울 것

3. 💡 핵심 투자 인사이트 (Key Takeaways)
   - 전문가가 제시하는 시장을 바라보는 뷰(View)의 핵심 3가지
   - 일시적 이슈와 구조적 성장 스토리를 구분하여 설명

4. 🛡️ 투자 전략 및 액션 플랜 (Action Plan)
   - 매수/매도/리스크 관리 관점에서의 구체적인 실행 가이드 (예: 분할 매수 시점, 추격 매수 금지, 투자 경고 관리 등)
   - 다가올 주요 이벤트나 변수(실적 발표, 추석/연말 수급, 정책 변화 등) 및 대응책

[출력 형식 및 가독성]
- Visual Hierarchy(계층 구조)를 적극 활용하여 작성할 것.
- 핵심 키워드, 종목명, 핵심 수치는 **굵은 글씨**로 강조.
- 불필요한 서론/결론 문구는 제외하고 곧바로 리포트 형식으로 작성.

[출력 문법 규칙 - 반드시 준수]
- key_summary 필드는 report_markdown과 별개의 짧은 문자열로, 1~2문장을 넘지 않을 것.
- report_markdown 필드에는 1~4번 리포트만 마크다운으로 작성할 것 (key_summary는 포함하지 말 것).
- 각 대분류(1~4) 제목은 줄 맨 앞에 "## " 를 붙여 마크다운 헤더로 작성 (예: "## 🌐 거시경제(Macro) 및 시장 진단").
- 하위 항목은 "- " 로 시작하는 목록으로 작성.
- 강조할 단어/문장은 반드시 **이렇게** 두 개의 별표로 감쌀 것.
- tickers 필드에는 리포트에서 실제 언급된 종목명만 배열로 별도 추출 (예: ["삼성전자", "엔비디아"]).
- keywords 필드에는 섹터/이슈/매크로 키워드를 배열로 별도 추출 (예: ["금리인상", "HBM", "반도체 사이클"]).
- leading_picks 필드에는 [우수/주도 섹터]에 해당하는 항목들을 {"sector": "섹터명", "tickers": ["종목명", ...]} 형태의 배열로 작성.
- watch_picks 필드에는 [관망/주의 섹터]에 해당하는 항목들을 같은 형태로 작성.
- 둘 다 명확한 항목이 없으면 빈 배열([])로 둘 것 (억지로 만들어내지 말 것).
"""

PICK_ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "sector": {"type": "STRING"},
        "tickers": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["sector", "tickers"],
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "key_summary": {"type": "STRING"},
        "report_markdown": {"type": "STRING"},
        "tickers": {"type": "ARRAY", "items": {"type": "STRING"}},
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "leading_picks": {"type": "ARRAY", "items": PICK_ITEM_SCHEMA},
        "watch_picks": {"type": "ARRAY", "items": PICK_ITEM_SCHEMA},
    },
    "required": ["key_summary", "report_markdown", "tickers", "keywords", "leading_picks", "watch_picks"],
}


def _log_usage(usage, title):
    """영상 한 건당 토큰이 어디서 얼마나 나가는지 로그로 남긴다.
    생각(thinking) 토큰도 출력 요금으로 청구되므로 따로 찍어둬야 비용 원인을 알 수 있다."""
    if not usage:
        return
    prompt = usage.get("promptTokenCount", 0)
    output = usage.get("candidatesTokenCount", 0)
    thoughts = usage.get("thoughtsTokenCount", 0)
    cost = prompt / 1_000_000 * INPUT_PRICE_PER_MTOK + (output + thoughts) / 1_000_000 * OUTPUT_PRICE_PER_MTOK
    print(
        f"[COST] {title[:40]} | 입력 {prompt:,} / 출력 {output:,} / 생각 {thoughts:,} 토큰"
        f" -> 약 ${cost:.4f}"
    )


def _normalize_newlines(result):
    """Gemini가 줄바꿈을 진짜 개행이 아니라 '\\n' 두 글자로 내보내는 경우가 있다.
    그대로 두면 대시보드에서 마크다운이 한 줄로 뭉개져 제목/불릿 구분이 전부 사라진다."""
    report = result.get("report_markdown")
    if isinstance(report, str) and "\\n" in report:
        result["report_markdown"] = report.replace("\\n", "\n")
    return result


def call_gemini(system_prompt, user_prompt, response_schema, label, max_output_tokens=8192):
    """구조화된 JSON 응답을 받아온다. 재시도/비용로그/개행 정규화를 공통으로 처리한다."""
    api_key = os.environ["GEMINI_API_KEY"]

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
            "maxOutputTokens": max_output_tokens,
        },
    }

    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.post(f"{API_URL}?key={api_key}", json=payload, timeout=90)
            resp.raise_for_status()
            data = resp.json()
            _log_usage(data.get("usageMetadata"), label)
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            return _normalize_newlines(json.loads(raw))
        except (requests.exceptions.HTTPError, requests.exceptions.Timeout, json.JSONDecodeError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            transient = isinstance(e, (requests.exceptions.Timeout, json.JSONDecodeError)) or status in RETRYABLE_STATUS_CODES
            last_error = e
            if not transient or attempt == MAX_ATTEMPTS - 1:
                raise
            wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            print(f"[INFO] Gemini transient error ({e}); retrying in {wait}s (attempt {attempt + 1}/{MAX_ATTEMPTS})")
            time.sleep(wait)

    raise last_error


def summarize_transcript(channel_name, title, transcript_text):
    user_prompt = f"채널명: {channel_name}\n영상 제목: {title}\n\n자막:\n{transcript_text[:MAX_TRANSCRIPT_CHARS]}"
    return call_gemini(SYSTEM_PROMPT, user_prompt, RESPONSE_SCHEMA, title)
