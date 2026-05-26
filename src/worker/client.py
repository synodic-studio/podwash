"""HTTP client for the Vultr queue API."""

from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass
class Job:
    """An episode claimed from the server, ready to process."""

    episode_id: int
    feed_id: int
    guid: str
    title: str
    source_audio_url: str
    duration_seconds: int | None
    claim_token: str


class QueueClient:
    """Thin wrapper around the /api/jobs/* endpoints."""

    def __init__(
        self, base_url: str, token: str, worker_id: str, timeout: float = 60.0
    ):
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Worker-Id": worker_id,
            },
        )

    def close(self) -> None:
        self._http.close()

    def claim_next(self) -> Job | None:
        """Claim the oldest pending episode. Returns None when the queue is empty."""
        r = self._http.get("/api/jobs/next")
        if r.status_code == 204:
            return None
        r.raise_for_status()
        data = r.json()
        claim_token = data.get("claim_token")
        if not claim_token:
            raise RuntimeError(
                "queue handed out a claim without a claim_token; "
                "the server is out of date"
            )
        return Job(
            episode_id=data["episode_id"],
            feed_id=data["feed_id"],
            guid=data["guid"],
            title=data["title"],
            source_audio_url=data["source_audio_url"],
            duration_seconds=data.get("duration_seconds"),
            claim_token=claim_token,
        )

    def submit_result(
        self,
        episode_id: int,
        processed_audio: Path,
        ad_segments_json: str | None,
        *,
        claim_token: str,
    ) -> dict:
        """Upload the cleaned MP3 + classifier metadata. Uses a long timeout for big files."""
        with processed_audio.open("rb") as f:
            r = self._http.post(
                f"/api/jobs/{episode_id}/result",
                files={"audio": (processed_audio.name, f, "audio/mpeg")},
                data={"ad_segments_json": ad_segments_json or ""},
                headers={"X-Claim-Token": claim_token},
                timeout=300.0,
            )
        r.raise_for_status()
        return r.json()

    def submit_failure(
        self, episode_id: int, error: str, *, claim_token: str
    ) -> dict:
        """Report a pipeline failure for retry bookkeeping."""
        r = self._http.post(
            f"/api/jobs/{episode_id}/fail",
            json={"error": error[:500]},
            headers={"X-Claim-Token": claim_token},
        )
        r.raise_for_status()
        return r.json()
