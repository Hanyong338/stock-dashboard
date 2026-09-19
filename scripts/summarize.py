"""Google Gemini 무료 API로 자막을 요약하고 언급 종목/키워드를 추출한다.
카드 등록 없이 https://aistudio.google.com/apikey 에서 키를 받아 GEMINI_API_KEY로 등록하면 된다.
"""
import json
import os

import requests

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
MAX_TRANSCRIPT_CHARS = 15000

SYSTEM_PROMPT = """당신은 한국 주식/미국 주식 관련 유튜브 영상 자막을 분석해 \
바쁜 직장인 투자자를 위한 간결한 요약을 만드는 애널리스트입니다.

규칙:
- summary_bullets는 3~6개, 각 불릿은 한 문장으로 핵심만 담을 것.
- tickers에는 실제 언급된 종목명만 적을 것(예: 삼성전자, 엔비디아, 테슬라). 확실치 않으면 비워둘 것.
- keywords에는 섹터/이슈/매크로 키워드를 적을 것(예: 금리인상, HBM, 반도체 사이클, 엔캐리트레이드).
"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary_bullets": {"type": "ARRAY", "items": {"type": "STRING"}},
        "tickers": {"type": "ARRAY", "items": {"type": "STRING"}},
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["summary_bullets", "tickers", "keywords"],
}


def summarize_transcript(channel_name, title, transcript_text):
    api_key = os.environ["GEMINI_API_KEY"]
    transcript_text = transcript_text[:MAX_TRANSCRIPT_CHARS]
    user_prompt = f"채널명: {channel_name}\n영상 제목: {title}\n\n자막:\n{transcript_text}"

    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }

    resp = requests.post(f"{API_URL}?key={api_key}", json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw)
