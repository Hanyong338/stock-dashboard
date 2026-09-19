"""한국경제TV [당신이 잠든 사이에](당잠사) 방송을 분석해 아침 마켓 리포트를 만든다.

채널별 유튜브 요약과는 형식이 완전히 달라서 별도 경로로 처리한다.
결과는 docs/data/morning_brief.json 에 저장되고 대시보드 Market Briefing 탭에 표시된다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from summarize import MAX_TRANSCRIPT_CHARS, call_gemini

# 당잠사 재생목록 (월~금 AM 5:30 생방송). 채널 전체 업로드에는 다른 코너가 섞여 있어서 이 목록만 본다.
PLAYLIST_ID = "PLh6kUo7pqm_69KUuM0hOj-ClLo6GWdgUH"
CHANNEL_ID = "UCF8AeLlUbEpKju6v1H6p8Eg"
CHANNEL_NAME = "한국경제TV"
PROGRAM_NAME = "당신이 잠든 사이에"

SYSTEM_PROMPT = """# Role & Objective
너는 월가 매크로 흐름과 국내 증시 밸류체인 연결에 정통한 **'수석 글로벌 에쿼티 전략가(Equity Strategist)'**야.
제공되는 한국경제TV [당신이 잠든 사이에]의 방송 자막을 분석해서, 매일 아침 투자자가 1분 만에 핵심을 파악할 수 있는
**[🇺🇸 Overnight → 🇰🇷 Korea 마켓 리포트]**를 아래 원칙에 맞춰 정확하게 작성해라.

## 분석 및 작성 원칙

1. **팩트 중심 수치 정리**
   - 3대 지수, SOX(필라델피아 반도체), 미 10년물 금리, 달러 인덱스, WTI 유가 등 핵심 지표 종가를 정확히 기재한다.
   - 경제지표(실업수당, CPI, PPI, 고용 등) 발표가 다뤄졌다면 발표치 / 컨센서스 / 직전치 / 시장 영향을 정리한다.
   - 방송에서 언급되지 않았거나 수치가 불명확하면 반드시 "-" 로 표기한다. 절대 추측해서 지어내지 않는다.

2. **간밤 주요 뉴스 전수 추출 & 한 줄 코멘트 (개수 제한 없음)**
   - 개수를 3~5개로 임의 축약하지 마라.
   - 방송의 모든 코너(글로벌 헤드라인, 마감 시황, 오늘장 특징주, 마켓무버, 이슈 인사이드, 특파원 리포트 등)에서 다뤄진
     정치·지정학·정책·규제·산업 매크로 뉴스를 누락 없이 전부 추출한다.
   - 각 뉴스마다 '핵심 팩트 요약'과 '시장에 미치는 영향/의미를 담은 한 줄 코멘트'를 함께 쓴다.

3. **급등과 급락 동시 포착**
   - 개별 기업 이슈 중 주가가 크게 움직인 종목(대략 ±3~5% 이상) 또는 시장에 중대한 화두를 던진 대형주를 선정한다.
   - 상승 종목만 편향되게 넣지 말고, 투심 악화나 리스크를 경고하는 급락 종목/섹터도 반드시 1개 이상 포함한다.

4. **국내 종목 연결 및 3단계 연관도 표기 (가장 중요)**
   - 근거 없는 찌라시성 잡주 연결을 엄격히 배제하고, 실제 사업/수주/공급망 기반으로 연결한다.
   - 반드시 "왜 연결되는지(연결 로직)"를 1문장으로 명확히 서술한다.
   - 연관도(strength) 기준:
     * 3 = 직접 연관: 직납 벤더, 핵심 부품 공급망, 글로벌 직접 경쟁사
     * 2 = 산업 연관: 동일 산업 CAPEX/인프라 수혜, 장비·소재 밸류체인
     * 1 = 테마 연관: 시장 심리 및 뉴스 모멘텀으로 함께 동조화되는 종목

## 출력 규칙
- 모든 텍스트는 한국어로 쓴다.
- 수치는 방송에서 언급된 값을 그대로 쓰고, 등락률에는 부호(+/-)를 붙인다.
- 강조가 필요한 핵심 수치나 종목명에는 마크다운 **굵게**를 쓴다.
- connections(급등·급락 매칭)는 3~5개 블록을 만든다. direction 은 급등이면 "up", 급락이면 "down" 으로 표기한다.
"""

KOREA_PICK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "name": {"type": "STRING", "description": "국내 종목명"},
        "strength": {"type": "INTEGER", "description": "3=직접 연관, 2=산업 연관, 1=테마 연관"},
        "reason": {"type": "STRING", "description": "구체적 연결 이유 1문장"},
    },
    "required": ["name", "strength", "reason"],
}

CHECKLIST_ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "theme": {"type": "STRING", "description": "테마/섹터명"},
        "us": {"type": "STRING", "description": "관련 미국 종목명 및 등락률"},
        "cause": {"type": "STRING", "description": "원인 요약"},
        "action": {"type": "STRING", "description": "오늘 국내장 대응 전략 및 체크 포인트"},
    },
    "required": ["theme", "us", "cause", "action"],
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "as_of": {"type": "STRING", "description": "예: 2026년 09월 18일 마감 기준"},
        "indices": {
            "type": "ARRAY",
            "description": "나스닥, S&P 500, 다우, 필라델피아 반도체, 미국 10년물 금리, 달러 인덱스, WTI 유가 순서",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "value": {"type": "STRING", "description": "종가/수치. 미언급이면 '-'"},
                    "change": {"type": "STRING", "description": "등락률 또는 등락폭. 부호 포함. 미언급이면 '-'"},
                },
                "required": ["name", "value", "change"],
            },
        },
        "economic_events": {
            "type": "ARRAY",
            "description": "방송에서 다룬 경제지표/이벤트. 없으면 빈 배열",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "actual": {"type": "STRING"},
                    "forecast": {"type": "STRING"},
                    "previous": {"type": "STRING"},
                    "assessment": {"type": "STRING", "description": "[호조/쇼크/중립] 1줄 평가"},
                },
                "required": ["name", "actual", "forecast", "previous", "assessment"],
            },
        },
        "ai_summary": {
            "type": "OBJECT",
            "properties": {
                "us_market": {"type": "STRING", "description": "지수 등락 원인 및 매크로 분위기"},
                "sector_flow": {"type": "STRING", "description": "가장 강했던/약했던 섹터와 원인"},
                "korea_impact": {"type": "STRING", "description": "환율·금리 여건 및 오늘 국내 증시 영향"},
            },
            "required": ["us_market", "sector_flow", "korea_impact"],
        },
        "news": {
            "type": "ARRAY",
            "description": "방송에서 다룬 매크로 뉴스 전부. 개수 제한 없음",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "fact": {"type": "STRING", "description": "핵심 팩트 요약"},
                    "comment": {"type": "STRING", "description": "시장 영향 및 시사점 한 줄"},
                },
                "required": ["title", "fact", "comment"],
            },
        },
        "connections": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "sector": {"type": "STRING", "description": "섹터명"},
                    "direction": {"type": "STRING", "description": "up 또는 down"},
                    "us_name": {"type": "STRING", "description": "미국 종목명"},
                    "us_ticker": {"type": "STRING"},
                    "us_change": {"type": "STRING", "description": "등락률. 부호 포함"},
                    "cause": {"type": "STRING", "description": "주가 등락의 구체적 원인"},
                    "sector_class": {"type": "STRING", "description": "예: HBM / 전력 인프라 / 광통신 / 로봇"},
                    "logic": {"type": "STRING", "description": "미국 기업 움직임 -> 산업 영향 -> 국내 기업 연결 고리"},
                    "korea_picks": {"type": "ARRAY", "items": KOREA_PICK_SCHEMA},
                },
                "required": [
                    "sector", "direction", "us_name", "us_ticker", "us_change",
                    "cause", "sector_class", "logic", "korea_picks",
                ],
            },
        },
        "checklist_caution": {"type": "ARRAY", "items": CHECKLIST_ITEM_SCHEMA, "description": "주의 테마"},
        "checklist_watch": {"type": "ARRAY", "items": CHECKLIST_ITEM_SCHEMA, "description": "주목 섹터"},
    },
    "required": [
        "as_of", "indices", "economic_events", "ai_summary",
        "news", "connections", "checklist_caution", "checklist_watch",
    ],
}


def build_morning_brief(title, transcript_text):
    user_prompt = (
        f"방송: 한국경제TV [{PROGRAM_NAME}]\n"
        f"영상 제목: {title}\n\n"
        f"자막:\n{transcript_text[:MAX_TRANSCRIPT_CHARS]}"
    )
    return call_gemini(SYSTEM_PROMPT, user_prompt, RESPONSE_SCHEMA, f"당잠사 {title[:24]}", max_output_tokens=16384)
