"""Shared data types for scraper tools."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScrapedComment:
    """Structured comment data from any scraper source."""
    text: str
    url: str
    subreddit: str  # or "source" for non-Reddit scrapers
    post_title: str
