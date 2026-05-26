"""Pydantic models for database entities."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class EpisodeStatus(str, Enum):
    # New: discovered by RSS poll, no one has asked for it yet. Worker ignores.
    NEW = "new"
    # Pending: user tapped → queued for worker.
    PENDING = "pending"
    DOWNLOADING = "downloading"
    TRANSCRIBING = "transcribing"
    CLASSIFYING = "classifying"
    EDITING = "editing"
    COMPLETED = "completed"
    FAILED = "failed"


class Feed(BaseModel):
    id: int | None = None
    name: str
    source_url: str
    slug: str
    enabled: bool = True
    poll_interval_minutes: int = 60
    last_polled_at: datetime | None = None
    image_url: str | None = None


class Episode(BaseModel):
    id: int | None = None
    feed_id: int
    guid: str
    title: str
    source_audio_url: str
    pub_date: datetime | None = None
    duration_seconds: int | None = None
    description: str = ""
    status: EpisodeStatus = EpisodeStatus.PENDING
    error_message: str | None = None
    retry_count: int = 0
    original_audio_path: str | None = None
    transcript_json_path: str | None = None
    ad_segments_json: str | None = None
    processed_audio_path: str | None = None
    clean_token: str | None = None
    claimed_at: datetime | None = None
    claimed_by: str | None = None
    claim_token: str | None = None
    source_identity: str | None = None
    is_active: bool = True
    publication_state: str = "placeholder"
    last_seen_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.now)


class ProcessingLog(BaseModel):
    id: int | None = None
    episode_id: int
    stage: str
    status: str
    message: str = ""
    duration_ms: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
