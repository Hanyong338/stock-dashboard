"""미장(미국 주식) 시그널 스크리너.

판정 로직은 국장과 한 벌이다. 킬스위치·섹션 판정·섹션별 자르기·차트 저장 모두
screening.py 의 같은 함수를 부르고, 금액 기준(US_CFG)만 달러로 바꿔 끼운다.
여기 있는 건 국장과 출처가 다른 부분뿐이다.

  0단계  나스닥 스크리너(거래소별 3회) -> 보통주만 남기고 체급 미달 배제
  1단계  살아남은 종목만 야후 일봉 조회 -> 킬스위치 + 섹션 판정 (screening.py)
  2단계  수급 없음 — 미국은 투자자별 순매수를 공개하지 않는다 (screening_rules.US_NO_FLOW_NOTE)
  3단계  산업분류 태그와 '오늘 주도 업종' (나스닥 스크리너에 이미 들어 있어 추가 조회가 없다)

실행 시점: 미국 정규장 마감(현지 16:00) 뒤, 한국시간 아침 7시 전후에 하루 한 번.
"""
import re
import time
from collections import defaultdict

import requests

from screening import (
    SECTION_ORDER,
    assign_sections,
    scan_daily,
    write_chart_files,
    _yahoo_chart,
)
from screening_rules import (
    SECTION_DESC,
    SECTION_EXITS,
    SECTION_LEGEND,
    SECTION_NAME,
    US_BASE_QUALITY,
    US_MONEY_RULES,
    US_NO_FLOW_NOTE,
    US_NO_FLOW_TYPES,
    US_SECTION_OVERRIDES,
)

SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    "Accept": "application/json",
}
# 전체 목록 한 번엔 거래소가 안 나온다. 거래소별로 세 번 불러 카드에 NASDAQ/NYSE 를 붙인다.
EXCHANGES = (("nasdaq", "NASDAQ"), ("nyse", "NYSE"), ("amex", "AMEX"))
THEME_TOP = 8
THEME_MIN_MEMBERS = 5  # 국장 테마와 같은 기준. 구성종목이 적으면 등락률이 튄다


def _usd(v):
    if v >= 1e12:
        return f"${v / 1e12:.2f}T"
    return f"${v / 1e9:.1f}B" if v >= 1e9 else f"${v / 1e6:.0f}M"


def _get_screener(params, tries=3):
    """전종목 다운로드는 응답이 커서(나스닥만 4천 종목) 공용 _get 의 20초 제한이 빠듯하다. 따로 넉넉히 준다."""
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(SCREENER_URL, params=params, headers=NASDAQ_HEADERS, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
    raise last


US_CFG = {**US_MONEY_RULES, "money": _usd, "digits": 2}

# 나스닥 산업분류 -> 한국어. '기술' 같은 대분류(sector)는 쓰지 않는다 — 뭘 하는 회사인지 안 보인다.
# 없는 분류는 영어 그대로 둔다(틀린 번역보다 낫다).
INDUSTRY_KO = {
    "Computer Software: Prepackaged Software": "소프트웨어",
    "Computer Software: Programming Data Processing": "데이터·IT플랫폼",
    "EDP Services": "IT서비스",
    "Semiconductors": "반도체",
    "Electronic Components": "전자부품",
    "Computer Manufacturing": "컴퓨터·하드웨어",
    "Computer peripheral equipment": "컴퓨터 주변기기",
    "Computer Communications Equipment": "네트워크 장비",
    "Telecommunications Equipment": "통신장비",
    "Radio And Television Broadcasting And Communications Equipment": "통신·방송장비",
    "Retail: Computer Software & Peripheral Equipment": "IT유통",
    "Electronics Distribution": "전자유통",
    "Consumer Electronics/Appliances": "가전",
    "Consumer Electronics/Video Chains": "가전유통",
    "Biotechnology: Pharmaceutical Preparations": "제약",
    "Biotechnology: Biological Products (No Diagnostic Substances)": "바이오",
    "Biotechnology: Laboratory Analytical Instruments": "분석장비",
    "Biotechnology: Commercial Physical & Biological Resarch": "바이오 연구서비스",
    "Biotechnology: In Vitro & In Vivo Diagnostic Substances": "진단",
    "Biotechnology: Electromedical & Electrotherapeutic Apparatus": "의료기기(전자)",
    "Medicinal Chemicals and Botanical Products": "원료의약",
    "Other Pharmaceuticals": "제약",
    "Medical/Dental Instruments": "의료기기",
    "Medical Specialities": "특수의료",
    "Medical Electronics": "의료전자",
    "Medical/Nursing Services": "의료서비스",
    "Hospital/Nursing Management": "병원운영",
    "Misc Health and Biotechnology Services": "헬스케어서비스",
    "Ophthalmic Goods": "안과·안경",
    "Accident &Health Insurance": "건강보험",
    "Major Banks": "대형은행",
    "Commercial Banks": "상업은행",
    "Banks": "은행",
    "Savings Institutions": "저축은행",
    "Finance: Consumer Services": "소비자금융·결제",
    "Investment Bankers/Brokers/Service": "증권·IB",
    "Investment Managers": "자산운용",
    "Finance/Investors Services": "금융서비스",
    "Property-Casualty Insurers": "손해보험",
    "Life Insurance": "생명보험",
    "Specialty Insurers": "특수보험",
    "Real Estate Investment Trusts": "리츠",
    "Real Estate": "부동산",
    "Building operators": "건물운영",
    "Trusts Except Educational Religious and Charitable": "신탁",
    "Misc Corporate Leasing Services": "리스",
    "Rental/Leasing Companies": "렌탈·리스",
    "Oil & Gas Production": "원유·가스 생산",
    "Integrated oil Companies": "종합 석유",
    "Oil/Gas Transmission": "파이프라인",
    "Oilfield Services/Equipment": "유전 서비스",
    "Oil and Gas Field Machinery": "유전 장비",
    "Natural Gas Distribution": "가스 유통",
    "Coal Mining": "석탄",
    "Electric Utilities: Central": "전력 유틸리티",
    "Power Generation": "발전",
    "Water Supply": "수도",
    "Water Sewer Pipeline Comm & Power Line Construction": "전력망·인프라 건설",
    "Precious Metals": "금·귀금속",
    "Metal Mining": "금속 광업",
    "Other Metals and Minerals": "기타 광물",
    "Mining & Quarrying of Nonmetallic Minerals (No Fuels)": "비금속 광물",
    "Steel/Iron Ore": "철강",
    "Aluminum": "알루미늄",
    "Major Chemicals": "화학",
    "Specialty Chemicals": "특수화학",
    "Agricultural Chemicals": "농화학·비료",
    "Paints/Coatings": "도료",
    "Plastic Products": "플라스틱",
    "Industrial Machinery/Components": "산업기계",
    "Industrial Specialties": "산업소재",
    "Metal Fabrications": "금속가공",
    "Electrical Products": "전력기기",
    "Fluid Controls": "유체제어",
    "Precision Instruments": "정밀기기",
    "Construction/Ag Equipment/Trucks": "건설·농기계",
    "Pollution Control Equipment": "환경설비",
    "Environmental Services": "폐기물·환경",
    "Engineering & Construction": "엔지니어링·건설",
    "Building Materials": "건자재",
    "Building Products": "건축자재",
    "Homebuilding": "주택건설",
    "RETAIL: Building Materials": "건자재 유통",
    "Tools/Hardware": "공구",
    "Containers/Packaging": "포장재",
    "Forest Products": "목재",
    "Paper": "제지",
    "Military/Government/Technical": "방산",
    "Aerospace": "항공우주",
    "Ordnance And Accessories": "총포·탄약",
    "Air Freight/Delivery Services": "항공화물·택배",
    "Trucking Freight/Courier Services": "트럭운송",
    "Marine Transportation": "해운",
    "Railroads": "철도",
    "Transportation Services": "운송서비스",
    "Integrated Freight & Logistics": "물류",
    "Auto Manufacturing": "자동차",
    "Motor Vehicles": "자동차",
    "Auto Parts:O.E.M.": "자동차부품",
    "Automotive Aftermarket": "자동차 애프터마켓",
    "Retail-Auto Dealers and Gas Stations": "자동차 딜러",
    "Auto & Home Supply Stores": "자동차용품점",
    "Business Services": "비즈니스 서비스",
    "Diversified Commercial Services": "상업 서비스",
    "Professional Services": "전문 서비스",
    "Other Consumer Services": "소비자 서비스",
    "Advertising": "광고",
    "Office Equipment/Supplies/Services": "사무용품",
    "Professional and commerical equipment": "업무용 장비",
    "Hotels/Resorts": "호텔·리조트",
    "Restaurants": "외식",
    "Services-Misc. Amusement & Recreation": "레저·엔터",
    "Movies/Entertainment": "영화·엔터",
    "Recreational Games/Products/Toys": "게임·완구",
    "Broadcasting": "방송",
    "Cable & Other Pay Television Services": "케이블·유료방송",
    "Newspapers/Magazines": "신문·잡지",
    "Publishing": "출판",
    "Department/Specialty Retail Stores": "백화점·전문점",
    "Other Specialty Stores": "전문 소매",
    "Clothing/Shoe/Accessory Stores": "의류 소매",
    "Catalog/Specialty Distribution": "온라인·통신판매",
    "Retail-Drug Stores and Proprietary Stores": "드럭스토어",
    "Food Chains": "식료품 소매",
    "Food Distributors": "식품 유통",
    "Packaged Foods": "가공식품",
    "Specialty Foods": "특수식품",
    "Meat/Poultry/Fish": "육류·수산",
    "Farming/Seeds/Milling": "농업·종자",
    "Beverages (Production/Distribution)": "음료·주류",
    "Package Goods/Cosmetics": "생활용품·화장품",
    "Consumer Specialties": "소비재",
    "Apparel": "의류",
    "Garments and Clothing": "의류",
    "Shoe Manufacturing": "신발",
    "Home Furnishings": "가구·인테리어",
    "Durable Goods": "내구재",
}

# 보통주가 아닌 것들. 우선주·워런트·신주인수권·폐쇄형 펀드·채권.
# 'Common Units'(MLP) 와 미국예탁증서(ADR)는 실제 거래되는 본주라 남긴다.
_NOT_COMMON = re.compile(r"Warrant|\bRights?\b|\bFund\b|\bNotes? due\b|Debenture", re.I)
_PREFERRED = re.compile(r"Preferred Stock|Perpetual", re.I)
# 이름 뒤에 붙는 증권 종류 설명을 잘라 회사명만 남긴다. 같은 회사의 여러 주식(GOOG/GOOGL)을 묶는 데도 쓴다.
_NAME_TAIL = re.compile(
    r"\s+(Class [A-Z]\b|Series [A-Z]\b|Common Stock|Common Shares|Common Units|Ordinary Shares|"
    r"American Depositary|Capital Stock|Depositary Shares|Registered Shares|Sponsored ADR|"
    r"Units? representing|Subordinate Voting|\(The\)|\(NEW\)|\(DE\)).*$",
    re.I,
)


def _f(v):
    try:
        return float(str(v).replace("$", "").replace(",", "").replace("%", "").strip())
    except Exception:
        return 0.0


def clean_name(name):
    # 나스닥 원문에 공백이 두 칸씩 들어간 이름이 있다(Suncor Energy  Inc.). 한 칸으로 맞춘다.
    name = " ".join((name or "").split())
    base = _NAME_TAIL.sub("", name)
    return base.rstrip(" ,.-") or name


def _is_common(row):
    name, sym = row.get("name") or "", row.get("symbol") or ""
    if "^" in sym:  # 나스닥 표기에서 ^ 는 우선주
        return False
    if _NOT_COMMON.search(name):
        return False
    if _PREFERRED.search(name) and "American Depositary" not in name:
        return False  # GOOGN·SMCIP 같은 전환우선주 예탁증서. 단 ITUB 처럼 본주가 ADR 인 건 남긴다
    return True


def base_quality_reject_us(s):
    q = US_BASE_QUALITY
    if s["trading_value"] < q["min_trading_value"]:
        return f"거래대금 {_usd(s['trading_value'])} ($50M 미만)"
    if s["market_cap"] < q["min_market_cap"]:
        return f"시가총액 {_usd(s['market_cap'])} ($2B 미만)"
    if s["close"] < q["min_price"]:
        return f"주가 ${s['close']:.2f} ($5 미만)"
    return None


def fetch_us_universe():
    """(체급 통과, 탈락, 보통주 전체). 보통주 전체는 주도 업종 계산에 쓴다."""
    commons = []
    for param, label in EXCHANGES:
        try:
            data = _get_screener({"tableonly": "true", "download": "true", "exchange": param})
        except Exception as e:
            print(f"[WARN] 미장 유니버스 조회 실패 {label}: {e}")
            continue
        for r in (data.get("data") or {}).get("rows") or []:
            if not _is_common(r):
                continue
            price, volume = _f(r.get("lastsale")), _f(r.get("volume"))
            # 야후는 클래스주를 BRK-B 로 쓴다(나스닥은 BRK/B). 차트 파일 이름으로도 쓰므로 / 를 없앤다.
            sym = r["symbol"].strip().replace("/", "-")
            commons.append(
                {
                    "code": sym,
                    "symbol": sym,
                    "name": clean_name(r.get("name")),
                    "market": label,
                    "close": price,
                    "change_percent": _f(r.get("pctchange")),
                    "volume": volume,
                    # 나스닥 거래량은 전체 시장 합산이다(야후와 1% 안에서 일치 확인).
                    "trading_value": price * volume,
                    "market_cap": _f(r.get("marketCap")),
                    "industry": (r.get("industry") or "").strip(),
                }
            )

    # 같은 회사의 여러 주식(GOOG/GOOGL, BRK-A/BRK-B)이 한 섹션에 나란히 뜨지 않게 거래대금 큰 쪽만 남긴다.
    best = {}
    for s in commons:
        key = s["name"].lower()
        if key not in best or s["trading_value"] > best[key]["trading_value"]:
            best[key] = s
    commons = list(best.values())

    passed, rejected = [], []
    for s in commons:
        reason = base_quality_reject_us(s)
        (rejected if reason else passed).append({**s, "reason": reason} if reason else s)
    print(f"[INFO] 미장 0단계: 보통주 {len(commons)} -> 체급 통과 {len(passed)} / 탈락 {len(rejected)}")
    return passed, rejected, commons


def industry_label(industry):
    return INDUSTRY_KO.get(industry, industry)


def industry_leaders(commons, top=THEME_TOP):
    """오늘 가장 센 업종. 국장의 '주도 테마'와 같은 모양으로 만든다.
    시총 $2B 미만은 뺀다 — 소형주 몇 개가 튀면 업종 평균이 왜곡된다."""
    groups = defaultdict(list)
    for s in commons:
        if s["industry"] and s["market_cap"] >= US_BASE_QUALITY["min_market_cap"]:
            groups[s["industry"]].append(s["change_percent"])
    rows = [
        {
            "name": industry_label(ind),
            "change_percent": round(sum(v) / len(v), 2),
            "total": len(v),
            "rise": sum(1 for x in v if x > 0),
        }
        for ind, v in groups.items()
        if len(v) >= THEME_MIN_MEMBERS
    ]
    return sorted(rows, key=lambda x: -x["change_percent"])[:top]


def _us_section_meta(sid):
    """섹션 설명·범례·청산 규칙. 수급이 필요한 유형은 범례에서 빼고, 종가베팅은 애프터마켓 진입으로 바꾼다."""
    over = US_SECTION_OVERRIDES.get(sid, {})
    gone = US_NO_FLOW_TYPES.get(sid, [])
    legend = [t for t in SECTION_LEGEND[sid] if not any(t.startswith(f"유형 {g} ") for g in gone)]
    return over.get("desc", SECTION_DESC[sid]), legend, over.get("exits", SECTION_EXITS[sid])


def probe_us_sources():
    checks = {}
    for key, fn in (
        ("nasdaq_screener", lambda: len((((_get_screener({"tableonly": "true", "limit": 5}) or {}).get("data") or {}).get("table") or {}).get("rows") or [])),
        ("yahoo_daily", lambda: len(_yahoo_chart("SPY", "1mo", "1d").get("timestamp") or [])),
    ):
        try:
            checks[key] = f"OK / {fn()}건"
        except Exception as e:
            checks[key] = f"실패: {type(e).__name__} {e}"
        print(f"[INFO] 미장 출처 점검 {key}: {checks[key]}")
    return checks


def build_us_screening(charts_dir=None):
    probe = probe_us_sources()
    passed, rejected, commons = fetch_us_universe()
    if not passed:
        raise RuntimeError(f"미장 0단계를 통과한 종목이 없습니다 — 출처 점검: {probe}")

    scanned, failures = scan_daily(passed, digits=2)

    candidates = []
    for res in scanned:
        s, bars = res["stock"], res["bars"]
        label = industry_label(s["industry"]) if s["industry"] else ""
        base = {
            "code": s["code"],
            "name": s["name"],
            "market": s["market"],
            "tags": [label] if label else [],
            "trading_value_usd": round(s["trading_value"]),
            "market_cap_usd": round(s["market_cap"]),
            **{k: res["quote"][k] for k in ("close", "change_percent", "volume")},
            "url": f"https://finance.yahoo.com/quote/{s['code']}",
            "_rank": s["trading_value"],
        }
        candidates.append((base, bars, s, []))  # 수급 없음 -> 수급 킬스위치·유형 A/C 는 자연히 판정되지 않는다

    sections, dropped = assign_sections(candidates, US_CFG)

    # 섹션이 정한 청산 규칙을 미장용으로 바꿔 끼운다(종가베팅 -> 애프터마켓 진입).
    for sid in SECTION_ORDER:
        _, _, exits = _us_section_meta(sid)
        for e in sections[sid]:
            e["exits"] = exits

    picked = [e for sid in SECTION_ORDER for e in sections[sid]]
    charts = write_chart_files(
        [{"code": e["code"], "name": e["name"], "symbol": e["code"]} for e in picked],
        charts_dir,
        digits=2,
    )

    print(
        "[INFO] 미장 스크리닝: "
        + " / ".join(f"{SECTION_NAME[sid]} {len(sections[sid])}" for sid in SECTION_ORDER)
        + f" / 탈락 {dropped} / 조회실패 {failures}"
    )
    out_sections = []
    for sid in SECTION_ORDER:
        desc, legend, _ = _us_section_meta(sid)
        out_sections.append(
            {"id": sid, "name": SECTION_NAME[sid], "desc": desc, "legend": legend, "items": sections[sid]}
        )
    return {
        "market": "us",
        "probe": probe,
        "chart_count": charts,
        "as_of_trading_day": scanned[0]["quote"]["trading_day"] if scanned else "",
        "universe_count": len(passed) + len(rejected),
        "base_passed": len(passed),
        "dropped": dropped,
        "fetch_failures": failures,
        "theme_leaders": industry_leaders(commons),
        "note": US_NO_FLOW_NOTE,
        "sections": out_sections,
    }
