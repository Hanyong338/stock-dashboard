"""기술적 분석 스크리닝 엔진.

scripts/screening_rules.py 에 정의한 규칙을 실제 시세/수급 데이터에 적용해
docs/data/screening.json 을 만든다. 뉴스·테마는 일절 쓰지 않는다.

전종목(약 2,900개)을 매번 훑으므로 3단계로 나눠 호출량을 줄인다.
  1단계  전종목 일봉 -> 전략 매칭 (여기서 수백 개로 줄어든다)
  2단계  통과 종목만 수급 조회 -> 킬스위치4 판정
  3단계  통과 종목만 1분봉 조회 -> 킬스위치1의 분봉 부분 판정
"""
import datetime
import json
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from screening_rules import STRATEGIES

UNIVERSE_URL = "https://m.stock.naver.com/api/stocks/marketValue/{market}"
TREND_URL = "https://m.stock.naver.com/api/stock/{code}/trend"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://m.stock.naver.com/",
}
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-dashboard-bot/1.0)"}

DAILY_WORKERS = 8  # 야후를 동시에 몇 개까지 두드릴지. 더 올리면 429 가 늘어난다.
PAGE_SIZE = 100  # 네이버 목록 API 상한. 200 이상은 400 으로 거절된다.
MIN_DAILY_BARS = 260  # 240일선을 만들려면 최소 이만큼은 있어야 한다
MAX_PER_STRATEGY = 6  # 한 전략이 목록을 독점하지 않도록 자른다
MAX_WATCH_PER_STRATEGY = 4  # 감시 후보도 마찬가지. 안 두면 갭 후보가 목록을 다 먹는다
ATTEMPT_GAP_DAYS = 5  # 이만큼 떨어져 있어야 '다른 시도'로 센다 (붙어 있으면 한 번의 시도)
STRATEGY_NAME = {s["id"]: s["name"] for s in STRATEGIES}
STRATEGY_EXIT = {s["id"]: s["exit"] for s in STRATEGIES}


# ──────────────────────────────────────────────────────────────────────────
# 데이터 수집
# ──────────────────────────────────────────────────────────────────────────
def _is_common_stock(row):
    """ETF·ETN·우선주를 걷어낸다. 이걸 안 하면 대상의 3할이 지수 상품이 된다."""
    if row.get("stockEndType") != "stock":
        return False
    name = (row.get("stockName") or "").strip()
    # 삼성전자우 / 현대차2우B 같은 우선주는 본주와 신호가 겹쳐 자리만 차지한다.
    return not (name.endswith("우") or (len(name) > 2 and name[-2] == "우"))


def probe_sources():
    """어느 출처가 살아 있는지 먼저 찔러본다.
    깃허브 서버에서는 한국은행이 막혔던 전례가 있어, 네이버도 막힐 수 있다.
    로그를 볼 수 없는 상황에서 원인을 알려면 결과 파일에 남기는 수밖에 없다."""
    checks = {}
    try:
        r = requests.get(
            UNIVERSE_URL.format(market="KOSPI"),
            params={"page": 1, "pageSize": 5},
            headers=NAVER_HEADERS,
            timeout=15,
        )
        checks["naver_universe"] = f"{r.status_code} / {len((r.json() or {}).get('stocks') or [])}종목"
    except Exception as e:
        checks["naver_universe"] = f"실패: {type(e).__name__} {e}"
    try:
        r = requests.get(TREND_URL.format(code="005930"), headers=NAVER_HEADERS, timeout=15)
        checks["naver_trend"] = f"{r.status_code} / {len(r.json() or [])}행"
    except Exception as e:
        checks["naver_trend"] = f"실패: {type(e).__name__} {e}"
    try:
        r = requests.get(
            CHART_URL.format(symbol="005930.KS"),
            params={"range": "1mo", "interval": "1d"},
            headers=YAHOO_HEADERS,
            timeout=15,
        )
        n = len(r.json()["chart"]["result"][0].get("timestamp") or [])
        checks["yahoo_daily"] = f"{r.status_code} / {n}봉"
    except Exception as e:
        checks["yahoo_daily"] = f"실패: {type(e).__name__} {e}"

    for k, v in checks.items():
        print(f"[INFO] 출처 점검 {k}: {v}")
    return checks


def fetch_universe():
    """코스피·코스닥 보통주 전종목을 [{code, name, market}] 로 모은다."""
    out = []
    for market, suffix in (("KOSPI", "KS"), ("KOSDAQ", "KQ")):
        page = 1
        while True:
            try:
                resp = requests.get(
                    UNIVERSE_URL.format(market=market),
                    params={"page": page, "pageSize": PAGE_SIZE},
                    headers=NAVER_HEADERS,
                    timeout=20,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"[WARN] 유니버스 조회 실패 {market} p{page}: {e}")
                break

            rows = data.get("stocks") or []
            if not rows:
                break
            for r in rows:
                if _is_common_stock(r):
                    out.append(
                        {
                            "code": r["itemCode"],
                            "name": r["stockName"],
                            "market": market,
                            "symbol": f"{r['itemCode']}.{suffix}",
                        }
                    )
            if page * PAGE_SIZE >= int(data.get("totalCount") or 0):
                break
            page += 1
            time.sleep(0.05)

    print(f"[INFO] 스크리닝 대상 {len(out)}종목 (ETF·ETN·우선주 제외)")
    return out


def _yahoo_chart(symbol, rng, interval):
    last = None
    for attempt in range(3):
        try:
            resp = requests.get(
                CHART_URL.format(symbol=symbol),
                params={"range": rng, "interval": interval},
                headers=YAHOO_HEADERS,
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()["chart"]["result"][0]
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
    raise last


def fetch_daily(stock):
    """일봉을 dict of list 로.
    조회 자체가 실패하면 예외를 그대로 올린다(호출부가 '실패'로 센다).
    신규 상장이라 240일선을 만들 봉이 모자란 경우는 실패가 아니므로 None 을 돌려준다.
    이 둘을 뭉뚱그리면 실패 건수가 부풀어서 진짜 장애를 못 알아본다."""
    result = _yahoo_chart(stock["symbol"], "3y", "1d")

    stamps = result.get("timestamp") or []
    q = (result.get("indicators", {}).get("quote") or [{}])[0]
    keys = ("open", "high", "low", "close", "volume")
    series = {k: (q.get(k) or []) for k in keys}
    bars = {"date": [], **{k: [] for k in keys}}
    for i, ts in enumerate(stamps):
        # 야후는 배열 길이가 timestamp 와 어긋나게 오는 경우가 있어 길이를 같이 확인한다
        row = [series[k][i] if i < len(series[k]) else None for k in keys]
        if any(v is None for v in row):
            continue
        bars["date"].append(datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat())
        for k, v in zip(keys, row):
            bars[k].append(float(v))
    return bars if len(bars["close"]) >= MIN_DAILY_BARS else None


def fetch_flow(code):
    """외국인·기관·개인 일별 순매수. 최신 거래일이 앞에 온다."""
    try:
        resp = requests.get(TREND_URL.format(code=code), headers=NAVER_HEADERS, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        return []

    def num(v):
        try:
            return int(str(v).replace(",", "").replace("+", ""))
        except Exception:
            return 0

    return [
        {
            "date": r.get("bizdate", ""),
            "foreign": num(r.get("foreignerPureBuyQuant")),
            "organ": num(r.get("organPureBuyQuant")),
            "individual": num(r.get("individualPureBuyQuant")),
        }
        for r in rows
    ]


def fetch_minute_ma(symbol):
    """1분봉 240·480분선 대비 현재 위치. 데이터가 모자라면 None(=판정 불가)."""
    try:
        result = _yahoo_chart(symbol, "7d", "1m")
    except Exception:
        return None
    closes = [c for c in ((result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []) if c is not None]
    if len(closes) < 480:
        return None
    return {
        "last": closes[-1],
        "ma240": sum(closes[-240:]) / 240,
        "ma480": sum(closes[-480:]) / 480,
    }


# ──────────────────────────────────────────────────────────────────────────
# 지표 계산
# ──────────────────────────────────────────────────────────────────────────
def ma(values, n, offset=0):
    """offset=0 이면 마지막 봉 기준, 1이면 한 봉 전 기준."""
    end = len(values) - offset
    if end < n:
        return None
    return sum(values[end - n : end]) / n


def body_ratio(o, h, l, c):
    """캔들 몸통이 전체 길이에서 차지하는 비율. 도지 판정에 쓴다."""
    rng = h - l
    return abs(c - o) / rng if rng > 0 else 1.0


def lower_tail_ratio(o, h, l, c):
    """밑꼬리 비율. 20일선 지지 음봉 판정에 쓴다."""
    rng = h - l
    return (min(o, c) - l) / rng if rng > 0 else 0.0


def pct(a, b):
    return (a - b) / b * 100 if b else 0.0


# ──────────────────────────────────────────────────────────────────────────
# 전략 판정 — 전부 일봉만으로 끝난다 (BATCH)
# ──────────────────────────────────────────────────────────────────────────
def match_long_ma_breakout(b):
    """전략 7. 바닥권 종목이 장기선을 대량 거래로 뚫는 시점."""
    c, o, v = b["close"], b["open"], b["volume"]
    close, open_, vol = c[-1], o[-1], v[-1]
    ma240, ma480 = ma(c, 240), ma(c, 480)
    prev_close = c[-2]
    avg20v = ma(v, 20, offset=1) or 0
    is_bull = close > open_ and pct(close, open_) >= 3.0

    for line, name in ((ma480, "480일선"), (ma240, "240일선")):
        if not line:
            continue
        # A: 전일까지 선 아래 -> 오늘 종가로 완전히 돌파 + 거래량 300% 폭증
        if prev_close < line <= close and is_bull and v[-2] > 0 and vol >= v[-2] * 4.0:
            return "A", f"{name} 종가 돌파 (거래량 전일 대비 {vol / v[-2] * 100:.0f}%)"

    # B: 480일선 돌파를 두 번 밀린 뒤 세 번째에 뚫는다.
    # 밀린 '날'을 세면 안 된다. 한 번의 시도가 며칠씩 이어지므로 날짜로 세면
    # 삼세판이 아니라 '10회 실패' 같은 엉뚱한 숫자가 나온다. 붙어 있는 날은 한 번의 시도로 묶는다.
    if ma480 and prev_close < ma480 <= close and avg20v and vol >= avg20v * 2:
        reject_days = []
        for i in range(len(c) - 120, len(c) - 1):
            line_i = ma(c, 480, offset=len(c) - 1 - i)
            if line_i and c[i] < line_i <= b["high"][i]:
                reject_days.append(i)  # 장중엔 뚫었는데 종가로 못 지킨 날
        attempts = 0
        for idx, day in enumerate(reject_days):
            if idx == 0 or day - reject_days[idx - 1] > ATTEMPT_GAP_DAYS:
                attempts += 1
        if attempts >= 2:
            return "B", f"480일선 돌파 {attempts}회 시도 실패 후 재돌파"

    # C: 단기 골든크로스 + 대량 거래로 240일선 돌파 (기관 수급은 2단계에서 확인)
    if ma240 and prev_close < ma240 <= close and avg20v and vol >= avg20v * 2:
        ma5, ma20 = ma(c, 5), ma(c, 20)
        ma5p, ma20p = ma(c, 5, offset=5), ma(c, 20, offset=5)
        if ma5 and ma20 and ma5p and ma20p and ma5p <= ma20p and ma5 > ma20:
            return "C", "단기 골든크로스 + 대량 거래 240일선 돌파"
    return None, None


def match_new_high(b):
    """전략 5. 완전 정배열 + 480일선 위 + 신고가 돌파."""
    c, v, h = b["close"], b["volume"], b["high"]
    close, vol = c[-1], v[-1]
    mas = [ma(c, n) for n in (5, 10, 20, 60, 120, 240)]
    if any(x is None for x in mas) or not all(mas[i] > mas[i + 1] for i in range(len(mas) - 1)):
        return None, None
    ma480 = ma(c, 480)
    if not ma480 or close <= ma480:
        return None, None

    window = h[-252:-1] if len(h) > 252 else h[:-1]
    prior_high = max(window) if window else None
    avg20v = ma(v, 20, offset=1) or 0
    if prior_high and close > prior_high and vol >= avg20v * 2 and close > b["open"][-1]:
        return "", f"52주 신고가 돌파 (거래량 20일 평균의 {vol / avg20v:.1f}배)"
    return None, None


def match_swing_pullback(b):
    """전략 4. 급등 후 10~14일 조정, 거래량 바닥에서 이평선 지지 캔들."""
    c, v, o, h, l = b["close"], b["volume"], b["open"], b["high"], b["low"]
    if len(c) < 90:
        return None, None

    # 최근 60일 안에 저점 대비 +50% 이상 급등 구간이 있었는가
    seg = c[-60:]
    peak_i = seg.index(max(seg))
    trough = min(seg[: peak_i + 1]) if peak_i > 0 else None
    if not trough or pct(seg[peak_i], trough) < 50:
        return None, None

    # 고점 이후 10~14 거래일 조정
    bars_since_peak = len(seg) - 1 - peak_i
    if not 10 <= bars_since_peak <= 14:
        return None, None

    # 조정 구간 거래량이 급등 구간 대비 바닥 수준으로 급감
    rally_v = sum(v[-60 : -60 + peak_i + 1]) / (peak_i + 1)
    calm_v = sum(v[-5:]) / 5
    if not rally_v or calm_v > rally_v * 0.5:
        return None, None

    close, open_, high, low = c[-1], o[-1], h[-1], l[-1]
    near = lambda line: line and abs(close - line) / line <= 0.03

    if near(ma(c, 20)) and close < open_ and lower_tail_ratio(open_, high, low, close) >= 0.4:
        return "20일선", "20일선 지지 + 밑꼬리 긴 음봉 (종가 진입 / 3회 분할)"
    for n, label in ((10, "10일선"), (60, "60일선")):
        line = ma(c, n)
        if near(line) and body_ratio(open_, high, low, close) <= 0.15:
            return label, f"{label} 지지 + 도지 캔들 (3회 분할)"
        if near(line) and len(l) >= 3 and all(abs(l[-i] - line) / line <= 0.03 for i in (1, 2, 3)):
            return label, f"{label} 3일 연속 저점 지지 (3회 분할)"
    return None, None


def match_closing_bet(b):
    """전략 6. Type C(장기선 돌파)는 전략 7 A 와 같은 규칙이라 여기서 다루지 않는다."""
    c, o, h, l, v = b["close"], b["open"], b["high"], b["low"], b["volume"]
    close, high = c[-1], h[-1]
    # A 수급주: 당일 고가 부근 마감 (외인·기관 동반 순매수는 2단계에서 확인).
    # 원 규칙은 '수급이 집중 유입' 이므로 문턱을 낮게 잡으면 안 된다.
    # 거래량 1.5배 + 상승률 무관으로 잡았더니 +1.8% 짜리 대형주까지 올라와 목록을 다 먹었다.
    if high > 0 and (high - close) / high <= 0.01 and close > o[-1]:
        avg20v = ma(v, 20, offset=1) or 0
        if avg20v and v[-1] >= avg20v * 2 and pct(close, c[-2]) >= 3:
            return "A", f"당일 고가 마감 + 거래량 20일 평균의 {v[-1] / avg20v:.1f}배"
    # B 도지: 전일 급등(윗꼬리) 후 당일 거래량 급감 도지
    if len(c) >= 2 and pct(c[-2], o[-2]) >= 5 and body_ratio(o[-2], h[-2], l[-2], c[-2]) <= 0.7:
        if v[-1] <= v[-2] * 0.5 and body_ratio(o[-1], high, l[-1], close) <= 0.15:
            return "B", "전일 급등 후 거래량 급감 도지 (시간외 잔량은 확인 불가)"
    return None, None


MATCHERS = [
    ("LONG_MA_BREAKOUT", match_long_ma_breakout),
    ("NEW_HIGH", match_new_high),
    ("SWING_PULLBACK", match_swing_pullback),
    ("CLOSING_BET", match_closing_bet),
]


# ──────────────────────────────────────────────────────────────────────────
# 킬 스위치
# ──────────────────────────────────────────────────────────────────────────
def kill_long_term_bearish(b, breakout):
    """일봉 240·480일선 아래면 진입 금지.
    단 전략 7 트리거가 성립했다면 '지금은 선 위'라는 뜻이므로 예외로 통과시킨다."""
    if breakout:
        return None
    close = b["close"][-1]
    for n in (240, 480):
        line = ma(b["close"], n)
        if line and close < line:
            return f"장기 역배열 ({n}일선 아래)"
    return None


def kill_major_outflow(flow):
    """외국인·기관 동반 순매도이거나, 어느 한쪽이 3일 연속 순매도면 진입 금지."""
    if not flow:
        return None
    if flow[0]["foreign"] < 0 and flow[0]["organ"] < 0:
        return "외국인·기관 동반 순매도(양매도)"
    if len(flow) >= 3:
        for key, label in (("foreign", "외국인"), ("organ", "기관")):
            if all(d[key] < 0 for d in flow[:3]):
                return f"{label} 3일 연속 순매도 이탈"
    return None


def kill_minute_bearish(mm):
    if not mm:
        return None  # 1분봉이 모자란 종목은 판정하지 않는다 (없는 근거로 죽이지 않는다)
    for n in (240, 480):
        if mm["last"] < mm[f"ma{n}"]:
            return f"1분봉 {n}분선 아래"
    return None


# ──────────────────────────────────────────────────────────────────────────
# 조립
# ──────────────────────────────────────────────────────────────────────────
def _watch_candidate(bars):
    """장중 실시간 전략(1·2·3)의 감시 후보인지. 배치는 여기까지만 좁혀준다.
    전략 매칭과 독립이다. 배치 전략에 안 걸려도 CK480 후보일 수 있기 때문이다."""
    c, v = bars["close"], bars["volume"]
    chg = pct(c[-1], c[-2])
    out = []
    if chg >= 15:
        out.append(("CK480", f"직전 거래일 +{chg:.1f}% → 1분봉 480분선 횡보 지지 확인"))
    if v[-1] >= 20_000_000:
        out.append(("DAY_MOMENTUM", f"거래량 {v[-1] / 10000:.0f}만 주 → 20분선 계단식 눌림 확인"))
    if 8 <= chg < 15:
        out.append(("GAP_PULLBACK", f"직전 거래일 +{chg:.1f}% → 갭상승 시 09:15~09:25 20분선 지지 확인"))
    return out


def _scan_one(stock):
    """일봉 한 번만 받아서 전략 매칭과 감시 후보를 같이 판정한다.
    봉 데이터는 여기서 다 소비하고 필요한 값만 남긴다(전종목 분량을 메모리에 들고 있지 않으려고)."""
    try:
        bars = fetch_daily(stock)
    except Exception:
        return {"stock": stock, "failed": True}
    if bars is None:
        return None  # 상장 기간이 짧아 장기 이평선을 만들 수 없는 종목

    hits = []
    breakout = False
    for sid, fn in MATCHERS:
        kind, reason = fn(bars)
        if reason:
            hits.append({"strategy": sid, "type": kind, "reason": reason})
            if sid == "LONG_MA_BREAKOUT":
                breakout = True

    watch = _watch_candidate(bars)
    if not hits and not watch:
        return None

    c, v = bars["close"], bars["volume"]
    return {
        "stock": stock,
        "failed": False,
        "hits": hits,
        "watch": watch,
        "breakout": breakout,
        "blocked_daily": kill_long_term_bearish(bars, breakout),
        "quote": {
            "close": round(c[-1]),
            "change_percent": round(pct(c[-1], c[-2]), 2),
            "volume": int(v[-1]),
            "trading_day": bars["date"][-1],
        },
    }


CHART_BARS = 620  # 480일선을 화면 왼쪽 끝부터 그리려면 480 + 볼 구간만큼 필요하다


def write_chart_files(stocks, charts_dir):
    """목록에 오른 종목의 일봉을 종목별 파일로 저장한다.

    브라우저에서 야후를 직접 부르면 CORS 로 막힌다(배포본에서 확인함).
    그래서 화면에 차트를 띄우려면 배치가 미리 받아두는 수밖에 없다.
    한 파일에 다 담으면 첫 로딩이 무거워지므로 종목별로 쪼개 클릭할 때만 받게 한다."""
    if charts_dir is None:
        return 0
    charts_dir.mkdir(parents=True, exist_ok=True)

    def one(s):
        try:
            result = _yahoo_chart(f"{s['code']}.{'KS' if s['market'] == 'KOSPI' else 'KQ'}", "3y", "1d")
        except Exception:
            return None
        stamps = result.get("timestamp") or []
        q = (result.get("indicators", {}).get("quote") or [{}])[0]
        keys = ("open", "high", "low", "close", "volume")
        series = {k: (q.get(k) or []) for k in keys}
        rows = []
        for i, ts in enumerate(stamps):
            vals = [series[k][i] if i < len(series[k]) else None for k in keys]
            if any(v is None for v in vals):
                continue
            day = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()
            rows.append([day] + [round(v) for v in vals])
        return {"code": s["code"], "name": s["name"], "bars": rows[-CHART_BARS:]} if rows else None

    written = set()
    with ThreadPoolExecutor(max_workers=DAILY_WORKERS) as pool:
        for payload in pool.map(one, stocks):
            if not payload:
                continue
            path = charts_dir / f"{payload['code']}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            written.add(path.name)

    # 목록에서 빠진 종목의 차트는 지운다. 안 그러면 저장소에 파일이 무한정 쌓인다.
    for old in charts_dir.glob("*.json"):
        if old.name not in written:
            old.unlink()

    print(f"[INFO] 차트 파일 {len(written)}개 저장")
    return len(written)


def build_screening(today=None, charts_dir=None):
    today = today or datetime.date.today()
    probe = probe_sources()
    universe = fetch_universe()
    if not universe:
        raise RuntimeError(f"유니버스를 가져오지 못했습니다 — 출처 점검: {probe}")

    # 1단계 — 전종목 일봉으로 전략 매칭 + 감시 후보 판정
    cands, failures = [], 0
    with ThreadPoolExecutor(max_workers=DAILY_WORKERS) as pool:
        for res in pool.map(_scan_one, universe):
            if res is None:
                continue  # 조건에 안 걸린 정상 종목
            if res["failed"]:
                failures += 1
            else:
                cands.append(res)
    print(f"[INFO] 1단계: {len(universe)}종목 중 {len(cands)}종목 후보 (조회 실패 {failures})")
    if failures > len(universe) * 0.2:
        print(f"[WARN] 조회 실패가 {failures}건이다 — 결과가 상당수 빠졌을 수 있다")

    # 2단계 — 후보만 수급 조회
    with ThreadPoolExecutor(max_workers=DAILY_WORKERS) as pool:
        flows = list(pool.map(lambda m: fetch_flow(m["stock"]["code"]), cands))
    # 3단계 — 배치 전략에 걸린 것만 1분봉 조회 (감시 후보는 어차피 사람이 장중에 본다)
    with ThreadPoolExecutor(max_workers=DAILY_WORKERS) as pool:
        minutes = list(
            pool.map(lambda m: fetch_minute_ma(m["stock"]["symbol"]) if m["hits"] else None, cands)
        )

    entries, danger, watch = [], [], []
    for m, flow, mm in zip(cands, flows, minutes):
        stock = m["stock"]
        base = {
            "code": stock["code"],
            "name": stock["name"],
            "market": stock["market"],
            **m["quote"],
            "url": f"https://m.stock.naver.com/domestic/stock/{stock['code']}/total",
        }
        base.pop("trading_day", None)
        if flow:
            base["flow"] = {
                "date": flow[0]["date"],
                "foreign": flow[0]["foreign"],
                "organ": flow[0]["organ"],
                "individual": flow[0]["individual"],
            }

        blocked = m["blocked_daily"] or kill_major_outflow(flow) or kill_minute_bearish(mm)

        if m["hits"]:
            hit = m["hits"][0]  # MATCHERS 에서 전략 7 이 먼저라 6C 와 겹치면 7 로 잡힌다
            if blocked:
                danger.append({**base, "reason": blocked, "strategy_name": STRATEGY_NAME[hit["strategy"]]})
            else:
                ok = True
                # 전략 7 C 는 '기관 자금' 이 전제다. 개인 주도 반등이면 조건 미달로 본다.
                if hit["strategy"] == "LONG_MA_BREAKOUT" and hit["type"] == "C":
                    ok = bool(flow) and flow[0]["organ"] > 0 and flow[0]["individual"] < 0
                # 원 규칙은 '외인/기관 순매수 집중 유입' 이다. OR 로 잡으면 한쪽만 사도 통과해
                # 의미가 사라진다. 동반 순매수(AND)여야 수급이 들어왔다고 볼 수 있다.
                if hit["strategy"] == "CLOSING_BET" and hit["type"] == "A":
                    ok = bool(flow) and flow[0]["foreign"] > 0 and flow[0]["organ"] > 0
                if ok:
                    entries.append(
                        {
                            **base,
                            "strategy": hit["strategy"],
                            "strategy_name": STRATEGY_NAME[hit["strategy"]],
                            "type": hit["type"],
                            "reason": hit["reason"],
                            "exits": STRATEGY_EXIT[hit["strategy"]],
                        }
                    )

        # 감시 후보는 배치 전략과 별개다. 다만 킬스위치에 걸린 종목은 감시 대상도 아니다.
        if not blocked:
            for sid, why in m["watch"]:
                watch.append(
                    {**base, "strategy": sid, "strategy_name": STRATEGY_NAME[sid], "reason": why}
                )

    # 한 전략이 목록을 독점하지 않도록 전략별로 잘라 20~30개로 맞춘다
    capped, per = [], {}
    for e in sorted(entries, key=lambda x: -x["volume"]):
        n = per.get(e["strategy"], 0)
        if n >= MAX_PER_STRATEGY:
            continue
        per[e["strategy"]] = n + 1
        capped.append(e)

    # 감시 후보 정리
    #  - 이미 '진입 조건 충족' 에 올라간 종목은 뺀다. 같은 종목이 두 칸을 먹으면 자리만 낭비다.
    #  - 한 종목이 여러 감시 전략에 걸려도 한 번만 보여준다.
    #  - 전략별로도 자른다. 안 하면 갭 후보가 목록을 다 먹는다(실제로 12개 중 8개가 갭이었다).
    entered = {e["code"] for e in capped}
    seen, per_w, watch_uniq = set(), {}, []
    for w in sorted(watch, key=lambda x: -x["volume"]):
        if w["code"] in entered or w["code"] in seen:
            continue
        n = per_w.get(w["strategy"], 0)
        if n >= MAX_WATCH_PER_STRATEGY:
            continue
        per_w[w["strategy"]] = n + 1
        seen.add(w["code"])
        watch_uniq.append(w)
    watch = watch_uniq
    danger = sorted(danger, key=lambda x: -x["volume"])[:8]

    print(f"[INFO] 스크리닝: 진입 {len(capped)} / 감시 {len(watch)} / 금지 {len(danger)} / 조회실패 {failures}")

    # 차트는 목록에 실제로 오른 종목만 만든다 (진입 + 감시 + 금지)
    chart_targets = {
        e["code"]: {"code": e["code"], "name": e["name"], "market": e["market"]}
        for e in capped + watch + danger
    }
    charts = write_chart_files(list(chart_targets.values()), charts_dir)

    return {
        "probe": probe,
        "chart_count": charts,
        "as_of_trading_day": cands[0]["quote"]["trading_day"] if cands else "",
        "universe_count": len(universe),
        "fetch_failures": failures,
        "entries": capped,
        "watch": watch,
        "danger": danger,
    }


if __name__ == "__main__":
    print(json.dumps(build_screening(), ensure_ascii=False, indent=2)[:4000])
