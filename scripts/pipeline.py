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

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
CHANNELS_FILE = ROOT / "scripts" / "channels.json"
STATE_FILE = DATA_DIR / "state.json"
SUMMARIES_FILE = DATA_DIR / "summaries.json"
CHANNELS_OUT_FILE = DATA_DIR / "channels.json"
MORNING_BRIEF_FILE = DATA_DIR / "morning_brief.json"
CALENDAR_FILE = DATA_DIR / "calendar.json"
SCREENING_FILE = DATA_DIR / "screening.json"
THEMES_FILE = DATA_DIR / "themes.json"

MAX_SUMMARIES = 500
RETENTION_DAYS = 7
STATE_HISTORY_PER_CHANNEL = 100
REQUEST_INTERVAL_SECONDS = 3  # 자막/AI API를 너무 빨리 연달아 호출해서 429(요청 한도 초과)에 걸리는 것을 막는다.
TRANSCRIPT_TIMEOUT_SECONDS = 90
SUMMARIZE_TIMEOUT_SECONDS = 300  # summarize.py의 재시도(최대 85초 대기)까지 포함해서 넉넉히 잡는다
MAX_VIDEO_DURATION_SECONDS = 3600  # 1시간 넘는 영상은 자막 생성 비용이 커서 아예 요약하지 않는다.
MIN_VIDEO_DURATION_SECONDS = 181  # 3분 이하는 쇼츠(Shorts)라 요약하지 않는다. 유튜브 쇼츠 최대 길이가 3분.

# 캘린더 생성 규칙(수집 범위·시간대 변환·범주 등)이 바뀌면 이 숫자를 올린다.
# 캘린더는 하루 한 번만 만들기 때문에, 이게 없으면 코드를 고쳐도 그날은 옛 데이터가 그대로 남는다.
CALENDAR_BUILDER_VERSION = 5

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
SCREENING_RULES_VERSION = 8


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
    pub = parse_published(pub_iso)
    if pub is None:
        return True  # 날짜를 못 읽으면 실수로 지우지 않고 남겨둔다
    return pub >= now - datetime.timedelta(days=RETENTION_DAYS)


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

        print(f"[INFO] New video: {name} - {v['title']}")
        time.sleep(REQUEST_INTERVAL_SECONDS)

        try:
            transcript_text = call_with_timeout(get_transcript, TRANSCRIPT_TIMEOUT_SECONDS, v["url"])
        except Exception as e:
            print(f"[WARN] transcript failed for {v['url']}: {e}")
            if not is_retryable_error(e) and not isinstance(e, TimeoutError):
                state[cid].append(v["video_id"])  # 자막 자체가 없는 영상일 확률이 높아 재시도하지 않는다.
            # 요청 한도 초과/크레딧 부족/타임아웃 등 일시적 오류면 '확인함' 처리하지 않아 다음 시간에 재시도된다.
            continue

        if not transcript_text:
            print(f"[WARN] empty transcript for {v['url']}")
            state[cid].append(v["video_id"])
            continue

        try:
            result = call_with_timeout(summarize_transcript, SUMMARIZE_TIMEOUT_SECONDS, name, v["title"], transcript_text)
        except Exception as e:
            # 요약 실패(예: 일시적인 API 요청 한도 초과, 타임아웃)는 '확인함' 처리하지 않아서
            # 다음 시간 실행 때 자동으로 재시도된다.
            print(f"[WARN] summarize failed for {v['url']}: {e}")
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
    up_to_date = (current.get("built_slot") or "").startswith(today) and (
        current.get("rules_version") == SCREENING_RULES_VERSION
    )

    if in_window:
        if up_to_date:
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
    data["intraday"] = kst.weekday() < 5 and (9, 0) <= (kst.hour, kst.minute) < (15, 30)
    save_json(SCREENING_FILE, data)
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
    transcript_text = call_with_timeout(get_transcript, TRANSCRIPT_TIMEOUT_SECONDS, latest["url"])
    if not transcript_text:
        print("[WARN] morning brief: 자막이 비어 있습니다")
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
        if update_screening(now):
            commit_and_push(f"chore: update screening {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] screening failed: {e}")

    print("[INFO] Pipeline run complete.")


if __name__ == "__main__":
    main()
