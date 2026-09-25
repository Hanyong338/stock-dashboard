"""시그널 스크리너 성과 추적 (1개월 = 20거래일 포워드, 목표가 없음).

1. 박제 (Snapshot)
   스크리너가 종목을 뽑은 날의 종가를 진입가로, 그날 계산한 손절가를 기준 손절가로 영구 고정한다.
   한 번 박제한 값은 다시 계산하거나 덮어쓰지 않는다.
   · 섹션 1~3 (국장·미장): 스크리너 결과 그대로. 진입가 = 발굴일 종가, 손절가 = screening.stop_price()
   · 섹션 4 모닝 브레이크아웃: 발굴은 09:30 이지만 규칙대로 그날 '종가'를 진입가로 쓴다.
     손절가는 섹션 4 청산 규칙의 '-2.0% 기계적 손절'을 진입가에 적용한다.
   · 같은 종목이 같은 섹션에 연달아 뽑히면(추세주는 며칠씩 남는다) 추적 중인 동안은 새로 박제하지 않는다.

2. 추적 (Tracking) — 발굴 다음 거래일(D+1)부터 20거래일
   · 일별 종가 기준 누적 수익률
   · 최고 수익률(HWM): 기간 중 장중 고가 기준, 그리고 도달 일차
   · 최대 낙폭(MDD): 기간 중 장중 저가 기준, 진입가 대비 (진입가 아래로 한 번도 안 갔으면 0)

3. 종결
   · SL_HIT  : 종가가 기준 손절가 아래로 마감한 날 그 종가로 손실 확정, 추적 종료
   · EXPIRED : 20거래일 동안 손절 없이 버티면 20일차 종가로 확정 (HWM 도 함께 남는다)

데이터는 docs/data/tracking.json 한 파일. 일별 기록은 수익률 숫자 배열로만 둬서 파일이 커지지 않게 한다.
"""
import datetime
from concurrent.futures import ThreadPoolExecutor

from screening import WORKERS, _bars_from, _yahoo_chart, stop_price

TRACK_DAYS = 20
MORNING_STOP_PCT = 2.0  # 섹션4 '-2.0% 기계적 손절'
KST = datetime.timezone(datetime.timedelta(hours=9))
KR_CLOSED_AT = datetime.time(15, 40)  # 정규장 15:30 마감 + 여유
US_CLOSED_AT = datetime.time(16, 10)  # 미 동부 16:00 마감 + 여유

try:
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
except Exception:  # 서버에 시간대 정보가 없을 때. 서머타임 기간 기준으로 근사한다
    ET = datetime.timezone(datetime.timedelta(hours=-4))


# ──────────────────────────────────────────────────────────────────────────
# 시장 시각
# ──────────────────────────────────────────────────────────────────────────
def _local_now(market, now):
    return now.astimezone(ET if market == "us" else KST)


def last_closed_day(market, now):
    """그 시장에서 마지막으로 '마감이 끝난' 날짜(달력 기준). 휴장 여부는 따지지 않는다 — 야후에 봉이 없으면 그냥 새 기록이 안 생긴다."""
    local = _local_now(market, now)
    closed_at = US_CLOSED_AT if market == "us" else KR_CLOSED_AT
    return local.date() if local.time() >= closed_at else local.date() - datetime.timedelta(days=1)


def _symbol(market, code, board):
    if market == "us":
        return code
    return f"{code}.{'KQ' if board == 'KOSDAQ' else 'KS'}"


def _daily(symbol, rng="3mo"):
    return _bars_from(_yahoo_chart(symbol, rng, "1d"))


# ──────────────────────────────────────────────────────────────────────────
# 박제
# ──────────────────────────────────────────────────────────────────────────
def _candidates(screening_kr, screening_us, morning):
    """세 결과 파일에서 박제 후보를 뽑는다. 장중 미완성 판정(intraday)은 박제하지 않는다."""
    out = []
    for data, market in ((screening_kr, "kr"), (screening_us, "us")):
        if not isinstance(data, dict) or data.get("intraday") or not data.get("as_of_trading_day"):
            continue
        for sec in data.get("sections") or []:
            for it in sec.get("items") or []:
                out.append(
                    {
                        "market": market,
                        "code": it["code"],
                        "name": it["name"],
                        "board": it.get("market", ""),
                        "tags": it.get("tags", []),
                        "section": sec["id"],
                        "section_name": sec.get("name", ""),
                        "type": it.get("type", ""),
                        "found_on": data["as_of_trading_day"],
                        "entry": it.get("close"),
                        "stop": it.get("stop"),
                        "reason": it.get("reason", ""),
                    }
                )
    if isinstance(morning, dict) and morning.get("as_of") and not morning.get("error"):
        day = morning["as_of"][:10]
        for it in morning.get("items") or []:
            out.append(
                {
                    "market": "kr",
                    "code": it["code"],
                    "name": it["name"],
                    "board": it.get("market", ""),
                    "tags": it.get("tags", []),
                    "section": morning.get("id", "MORNING_BREAKOUT"),
                    "section_name": morning.get("name", "모닝 브레이크아웃"),
                    "type": it.get("type", ""),
                    "found_on": day,
                    "entry": None,  # 그날 종가가 나온 뒤 채운다
                    "stop": None,
                    "reason": it.get("reason", ""),
                }
            )
    return out


def _backfill_stop(p):
    """손절가를 기록하기 전에 만든 스크리너 결과(첫 박제분)는 손절가가 없다. 발굴일까지의 일봉으로 같은 규칙대로 계산한다."""
    try:
        bars = _daily(p["symbol"], "3y")
    except Exception:
        return None
    upto = [i for i, d in enumerate(bars["date"]) if d <= p["found_on"]]
    if not upto:
        return None
    cut = upto[-1] + 1
    sub = {k: v[:cut] for k, v in bars.items()}
    return stop_price(p["section"], p["type"], sub, p.get("reason", ""), 2 if p["market"] == "us" else 0)


def snapshot(tracking, screening_kr, screening_us, morning, now):
    """새로 뽑힌 종목을 박제한다. 반환: 새로 박제한 수."""
    positions = tracking.setdefault("positions", [])
    known = {p["id"] for p in positions}
    active = {(p["market"], p["code"], p["section"]) for p in positions if p["status"] == "ACTIVE"}
    added = 0
    for c in _candidates(screening_kr, screening_us, morning):
        pid = f"{c['market']}:{c['code']}:{c['section']}:{c['found_on']}"
        if pid in known or (c["market"], c["code"], c["section"]) in active:
            continue
        p = {
            "id": pid,
            **{k: c[k] for k in ("market", "code", "name", "board", "tags", "section", "section_name", "type", "found_on", "entry", "stop")},
            "symbol": _symbol(c["market"], c["code"], c["board"]),
            "status": "ACTIVE",
            "rets": [],  # D+1 부터 일별 종가 누적 수익률(%)
            "last_date": "",
            "snapshot_at": now.isoformat(),
            "reason": c["reason"],
        }
        if p["stop"] is None and p["section"] != "MORNING_BREAKOUT":
            p["stop"] = _backfill_stop(p)
        positions.append(p)
        known.add(pid)
        active.add((p["market"], p["code"], p["section"]))
        added += 1
    return added


# ──────────────────────────────────────────────────────────────────────────
# 추적
# ──────────────────────────────────────────────────────────────────────────
def _evaluate(p, bars, now):
    """발굴 다음 날부터 최대 20거래일을 처음부터 다시 계산한다(같은 입력이면 항상 같은 결과).
    반환: 바뀌었으면 True."""
    closed = last_closed_day(p["market"], now).isoformat()
    rows = [
        (bars["date"][i], bars["high"][i], bars["low"][i], bars["close"][i])
        for i in range(len(bars["date"]))
        if bars["date"][i] <= closed
    ]
    found = [r for r in rows if r[0] == p["found_on"]]

    # 섹션4: 진입가(발굴일 종가)와 손절가를 그날 마감 뒤에 채운다. 채운 뒤로는 고정.
    if p.get("entry") is None:
        if not found:
            return False
        px = (lambda v: round(v, 2)) if p["market"] == "us" else (lambda v: int(round(v)))  # 국장은 원 단위 정수
        p["entry"] = px(found[0][3])
        p["stop"] = px(p["entry"] * (1 - MORNING_STOP_PCT / 100))

    entry, stop = p["entry"], p.get("stop")
    if not entry:
        return False

    after = [r for r in rows if r[0] > p["found_on"]][:TRACK_DAYS]
    rets, hwm, hwm_day, mdd = [], None, None, 0.0
    status, final, closed_on = "ACTIVE", None, ""
    for n, (d, hi, lo, cl) in enumerate(after, start=1):
        r_close = (cl / entry - 1) * 100
        r_high = (hi / entry - 1) * 100
        r_low = (lo / entry - 1) * 100
        rets.append(round(r_close, 2))
        if hwm is None or r_high > hwm:
            hwm, hwm_day = r_high, n
        mdd = min(mdd, r_low)
        if stop and cl < stop:
            status, final, closed_on = "SL_HIT", r_close, d
            break
        if n == TRACK_DAYS:
            status, final, closed_on = "EXPIRED", r_close, d

    new = {
        "rets": rets,
        "hwm": round(hwm, 2) if hwm is not None else None,
        "hwm_day": hwm_day,
        "mdd": round(mdd, 2),
        "status": status,
        "final_ret": round(final, 2) if final is not None else None,
        "closed_on": closed_on,
        "last_date": after[-1][0] if after else "",
    }
    changed = any(p.get(k) != v for k, v in new.items())
    p.update(new)
    return changed


def update(tracking, now, markets=("kr", "us")):
    """추적 중인 종목을 갱신한다. 시장마다 하루 한 번(그 시장 마감 뒤)만 야후를 부른다.
    처음 박제된 종목(last_date 가 빈 것)은 시각과 상관없이 바로 계산한다(과거 발굴분 소급)."""
    marks = tracking.setdefault("updated_for", {})
    todo = []
    for market in markets:
        mark = last_closed_day(market, now).isoformat()
        due = marks.get(market) != mark
        for p in tracking.get("positions", []):
            if p["market"] != market or p["status"] != "ACTIVE":
                continue
            if due or not p.get("last_date"):
                todo.append(p)
        if due:
            marks[market] = mark

    if not todo:
        return 0

    def one(p):
        try:
            return p, _daily(p["symbol"])
        except Exception:
            return p, None

    changed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for p, bars in pool.map(one, todo):
            if bars and _evaluate(p, bars, now):
                changed += 1
    print(f"[INFO] 성과 추적: {len(todo)}종목 확인, {changed}종목 갱신")
    return changed


def summarize(tracking):
    """화면 상단 요약. 종결된 것만으로 승률·평균을 낸다(추적 중인 건 결과가 아직 없다)."""
    ps = tracking.get("positions", [])
    closed = [p for p in ps if p["status"] in ("SL_HIT", "EXPIRED") and p.get("final_ret") is not None]

    def stats(group):
        if not group:
            return None
        n = len(group)
        return {
            "count": n,
            "win_rate": round(sum(1 for p in group if p["final_ret"] > 0) / n * 100, 1),
            "avg_final": round(sum(p["final_ret"] for p in group) / n, 2),
            "avg_hwm": round(sum(p.get("hwm") or 0 for p in group) / n, 2),
            "avg_mdd": round(sum(p.get("mdd") or 0 for p in group) / n, 2),
            "sl_rate": round(sum(1 for p in group if p["status"] == "SL_HIT") / n * 100, 1),
        }

    by = {}
    for p in closed:
        by.setdefault(f"{p['market']}:{p['section']}", []).append(p)
    return {
        "active": sum(1 for p in ps if p["status"] == "ACTIVE"),
        "closed": len(closed),
        "sl_hit": sum(1 for p in closed if p["status"] == "SL_HIT"),
        "expired": sum(1 for p in closed if p["status"] == "EXPIRED"),
        "overall": stats(closed),
        "by_section": {k: stats(v) for k, v in by.items()},
    }
