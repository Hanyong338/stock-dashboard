"""무료 야후 파이낸스 API로 미국 주요 지수/섹터 시세를 가져온다. API 키 불필요, 비용 없음."""
import datetime

import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# 업종 단위 ETF. 예전에는 SPDR 11개 대분류(기술/금융/임의소비재...)를 썼는데,
# "기술이 올랐다"는 말로는 국내에서 뭘 봐야 하는지 알 수가 없다. 반도체가 오른 건지
# 소프트웨어가 오른 건지에 따라 대응 종목이 완전히 달라지기 때문에 업종 단위로 쪼갰다.
# 국내 증시에 테마가 그대로 연결되는 것들로만 골랐고, 전부 야후에서 시세가 나오는 걸 확인했다.
SECTORS = [
    {"symbol": "SOXX", "name": "반도체"},
    {"symbol": "IGV", "name": "소프트웨어"},
    {"symbol": "SKYY", "name": "클라우드"},
    {"symbol": "CIBR", "name": "사이버보안"},
    {"symbol": "BOTZ", "name": "AI·로봇"},
    {"symbol": "FDN", "name": "인터넷 플랫폼"},
    {"symbol": "XTL", "name": "통신장비"},
    {"symbol": "GRID", "name": "전력망·전력기기"},
    {"symbol": "URA", "name": "원자력"},
    {"symbol": "TAN", "name": "태양광"},
    {"symbol": "LIT", "name": "2차전지"},
    {"symbol": "IDRV", "name": "전기차"},
    {"symbol": "ITA", "name": "방산·우주항공"},
    {"symbol": "XBI", "name": "바이오"},
    {"symbol": "IHI", "name": "의료기기"},
    {"symbol": "KRE", "name": "은행"},
    {"symbol": "XOP", "name": "원유·가스"},
    {"symbol": "COPX", "name": "구리"},
    {"symbol": "GDX", "name": "금광"},
    {"symbol": "IYT", "name": "운송"},
    {"symbol": "XRT", "name": "소매"},
    {"symbol": "ESPO", "name": "게임"},
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


def fetch_session_sectors(target_date):
    """target_date 거래일의 섹터별 등락률을 [{name, change_percent}] 로 반환 (내림차순).
    유튜브 발언이 아니라 실제 SPDR 섹터 ETF 시세 기준이다."""
    rows = []
    for item in SECTORS:
        try:
            daily = _fetch_daily_closes(item["symbol"])
        except Exception as e:
            print(f"[WARN] sector session fetch failed for {item['symbol']}: {e}")
            continue

        usable = [r for r in daily if r[0] <= target_date]
        if len(usable) < 2:
            continue

        close, prev = usable[-1][1], usable[-2][1]
        if not prev:
            continue
        rows.append({"name": item["name"], "change_percent": (close - prev) / prev * 100})

    rows.sort(key=lambda r: r["change_percent"], reverse=True)
    return rows


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
