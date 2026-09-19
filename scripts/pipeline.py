"""메인 파이프라인: RSS 신규 영상 감지 -> 자막 추출 -> 요약 -> 저장.
GitHub Actions에서 1시간마다 실행된다 (.github/workflows/pipeline.yml 참고).
"""
import datetime
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from youtube_check import fetch_channel_videos
from summarize import summarize_transcript
from transcript import get_transcript, is_retryable_error
from market_data import BRIEF_INDICES, fetch_market_brief, fetch_session_closes
import morning_brief as mb

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
CHANNELS_FILE = ROOT / "scripts" / "channels.json"
STATE_FILE = DATA_DIR / "state.json"
SUMMARIES_FILE = DATA_DIR / "summaries.json"
CROSS_FILE = DATA_DIR / "cross_mentions.json"
CHANNELS_OUT_FILE = DATA_DIR / "channels.json"
DAILY_PICKS_FILE = DATA_DIR / "daily_picks.json"
MARKET_BRIEF_FILE = DATA_DIR / "market_brief.json"
MORNING_BRIEF_FILE = DATA_DIR / "morning_brief.json"

MAX_SUMMARIES = 500
RETENTION_DAYS = 7
STATE_HISTORY_PER_CHANNEL = 100
CROSS_WINDOW_HOURS = 48
DAILY_PICKS_WINDOW_HOURS = 24
MAX_PICKS_PER_SIDE = 6
REQUEST_INTERVAL_SECONDS = 3  # 자막/AI API를 너무 빨리 연달아 호출해서 429(요청 한도 초과)에 걸리는 것을 막는다.
TRANSCRIPT_TIMEOUT_SECONDS = 90
SUMMARIZE_TIMEOUT_SECONDS = 300  # summarize.py의 재시도(최대 85초 대기)까지 포함해서 넉넉히 잡는다
MAX_VIDEO_DURATION_SECONDS = 3600  # 1시간 넘는 영상은 자막 생성 비용이 커서 아예 요약하지 않는다.
MIN_VIDEO_DURATION_SECONDS = 181  # 3분 이하는 쇼츠(Shorts)라 요약하지 않는다. 유튜브 쇼츠 최대 길이가 3분.


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


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
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


def build_cross_mentions(summaries):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=CROSS_WINDOW_HOURS)
    mention_map = {}

    for s in summaries:
        pub = parse_published(s.get("published", ""))
        if pub is None or pub < cutoff:
            continue
        for ticker in s.get("tickers", []):
            mention_map.setdefault(ticker, set()).add(s["channel"])

    cross = [
        {"ticker": ticker, "channels": sorted(chs), "count": len(chs)}
        for ticker, chs in mention_map.items()
        if len(chs) >= 2
    ]
    cross.sort(key=lambda x: x["count"], reverse=True)
    return cross


def _aggregate_picks(summaries, cutoff, field):
    sector_map = {}  # sector -> {"tickers": set, "channels": set}

    for s in summaries:
        pub = parse_published(s.get("published", ""))
        if pub is None or pub < cutoff:
            continue
        for pick in s.get(field, []) or []:
            sector = (pick.get("sector") or "").strip()
            if not sector:
                continue
            entry = sector_map.setdefault(sector, {"tickers": set(), "channels": set()})
            entry["tickers"].update(t for t in pick.get("tickers", []) if t)
            entry["channels"].add(s["channel"])

    items = [
        {
            "sector": sector,
            "tickers": sorted(v["tickers"]),
            "channels": sorted(v["channels"]),
            "count": len(v["channels"]),
        }
        for sector, v in sector_map.items()
    ]
    items.sort(key=lambda x: x["count"], reverse=True)
    return items[:MAX_PICKS_PER_SIDE]


def build_daily_picks(summaries):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=DAILY_PICKS_WINDOW_HOURS)
    return {
        "leading": _aggregate_picks(summaries, cutoff, "leading_picks"),
        "watch": _aggregate_picks(summaries, cutoff, "watch_picks"),
    }


def _save_data_files(state, summaries, channels, now):
    summaries.sort(key=lambda s: s.get("published", ""), reverse=True)
    trimmed = [s for s in summaries if within_retention(s.get("published", ""), now)][:MAX_SUMMARIES]

    save_json(STATE_FILE, state)
    save_json(SUMMARIES_FILE, trimmed)
    save_json(CROSS_FILE, build_cross_mentions(trimmed))
    save_json(DAILY_PICKS_FILE, build_daily_picks(trimmed))
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
    print(f"[INFO] morning brief: 지수 {len(report['indices'])}개를 시세 데이터로 교체 (거래일 {target_date})")


def update_morning_brief(now):
    """당잠사(한국경제TV) 최신 방송 1건만 분석해 아침 리포트를 만든다.
    이미 같은 영상으로 만들어둔 리포트가 있으면 아무것도 하지 않는다. True를 반환하면 저장된 것."""
    videos = fetch_channel_videos(mb.CHANNEL_ID, max_results=5, playlist_id=mb.PLAYLIST_ID)
    if not videos:
        print("[WARN] morning brief: 당잠사 재생목록이 비어 있습니다")
        return False

    latest = videos[0]
    current = load_json(MORNING_BRIEF_FILE, {})
    if current.get("video_id") == latest["video_id"]:
        return False  # 이미 최신 방송으로 만들어둔 리포트가 있다

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

    push = _git("push")
    if push.returncode != 0:
        print(f"[WARN] git push failed: {push.stderr.strip()}")
    else:
        print(f"[INFO] committed and pushed: {message}")


def main():
    channels = load_json(CHANNELS_FILE, [])
    state = load_json(STATE_FILE, {})
    summaries = load_json(SUMMARIES_FILE, [])
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
        market_brief = fetch_market_brief()
        market_brief["as_of"] = now.isoformat()
        save_json(MARKET_BRIEF_FILE, market_brief)
        commit_and_push(f"chore: update market brief {now.isoformat()}")
    except Exception as e:
        print(f"[WARN] market brief fetch failed: {e}")

    print("[INFO] Pipeline run complete.")


if __name__ == "__main__":
    main()
