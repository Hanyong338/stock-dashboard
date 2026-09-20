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

1. **지수 수치는 네가 채우지 않는다**
   - 나스닥/S&P/다우/SOX/미 10년물/달러 인덱스/WTI 의 종가와 등락률은 **시스템이 검증된 시장 데이터로 직접 채운다.**
     방송에서 귀로 들은 수치는 부정확할 수 있어 쓰지 않는다.
   - indices 항목은 빈 배열 `[]` 로 두어라. 여기에 숫자를 적어도 무시되고 덮어씌워진다.
   - 대신 **경제지표(실업수당, CPI, PPI, 고용 등) 발표는 economic_events 에 정확히 정리**한다.
     이건 방송에서 발표치/예상치를 명확히 읽어주므로 자막 그대로 옮긴다.
   - 지수 움직임에 대한 **해석과 원인**은 ai_summary 와 news 에서 서술하라. 숫자 나열이 아니라 맥락이 네 역할이다.

2. **간밤 주요 뉴스 전수 추출 + 심층 분석 (경제지표와 중복 금지)**
   - **[중복 제거 필수] economic_events 표에 넣은 수치 발표(실업수당, 물가, 고용 등)는 news 리스트에서 반드시 제외한다.**
     같은 내용이 두 곳에 나오면 안 된다.
   - 경제지표를 제외한 정치·지정학·정책·규제·산업 매크로 뉴스를 개수 제한 없이 누락 없이 전부 추출한다.
   - **[축약 금지 — 가장 자주 어기는 규칙이다]** 이 방송은 보통 한 회에 8~15개 꼭지를 다룬다.
     자막을 처음부터 끝까지 순서대로 훑으면서, 새로운 주제가 나올 때마다 news 항목을 하나씩 추가해라.
     **news 가 5건 미만이면 그건 네가 빠뜨린 것이다.** "중요한 것만 골랐다"는 판단을 하지 마라.
     고르는 건 네 역할이 아니고, 빠짐없이 옮긴 다음 각각을 깊게 분석하는 게 네 역할이다.
   - 방송의 모든 코너(글로벌 헤드라인, 마감 시황, 오늘장 특징주, 마켓무버, 이슈 인사이드, 특파원 리포트 등)를 훑는다.
   - connections 에 넣은 개별 종목 이슈라도, 그 배경에 정책·지정학·산업 구조 뉴스가 있다면
     그 뉴스는 news 에 따로 쓴다. 종목 등락과 뉴스는 다른 층위다.
   - **[심층 분석 필수] 단신이나 한 줄 요약으로 끝내지 마라. 각 뉴스를 아래 세 층으로 나눠서 쓴다.**
     * `fact` — **핵심 배경 및 팩트**: 방송에서 언급된 구체적 수치, 인물의 발언, 사건 경과를 2~3문장으로 상세히 기술한다.
       "누가, 무엇을, 얼마나" 가 빠지면 안 된다.
     * `street_view` — **월가 시각 및 시장 행간**: 왜 시장이 이 뉴스에 민감하게 반응했는지,
       그 이면의 구조적 변화나 긴축/완화 시그널이 무엇인지 해석한다. 사실 반복이 아니라 해석이어야 한다.
     * `korea_impact` — **국내 증시 & 섹터 영향**: 국내 어떤 산업·종목에 직접/간접으로 파급되는지,
       그래서 오늘 장에서 무엇을 어떻게 대응할지까지 쓴다.

3. **급등과 급락 동시 포착 (등락률 공백 금지)**
   - 개별 기업 이슈 중 주가가 크게 움직인 종목(대략 ±3~5% 이상) 또는 시장에 중대한 화두를 던진 대형주를 선정한다.
   - 상승 종목만 편향되게 넣지 말고, 투심 악화나 리스크를 경고하는 급락 종목/섹터도 반드시 1개 이상 포함한다.
   - **[공백 방지] 방송에서 그 종목의 구체적 등락률을 말하지 않았다면 us_change 를 빈칸이나 "-" 로 두지 마라.**
     대신 상황에 맞는 상태 뱃지를 쓴다: `[이슈]` `[수혜]` `[주목]` `[계약]` `[실적]` `[규제]`
     (예: 대형 공급 계약 소식이면 `[계약]`, 정책 수혜 기대면 `[수혜]`)

4. **국내 종목 연결 및 3단계 연관도 표기 (가장 중요)**
   - 근거 없는 찌라시성 잡주 연결을 엄격히 배제하고, 실제 사업/수주/공급망 기반으로 연결한다.
   - 반드시 "왜 연결되는지(연결 로직)"를 1문장으로 명확히 서술한다.
   - 연관도(strength) 기준:
     * 3 = 직접 연관: 직납 벤더, 핵심 부품 공급망, 글로벌 직접 경쟁사
     * 2 = 산업 연관: 동일 산업 CAPEX/인프라 수혜, 장비·소재 밸류체인
     * 1 = 테마 연관: 시장 심리 및 뉴스 모멘텀으로 함께 동조화되는 종목

5. **오늘 국내시장 체크리스트 (개수를 지켜라)**
   - checklist_watch(주목 = 오늘 눈여겨볼 기회)는 **최소 3개**를 쓴다.
   - checklist_caution(주의 = 오늘 조심할 리스크)은 **최소 2개**를 쓴다.
   - 하나씩만 쓰고 끝내지 마라. 간밤 뉴스와 급등·급락 종목에서 오늘 국내장과 이어지는 테마를
     빠짐없이 뽑아내면 이 개수는 자연히 채워진다.
   - 각 항목은 theme(테마/섹터), us(관련 미국 종목과 등락률), cause(원인), action(오늘 대응)을 모두 채운다.

## 출력 규칙
- 모든 텍스트는 한국어로 쓴다.
- **[지표 수치 인용 금지] 지수(나스닥·S&P·다우·SOX)·금리·유가(WTI)·달러 인덱스의 등락률과 종가는
  서술문 안에서도 숫자로 쓰지 마라.** 방송에서 귀로 들은 값은 실제와 어긋난다
  (실제 사례: 방송 "WTI -2.32%" ↔ 검증된 실제 -0.51%).
  이 지표들은 "유가가 하락 마감하며", "금리가 상승 압력을 받으며" 처럼 **방향과 맥락만** 서술하고,
  숫자는 시스템이 채운 표를 보게 둬라. 개별 기업 주가 등락률은 방송 값을 써도 된다.
- 그 외 수치(경제지표 발표치, 계약 금액, 개별 종목 등락률)는 방송 값을 그대로 쓰고, 등락률에는 부호(+/-)를 붙인다.
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
            "description": "빈 배열 []로 둘 것. 지수 수치는 시스템이 검증된 시장 데이터로 채우므로 여기 적은 값은 무시된다",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "value": {"type": "STRING"},
                    "change": {"type": "STRING"},
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
            "description": "방송에서 다룬 매크로 뉴스 전부. 개수 제한 없고 축약 금지 — 보통 5~15건이 나온다. "
            "economic_events 에 넣은 수치 발표(실업수당·물가·고용 등)는 여기서 반드시 제외할 것",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "fact": {
                        "type": "STRING",
                        "description": "핵심 배경 및 팩트. 방송에 나온 구체적 수치·인물 발언·사건 경과를 2~3문장으로",
                    },
                    "street_view": {
                        "type": "STRING",
                        "description": "월가 시각 및 시장 행간. 시장이 왜 민감하게 반응했는지, "
                        "이면의 구조적 변화나 긴축/완화 시그널 해석",
                    },
                    "korea_impact": {
                        "type": "STRING",
                        "description": "국내 증시·섹터 영향. 관련 국내 산업/종목 파급과 오늘 장 대응까지",
                    },
                },
                "required": ["title", "fact", "street_view", "korea_impact"],
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
                    "us_change": {
                        "type": "STRING",
                        "description": "등락률(부호 포함, 예 '+2.54%'). 방송에서 등락률을 언급하지 않았으면 "
                        "'-' 대신 상태 뱃지를 쓸 것: [이슈] [수혜] [주목] [계약] [실적] [규제]",
                    },
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
        "checklist_caution": {
            "type": "ARRAY",
            "items": CHECKLIST_ITEM_SCHEMA,
            "description": "오늘 조심할 리스크 테마. 최소 2개 이상",
        },
        "checklist_watch": {
            "type": "ARRAY",
            "items": CHECKLIST_ITEM_SCHEMA,
            "description": "오늘 눈여겨볼 기회 섹터. 최소 3개 이상",
        },
    },
    "required": [
        "as_of", "indices", "economic_events", "ai_summary",
        "news", "connections", "checklist_caution", "checklist_watch",
    ],
}


def build_morning_brief(title, transcript_text, broadcast_date):
    # 방송에서 연도를 말하지 않아 모델이 엉뚱한 해(2024년 등)로 적는 일이 있어서 날짜를 직접 넘긴다.
    user_prompt = (
        f"방송: 한국경제TV [{PROGRAM_NAME}]\n"
        f"방송일(한국시간): {broadcast_date}\n"
        f"as_of 에는 반드시 이 날짜를 '{broadcast_date} 마감 기준' 형식으로 그대로 써라. 연도를 임의로 바꾸지 마라.\n"
        f"영상 제목: {title}\n\n"
        f"자막:\n{transcript_text[:MAX_TRANSCRIPT_CHARS]}"
    )
    # 뉴스를 전수(최대 15건) 3단으로 쓰고 체크리스트까지 채우면 출력이 길어진다.
    # 한도에 걸려 뒷부분이 잘리면 JSON 자체가 깨지므로 넉넉히 잡는다(한도일 뿐 그만큼 과금되지 않는다).
    return call_gemini(SYSTEM_PROMPT, user_prompt, RESPONSE_SCHEMA, f"당잠사 {title[:24]}", max_output_tokens=32768)
