"""유튜브 영상 자막(트랜스크립트)을 가져온다.
Supadata API(유료)만 사용한다. 무료 라이브러리(youtube-transcript-api)는 GitHub Actions
서버에서 응답 없이 멈추는 등 신뢰성 문제가 있어서 현재 비활성화해뒀다 (_get_transcript_free 참고,
필요하면 나중에 다시 켤 수 있음). https://docs.supadata.ai 참고.
"""
import os
import re
import time

import requests

API_URL = "https://api.supadata.ai/v1/transcript"

RETRYABLE_STATUS_CODES = {429, 402, 500, 502, 503, 504}


def is_retryable_error(exc):
    """True면 일시적 오류(요청 한도 초과, 크레딧 부족, 서버 오류 등)로 보고 다음 실행 때 재시도해야 한다."""
    response = getattr(exc, "response", None)
    if response is not None:
        return response.status_code in RETRYABLE_STATUS_CODES
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, TimeoutError))


def _video_id_from_url(video_url):
    match = re.search(r"[?&]v=([\w-]{11})", video_url)
    if match:
        return match.group(1)
    match = re.search(r"youtu\.be/([\w-]{11})", video_url)
    if match:
        return match.group(1)
    raise ValueError(f"URL에서 영상 ID를 찾을 수 없습니다: {video_url}")


def _get_transcript_free(video_id):
    """무료 라이브러리로 시도. 실패하면 예외를 던진다 (호출부에서 Supadata로 폴백).
    youtube-transcript-api 1.0+ 는 인스턴스 기반 API(.fetch)로 바뀌었고, 구버전은
    클래스 메서드(.get_transcript)를 쓰므로 설치된 버전에 관계없이 동작하도록 둘 다 시도한다.
    """
    from youtube_transcript_api import YouTubeTranscriptApi

    segments = None
    try:
        api = YouTubeTranscriptApi()
        segments = api.fetch(video_id, languages=["ko", "en"])
    except AttributeError:
        pass

    if segments is None:
        segments = YouTubeTranscriptApi.get_transcript(video_id, languages=["ko", "en"])

    parts = []
    for seg in segments:
        text = getattr(seg, "text", None)
        if text is None and isinstance(seg, dict):
            text = seg.get("text")
        if text:
            parts.append(text)

    text = " ".join(parts)
    if not text.strip():
        raise RuntimeError("free transcript came back empty")
    return text


def _api_key():
    key = os.environ.get("SUPADATA_API_KEY")
    if not key:
        raise RuntimeError("SUPADATA_API_KEY 환경변수가 설정되어 있지 않습니다.")
    return key


def _get_transcript_supadata(video_url, max_wait_seconds=120):
    resp = requests.get(
        API_URL,
        headers={"x-api-key": _api_key()},
        params={"url": video_url, "text": "true", "mode": "auto"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    if "jobId" in data:
        return _poll_job(data["jobId"], max_wait_seconds)

    return data.get("content", "")


def _poll_job(job_id, max_wait_seconds):
    url = f"{API_URL}/{job_id}"
    waited = 0
    while waited < max_wait_seconds:
        time.sleep(2)
        waited += 2
        resp = requests.get(url, headers={"x-api-key": _api_key()}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status == "completed":
            return data.get("content", "")
        if status == "failed":
            raise RuntimeError(f"트랜스크립트 작업 실패: {data.get('error')}")
    raise TimeoutError(f"트랜스크립트 작업이 {max_wait_seconds}초 내에 끝나지 않았습니다 (job_id={job_id})")


def get_transcript(video_url, max_wait_seconds=120):
    return _get_transcript_supadata(video_url, max_wait_seconds=max_wait_seconds)
