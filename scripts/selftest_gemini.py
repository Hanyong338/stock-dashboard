"""제미나이 키·모델 선택 규칙 자가 시험. 구글에 진짜 요청을 보내지 않고 가짜 응답으로만 확인한다(비용·한도 0).

확인하는 규칙(사용자 지정, 2026-10-07):
- 낮(07~18시)엔 무료 모델 전부의 하루 한도가 모두 차도 유료로 넘기지 않는다(10/8 변경, 당잠사만 예외)
- 무료가 혼잡(503)으로 막혔을 뿐이면 유료로 넘기지 않는다
- 무료 모델이 하나라도 남아 있으면 무료로 처리한다
- 유료 키를 새로 넣으면 예전 키 때문에 걸어둔 쉬는 기간이 풀린다
- 휴식 시간에는 아무 요청도 보내지 않는다
- 유료 키가 분당 제한(429)에 잠깐 걸려도 몇 시간씩 쉬게 하지 않는다

결과는 docs/data/gemini_selftest.json 에 남긴다(워크플로 로그는 저장소 관리자만 볼 수 있어서).
실제 한도 기록(gemini_quota.json)은 건드리지 않는다.
"""
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import summarize as s  # noqa: E402

OUT_FILE = Path(__file__).resolve().parent.parent / "docs" / "data" / "gemini_selftest.json"
ACTIVE_NOW = datetime.datetime(2026, 10, 8, 1, 0, tzinfo=datetime.timezone.utc)  # 한국시간 10:00
QUIET_NOW = datetime.datetime(2026, 10, 8, 12, 0, tzinfo=datetime.timezone.utc)  # 한국시간 21:00
PAID_NOW = datetime.datetime(2026, 10, 8, 10, 0, tzinfo=datetime.timezone.utc)  # 한국시간 19:00(저녁 유료 시간대)

OK_BODY = {
    "candidates": [{"content": {"parts": [{"text": json.dumps({"key_summary": "ok", "report_markdown": "## ok"})}]}}],
    "usageMetadata": {},
}
DAILY_429 = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota",
        "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]}],
    }
}
MINUTE_429 = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "rate limit",
        "details": [{"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}]}],
    }
}
BUSY_503 = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "high demand"}}


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self.ok = 200 <= status < 300

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


def _reset(tmpdir, now):
    """모듈의 실행 상태를 처음으로 되돌리고, 한도 기록 파일을 임시 파일로 바꾼다."""
    s.QUOTA_FILE = Path(tmpdir) / "quota.json"
    s._quota = {}
    s._busy = set()
    s._paid_depleted_this_run = False
    s._fails_this_run = 0
    s._halted_this_run = False
    s.FAIL_PAUSE_SECONDS = 0
    s._now = lambda: now
    os.environ["GEMINI_API_KEY_FREE"] = "FREE-KEY"
    os.environ["GEMINI_API_KEY"] = "PAID-KEY"


def _run(tmpdir, responder, now=ACTIVE_NOW, before=None, **call_kwargs):
    """responder(kind, model) -> FakeResp. 보낸 요청 목록과 결과(성공/미룸/오류)를 돌려준다."""
    _reset(tmpdir, now)
    if before:
        before()
    calls = []

    def fake_post(payload, key, timeout=90, model=None):
        kind = "free" if key == "FREE-KEY" else "paid"
        calls.append(f"{kind}:{model}")
        return responder(kind, model)

    s._post = fake_post
    try:
        s.call_gemini("sys", "user", {}, "selftest", **call_kwargs)
        outcome = "success"
    except s.GeminiUnavailable:
        outcome = "deferred"
    except Exception as e:  # 예상 못 한 오류도 결과로 남긴다
        outcome = f"error: {type(e).__name__}: {e}"
    return calls, outcome


def main():
    results = []

    def check(name, ok, detail):
        results.append({"case": name, "pass": bool(ok), "detail": detail})

    with tempfile.TemporaryDirectory() as tmp:
        # 1. 낮(무료 시간대)에 무료 모델 전부 하루 한도 → 유료로 안 넘어가고 18시까지 미룸(10/8 변경)
        calls, out = _run(tmp, lambda k, m: FakeResp(200, OK_BODY) if k == "paid" else FakeResp(429, DAILY_429))
        check(
            "낮에 무료 모델 전부 한도 소진 → 유료로 안 넘어감(18시까지 미룸)",
            out == "deferred" and sum(c.startswith("free") for c in calls) == len(s.all_models()) and not any(c.startswith("paid") for c in calls),
            {"calls": calls, "outcome": out},
        )

        # 2. 무료가 혼잡(503)뿐 → 유료 안 씀, 미룸
        calls, out = _run(tmp, lambda k, m: FakeResp(200, OK_BODY) if k == "paid" else FakeResp(503, BUSY_503))
        check(
            "무료 혼잡(503) → 유료로 안 넘어감",
            out == "deferred" and not any(c.startswith("paid") for c in calls),
            {"calls": calls, "outcome": out},
        )

        # 3. 한 모델만 한도, 다음 모델 성공 → 무료로 처리
        first_free = []

        def one_exhausted(k, m):
            if k == "paid":
                return FakeResp(200, OK_BODY)
            if not first_free:
                first_free.append(m)  # 처음 부른 무료 모델만 한도 소진
            return FakeResp(429, DAILY_429) if m == first_free[0] else FakeResp(200, OK_BODY)

        calls, out = _run(tmp, one_exhausted)
        check(
            "무료 모델 하나만 한도 → 남은 무료로 처리",
            out == "success" and not any(c.startswith("paid") for c in calls),
            {"calls": calls, "outcome": out},
        )

        # 4. 예전 키 때문에 걸어둔 유료 쉬는 기간 + 새 유료 키 → 쉬는 기간 풀림
        def old_bench():
            s._quota["paid_key_fp"] = "oldkey00"
            s._quota["paid_unavailable_until"] = (ACTIVE_NOW + datetime.timedelta(days=6)).isoformat()

        calls, out = _run(
            tmp, lambda k, m: FakeResp(200, OK_BODY) if k == "paid" else FakeResp(429, DAILY_429), before=old_bench,
            now=PAID_NOW,
        )
        check(
            "새 유료 키 → 예전 쉬는 기간 무시",
            out == "success" and any(c.startswith("paid") for c in calls),
            {"calls": calls, "outcome": out},
        )

        # 5. 휴식 시간 → 요청 0번
        calls, out = _run(tmp, lambda k, m: FakeResp(200, OK_BODY), now=QUIET_NOW)
        check("휴식 시간 → 요청 안 보냄", out == "deferred" and not calls, {"calls": calls, "outcome": out})

        # 6. 무료 한도 소진 + 유료가 분당 제한(429)에 잠깐 걸림 → 유료를 몇 시간씩 쉬게 하지 않음
        first_paid = []

        def paid_minute_limit(k, m):
            if k == "free":
                return FakeResp(429, DAILY_429)
            if not first_paid:
                first_paid.append(m)  # 처음 부른 유료 모델만 분당 제한
            return FakeResp(429, MINUTE_429) if m == first_paid[0] else FakeResp(200, OK_BODY)

        calls, out = _run(tmp, paid_minute_limit, now=PAID_NOW)
        check(
            "유료 분당 제한 → 다른 모델로 이어서 처리, 길게 쉬지 않음",
            out == "success" and "paid_unavailable_until" not in s._quota,
            {"calls": calls, "outcome": out},
        )

        # 7. gemini_ready: 무료 모델 전부 한도 소진 표시 → 낮엔 준비 안 됨(유료 안 씀), 저녁 유료 시간대엔 준비됨
        _reset(tmp, ACTIVE_NOW)
        until = (ACTIVE_NOW + datetime.timedelta(hours=5)).isoformat()
        s._quota["exhausted"] = {m: until for m in s.all_models()}
        day_ready = s.gemini_ready()
        s._now = lambda: PAID_NOW
        check("무료 소진: 낮엔 대기, 18시 유료 시간대엔 진행(gemini_ready)", not day_ready and s.gemini_ready(), {})

        # 8. 무료 전용 요청(묶음 시험): 무료 모델 전부 한도 소진 → 유료 안 쓰고 미룸
        calls, out = _run(
            tmp, lambda k, m: FakeResp(200, OK_BODY) if k == "paid" else FakeResp(429, DAILY_429), allow_paid=False
        )
        check(
            "무료 전용 요청: 무료 한도 소진이어도 유료로 안 넘어감",
            out == "deferred" and not any(c.startswith("paid") for c in calls),
            {"calls": calls, "outcome": out},
        )

        # 9. 무료가 당잠사 몫(4번)만 남았을 때 일반 영상 → 그 몫은 안 쓰고 유료로
        def leave_four():
            day = s._quota_day()
            s._quota["sent"] = {day: {m: {"503": s.FREE_RPD - 1} for m in s.all_models()}}

        calls, out = _run(tmp, lambda k, m: FakeResp(200, OK_BODY), before=leave_four, reserve=4)
        check(
            "일반 영상: 당잠사 몫으로 남긴 무료 4번은 안 쓰고, 낮이라 유료로도 안 넘어감",
            out == "deferred" and not calls,
            {"calls": calls, "outcome": out},
        )

        # 10. 당잠사(유료 전용): 무료가 남아 있어도 유료로만 바로 처리
        calls, out = _run(tmp, lambda k, m: FakeResp(200, OK_BODY), paid_only=True)
        check(
            "당잠사: 무료가 남아 있어도 유료로 바로 처리",
            out == "success" and calls and all(c.startswith("paid") for c in calls),
            {"calls": calls, "outcome": out},
        )

        # 11. 시간대 경계: 06:59 휴식 / 07:00·17:59 무료 / 18:00·20:59 유료만 / 21:00 휴식
        def kst(h, mi):
            return datetime.datetime(2026, 10, 8, h, mi, tzinfo=datetime.timezone.utc) - datetime.timedelta(hours=9)

        everything_ok = lambda k, m: FakeResp(200, OK_BODY)  # noqa: E731
        seen = {}
        for label, t in [("06:59", kst(6, 59)), ("07:00", kst(7, 0)), ("17:59", kst(17, 59)),
                         ("18:00", kst(18, 0)), ("20:59", kst(20, 59)), ("21:00", kst(21, 0))]:
            calls, out = _run(tmp, everything_ok, now=t)
            seen[label] = {"outcome": out, "calls": calls}
        expect = {
            "06:59": ("deferred", None),
            "07:00": ("success", "free"),
            "17:59": ("success", "free"),
            "18:00": ("success", "paid"),
            "20:59": ("success", "paid"),
            "21:00": ("deferred", None),
        }
        ok = all(
            seen[k]["outcome"] == out and (not kind and not seen[k]["calls"] or kind and seen[k]["calls"] and all(c.startswith(kind) for c in seen[k]["calls"]))
            for k, (out, kind) in expect.items()
        )
        check("시간대: 07~18 무료 / 18~21 유료만 / 21~07 휴식", ok, seen)

        # 12. 당잠사(유료 전용): 휴식 시간(새벽 06:30)에도 올라오자마자 유료로 처리
        calls, out = _run(tmp, everything_ok, now=kst(6, 30), paid_only=True)
        check(
            "당잠사: 휴식 시간(06:30)에도 유료로 바로 처리",
            out == "success" and calls and all(c.startswith("paid") for c in calls),
            {"calls": calls, "outcome": out},
        )

        # 14. 무료 전용 요청은 저녁 유료 시간대엔 안 돌림
        calls, out = _run(tmp, everything_ok, now=kst(19, 0), allow_paid=False)
        check("무료 전용 요청: 저녁 유료 시간대엔 안 돌림", out == "deferred" and not calls, {"calls": calls})

        # 13. 일회성 정지(PAUSE_UNTIL_KST) 중엔 낮이어도 요청 안 보냄
        calls, out = _run(tmp, everything_ok, now=kst(10, 0) - datetime.timedelta(days=1))
        check("일회성 정지(10/7 저녁~10/8 07시) 중 요청 안 보냄", out == "deferred" and not calls, {"calls": calls})

        # 15. 혼잡 거절 간격 = 초기화까지 남은 시간 ÷ 남은 무료 한도
        #  - 07:00, 한도 가득(모델 수 × 20회) → 간격 = 9시간 ÷ 남은 한도 → 20분 실행당 허용 거절 수에서 멈춤,
        #    간격이 20분보다 짧으니 다음 실행에선 바로 다시(긴 쉬기 없음).
        #    모델 4개(80회)일 땐 약 6.75분 간격 → 2번, 10/10 모델 2개(40회)부터는 약 13.5분 간격 → 1번.
        full = s.FREE_RPD * len(s.all_models())
        minutes_to_reset = (s._next_quota_reset(kst(7, 0)) - kst(7, 0)).total_seconds() / 60
        expected_fails = max(1, int(s.RUN_INTERVAL_MINUTES // (minutes_to_reset / full)))
        calls, out = _run(tmp, lambda k, m: FakeResp(503, BUSY_503), now=kst(7, 0))
        check(
            "07시 혼잡: 남은 한도에 맞춘 횟수만큼만 거절받고 멈춤(아침에 한도를 다 쓰지 않음), 다음 실행은 바로",
            out == "deferred" and len(calls) == expected_fails and s._halted_this_run and "backoff_until" not in s._quota,
            {"calls": calls, "expected_fails": expected_fails, "pace_minutes": round(s._pace_minutes(), 2)},
        )

        #  - 16:30(막 초기화), 한도 가득 → 간격이 20분 안팎 이상 → 실행당 1번
        calls, out = _run(tmp, lambda k, m: FakeResp(503, BUSY_503), now=kst(16, 30))
        check("16:30 혼잡: 실행당 거절 1번에서 멈춤", out == "deferred" and len(calls) == 1 and s._halted_this_run, {"calls": calls})

        #  - 15:30(초기화 30분 전), 한도 가득 → 간격이 아주 좁음 → 멈추지 않고 다음 영상에서 같은 모델을 또 부름
        calls, out = _run(tmp, lambda k, m: FakeResp(200, OK_BODY) if k == "paid" else FakeResp(503, BUSY_503), now=kst(15, 30))
        busy_after = set(s._busy)
        calls2 = []
        s._post = lambda payload, key, timeout=90, model=None: calls2.append(model) or FakeResp(503, BUSY_503)
        try:
            s.call_gemini("sys", "user", {}, "selftest-2")
        except s.GeminiUnavailable:
            pass
        check(
            "초기화 직전: 간격이 좁아 다음 영상에서 바로 다시 시도, 유료로는 안 넘어감",
            out == "deferred" and not any(c.startswith("paid") for c in calls) and not busy_after
            and not s._halted_this_run and "backoff_until" not in s._quota and len(calls2) == len(s.all_models()),
            {"calls": calls, "calls_next_video": calls2},
        )

        # 모델 목록 자동 관리(10/10): 새 Flash 모델은 추가, 사라진 모델은 제외, 목록을 못 읽으면 그대로
        base = [s.MODEL] + [m for m in s.FALLBACK_MODELS if m != s.MODEL]
        newest = max(base, key=s._flash_version)
        major, minor = s._flash_version(newest)
        next_model = f"gemini-{major}.{minor + 1}-flash"

        def run_refresh(names):
            s._quota.pop("models_checked_on", None)
            s._list_models = lambda key: names
            return s.refresh_model_list()

        _reset(tmp, ACTIVE_NOW)
        listed = [f"models/{m}" for m in base] + [
            f"models/{next_model}",
            f"models/{next_model}-lite",  # lite·preview 는 넣지 않는다
            f"models/gemini-{major}.{minor + 2}-flash-preview",
        ]
        changes = run_refresh(listed)
        check(
            "새 Flash 모델 자동 추가(lite·preview 제외)",
            s.all_models() == base + [next_model] and len(changes) == 1,
            {"models": s.all_models(), "changes": changes},
        )

        changes = run_refresh([f"models/{m}" for m in base[1:]] + [f"models/{next_model}"])
        check(
            "구글 목록에서 사라진 모델 자동 제외",
            s.all_models() == base[1:] + [next_model] and any(base[0] in c for c in changes),
            {"models": s.all_models(), "changes": changes},
        )

        _reset(tmp, ACTIVE_NOW)
        changes = run_refresh(["models/some-other-model"])
        check(
            "목록을 제대로 못 읽으면 아무것도 빼지 않음",
            s.all_models() == base and not changes,
            {"models": s.all_models(), "changes": changes},
        )

        _reset(tmp, ACTIVE_NOW)
        s._list_models = lambda key: None
        s._quota.pop("models_checked_on", None)
        check(
            "목록 조회 실패 → 그대로 두고 내일 다시",
            s.refresh_model_list() == [] and s.all_models() == base and "models_checked_on" not in s._quota,
            {"models": s.all_models()},
        )

    summary = {"all_pass": all(r["pass"] for r in results), "results": results}
    OUT_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
