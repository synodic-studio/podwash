"""FastAPI application factory."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

from src.config import Settings
from src.database import queries
from src.database.db import init_db
from src.database.models import Feed
from src.scheduler import start_scheduler

from .routes import audio, feeds, jobs, management

STATIC_DIR = Path(__file__).parent.parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        from src.config import load_settings

        settings = load_settings()

    # Initialize database
    conn = init_db(settings.data_dir)

    # Sync configured feeds to database
    _sync_feeds(conn, settings)

    # Crash recovery: any episode left in an in-flight state (e.g. from a
    # prior OOM kill) is orphaned — reset to pending so it retries.
    orphaned = queries.reset_orphaned_in_flight(conn)
    if orphaned:
        print(f"[startup] Reset {orphaned} orphaned in-flight episodes to pending")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: launch background scheduler
        scheduler = start_scheduler(conn, settings)
        app.state.scheduler = scheduler
        yield
        # Shutdown: stop scheduler
        scheduler.shutdown(wait=False)

    app = FastAPI(
        title="podwash",
        description="Self-hosted podcast ad-skipping proxy",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.db = conn
    app.state.settings = settings

    # Routes
    app.include_router(feeds.router)
    app.include_router(audio.router)
    app.include_router(management.router)
    app.include_router(jobs.router)

    @app.get("/")
    async def root():
        return RedirectResponse(url="/submit")

    @app.get("/submit")
    async def submit_page():
        return FileResponse(STATIC_DIR / "submit.html")

    @app.get("/health")
    async def health():
        return {"status": "healthy", "version": "0.1.0"}

    return app


def _sync_feeds(conn, settings: Settings) -> None:
    """Ensure all feeds from config.yml exist in the database."""
    for feed_cfg in settings.feeds:
        queries.upsert_feed(
            conn,
            Feed(
                name=feed_cfg.name,
                source_url=feed_cfg.url,
                slug=feed_cfg.slug,
                poll_interval_minutes=feed_cfg.poll_interval_minutes,
            ),
        )
