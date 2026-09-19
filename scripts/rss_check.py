"""유튜브 채널 RSS 피드에서 최신 영상 목록을 가져온다."""
import feedparser

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def _extract_video_id(entry):
    video_id = entry.get("yt_videoid")
    if video_id:
        return video_id
    entry_id = entry.get("id", "")
    if "video:" in entry_id:
        return entry_id.split("video:")[-1]
    link = entry.get("link", "")
    if "v=" in link:
        return link.split("v=")[-1].split("&")[0]
    raise ValueError(f"영상 ID를 찾을 수 없습니다: {entry}")


def fetch_channel_videos(channel_id):
    feed = feedparser.parse(RSS_URL.format(channel_id=channel_id))
    if getattr(feed, "bozo", 0) and not feed.entries:
        raise RuntimeError(f"RSS 파싱 실패 (channel_id={channel_id}): {feed.bozo_exception}")

    videos = []
    for entry in feed.entries:
        videos.append(
            {
                "video_id": _extract_video_id(entry),
                "title": entry.get("title", ""),
                "published": entry.get("published", ""),
                "url": entry.get("link", ""),
            }
        )
    return videos
