"""Google Gemini 무료 API로 자막을 분석해 투자 전략 리포트를 생성한다.
카드 등록 없이 https://aistudio.google.com/apikey 에서 키를 받아 GEMINI_API_KEY로 등록하면 된다.
"""
import datetime
import json
import os
import time
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
※ 먼저 investment_related 를 판단할 것. 주식·채권·환율·원자재·코인·경제 지표 등 투자 판단에 쓸 내용이 없는 영상
  (역사·교양·예능·홍보·이벤트 등)이면 investment_related=false 로 두고, 나머지 필드는 모두 빈 문자열/빈 배열로 둘 것.
  억지로 투자 리포트처럼 꾸미지 말 것(10/9 삼국지 강의를 주식처럼 분석해 종목 칸에 '유비, 조조'가 들어간 사례).

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

[정확성 규칙 - 반드시 준수]
- '오늘/내일/다음 주/장 마감 전' 같은 시점 표현과 '10%씩 3번 분할', '현금 10~30%' 같은 실행 수치는 줄이거나 빼지 말고 그대로 살릴 것.
- 자막에 없는 해석·원인·연도·섹터 전망을 덧붙이지 말 것. 연도는 자막에 나온 그대로만 쓰고, '내년'을 특정 연도로 바꾸지 말 것.
- 자막에서 지나가듯 한 번 언급된 종목·섹터를 추천/관망 종목으로 올리지 말 것.
- leading_picks / watch_picks 는 본문과 key_summary 의 결론과 어긋나지 않게 채울 것(본문에서 매수라고 한 종목을 관망에 넣지 말 것).
- tickers 에는 report_markdown 본문에 실제로 쓴 종목만 넣을 것. 본문에 없는 종목을 자막에 나왔다는 이유로 추가하지 말 것.
- 수치는 자막에서 그 수치가 붙어 있던 대상에만 쓸 것(예: '인텔 -5.3%'를 '국채 금리 5.3%'로 옮기지 말 것).
- 자막은 음성 인식이라 철자가 틀린 곳이 많다. 종목명·인명·용어는 맞는 이름으로 고쳐 쓸 것(예: 네우스→네비우스, 체포 B2C→챗봇 B2C).

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
        # 제미나이는 속성을 이름순으로 출력하므로 이 필드가 맨 먼저 나온다(투자 관련 여부부터 정함).
        # false 면 파이프라인이 저장하지 않는다.
        "investment_related": {"type": "BOOLEAN"},
        "key_summary": {"type": "STRING"},
        "report_markdown": {"type": "STRING"},
        "tickers": {"type": "ARRAY", "items": {"type": "STRING"}},
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "leading_picks": {"type": "ARRAY", "items": PICK_ITEM_SCHEMA},
        "watch_picks": {"type": "ARRAY", "items": PICK_ITEM_SCHEMA},
    },
    "required": ["investment_related", "key_summary", "report_markdown", "tickers", "keywords", "leading_picks", "watch_picks"],
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


# 키 순서: 무료 키(GEMINI_API_KEY_FREE, 결제 없는 프로젝트) 먼저, 유료 키(GEMINI_API_KEY, 결제 켠 별도 프로젝트)는
# 무료 모델 4개의 하루 한도가 모두 찼을 때만(혼잡으로 막혔을 땐 쓰지 않는다, _usable 참고).
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


def quota_day_start(now=None):
    """지금 한도 날짜(태평양 시간 자정 = 한국시간 16시/17시)가 시작된 시각(UTC)."""
    now = now or _now()
    return _next_quota_reset(now) - datetime.timedelta(minutes=2) - datetime.timedelta(days=1)


def _quota_day():
    """한도 날짜(태평양 시간 기준 날짜). 한국시간으로는 16시(겨울 17시)에 날짜가 바뀐다."""
    return (quota_day_start() + datetime.timedelta(hours=12)).date().isoformat()


# 모델당 무료 하루 요청 수(10/6 진단에서 구글이 보낸 quotaValue). 바뀌면 GEMINI_FREE_RPD 로 덮어쓴다.
FREE_RPD = int(os.environ.get("GEMINI_FREE_RPD", "20"))


def remaining_free_requests():
    """이번 한도 날짜에 무료 키로 더 보낼 수 있는 요청 수(모델 4개 합계, 우리가 센 요청 기준)."""
    sent = (_quota.get("sent") or {}).get(_quota_day(), {})
    total = 0
    for model in [MODEL] + FALLBACK_MODELS:
        if _quota_exhausted(model):
            continue
        used = sum(sent.get(model, {}).values())
        total += max(0, FREE_RPD - used)
    return total


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


def diagnose_keys(only_kind=None):
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
        if only_kind and kind != only_kind:
            continue
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
    """덜 막히는 모델부터 부른다. 혼잡한 모델부터 두드려 요청(=한도)을 버리지 않게.
    - 최근 7일 성공률(우리가 센 요청 기준)이 높은 모델 먼저
    - 같으면 오늘 남은 한도가 많은 모델 먼저(한 모델에 몰려 먼저 바닥나지 않게. 10/6 3.5 가 그랬다)
    - 단, 오늘 성공이 없고 이 규칙으로 아직 한 번도 안 불러본 모델은 맨 앞에 한 번 세운다.
      성공률 순서만 쓰면 꼴찌 모델은 앞 모델들이 혼잡으로 막히는 순간 '쉬기'로 끝나 영영 차례가 안 온다
      (10/8 3.6-flash: 한도 18회가 남았는데 21시간 동안 한 번도 안 불렸다)."""
    order = [MODEL] + FALLBACK_MODELS
    sent = _quota.get("sent") or {}
    day = _quota_day()
    today = sent.get(day, {})
    probed = set((_quota.get("probed") or {}).get(day, []))

    def needs_probe(model):
        return model not in probed and not (today.get(model) or {}).get("200") and not _quota_exhausted(model)

    def score(model):
        ok = fail = 0
        for day in sent.values():
            for status, n in (day.get(model) or {}).items():
                if status == "200":
                    ok += n
                else:
                    fail += n
        rate = (ok + 1) / (ok + fail + 2)  # 기록이 없으면 0.5
        remaining = FREE_RPD - sum((today.get(model) or {}).values())
        return (-round(rate, 2), -remaining)

    return sorted(order, key=lambda m: (not needs_probe(m), score(m), order.index(m)))


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
    day = _quota_day()
    sent = _quota.setdefault("sent", {})
    for old in sorted(sent)[:-6]:
        sent.pop(old, None)  # 최근 7일치만 둔다
    bucket = sent.setdefault(day, {}).setdefault(model, {})
    key = str(status)
    bucket[key] = bucket.get(key, 0) + 1
    probed = _quota.get("probed") or {}
    _quota["probed"] = {day: sorted(set(probed.get(day, [])) | {model})}  # 오늘치만 둔다(_models 참고)
    _save_quota()


def _paid_fingerprint():
    """유료 키가 바뀌었는지만 알아보는 지문(해시 앞 8자리). 키 자체는 남기지 않고, 이것으로 키를 되살릴 수도 없다."""
    import hashlib

    paid = (os.environ.get("GEMINI_API_KEY") or "").strip()
    return hashlib.sha256(paid.encode()).hexdigest()[:8] if paid else None


def _paid_unavailable():
    if _paid_depleted_this_run:
        return True
    # 유료 키를 새로 넣었으면(예: 결제를 켠 새 프로젝트의 키) 예전 키 때문에 걸어둔 쉬는 기간은 무시한다
    fp = _paid_fingerprint()
    if _quota.get("paid_key_fp") != fp:
        _quota["paid_key_fp"] = fp
        _quota.pop("paid_unavailable_until", None)
        _save_quota()
        return False
    until = _quota.get("paid_unavailable_until")
    try:
        return bool(until) and _now() < datetime.datetime.fromisoformat(until)
    except ValueError:
        return False


# 혼잡(503)으로 거절된 요청도 무료 하루 한도(모델당 20회)를 깎는다(10/7 아침 3.6-flash: 503 직후 바로 한도 초과).
# 그렇다고 오래 쉬면 남은 한도가 초기화(한국시간 16시/17시) 때 이월되지 않고 사라진다(10/8 오후 34회를 160분씩 쉬며 버릴 뻔했다).
# 그래서 거절 간격을 '초기화까지 남은 시간 ÷ 남은 무료 한도'로 맞춘다(사용자 지정 10/8).
#   - 한도가 시간에 비해 넉넉하면 간격이 좁아져 한 실행에서 여러 번 다시 시도한다(예: 15시, 28회 남음 → 약 2분 간격 → 실행당 약 9번).
#   - 빠듯하면 간격이 넓어져 실행당 1번만 시도하고, 간격이 실행 주기보다 길면 그만큼 다음 실행들을 건너뛴다.
#   - 쉬지 않고 두드리지는 않는다 — 아침 혼잡에 하루치를 다 날리지 않게(사용자 지적: "7~8시에 다 쓸 수도 있잖아").
RUN_INTERVAL_MINUTES = 20  # 워크플로 예약 주기(*/20)
FAIL_PAUSE_SECONDS = 15  # 한 실행 안에서 거절 뒤 다음 시도까지 잠깐 쉰다
_fails_this_run = 0
_halted_this_run = False


def _pace_minutes():
    """거절 한 번당 다음 시도까지의 간격(분) = 초기화까지 남은 시간 ÷ 남은 무료 한도."""
    minutes_left = (_next_quota_reset(_now()) - _now()).total_seconds() / 60
    return minutes_left / max(1, remaining_free_requests())


def _fails_allowed_this_run():
    """이번 실행에서 허용할 거절 횟수. 실행 주기(20분) 동안 간격대로 시도할 수 있는 만큼, 최소 1번."""
    return max(1, int(RUN_INTERVAL_MINUTES // _pace_minutes()))


def _note_failure():
    global _fails_this_run, _halted_this_run
    _fails_this_run += 1
    if _halted_this_run:
        return
    pace = _pace_minutes()
    allowed = _fails_allowed_this_run()
    if _fails_this_run < allowed:
        # 아직 이번 실행 몫이 남았다. 막힌 무료 모델들을 다시 풀어 다음 영상에서 또 불러본다.
        for entry in [b for b in _busy if b[0] == "free"]:
            _busy.discard(entry)
        print(f"[INFO] 제미나이 거절 {_fails_this_run}/{allowed}회(간격 약 {pace:.1f}분) — {FAIL_PAUSE_SECONDS}초 뒤 다시 시도")
        time.sleep(FAIL_PAUSE_SECONDS)
        return
    _halted_this_run = True
    # 간격이 실행 주기보다 길면 그만큼 쉰다. 짧으면 다음 실행에서 바로 다시 시도한다.
    wait = pace if pace > RUN_INTERVAL_MINUTES else 0
    if wait:
        _quota["backoff_until"] = (_now() + datetime.timedelta(minutes=wait)).isoformat()
    else:
        _quota.pop("backoff_until", None)
    _quota.pop("backoff_minutes", None)
    _save_quota()
    print(f"[INFO] 제미나이 거절 {_fails_this_run}회 — 이번 실행은 멈춤, {wait:.0f}분 뒤부터 다시 시도(간격 약 {pace:.1f}분)")


def _note_success():
    if "backoff_minutes" in _quota or "backoff_until" in _quota:
        _quota.pop("backoff_minutes", None)
        _quota.pop("backoff_until", None)
        _save_quota()


def _backing_off():
    if _halted_this_run:
        return True
    until = _quota.get("backoff_until")
    try:
        return bool(until) and _now() < datetime.datetime.fromisoformat(until)
    except ValueError:
        return False


def _usable(kind, model, allow_paid=True, reserve=0, paid_only=False):
    """reserve: 이 요청은 무료 요청을 이만큼 남겨두고 써야 한다.
    allow_paid=False: 무료로만 처리한다(묶음 요약 시험).
    paid_only=True: 유료로만, 시간대 규칙과 상관없이 바로 처리한다(당잠사, 사용자 지정 10/8)."""
    if (kind, model) in _busy:
        return False
    if paid_only:
        return kind == "paid" and not _paid_unavailable()
    if kind == "free":
        # 혼잡 쉬기(backoff)는 무료 한도를 아끼려는 것이라 무료 키에만 건다. 유료 키는 쉬지 않고 바로 이어받는다.
        if gemini_mode() != "free" or _backing_off() or _quota_exhausted(model):
            return False
        return not reserve or remaining_free_requests() > reserve
    if not allow_paid or _paid_unavailable():
        return False
    if gemini_mode() == "paid":
        return True  # 저녁 유료 시간대: 무료는 쓰지 않고 유료로 바로 처리
    # 낮(무료 시간대)엔 유료를 쓰지 않는다. 무료가 혼잡이든 하루 한도 소진이든 18시 유료 시간대까지 기다린다
    # (사용자 지정 10/8: 15:57 무료 4개 한도가 모두 차자 11편이 유료로 넘어갔다 → "07~18시엔 유료 쓰지 말고 기다리기").
    # 당잠사(paid_only)만 예외로 위에서 바로 유료로 처리한다. 무료 키가 아예 없으면 유료로 처리한다.
    return not (os.environ.get("GEMINI_API_KEY_FREE") or "").strip()


# 시간대별 운영(사용자 지정, 2026-10-07):
# - 07:00~18:00 무료: 무료로만. 무료 4개 한도가 모두 차도 유료로 넘기지 않고 18시까지 기다린다(10/8 변경).
#   당잠사만 예외로 올라오자마자 유료로 처리한다(10/8).
# - 18:00~21:00 유료: 이 시간대부터 무료는 혼잡으로 거절될 확률이 높아 유료로만 처리한다.
# - 21:00~07:00 휴식: 구글이 가장 바쁜 시간. 요약은 하지 않고 자막만 받아둔다(파이프라인 쪽).
#   07시가 되면 밀린 영상을 받아둔 자막으로 바로 무료 요약한다.
FREE_WINDOW_KST = ((7, 0), (18, 0))
PAID_WINDOW_KST = ((18, 0), (21, 0))
ACTIVE_START_KST = FREE_WINDOW_KST[0]
QUIET_LABEL = "한국시간 21:00~07:00"


# 이 시각(한국시간)까지는 운영 시간이어도 제미나이를 부르지 않는다. 일회성 정지용.
# 10/7 저녁 무료가 계속 혼잡해 사용자가 "오늘은 그만, 내일 07시부터 다시"로 정했다.
PAUSE_UNTIL_KST = datetime.datetime(2026, 10, 8, 7, 0)


def gemini_mode():
    """지금 시간대: 'free'(무료 시간), 'paid'(저녁 유료 시간), 'off'(휴식 또는 일회성 정지)."""
    kst = _now() + datetime.timedelta(hours=9)
    if kst.replace(tzinfo=None) < PAUSE_UNTIL_KST:
        return "off"
    hm = (kst.hour, kst.minute)
    if FREE_WINDOW_KST[0] <= hm < FREE_WINDOW_KST[1]:
        return "free"
    if PAID_WINDOW_KST[0] <= hm < PAID_WINDOW_KST[1]:
        return "paid"
    return "off"


def gemini_quiet_now():
    return gemini_mode() == "off"


def gemini_ready(reserve=0, allow_paid=True, paid_only=False):
    """지금 요약을 맡길 수 있는 키·모델이 하나라도 남았는가. 다 막혔으면 파이프라인은 요약을 다음 실행으로 미룬다
    (요청을 보내 봐야 거절만 쌓이고, 그 거절이 무료 한도를 깎는다). 밤 휴식 시간에도 False(paid_only 는 예외).
    reserve / allow_paid / paid_only 는 _usable 참고."""
    if gemini_quiet_now() and not paid_only:
        return False
    return any(_usable(kind, model, allow_paid, reserve, paid_only) for kind, _ in _keys() for model in _models())


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


def call_gemini(
    system_prompt, user_prompt, response_schema, label, max_output_tokens=8192, allow_paid=True, reserve=0, paid_only=False
):
    """구조화된 JSON 응답을 받아온다. 키·모델 선택/비용로그/개행 정규화를 공통으로 처리한다.
    쓸 수 있는 키·모델이 다 막히면 GeminiUnavailable 을 던진다(다음 실행에서 다시 시도).
    allow_paid / reserve / paid_only 는 _usable 참고."""
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
            "maxOutputTokens": max_output_tokens,
        },
    }

    if gemini_quiet_now() and not paid_only:
        raise GeminiUnavailable(f"휴식 시간({QUIET_LABEL})이라 제미나이를 부르지 않는다")

    last_error = None
    for kind, key in _keys():
        for model in _models():
            if not _usable(kind, model, allow_paid, reserve, paid_only):
                continue
            resp = None
            try:
                resp = _post(payload, key, model=model)
                if kind == "free":
                    _count(model, resp.status_code)  # 유료 키는 별도 프로젝트라 무료 한도 계산에 넣지 않는다
                # 유료 키를 당분간 쉬게 하는 건 '잔액 없음(402/prepayment)'이나 '결제 꺼진 무료 등급(FreeTier)'일 때뿐.
                # 그 밖의 429(분당 요청 제한 등)는 잠깐 막힌 것이라 아래에서 이번 실행만 건너뛴다.
                if kind == "paid" and (_paid_depleted(resp) or (resp.status_code == 429 and "FreeTier" in (resp.text or ""))):
                    _mark_paid_unavailable(resp)
                    last_error = last_error or RuntimeError("유료 키 선불 잔액 없음/무료 등급")
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
                    # 혼잡·시간초과·분당 한도·없는 모델 — 이번 실행 동안은 이 모델을 다시 부르지 않는다.
                    # (무료 거절은 _note_failure 가 이번 실행 몫이 남았으면 다시 풀어준다)
                    _busy.add((kind, model))
                    print(f"[INFO] {kind} {model} 거절({status or type(e).__name__}) — 이번 실행에선 건너뛴다")
                    if status != 404 and kind == "free":
                        _note_failure()
                    continue
                raise last_error  # 요청 자체가 잘못된 경우(400 등)는 다른 모델로도 안 된다

            _note_success()
            if _quota.get("last_ok_model") != model:
                _quota["last_ok_model"] = model
                _save_quota()
            if kind == "paid":
                print(f"[INFO] 무료가 막혀 유료 키({model})로 요약: {label[:40]}")
            elif model != MODEL:
                print(f"[INFO] 대체 모델 {model} 로 요약 완료: {label[:40]}")
            return result

    raise GeminiUnavailable(f"쓸 수 있는 제미나이 모델이 없다(혼잡/한도). 마지막 오류: {last_error}")


def summarize_transcript(channel_name, title, transcript_text, reserve=0, allow_paid=True):
    user_prompt = f"채널명: {channel_name}\n영상 제목: {title}\n\n자막:\n{transcript_text[:MAX_TRANSCRIPT_CHARS]}"
    return call_gemini(SYSTEM_PROMPT, user_prompt, RESPONSE_SCHEMA, title, reserve=reserve, allow_paid=allow_paid)


# 요청 한 번에 영상 여러 개 요약하기(시험 중). 무료 한도는 '요청 횟수'로 세므로 2개씩 묶으면 같은 한도로 두 배를 처리한다.
# 결과는 영상마다 따로 돌려받아 지금처럼 영상별 카드로 저장한다. 품질 비교(pair_trial)를 거친 뒤에만 실제로 쓴다.
PAIR_SYSTEM_ADDENDUM = """

[여러 영상 동시 분석 규칙 - 반드시 준수]
- 이번 요청에는 영상이 여러 개 들어 있다. 영상마다 위 분석 요구사항 전체를 각각 '완전히 독립된 리포트'로 작성할 것.
- 한 영상의 내용·종목·수치를 다른 영상의 리포트에 절대 섞지 말 것. 영상끼리 비교하거나 합쳐서 정리하지 말 것.
- 영상 하나만 받았을 때와 똑같은 깊이와 분량으로 작성할 것(영상이 여러 개라고 줄이지 말 것).
- reports 배열에 영상 순서대로 하나씩 넣고, video_no 에 그 영상 번호(1부터)를 적을 것."""

PAIR_ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {"video_no": {"type": "INTEGER"}, **RESPONSE_SCHEMA["properties"]},
    "required": ["video_no"] + RESPONSE_SCHEMA["required"],
}
PAIR_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"reports": {"type": "ARRAY", "items": PAIR_ITEM_SCHEMA}},
    "required": ["reports"],
}


def summarize_many(items, reserve=0, allow_paid=False):
    """items: [(채널명, 제목, 자막), ...] → 같은 순서의 리포트 목록. 개수가 안 맞으면 실패로 본다.
    지금은 품질 비교 시험에만 쓰므로 기본은 무료로만 처리한다(시험에 돈을 쓰지 않게)."""
    parts = []
    for i, (channel_name, title, transcript_text) in enumerate(items, 1):
        parts.append(
            f"=== 영상 {i} ===\n채널명: {channel_name}\n영상 제목: {title}\n\n자막:\n{transcript_text[:MAX_TRANSCRIPT_CHARS]}"
        )
    user_prompt = f"아래 영상 {len(items)}개를 각각 따로 분석하라.\n\n" + "\n\n".join(parts)
    label = " + ".join(t[:16] for _, t, _ in items)
    result = call_gemini(
        SYSTEM_PROMPT + PAIR_SYSTEM_ADDENDUM,
        user_prompt,
        PAIR_RESPONSE_SCHEMA,
        label,
        max_output_tokens=8192 * len(items),
        allow_paid=allow_paid,
        reserve=reserve,
    )
    reports = sorted(result.get("reports") or [], key=lambda r: r.get("video_no", 0))
    if [r.get("video_no") for r in reports] != list(range(1, len(items) + 1)):
        raise RuntimeError(f"묶음 요약 결과 개수/번호가 맞지 않는다: {[r.get('video_no') for r in reports]}")
    return [_normalize_newlines(r) for r in reports]
