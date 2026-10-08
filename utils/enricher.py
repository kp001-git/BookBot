import aiohttp
import asyncio
import logging
import urllib.parse
from typing import Optional, NamedTuple

logger = logging.getLogger(__name__)


class EnrichedMetadata(NamedTuple):
    cover_url: Optional[str] = None
    description: Optional[str] = None
    rating: Optional[float] = None
    genres: Optional[str] = None
    year: Optional[str] = None


def format_duration(seconds: Optional[int]) -> str:
    """Formats duration in seconds to HH:MM:SS or MM:SS."""
    if not seconds or seconds <= 0:
        return "N/A"
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_size(size_bytes: Optional[int]) -> str:
    """Formats file size in bytes to human-readable string (MB / GB)."""
    if not size_bytes or size_bytes <= 0:
        return "N/A"
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


async def fetch_google_books_metadata(title: str, author: str, session: aiohttp.ClientSession) -> Optional[EnrichedMetadata]:
    """Queries Google Books API for volume info."""
    try:
        query_parts = []
        if title and title != "Untitled":
            query_parts.append(f'intitle:"{title}"')
        if author and author != "Unknown Author":
            query_parts.append(f'inauthor:"{author}"')

        query = "+".join(query_parts) if query_parts else title
        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://www.googleapis.com/books/v1/volumes?q={encoded_query}&maxResults=1"

        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            items = data.get("items")
            if not items:
                return None

            volume_info = items[0].get("volumeInfo", {})

            # Cover image URL
            image_links = volume_info.get("imageLinks", {})
            cover_url = (
                image_links.get("extraLarge")
                or image_links.get("large")
                or image_links.get("medium")
                or image_links.get("small")
                or image_links.get("thumbnail")
            )
            if cover_url and cover_url.startswith("http://"):
                cover_url = cover_url.replace("http://", "https://")

            # Description
            description = volume_info.get("description")
            if description and len(description) > 600:
                description = description[:597].rstrip() + "..."

            # Rating
            rating = volume_info.get("averageRating")

            # Genres / Categories
            categories = volume_info.get("categories", [])
            genres = ", ".join(categories) if categories else None

            # Year
            pub_date = volume_info.get("publishedDate", "")
            year = pub_date[:4] if pub_date and len(pub_date) >= 4 else None

            return EnrichedMetadata(
                cover_url=cover_url,
                description=description,
                rating=float(rating) if rating else None,
                genres=genres,
                year=year
            )
    except Exception as e:
        logger.warning(f"Google Books API lookup failed for '{title}' by '{author}': {e}")
        return None


async def fetch_open_library_metadata(title: str, author: str, session: aiohttp.ClientSession) -> Optional[EnrichedMetadata]:
    """Fallback query to Open Library API."""
    try:
        params = {"title": title}
        if author and author != "Unknown Author":
            params["author"] = author

        url = f"https://openlibrary.org/search.json?{urllib.parse.urlencode(params)}&limit=1"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            docs = data.get("docs")
            if not docs:
                return None

            doc = docs[0]
            cover_i = doc.get("cover_i")
            cover_url = f"https://covers.openlibrary.org/b/id/{cover_i}-L.jpg" if cover_i else None

            rating = doc.get("ratings_average")
            subjects = doc.get("subject", [])
            genres = ", ".join(subjects[:3]) if subjects else None
            first_publish_year = doc.get("first_publish_year")
            year = str(first_publish_year) if first_publish_year else None

            return EnrichedMetadata(
                cover_url=cover_url,
                description=None,
                rating=round(float(rating), 1) if rating else None,
                genres=genres,
                year=year
            )
    except Exception as e:
        logger.warning(f"Open Library API lookup failed for '{title}' by '{author}': {e}")
        return None


async def enrich_book_metadata(title: str, author: str) -> EnrichedMetadata:
    """Orchestrates Google Books lookup with Open Library fallback."""
    headers = {
        "User-Agent": "TelegramAudiobookBot/1.0 (https://github.com/telegram-audiobook-bot)"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        # Try Google Books first
        meta = await fetch_google_books_metadata(title, author, session)
        if meta and (meta.cover_url or meta.description):
            return meta

        # Fallback to Open Library
        ol_meta = await fetch_open_library_metadata(title, author, session)
        if ol_meta:
            # Combine if partial
            if meta:
                return EnrichedMetadata(
                    cover_url=meta.cover_url or ol_meta.cover_url,
                    description=meta.description or ol_meta.description,
                    rating=meta.rating or ol_meta.rating,
                    genres=meta.genres or ol_meta.genres,
                    year=meta.year or ol_meta.year
                )
            return ol_meta

        return meta or EnrichedMetadata()
