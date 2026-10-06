"""Google Gemini 무료 API로 자막을 분석해 투자 전략 리포트를 생성한다.
카드 등록 없이 https://aistudio.google.com/apikey 에서 키를 받아 GEMINI_API_KEY로 등록하면 된다.
"""
import datetime
import json
import os
from pathlib import Path

import requests

# 한 실행 안에서는 모델마다 딱 한 번만 요청한다. 거절(503 혼잡 등)되면 그 모델은 이번 실행 동안 다시 부르지 않고
# 다음 모델로 넘어가며, 다음 실행(20분 뒤)에 다시 시도한다.
# 2026-10-05~06 밤: 영상마다 모델당 2~3번씩 재시도하다 보니 성공은 4편인데 요청은 수백 번 나갔고,
# 무료 하루 한도(모델당 100회)가 실패한 요청으로 다 찼다(429). 거절된 요청도 한도를 깎는 것으로 보인다.
# (자막은 이미 받아 저장해 두므로 요약이 실패해도 자막 크레딧은 다시 나가지 않는다)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
# 기본 모델이 혼잡(503)·한도(429)·시간초과로 계속 거절하면 차례로 넘어갈 모델. 모두 무료 사용량이 있다(공식 가격표).
# 2026-10-05 무료로 전환하자 3.5-flash(공식 문서상 'Legacy')가 503 을 반복해 요약이 몇 시간 밀렸다.
# 무료 하루 한도도 모델마다 따로라 한 모델 한도가 차도 다음 모델로 이어갈 수 있다.
# 요약 품질이 떨어지지 않도록 3.5 보다 새로운 세대만 둔다(2.5 처럼 이전 세대는 넣지 않는다).
FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.7-flash,gemini-3.8-flash").split(",")
    if m.strip() and m.strip() != MODEL
]

# 로그에 대략적인 비용을 찍기 위한 단가 (2026-09 기준, 100만 토큰당 USD)
PRICES_PER_MTOK = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
INPUT_PRICE_PER_MTOK, OUTPUT_PRICE_PER_MTOK = PRICES_PER_MTOK.get(MODEL, (1.50, 9.00))

def _model_url(model):
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"  # 키는 헤더로 보낸다(_post)


API_URL = _model_url(MODEL)
MAX_TRANSCRIPT_CHARS = 30000

SYSTEM_PROMPT = """[역할 정의]
당신은 수석 월가 투자 전략가(Chief Investment Strategist)이자 경제 분석가입니다.
제공된 영상의 트랜스크립트/내용을 바탕으로, 노이즈는 제거하고 투자 판단에 필요한 핵심 정수만 추출하여 정밀 리포트를 작성해 주세요.

[분석 요구사항]
0. key_summary (한줄 미리보기)
   - 이 영상의 핵심을 1~2문장으로 압축한 미리보기 문장. 카드 목록에서 리포트 본문을 펼치기 전에 가장 먼저 보이는 문장이므로,
     가장 중요한 종목명/수치/결론을 반드시 포함해 임팩트 있게 작성할 것 (예: "**삼성전자** 파운드리 가격 주도권 강화로 4분기
     실적 서프라이즈 기대, **분할 매수** 유효").

1. 🌐 거시경제(Macro) 및 시장 진단
   - 현재 시장의 핵심 인과관계(원인 ➔ 결과) 분석 (예: 금리, 환율, 유가, 통화정책 등)
   - 시장 참여자들이 오해하거나 선반영한 악재/호재 요소 명시
   - 현재 장세의 성격 정리 (예: 주도주 부재 박스권, 순환매 장세, 강세장 등)

2. 📊 섹터 및 종목별 상세 분석
   - [우수/주도 섹터]: 전문가가 강하게 추천하거나 수급이 쏠리는 섹터, 이유, 관련 핵심 종목
   - [관망/주의 섹터]: 조정 가능성이 있거나 리스크가 존재하는 섹터 및 종목
   - 각 종목/섹터별 핵심 모멘텀(실적 성장률, AI 수혜, 정책 수혜 등) 명확히 명시
   - 이 항목들을 leading_picks / watch_picks 배열에도 각각 {sector, tickers} 형태로 구조화해서 중복으로 채울 것

3. 💡 핵심 투자 인사이트 (Key Takeaways)
   - 전문가가 제시하는 시장을 바라보는 뷰(View)의 핵심 3가지
   - 일시적 이슈와 구조적 성장 스토리를 구분하여 설명

4. 🛡️ 투자 전략 및 액션 플랜 (Action Plan)
   - 매수/매도/리스크 관리 관점에서의 구체적인 실행 가이드 (예: 분할 매수 시점, 추격 매수 금지, 투자 경고 관리 등)
   - 다가올 주요 이벤트나 변수(실적 발표, 추석/연말 수급, 정책 변화 등) 및 대응책

[출력 형식 및 가독성]
- Visual Hierarchy(계층 구조)를 적극 활용하여 작성할 것.
- 핵심 키워드, 종목명, 핵심 수치는 **굵은 글씨**로 강조.
- 불필요한 서론/결론 문구는 제외하고 곧바로 리포트 형식으로 작성.

[출력 문법 규칙 - 반드시 준수]
- key_summary 필드는 report_markdown과 별개의 짧은 문자열로, 1~2문장을 넘지 않을 것.
- report_markdown 필드에는 1~4번 리포트만 마크다운으로 작성할 것 (key_summary는 포함하지 말 것).
- 각 대분류(1~4) 제목은 줄 맨 앞에 "## " 를 붙여 마크다운 헤더로 작성 (예: "## 🌐 거시경제(Macro) 및 시장 진단").
- 하위 항목은 "- " 로 시작하는 목록으로 작성.
- 강조할 단어/문장은 반드시 **이렇게** 두 개의 별표로 감쌀 것.
- tickers 필드에는 리포트에서 실제 언급된 종목명만 배열로 별도 추출 (예: ["삼성전자", "엔비디아"]).
- keywords 필드에는 섹터/이슈/매크로 키워드를 배열로 별도 추출 (예: ["금리인상", "HBM", "반도체 사이클"]).
- leading_picks 필드에는 [우수/주도 섹터]에 해당하는 항목들을 {"sector": "섹터명", "tickers": ["종목명", ...]} 형태의 배열로 작성.
- watch_picks 필드에는 [관망/주의 섹터]에 해당하는 항목들을 같은 형태로 작성.
- 둘 다 명확한 항목이 없으면 빈 배열([])로 둘 것 (억지로 만들어내지 말 것).
"""

PICK_ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "sector": {"type": "STRING"},
        "tickers": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["sector", "tickers"],
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "key_summary": {"type": "STRING"},
        "report_markdown": {"type": "STRING"},
        "tickers": {"type": "ARRAY", "items": {"type": "STRING"}},
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "leading_picks": {"type": "ARRAY", "items": PICK_ITEM_SCHEMA},
        "watch_picks": {"type": "ARRAY", "items": PICK_ITEM_SCHEMA},
    },
    "required": ["key_summary", "report_markdown", "tickers", "keywords", "leading_picks", "watch_picks"],
}


def _log_usage(usage, title):
    """영상 한 건당 토큰이 어디서 얼마나 나가는지 로그로 남긴다.
    생각(thinking) 토큰도 출력 요금으로 청구되므로 따로 찍어둬야 비용 원인을 알 수 있다."""
    if not usage:
        return
    prompt = usage.get("promptTokenCount", 0)
    output = usage.get("candidatesTokenCount", 0)
    thoughts = usage.get("thoughtsTokenCount", 0)
    cost = prompt / 1_000_000 * INPUT_PRICE_PER_MTOK + (output + thoughts) / 1_000_000 * OUTPUT_PRICE_PER_MTOK
    print(
        f"[COST] {title[:40]} | 입력 {prompt:,} / 출력 {output:,} / 생각 {thoughts:,} 토큰"
        f" -> 약 ${cost:.4f}"
    )


def _normalize_newlines(result):
    """Gemini가 줄바꿈을 진짜 개행이 아니라 '\\n' 두 글자로 내보내는 경우가 있다.
    그대로 두면 대시보드에서 마크다운이 한 줄로 뭉개져 제목/불릿 구분이 전부 사라진다."""
    report = result.get("report_markdown")
    if isinstance(report, str) and "\\n" in report:
        result["report_markdown"] = report.replace("\\n", "\n")
    return result


# 키 순서: 무료 키(GEMINI_API_KEY_FREE, 결제 없는 프로젝트) 먼저, 유료 키(GEMINI_API_KEY, 선불)는 무료가 전부 막혔을 때만.
# 결제가 연결된 프로젝트는 무료 사용량을 못 쓰므로 키가 두 개다. 선불 잔액이 0 이면 유료 키는 402 로 바로 거절되고
# (돈 안 나감), 나중에 선불을 충전하면 무료가 막힌 영상만 유료로 처리된다.
# 2026-10-06 이전엔 유료 먼저였는데, 선불이 0 이 된 뒤로는 매 실행 첫 요청이 402 로 버려졌다.
_paid_depleted_this_run = False
# 이번 실행 동안 다시 부르지 않을 (키 종류, 모델). 혼잡(503)·시간초과·분당 한도(429)·없는 모델(404)
_busy = set()
# 하루 한도가 찬 무료 모델과 풀리는 시각. 실행이 바뀌어도 기억해야 하므로 파일로 남긴다(GitHub Actions 는 매번 새로 시작).
QUOTA_FILE = Path(__file__).resolve().parent.parent / "docs" / "data" / "gemini_quota.json"


def _load_quota():
    try:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_quota = _load_quota()


def _save_quota():
    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUOTA_FILE.write_text(json.dumps(_quota, ensure_ascii=False, indent=2), encoding="utf-8")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _next_quota_reset(now):
    """무료 하루 한도는 미국 태평양 시간 자정에 초기화된다(공식 문서) = 한국시간 16시(서머타임) / 17시."""
    try:
        from zoneinfo import ZoneInfo

        pt = now.astimezone(ZoneInfo("America/Los_Angeles"))
        reset = (pt + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        reset = reset.astimezone(datetime.timezone.utc)
    except Exception:
        # 시간대 정보가 없으면 늦은 쪽(겨울, UTC 08시)으로 잡는다. 일찍 풀면 거절만 또 쌓인다.
        reset = (now + datetime.timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        if reset - now > datetime.timedelta(days=1):
            reset -= datetime.timedelta(days=1)
    return reset + datetime.timedelta(minutes=2)


def _quota_exhausted(model):
    until = (_quota.get("exhausted") or {}).get(model)
    if not until:
        return False
    try:
        return _now() < datetime.datetime.fromisoformat(until)
    except ValueError:
        return False


def _quota_details(resp):
    """429 오류 상세에서 어떤 한도에 걸렸는지(quotaId)와 그 한도 값(quotaValue)만 뽑는다.
    오류 문구(message)만으론 '하루 요청 수'인지 '토큰 양'인지, 한도가 몇인지 알 수 없다(10/6)."""
    try:
        details = resp.json().get("error", {}).get("details", []) or []
    except Exception:
        return []
    out = []
    for d in details:
        for v in d.get("violations", []) or []:
            item = {k: v.get(k) for k in ("quotaId", "quotaValue", "quotaMetric") if v.get(k)}
            if item:
                out.append(item)
    return out


def _consumer(resp):
    """오류 상세에 적힌 프로젝트 번호(projects/NNN). 두 키가 같은 프로젝트인지 비교하는 데만 쓴다."""
    try:
        details = resp.json().get("error", {}).get("details", []) or []
    except Exception:
        return None
    for d in details:
        consumer = (d.get("metadata") or {}).get("consumer")
        if consumer:
            return consumer
    return None


def diagnose_keys():
    """키 두 개 × 모델 네 개에 아주 짧은 요청을 한 번씩 보내 상태를 기록한다(요청 8번, 비용 없음).
    - 거절되면 어떤 한도(quotaId)에 몇(quotaValue)으로 걸렸는지
    - 두 키가 같은 프로젝트인지(같으면 무료 한도를 나눠 쓴다)
    키 값과 프로젝트 번호 전체는 남기지 않는다(공개 저장소)."""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "Reply with the single word: OK"}]}],
        "generationConfig": {"maxOutputTokens": 16},
    }
    results, consumers = [], {}
    for kind, key in _keys():
        for model in [MODEL] + FALLBACK_MODELS:
            entry = {"key": kind, "model": model}
            try:
                resp = _post(payload, key, timeout=60, model=model)
            except Exception as e:
                entry["status"] = type(e).__name__
                results.append(entry)
                continue
            entry["status"] = resp.status_code
            if not resp.ok:
                try:
                    err = resp.json().get("error", {})
                except Exception:
                    err = {}
                entry["error"] = f"{err.get('status', '')} {err.get('message', '')}".strip()[:160]
                entry["quota"] = _quota_details(resp)
                consumer = _consumer(resp)
                if consumer:
                    consumers.setdefault(kind, set()).add(consumer)
                    entry["project"] = "…" + consumer[-4:]
            results.append(entry)
    free_p, paid_p = consumers.get("free", set()), consumers.get("paid", set())
    same = bool(free_p & paid_p) if free_p and paid_p else None
    # 유료 키가 무료 한도(FreeTier)에 걸린다면, 그 키의 프로젝트는 이미 결제가 꺼진 무료 프로젝트다
    paid_free_tier = any(
        "FreeTier" in (q.get("quotaId") or "") for r in results if r["key"] == "paid" for q in r.get("quota", [])
    )
    return {"results": results, "same_project": same, "paid_key_on_free_tier": paid_free_tier}


def _mark_exhausted(model, resp=None):
    reset = _next_quota_reset(_now())
    _quota.setdefault("exhausted", {})[model] = reset.isoformat()
    if resp is not None:
        # 나중에 '왜 한도가 찼는지' 바로 알 수 있게 걸린 한도 이름과 값을 같이 남긴다
        _quota.setdefault("exhausted_detail", {})[model] = _quota_details(resp)
    _save_quota()
    kst = reset + datetime.timedelta(hours=9)
    print(f"[WARN] {model} 무료 하루 한도 소진 — 한국시간 {kst:%m/%d %H:%M} 초기화까지 이 모델은 요청하지 않는다")


def _is_daily_quota(resp):
    """429 가 '하루 한도'인지('분당 한도'와 구분). 구글은 오류 상세의 quotaId 에 PerDay / PerMinute 를 적어 보낸다."""
    return resp is not None and "PerDay" in (resp.text or "")


def _keys():
    free = (os.environ.get("GEMINI_API_KEY_FREE") or "").strip()
    paid = (os.environ.get("GEMINI_API_KEY") or "").strip()
    keys = []
    if free:
        keys.append(("free", free))
    if paid and paid != free:
        keys.append(("paid", paid))
    return keys


def _models():
    """마지막으로 성공한 모델을 먼저 부른다. 혼잡한 모델부터 두드려 요청(=한도)을 버리지 않게."""
    order = [MODEL] + FALLBACK_MODELS
    last = _quota.get("last_ok_model")
    if last in order:
        order.remove(last)
        order.insert(0, last)
    return order


# 유료 키가 잔액 없음으로 거절되면 이 시간만큼은 다시 두드리지 않는다(실행마다 헛요청·실패 기록이 쌓이지 않게).
# 선불을 충전하면 길어야 이 시간 뒤에 저절로 다시 쓰기 시작한다.
PAID_RECHECK_HOURS = 6
# 유료 키가 '무료 등급 한도(FreeTier)'로 거절되면 그 키의 프로젝트는 결제가 꺼진 무료 프로젝트다.
# 10/6 진단: 무료 키와 같은 하루 한도(모델당 20회)를 나눠 쓰고 있어서, 유료 키로 다시 시도할수록 무료 한도만 깎였다.
# 이 경우는 일주일 동안 아예 쓰지 않는다(결제를 다시 켜면 그 뒤에 저절로 다시 쓴다).
PAID_FREE_TIER_RECHECK_DAYS = 7


def _mark_paid_unavailable(resp=None):
    global _paid_depleted_this_run
    _paid_depleted_this_run = True
    free_tier = resp is not None and "FreeTier" in (resp.text or "")
    rest = datetime.timedelta(days=PAID_FREE_TIER_RECHECK_DAYS) if free_tier else datetime.timedelta(hours=PAID_RECHECK_HOURS)
    _quota["paid_unavailable_until"] = (_now() + rest).isoformat()
    _save_quota()
    why = "결제가 꺼져 무료 한도를 같이 쓴다" if free_tier else "선불 잔액이 없다"
    print(f"[INFO] 유료 키는 {why} — {rest} 동안 유료 키를 쓰지 않는다")


def _count(model, status):
    """무료 키로 보낸 요청을 태평양 시간 날짜(=한도 날짜)별·모델별·결과별로 센다.
    구글 화면의 사용량과 맞춰 보면 거절(503)된 요청도 한도에서 빠지는지 확인할 수 있다."""
    try:
        from zoneinfo import ZoneInfo

        day = _now().astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        day = (_now() - datetime.timedelta(hours=8)).date().isoformat()
    sent = _quota.setdefault("sent", {})
    for old in sorted(sent)[:-6]:
        sent.pop(old, None)  # 최근 7일치만 둔다
    bucket = sent.setdefault(day, {}).setdefault(model, {})
    key = str(status)
    bucket[key] = bucket.get(key, 0) + 1
    _save_quota()


def _paid_unavailable():
    if _paid_depleted_this_run:
        return True
    until = _quota.get("paid_unavailable_until")
    try:
        return bool(until) and _now() < datetime.datetime.fromisoformat(until)
    except ValueError:
        return False


def _usable(kind, model):
    if (kind, model) in _busy:
        return False
    if kind == "free":
        return not _quota_exhausted(model)
    return not _paid_unavailable()


# 한국시간 밤 21시~새벽 5시(미국 낮, 구글 피크)에는 제미나이를 아예 부르지 않는다.
# 2026-10-05 밤 무료 사용량이 이 시간대 내내 혼잡(503)으로 거절됐다. 이 시간에 올라온 영상은 5시 이후에 요약된다.
QUIET_START_HOUR_KST = 21
QUIET_END_HOUR_KST = 5


def gemini_quiet_now():
    hour = (_now() + datetime.timedelta(hours=9)).hour
    return hour >= QUIET_START_HOUR_KST or hour < QUIET_END_HOUR_KST


def gemini_ready():
    """지금 요약을 맡길 수 있는 키·모델이 하나라도 남았는가. 다 막혔으면 파이프라인은 요약을 다음 실행으로 미룬다
    (요청을 보내 봐야 거절만 쌓이고, 그 거절이 무료 한도를 깎는다). 밤 휴식 시간에도 False."""
    if gemini_quiet_now():
        return False
    return any(_usable(kind, model) for kind, _ in _keys() for model in _models())


class GeminiUnavailable(RuntimeError):
    """쓸 수 있는 키·모델이 모두 막혔다(혼잡·한도·잔액 없음). 영상 문제가 아니라 다음 실행에서 다시 하면 된다."""


def _paid_depleted(resp):
    """선불 잔액 소진 응답인가. 문서상 402, 실제로는 429 + 'prepayment credits are depleted' 로도 온다."""
    if resp is None:
        return False
    if resp.status_code == 402:
        return True
    return resp.status_code == 429 and "prepayment" in (resp.text or "").lower()


def _post(payload, key, timeout=90, model=None):
    """키는 주소(?key=)가 아니라 헤더로 보낸다. 주소에 넣으면 실패했을 때 오류 문구에 키가 그대로 찍히고,
    그 문구가 실패 기록(docs/data/pipeline_log.json)에 남아 공개 저장소에 올라갈 수 있다."""
    return requests.post(_model_url(model or MODEL), headers={"x-goog-api-key": key}, json=payload, timeout=timeout)


def check_free_key():
    """무료 키가 실제로 작동하는지 아주 짧은 요청으로 확인한다(무료 사용량, 비용 없음).
    잔액이 떨어지는 날 처음으로 무료 키를 쓰게 되는데, 그때 키가 틀렸으면 요약이 멈춘다. 미리 알아두기 위한 것.
    반환: {"configured", "ok", "status", "message"} — 키 값 자체는 절대 담지 않는다."""
    key = (os.environ.get("GEMINI_API_KEY_FREE") or "").strip()
    if not key:
        return {"configured": False, "ok": False, "status": None, "message": "GEMINI_API_KEY_FREE 가 등록되지 않았다"}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "Reply with the single word: OK"}]}],
        "generationConfig": {"maxOutputTokens": 32},
    }
    try:
        resp = _post(payload, key, timeout=60)
    except Exception as e:
        return {"configured": True, "ok": False, "status": None, "message": f"요청 실패: {type(e).__name__}"}
    if resp.ok:
        return {"configured": True, "ok": True, "status": resp.status_code, "message": f"{MODEL} 응답 정상"}
    try:
        detail = resp.json().get("error", {}).get("message", "")
    except Exception:
        detail = resp.text
    # 한도(429)·혼잡(5xx)은 키가 '받아들여진' 뒤의 거절이라 키 자체는 정상이다. 잘못된 키는 400/403 으로 온다.
    if resp.status_code == 429 or resp.status_code >= 500:
        return {"configured": True, "ok": True, "status": resp.status_code, "message": f"키 정상(지금은 한도/혼잡): {detail[:150]}"}
    return {"configured": True, "ok": False, "status": resp.status_code, "message": detail[:200]}


def _error_detail(e):
    """구글이 오류와 함께 보내는 설명 문구(error.message). 키는 헤더로 보내므로 여기엔 들어 있지 않다."""
    resp = getattr(e, "response", None)
    if resp is None:
        return str(e)[:150]
    try:
        err = resp.json().get("error", {})
        return f"{err.get('status', '')} {err.get('message', '')}".strip()[:220]
    except Exception:
        return (resp.text or "")[:220]


def call_gemini(system_prompt, user_prompt, response_schema, label, max_output_tokens=8192):
    """구조화된 JSON 응답을 받아온다. 키·모델 선택/비용로그/개행 정규화를 공통으로 처리한다.
    쓸 수 있는 키·모델이 다 막히면 GeminiUnavailable 을 던진다(다음 실행에서 다시 시도)."""
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
            "maxOutputTokens": max_output_tokens,
        },
    }

    if gemini_quiet_now():
        raise GeminiUnavailable("밤 휴식 시간(한국시간 21~05시)이라 제미나이를 부르지 않는다")

    last_error = None
    for kind, key in _keys():
        for model in _models():
            if not _usable(kind, model):
                continue
            resp = None
            try:
                resp = _post(payload, key, model=model)
                if kind != "paid" or resp.ok:
                    _count(model, resp.status_code)
                # 잔액 없는 유료 키는 402 나 429(문구는 그때그때 다르다)로 온다. 어느 쪽이든 유료는 당분간 쉰다.
                if kind == "paid" and (_paid_depleted(resp) or resp.status_code == 429):
                    _mark_paid_unavailable(resp)
                    last_error = last_error or RuntimeError("유료 키 선불 잔액 없음/한도")
                    break
                resp.raise_for_status()
                data = resp.json()
                _log_usage(data.get("usageMetadata"), label)
                raw = data["candidates"][0]["content"]["parts"][0]["text"]
                result = _normalize_newlines(json.loads(raw))
            except (
                requests.exceptions.HTTPError,
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                json.JSONDecodeError,
            ) as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if resp is None and kind == "free":
                    _count(model, type(e).__name__)  # 시간초과·연결 끊김도 구글 쪽에선 요청으로 셀 수 있다
                # 실패 기록에 '왜' 거절됐는지 남긴다. 상태 코드만으론 원인을 못 가린다(10/5 503 반복 때 그랬다).
                last_error = RuntimeError(f"{model} {status or type(e).__name__}: {_error_detail(e)}")
                if isinstance(e, json.JSONDecodeError):
                    continue  # 답이 중간에 잘린 경우. 모델 문제는 아니니 막지 않고, 이 영상만 다음 모델로
                if status == 429 and kind == "free" and _is_daily_quota(resp):
                    _mark_exhausted(model, resp)
                    continue
                if status == 404 or status is None or status in RETRYABLE_STATUS_CODES:
                    # 혼잡·시간초과·분당 한도·없는 모델 — 이번 실행 동안은 이 모델을 다시 부르지 않는다
                    _busy.add((kind, model))
                    print(f"[INFO] {kind} {model} 거절({status or type(e).__name__}) — 이번 실행에선 건너뛴다")
                    continue
                raise last_error  # 요청 자체가 잘못된 경우(400 등)는 다른 모델로도 안 된다

            if _quota.get("last_ok_model") != model:
                _quota["last_ok_model"] = model
                _save_quota()
            if kind == "paid":
                print(f"[INFO] 무료가 막혀 유료 키({model})로 요약: {label[:40]}")
            elif model != MODEL:
                print(f"[INFO] 대체 모델 {model} 로 요약 완료: {label[:40]}")
            return result

    raise GeminiUnavailable(f"쓸 수 있는 제미나이 모델이 없다(혼잡/한도). 마지막 오류: {last_error}")


def summarize_transcript(channel_name, title, transcript_text):
    user_prompt = f"채널명: {channel_name}\n영상 제목: {title}\n\n자막:\n{transcript_text[:MAX_TRANSCRIPT_CHARS]}"
    return call_gemini(SYSTEM_PROMPT, user_prompt, RESPONSE_SCHEMA, title)
