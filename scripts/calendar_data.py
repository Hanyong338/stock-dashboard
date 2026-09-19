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

# 국가별로 챙길 지표를 따로 정의한다. 미국만 보면 BOJ 금리결정이나 한국 금통위처럼
# 국내 증시를 크게 흔드는 일정이 통째로 빠진다.
# major = 시장을 흔드는 이벤트(금리결정 등), macro = 일반 지표.
# 라벨에 국가를 붙여야 달력에서 어느 나라 것인지 바로 보인다.
COUNTRY_RULES = {
    "United States": {
        "major": {
            "Interest Rate Decision": "FOMC 금리결정",
            "FOMC Statement": "FOMC 성명",
            "FOMC Meeting Minutes": "FOMC 의사록",
            "FOMC Economic Projections": "FOMC 점도표",
            "Fed Chair Powell Speaks": "파월 연설",
        },
        "macro": {
            "Core CPI": "미국 근원 CPI",
            "CPI": "미국 CPI",
            "Core PPI": "미국 근원 PPI",
            "PPI": "미국 PPI",
            "Core PCE Price Index": "미국 근원 PCE",
            "PCE Price Index": "미국 PCE",
            "Nonfarm Payrolls": "미국 고용지표",
            "Unemployment Rate": "미국 실업률",
            "Initial Jobless Claims": "미국 실업수당",
            "ADP Nonfarm": "ADP 고용",
            "JOLTS Job Openings": "JOLTS 구인",
            "GDP (QoQ)": "미국 GDP",
            "Retail Sales": "미국 소매판매",
            "ISM Manufacturing PMI": "ISM 제조업",
            "ISM Non-Manufacturing PMI": "ISM 서비스업",
            "Durable Goods Orders": "미국 내구재",
            "Michigan Consumer Sentiment": "미시간 소비심리",
            "CB Consumer Confidence": "미국 소비자신뢰",
            "Philadelphia Fed Manufacturing Index": "필라델피아 연은지수",
            "Existing Home Sales": "미국 기존주택판매",
            "New Home Sales": "미국 신규주택판매",
            "Building Permits": "미국 건축허가",
            "Housing Starts": "미국 주택착공",
        },
    },
    "South Korea": {
        "major": {"Interest Rate Decision": "한국 금통위"},
        "macro": {
            "CPI": "한국 CPI",
            "PPI": "한국 PPI",
            "GDP": "한국 GDP",
            "Exports": "한국 수출",
            "Imports": "한국 수입",
            "Trade Balance": "한국 무역수지",
            "Industrial Production": "한국 산업생산",
            "Current Account": "한국 경상수지",
            "Business Survey Index": "한국 BSI",
            "Consumer Confidence": "한국 소비자심리",
            "Unemployment Rate": "한국 실업률",
            "Retail Sales": "한국 소매판매",
            "Manufacturing PMI": "한국 제조업 PMI",
        },
    },
    # 일본은 BOJ 금리 관련만 본다. 엔캐리·환율 경로로 국내 증시에 직접 영향을 주기 때문.
    # 일본 CPI/단칸 같은 일반 지표는 빼서 달력이 복잡해지지 않게 한다.
    "Japan": {
        "major": {
            "BoJ Interest Rate Decision": "BOJ 금리결정",
            "BoJ Monetary Policy Statement": "BOJ 정책성명",
        },
        "macro": {},
    },
}

# 키워드에 걸리지만 실제 지표 발표가 아닌 것들(추정 모델, 잡다한 연설 등)은 걷어낸다.
EXCLUDE_KEYS = ("GDPNow", "Atlanta Fed", "Redbook", "API Weekly")

# 국내 증시 휴장일. 추석·설날은 음력이라 계산하지 않고 확인된 것만 적는다.
KR_HOLIDAYS = [
    {"start": "2026-09-24", "end": "2026-09-26", "title": "추석 연휴 (한국 휴장)"},
    {"start": "2026-10-03", "end": "2026-10-03", "title": "개천절 (한국 휴장)"},
    {"start": "2026-10-09", "end": "2026-10-09", "title": "한글날 (한국 휴장)"},
    {"start": "2026-12-25", "end": "2026-12-25", "title": "성탄절 (한국 휴장)"},
]


def _nth_weekday(year, month, weekday, n):
    """그 달의 n번째 특정 요일. weekday 는 월=0 ... 일=6."""
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year, month, weekday):
    nxt = datetime.date(year + (month // 12), (month % 12) + 1, 1)
    last = nxt - datetime.timedelta(days=1)
    return last - datetime.timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year):
    """부활절(그레고리력). 성금요일 계산에 쓴다."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def _observed(d):
    """토요일이면 전날 금요일, 일요일이면 다음날 월요일로 대체휴장 (NYSE 규칙)."""
    if d.weekday() == 5:
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d


def us_market_holidays(year):
    """미국 증시(NYSE/나스닥) 휴장일을 연도별로 계산한다. 손으로 적으면 틀리기 쉬워서 계산한다."""
    days = [
        (_observed(datetime.date(year, 1, 1)), "신정"),
        (_nth_weekday(year, 1, 0, 3), "마틴 루터 킹 데이"),
        (_nth_weekday(year, 2, 0, 3), "대통령의 날"),
        (_easter(year) - datetime.timedelta(days=2), "성금요일"),
        (_last_weekday(year, 5, 0), "메모리얼 데이"),
        (_observed(datetime.date(year, 6, 19)), "준틴스"),
        (_observed(datetime.date(year, 7, 4)), "독립기념일"),
        (_nth_weekday(year, 9, 0, 1), "노동절"),
        (_nth_weekday(year, 11, 3, 4), "추수감사절"),
        (_observed(datetime.date(year, 12, 25)), "성탄절"),
    ]
    return [
        {"start": d.isoformat(), "end": d.isoformat(), "title": f"{name} (미국 휴장)"}
        for d, name in days
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


# 나스닥 경제지표 API 는 미국 발표를 하루 뒤 날짜로 준다. 실제 확인한 사례:
#   비농업고용 API 9/05(토) -> 실제 9/04(금)
#   CPI        API 9/12(토) -> 실제 9/11(금)
#   FOMC       API 9/17(목) -> 실제 9/16(수)  (당잠사 09/17 방송이 이 금리인상을 다룸)
#   주간실업수당 API 9/18(금) -> 실제 9/17(목) (당잠사가 9/17 세션으로 보도)
# 반면 중국 LPR 은 9/21(월)로 정확하고, 실적 API 도 정확하다(테슬라 수요일·애플 목요일).
# 그래서 '미국 경제지표'에만 하루를 빼준다.
DATE_SHIFT_COUNTRIES = {"United States"}


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
            country = (row.get("country") or "").strip()
            rules = COUNTRY_RULES.get(country)
            if not rules:
                continue
            real_date = date_obj - datetime.timedelta(days=1) if country in DATE_SHIFT_COUNTRIES else date_obj
            real_str = real_date.isoformat()
            name = (row.get("eventName") or "").strip()
            if not name or any(x.lower() in name.lower() for x in EXCLUDE_KEYS):
                continue

            label = _match(name, rules["major"])
            category = "major"
            if not label:
                label = _match(name, rules["macro"])
                category = "macro"
            if not label:
                continue

            events.append(
                {
                    "start": real_str,
                    "end": real_str,
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


def issues_from_brief(brief):
    """당잠사 리포트의 뉴스를 캘린더 '이슈'로 바꾼다.
    법안 통과·규제 변경 같은 정책 이벤트는 경제지표 API 에 없어서 이 경로로만 들어온다."""
    if not brief or not brief.get("news"):
        return []

    # 리포트가 다룬 거래일에 붙인다. published(UTC)를 미 동부로 옮기면 그 날짜가 나온다.
    try:
        pub = datetime.datetime.fromisoformat(brief["published"].replace("Z", "+00:00"))
        day = (pub - datetime.timedelta(hours=4)).date().isoformat()
    except Exception:
        return []

    out = []
    for n in brief["news"]:
        title = (n.get("title") or "").strip()
        if not title:
            continue
        out.append(
            {
                "start": day,
                "end": day,
                "title": title,
                "category": "issue",
                "detail": (n.get("comment") or "").strip()[:120],
            }
        )
    return out


def build_calendar(today=None, carry_issues=None):
    """당월부터 MONTHS_AHEAD 개월치 캘린더 데이터를 만든다.
    carry_issues 로 이전에 쌓아둔 이슈를 넘기면 함께 보존한다."""
    today = today or datetime.date.today()
    months = _month_starts(today, MONTHS_AHEAD + 1)
    start = months[0]
    last = months[-1]
    end = (datetime.date(last.year + (last.month // 12), (last.month % 12) + 1, 1)) - datetime.timedelta(days=1)

    events = []
    # 주말도 조회해야 한다. 미국 지표가 API 상에서 토요일 날짜로 들어오기 때문에
    # 평일만 훑으면 CPI·비농업고용 같은 핵심 지표가 통째로 빠진다.
    day = start - datetime.timedelta(days=1)  # 앞으로 당길 미국 지표까지 잡으려면 하루 먼저 시작
    fetched = 0
    while day <= end:
        events.extend(fetch_day(day))
        fetched += 1
        time.sleep(REQUEST_INTERVAL)
        day += datetime.timedelta(days=1)

    holidays = list(KR_HOLIDAYS)
    for yr in {start.year, end.year}:
        holidays.extend(us_market_holidays(yr))

    for h in holidays:
        if start.isoformat() <= h["start"] <= end.isoformat():
            events.append({**h, "category": "holiday", "detail": ""})

    events.extend(carry_issues or [])

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
