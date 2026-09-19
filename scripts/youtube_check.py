"""YouTube Data API v3로 채널의 최신 업로드 영상 목록(+ 길이)을 가져온다.
(예전에는 RSS 피드를 썼지만 유튜브가 /feeds/videos.xml 엔드포인트를 없애서 이 방식으로 교체함)
https://console.cloud.google.com 에서 카드 등록 없이 무료로 키를 받을 수 있다 (YOUTUBE_API_KEY).
"""
import os
import re

import requests

PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _api_key():
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY 환경변수가 설정되어 있지 않습니다.")
    return key


def _uploads_playlist_id(channel_id):
    # 채널의 "업로드 전체" 재생목록 ID는 채널ID의 "UC" 접두어를 "UU"로 바꾼 값과 같다 (유튜브 관례).
    if not channel_id.startswith("UC"):
        raise ValueError(f"예상치 못한 channel_id 형식: {channel_id}")
    return "UU" + channel_id[2:]


def _parse_duration_seconds(duration_str):
    if not duration_str:
        return None
    match = _DURATION_RE.match(duration_str)
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _fetch_durations(video_ids):
    """영상 ID 목록의 길이(초)를 {video_id: 초} 형태로 반환한다. 최대 50개씩 묶어서 조회."""
    durations = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = requests.get(
            VIDEOS_URL,
            params={"part": "contentDetails", "id": ",".join(batch), "key": _api_key()},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"[WARN] duration fetch failed {resp.status_code}: {resp.text[:200]}")
            continue
        for item in resp.json().get("items", []):
            video_id = item.get("id")
            seconds = _parse_duration_seconds(item.get("contentDetails", {}).get("duration"))
            if video_id and seconds is not None:
                durations[video_id] = seconds
    return durations


def fetch_channel_videos(channel_id, max_results=50):
    playlist_id = _uploads_playlist_id(channel_id)
    resp = requests.get(
        PLAYLIST_ITEMS_URL,
        params={
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": max_results,
            "key": _api_key(),
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"YouTube API 오류 {resp.status_code} (channel_id={channel_id}): {resp.text[:300]}")

    data = resp.json()
    videos = []
    for item in data.get("items", []):
        try:
            snippet = item["snippet"]
            video_id = snippet["resourceId"]["videoId"]
        except (KeyError, TypeError):
            # 비공개/삭제된 영상 등 항목 하나가 이상해도 채널 전체 조회를 실패시키지 않는다.
            print(f"[WARN] skipping malformed playlist item for channel_id={channel_id}: {item}")
            continue
        videos.append(
            {
                "video_id": video_id,
                "title": snippet.get("title", ""),
                "published": snippet.get("publishedAt", ""),
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )

    try:
        durations = _fetch_durations([v["video_id"] for v in videos])
        for v in videos:
            v["duration_seconds"] = durations.get(v["video_id"])
    except Exception as e:
        print(f"[WARN] duration fetch failed for channel_id={channel_id}: {e}")

    return videos
