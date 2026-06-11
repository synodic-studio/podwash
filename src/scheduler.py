"""APScheduler-based background tasks for the Vultr server.

Scope is intentionally thin:
  1. poll_feeds — discover new episodes (inserted as 'pending')
  2. cleanup_old — delete processed audio past retention period

All heavy processing (download, transcribe, classify, cut) happens on the
Mac worker, which claims jobs via /api/jobs/next. Nothing in this file
touches Whisper, anthropic, or ffmpeg.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from src.alerting import send_alert
from src.config import Settings
from src.database import queries
from src.database.models import EpisodeStatus
from src.feeds.fetcher import fetch_public_feed_sync
from src.feeds.parser import (
    extract_feed_image_from_content,
    parse_feed,
    parse_feed_content,
)
from src.safe_paths import UnsafeRelativePath, resolve_under_data_dir

# ``parse_feed`` is re-exported for compatibility but the scheduler
# itself must always go through the SSRF-safe fetcher.
__all__ = ["start_scheduler", "parse_feed", "parse_feed_content"]

# Watchdog thresholds. The watchdog is the safety net for failures the
# worker-side preflight + crash wrapper miss (Vultr can't see worker logs).
_WATCHDOG_INTERVAL_MIN = 5
_NO_RECENT_CLAIM_MIN = 30  # "worker is silent" if no claim in this window
_FAILED_BURST_THRESHOLD = 5  # alert if this many episodes failed in last 24h
_FEED_POLL_FAIL_THRESHOLD = 3  # alert after this many consecutive feed errors
DEFAULT_MAX_EPISODES_PER_FEED = 100

# In-memory consecutive-failure counter per feed_id, scoped to the
# scheduler process. We don't persist it — restarts forgive a feed and
# require it to fail _FEED_POLL_FAIL_THRESHOLD more times before alerting
# again, which is exactly the behaviour we want.
_feed_failure_counts: dict[int, int] = {}


def start_scheduler(
    conn: sqlite3.Connection, settings: Settings
) -> BackgroundScheduler:
    """Start the background scheduler with poll + cleanup + watchdog jobs."""
    scheduler = BackgroundScheduler(daemon=True)

    scheduler.add_job(
        _poll_feeds,
        "interval",
        minutes=15,
        args=[conn, settings],
        id="poll_feeds",
        next_run_time=datetime.now(),  # Run immediately on startup
    )

    scheduler.add_job(
        _cleanup_old,
        "interval",
        hours=6,
        args=[conn, settings],
        id="cleanup_old",
    )

    scheduler.add_job(
        _watchdog,
        "interval",
        minutes=_WATCHDOG_INTERVAL_MIN,
        args=[conn, settings],
        id="watchdog",
        # Stagger the first run so we don't alert during the API's
        # warm-up before the worker has had a chance to claim.
        next_run_time=datetime.now() + timedelta(minutes=_WATCHDOG_INTERVAL_MIN),
    )

    scheduler.start()
    print("[scheduler] Started background scheduler (poll + cleanup + watchdog)")
    return scheduler


def _watchdog(conn: sqlite3.Connection, settings: Settings) -> None:
    """Detect failure modes the worker can't tell us about itself.

    Two conditions trigger an alert:
      1. There is at least one row in 'pending' AND the most recent claim
         across the whole table is older than _NO_RECENT_CLAIM_MIN — i.e.
         users want episodes processed and no worker is pulling.
      2. There is at least one row claimed but stuck (existing stale-claim
         sweep handles the rollback; the watchdog just makes it audible).

    Each condition is throttled (1/hour by default in send_alert).
    """
    snapshot = queries.queue_health_snapshot(conn)
    pending = snapshot["pending_count"]
    last_claim_at = snapshot["last_claim_at"]

    # Condition 1: pending backlog with no recent worker activity.
    if pending > 0:
        silent_for_minutes: float | None = None
        if last_claim_at is None:
            silent_for_minutes = float("inf")
        else:
            try:
                last_dt = datetime.fromisoformat(last_claim_at)
                silent_for_minutes = (datetime.now() - last_dt).total_seconds() / 60
            except ValueError:
                silent_for_minutes = None

        if (
            silent_for_minutes is not None
            and silent_for_minutes >= _NO_RECENT_CLAIM_MIN
        ):
            if silent_for_minutes == float("inf"):
                silent_str = "never seen a claim"
                quiet_phrase = "Mac worker has never claimed work."
            else:
                silent_str = f"{silent_for_minutes:.0f} min"
                quiet_phrase = f"Mac worker quiet for {silent_str}."
            # This is now a backstop. The Mac runs its own idle
            # watchdog every 5 min and self-heals; if we're firing,
            # both the worker AND that watchdog have failed to act —
            # which almost always means the Mac is offline / asleep /
            # off Tailscale. Keep the copy oriented on that.
            send_alert(
                subsystem="server-watchdog",
                kind="worker silent with backlog",
                problem=(
                    f"{quiet_phrase} {pending} episode(s) waiting "
                    "and the Mac's own watchdog hasn't healed it either."
                ),
                fix="The Mac is likely offline. Wake it up.",
                context={
                    "pending": pending,
                    "silent_for": silent_str,
                },
            )

    # Condition 2: stuck in-flight claims. The reset itself IS the
    # repair — rows go back to 'pending' and will be retried on the
    # next claim. Silent recovery; do not alert. If retries can't
    # actually finish the work, Condition 3 (failed-episode burst)
    # catches it. If the worker is gone for good, Condition 1
    # (worker silent with backlog) catches it.
    queries.reset_stale_claims(conn, settings.worker.stale_minutes)

    # Condition 3: terminal-failure burst. Episodes that hit retry-cap
    # land in 'failed' and stay there. Without this alert a quietly
    # broken pipeline (bad prompt, ffmpeg edge case, model regression)
    # could rot every episode for days unnoticed.
    failed_24h = queries.count_recent_failures(conn, hours=24)
    if failed_24h >= _FAILED_BURST_THRESHOLD:
        send_alert(
            subsystem="server-watchdog",
            kind="failed-episode burst",
            problem=(
                f"{failed_24h} episode(s) hit terminal 'failed' status in "
                f"the last 24h (threshold: {_FAILED_BURST_THRESHOLD}). The "
                "pipeline is silently dropping work."
            ),
            fix=(
                "Look at the most recent failures' error_message column "
                "to see the common cause. Likely culprits: Anthropic "
                "prompt regression, ffmpeg version drift, source CDNs "
                "blocking us."
            ),
            context={"failed_24h": failed_24h},
        )


def _poll_feeds(conn: sqlite3.Connection, settings: Settings) -> None:
    """Check each enabled feed for new episodes. New rows start as 'pending'."""
    feeds = queries.get_all_feeds(conn, enabled_only=True)
    for feed in feeds:
        if feed.last_polled_at:
            last = (
                datetime.fromisoformat(feed.last_polled_at)
                if isinstance(feed.last_polled_at, str)
                else feed.last_polled_at
            )
            if datetime.now() - last < timedelta(minutes=feed.poll_interval_minutes):
                continue

        max_episodes = DEFAULT_MAX_EPISODES_PER_FEED
        title_includes: list[str] = []
        for fc in settings.feeds:
            if fc.slug == feed.slug:
                max_episodes = fc.max_episodes or DEFAULT_MAX_EPISODES_PER_FEED
                title_includes = fc.title_includes
                break

        print(f"[scheduler] Polling feed: {feed.name}")
        try:
            content = fetch_public_feed_sync(feed.source_url)
            episodes = parse_feed_content(
                content,
                feed.id,
                max_episodes=max_episodes,
                title_includes=title_includes,
            )
            new_count = 0
            seen_at = datetime.now()
            active_ids: list[int] = []
            for episode in episodes:
                ep_id, inserted = queries.upsert_episode_from_poll(
                    conn, episode, seen_at=seen_at
                )
                active_ids.append(ep_id)
                if inserted:
                    new_count += 1
            # Only prune visibility when we actually got episodes back —
            # an empty parse is suspicious enough to leave existing rows
            # alone rather than blank the whole feed.
            if active_ids:
                queries.mark_feed_poll_visibility(
                    conn, feed.id, active_ids, seen_at=seen_at
                )
            if not feed.image_url:
                image_url = extract_feed_image_from_content(content)
                if image_url:
                    queries.update_feed_image(conn, feed.id, image_url)
            queries.update_feed_polled(conn, feed.id)
            if new_count:
                print(
                    f"[scheduler] Found {new_count} new episodes in {feed.name} "
                    f"(queued for worker)"
                )
            # Success forgives any prior failure streak.
            _feed_failure_counts.pop(feed.id, None)
        except Exception as e:
            count = _feed_failure_counts.get(feed.id, 0) + 1
            _feed_failure_counts[feed.id] = count
            print(f"[scheduler] Error polling {feed.name} ({count} consecutive): {e}")
            if count == _FEED_POLL_FAIL_THRESHOLD:
                send_alert(
                    subsystem="server-feed-poll",
                    kind="feed persistently failing",
                    problem=(
                        f"RSS feed '{feed.name}' has failed {count} polls "
                        f"in a row. New episodes from this feed will not "
                        f"appear in the proxy until it succeeds."
                    ),
                    fix=(
                        "Open the source URL in a browser and confirm it "
                        "still serves valid RSS. Common causes: feed "
                        "moved, host blocking us, podcast was removed. "
                        "Update or disable the feed in the database."
                    ),
                    context={
                        "feed_id": feed.id,
                        "feed_name": feed.name,
                        "source_url": feed.source_url,
                        "error": f"{type(e).__name__}: {e}",
                    },
                )


def _cleanup_old(conn: sqlite3.Connection, settings: Settings) -> None:
    """Delete processed audio files older than retention period.

    Resets row to 'new' with processed_audio_path cleared. Feed goes back
    to showing the pre-completion GUID with the placeholder audio; if the
    user taps it, the audio route flips it to 'pending' and the worker
    re-processes. On-demand only — no automatic re-processing.
    """
    data_dir = Path(settings.data_dir)
    cutoff = datetime.now() - timedelta(days=settings.processing.retention_days)

    # Cleanup keys off completed_at (when the row finished) rather than
    # created_at (when the row was first discovered). A recently-
    # completed episode that was discovered months ago should not be
    # pruned. Older rows missing completed_at fall back to created_at
    # so legacy DBs still tidy up.
    rows = conn.execute(
        """SELECT id, processed_audio_path FROM episodes
        WHERE status = 'completed'
          AND processed_audio_path IS NOT NULL
          AND COALESCE(completed_at, created_at) < ?""",
        (cutoff.isoformat(),),
    ).fetchall()

    for row in rows:
        ep_id, path_str = row["id"], row["processed_audio_path"]
        try:
            file_path = resolve_under_data_dir(data_dir, path_str)
        except UnsafeRelativePath:
            send_alert(
                subsystem="server-cleanup",
                kind="unsafe processed path",
                problem=(
                    f"Refusing to unlink episode {ep_id} — stored path "
                    f"{path_str!r} escapes data dir."
                ),
                fix="Inspect the DB row before deleting anything.",
                context={"episode_id": ep_id, "stored_path": path_str},
            )
            continue
        try:
            if file_path.exists():
                file_path.unlink()
                print(f"[cleanup] Deleted old processed file for episode {ep_id}")
            # Clear clean_token so the stale /audio/clean/{token}.mp3
            # URL stops resolving once we've removed the file; reset
            # the row to NEW with placeholder publication state.
            conn.execute(
                """UPDATE episodes
                SET processed_audio_path = NULL,
                    clean_token = NULL,
                    status = ?,
                    publication_state = 'placeholder',
                    completed_at = NULL
                WHERE id = ?""",
                (EpisodeStatus.NEW.value, ep_id),
            )
            conn.commit()
        except OSError as e:
            print(f"[cleanup] Error deleting file for episode {ep_id}: {e}")
