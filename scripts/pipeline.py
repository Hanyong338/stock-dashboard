"""메인 파이프라인: RSS 신규 영상 감지 -> 자막 추출 -> 요약 -> 저장.
GitHub Actions에서 1시간마다 실행된다 (.github/workflows/pipeline.yml 참고).
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time

import requests
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from youtube_check import fetch_channel_videos
from summarize import (
    ACTIVE_START_KST,
    QUIET_LABEL,
    GeminiUnavailable,
    check_free_key,
    diagnose_keys,
    gemini_mode,
    gemini_quiet_now,
    gemini_ready,
    quota_day_start,
    refresh_model_list,
    summarize_many,
    summarize_transcript,
)
from transcript import get_transcript, is_retryable_error
from market_data import BRIEF_INDICES, fetch_session_closes, fetch_session_sectors
import morning_brief as mb
from calendar_data import KST, build_calendar, refresh_values
from screening import build_screening, fetch_theme_groups, theme_leaders
from screening_us import build_us_screening
import tracking as trk
from morning_breakout import build_morning_breakout

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
CHANNELS_FILE = ROOT / "scripts" / "channels.json"
STATE_FILE = DATA_DIR / "state.json"
SUMMARIES_FILE = DATA_DIR / "summaries.json"
CHANNELS_OUT_FILE = DATA_DIR / "channels.json"
MORNING_BRIEF_FILE = DATA_DIR / "morning_brief.json"
CALENDAR_FILE = DATA_DIR / "calendar.json"
KR_CONSENSUS_FILE = DATA_DIR / "kr_consensus.json"
SCREENING_FILE = DATA_DIR / "screening.json"
US_SCREENING_FILE = DATA_DIR / "screening_us.json"
TRACKING_FILE = DATA_DIR / "tracking.json"
GEMINI_KEY_CHECK_FILE = DATA_DIR / "gemini_key_check.json"
# 한 번만 도는 키 진단(어떤 한도에 걸리는지, 두 키가 같은 프로젝트인지). 다시 돌리려면 버전을 올린다.
GEMINI_DIAG_FILE = DATA_DIR / "gemini_diag.json"
GEMINI_DIAG_VERSION = 2
# 2: 10/7 결제를 켠 새 프로젝트의 유료 키를 넣었다. 그 키만 확인한다(무료 한도는 건드리지 않게).
GEMINI_DIAG_ONLY_KIND = "paid"
# 영상 2개를 요청 한 번에 요약했을 때 품질이 괜찮은지 보는 시험(10/7 사용자 요청).
# 평소처럼 하나씩 요약한 영상의 자막을 몇 개 남겨뒀다가, 2개씩 묶어 한 번 더 요약해 나란히 저장한다.
# 대시보드에는 나오지 않고 비교용으로만 쓴다. 자막은 이미 받아둔 것을 쓰므로 자막 크레딧은 안 나간다(제미나이 요청 2번 추가).
PAIR_TRIAL_FILE = DATA_DIR / "pair_trial.json"
# 10/9: 처음 2쌍은 번역 자막(영어·인도네시아어)으로 돌았고 품질 차이가 뚜렷하지 않았다(사용자: 계속 시험).
# 한국어 자막 + 정확성 규칙(prompt_v 2)으로 2쌍을 모았다(한 편씩 3 : 묶음 1).
# 10/9 저녁: 규칙 3개(투자 무관 영상 제외, tickers 는 본문 종목만, 수치·철자 정확히) 추가 후 4쌍 더(prompt_v 3).
PAIR_TRIAL_TARGET_PAIRS = 8
PAIR_TRIAL_PROMPT_V = 3
TRACKING_SEED_FILE = DATA_DIR / "tracking_seed.json"
THEMES_FILE = DATA_DIR / "themes.json"
MORNING_FILE = DATA_DIR / "morning_breakout.json"
# 자막 캐시. 요약이 실패해도 자막을 다시 받지 않기 위해 남겨둔다(성공하면 지운다).
TRANSCRIPT_CACHE_DIR = DATA_DIR / "transcripts"
PIPELINE_LOG_FILE = DATA_DIR / "pipeline_log.json"  # 실패 기록. 워크플로 로그를 볼 수 없어 파일로 남긴다
MAX_LOG_ENTRIES = 300
ATTEMPTS_KEY = "_attempts"  # state.json 안에서 영상별 시도 횟수를 담는 키 (채널 ID 와 겹치지 않는다)

MAX_SUMMARIES = 500
RETENTION_DAYS = 4  # 마켓 라이브에 보여줄 기간. 이보다 오래된 요약은 지우고, 그보다 오래된 영상은 새로 요약하지 않는다
STATE_HISTORY_PER_CHANNEL = 100
REQUEST_INTERVAL_SECONDS = 3  # 자막/AI API를 너무 빨리 연달아 호출해서 429(요청 한도 초과)에 걸리는 것을 막는다.
# 자막 요청의 바깥 제한. transcript.py 가 안에서 최대 120초까지 기다리므로 그보다 넉넉해야 한다.
# 예전에 90초로 잡혀 있어서, 90~120초 걸리는 긴 영상은 매번 강제 종료됐다.
# Supadata 쪽에는 작업이 이미 생성돼 크레딧은 나가는데 결과는 못 받고,
# 타임아웃은 '일시적 오류'라 확인함 처리도 안 되어 매시간 같은 영상을 다시 받는 루프가 됐다.
TRANSCRIPT_TIMEOUT_SECONDS = 180
# summarize.py 의 재시도 + 대체 모델 3개까지 다 써도 안 잘리게 잡는다.
# 최악(매 요청 90초 시간초과): 기본 3회 + 대기 25초 + 대체 2회 x 3모델 + 대기 ≈ 850초.
# 실제로 혼잡(503)은 응답이 바로 와서 몇십 초면 다음 모델로 넘어간다.
SUMMARIZE_TIMEOUT_SECONDS = 900
MAX_VIDEO_DURATION_SECONDS = 3600  # 1시간 넘는 영상은 자막 생성 비용이 커서 아예 요약하지 않는다.
NO_CAPTION_TRIES = 3  # '자막 없음' 응답을 받은 영상을 몇 번까지 다시 받아볼지(한 번에 1크레딧). 일시적 실패 대비
MIN_VIDEO_AGE_MINUTES = 60  # 업로드 후 이만큼 지나야 자막을 요청한다(유튜브 자동 자막이 만들어질 시간)
MIN_VIDEO_DURATION_SECONDS = 181  # 3분 이하는 쇼츠(Shorts)라 요약하지 않는다. 유튜브 쇼츠 최대 길이가 3분.

# 캘린더 생성 규칙(수집 범위·시간대 변환·범주 등)이 바뀌면 이 숫자를 올린다.
# 캘린더는 하루 한 번만 만들기 때문에, 이게 없으면 코드를 고쳐도 그날은 옛 데이터가 그대로 남는다.
CALENDAR_BUILDER_VERSION = 10

# 당잠사 리포트의 프롬프트/출력 형식을 바꾸면 이 숫자를 올린다.
# 같은 방송이면 다시 분석하지 않기 때문에, 이게 없으면 새 방송이 올라올 때까지 옛 형식이 남는다.
# 올릴 때마다 제미나이 호출이 1회 더 발생한다는 점을 알고 올릴 것.
MORNING_BRIEF_PROMPT_VERSION = 5

# 시그널 스크리너의 종목 선별은 전종목을 훑어 2~3분 걸리므로 하루 한 번, 장 마감 후에만 돈다.
# 한국시간 15:50 — 정규장 마감(15:30) 직후라 그날 일봉이 확정된 시점이다.
# 시각을 '분'까지 봐야 한다. 시(hour)만 보면 매시 크론의 15:00 실행이 먼저 걸려
# 장이 끝나기도 전의 미완성 일봉으로 판정해버린다.
# 끝을 18시로 둔 건 예약 실행이 늦게 시작될 때를 위한 여유다(깃허브 크론은 흔히 수십 분 늦는다).
SCREENING_AFTER = (15, 45)
SCREENING_BEFORE_HOUR = 18

# 오늘의 주도 테마는 목록 API 3번이면 끝나서 매시간 갱신해도 부담이 없다.
# 종목 선별과 분리해 따로 저장한다(무거운 screening.json 을 매시간 건드리지 않으려는 것).
THEME_TOP = 8

# 섹션4 모닝 브레이크아웃은 장 시작 30분(09:00~09:30) 분봉으로 판정하므로 09:30 이후에 돈다.
# 깃허브 예약 실행이 늦게 시작돼도 판정은 09:30 시점 기준 그대로다(분봉을 잘라 쓰기 때문).
# 끝을 11시로 둔 건 늦은 시작을 받아주기 위한 여유다.
MORNING_AFTER = (9, 30)
MORNING_BEFORE_HOUR = 11
SCREENING_RULES_VERSION = 8

# 미장 섹션 1~3. 미국 정규장 마감(현지 16:00) = 한국시간 05:00(서머타임) / 06:00(겨울).
# 06:30 부터 잡으면 두 경우 모두 마감 뒤라 매시 크론의 07:00 실행이 받는다.
# 끝을 12시로 둔 건 예약 실행이 늦게 시작될 때를 위한 여유다.
# 요일은 한국 기준 화~토 = 미국 월~금 장이 끝난 다음 날 아침.
US_SCREENING_AFTER = (6, 30)
US_SCREENING_BEFORE_HOUR = 12
US_SCREENING_WEEKDAYS = (1, 2, 3, 4, 5)


def call_with_timeout(fn, timeout, *args, **kwargs):
    """무료 자막 라이브러리 등 내부에 자체 타임아웃이 없는 호출이 영원히 멈춰서
    파이프라인 전체가 몇 시간씩 멈춰버리는 것을 막는다.
    매번 새 executor를 써서, 이번 호출이 멈추더라도 다음 영상 처리까지 같이 멈추지 않게 한다.
    """
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        raise TimeoutError(f"{fn.__name__} 호출이 {timeout}초 안에 끝나지 않았습니다")
    finally:
        executor.shutdown(wait=False)


def parse_published(pub_iso):
    try:
        return datetime.datetime.strptime(pub_iso[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc
        )
    except (ValueError, KeyError, TypeError):
        return None


def within_retention(pub_iso, now):
    """오늘(한국시간)을 포함해 RETENTION_DAYS 일치만 남긴다.

    '몇 시간 전'이 아니라 '며칠 전'으로 세야 한다. 시각으로 재면 같은 날 영상인데도
    올라온 시간에 따라 어떤 건 남고 어떤 건 빠져서, 화면에 날짜가 하나 더 보인다."""
    pub = parse_published(pub_iso)
    if pub is None:
        return True  # 날짜를 못 읽으면 실수로 지우지 않고 남겨둔다
    cutoff = (now.astimezone(KST) - datetime.timedelta(days=RETENTION_DAYS - 1)).date()
    return pub.astimezone(KST).date() >= cutoff


def load_json(path, default, required=False):
    """JSON 하나가 깨졌다고 파이프라인 전체가 죽지 않게 한다.
    실제로 머지가 잘못 풀려 state.json 바깥에 중괄호가 한 겹 더 씌워졌고,
    그 한 파일 때문에 실행이 첫 줄에서 통째로 실패했다.

    깨진 파일은 .broken 으로 옮겨두고(덮어써서 증거를 없애지 않는다) 기본값으로 계속 간다.
    다만 required=True 인 파일은 기본값으로 돌아가면 멀쩡한 데이터를 빈 값으로
    덮어쓰게 되므로 차라리 멈춘다."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[WARN] {path.name} 이 깨져 있습니다: {e}")
        if required:
            raise
        broken = path.with_suffix(path.suffix + ".broken")
        try:
            path.replace(broken)
            print(f"[WARN] {broken.name} 으로 옮기고 기본값으로 계속합니다")
        except Exception as move_err:
            print(f"[WARN] 깨진 파일을 옮기지 못했습니다: {move_err}")
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 실패 기록 ──────────────────────────────────────────────────────────────
# 워크플로 로그는 저장소 관리자만 볼 수 있어서, 무엇이 왜 실패했는지 확인할 방법이 없었다.
# 크레딧이 왜 나갔는지 추측만 하다 틀린 적이 있다. 그래서 실패를 파일로 남긴다.
_warnings = []
# 제미나이가 모두 막혀 이번 실행에서 요약을 미룬 영상. 영상마다 남기면 기록이 넘치니 실행 끝에 한 줄로 남긴다.
_deferred = []
# 자막까지 준비돼 요약을 기다리는 영상. 채널을 다 돈 뒤 summarize_pending 이 2편씩 묶어 처리한다.
_pending = []
# 사후 검증용: 요약이 끝난 자막을 며칠 남겨 Claude 예약 작업이 요약과 대조할 수 있게 한다(10/10).
VERIFY_TRANSCRIPT_DIR = DATA_DIR / "transcripts_review"
VERIFY_KEPT_KEY = "_review_kept"  # state.json: 검수용 자막을 남긴 시각(영상 id → ISO)
VERIFY_KEEP_DAYS = 3
TYPO_FIXES_FILE = DATA_DIR / "typo_fixes.json"  # 음성 인식 오타 사전(검수 작업이 자주 나온 오타를 추가한다)
PAIR_WAIT_KEY = "_pair_wait"  # state.json: 짝을 기다리기 시작한 시각(영상 id → ISO)
PAIR_WAIT_MINUTES = 60  # 혼자 남은 영상이 짝을 기다리는 최대 시간
PAIR_FAIL_KEY = "_pair_fail"  # state.json: 묶음 요청이 (제미나이 혼잡이 아닌 이유로) 실패한 횟수
PAIR_FALLBACK_FAILS = 2  # 묶음이 이만큼 실패한 영상은 하나씩 요약
SKIP_BACKLOG_CHANNELS = {"삼프로TV", "815머니톡", "한국경제TV"}
SKIP_BACKLOG_BEFORE_KST = datetime.date(2026, 10, 8)

# 무료 한도 날짜는 한국시간 16시(겨울 17시)에 바뀐다. 그래서 전날 16~21:30 요약이 한도를 다 쓰면
# 다음 날 아침 당잠사를 분석할 요청이 하나도 안 남는다(10/7 아침 실제로 그랬다).
# 이번 한도 날짜의 당잠사가 아직 안 끝났으면 채널 요약은 이만큼의 요청을 남겨두고 멈춘다.
# 1번이면 되지만 혼잡(503)으로 한두 번 거절될 수 있어 넉넉히 잡는다.
# 10/8부터 당잠사는 유료로 처리하므로 무료를 남겨둘 필요가 없다(0). 다시 무료로 돌리면 4로.
MORNING_BRIEF_RESERVE = 0


def _brief_reserve():
    current = load_json(MORNING_BRIEF_FILE, {})
    pub = parse_published(current.get("published", "")) if isinstance(current, dict) else None
    start = quota_day_start()
    if pub is not None and pub >= start:
        return 0  # 이번 한도 날짜의 당잠사는 이미 분석했다
    if datetime.datetime.now(datetime.timezone.utc) >= start + datetime.timedelta(hours=20):
        return 0  # 한국시간 정오가 지나도록 안 올라왔으면 오늘은 방송이 없는 날로 보고 남겨둔 몫을 푼다
    return MORNING_BRIEF_RESERVE


# 오류 문구에 API 키가 섞여 들어오면 공개 저장소에 그대로 올라간다. 파일에 남기기 전에 가린다.
# (구글 키 AIza…, 주소의 key=… 파라미터)
_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"([?&](?:key|api_key|apikey)=)[^&\s'\"]+", re.I),
]


def _redact(text):
    text = str(text)
    text = _SECRET_PATTERNS[0].sub("***", text)
    return _SECRET_PATTERNS[1].sub(r"\1***", text)


def warn(message):
    """로그에도 찍고 파일에도 남긴다. 파일은 공개 저장소에 올라가 나중에 읽을 수 있다."""
    message = _redact(message)
    print(f"[WARN] {message}")
    _warnings.append({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "message": message[:300]})


def save_warnings(now):
    """이번 실행에서 난 실패를 기록에 덧붙인다. 최근 것만 남기고 오래된 건 버린다."""
    if not _warnings:
        return False
    prev = load_json(PIPELINE_LOG_FILE, {})
    if not isinstance(prev, dict):
        prev = {}
    entries = (prev.get("entries") or []) + _warnings
    save_json(
        PIPELINE_LOG_FILE,
        {"updated_at": now.isoformat(), "entries": entries[-MAX_LOG_ENTRIES:]},
    )
    return True


# ── 자막 캐시 ──────────────────────────────────────────────────────────────
# 자막 1건 = Supadata 크레딧 1개다. 요약이 실패했다고 자막을 다시 받을 이유가 없다.
# 받아둔 자막을 파일로 남겨두고, 재시도 때는 그걸 그대로 쓴다.
# 요약에 성공하면 지운다. 그래서 저장소에는 '아직 요약 못 한 것'만 잠깐 남는다.
def _transcript_cache_path(video_id):
    return TRANSCRIPT_CACHE_DIR / f"{video_id}.txt"


def read_transcript_cache(video_id):
    path = _transcript_cache_path(video_id)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        return text or None
    except Exception as e:
        print(f"[WARN] 자막 캐시 읽기 실패 {video_id}: {e}")
        return None


def write_transcript_cache(video_id, text):
    if not text:
        return
    try:
        TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _transcript_cache_path(video_id).write_text(text, encoding="utf-8")
    except Exception as e:
        print(f"[WARN] 자막 캐시 저장 실패 {video_id}: {e}")


def keep_transcript_for_review(video_id, state, now):
    """요약이 끝난 자막을 검수용 폴더로 옮겨 VERIFY_KEEP_DAYS 동안 둔다(사용자 지정 10/10: 사후 검증).
    검수(Claude 예약 작업)가 요약을 자막과 대조해 숫자·인물·오타를 고친다. 기간이 지나면 지운다."""
    src = _transcript_cache_path(video_id)
    try:
        if src.exists():
            VERIFY_TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
            src.replace(VERIFY_TRANSCRIPT_DIR / f"{video_id}.txt")
            state.setdefault(VERIFY_KEPT_KEY, {})[video_id] = now.isoformat()
    except Exception as e:
        print(f"[WARN] 검수용 자막 보관 실패 {video_id}: {e}")


def prune_review_transcripts(state, now):
    """검수용 자막 중 보관 기간이 지난 것을 지운다. 기록이 없는 파일도(예전 것) 지운다."""
    kept = state.setdefault(VERIFY_KEPT_KEY, {})
    for vid, at in list(kept.items()):
        if (now - datetime.datetime.fromisoformat(at)).days >= VERIFY_KEEP_DAYS:
            kept.pop(vid, None)
    if VERIFY_TRANSCRIPT_DIR.exists():
        for f in VERIFY_TRANSCRIPT_DIR.glob("*.txt"):
            if f.stem not in kept:
                f.unlink(missing_ok=True)


def clear_transcript_cache(video_id):
    try:
        _transcript_cache_path(video_id).unlink(missing_ok=True)
    except Exception:
        pass


def fetch_transcript_cached(video_id, url, title=""):
    """캐시에 있으면 그걸 쓰고(크레딧 0), 없을 때만 받아온다."""
    cached = read_transcript_cache(video_id)
    if cached:
        print(f"[INFO] 자막 캐시 사용 (크레딧 0): {title[:40]}")
        return cached
    text = call_with_timeout(get_transcript, TRANSCRIPT_TIMEOUT_SECONDS, url)
    write_transcript_cache(video_id, text)
    return text


def process_channel(ch, state, summaries, now):
    cid, name = ch["channel_id"], ch["name"]
    now_iso = now.isoformat()

    if ch.get("paused"):
        # 대시보드 탭/순서는 그대로 두되 새 영상 요약만 멈춘다.
        print(f"[INFO] paused, skipping: {name}")
        return

    try:
        videos = fetch_channel_videos(cid, playlist_id=ch.get("playlist_id"))
    except Exception as e:
        print(f"[WARN] video list fetch failed for {name}: {e}")
        return

    if cid not in state:
        # 첫 실행: 기존 영상은 요약하지 않고 '확인함'으로만 기록해서
        # 과거 영상 전체를 한번에 요약하며 API 비용이 폭증하는 것을 막는다.
        # 채널의 대상 범위를 바꿨을 때도(재생목록 -> 채널 전체, 출연자 필터 추가 등)
        # state.json 에서 그 채널 키를 지우면 이 경로를 타서 밀린 영상 없이 새것부터 시작한다.
        state[cid] = [v["video_id"] for v in videos]
        print(f"[INFO] Bootstrapped {name} with {len(videos)} existing videos (no summarization).")
        return

    seen = set(state[cid])
    already_summarized = {s["video_id"] for s in summaries if s.get("channel_id") == cid}
    new_videos = [v for v in videos if v["video_id"] not in seen]
    if not new_videos:
        return

    for v in new_videos:
        if v["video_id"] in already_summarized:
            # state.json이 초기화됐어도 이미 요약이 있으면 중복 생성하지 않는다.
            state[cid].append(v["video_id"])
            continue

        if not within_retention(v.get("published", ""), now):
            # 보관 기간(RETENTION_DAYS)보다 오래된 영상은 요약하지 않고 확인만 하고 넘어간다.
            state[cid].append(v["video_id"])
            continue

        # 사용자 지정(10/8): 혼잡으로 밀린 이 채널들의 10/7 이전 영상은 요약하지 않고 넘긴다(받아둔 자막도 버린다).
        # 날짜를 박아둔 일회성 정리라 10/8 이후 영상에는 영향이 없다.
        if name in SKIP_BACKLOG_CHANNELS:
            pub = parse_published(v.get("published", ""))
            if pub is not None and (pub + datetime.timedelta(hours=9)).date() < SKIP_BACKLOG_BEFORE_KST:
                state[cid].append(v["video_id"])
                clear_transcript_cache(v["video_id"])
                state.get(ATTEMPTS_KEY, {}).pop(v["video_id"], None)
                continue

        # 예정됐거나 아직 방송 중인 생방송은 유튜브가 길이를 0초로 준다. 그대로 두면 아래 '쇼츠' 판정에 걸려
        # '확인함'이 되고, 방송이 끝난 뒤에도 다시 안 본다. 2026-09-28 하나TV 모닝브리프가 이렇게 빠졌다
        # (07:16 실행 때 07:29 시작 예정이었다). 방송이 끝날 때까지 '확인함' 처리하지 않고 넘긴다.
        if v.get("live_pending"):
            if ch.get("exclude_live"):
                state[cid].append(v["video_id"])  # 어차피 생방송을 빼는 채널이면 바로 확인함
            else:
                print(f"[INFO] 생방송 예정/진행 중 — 끝난 뒤 요약: {name} - {v['title'][:40]}")
            continue

        duration = v.get("duration_seconds")
        if duration is not None and duration >= MAX_VIDEO_DURATION_SECONDS:
            print(f"[INFO] skipping long video ({duration // 60}min): {name} - {v['title']}")
            state[cid].append(v["video_id"])
            continue

        if duration is not None and duration < MIN_VIDEO_DURATION_SECONDS:
            print(f"[INFO] skipping shorts ({duration}s): {name} - {v['title']}")
            state[cid].append(v["video_id"])
            continue

        # 생방송 다시보기를 빼고 싶은 채널만 channels.json 에 exclude_live 를 켠다.
        # (삼프로TV의 '마켓 인사이드'처럼 생방송 자체가 요약 대상인 채널도 있어서 전역 설정이 아니다)
        if ch.get("exclude_live") and v.get("was_live"):
            print(f"[INFO] skipping live stream: {name} - {v['title']}")
            state[cid].append(v["video_id"])
            continue

        # 특정 출연자가 나오는 영상만 보고 싶은 채널은 channels.json 에 title_include 를 준다.
        # 채널 전체를 대상으로 하되 제목에 그 이름이 있는 것만 요약한다.
        # 이 필터가 없으면 채널의 모든 영상이 대상이 되어 비용이 크게 늘어난다.
        wanted = ch.get("title_include")
        if wanted and not any(w in v["title"] for w in wanted):
            state[cid].append(v["video_id"])
            continue

        # 막 올라온 영상은 아직 유튜브 자동 자막이 없다(유튜브가 음성을 인식해 만드는 데 시간이 걸린다).
        # 2026-09-28 업로드 3~4분 만에 요청한 22·29분 영상 두 개가 자막이 없어 AI 생성으로 넘어가 102크레딧이 나갔다.
        # 일정 시간이 지날 때까지는 요청하지 않고 '확인함' 처리도 하지 않아 다음 실행에서 다시 본다(크레딧 0).
        pub = parse_published(v.get("published", ""))
        if pub is not None and (now - pub).total_seconds() < MIN_VIDEO_AGE_MINUTES * 60:
            print(f"[INFO] 업로드 {int((now - pub).total_seconds() // 60)}분 — 자막 생성 대기 후 요약: {name} - {v['title'][:40]}")
            continue

        # 제미나이를 지금 못 부르면(휴식 시간·혼잡·한도) 요약은 다음 실행으로 미룬다.
        # 보내 봐야 거절만 쌓이고, 거절된 요청도 무료 하루 한도를 깎는다(10/5~6 밤에 그렇게 한도가 다 찼다).
        # 다만 자막은 지금 받아둔다(언제 받든 1크레딧으로 같다). 그러면 아침 07시엔 받아둔 자막으로 바로 요약한다
        # (사용자 지정, 10/7: 21~07시엔 자막만 받기). 이미 받아둔 영상은 아무것도 하지 않는다.
        # '확인함' 처리하지 않으니 영상이 빠지지는 않는다.
        summarize_now = gemini_ready(reserve=_brief_reserve())
        if not summarize_now and read_transcript_cache(v["video_id"]):
            _deferred.append(v["video_id"])
            continue

        # 같은 영상을 몇 번째 시도하는지 센다. 포기시키지는 않는다(그러면 영상이 영영 누락된다).
        # 다만 비정상적으로 반복되면 로그에 드러나야 원인을 찾을 수 있다.
        attempts = state.setdefault(ATTEMPTS_KEY, {})
        tries = attempts.get(v["video_id"], 0) + 1
        attempts[v["video_id"]] = tries
        if tries >= 5:
            warn(f"{tries}번째 시도 [{name}] {v['title'][:40]}")

        print(f"[INFO] New video: {name} - {v['title']}")
        time.sleep(REQUEST_INTERVAL_SECONDS)

        try:
            transcript_text = fetch_transcript_cached(v["video_id"], v["url"], v["title"])
        except Exception as e:
            warn(f"자막 실패 [{name}] {v['title'][:40]} : {e}")
            if not is_retryable_error(e) and not isinstance(e, TimeoutError):
                state[cid].append(v["video_id"])  # 자막 자체가 없는 영상일 확률이 높아 재시도하지 않는다.
            # 요청 한도 초과/크레딧 부족/타임아웃 등 일시적 오류면 '확인함' 처리하지 않아 다음 시간에 재시도된다.
            continue

        if not transcript_text:
            # 'native' 에서 Supadata 가 기존 자막을 못 받으면 '자막 없음'(206, 1크레딧)이 온다.
            # 2026-09-28 에 자막이 있는 영상도 이렇게 일시적으로 못 받은 적이 있다(그땐 auto 라 AI 생성으로 넘어가 58크레딧).
            # 그래서 바로 포기하지 않고 다음 실행에서 다시 받아본다. 다만 매번 1크레딧이라 NO_CAPTION_TRIES 번까지만.
            clear_transcript_cache(v["video_id"])
            if tries < NO_CAPTION_TRIES:
                warn(f"자막 없음 응답 [{name}] {v['title'][:40]} — 다음 실행에서 다시 시도 ({tries}/{NO_CAPTION_TRIES})")
            else:
                warn(f"자막 없음 {NO_CAPTION_TRIES}회 — 요약 건너뜀 [{name}] {v['title'][:40]}")
                state[cid].append(v["video_id"])
                attempts.pop(v["video_id"], None)
            continue

        if not summarize_now:
            print(f"[INFO] 자막만 받아둠(요약은 다음에): {name} - {v['title'][:40]}")
            _deferred.append(v["video_id"])
            continue

        # 요약은 여기서 바로 하지 않고 모아뒀다가 채널을 다 돈 뒤 2편씩 묶어 요청 한 번에 처리한다(summarize_pending).
        _pending.append({"ch": ch, "v": v, "text": transcript_text})

    state[cid] = state[cid][-STATE_HISTORY_PER_CHANNEL:]


_typo_fixes = None


def _load_typo_fixes():
    global _typo_fixes
    if _typo_fixes is None:
        data = load_json(TYPO_FIXES_FILE, {})
        _typo_fixes = {k: v for k, v in (data.get("fixes") or {}).items() if k and v and k != v}
    return _typo_fixes


def _norm(s):
    return re.sub(r"[\s*·]", "", s or "")


def clean_summary(result, label):
    """요약 직후 기계적 검사(무료, 사용자 지정 10/10 사후 검증 1단계).
    - 오타 사전(typo_fixes.json)으로 음성 인식 오타를 바로잡는다
    - 본문(key_summary·report_markdown)에 없는 종목은 tickers / picks 에서 뺀다
    - 같은 종목이 주도와 관망에 동시에 있으면 로그로 알린다(어느 쪽이 맞는지는 검수 작업이 판단)"""
    fixes = _load_typo_fixes()

    def fix(s):
        for wrong, right in fixes.items():
            s = s.replace(wrong, right)
        return s

    for key in ("key_summary", "report_markdown"):
        result[key] = fix(result.get(key) or "")
    for key in ("tickers", "keywords"):
        result[key] = [fix(t) for t in (result.get(key) or [])]
    for key in ("leading_picks", "watch_picks"):
        for p in result.get(key) or []:
            p["sector"] = fix(p.get("sector") or "")
            p["tickers"] = [fix(t) for t in (p.get("tickers") or [])]

    body = _norm(result["key_summary"] + result["report_markdown"])
    dropped = [t for t in result["tickers"] if _norm(t) not in body]
    result["tickers"] = [t for t in result["tickers"] if _norm(t) in body]
    for key in ("leading_picks", "watch_picks"):
        for p in result.get(key) or []:
            dropped += [t for t in p["tickers"] if _norm(t) not in body]
            p["tickers"] = [t for t in p["tickers"] if _norm(t) in body]
    if dropped:
        print(f"[INFO] 본문에 없는 종목 뺌 {sorted(set(dropped))}: {label[:40]}")

    lead = {_norm(t) for p in result.get("leading_picks") or [] for t in p["tickers"]}
    both = [t for p in result.get("watch_picks") or [] for t in p["tickers"] if _norm(t) in lead]
    if both:
        warn(f"주도와 관망에 같은 종목 {both}: {label[:40]}")
    return result


def _store_summary(ch, v, result, state, summaries, now_iso):
    """요약 결과 한 편을 저장하고 '확인함' 처리한다."""
    cid, name = ch["channel_id"], ch["name"]
    if result.get("investment_related") is False:
        # 사용자 지정(10/9): 투자와 무관한 영상(역사·교양·홍보 등)은 대시보드에 올리지 않는다.
        # 확인함으로 처리해 다시 요약하지 않는다.
        warn(f"투자와 무관한 영상이라 요약을 저장하지 않음 [{name}] {v['title'][:40]}")
        clear_transcript_cache(v["video_id"])
    else:
        result = clean_summary(result, v["title"])
        keep_transcript_for_review(v["video_id"], state, datetime.datetime.fromisoformat(now_iso))
        summaries.append(
            {
                "channel": name,
                "channel_id": cid,
                "video_id": v["video_id"],
                "title": v["title"],
                "url": v["url"],
                "published": v["published"],
                "fetched_at": now_iso,
                "key_summary": result.get("key_summary", ""),
                "report_markdown": result.get("report_markdown", ""),
                "tickers": result.get("tickers", []),
                "keywords": result.get("keywords", []),
                "leading_picks": result.get("leading_picks", []),
                "watch_picks": result.get("watch_picks", []),
            }
        )
    state.setdefault(cid, []).append(v["video_id"])
    state.get(ATTEMPTS_KEY, {}).pop(v["video_id"], None)
    state.get(PAIR_WAIT_KEY, {}).pop(v["video_id"], None)
    state.get(PAIR_FAIL_KEY, {}).pop(v["video_id"], None)


def summarize_pending(state, summaries, now):
    """모아둔 영상을 2편씩 묶어 요약한다(사용자 지정 10/10: 묶음 시험 7쌍 결과 품질 차이 없음 → 묶음으로 전환).
    무료 한도는 요청 횟수로 세므로 같은 한도로 두 배를 처리한다.
    - 혼자 남은 영상은 짝이 올 때까지 PAIR_WAIT_MINUTES 동안 기다린다(실행마다 새 영상은 0~1편이라
      기다리지 않으면 거의 묶이지 않는다). 그 뒤엔 혼자 요약한다.
      저녁 유료 시간대엔 기다리지 않는다(유료는 요청 수가 아니라 토큰으로 내서 묶어도 아낄 게 없다).
    - 묶음 요청이 제미나이 혼잡이 아닌 이유로 실패하면 다음 실행에서 다시 묶는다.
      그렇게 PAIR_FALLBACK_FAILS 번 실패한 영상은 하나씩 요약한다.
    반환: 저장한 편수"""
    now_iso = now.isoformat()
    waits = state.setdefault(PAIR_WAIT_KEY, {})
    fails = state.setdefault(PAIR_FAIL_KEY, {})
    # 다른 경로로 정리된 영상(건너뜀 등)의 기다림 기록이 쌓이지 않게 하루 지난 건 지운다
    for vid, at in list(waits.items()):
        if (now - datetime.datetime.fromisoformat(at)).total_seconds() > 86400:
            waits.pop(vid, None)
            fails.pop(vid, None)
    # 오래 기다린 영상부터 묶는다(혼자 남는 건 가장 최근 영상이 되게)
    items = sorted(_pending, key=lambda it: waits.get(it["v"]["video_id"], now_iso))
    _pending.clear()
    stored = 0

    # 혼자 남은 영상: 짝을 기다릴지 정한다
    if len(items) % 2 == 1 and gemini_mode() == "free":
        last = items[-1]
        vid = last["v"]["video_id"]
        first = waits.setdefault(vid, now_iso)
        waited = (now - datetime.datetime.fromisoformat(first)).total_seconds() / 60
        if waited < PAIR_WAIT_MINUTES:
            print(f"[INFO] 묶을 짝을 기다림({int(waited)}/{PAIR_WAIT_MINUTES}분): {last['ch']['name']} - {last['v']['title'][:40]}")
            items = items[:-1]
            # 짝 기다림은 실패가 아니므로 시도 횟수에서 뺀다(10/10: 기다리는 동안 '5번째 시도' 경고가 떴다)
            attempts = state.get(ATTEMPTS_KEY, {})
            if attempts.get(vid, 0) > 0:
                attempts[vid] -= 1

    groups = []
    for i in range(0, len(items), 2):
        group = items[i : i + 2]
        if len(group) == 2 and any(fails.get(it["v"]["video_id"], 0) >= PAIR_FALLBACK_FAILS for it in group):
            groups.extend([[it] for it in group])  # 묶음이 거듭 실패한 영상은 하나씩
        else:
            groups.append(group)

    for group in groups:
        label = " + ".join(f"[{it['ch']['name']}] {it['v']['title'][:30]}" for it in group)
        try:
            if len(group) == 1:
                it = group[0]
                results = [
                    call_with_timeout(
                        summarize_transcript, SUMMARIZE_TIMEOUT_SECONDS,
                        it["ch"]["name"], it["v"]["title"], it["text"], reserve=_brief_reserve(),
                    )
                ]
            else:
                results = call_with_timeout(
                    summarize_many, SUMMARIZE_TIMEOUT_SECONDS,
                    [(it["ch"]["name"], it["v"]["title"], it["text"]) for it in group],
                    reserve=_brief_reserve(), allow_paid=True,
                )
        except GeminiUnavailable as e:
            # 영상 문제가 아니라 제미나이가 전부 막힌 것. 실패 기록 대신 실행 끝에 '미룸' 한 줄로 남긴다.
            print(f"[INFO] 요약 미룸 {label} : {e}")
            _deferred.extend(it["v"]["video_id"] for it in group)
            continue
        except Exception as e:
            # 요약만 실패한 것이므로 자막 캐시는 남겨둔다. 다음 실행에서 자막을 다시 받지 않고(크레딧 0) 다시 시도한다.
            warn(f"요약 실패 {label} : {e}")
            if len(group) == 2:
                for it in group:
                    fails[it["v"]["video_id"]] = fails.get(it["v"]["video_id"], 0) + 1
            continue
        for it, result in zip(group, results):
            _store_summary(it["ch"], it["v"], result, state, summaries, now_iso)
            stored += 1
        if len(group) == 2:
            print(f"[INFO] 2편 묶음 요약 완료(요청 1번): {label}")
    return stored



def _save_data_files(state, summaries, channels, now):
    summaries.sort(key=lambda s: s.get("published", ""), reverse=True)
    trimmed = [s for s in summaries if within_retention(s.get("published", ""), now)][:MAX_SUMMARIES]

    save_json(STATE_FILE, state)
    save_json(SUMMARIES_FILE, trimmed)
    save_json(CHANNELS_OUT_FILE, channels)
    return trimmed


def _overlay_session_closes(report, published):
    """지수/금리/유가를 방송이 다룬 거래일의 검증된 시세 데이터로 채운다.
    방송에서 귀로 들은 수치는 쓰지 않는다."""
    # 방송 시점(UTC)을 미 동부로 옮기면 그 방송이 다룬 거래일이 나온다.
    target_date = (published - datetime.timedelta(hours=4)).date()

    try:
        closes = fetch_session_closes(target_date)
    except Exception as e:
        print(f"[WARN] session closes overlay skipped: {e}")
        return

    if not closes:
        return

    # 방송에서 귀로 들은 숫자는 검증되지 않았고 실제로 어긋난다(예: WTI 방송 -2.32% vs 실제 -0.51%).
    # 지수/금리/유가는 종가와 등락률 '둘 다' 시세 데이터로만 채우고, AI가 말한 수치는 쓰지 않는다.
    report["indices"] = [
        {"name": item["name"], **closes[item["name"]]}
        for item in BRIEF_INDICES
        if item["name"] in closes
    ]
    report["indices_note"] = "지수·금리·유가는 시장 데이터 종가 기준"

    # 어느 섹터가 좋았고 나빴는지도 방송 발언이 아니라 실제 섹터 ETF 시세로 보여준다.
    try:
        report["sectors"] = fetch_session_sectors(target_date)
    except Exception as e:
        print(f"[WARN] sector session fetch skipped: {e}")

    print(
        f"[INFO] morning brief: 지수 {len(report['indices'])}개 / "
        f"섹터 {len(report.get('sectors', []))}개를 시세 데이터로 채움 (거래일 {target_date})"
    )


def _refresh_market_overlay(report, now):
    """AI 요약은 그대로 두고, 시세로 채우는 블록(지수·업종)만 다시 덮는다.
    바뀐 게 없으면 저장하지 않는다. 매시간 의미 없는 커밋이 쌓이는 걸 막기 위해서다."""
    if not report.get("video_id"):
        return False

    snapshot = json.dumps(
        [report.get("indices"), report.get("sectors")], ensure_ascii=False, sort_keys=True
    )
    _overlay_session_closes(report, parse_published(report.get("published", "")) or now)
    if snapshot == json.dumps(
        [report.get("indices"), report.get("sectors")], ensure_ascii=False, sort_keys=True
    ):
        return False

    report["fetched_at"] = now.isoformat()
    save_json(MORNING_BRIEF_FILE, report)
    print("[INFO] morning brief: 시세 블록(지수·업종)만 갱신")
    return True


def update_themes(now):
    """오늘의 주도 테마만 매시간 갱신한다.
    목록 API 3번이면 끝나 부담이 없고, 무거운 종목 선별과 분리해 따로 저장한다.
    값이 그대로면 저장하지 않아 의미 없는 커밋이 쌓이지 않는다."""
    try:
        groups = fetch_theme_groups()
    except Exception as e:
        print(f"[WARN] 테마 조회 실패: {e}")
        return False
    if not groups:
        return False

    leaders = theme_leaders(groups, top=THEME_TOP)
    current = load_json(THEMES_FILE, {})
    if not isinstance(current, dict):
        current = {}
    if current.get("leaders") == leaders:
        return False

    save_json(
        THEMES_FILE,
        {"leaders": leaders, "group_count": len(groups), "updated_at": now.isoformat()},
    )
    print(f"[INFO] 주도 테마 갱신: {', '.join(t['name'] for t in leaders[:3])} ...")
    return True


def update_morning_breakout(now):
    """섹션4 모닝 브레이크아웃. 평일 09:30 이후 그날 한 번만 돈다.
    섹션 1~3(마감 후 일봉)과 실행 시점·데이터가 달라 파일도 따로 쓴다."""
    kst = now.astimezone(KST)
    if kst.weekday() >= 5:
        return False  # 주말엔 장이 없다
    in_window = (kst.hour, kst.minute) >= MORNING_AFTER and kst.hour < MORNING_BEFORE_HOUR
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    today = kst.date().isoformat()

    current = load_json(MORNING_FILE, {})
    if not isinstance(current, dict):
        current = {}
    if current.get("built_on") == today:
        return False
    if not (in_window or manual):
        return False

    try:
        data = build_morning_breakout(kst.date())
    except Exception as e:
        print(f"[WARN] 모닝 브레이크아웃 실패: {e}")
        save_json(MORNING_FILE, {"error": f"{type(e).__name__}: {e}", "items": [], "updated_at": now.isoformat()})
        return True

    if data.get("market_closed"):
        return False  # 휴장일. 직전 거래일 결과를 지우지 않는다

    data["built_on"] = today
    data["updated_at"] = now.isoformat()
    save_json(MORNING_FILE, data)
    return True


def update_screening(now):
    """종목 선별은 하루 한 번, 한국시간 15:50(정규장 마감 직후)에만 돌린다.
    전종목을 훑어 2~3분 걸리므로 매시간 돌릴 수는 없다.
    주도 테마는 여기 끼지 않고 update_themes 가 매시간 따로 갱신한다.

    손으로 돌린 실행(Run workflow)은 시간대 밖이어도 '그날 아직 안 만들었으면' 돌려준다.
    규칙을 고쳐 배포해놓고 마감까지 기다리지 않고 결과를 보기 위해서다."""
    kst = now.astimezone(KST)
    today = kst.date().isoformat()
    current = load_json(SCREENING_FILE, {})
    if not isinstance(current, dict):
        # 기능을 만들기 전 자리만 잡아둔 옛 파일이 빈 배열([])이라 .get 에서 터진다.
        # 그 예외가 바깥에서 삼켜져 '아무 일도 안 일어난 것처럼' 보였다.
        current = {}

    in_window = (kst.hour, kst.minute) >= SCREENING_AFTER and kst.hour < SCREENING_BEFORE_HOUR
    same_rules = current.get("rules_version") == SCREENING_RULES_VERSION
    up_to_date = (current.get("built_slot") or "").startswith(today) and same_rules

    if in_window:
        # 마감 실행은 '마감 후 결과'가 있을 때만 건너뛴다. 그날 낮에 손으로 돌린 결과(장중 미완성 일봉)가 있어도
        # 마감 결과로 덮어써야 한다. 예전엔 '오늘 만든 게 있으면' 건너뛰어서 장중 결과가 하루 종일 남았다.
        if current.get("built_slot") == f"{today}/close" and same_rules:
            return False
        stamp = f"{today}/close"
    elif os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and not up_to_date:
        stamp = f"{today}/manual"
        print(f"[INFO] 스크리닝: 시간대 밖({kst:%H:%M})이지만 수동 실행이라 진행한다")
    else:
        return False

    try:
        data = build_screening(kst.date(), charts_dir=DATA_DIR / "charts")
    except Exception as e:
        # 실패를 조용히 삼키면 화면은 '아직 준비 중'으로만 보이고 원인을 알 수 없다.
        # 워크플로 로그는 저장소 관리자만 볼 수 있어서, 실패 사실을 결과 파일에 남긴다.
        print(f"[WARN] 스크리닝 실패: {e}")
        save_json(
            SCREENING_FILE,
            {
                "error": f"{type(e).__name__}: {e}",
                "built_slot": "",  # 다음 실행에서 다시 시도하도록 슬롯은 비워둔다
                "rules_version": SCREENING_RULES_VERSION,
                "updated_at": now.isoformat(),
                "entries": [],
                "watch": [],
                "danger": [],
            },
        )
        return True

    data["built_slot"] = stamp
    data["rules_version"] = SCREENING_RULES_VERSION
    data["updated_at"] = now.isoformat()
    # 예약 실행은 마감 후에만 돌지만, 손으로 돌리면 장중일 수 있다.
    # 그때는 그날 일봉이 아직 안 끝난 상태로 판정한 것이므로 화면에 표시해준다.
    # 휴장일(추석 등)엔 시각이 장중이어도 최신 일봉이 이미 확정된 전 거래일 것이다.
    # 기준일이 오늘일 때만 장중으로 본다.
    data["intraday"] = (
        kst.weekday() < 5
        and (9, 0) <= (kst.hour, kst.minute) < (15, 30)
        and data.get("as_of_trading_day") == today
    )
    save_json(SCREENING_FILE, data)
    return True


def update_us_screening(now):
    """미장 종목 선별. 한국시간 화~토 아침(미국 장 마감 뒤) 하루 한 번.
    국장과 같이, 손으로 돌린 실행은 시간대 밖이어도 그날 아직 안 만들었으면 돌려준다."""
    kst = now.astimezone(KST)
    today = kst.date().isoformat()
    current = load_json(US_SCREENING_FILE, {})
    if not isinstance(current, dict):
        current = {}

    in_window = (
        kst.weekday() in US_SCREENING_WEEKDAYS
        and (kst.hour, kst.minute) >= US_SCREENING_AFTER
        and kst.hour < US_SCREENING_BEFORE_HOUR
    )
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    same_rules = current.get("rules_version") == SCREENING_RULES_VERSION
    slot = current.get("built_slot") or ""

    if in_window:
        # 국장과 같다. 아침 결과가 이미 있을 때만 건너뛴다. 새벽에 손으로 돌린 장중 결과는 덮어쓴다.
        if slot == f"{today}/close" and same_rules:
            return False
        stamp = f"{today}/close"
    elif manual and not (slot.startswith(today) and same_rules):
        stamp = f"{today}/manual"
        print(f"[INFO] 미장 스크리닝: 시간대 밖({kst:%H:%M})이지만 수동 실행이라 진행한다")
    else:
        return False

    try:
        data = build_us_screening(charts_dir=DATA_DIR / "charts_us")
    except Exception as e:
        # 국장과 같은 이유로 실패를 결과 파일에 남긴다. 슬롯을 비워 다음 실행에서 다시 시도한다.
        print(f"[WARN] 미장 스크리닝 실패: {e}")
        save_json(
            US_SCREENING_FILE,
            {
                "market": "us",
                "error": f"{type(e).__name__}: {e}",
                "built_slot": "",
                "rules_version": SCREENING_RULES_VERSION,
                "updated_at": now.isoformat(),
                "sections": [],
            },
        )
        return True

    data["built_slot"] = stamp
    data["rules_version"] = SCREENING_RULES_VERSION
    data["updated_at"] = now.isoformat()
    # 손으로 미국 정규장 중에 돌리면 그날 일봉이 미완성이다. 화면에 표시해준다.
    # 미국 장중 = 한국시간 22:30~05:00(서머타임) / 23:30~06:00(겨울). 넉넉히 22:30~06:00 으로 본다.
    # 월~금 밤에 열려 화~토 새벽에 닫힌다.
    us_evening = (kst.hour, kst.minute) >= (22, 30) and kst.weekday() <= 4
    us_early = kst.hour < 6 and 1 <= kst.weekday() <= 5
    data["intraday"] = bool(manual and not in_window and (us_evening or us_early))
    save_json(US_SCREENING_FILE, data)
    return True


_SUMMARY_FIELDS = ("key_summary", "report_markdown", "tickers", "keywords", "leading_picks", "watch_picks")


def _load_pair_trial():
    data = load_json(PAIR_TRIAL_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("pool", [])
    data.setdefault("pairs", [])
    return data


def _pair_trial_keep(v, channel_name, result):
    """시험에 쓸 영상이 더 필요하면 이 영상의 단독 요약을 적어두고 True(자막 캐시를 지우지 말 것)."""
    data = _load_pair_trial()
    if len(data["pairs"]) * 2 + len(data["pool"]) >= PAIR_TRIAL_TARGET_PAIRS * 2:
        return False
    data["pool"].append(
        {
            "video_id": v["video_id"],
            "channel": channel_name,
            "title": v["title"],
            "url": v["url"],
            "single": {k: result.get(k) for k in _SUMMARY_FIELDS},
        }
    )
    save_json(PAIR_TRIAL_FILE, data)
    return True


def update_pair_trial(now):
    """남겨둔 영상 2개를 요청 한 번에 묶어 다시 요약해 단독 요약 옆에 저장한다. 실행마다 한 쌍만.
    채널 요약보다 먼저 돈다(밀린 영상이 있어도). 일반 요약과 같은 시간대 규칙: 낮엔 무료, 18~21시엔 유료
    (사용자 지정 10/9: 유료 시간대에도 돌려서 빨리 모으기). 휴식 시간·혼잡이면 다음 실행으로 미룬다."""
    data = _load_pair_trial()
    if len(data["pairs"]) >= PAIR_TRIAL_TARGET_PAIRS or len(data["pool"]) < 2:
        return False
    if not gemini_ready(reserve=_brief_reserve()):
        return False
    picked = data["pool"][:2]
    texts = [read_transcript_cache(p["video_id"]) for p in picked]
    if not all(texts):
        # 자막 캐시가 없어졌으면 그 영상은 시험에서 뺀다(다시 받으면 크레딧이 나가므로)
        data["pool"] = [p for p, t in zip(picked, texts) if t] + data["pool"][2:]
        save_json(PAIR_TRIAL_FILE, data)
        return True
    try:
        reports = call_with_timeout(
            summarize_many,
            SUMMARIZE_TIMEOUT_SECONDS,
            [(p["channel"], p["title"], t) for p, t in zip(picked, texts)],
            reserve=_brief_reserve(),
            allow_paid=True,
        )
    except GeminiUnavailable as e:
        print(f"[INFO] 묶음 요약 시험 미룸: {e}")
        return False
    except Exception as e:
        warn(f"묶음 요약 시험 실패(다음 실행에서 다시): {e}")
        return False
    # 기준선: 첫 영상을 같은 방식(단독)으로 한 번 더 요약한다. 단독끼리도 이만큼 달라지는지 봐야
    # 단독↔묶음 차이가 묶음 탓인지 그냥 실행마다 생기는 편차인지 가를 수 있다. 실패해도 시험은 그대로 저장한다.
    single_again = None
    try:
        again = call_with_timeout(
            summarize_transcript,
            SUMMARIZE_TIMEOUT_SECONDS,
            picked[0]["channel"],
            picked[0]["title"],
            texts[0],
            reserve=_brief_reserve(),
        )
        single_again = {k: again.get(k) for k in _SUMMARY_FIELDS}
    except Exception as e:
        # 10/9 두 쌍 모두 기준선이 빠졌는데 이유가 안 남았다. 로그에 남겨 원인을 보이게 한다.
        warn(f"묶음 시험 기준선(단독 재요약) 건너뜀: {e}")
    videos = [{**p, "paired": {k: r.get(k) for k in _SUMMARY_FIELDS}} for p, r in zip(picked, reports)]
    if single_again:
        videos[0]["single_again"] = single_again
    data["pairs"].append({"at": now.isoformat(), "prompt_v": PAIR_TRIAL_PROMPT_V, "videos": videos})
    data["pool"] = data["pool"][2:]
    save_json(PAIR_TRIAL_FILE, data)
    for p in picked:
        clear_transcript_cache(p["video_id"])
    print(f"[INFO] 묶음 요약 시험 {len(data['pairs'])}/{PAIR_TRIAL_TARGET_PAIRS} 완료")
    return True


def update_gemini_diag(now):
    """10/6 밤 3.5-flash 가 요청 30여 번 만에 '하루 한도'로 거절됐다. 원인을 확정하려고 한 번만 진단한다.
    밤 휴식 시간과 상관없이 돈다(요청 8번뿐)."""
    current = load_json(GEMINI_DIAG_FILE, {})
    if isinstance(current, dict) and current.get("version") == GEMINI_DIAG_VERSION:
        return False
    result = diagnose_keys(only_kind=GEMINI_DIAG_ONLY_KIND)
    result.update({"version": GEMINI_DIAG_VERSION, "checked_at": now.isoformat()})
    save_json(GEMINI_DIAG_FILE, result)
    print(f"[INFO] 제미나이 키 진단: 같은 프로젝트={result['same_project']}, 유료 키가 무료 한도={result['paid_key_on_free_tier']}")
    return True


def update_gemini_key_check(now):
    """무료 제미나이 키가 작동하는지 하루 한 번만 확인해 파일로 남긴다. 키 값은 남기지 않는다.
    예전엔 실패하면 매 실행 다시 확인했는데, 한도가 찬 날엔 20분마다 거절 요청을 하나씩 더 보내 한도만 깎았다(10/6)."""
    today = now.astimezone(KST).date().isoformat()
    current = load_json(GEMINI_KEY_CHECK_FILE, {})
    if isinstance(current, dict) and current.get("checked_on") == today:
        return False
    if gemini_quiet_now():
        return False  # 밤 휴식 시간엔 확인 요청도 보내지 않는다
    result = check_free_key()
    result.update({"checked_on": today, "checked_at": now.isoformat()})
    print(f"[INFO] 무료 제미나이 키 확인: {'정상' if result['ok'] else '실패'} — {result['message']}")
    if isinstance(current, dict) and {k: current.get(k) for k in ("ok", "status", "message", "checked_on")} == {
        k: result.get(k) for k in ("ok", "status", "message", "checked_on")
    }:
        return False  # 같은 결과면 저장하지 않는다(의미 없는 커밋 방지)
    save_json(GEMINI_KEY_CHECK_FILE, result)
    return True


def update_tracking(now):
    """스크리너가 뽑은 종목을 박제하고 20거래일 성과를 추적한다(tracking.py 참고).
    매시간 불리지만 야후 조회는 시장마다 하루 한 번(마감 뒤)뿐이다. 바뀐 게 없으면 저장하지 않는다."""
    data = load_json(TRACKING_FILE, {})
    if not isinstance(data, dict):
        data = {}
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)

    added = trk.snapshot(
        data,
        load_json(SCREENING_FILE, {}),
        load_json(US_SCREENING_FILE, {}),
        load_json(MORNING_FILE, {}),
        now,
    )
    # 과거 발굴분(커밋 기록에서 꺼낸 당일 결과). 한 번 반영하면 다시 반영하지 않는다.
    added += trk.apply_seed(data, load_json(TRACKING_SEED_FILE, {}), now)
    if added:
        print(f"[INFO] 성과 추적: {added}종목 새로 박제")
    trk.update(data, now)
    # 겹침 정리는 추적 계산 뒤에 한다(앞선 기록이 언제 끝났는지 알아야 새 발굴인지 판단된다).
    dup = trk.dedupe(data.get("positions", []))
    if dup:
        print(f"[INFO] 성과 추적: 추적 중 다시 뽑힌 기록 {dup}건 정리")
    data["summary"] = trk.summarize(data)

    if json.dumps(data, ensure_ascii=False, sort_keys=True) == before:
        return False
    data["updated_at"] = now.isoformat()
    save_json(TRACKING_FILE, data)
    return True


def update_calendar(now):
    """증시 캘린더를 하루에 한 번만 다시 만든다.
    한 번에 100일 넘게 조회하므로 매시간 돌리면 API 호출이 낭비된다.

    기준 날짜는 반드시 한국시간이어야 한다. UTC 로 잡으면 한국 기준 00~09시 사이에는
    '어제'로 계산돼서, 그 시간대 실행이 전부 '오늘 이미 만들었다'며 건너뛴다.

    생성 규칙을 고쳤을 때도 다시 만들어야 하므로 builder_version 을 같이 본다.
    날짜만 보면, 코드를 고쳐 배포해도 그날 안에는 반영이 안 된다."""
    current = load_json(CALENDAR_FILE, {})
    kst_today = now.astimezone(KST).date()
    today = kst_today.isoformat()
    if (
        current.get("built_on") == today
        and current.get("builder_version") == CALENDAR_BUILDER_VERSION
    ):
        # 오늘 이미 만들었으면 발표된 지표·실적의 실제치만 채운다(3일치, 호출 6번).
        if refresh_values(current, kst_today):
            current["values_updated_at"] = now.isoformat()
            save_json(CALENDAR_FILE, current)
            print("[INFO] calendar: 발표된 실제치 갱신")
            return True
        return False

    # 국내 실적 컨센서스 저장소. 발표 순간 네이버에서 컨센서스가 사라지므로 발표 전 값을 여기 적어둔다.
    consensus = load_json(KR_CONSENSUS_FILE, {})
    if not isinstance(consensus, dict):
        consensus = {}
    data = build_calendar(kst_today, kr_consensus=consensus)
    save_json(KR_CONSENSUS_FILE, consensus)
    data["built_on"] = today
    data["builder_version"] = CALENDAR_BUILDER_VERSION
    data["updated_at"] = now.isoformat()
    save_json(CALENDAR_FILE, data)
    return True


def _youtube_has_captions(video_id):
    """유튜브 영상 페이지에 자막 목록(captionTracks)이 있는지. True/False, 확인 실패면 None.
    자동 자막(asr)도 여기 잡힌다. 비용 없음."""
    try:
        resp = requests.get(
            f"https://www.youtube.com/watch?v={video_id}&hl=ko",
            headers={"Accept-Language": "ko-KR,ko;q=0.9", "User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        if resp.status_code != 200 or "ytInitialPlayerResponse" not in resp.text:
            return None  # 차단·동의 페이지 등으로 제대로 못 읽었다
        if '"captionTracks"' in resp.text:
            return True
        # 봇 확인('로그인해서 봇이 아님을 확인') 페이지는 자막 목록이 원래 빠져 있다. 그땐 '없다'고 단정하지 않는다
        # (단정하면 당잠사가 영영 처리되지 않는다). 재생 가능 상태(OK)로 정상적으로 읽었을 때만 False.
        if re.search(r'"playabilityStatus":\{"status":"OK"', resp.text):
            return False
        return None
    except Exception:
        return None


def update_morning_brief(now):
    """당잠사(한국경제TV) 최신 방송 1건만 분석해 아침 리포트를 만든다.
    이미 같은 영상으로 만들어둔 리포트가 있으면 AI 요약은 건너뛰고 시세 블록만 갱신한다.
    True를 반환하면 파일이 저장된 것이다."""
    videos = fetch_channel_videos(mb.CHANNEL_ID, max_results=5, playlist_id=mb.PLAYLIST_ID)
    if not videos:
        print("[WARN] morning brief: 당잠사 재생목록이 비어 있습니다")
        return False

    latest = videos[0]
    current = load_json(MORNING_BRIEF_FILE, {})
    if (
        current.get("video_id") == latest["video_id"]
        and current.get("prompt_version") == MORNING_BRIEF_PROMPT_VERSION
    ):
        # 같은 방송이고 형식도 그대로니 AI 요약은 다시 만들 필요가 없다(= Gemini 비용 0).
        # 다만 지수·업종은 시세에서 채우는 블록이라, 업종 목록 같은 코드를 고치면
        # 새 방송이 올라올 때까지 낡은 값이 그대로 남는다. 야후 조회는 공짜라 매번 다시 덮는다.
        return _refresh_market_overlay(current, now)

    duration = latest.get("duration_seconds")
    if duration is not None and duration >= MAX_VIDEO_DURATION_SECONDS:
        print(f"[INFO] morning brief: 1시간 초과라 건너뜀 ({duration // 60}min)")
        return False

    # 막 올라온 방송(또는 아직 방송 중·예정)은 유튜브 자동 자막이 아직 없다. 일정 시간 기다린다(크레딧 0).
    pub = parse_published(latest.get("published", ""))
    if latest.get("live_pending") or (pub is not None and (now - pub).total_seconds() < MIN_VIDEO_AGE_MINUTES * 60):
        print(f"[INFO] morning brief: 업로드 {int((now - pub).total_seconds() // 60)}분 — 자막 생성 대기")
        return _refresh_market_overlay(current, now) if current.get("video_id") else False

    if not gemini_ready(paid_only=True):  # 당잠사는 올라오자마자 유료로(사용자 지정, 10/8)
        print("[INFO] morning brief: 유료 제미나이를 쓸 수 없음(잔액/거절) — 다음 실행에서 분석")
        _deferred.append(latest["video_id"])
        return _refresh_market_overlay(current, now) if current.get("video_id") else False

    print(f"[INFO] morning brief: {latest['title']}")
    # 생방송 다시보기라 유튜브 자동 자막이 몇 시간 뒤에야 생긴다(10/8: 06:16 업로드, 08시에도 자막 없음).
    # 자막이 없을 때 자막 업체에 물으면 '자막 없음'에 1크레딧씩 나가고 20분마다 반복됐다.
    # 유튜브 페이지에서 자막이 생겼는지 먼저 공짜로 확인한다. 확인 자체가 실패하면 예전처럼 그냥 받아본다.
    if not read_transcript_cache(latest["video_id"]) and _youtube_has_captions(latest["video_id"]) is False:
        print("[INFO] morning brief: 유튜브 자동 자막이 아직 없음 — 다음 실행에서 다시 확인(크레딧 0)")
        return _refresh_market_overlay(current, now) if current.get("video_id") else False
    # 프롬프트를 고쳐 같은 방송을 다시 분석할 때 자막을 또 받지 않도록 캐시를 쓴다
    transcript_text = fetch_transcript_cached(latest["video_id"], latest["url"], latest["title"])
    if not transcript_text:
        print("[WARN] morning brief: 자막이 비어 있습니다")
        clear_transcript_cache(latest["video_id"])
        return False

    published = parse_published(latest.get("published", ""))
    kst_date = (published + datetime.timedelta(hours=9)) if published else now + datetime.timedelta(hours=9)
    broadcast_date = f"{kst_date.year}년 {kst_date.month:02d}월 {kst_date.day:02d}일"

    report = call_with_timeout(
        mb.build_morning_brief, SUMMARIZE_TIMEOUT_SECONDS, latest["title"], transcript_text, broadcast_date
    )
    _overlay_session_closes(report, published or now)

    report.update(
        {
            "video_id": latest["video_id"],
            "prompt_version": MORNING_BRIEF_PROMPT_VERSION,
            "title": latest["title"],
            "url": latest["url"],
            "published": latest.get("published", ""),
            "fetched_at": now.isoformat(),
            "program": mb.PROGRAM_NAME,
            "channel": mb.CHANNEL_NAME,
        }
    )
    save_json(MORNING_BRIEF_FILE, report)
    print(f"[INFO] morning brief saved: {latest['video_id']}")
    return True


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def commit_and_push(message):
    """채널 하나 끝날 때마다 즉시 커밋+푸시해서, 실행이 중간에 취소/중단돼도
    그때까지 처리한 영상(과 이미 쓴 크레딧)이 저장되지 않고 날아가는 일을 막는다."""
    _git("config", "user.name", "stock-dashboard-bot")
    _git("config", "user.email", "actions@github.com")
    _git("add", "docs/data")

    if _git("diff", "--cached", "--quiet").returncode == 0:
        return  # 변경 없음

    commit = _git("commit", "-m", message)
    if commit.returncode != 0:
        print(f"[WARN] git commit failed: {commit.stderr.strip()}")
        return

    # 매시 정각 예약 실행과 수동 실행이 겹치면, 먼저 끝난 쪽이 밀어넣은 커밋 때문에
    # 나중 쪽 push 가 거절된다. 실제로 실패한 게 아닌데 워크플로가 빨갛게 뜬다.
    # 원격 것을 받아 내 커밋을 그 위에 얹고 다시 시도한다.
    for attempt in range(3):
        push = _git("push")
        if push.returncode == 0:
            print(f"[INFO] committed and pushed: {message}")
            return
        print(f"[WARN] git push 거절됨 ({attempt + 1}/3): {push.stderr.strip()}")
        pull = _git("pull", "--rebase", "origin", "main")
        if pull.returncode != 0:
            print(f"[WARN] git pull --rebase 실패: {pull.stderr.strip()}")
            break
        time.sleep(2)

    print("[WARN] git push 최종 실패 — 이번 변경은 다음 실행에서 다시 올라간다")


def main():
    # channels.json 과 summaries.json 은 기본값으로 넘어가면 안 된다.
    # 채널 목록이 비면 아무것도 안 돌고, 요약본이 비면 그 빈 값으로 덮어써 대시보드가 통째로 날아간다.
    channels = load_json(CHANNELS_FILE, [], required=True)
    state = load_json(STATE_FILE, {})
    summaries = load_json(SUMMARIES_FILE, [], required=True)
    now = datetime.datetime.now(datetime.timezone.utc)

    try:
        if update_gemini_diag(now):
            commit_and_push(f"chore: gemini key diagnostic {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] 제미나이 키 진단 실패: {e}")

    # 하루 한 번 구글 모델 목록을 보고 새 Flash 모델은 추가, 통합·종료된 모델은 뺀다(사용자 지정 10/10). 무료 한도와 무관.
    try:
        for change in refresh_model_list():
            warn(change)
    except Exception as e:
        print(f"[WARN] 제미나이 모델 목록 갱신 실패: {e}")

    # 당잠사(아침 브리핑)를 채널 요약보다 먼저 돌린다. 무료 한도가 빠듯해서 채널 요약이 먼저 다 쓰면
    # 아침 브리핑이 오후 4시 한도 초기화 뒤로 밀린다(10/7).
    try:
        if update_morning_brief(now):
            commit_and_push(f"chore: update morning brief {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] morning brief failed: {e}")

    # 묶음 요약 시험(update_pair_trial)은 10/10 끝냈다(7쌍, 품질 차이 없음 → 실제 요약을 묶음으로 전환).

    # 채널마다 새 영상을 확인하고 자막까지 받아둔다. 요약은 다 모은 뒤 2편씩 묶어서 한다.
    for ch in channels:
        process_channel(ch, state, summaries, now)
        summaries = _save_data_files(state, summaries, channels, now)
        commit_and_push(f"chore: update data ({ch['name']}) {now.isoformat()}")

    try:
        summarize_pending(state, summaries, now)
    except Exception as e:
        warn(f"묶음 요약 처리 중 오류: {e}")
    try:
        prune_review_transcripts(state, now)
    except Exception as e:
        print(f"[WARN] 검수용 자막 정리 실패: {e}")
    # 저장한 요약이 없어도 짝 기다림 기록(state)은 남긴다. 바뀐 게 없으면 커밋하지 않는다.
    summaries = _save_data_files(state, summaries, channels, now)
    commit_and_push(f"chore: update data (요약) {now.isoformat()}")

    try:
        if update_calendar(now):
            commit_and_push(f"chore: update calendar {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] calendar build failed: {e}")

    try:
        if update_themes(now):
            commit_and_push(f"chore: update themes {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] themes failed: {e}")

    try:
        if update_morning_breakout(now):
            commit_and_push(f"chore: update morning breakout {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] morning breakout failed: {e}")

    try:
        if update_screening(now):
            commit_and_push(f"chore: update screening {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] screening failed: {e}")

    try:
        if update_us_screening(now):
            commit_and_push(f"chore: update us screening {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] us screening failed: {e}")

    # 스크리닝들이 끝난 뒤에 돌아야 그날 뽑힌 종목을 바로 박제한다.
    try:
        if update_tracking(now):
            commit_and_push(f"chore: update tracking {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] tracking failed: {e}")

    try:
        if update_gemini_key_check(now):
            commit_and_push(f"chore: gemini free key check {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] 무료 키 확인 실패: {e}")

    if _deferred and gemini_quiet_now():
        # 밤 휴식은 정해둔 일이라 실패 기록에 남기지 않는다(20분마다 같은 줄이 쌓이기만 한다)
        print(f"[INFO] 휴식 시간({QUIET_LABEL}) — {len(_deferred)}편은 다음 날 {ACTIVE_START_KST[0]:02d}시 첫 실행에서 요약")
    elif _deferred:
        warn(f"제미나이 모델이 모두 혼잡/한도라 {len(_deferred)}편 요약을 다음 실행으로 미룸")

    try:
        if save_warnings(now):
            commit_and_push(f"chore: update pipeline log {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] 실패 기록 저장 실패: {e}")

    print("[INFO] Pipeline run complete.")


if __name__ == "__main__":
    main()
