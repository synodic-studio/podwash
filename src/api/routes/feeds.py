"""RSS feed serving routes."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from src.database import queries
from src.feeds.generator import generate_feed_xml

router = APIRouter()


@router.get("/feeds/{slug}.xml")
async def get_feed(slug: str, request: Request):
    """Serve a proxy RSS feed by slug."""
    conn = request.app.state.db
    settings = request.app.state.settings

    feed = queries.get_feed_by_slug(conn, slug)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    episodes = queries.get_episodes_for_feed(conn, feed.id)
    xml = generate_feed_xml(feed, episodes, settings.base_url)

    return Response(
        content=xml,
        media_type="application/rss+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )
