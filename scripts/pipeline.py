"""메인 파이프라인: RSS 신규 영상 감지 -> 자막 추출 -> 요약 -> 저장.
GitHub Actions에서 1시간마다 실행된다 (.github/workflows/pipeline.yml 참고).
"""
import datetime
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from youtube_check import fetch_channel_videos
from summarize import summarize_transcript
from transcript import get_transcript, is_retryable_error
from market_data import BRIEF_INDICES, fetch_session_closes, fetch_session_sectors
import morning_brief as mb
from calendar_data import KST, build_calendar
from screening import build_screening, fetch_theme_groups, theme_leaders
from screening_us import build_us_screening
from morning_breakout import build_morning_breakout

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
CHANNELS_FILE = ROOT / "scripts" / "channels.json"
STATE_FILE = DATA_DIR / "state.json"
SUMMARIES_FILE = DATA_DIR / "summaries.json"
CHANNELS_OUT_FILE = DATA_DIR / "channels.json"
MORNING_BRIEF_FILE = DATA_DIR / "morning_brief.json"
CALENDAR_FILE = DATA_DIR / "calendar.json"
SCREENING_FILE = DATA_DIR / "screening.json"
US_SCREENING_FILE = DATA_DIR / "screening_us.json"
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
SUMMARIZE_TIMEOUT_SECONDS = 300  # summarize.py의 재시도(최대 85초 대기)까지 포함해서 넉넉히 잡는다
MAX_VIDEO_DURATION_SECONDS = 3600  # 1시간 넘는 영상은 자막 생성 비용이 커서 아예 요약하지 않는다.
MIN_VIDEO_DURATION_SECONDS = 181  # 3분 이하는 쇼츠(Shorts)라 요약하지 않는다. 유튜브 쇼츠 최대 길이가 3분.

# 캘린더 생성 규칙(수집 범위·시간대 변환·범주 등)이 바뀌면 이 숫자를 올린다.
# 캘린더는 하루 한 번만 만들기 때문에, 이게 없으면 코드를 고쳐도 그날은 옛 데이터가 그대로 남는다.
CALENDAR_BUILDER_VERSION = 7

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


def warn(message):
    """로그에도 찍고 파일에도 남긴다. 파일은 공개 저장소에 올라가 나중에 읽을 수 있다."""
    print(f"[WARN] {message}")
    _warnings.append({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "message": str(message)[:300]})


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
            warn(f"자막 비어있음 [{name}] {v['title'][:40]}")
            state[cid].append(v["video_id"])
            clear_transcript_cache(v["video_id"])
            continue

        try:
            result = call_with_timeout(summarize_transcript, SUMMARIZE_TIMEOUT_SECONDS, name, v["title"], transcript_text)
        except Exception as e:
            # 요약만 실패한 것이므로 자막 캐시는 남겨둔다.
            # 다음 실행에서 자막을 다시 받지 않고(크레딧 0) 요약만 다시 시도한다.
            warn(f"요약 실패 [{name}] {v['title'][:40]} : {e}")
            continue

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
        state[cid].append(v["video_id"])
        # 요약까지 끝났으니 캐시와 시도 기록을 정리한다
        clear_transcript_cache(v["video_id"])
        state.get(ATTEMPTS_KEY, {}).pop(v["video_id"], None)

    state[cid] = state[cid][-STATE_HISTORY_PER_CHANNEL:]



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
        return False

    data = build_calendar(kst_today)
    data["built_on"] = today
    data["builder_version"] = CALENDAR_BUILDER_VERSION
    data["updated_at"] = now.isoformat()
    save_json(CALENDAR_FILE, data)
    return True


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

    print(f"[INFO] morning brief: {latest['title']}")
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

    for ch in channels:
        process_channel(ch, state, summaries, now)
        summaries = _save_data_files(state, summaries, channels, now)
        commit_and_push(f"chore: update data ({ch['name']}) {now.isoformat()}")

    try:
        if update_morning_brief(now):
            commit_and_push(f"chore: update morning brief {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] morning brief failed: {e}")

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

    try:
        if save_warnings(now):
            commit_and_push(f"chore: update pipeline log {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] 실패 기록 저장 실패: {e}")

    print("[INFO] Pipeline run complete.")


if __name__ == "__main__":
    main()
