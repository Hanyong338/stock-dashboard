"""무료 야후 파이낸스 API로 미국 주요 지수/섹터 시세를 가져온다. API 키 불필요, 비용 없음."""
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
