"""기술적 분석 스크리닝 엔진.

screening_rules.py 에 정의한 규칙을 실제 시세/수급/테마 데이터에 적용해
docs/data/screening.json 과 종목별 차트 파일을 만든다.

호출량을 줄이려고 단계를 나눈다. 특히 0단계가 핵심이다.
네이버 목록 API 한 번이 거래대금·시가총액·종가를 다 주기 때문에,
야후를 한 번도 부르지 않고 전종목의 3/4 을 먼저 걷어낼 수 있다.

  0단계  네이버 목록 -> 체급 미달(거래대금·시총·주가) 원천 배제
  1단계  살아남은 종목만 일봉 조회 -> 킬스위치 + 섹션 판정
  2단계  후보만 수급 조회 -> 무수급 배제, 수급 관련 킬스위치/조건
  3단계  테마 맵 작성 -> 종목별 태그와 오늘의 주도 테마
"""
import datetime
import json
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from screening_rules import (
    ATTEMPT_GAP_DAYS,
    BASE_QUALITY,
    KILL_SWITCHES,
    MAX_PER_SECTION,
    SECTION_DESC,
    SECTION_EXITS,
    SECTION_LEGEND,
    SECTION_NAME,
    SECTION_ORDER,
    SECTIONS,
)

UNIVERSE_URL = "https://m.stock.naver.com/api/stocks/marketValue/{market}"
TREND_URL = "https://m.stock.naver.com/api/stock/{code}/trend"
THEME_LIST_URL = "https://m.stock.naver.com/api/stocks/theme"
THEME_MEMBERS_URL = "https://m.stock.naver.com/api/stocks/theme/{no}"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://m.stock.naver.com/",
}
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-dashboard-bot/1.0)"}

WORKERS = 8
PAGE_SIZE = 100  # 네이버 목록 API 상한. 200 이상은 400 으로 거절된다.
MIN_DAILY_BARS = 260
CHART_BARS = 620  # 480일선을 화면 왼쪽 끝부터 그리려면 이만큼 필요하다
THEME_PAGES = 3  # 264개 그룹 = 3페이지
MAX_TAGS = 2  # 태그가 너무 많으면 오히려 뭘 하는 회사인지 흐려진다
MIN_ORGAN_INFLOW = 1_000_000_000  # 섹션3 유형C 의 '기관 지속 순매수' 5일 누적 최소 금액(10억)


# ──────────────────────────────────────────────────────────────────────────
# 공통
# ──────────────────────────────────────────────────────────────────────────
def _get(url, params=None, headers=None, tries=3):
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers or NAVER_HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if attempt < tries - 1:
                time.sleep(0.8 * (attempt + 1))
    raise last


def _num(v):
    try:
        return int(str(v).replace(",", "").replace("+", ""))
    except Exception:
        return 0


def ma(values, n, offset=0):
    end = len(values) - offset
    if end < n:
        return None
    return sum(values[end - n : end]) / n


def pct(a, b):
    return (a - b) / b * 100 if b else 0.0


def body_ratio(o, h, l, c):
    rng = h - l
    return abs(c - o) / rng if rng > 0 else 1.0


def upper_tail(o, h, l, c):
    rng = h - l
    return (h - max(o, c)) / rng if rng > 0 else 0.0


def lower_tail(o, h, l, c):
    rng = h - l
    return (min(o, c) - l) / rng if rng > 0 else 0.0


# ──────────────────────────────────────────────────────────────────────────
# 0단계 — 체급 필터 (야후를 부르기 전에 여기서 대부분 걸러진다)
# ──────────────────────────────────────────────────────────────────────────
def _is_common_stock(row):
    """ETF·ETN·우선주 제거. 안 하면 대상의 3할이 지수 상품이 된다."""
    if row.get("stockEndType") != "stock":
        return False
    name = (row.get("stockName") or "").strip()
    return not (name.endswith("우") or (len(name) > 2 and name[-2] == "우"))


def fetch_universe():
    """전종목을 체급 지표와 함께 가져와 0단계를 바로 적용한다."""
    passed, rejected = [], []
    for market, suffix in (("KOSPI", "KS"), ("KOSDAQ", "KQ")):
        page = 1
        while True:
            try:
                data = _get(UNIVERSE_URL.format(market=market), {"page": page, "pageSize": PAGE_SIZE})
            except Exception as e:
                print(f"[WARN] 유니버스 조회 실패 {market} p{page}: {e}")
                break
            rows = data.get("stocks") or []
            if not rows:
                break

            for r in rows:
                if not _is_common_stock(r):
                    continue
                s = {
                    "code": r["itemCode"],
                    "name": r["stockName"],
                    "market": market,
                    "symbol": f"{r['itemCode']}.{suffix}",
                    "close": _num(r.get("closePriceRaw")),
                    "trading_value": _num(r.get("accumulatedTradingValueRaw")),
                    "market_cap": _num(r.get("marketValueRaw")),
                    "volume": _num(r.get("accumulatedTradingVolumeRaw")),
                }
                reason = base_quality_reject(s)
                (rejected if reason else passed).append({**s, "reason": reason} if reason else s)

            if page * PAGE_SIZE >= int(data.get("totalCount") or 0):
                break
            page += 1
            time.sleep(0.05)

    print(f"[INFO] 0단계: 체급 통과 {len(passed)}종목 / 탈락 {len(rejected)}종목")
    return passed, rejected


def base_quality_reject(s):
    """체급 미달이면 사유 문자열, 통과면 None. 수급 조건(4번)은 2단계에서 본다."""
    if s["trading_value"] < BASE_QUALITY["min_trading_value"]:
        return f"거래대금 {s['trading_value'] / 1e8:.0f}억 (100억 미만)"
    if s["market_cap"] < BASE_QUALITY["min_market_cap"]:
        return f"시가총액 {s['market_cap'] / 1e8:.0f}억 (1,000억 미만)"
    if s["close"] < BASE_QUALITY["min_price"]:
        return f"주가 {s['close']:,}원 (3,000원 미만)"
    return None


# ──────────────────────────────────────────────────────────────────────────
# 시세·수급·테마 수집
# ──────────────────────────────────────────────────────────────────────────
def _yahoo_chart(symbol, rng, interval):
    last = None
    for attempt in range(3):
        try:
            r = requests.get(
                CHART_URL.format(symbol=symbol),
                params={"range": rng, "interval": interval},
                headers=YAHOO_HEADERS,
                timeout=20,
            )
            r.raise_for_status()
            return r.json()["chart"]["result"][0]
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
    raise last


def _bars_from(result, limit=None):
    stamps = result.get("timestamp") or []
    q = (result.get("indicators", {}).get("quote") or [{}])[0]
    keys = ("open", "high", "low", "close", "volume")
    series = {k: (q.get(k) or []) for k in keys}
    bars = {"date": [], **{k: [] for k in keys}}
    for i, ts in enumerate(stamps):
        # 야후는 배열 길이가 timestamp 와 어긋나게 오는 경우가 있어 길이를 같이 본다
        row = [series[k][i] if i < len(series[k]) else None for k in keys]
        if any(v is None for v in row):
            continue
        bars["date"].append(datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat())
        for k, v in zip(keys, row):
            bars[k].append(float(v))
    if limit:
        for k in bars:
            bars[k] = bars[k][-limit:]
    return bars


def fetch_daily(symbol):
    """조회 실패는 예외로 올리고, 상장 기간이 짧아 장기선을 못 만드는 건 None 으로 구분한다.
    둘을 뭉뚱그리면 실패 건수가 부풀어 진짜 장애를 못 알아본다."""
    bars = _bars_from(_yahoo_chart(symbol, "3y", "1d"))
    return bars if len(bars["close"]) >= MIN_DAILY_BARS else None


def fetch_flow(code):
    """외국인·기관·개인 일별 순매수 수량. 최신 거래일이 앞에 온다."""
    try:
        rows = _get(TREND_URL.format(code=code))
    except Exception:
        return []
    return [
        {
            "date": r.get("bizdate", ""),
            "foreign": _num(r.get("foreignerPureBuyQuant")),
            "organ": _num(r.get("organPureBuyQuant")),
            "individual": _num(r.get("individualPureBuyQuant")),
        }
        for r in rows
    ]


def fetch_theme_groups():
    """테마 그룹 목록(이름·등락률·구성종목수). 호출 3번이면 끝나 매시간 돌려도 부담이 없다."""
    groups = []
    for page in range(1, THEME_PAGES + 1):
        try:
            data = _get(THEME_LIST_URL, {"page": page, "pageSize": PAGE_SIZE})
        except Exception as e:
            print(f"[WARN] 테마 목록 조회 실패 p{page}: {e}")
            break
        rows = data.get("groups") or []
        if not rows:
            break
        groups.extend(rows)
    return groups


def theme_leaders(groups, top=8):
    """오늘 가장 센 테마. 구성종목이 너무 적은 그룹은 등락률이 튀어서 뺀다."""
    return sorted(
        (
            {
                "name": g["name"],
                "change_percent": round(float(g.get("changeRate") or 0), 2),
                "total": g.get("totalCount", 0),
                "rise": g.get("riseCount", 0),
            }
            for g in groups
            if g.get("totalCount", 0) >= 5
        ),
        key=lambda x: -x["change_percent"],
    )[:top]


def fetch_theme_map():
    """종목코드 -> 테마명 목록. 투자자가 '이 회사가 뭐 하는 곳인지' 바로 알 수 있게 태그를 붙인다.
    구성종목까지 받아야 해서 264번을 더 부르므로, 스크리닝을 돌릴 때만 쓴다."""
    groups = fetch_theme_groups()

    def members(g):
        try:
            data = _get(THEME_MEMBERS_URL.format(no=g["no"]))
            return g, [s["itemCode"] for s in (data.get("stocks") or [])]
        except Exception:
            return g, []

    tag_map = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for g, codes in pool.map(members, groups):
            for c in codes:
                tag_map.setdefault(c, []).append(g["name"])

    print(f"[INFO] 테마 {len(groups)}개 / 태그가 붙은 종목 {len(tag_map)}개")
    return tag_map, theme_leaders(groups)


# ──────────────────────────────────────────────────────────────────────────
# 킬 스위치
# ──────────────────────────────────────────────────────────────────────────
def _long_line_breakout(b, trading_value):
    """오늘 240·480일선을 장대양봉 종가로 돌파했는가. 돌파선 이름을 돌려준다.
    역배열 킬스위치의 예외 판정과 섹션1 유형C 양쪽에서 쓴다."""
    c, o, v = b["close"], b["open"], b["volume"]
    if not (c[-1] > o[-1] and pct(c[-1], o[-1]) >= 3.0):
        return None
    if v[-2] <= 0 or v[-1] < v[-2] * (1 + KILL_SWITCHES[0]["exception_params"]["min_volume_ratio"]):
        return None
    if trading_value < KILL_SWITCHES[0]["exception_params"]["min_trading_value"]:
        return None
    for n, label in ((480, "480일선"), (240, "240일선")):
        line = ma(c, n)
        if line and c[-2] < line <= c[-1]:
            return label
    return None


def kill_checks(b, s, flow):
    """걸리면 사유 문자열, 통과면 None."""
    c, o, h, l, v = b["close"], b["open"], b["high"], b["low"], b["volume"]
    close = c[-1]
    avg20v = ma(v, 20, offset=1) or 0

    # 1. 역배열 침체 — 바닥권 돌파 종목은 예외로 살린다
    below = [n for n in (240, 480) if (ma(c, n) or 0) and close < ma(c, n)]
    if below and not _long_line_breakout(b, s["trading_value"]):
        return f"역배열 침체 ({below[0]}일선 아래)"

    # 2. 메이저 수급 이탈 — 3일 연속 외인·기관 동반 순매도
    if len(flow) >= 3 and all(d["foreign"] < 0 and d["organ"] < 0 for d in flow[:3]):
        return "3일 연속 외국인·기관 동반 순매도"

    # 3. 단기 과열 음봉 — 5일선에서 크게 벌어진 상태에서 대량 장대음봉
    p = KILL_SWITCHES[2]["params"]
    ma5_prev = ma(c, 5, offset=1)
    if ma5_prev and pct(c[-2], ma5_prev) >= p["min_gap_pct"]:
        if close < o[-1] and pct(close, c[-2]) <= -p["min_drop_pct"] and avg20v and v[-1] >= avg20v * p["min_volume_ratio"]:
            return "5일선 이격 과열 + 대량 장대음봉"

    # 4. 추세 마지노선 붕괴 — 60일선을 거래량 실린 음봉으로 이탈
    ma60 = ma(c, 60)
    if ma60 and c[-2] >= ma60 > close and close < o[-1]:
        if avg20v and v[-1] >= avg20v * KILL_SWITCHES[3]["params"]["min_volume_ratio"]:
            return "60일선 거래량 실린 음봉 이탈"
    return None


def no_major_flow(flow, close):
    """외국인+기관 순매수 거래대금이 3억 미만이면 무수급으로 본다.
    API 가 금액을 안 줘서 순매수 수량 x 종가로 환산한다."""
    if not flow:
        return "수급 데이터 없음"
    value = (abs(flow[0]["foreign"]) + abs(flow[0]["organ"])) * close
    if value < BASE_QUALITY["min_major_flow_value"]:
        return f"외인·기관 순매수 {value / 1e8:.1f}억 (3억 미만)"
    return None


# ──────────────────────────────────────────────────────────────────────────
# 섹션 판정
# ──────────────────────────────────────────────────────────────────────────
def match_closing_bet(b, s, flow):
    c, o, h, l, v = b["close"], b["open"], b["high"], b["low"], b["volume"]
    close, open_, high, low = c[-1], o[-1], h[-1], l[-1]
    p = SECTIONS[0]["params"]

    # C: 바닥권 장기선 대량거래 돌파 (장기선 첫 돌파는 이 섹션 최우선)
    if s["trading_value"] >= p["type_c_min_trading_value"] and v[-2] > 0:
        if v[-1] >= v[-2] * (1 + p["type_c_min_volume_ratio"]) and close > open_ and pct(close, open_) >= 3.0:
            for n, label in ((480, "480일선"), (240, "240일선")):
                line = ma(c, n)
                if line and c[-2] < line <= close:
                    return "C", f"{label} 장대양봉 종가 돌파 · 거래대금 {s['trading_value'] / 1e8:.0f}억 · 거래량 전일 {v[-1] / v[-2] * 100:.0f}%"

    # A: 메이저 수급 집중 + 윗꼬리 거의 없는 양봉 고가 마감
    if close > open_ and upper_tail(open_, high, low, close) <= 0.12 and high > 0 and (high - close) / high <= 0.01:
        if flow and (flow[0]["foreign"] > 0 or flow[0]["organ"] > 0):
            big = max(flow[0]["foreign"], flow[0]["organ"]) * close
            if big >= 1_000_000_000:  # 10억 이상이어야 '대량 순매수'로 본다
                who = "외국인" if flow[0]["foreign"] >= flow[0]["organ"] else "기관"
                return "A", f"{who} 순매수 {big / 1e8:.0f}억 집중 + 윗꼬리 없는 양봉 고가 마감"

    # B: 전일 대량 급등 후 당일 거래급감 도지 + 10·20일선 지지
    if len(c) >= 3 and v[-2] > 0 and pct(c[-2], o[-2]) >= 5 and upper_tail(o[-2], h[-2], l[-2], c[-2]) <= 0.4:
        if v[-1] <= v[-2] * 0.5 and body_ratio(open_, high, low, close) <= 0.15:
            for n, label in ((10, "10일선"), (20, "20일선")):
                line = ma(c, n)
                if line and abs(close - line) / line <= 0.03:
                    return "B", f"전일 급등 후 거래량 {v[-1] / v[-2] * 100:.0f}% 급감 도지 + {label} 지지"
    return None, None


def match_swing_pullback(b, s, flow):
    c, v, o, h, l = b["close"], b["volume"], b["open"], b["high"], b["low"]
    p = SECTIONS[1]["params"]
    win = p["rally_window"] + p["pullback_days"][1]  # 급등 구간 + 조정 구간을 모두 담을 만큼
    if len(c) < win + 25:
        return None, None

    seg_c, seg_v, seg_o = c[-win:], v[-win:], o[-win:]
    peak_i = max(range(len(seg_c)), key=lambda i: seg_c[i])
    bars_since_peak = len(seg_c) - 1 - peak_i
    lo_d, hi_d = p["pullback_days"]
    if not lo_d <= bars_since_peak <= hi_d:
        return None, None

    # 1차 상승 파동: 고점 직전 저점 대비 상승률, 또는 고점일이 거래대금 큰 장대양봉이면 인정
    trough = min(seg_c[: peak_i + 1]) if peak_i > 0 else seg_c[0]
    rally = pct(seg_c[peak_i], trough) if trough else 0
    peak_value = seg_c[peak_i] * seg_v[peak_i]  # 그날 거래대금(종가 x 거래량으로 근사)
    big_candle = (
        peak_value >= p["big_candle_value"]
        and seg_c[peak_i] > seg_o[peak_i]
        and pct(seg_c[peak_i], seg_o[peak_i]) >= 3.0
    )
    if rally < p["min_rally_pct"] and not big_candle:
        return None, None

    # 거래량 마름: 급등일 대비 절반 이하 '또는' 20일 평균 이하
    peak_v = seg_v[peak_i]
    calm_v = sum(v[-3:]) / 3
    avg20v = ma(v, 20) or 0
    dried = (peak_v and calm_v <= peak_v * p["max_volume_ratio"]) or (avg20v and calm_v <= avg20v)
    if not dried:
        return None, None
    drop = (1 - calm_v / peak_v) * 100 if peak_v else 0

    close, open_, high, low = c[-1], o[-1], h[-1], l[-1]
    near = lambda line: line and abs(close - line) / line <= 0.03
    held = lambda line: line and all(abs(l[-i] - line) / line <= 0.035 for i in (1, 2, 3))

    lead = f"1차 상승 +{rally:.0f}%" if rally >= p["min_rally_pct"] else f"거래대금 {peak_value / 1e8:.0f}억 장대양봉"
    tail = f"고점 후 {bars_since_peak}거래일 조정 · 거래량 {drop:.0f}% 감소"

    ma20, ma10, ma60 = ma(c, 20), ma(c, 10), ma(c, 60)
    if near(ma20) and ((close < open_ and lower_tail(open_, high, low, close) >= 0.4) or held(ma20)):
        return "20일선", f"{lead} · {tail} · 20일선 지지"
    if near(ma10) and body_ratio(open_, high, low, close) <= 0.15:
        return "10일선", f"{lead} · {tail} · 10일선 도지 지지"
    if near(ma60) and held(ma60) and ma20 and close < ma20:
        return "60일선", f"{lead} · {tail} · 20일선 이탈 후 60일선 3일 저점 지지"
    return None, None


def match_trend_rally(b, s, flow):
    c, v, o, h = b["close"], b["volume"], b["open"], b["high"]
    close = c[-1]
    avg20v = ma(v, 20, offset=1) or 0
    ma480, ma240 = ma(c, 480), ma(c, 240)

    # A: 완전 정배열 + 480일선 안착 + 신고가 영역
    mas = [ma(c, n) for n in (5, 10, 20, 60, 120, 240)]
    if all(x is not None for x in mas) and all(mas[i] > mas[i + 1] for i in range(len(mas) - 1)):
        if ma480 and close > ma480:
            window = h[-252:-1] if len(h) > 252 else h[:-1]
            if window and close >= max(window) * 0.98:
                return "A", "완전 정배열 + 480일선 안착 + 52주 신고가 영역"

    # B: 480일선 돌파 2회 실패 후 삼세판 돌파, 그 뒤 우상향
    if ma480 and close > ma480:
        rejects = []
        for i in range(max(0, len(c) - 120), len(c) - 1):
            line_i = ma(c, 480, offset=len(c) - 1 - i)
            if line_i and c[i] < line_i <= h[i]:
                rejects.append(i)
        attempts = sum(1 for k, d in enumerate(rejects) if k == 0 or d - rejects[k - 1] > ATTEMPT_GAP_DAYS)
        ma10 = ma(c, 10)
        if attempts >= 2 and ma10 and close > ma10 and avg20v and v[-1] >= avg20v * 1.2:
            return "B", f"480일선 돌파 {attempts}회 실패 후 재돌파 · 상단 우상향"

    # C: 기관 지속 순매수 + 240일선 돌파 후 10·20일선 지지 저점 상승
    if ma240 and close > ma240 and flow and len(flow) >= 3:
        organ_days = sum(1 for d in flow[:5] if d["organ"] > 0)
        ma10, ma20 = ma(c, 10), ma(c, 20)
        if organ_days >= 3 and ma10 and ma20 and close > ma10 > ma20:
            total = sum(d["organ"] for d in flow[:5]) * close
            # 금액 하한이 없으면 '5일 합계 306주 = 322만원' 짜리도 지속 순매수로 통과한다.
            # 실제로 광전자가 그렇게 뽑혔다. 기관 자금이 들어왔다고 하려면 규모가 있어야 한다.
            if total >= MIN_ORGAN_INFLOW:
                return "C", f"기관 5일 중 {organ_days}일 순매수({total / 1e8:.0f}억) + 240일선 위 10·20일선 지지"
    return None, None


MATCHERS = [
    ("CLOSING_BET", match_closing_bet),
    ("TREND_RALLY", match_trend_rally),
    ("SWING_PULLBACK", match_swing_pullback),
]


# ──────────────────────────────────────────────────────────────────────────
# 차트 파일
# ──────────────────────────────────────────────────────────────────────────
def write_chart_files(stocks, charts_dir):
    """목록에 오른 종목의 일봉을 종목별 파일로 저장한다.
    브라우저에서 야후를 직접 부르면 CORS 로 막혀서(배포본에서 확인), 배치가 미리 받아둬야 한다."""
    if charts_dir is None:
        return 0
    charts_dir.mkdir(parents=True, exist_ok=True)

    def one(s):
        try:
            bars = _bars_from(_yahoo_chart(s["symbol"], "3y", "1d"), limit=CHART_BARS)
        except Exception:
            return None
        rows = [
            [bars["date"][i], round(bars["open"][i]), round(bars["high"][i]),
             round(bars["low"][i]), round(bars["close"][i]), round(bars["volume"][i])]
            for i in range(len(bars["close"]))
        ]
        return {"code": s["code"], "name": s["name"], "bars": rows} if rows else None

    written = set()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for payload in pool.map(one, stocks):
            if not payload:
                continue
            path = charts_dir / f"{payload['code']}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            written.add(path.name)

    for old in charts_dir.glob("*.json"):
        if old.name not in written:
            old.unlink()  # 목록에서 빠진 종목 차트는 지운다. 안 그러면 저장소에 쌓인다.

    print(f"[INFO] 차트 파일 {len(written)}개 저장")
    return len(written)


# ──────────────────────────────────────────────────────────────────────────
# 조립
# ──────────────────────────────────────────────────────────────────────────
def probe_sources():
    """어느 출처가 살아 있는지 먼저 찔러본다. 워크플로 로그를 볼 수 없어서
    실패 원인을 알려면 결과 파일에 남기는 수밖에 없다."""
    checks = {}
    for key, fn in (
        ("naver_universe", lambda: len((_get(UNIVERSE_URL.format(market="KOSPI"), {"page": 1, "pageSize": 5}) or {}).get("stocks") or [])),
        ("naver_trend", lambda: len(_get(TREND_URL.format(code="005930")) or [])),
        ("naver_theme", lambda: len((_get(THEME_LIST_URL, {"page": 1, "pageSize": 5}) or {}).get("groups") or [])),
        ("yahoo_daily", lambda: len(_yahoo_chart("005930.KS", "1mo", "1d").get("timestamp") or [])),
    ):
        try:
            checks[key] = f"OK / {fn()}건"
        except Exception as e:
            checks[key] = f"실패: {type(e).__name__} {e}"
        print(f"[INFO] 출처 점검 {key}: {checks[key]}")
    return checks


def _scan_one(s):
    try:
        bars = fetch_daily(s["symbol"])
    except Exception:
        return {"stock": s, "failed": True}
    if bars is None:
        return None  # 상장 기간이 짧아 장기 이평선을 만들 수 없는 종목

    c, v = bars["close"], bars["volume"]
    return {
        "stock": s,
        "failed": False,
        "bars": bars,
        "quote": {
            "close": round(c[-1]),
            "change_percent": round(pct(c[-1], c[-2]), 2),
            "volume": int(v[-1]),
            "trading_day": bars["date"][-1],
        },
    }


def build_screening(today=None, charts_dir=None):
    today = today or datetime.date.today()
    probe = probe_sources()

    passed, rejected = fetch_universe()
    if not passed:
        raise RuntimeError(f"0단계를 통과한 종목이 없습니다 — 출처 점검: {probe}")

    # 1단계 — 체급을 통과한 종목만 일봉 조회
    scanned, failures = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for res in pool.map(_scan_one, passed):
            if res is None:
                continue
            if res["failed"]:
                failures += 1
            else:
                scanned.append(res)
    print(f"[INFO] 1단계: 일봉 확보 {len(scanned)}종목 (조회 실패 {failures})")
    if failures > len(passed) * 0.2:
        print(f"[WARN] 조회 실패가 {failures}건이다 — 결과가 상당수 빠졌을 수 있다")

    # 2단계 — 수급 조회 (전량. 킬스위치2와 0단계 4번 모두 수급이 있어야 판정된다)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        flows = list(pool.map(lambda x: fetch_flow(x["stock"]["code"]), scanned))

    # 3단계 — 테마 태그
    tag_map, leaders = fetch_theme_map()

    sections = {sid: [] for sid in SECTION_ORDER}
    dropped = 0
    for res, flow in zip(scanned, flows):
        s, bars = res["stock"], res["bars"]
        base = {
            "code": s["code"],
            "name": s["name"],
            "market": s["market"],
            "tags": tag_map.get(s["code"], [])[:MAX_TAGS],
            "trading_value_eok": round(s["trading_value"] / 1e8),
            "market_cap_eok": round(s["market_cap"] / 1e8),
            **{k: res["quote"][k] for k in ("close", "change_percent", "volume")},
            "url": f"https://m.stock.naver.com/domestic/stock/{s['code']}/total",
        }
        if flow:
            base["flow"] = {
                "date": flow[0]["date"],
                "foreign": flow[0]["foreign"],
                "organ": flow[0]["organ"],
                "individual": flow[0]["individual"],
            }

        # 체급 미달·킬스위치는 즉시 영구 탈락이다. 결과에 노출하지 않는다.
        # (예전엔 '진입 금지' 목록으로 보여줬는데, 안 살 종목을 보여줄 이유가 없다.)
        blocked = no_major_flow(flow, res["quote"]["close"]) or kill_checks(bars, s, flow)
        if blocked:
            dropped += 1
            continue

        for sid, fn in MATCHERS:
            kind, reason = fn(bars, s, flow)
            if reason:
                sections[sid].append(
                    {**base, "section": sid, "section_name": SECTION_NAME[sid], "type": kind,
                     "reason": reason, "exits": SECTION_EXITS[sid]}
                )
                break  # 중복 배정 금지. MATCHERS 순서가 곧 우선순위다.

    # 섹션 안에서 거래대금순으로만 10개를 자르면, 조건이 느슨한 유형이 자리를 다 먹는다.
    # 실제로 대시세 추세가 전부 유형 C 로만 채워져 A(정배열 신고가)·B(480일선 삼세판)가
    # 매칭돼도 11위 밖으로 밀려 안 보였다. 유형별로 돌아가며 뽑아 자리를 보장한다.
    for sid in sections:
        by_type = {}
        for e in sorted(sections[sid], key=lambda x: -x["trading_value_eok"]):
            by_type.setdefault(e["type"], []).append(e)
        picked_sec, order = [], sorted(by_type)
        while len(picked_sec) < MAX_PER_SECTION and any(by_type[t] for t in order):
            for t in order:
                if by_type[t] and len(picked_sec) < MAX_PER_SECTION:
                    picked_sec.append(by_type[t].pop(0))
        sections[sid] = sorted(picked_sec, key=lambda x: -x["trading_value_eok"])

    picked = [e for sid in SECTION_ORDER for e in sections[sid]]
    charts = write_chart_files(
        [{"code": e["code"], "name": e["name"], "symbol": f"{e['code']}.{'KS' if e['market'] == 'KOSPI' else 'KQ'}"}
         for e in picked],
        charts_dir,
    )

    print(
        "[INFO] 스크리닝: "
        + " / ".join(f"{SECTION_NAME[sid]} {len(sections[sid])}" for sid in SECTION_ORDER)
        + f" / 탈락 {dropped} / 조회실패 {failures}"
    )
    return {
        "probe": probe,
        "chart_count": charts,
        "as_of_trading_day": scanned[0]["quote"]["trading_day"] if scanned else "",
        "universe_count": len(passed) + len(rejected),
        "base_passed": len(passed),
        "dropped": dropped,
        "fetch_failures": failures,
        "theme_leaders": leaders,
        "sections": [
            {
                "id": sid,
                "name": SECTION_NAME[sid],
                "desc": SECTION_DESC[sid],
                "legend": SECTION_LEGEND[sid],
                "items": sections[sid],
            }
            for sid in SECTION_ORDER
        ],
    }
