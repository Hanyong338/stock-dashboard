"""시그널 스크리너 성과 추적 (1개월 = 20거래일 포워드, 목표가 없음).

1. 박제 (Immutable Snapshot)
   발굴일(D-0) 종가 = 진입가, 그날 기준 손절가를 영구 고정한다. 이후 덮어쓰지 않는다.
   · 섹션 1~3 (국장·미장): 진입가 = 발굴일 종가. 지지선 = screening.stop_price() (섹션 청산 규칙의 손절선)
   · 섹션 4 모닝 브레이크아웃: 규칙대로 발굴일 '종가'가 진입가. 손절은 청산 규칙의 '-2.0% 기계적 손절'
   · 같은 종목·섹션이 대기/추적 중이면(추세주는 며칠씩 연달아 뽑힌다) 새로 박제하지 않는다.

2. 손절 완충 버퍼
   이평선 가격을 그대로 손절가로 쓰면 0.1~0.5% 흔들림에 손절로 끝난다(첫 박제분 눌림목 중간값 -0.7%).
   지지선에서 SL_BUFFER_PCT 만큼 아래를 손절가로 쓴다(명세: -2.0% ~ -3.0% 중 가운데).
   섹션4 는 지지선이 아니라 진입가 기준 기계적 손절이라 버퍼를 따로 얹지 않는다.

3. 상태
   PENDING  발굴 뒤 D+1 시세가 아직 없음 (섹션4 는 발굴일 종가 확정 전도 여기)
   ACTIVE   D+1 ~ D+20 추적 중
   SL_HIT   종가가 손절가 아래로 마감 -> 그 종가로 손실 확정, 추적 종료
   EXPIRED  20거래일 손절 없이 버팀 -> 20일차 종가로 정산

4. 일별 지표 (발굴 다음 거래일부터 다시 계산 — 같은 입력이면 항상 같은 결과)
   · 일별 종가 누적 수익률 / 현재가
   · 최고 도달률(HWM): 장중 고가 기준 + 도달 일차
   · 최대 낙폭(MDD): 장중 저가 기준, 진입가 대비 (아래로 안 갔으면 0)
"""
import datetime
from concurrent.futures import ThreadPoolExecutor

from screening import WORKERS, _bars_from, _yahoo_chart, stop_price

TRACK_DAYS = 20
SL_BUFFER_PCT = 2.5
# 손절·종결 판정 규칙을 바꾸면 올린다. 올리면 종결된 기록까지 새 규칙으로 다시 계산한다(진입가는 그대로).
# 2: 손절 버퍼 도입. 버퍼 이전 코드가 먼저 돌아 미장 7건이 버퍼 없는 손절가로 손절 판정을 받았는데,
#    그중 6건은 지지선을 0.1~1.3% 차이로 깬 노이즈였다(버퍼 손절가로는 유지).
TRACKING_RULES_VERSION = 2
MORNING_STOP_PCT = 2.0  # 섹션4 '-2.0% 기계적 손절'
OPEN_STATUSES = ("PENDING", "ACTIVE")
KST = datetime.timezone(datetime.timedelta(hours=9))
KR_CLOSED_AT = datetime.time(15, 40)  # 정규장 15:30 마감 + 여유
US_CLOSED_AT = datetime.time(16, 10)  # 미 동부 16:00 마감 + 여유

try:
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
except Exception:  # 서버에 시간대 정보가 없을 때. 서머타임 기간 기준으로 근사한다
    ET = datetime.timezone(datetime.timedelta(hours=-4))


def _px(market, v):
    """가격 표기 단위. 국장 원 단위 정수, 미장 센트."""
    if v is None:
        return None
    return round(v, 2) if market == "us" else int(round(v))


def _buffered(market, line):
    return _px(market, line * (1 - SL_BUFFER_PCT / 100)) if line else None


# ──────────────────────────────────────────────────────────────────────────
# 시장 시각
# ──────────────────────────────────────────────────────────────────────────
def last_closed_day(market, now):
    """그 시장에서 마지막으로 마감이 끝난 날짜(달력 기준). 휴장이면 야후에 봉이 없어 새 기록이 안 생길 뿐이다."""
    local = now.astimezone(ET if market == "us" else KST)
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
                        "market": market, "code": it["code"], "name": it["name"],
                        "board": it.get("market", ""), "tags": it.get("tags", []),
                        "section": sec["id"], "section_name": sec.get("name", ""), "type": it.get("type", ""),
                        "found_on": data["as_of_trading_day"], "entry": it.get("close"),
                        "stop_line": it.get("stop"), "reason": it.get("reason", ""),
                    }
                )
    if isinstance(morning, dict) and morning.get("as_of") and not morning.get("error"):
        for it in morning.get("items") or []:
            out.append(
                {
                    "market": "kr", "code": it["code"], "name": it["name"],
                    "board": it.get("market", ""), "tags": it.get("tags", []),
                    "section": morning.get("id", "MORNING_BREAKOUT"),
                    "section_name": morning.get("name", "모닝 브레이크아웃"), "type": it.get("type", ""),
                    "found_on": morning["as_of"][:10],
                    "entry": None,  # 그날 종가가 나온 뒤 채운다
                    "stop_line": None, "reason": it.get("reason", ""),
                }
            )
    return out


def _backfill_line(p):
    """지지선을 기록하기 전에 만든 스크리너 결과는 지지선이 없다. 발굴일까지의 일봉으로 같은 규칙대로 계산한다."""
    try:
        bars = _daily(p["symbol"], "3y")
    except Exception:
        return None
    upto = [i for i, d in enumerate(bars["date"]) if d <= p["found_on"]]
    if not upto:
        return None
    sub = {k: v[: upto[-1] + 1] for k, v in bars.items()}
    return stop_price(p["section"], p["type"], sub, p.get("reason", ""), 2 if p["market"] == "us" else 0)


def _migrate(positions):
    """버퍼 규칙 이전(2026-09-25)에 박제한 기록을 새 형식으로 옮긴다.
    그때 'stop' 에 넣은 값은 버퍼 없는 지지선이라 stop_line 으로 옮기고 버퍼를 얹는다.
    추적 기록(D+1)이 생기기 전에 규칙이 바뀐 것이라 결과에 영향이 없다. 진입가는 건드리지 않는다."""
    for p in positions:
        if "stop_line" in p:
            continue
        if p["section"] == "MORNING_BREAKOUT":
            p["stop_line"] = None
        else:
            p["stop_line"] = p.get("stop")
            p["stop"] = _buffered(p["market"], p["stop_line"])
        if p["status"] == "ACTIVE" and not p.get("rets"):
            p["status"] = "PENDING"


def _reopen_on_rule_change(tracking):
    """규칙 버전이 바뀌면 종결된 기록을 다시 열어 새 규칙으로 처음부터 계산하게 한다.
    _evaluate 는 발굴 다음 날부터 매번 전부 다시 계산하므로, 새 규칙에서도 손절이면 그대로 다시 종결된다."""
    if tracking.get("rules_version") == TRACKING_RULES_VERSION:
        return
    for p in tracking.get("positions", []):
        if p["status"] not in OPEN_STATUSES:
            p["status"] = "ACTIVE"
        p.pop("checked_for", None)
    tracking["rules_version"] = TRACKING_RULES_VERSION


def _dedupe_open(positions):
    """같은 종목·섹션이 동시에 둘 이상 열려 있으면 가장 먼저 발굴된 것만 남긴다.
    잘못된 손절 판정으로 종결됐던 종목이 그사이 다시 뽑혀 새로 박제됐다가, 재계산으로 옛 기록이
    다시 열리면 둘이 겹친다(실제로 SU·CNH·VIST 가 그랬다). 규칙상 추적 중엔 새로 박제하지 않는다."""
    first = {}
    for p in sorted(positions, key=lambda x: x["found_on"]):
        if p["status"] in OPEN_STATUSES:
            first.setdefault((p["market"], p["code"], p["section"]), p["id"])
    keep = [
        p for p in positions
        if p["status"] not in OPEN_STATUSES or first.get((p["market"], p["code"], p["section"])) == p["id"]
    ]
    removed = len(positions) - len(keep)
    positions[:] = keep
    return removed


def snapshot(tracking, screening_kr, screening_us, morning, now):
    """새로 뽑힌 종목을 박제한다. 반환: 새로 박제한 수."""
    positions = tracking.setdefault("positions", [])
    _migrate(positions)
    _reopen_on_rule_change(tracking)
    dup = _dedupe_open(positions)
    if dup:
        print(f"[INFO] 성과 추적: 겹친 박제 {dup}건 정리")
    known = {p["id"] for p in positions}
    open_keys = {(p["market"], p["code"], p["section"]) for p in positions if p["status"] in OPEN_STATUSES}
    added = 0
    for c in _candidates(screening_kr, screening_us, morning):
        pid = f"{c['market']}:{c['code']}:{c['section']}:{c['found_on']}"
        key = (c["market"], c["code"], c["section"])
        if pid in known or key in open_keys:
            continue
        p = {
            "id": pid,
            **{k: c[k] for k in ("market", "code", "name", "board", "tags", "section", "section_name", "type", "found_on", "entry", "stop_line")},
            "symbol": _symbol(c["market"], c["code"], c["board"]),
            "status": "PENDING",
            "rets": [],  # D+1 부터 일별 종가 누적 수익률(%)
            "last_date": "",
            "snapshot_at": now.isoformat(),
            "reason": c["reason"],
        }
        if p["section"] == "MORNING_BREAKOUT":
            p["stop"] = None  # 진입가(발굴일 종가) 확정 때 함께 정한다
        else:
            if p["stop_line"] is None:
                p["stop_line"] = _backfill_line(p)
            p["stop"] = _buffered(p["market"], p["stop_line"])
        positions.append(p)
        known.add(pid)
        open_keys.add(key)
        added += 1
    return added


# ──────────────────────────────────────────────────────────────────────────
# 추적
# ──────────────────────────────────────────────────────────────────────────
def _evaluate(p, bars, now):
    """발굴 다음 날부터 최대 20거래일을 처음부터 다시 계산한다. 반환: 바뀌었으면 True."""
    market = p["market"]
    closed = last_closed_day(market, now).isoformat()
    rows = [
        (bars["date"][i], bars["high"][i], bars["low"][i], bars["close"][i])
        for i in range(len(bars["date"]))
        if bars["date"][i] <= closed  # 장중 미완성 봉은 뺀다
    ]

    if p.get("entry") is None:  # 섹션4: 발굴일 종가가 나오면 진입가·손절가 확정. 이후 고정
        found = [r for r in rows if r[0] == p["found_on"]]
        if not found:
            return False
        p["entry"] = _px(market, found[0][3])
        p["stop"] = _px(market, p["entry"] * (1 - MORNING_STOP_PCT / 100))

    entry, stop = p["entry"], p.get("stop")
    if not entry:
        return False

    after = [r for r in rows if r[0] > p["found_on"]][:TRACK_DAYS]
    rets, hwm, hwm_day, mdd = [], None, None, 0.0
    status, final, closed_on, last_close = ("ACTIVE" if after else "PENDING"), None, "", None
    for n, (d, hi, lo, cl) in enumerate(after, start=1):
        r_close = (cl / entry - 1) * 100
        rets.append(round(r_close, 2))
        last_close = cl
        r_high = (hi / entry - 1) * 100
        if hwm is None or r_high > hwm:
            hwm, hwm_day = r_high, n
        mdd = min(mdd, (lo / entry - 1) * 100)
        if stop and cl < stop:
            status, final, closed_on = "SL_HIT", r_close, d
            break
        if n == TRACK_DAYS:
            status, final, closed_on = "EXPIRED", r_close, d

    new = {
        "rets": rets,
        "last_close": _px(market, last_close),
        "hwm": round(hwm, 2) if hwm is not None else None,
        "hwm_day": hwm_day,
        "mdd": round(mdd, 2) if after else None,
        "status": status,
        "final_ret": round(final, 2) if final is not None else None,
        "closed_on": closed_on,
        "last_date": after[-1][0] if after else "",
    }
    changed = any(p.get(k) != v for k, v in new.items())
    p.update(new)
    return changed


def update(tracking, now, markets=("kr", "us")):
    """대기·추적 중인 종목을 갱신한다. 종목마다 '그 시장의 마지막 마감일' 기준으로 하루 한 번만 야후를 부른다.
    새로 박제된 종목은 아직 확인 표시가 없으니 바로 계산된다(과거 발굴분 소급)."""
    todo = []
    for market in markets:
        mark = last_closed_day(market, now).isoformat()
        for p in tracking.get("positions", []):
            if p["market"] == market and p["status"] in OPEN_STATUSES and p.get("checked_for") != mark:
                todo.append((p, mark))
    if not todo:
        return 0

    def one(item):
        p, mark = item
        try:
            return p, mark, _daily(p["symbol"])
        except Exception:
            return p, mark, None

    changed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for p, mark, bars in pool.map(one, todo):
            if not bars:
                continue  # 조회 실패. 확인 표시를 안 남겨 다음 실행에서 다시 시도한다
            if _evaluate(p, bars, now):
                changed += 1
            p["checked_for"] = mark
    print(f"[INFO] 성과 추적: {len(todo)}종목 확인, {changed}종목 갱신")
    return changed


def summarize(tracking):
    """화면 상단 요약용. 화면도 같은 방식으로 직접 계산한다(국장/미장 필터 때문에)."""
    ps = tracking.get("positions", [])
    with_data = [p for p in ps if p.get("rets")]
    active = [p for p in ps if p["status"] == "ACTIVE" and p.get("rets")]
    closed = [p for p in ps if p["status"] in ("SL_HIT", "EXPIRED") and p.get("final_ret") is not None]
    pct = lambda part, whole: round(len(part) / len(whole) * 100, 1) if whole else None
    avg = lambda group, k: round(sum(p.get(k) or 0 for p in group) / len(group), 2) if group else None
    return {
        "total": len(ps),
        "pending": sum(1 for p in ps if p["status"] == "PENDING"),
        "active": sum(1 for p in ps if p["status"] == "ACTIVE"),
        "closed": len(closed),
        "in_the_money": pct([p for p in active if p["rets"][-1] > 0], active),
        "hit_5": pct([p for p in with_data if (p.get("hwm") or 0) >= 5], with_data),
        "hit_10": pct([p for p in with_data if (p.get("hwm") or 0) >= 10], with_data),
        "avg_hwm": avg(with_data, "hwm"),
        "avg_mdd": avg(with_data, "mdd"),
        "closed_win_rate": pct([p for p in closed if p["final_ret"] > 0], closed),
        "closed_avg_final": avg(closed, "final_ret"),
    }
