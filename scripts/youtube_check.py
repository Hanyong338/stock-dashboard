"""YouTube Data API v3로 채널의 최신 업로드 영상 목록을 가져온다.
(예전에는 RSS 피드를 썼지만 유튜브가 /feeds/videos.xml 엔드포인트를 없애서 이 방식으로 교체함)
https://console.cloud.google.com 에서 카드 등록 없이 무료로 키를 받을 수 있다 (YOUTUBE_API_KEY).
"""
import os

import requests

API_URL = "https://www.googleapis.com/youtube/v3/playlistItems"


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


def fetch_channel_videos(channel_id, max_results=50):
    playlist_id = _uploads_playlist_id(channel_id)
    resp = requests.get(
        API_URL,
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
        snippet = item["snippet"]
        video_id = snippet["resourceId"]["videoId"]
        videos.append(
            {
                "video_id": video_id,
                "title": snippet.get("title", ""),
                "published": snippet.get("publishedAt", ""),
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return videos
