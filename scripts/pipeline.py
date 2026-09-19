"""메인 파이프라인: RSS 신규 영상 감지 -> 자막 추출 -> 요약 -> 저장.
GitHub Actions에서 1시간마다 실행된다 (.github/workflows/pipeline.yml 참고).
"""
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from youtube_check import fetch_channel_videos
from summarize import summarize_transcript
from transcript import get_transcript, is_retryable_error

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
CHANNELS_FILE = ROOT / "scripts" / "channels.json"
STATE_FILE = DATA_DIR / "state.json"
SUMMARIES_FILE = DATA_DIR / "summaries.json"
CROSS_FILE = DATA_DIR / "cross_mentions.json"
CHANNELS_OUT_FILE = DATA_DIR / "channels.json"
DAILY_PICKS_FILE = DATA_DIR / "daily_picks.json"

MAX_SUMMARIES = 500
RETENTION_DAYS = 7
STATE_HISTORY_PER_CHANNEL = 100
CROSS_WINDOW_HOURS = 48
DAILY_PICKS_WINDOW_HOURS = 24
MAX_PICKS_PER_SIDE = 6


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

    try:
        videos = fetch_channel_videos(cid)
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

        print(f"[INFO] New video: {name} - {v['title']}")

        try:
            transcript_text = get_transcript(v["url"])
        except Exception as e:
            print(f"[WARN] transcript failed for {v['url']}: {e}")
            if not is_retryable_error(e):
                state[cid].append(v["video_id"])  # 자막 자체가 없는 영상일 확률이 높아 재시도하지 않는다.
            # 요청 한도 초과/크레딧 부족 등 일시적 오류면 '확인함' 처리하지 않아 다음 시간에 재시도된다.
            continue

        if not transcript_text:
            print(f"[WARN] empty transcript for {v['url']}")
            state[cid].append(v["video_id"])
            continue

        try:
            result = summarize_transcript(name, v["title"], transcript_text)
        except Exception as e:
            # 요약 실패(예: 일시적인 API 요청 한도 초과)는 '확인함' 처리하지 않아서
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


def main():
    channels = load_json(CHANNELS_FILE, [])
    state = load_json(STATE_FILE, {})
    summaries = load_json(SUMMARIES_FILE, [])
    now = datetime.datetime.now(datetime.timezone.utc)

    for ch in channels:
        process_channel(ch, state, summaries, now)

    summaries.sort(key=lambda s: s.get("published", ""), reverse=True)
    summaries = [s for s in summaries if within_retention(s.get("published", ""), now)][:MAX_SUMMARIES]

    save_json(STATE_FILE, state)
    save_json(SUMMARIES_FILE, summaries)
    save_json(CROSS_FILE, build_cross_mentions(summaries))
    save_json(DAILY_PICKS_FILE, build_daily_picks(summaries))
    save_json(CHANNELS_OUT_FILE, channels)
    print("[INFO] Pipeline run complete.")


if __name__ == "__main__":
    main()
