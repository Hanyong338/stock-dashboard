"""섹션 4 모닝 브레이크아웃 — 장 시작 30분(09:00~09:30) 분봉으로 판정하는 단타 스크리너.

섹션 1~3 과는 실행 시점도 데이터도 완전히 달라서 파일을 나눴다.
1~3 은 마감 후 일봉으로 보지만, 이건 장중 분봉으로 본다.

--------------------------------------------------------------------------
알아둘 한계
--------------------------------------------------------------------------
깃허브 예약 실행은 제시각에 시작하지 않는다(보통 5~20분, 때로 그 이상 늦는다).
그래서 '09:30 돌파 타점을 즉시 알려주는' 용도로는 쓸 수 없다.

다만 판정 자체는 정확하다. 분봉을 받아 09:00~09:30 구간만 잘라 쓰기 때문에,
실행이 09:45 에 시작돼도 결과는 09:30 시점 기준 그대로다.
'지금 사라'가 아니라 '오늘 아침 무엇이 터졌나'를 보는 용도로 쓸 것.

--------------------------------------------------------------------------
3분봉은 야후가 주지 않아서 1분봉을 3개씩 묶어 만든다.
3분봉 20선(=60분)은 장 시작 30분 안에는 당일 봉만으로 만들 수 없어,
전날 분봉까지 이어붙여 계산한다.
"""
import datetime
import json
from concurrent.futures import ThreadPoolExecutor

import requests

from screening_rules import MORNING_SECTION
from screening import (
    NAVER_HEADERS,
    PAGE_SIZE,
    UNIVERSE_URL,
    WORKERS,
    YAHOO_HEADERS,
    _get,
    _is_common_stock,
    _num,
    _yahoo_chart,
    fetch_theme_map,
    ma,
    pct,
)

KST = datetime.timezone(datetime.timedelta(hours=9))
SESSION_START = datetime.time(9, 0)
CUTOFF = datetime.time(9, 30)
MID = datetime.time(9, 15)  # 유형 B 의 전반/후반 경계
P = MORNING_SECTION["params"]
MAX_TAGS = 2


# ──────────────────────────────────────────────────────────────────────────
# 수집
# ──────────────────────────────────────────────────────────────────────────
def fetch_universe():
    """09:30 시점 체급 필터. 거래대금 100억 기준은 여기서 쓸 수 없다 —
    그 시각엔 아직 하루치가 안 쌓여서 거의 전 종목이 탈락해버린다.
    시총·주가만 보고, 거래대금 조건은 유형 A 의 '30분 200억' 이 대신한다."""
    out = []
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
                cap = _num(r.get("marketValueRaw"))
                price = _num(r.get("closePriceRaw"))
                early = _num(r.get("accumulatedTradingValueRaw"))
                if cap < P["min_market_cap"] or price < P["min_price"]:
                    continue
                if early < P["min_early_value"]:
                    continue  # 장 시작 30분에 10억도 안 붙었으면 볼 것도 없다
                out.append(
                    {
                        "code": r["itemCode"],
                        "name": r["stockName"],
                        "market": market,
                        "symbol": f"{r['itemCode']}.{suffix}",
                        "market_cap": cap,
                    }
                )
            if page * PAGE_SIZE >= int(data.get("totalCount") or 0):
                break
            page += 1

    print(f"[INFO] 모닝: 체급 통과 {len(out)}종목")
    return out


def _minute_rows(symbol):
    """최근 5거래일 1분봉을 [(한국시간, o,h,l,c,v)] 로. 3분봉 20선을 만들려면 전날치가 필요하다."""
    res = _yahoo_chart(symbol, "5d", "1m")
    stamps = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    keys = ("open", "high", "low", "close", "volume")
    series = {k: (q.get(k) or []) for k in keys}
    rows = []
    for i, ts in enumerate(stamps):
        vals = [series[k][i] if i < len(series[k]) else None for k in keys]
        if any(v is None for v in vals):
            continue
        t = datetime.datetime.fromtimestamp(ts, KST)
        rows.append((t, *[float(v) for v in vals]))
    return rows


def _daily_rows(symbol):
    res = _yahoo_chart(symbol, "3y", "1d")
    stamps = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    keys = ("open", "high", "low", "close", "volume")
    series = {k: (q.get(k) or []) for k in keys}
    out = []
    for i, ts in enumerate(stamps):
        vals = [series[k][i] if i < len(series[k]) else None for k in keys]
        if any(v is None for v in vals):
            continue
        out.append((datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date(), *[float(v) for v in vals]))
    return out


def to_3min(rows):
    """1분봉을 3개씩 묶어 3분봉으로. 야후가 3분봉을 주지 않아 직접 만든다."""
    buckets = {}
    for t, o, h, l, c, v in rows:
        key = (t.date(), t.hour, t.minute // 3)
        b = buckets.get(key)
        if b is None:
            buckets[key] = [t, o, h, l, c, v]
        else:
            b[2] = max(b[2], h)
            b[3] = min(b[3], l)
            b[4] = c
            b[5] += v
    return [tuple(b) for _, b in sorted(buckets.items())]


# ──────────────────────────────────────────────────────────────────────────
# 판정
# ──────────────────────────────────────────────────────────────────────────
def _match(stock, minutes, dailies, target_day):
    """조건에 맞으면 (유형, 사유, 지표) 를 돌려준다."""
    today = [r for r in minutes if r[0].date() == target_day and SESSION_START <= r[0].time() < CUTOFF]
    if len(today) < 20:  # 장 시작 30분 중 20분도 안 채워졌으면 판정하지 않는다
        return None

    prev_days = [d for d in dailies if d[0] < target_day]
    if not prev_days:
        return None
    _, _, prev_high, _, prev_close, prev_vol = prev_days[-1]

    opens = today[0][1]
    highs = max(r[2] for r in today)
    lows = min(r[3] for r in today)
    last = today[-1][4]
    vol30 = sum(r[5] for r in today)
    value30 = sum(r[4] * r[5] for r in today)
    gap = pct(opens, prev_close)
    held_high = highs > 0 and last >= highs * P["high_hold"]

    bars3 = to_3min([r for r in minutes if r[0].date() <= target_day])
    today3 = [b for b in bars3 if b[0].date() == target_day and SESSION_START <= b[0].time() < CUTOFF]
    closes3 = [b[4] for b in bars3]
    ma5, ma10, ma20 = ma(closes3, 5), ma(closes3, 10), ma(closes3, 20)

    base = {
        "open_gap": round(gap, 2),
        "value_30m_eok": round(value30 / 1e8),
        "vol_vs_prev_day": round(vol30 / prev_vol * 100) if prev_vol else 0,
        # 화면 카드가 섹션 1~3 과 같은 필드명을 쓰므로 맞춰준다
        "close": round(last),
        "volume": int(vol30),
        "change_percent": round(pct(last, prev_close), 2),
    }

    # A: 전일 고가 돌파형 — 거래량·거래대금이 먼저 터지고 전일 고가를 장대양봉으로 뚫는다
    if prev_vol and vol30 >= prev_vol * P["a_volume_vs_prev_day"] and value30 >= P["a_min_30m_value"]:
        broke = any(
            b[4] > prev_high and b[4] > b[1] and pct(b[4], b[1]) >= 0.5 for b in today3
        )
        if broke and last > prev_high and held_high:
            return (
                "A",
                f"전일 고가 {prev_high:,.0f} 돌파 · 30분 거래대금 {value30 / 1e8:.0f}억 · "
                f"거래량 전일 하루의 {vol30 / prev_vol * 100:.0f}%",
                base,
            )

    # B: 시초 갭 지지 N자 반등 — 갭 출발 → 눌림에도 시초가 사수 → 고점 재돌파
    lo_g, hi_g = P["b_gap_range"]
    if lo_g <= gap <= hi_g:
        early = [r for r in today if r[0].time() < MID]
        late = [r for r in today if r[0].time() >= MID]
        if early and late:
            dipped = min(r[3] for r in early) < max(r[2] for r in early)  # 전반부에 조정이 있었다
            kept_open = min(r[3] for r in early) >= opens * 0.995  # 시초가를 깨지 않았다
            re_break = max(r[2] for r in late) > max(r[2] for r in early)  # 전반부 고점 재돌파
            vol_back = sum(r[5] for r in late) >= sum(r[5] for r in early) * 0.5  # 거래량 재유입
            on_ma20 = ma20 and last >= ma20
            if dipped and kept_open and re_break and vol_back and on_ma20:
                return (
                    "B",
                    f"시초 +{gap:.1f}% 갭 · 시초가 {opens:,.0f} 사수 후 전반부 고점 재돌파(N자) · "
                    f"3분봉 20선 지지 · 30분 거래대금 {value30 / 1e8:.0f}억",
                    base,
                )

    # C: 장기 이평선 갭 돌파 — 240·480일선을 갭으로 넘거나 30분 만에 뚫고 정배열로 버틴다
    closes_d = [d[4] for d in dailies]
    aligned = ma5 and ma10 and ma20 and ma5 > ma10 > ma20
    if aligned:
        for n in P["c_ma_lines"]:
            line = ma(closes_d, n)
            if not line or prev_close >= line:
                continue  # 어제 이미 위였으면 '돌파'가 아니다
            if last > line and lows >= line:  # 뚫은 뒤 그 가격대를 지켰다
                how = "갭상승 출발" if opens > line else "장초반 대량 거래"
                return (
                    "C",
                    f"{n}일선 {line:,.0f} {how}으로 돌파 · 3분봉 5·10·20선 정배열 유지 · "
                    f"30분 거래대금 {value30 / 1e8:.0f}억",
                    base,
                )
    return None


def _scan(stock, target_day):
    try:
        minutes = _minute_rows(stock["symbol"])
        dailies = _daily_rows(stock["symbol"])
    except Exception:
        return {"failed": True}
    if not minutes or len(dailies) < 260:
        return None
    hit = _match(stock, minutes, dailies, target_day)
    if not hit:
        return None
    kind, reason, metrics = hit
    return {"failed": False, "stock": stock, "type": kind, "reason": reason, "metrics": metrics}


def build_morning_breakout(target_day=None, tag_map=None):
    """09:30 기준 모닝 브레이크아웃 후보를 만든다."""
    universe = fetch_universe()
    if not universe:
        raise RuntimeError("모닝: 체급을 통과한 종목이 없습니다")

    target_day = target_day or datetime.datetime.now(KST).date()
    hits, failures = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for res in pool.map(lambda s: _scan(s, target_day), universe):
            if res is None:
                continue
            if res.get("failed"):
                failures += 1
            else:
                hits.append(res)

    if tag_map is None:
        tag_map = {}
    items = []
    for h in hits:
        s = h["stock"]
        items.append(
            {
                "code": s["code"],
                "name": s["name"],
                "market": s["market"],
                "tags": tag_map.get(s["code"], [])[:MAX_TAGS],
                "market_cap_eok": round(s["market_cap"] / 1e8),
                "type": h["type"],
                "reason": h["reason"],
                "exits": MORNING_SECTION["exits"],
                "url": f"https://m.stock.naver.com/domestic/stock/{s['code']}/total",
                **h["metrics"],
            }
        )

    # 유형별로 돌아가며 뽑아 한 유형이 자리를 다 먹지 않게 한다 (섹션 1~3 과 같은 방식)
    by_type = {}
    for e in sorted(items, key=lambda x: -x["value_30m_eok"]):
        by_type.setdefault(e["type"], []).append(e)
    picked, order = [], sorted(by_type)
    cap = MORNING_SECTION["max_items"]
    while len(picked) < cap and any(by_type[t] for t in order):
        for t in order:
            if by_type[t] and len(picked) < cap:
                picked.append(by_type[t].pop(0))
    picked.sort(key=lambda x: -x["value_30m_eok"])

    print(f"[INFO] 모닝 브레이크아웃: {len(picked)}종목 (후보 {len(items)} / 조회실패 {failures})")
    return {
        "id": MORNING_SECTION["id"],
        "name": MORNING_SECTION["name"],
        "desc": MORNING_SECTION["desc"],
        "legend": MORNING_SECTION["legend"],
        "as_of": f"{target_day.isoformat()} 09:30",
        "universe_count": len(universe),
        "fetch_failures": failures,
        "items": picked,
    }


if __name__ == "__main__":
    print(json.dumps(build_morning_breakout(), ensure_ascii=False, indent=2)[:3000])
