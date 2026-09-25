"""증시 캘린더 데이터를 만든다. 나스닥 공개 API(키 불필요)에서 실적/경제지표를 가져와
국내 투자자에게 의미 있는 것만 걸러 docs/data/calendar.json 으로 저장한다.

하루에 전 세계 지표가 15건씩 들어오는데 대부분(스위스 M3, 브라질 주간보고 등)은 국내 증시와 무관하다.
그래서 종목은 관심 리스트, 지표는 화이트리스트로 좁힌다.
"""
import datetime
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
ECONOMIC_URL = "https://api.nasdaq.com/api/calendar/economicevents"
BOK_URL = "https://www.bok.or.kr/portal/singl/crncyPolicyDrcMtg/listYear.do"
MSCI_DATES_URL = "https://app2.msci.com/eqb/pressreleases/archive/ir_dates.csv"
KIND_IR_URL = "https://kind.krx.co.kr/corpgeneral/irschedule.do"
NAVER_CAP_URL = "https://m.stock.naver.com/api/stocks/marketValue/{market}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    "Accept": "application/json",
}

FETCH_WORKERS = 6  # 날짜별 조회를 동시에 몇 개까지 돌릴지. 올릴수록 빠르지만 차단 위험이 커진다.
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
            "FOMC Press Conference": "FOMC 기자회견",
            # 이름을 넣으면 의장이 바뀔 때 조용히 빠진다. 실제로 'Powell' 로 적어둬서
            # 2026년 5월 의장 교체(워시) 이후 의장 연설이 달력에서 사라졌었다.
            "Fed Chair": "연준 의장 연설",
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
            # S&P글로벌 PMI 는 ISM 보다 먼저 나오는 속보치라 장중 변동성을 자주 만든다.
            # Composite 은 위 둘의 합성이라 중복이므로 키를 정확히 잡아 제외한다.
            "S&P Global Manufacturing PMI": "S&P글로벌 제조업 PMI",
            "S&P Global Services PMI": "S&P글로벌 서비스업 PMI",
            "Richmond Manufacturing Index": "리치먼드 연은지수",
            "Current Account": "미국 경상수지",
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


def _bok_entry(d):
    return {
        "start": d.isoformat(),
        "end": d.isoformat(),
        "title": "한국 금통위",
        "category": "major",
        "detail": "10:00",  # 나스닥 과거 금통위 데이터(KST 10:00)로 확인
    }


# 한국은행 공식 통화정책방향 회의일정. bok.or.kr 에서 읽어 요일까지 대조해 확인한 값이다.
# 아래 사이트 조회가 실패할 때(깃허브 서버에서 막히는 듯하다) 쓰는 대비책이라
# 연도가 바뀌면 여기에 새 해 일정을 확인해서 넣어야 한다.
BOK_MEETINGS_FALLBACK = {
    2026: ["01-15", "02-26", "04-10", "05-28", "07-16", "08-27", "10-22", "11-26"],
}


def bok_rate_decisions(year):
    """한국은행이 공시한 그 해 금통위 통화정책방향 회의일정을 가져온다.

    나스닥 경제지표 API 는 한 달 앞까지만 채워져 있어서, 국내 투자자에게 가장 중요한
    금통위가 두 달 뒤부터는 통째로 빠진다. 그래서 한국은행 공식 일정표에서 직접 읽는다.
    페이지가 '10월 22일(목)' 형태로 요일까지 같이 주기 때문에 파싱 결과를 자체 검증할 수 있다."""
    live = _bok_from_site(year)
    if live:
        return live

    fallback = BOK_MEETINGS_FALLBACK.get(year, [])
    if fallback:
        print(f"[INFO] BOK {year}: 사이트 조회 실패 — 확인해둔 일정 {len(fallback)}건으로 대체")
    else:
        print(f"[WARN] BOK {year}: 사이트도 실패하고 대비책도 없다 — 금통위가 달력에서 빠진다")
    return [_bok_entry(datetime.date(year, int(md[:2]), int(md[3:]))) for md in fallback]


def _bok_from_site(year):
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
        out.append(_bok_entry(d))

    print(f"[INFO] BOK {year} 금통위 {len(out)}건 (사이트)")
    return out


# ──────────────────────────────────────────────────────────────────────────
# 수급 이벤트 — 선물·옵션 만기일, 지수 정기변경 (category: flow)
# ──────────────────────────────────────────────────────────────────────────
def _holiday_dates(entries):
    """휴장 목록({start, end})을 날짜 집합으로 푼다."""
    out = set()
    for h in entries:
        d = datetime.date.fromisoformat(h["start"])
        last = datetime.date.fromisoformat(h["end"])
        while d <= last:
            out.add(d)
            d += datetime.timedelta(days=1)
    return out


def _prev_business_day(d, closed):
    while d.weekday() >= 5 or d in closed:
        d -= datetime.timedelta(days=1)
    return d


def _next_business_day(d, closed):
    d += datetime.timedelta(days=1)
    while d.weekday() >= 5 or d in closed:
        d += datetime.timedelta(days=1)
    return d


def _flow(d, title, detail):
    return {"start": d.isoformat(), "end": d.isoformat(), "title": title, "category": "flow", "detail": detail}


def expiry_events(months, kr_closed, us_closed):
    """만기일은 규칙으로 정해져 있어 조회 없이 계산한다(틀릴 여지가 없다).
      국장  코스피200 옵션 — 매월 둘째 목요일. 3·6·9·12월은 선물과 겹치는 동시만기
      미장  주식·지수 옵션 — 매월 셋째 금요일. 3·6·9·12월은 쿼드러플 위칭
    만기일이 휴장이면 직전 거래일로 당겨진다.
    코스피200·코스닥150 정기변경은 6·12월 동시만기 다음 거래일에 반영된다
    (거래소 보도자료로 확인: 2026년 6월 만기 6/11 -> 반영 6/12)."""
    out = []
    for m in months:
        quarter = m.month in (3, 6, 9, 12)

        kr = _prev_business_day(_nth_weekday(m.year, m.month, 3, 2), kr_closed)
        if quarter:
            out.append(_flow(kr, "국내 선물·옵션 동시만기", "최종거래일 · 장 막판 프로그램 매매 변동성"))
        else:
            out.append(_flow(kr, "국내 옵션만기", "코스피200 옵션 최종거래일"))
        if m.month in (6, 12):
            out.append(
                _flow(_next_business_day(kr, kr_closed), "코스피200·코스닥150 정기변경 반영",
                      "전날(만기일) 종가에 패시브 편입·편출 매매")
            )

        us = _prev_business_day(_nth_weekday(m.year, m.month, 4, 3), us_closed)
        if quarter:
            out.append(_flow(us, "미국 쿼드러플 위칭", "지수·주식 선물옵션 동시만기 · 한국시간 토 새벽 마감"))
        else:
            out.append(_flow(us, "미국 옵션만기", "월간 옵션 · 한국시간 토 새벽 마감"))
    return out


# MSCI 공식 발표 일정. 아래 CSV 조회가 실패할 때 쓰는 대비책이다.
# 출처: MSCI 'Announces the Next Eight Index Review Dates' (2026-08-12 발표)
MSCI_FALLBACK = [
    ("2026-11-11", "2026-12-01"), ("2027-02-09", "2027-03-01"), ("2027-05-10", "2027-05-28"),
    ("2027-08-12", "2027-09-01"), ("2027-11-11", "2027-12-01"), ("2028-02-10", "2028-03-01"),
    ("2028-05-11", "2028-06-01"), ("2028-08-14", "2028-09-01"),
]


def _msci_dates():
    """MSCI 가 정기변경 때마다 앞으로 8회분 일정을 CSV 로 올려둔다. 연도가 바뀌어도 손댈 필요가 없다."""
    try:
        resp = requests.get(MSCI_DATES_URL, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=20)
        resp.raise_for_status()
        pairs = [
            (f"{a[2]}-{a[0]}-{a[1]}", f"{b[2]}-{b[0]}-{b[1]}")
            for a, b in (
                (m[0:3], m[3:6])
                for m in re.findall(r"(\d\d)-(\d\d)-(\d{4})\|(\d\d)-(\d\d)-(\d{4})", resp.text)
            )
        ]
        if pairs:
            print(f"[INFO] MSCI 정기변경 일정 {len(pairs)}건 (공식 CSV)")
            return pairs
    except Exception as e:
        print(f"[WARN] MSCI 일정 조회 실패: {e}")
    print(f"[INFO] MSCI: 확인해둔 일정 {len(MSCI_FALLBACK)}건으로 대체")
    return MSCI_FALLBACK


def msci_events(kr_closed):
    """MSCI 는 발표일 현지 밤 11시(중유럽)에 명단을 내고 = 한국시간 다음 날 아침 6~7시,
    '발효일 전 거래일 종가 기준'으로 반영한다(공식 공지 문구: 'as of the close of ...').
    국내 패시브 자금이 실제로 움직이는 건 그 반영 거래일 장 마감이다."""
    out = []
    for ann, eff in _msci_dates():
        ann_kst = datetime.date.fromisoformat(ann) + datetime.timedelta(days=1)
        out.append(_flow(ann_kst, "MSCI 정기변경 발표", "한국시간 아침 6~7시 편입·편출 명단"))
        last_day = _prev_business_day(datetime.date.fromisoformat(eff) - datetime.timedelta(days=1), kr_closed)
        out.append(_flow(last_day, "MSCI 정기변경 반영", "장 마감 동시호가에 패시브 자금 집중"))
    return out


# ──────────────────────────────────────────────────────────────────────────
# 국내 기업 실적발표 — 한국거래소 KIND 'IR 일정' (category: earnings)
# ──────────────────────────────────────────────────────────────────────────
# 실적 시즌 한 분기에 180곳 넘게 설명회를 연다. 달력이 못 쓰게 되지 않도록 시총으로 좁힌다.
# 2026년 2분기 실측: 1조 이상 127곳 / 2조 이상 107곳 / 5조 이상 68곳. 2조면 삼성전자·SK하이닉스는 물론
# 한미반도체·ISC·심텍 같은 반도체 중형주까지 들어온다.
KR_EARNINGS_MIN_CAP = 2_000_000_000_000
# 실적 설명회만 고른다. KIND IR 일정의 대부분은 증권사 컨퍼런스 참가·소형주 설명회다.
_EARNINGS_PURPOSE = re.compile(
    r"(\d\s*분기|상반기|하반기|반기|연간|결산|[1-4]Q|Q[1-4]|[12]H|FY).{0,20}(실적|Earnings|earnings)"
    r"|(실적|Earnings|earnings).{0,12}(발표|설명|Release|release|Announcement|Conference|conference)"
)
_NOT_EARNINGS = re.compile(r"non-deal|NDR|[Rr]oadshow|Post-earnings")


def _kr_market_caps(min_cap):
    """종목코드(6자리) -> 시가총액. 네이버 목록은 시총 큰 순이라 기준 아래로 내려가면 멈춘다."""
    caps = {}
    headers = {"User-Agent": HEADERS["User-Agent"], "Referer": "https://m.stock.naver.com/"}
    for market in ("KOSPI", "KOSDAQ"):
        for page in range(1, 11):
            resp = requests.get(
                NAVER_CAP_URL.format(market=market), params={"page": page, "pageSize": 100},
                headers=headers, timeout=20,
            )
            resp.raise_for_status()
            rows = resp.json().get("stocks") or []
            for r in rows:
                try:
                    caps[r["itemCode"]] = float(r.get("marketValueRaw") or 0)
                except (TypeError, ValueError):
                    pass
            if not rows or float(rows[-1].get("marketValueRaw") or 0) < min_cap:
                break
    return caps


def _kind_ir_rows(start, end):
    """KIND IR 일정 표를 (날짜, 시각, 시장, KIND코드, 회사, 목적) 로 푼다."""
    headers = {"User-Agent": HEADERS["User-Agent"]}
    out = []
    for page in range(1, 11):
        resp = requests.post(
            KIND_IR_URL,
            data={
                "method": "searchIRScheduleSub", "currentPageSize": "100", "pageIndex": str(page),
                "orderMode": "1", "orderStat": "D",
                "fromDate": start.isoformat(), "toDate": end.isoformat(),
            },
            headers=headers,
            timeout=25,
        )
        resp.raise_for_status()
        rows = [r for r in re.findall(r"(?s)<tr[^>]*>.*?</tr>", resp.text) if re.search(r"20\d\d-\d\d-\d\d", r)]
        if not rows:
            break
        for r in rows:
            co = re.search(r"companysummary_open\('(\w+)'\);[^>]*>([^<]+)<", r)
            purpose = re.search(r"fnDetailView\('\d+'\); return false;\">([^<]+)<", r)
            day = re.search(r"(20\d\d-\d\d-\d\d)", r)
            if not (co and purpose and day):
                continue
            clock = re.search(r'txc">\s*(\d{1,2})[:시]\s*(\d{2})', r)
            mkt = re.search(r"alt='([^']+)'", r)
            out.append(
                {
                    "date": day.group(1),
                    "time": f"{int(clock.group(1)):02d}:{clock.group(2)}" if clock else "",
                    "market": mkt.group(1) if mkt else "",
                    "kind_code": co.group(1),
                    "name": html.unescape(co.group(2)).strip(),
                    "purpose": " ".join(html.unescape(purpose.group(1)).split()),
                }
            )
        if len(rows) < 100:
            break
    return out


def kr_earnings_events(start, end):
    """국내 대형주 실적발표(컨퍼런스콜) 일정. 회사가 보통 1주일쯤 전에 KIND 에 올리므로
    먼 달은 비어 있다가 실적 시즌이 다가오면 채워진다.
    삼성전자 '잠정실적'은 IR 일정 없이 당일 공시로 나와서 여기 잡히지 않는다(확정실적 설명회는 잡힌다)."""
    try:
        rows = _kind_ir_rows(start, end)
        caps = _kr_market_caps(KR_EARNINGS_MIN_CAP)
    except Exception as e:
        print(f"[WARN] 국내 실적 일정 조회 실패: {e}")
        return []

    out, seen = [], {}
    labels = {"유가증권": "코스피", "코스닥": "코스닥"}
    for r in sorted(rows, key=lambda x: (x["date"], x["time"])):
        if r["market"] not in labels:
            continue  # 코넥스 제외
        if not _EARNINGS_PURPOSE.search(r["purpose"]) or _NOT_EARNINGS.search(r["purpose"]):
            continue
        # KIND 는 6자리 종목코드의 앞 5자리를 쓴다(삼성전자 005930 -> 00593).
        if caps.get(r["kind_code"] + "0", 0) < KR_EARNINGS_MIN_CAP:
            continue
        # 한글·영문 공지가 따로 올라오고, 이틀에 걸쳐 여는 회사도 있다. 한 분기에 한 번(첫날)만 남긴다.
        d = datetime.date.fromisoformat(r["date"])
        prev = seen.get(r["name"])
        if prev and (d - prev).days < 30:
            continue
        seen[r["name"]] = d
        out.append(
            {
                "start": r["date"],
                "end": r["date"],
                "title": f"{r['name']} 실적",
                "category": "earnings",
                "detail": f"{labels[r['market']]} {r['time']}".strip(),
            }
        )
    print(f"[INFO] 국내 실적발표 {len(out)}건 (KIND IR {len(rows)}건 중 시총 2조 이상 실적 설명회)")
    return out


def _get_json(url, date_str):
    """한 번 실패했다고 그 날짜를 통째로 버리면 지표가 소리 없이 빠진다. 두 번까지 더 시도한다."""
    last = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params={"date": date_str}, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise last


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
    """하루치 이벤트와 실패 건수를 (events, failures) 로 반환.
    실패를 세어 올리는 이유: 조용히 빠진 날짜가 생기면 달력에 CPI 가 통째로 없어도 아무도 모른다."""
    date_str = date_obj.isoformat()
    events = []
    failures = 0

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
        failures += 1
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
        failures += 1
        print(f"[WARN] economic fetch failed {date_str}: {e}")

    return events, failures


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

    # 주말도 조회해야 한다. 미국 지표가 API 상에서 토요일 날짜로 들어오기 때문에
    # 평일만 훑으면 CPI·비농업고용 같은 핵심 지표가 통째로 빠진다.
    days = []
    day = start - datetime.timedelta(days=1)  # 앞으로 당길 미국 지표까지 잡으려면 하루 먼저 시작
    while day <= end:
        days.append(day)
        day += datetime.timedelta(days=1)

    # 하루에 2번씩, 120일이면 240번을 순서대로 호출해서 10분 넘게 걸리던 걸 병렬로 돌린다.
    # 동시 6개는 나스닥이 막지 않는 선이고, 막히더라도 _get_json 이 두 번 더 재시도한다.
    events = []
    failures = 0
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for got, failed in pool.map(fetch_day, days):
            events.extend(got)
            failures += failed
    fetched = len(days)
    if failures:
        print(f"[WARN] calendar: {failures}건 조회 실패 — 그만큼 일정이 빠졌을 수 있다")

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

    # 수급 이벤트(만기일·지수 정기변경)와 국내 실적발표
    kr_closed = _holiday_dates(KR_HOLIDAYS)
    us_closed = _holiday_dates([h for yr in {start.year, end.year} for h in us_market_holidays(yr)])
    extra = expiry_events(months, kr_closed, us_closed) + msci_events(kr_closed) + kr_earnings_events(start, end)
    events.extend(e for e in extra if start.isoformat() <= e["start"] <= end.isoformat())

    # 주말은 어차피 장이 안 열려서 발표되는 게 없다. 여기 걸리는 건 한국시간으로 옮기다가
    # 토요일 새벽으로 밀린 자투리뿐이라 지운다. 연휴처럼 여러 날 걸친 일정은 건드리지 않는다.
    # 수급 이벤트는 날짜를 직접 계산해 넣은 것이라 예외다(MSCI 가 금요일 밤에 발표하면 한국은 토요일 아침이다).
    events = [
        e
        for e in events
        if e["start"] != e["end"]
        or e["category"] == "flow"
        or datetime.date.fromisoformat(e["start"]).weekday() < 5
    ]

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
