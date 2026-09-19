"""무료 야후 파이낸스 API로 미국 주요 지수/섹터 시세를 가져온다. API 키 불필요, 비용 없음."""
import datetime

import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

INDICES = [
    {"symbol": "^GSPC", "name": "S&P 500"},
    {"symbol": "^IXIC", "name": "나스닥"},
    {"symbol": "^DJI", "name": "다우존스"},
    {"symbol": "^VIX", "name": "VIX(공포지수)"},
]

SECTORS = [
    {"symbol": "XLK", "name": "기술"},
    {"symbol": "XLF", "name": "금융"},
    {"symbol": "XLV", "name": "헬스케어"},
    {"symbol": "XLY", "name": "임의소비재"},
    {"symbol": "XLP", "name": "필수소비재"},
    {"symbol": "XLE", "name": "에너지"},
    {"symbol": "XLI", "name": "산업재"},
    {"symbol": "XLB", "name": "소재"},
    {"symbol": "XLU", "name": "유틸리티"},
    {"symbol": "XLRE", "name": "부동산"},
    {"symbol": "XLC", "name": "커뮤니케이션"},
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-dashboard-bot/1.0)"}

# 당잠사 방송은 지수 '포인트'를 잘 읽어주지 않고 등락률·정성 표현으로만 말하는 경우가 많다.
# 없는 숫자를 AI가 지어내게 할 수는 없으므로, 종가는 실제 시세에서 가져와 채운다.
BRIEF_INDICES = [
    {"symbol": "^IXIC", "name": "나스닥", "fmt": "index"},
    {"symbol": "^GSPC", "name": "S&P 500", "fmt": "index"},
    {"symbol": "^DJI", "name": "다우", "fmt": "index"},
    {"symbol": "^SOX", "name": "필라델피아 반도체", "fmt": "index"},
    {"symbol": "^TNX", "name": "미국 10년물 금리", "fmt": "yield"},
    {"symbol": "DX-Y.NYB", "name": "달러 인덱스", "fmt": "plain"},
    {"symbol": "CL=F", "name": "WTI 유가", "fmt": "usd"},
]


def _fetch_quote(symbol):
    resp = requests.get(CHART_URL.format(symbol=symbol), headers=HEADERS, timeout=15)
    resp.raise_for_status()
    meta = resp.json()["chart"]["result"][0]["meta"]

    price = meta.get("regularMarketPrice")
    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
    change_percent = meta.get("regularMarketChangePercent")
    if change_percent is None and price is not None and prev_close:
        change_percent = (price - prev_close) / prev_close * 100

    return {"price": price, "change_percent": change_percent}


def _fetch_group(items):
    results = []
    for item in items:
        try:
            quote = _fetch_quote(item["symbol"])
        except Exception as e:
            print(f"[WARN] market data fetch failed for {item['symbol']}: {e}")
            continue
        results.append({**item, **quote})
    return results


def fetch_market_brief():
    indices = _fetch_group(INDICES)
    sectors = _fetch_group(SECTORS)
    sectors.sort(key=lambda s: s.get("change_percent") or 0, reverse=True)
    return {"indices": indices, "sectors": sectors}


def _fetch_daily_closes(symbol, days=12):
    """최근 일봉을 [(date, close), ...] 로 돌려준다. 오래된 것부터 정렬."""
    resp = requests.get(
        CHART_URL.format(symbol=symbol),
        params={"range": f"{days}d", "interval": "1d"},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    stamps = result.get("timestamp") or []
    closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []

    rows = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        rows.append((datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date(), close))
    return rows


def _format_close(fmt, close, prev):
    if fmt == "yield":
        # 금리는 수치 자체가 %, 변화폭은 bp(0.01%p)로 읽는 게 관례
        value = f"{close:.2f}%"
        change = f"{(close - prev) * 100:+.1f}bp" if prev else "-"
        return value, change

    pct = ((close - prev) / prev * 100) if prev else None
    change = f"{pct:+.2f}%" if pct is not None else "-"
    if fmt == "usd":
        return f"${close:,.2f}", change
    if fmt == "plain":
        return f"{close:,.2f}", change
    return f"{close:,.2f}", change


def fetch_session_closes(target_date):
    """target_date(미 동부 기준 거래일)의 종가와 전일 대비 등락을 {지표명: {value, change}} 로 반환.
    해당 날짜에 거래가 없으면 그 이전 가장 가까운 거래일을 쓴다."""
    out = {}
    for item in BRIEF_INDICES:
        try:
            rows = _fetch_daily_closes(item["symbol"])
        except Exception as e:
            print(f"[WARN] session close fetch failed for {item['symbol']}: {e}")
            continue

        usable = [r for r in rows if r[0] <= target_date]
        if len(usable) < 2:
            continue

        close_date, close = usable[-1]
        prev = usable[-2][1]
        value, change = _format_close(item["fmt"], close, prev)
        out[item["name"]] = {"value": value, "change": change, "close_date": close_date.isoformat()}
    return out
