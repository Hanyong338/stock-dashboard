"""Supadata API로 유튜브 영상 자막(트랜스크립트)을 가져온다.
https://docs.supadata.ai 참고. 다른 트랜스크립트 API로 바꾸고 싶으면 이 파일만 수정하면 된다.
"""
import os
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


def _api_key():
    key = os.environ.get("SUPADATA_API_KEY")
    if not key:
        raise RuntimeError("SUPADATA_API_KEY 환경변수가 설정되어 있지 않습니다.")
    return key


def get_transcript(video_url, max_wait_seconds=120):
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
