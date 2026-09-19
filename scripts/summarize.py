"""Claude API로 자막을 요약하고 언급 종목/키워드를 추출한다."""
import json
import os

from anthropic import Anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
MAX_TRANSCRIPT_CHARS = 15000

SYSTEM_PROMPT = """당신은 한국 주식/미국 주식 관련 유튜브 영상 자막을 분석해 \
바쁜 직장인 투자자를 위한 간결한 요약을 만드는 애널리스트입니다.
반드시 아래 JSON 형식으로만 응답하고, 다른 설명 텍스트는 절대 출력하지 마세요.

{
  "summary_bullets": ["핵심 요약 불릿1", "핵심 요약 불릿2"],
  "tickers": ["언급된 종목명"],
  "keywords": ["언급된 섹터/이슈/매크로 키워드"]
}

규칙:
- summary_bullets는 3~6개, 각 불릿은 한 문장으로 핵심만 담을 것.
- tickers에는 실제 언급된 종목명만 적을 것(예: 삼성전자, 엔비디아, 테슬라). 확실치 않으면 비워둘 것.
- keywords에는 섹터/이슈/매크로 키워드를 적을 것(예: 금리인상, HBM, 반도체 사이클, 엔캐리트레이드).
"""


def summarize_transcript(channel_name, title, transcript_text):
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    transcript_text = transcript_text[:MAX_TRANSCRIPT_CHARS]
    user_prompt = f"채널명: {channel_name}\n영상 제목: {title}\n\n자막:\n{transcript_text}"

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = "".join(block.text for block in response.content if block.type == "text")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"Claude 응답을 JSON으로 파싱할 수 없습니다: {raw[:300]}")
        return json.loads(raw[start : end + 1])
