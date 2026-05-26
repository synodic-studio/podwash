"""Slug fallback for emoji/punctuation-only titles."""

from src.api.routes.management import _slug_for, _slugify


def test_slugify_can_produce_empty():
    assert _slugify("🎙️🎙️") == ""


def test_slug_for_falls_back_to_hostname():
    assert _slug_for("🎙️", "https://feeds.example.com/show.xml") == "feedsexamplecom"


def test_slug_for_falls_back_to_constant_when_nothing_works():
    assert _slug_for("", "") == "feed"
