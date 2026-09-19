"""증시 캘린더 데이터를 만든다. 나스닥 공개 API(키 불필요)에서 실적/경제지표를 가져와
국내 투자자에게 의미 있는 것만 걸러 docs/data/calendar.json 으로 저장한다.

하루에 전 세계 지표가 15건씩 들어오는데 대부분(스위스 M3, 브라질 주간보고 등)은 국내 증시와 무관하다.
그래서 종목은 관심 리스트, 지표는 화이트리스트로 좁힌다.
"""
import datetime
import time

import requests

EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
ECONOMIC_URL = "https://api.nasdaq.com/api/calendar/economicevents"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    "Accept": "application/json",
}

REQUEST_INTERVAL = 0.35  # 연속 호출로 차단당하지 않게 간격을 둔다
MONTHS_AHEAD = 3  # 당월 + 3개월

# 실적 발표가 국내 증시에 영향을 주는 종목만. 소형주까지 넣으면 하루 20건씩 쌓여 달력이 못 쓰게 된다.
WATCHLIST = {
    # 반도체·AI
    "NVDA": "엔비디아", "AMD": "AMD", "AVGO": "브로드컴", "MU": "마이크론", "INTC": "인텔",
    "TSM": "TSMC", "QCOM": "퀄컴", "TXN": "TI", "ARM": "ARM", "MRVL": "마벨",
    "AMAT": "어플라이드", "LRCX": "램리서치", "KLAC": "KLA", "ASML": "ASML", "ON": "온세미",
    "SMCI": "슈퍼마이크로", "DELL": "델", "WDC": "웨스턴디지털", "STX": "시게이트",
    # 빅테크
    "AAPL": "애플", "MSFT": "마이크로소프트", "GOOGL": "알파벳", "AMZN": "아마존",
    "META": "메타", "TSLA": "테슬라", "NFLX": "넷플릭스", "ORCL": "오라클",
    "CRM": "세일즈포스", "ADBE": "어도비", "NOW": "서비스나우", "PLTR": "팔란티어",
    "PANW": "팔로알토", "CRWD": "크라우드스트라이크", "SNOW": "스노우플레이크",
    # 금융·결제
    "JPM": "JP모건", "BAC": "뱅크오브아메리카", "GS": "골드만삭스", "MS": "모건스탠리",
    "V": "비자", "MA": "마스터카드", "COIN": "코인베이스", "HOOD": "로빈후드",
    # 소비·산업·헬스케어
    "WMT": "월마트", "COST": "코스트코", "HD": "홈디포", "NKE": "나이키",
    "MCD": "맥도날드", "SBUX": "스타벅스", "KO": "코카콜라", "PG": "P&G",
    "BA": "보잉", "CAT": "캐터필러", "GE": "GE", "UPS": "UPS",
    "XOM": "엑슨모빌", "CVX": "셰브론",
    "LLY": "일라이릴리", "UNH": "유나이티드헬스", "JNJ": "존슨앤존슨", "MRK": "머크",
    "UBER": "우버", "ABNB": "에어비앤비", "BABA": "알리바바",
}

# 지표명에 이 문구가 들어가면 채택. 나스닥 eventName 이 영어라 영어로 매칭한다.
MACRO_KEYS = {
    "CPI": "소비자물가(CPI)",
    "PPI": "생산자물가(PPI)",
    "PCE Price Index": "PCE 물가",
    "Nonfarm Payrolls": "비농업 고용",
    "Unemployment Rate": "실업률",
    "Initial Jobless Claims": "주간 실업수당",
    "ADP Nonfarm": "ADP 고용",
    "JOLTS Job Openings": "JOLTS 구인",
    "GDP (QoQ)": "GDP",
    "GDP Price Index": "GDP 물가지수",
    "Retail Sales": "소매판매",
    "ISM Manufacturing PMI": "ISM 제조업",
    "ISM Non-Manufacturing PMI": "ISM 서비스업",
    "Durable Goods Orders": "내구재 주문",
    "Michigan Consumer Sentiment": "미시간 소비자심리",
    "Building Permits": "건축 허가",
    "Housing Starts": "주택 착공",
    "Crude Oil Inventories": "원유 재고",
}

# 시장을 통째로 흔드는 이벤트는 따로 강조한다.
MAJOR_KEYS = {
    "Interest Rate Decision": "FOMC 금리결정",
    "FOMC Statement": "FOMC 성명",
    "FOMC Meeting Minutes": "FOMC 의사록",
    "FOMC Economic Projections": "FOMC 점도표",
    "Fed Chair Powell Speaks": "파월 연설",
    "Fed Interest Rate Decision": "FOMC 금리결정",
}

# 키워드에 걸리지만 실제 지표 발표가 아닌 것들(추정 모델, 잡다한 연설 등)은 걷어낸다.
EXCLUDE_KEYS = ("GDPNow", "Atlanta Fed", "Redbook", "API Weekly")

# 국내 증시 휴장일(양력 고정일 + 확인된 연휴). 음력 계산은 하지 않고 확실한 것만 넣는다.
MARKET_HOLIDAYS = [
    {"start": "2026-09-24", "end": "2026-09-26", "title": "추석 연휴 (휴장)"},
    {"start": "2026-10-03", "end": "2026-10-03", "title": "개천절 (휴장)"},
    {"start": "2026-10-09", "end": "2026-10-09", "title": "한글날 (휴장)"},
    {"start": "2026-12-25", "end": "2026-12-25", "title": "성탄절 (휴장)"},
]


def _get_json(url, date_str):
    resp = requests.get(url, params={"date": date_str}, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _rows(payload):
    data = payload.get("data") or {}
    if isinstance(data, dict):
        return data.get("rows") or []
    return data if isinstance(data, list) else []


def _match(name, table):
    """지표명에서 화이트리스트 키를 찾아 한글 라벨을 돌려준다."""
    for key, label in table.items():
        if key.lower() in name.lower():
            return label
    return None


def fetch_day(date_obj):
    """하루치 이벤트를 [{date, title, category, detail}] 로 반환."""
    date_str = date_obj.isoformat()
    events = []

    try:
        for row in _rows(_get_json(EARNINGS_URL, date_str)):
            symbol = (row.get("symbol") or "").strip().upper()
            if symbol not in WATCHLIST:
                continue
            events.append(
                {
                    "start": date_str,
                    "end": date_str,
                    "title": f"{WATCHLIST[symbol]} 실적",
                    "category": "earnings",
                    "detail": symbol,
                }
            )
    except Exception as e:
        print(f"[WARN] earnings fetch failed {date_str}: {e}")

    try:
        for row in _rows(_get_json(ECONOMIC_URL, date_str)):
            if (row.get("country") or "") != "United States":
                continue
            name = (row.get("eventName") or "").strip()
            if not name or any(x.lower() in name.lower() for x in EXCLUDE_KEYS):
                continue

            label = _match(name, MAJOR_KEYS)
            category = "major"
            if not label:
                label = _match(name, MACRO_KEYS)
                category = "macro"
            if not label:
                continue

            events.append(
                {
                    "start": date_str,
                    "end": date_str,
                    "title": label,
                    "category": category,
                    "detail": (row.get("gmt") or "").strip(),
                }
            )
    except Exception as e:
        print(f"[WARN] economic fetch failed {date_str}: {e}")

    return events


def _month_starts(today, count):
    starts = []
    y, m = today.year, today.month
    for _ in range(count):
        starts.append(datetime.date(y, m, 1))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return starts


def build_calendar(today=None):
    """당월부터 MONTHS_AHEAD 개월치 캘린더 데이터를 만든다."""
    today = today or datetime.date.today()
    months = _month_starts(today, MONTHS_AHEAD + 1)
    start = months[0]
    last = months[-1]
    end = (datetime.date(last.year + (last.month // 12), (last.month % 12) + 1, 1)) - datetime.timedelta(days=1)

    events = []
    day = start
    fetched = 0
    while day <= end:
        if day.weekday() < 5:  # 실적·지표는 평일에만 나온다
            events.extend(fetch_day(day))
            fetched += 1
            time.sleep(REQUEST_INTERVAL)
        day += datetime.timedelta(days=1)

    for h in MARKET_HOLIDAYS:
        if start.isoformat() <= h["start"] <= end.isoformat():
            events.append({**h, "category": "holiday", "detail": ""})

    # 같은 날 같은 제목이 중복으로 들어오는 경우가 있어 정리한다.
    seen = set()
    unique = []
    for e in sorted(events, key=lambda x: (x["start"], x["category"], x["title"])):
        key = (e["start"], e["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)

    print(f"[INFO] calendar: {fetched}일 조회, 이벤트 {len(unique)}건")
    return {
        "months": [m.isoformat()[:7] for m in months],
        "events": unique,
    }
