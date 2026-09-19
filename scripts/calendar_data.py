"""증시 캘린더 데이터를 만든다. 나스닥 공개 API(키 불필요)에서 실적/경제지표를 가져와
국내 투자자에게 의미 있는 것만 걸러 docs/data/calendar.json 으로 저장한다.

하루에 전 세계 지표가 15건씩 들어오는데 대부분(스위스 M3, 브라질 주간보고 등)은 국내 증시와 무관하다.
그래서 종목은 관심 리스트, 지표는 화이트리스트로 좁힌다.
"""
import datetime
import re
import time

import requests

EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
ECONOMIC_URL = "https://api.nasdaq.com/api/calendar/economicevents"
BOK_URL = "https://www.bok.or.kr/portal/singl/crncyPolicyDrcMtg/listYear.do"

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
# major = 시장을 흔드는 이벤트(금리결정 등), macro = 일반 지표,
# issue = 지표는 아니지만 증시에서 챙겨야 할 체크포인트(연준 인사 발언, 국채 입찰, 원유재고).
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
        # 국채 입찰은 금리(=밸류에이션)로, 원유재고는 에너지·인플레 경로로 증시에 직결된다.
        # 연준 인사 발언은 하루에 여러 명이 잡히는데, 라벨을 하나로 통일해 같은 날 한 줄로 묶이게 한다.
        "issue": {
            "30-Year Bond Auction": "미 30년물 입찰",
            "20-Year Bond Auction": "미 20년물 입찰",
            "10-Year TIPS Auction": "미 10년물 물가채 입찰",
            "10-Year Note Auction": "미 10년물 입찰",
            "Crude Oil Inventories": "미국 원유재고",
            "Beige Book": "연준 베이지북",
            "Testimony": "연준 의회 증언",
            "Speaks": "연준 인사 발언",
        },
    },
    "South Korea": {
        "major": {"Interest Rate Decision": "한국 금통위"},
        "issue": {},
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
        "issue": {},
        "macro": {},
    },
}

# 키워드에 걸리지만 실제 지표 발표가 아닌 것들(추정 모델, 잡다한 연설 등)은 걷어낸다.
# Cleveland CPI 는 연은의 추정치라 실제 CPI 발표와 다른 시각에 뜬다. 빼지 않으면 달력에 CPI 가 두 번 찍힌다.
EXCLUDE_KEYS = ("GDPNow", "Atlanta Fed", "Redbook", "API Weekly", "Cushing", "Cleveland")

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


def bok_rate_decisions(year):
    """한국은행이 공시한 그 해 금통위 통화정책방향 회의일정을 가져온다.

    나스닥 경제지표 API 는 한 달 앞까지만 채워져 있어서, 국내 투자자에게 가장 중요한
    금통위가 두 달 뒤부터는 통째로 빠진다. 그래서 한국은행 공식 일정표에서 직접 읽는다.
    페이지가 '10월 22일(목)' 형태로 요일까지 같이 주기 때문에 파싱 결과를 자체 검증할 수 있다.
    발표시각 10:00 은 나스닥의 과거 금통위 데이터(KST 10:00)로 확인했다."""
    try:
        resp = requests.get(
            BOK_URL,
            params={"mtgSe": "A", "menuNo": "200755", "pYear": year},
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] BOK 금통위 일정 조회 실패 {year}: {e}")
        return []

    weekdays = "월화수목금토일"
    out = []
    for mm, dd, wd in re.findall(r"(\d{1,2})월\s?(\d{1,2})일\(([월화수목금토일])\)", resp.text):
        try:
            d = datetime.date(year, int(mm), int(dd))
        except ValueError:
            continue
        # 페이지가 적어준 요일과 실제 요일이 어긋나면 엉뚱한 연도를 읽은 것이다.
        if weekdays[d.weekday()] != wd:
            print(f"[WARN] BOK 일정 요일 불일치 {d} (페이지:{wd}) — 건너뜀")
            continue
        out.append(
            {
                "start": d.isoformat(),
                "end": d.isoformat(),
                "title": "한국 금통위",
                "category": "major",
                "detail": "10:00",
            }
        )

    print(f"[INFO] BOK {year} 금통위 {len(out)}건")
    return out


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


# 나스닥 경제지표 API 의 날짜/시각 규칙 (실제 데이터로 확인함):
#   - date 는 실제 발표일보다 하루 뒤로 들어온다
#   - gmt 필드는 이름과 달리 UTC 도, 미 동부 현지시각도 아니다.
#     서머타임과 무관하게 '항상 UTC-4' 로 고정돼 있다. 즉 겨울(EST)에는
#     실제 발표시각보다 1시간 크게 들어온다. 그래서 서머타임 보정을 하면 안 된다.
# 교차검증 (여름/겨울 모두 확인):
#   미국 CPI  여름 API 9/12  gmt 08:30  겨울 API 1/14  gmt 09:30  (실제 둘 다 08:30 ET)
#   원유재고   여름 API 9/17  gmt 10:30  겨울 API 1/15  gmt 11:30  (실제 둘 다 10:30 ET)
#   FOMC     여름 API 9/17  gmt 14:00  겨울 API 1/29  gmt 15:00  (실제 둘 다 14:00 ET)
#   -> UTC-4 고정으로 풀면: 미국 CPI 한국 9/11 21:30, FOMC 한국 9/17 03:00, BOJ 한국 1/23 12:00
KST = datetime.timezone(datetime.timedelta(hours=9))
API_TZ = datetime.timezone(datetime.timedelta(hours=-4))  # 서머타임 보정 금지 (위 주석 참조)


def to_kst(api_date, gmt_str):
    """API 날짜/시각을 한국시간 (날짜, HH:MM) 으로 바꾼다."""
    base_date = api_date - datetime.timedelta(days=1)
    try:
        hh, mm = (int(x) for x in (gmt_str or "").split(":")[:2])
    except Exception:
        return base_date, ""

    stamp = datetime.datetime(base_date.year, base_date.month, base_date.day, hh, mm, tzinfo=API_TZ)
    kst = stamp.astimezone(KST)
    return kst.date(), kst.strftime("%H:%M")


def fetch_day(date_obj):
    """하루치 이벤트를 [{date, title, category, detail}] 로 반환."""
    date_str = date_obj.isoformat()
    events = []

    try:
        for row in _rows(_get_json(EARNINGS_URL, date_str)):
            symbol = (row.get("symbol") or "").strip().upper()
            if symbol not in WATCHLIST:
                continue

            # 실적 API 는 날짜가 정확하다(미 동부 기준). 다만 장마감 후 발표는
            # 한국에선 다음 날 새벽이라 국내 투자자가 보는 날짜가 하루 밀린다.
            when = (row.get("time") or "").strip()
            if "pre-market" in when:
                kst_date, note = date_obj, "장전"
            elif "after-hours" in when:
                kst_date, note = date_obj + datetime.timedelta(days=1), "장마감 후"
            else:
                kst_date, note = date_obj, ""

            events.append(
                {
                    "start": kst_date.isoformat(),
                    "end": kst_date.isoformat(),
                    "title": f"{WATCHLIST[symbol]} 실적",
                    "category": "earnings",
                    "detail": f"{symbol} {note}".strip(),
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
            kst_date, kst_time = to_kst(date_obj, row.get("gmt"))
            real_str = kst_date.isoformat()
            name = (row.get("eventName") or "").strip()
            if not name or any(x.lower() in name.lower() for x in EXCLUDE_KEYS):
                continue

            label = _match(name, rules["major"])
            category = "major"
            if not label:
                label = _match(name, rules["macro"])
                category = "macro"
            if not label:
                label = _match(name, rules.get("issue") or {})
                category = "issue"
            if not label:
                continue

            events.append(
                {
                    "start": real_str,
                    "end": real_str,
                    "title": label,
                    "category": category,
                    "detail": kst_time,  # 한국시간
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

    for yr in {start.year, end.year}:
        for e in bok_rate_decisions(yr):
            if start.isoformat() <= e["start"] <= end.isoformat():
                events.append(e)

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
