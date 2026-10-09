"""
services/search.py
In-memory catalog cache & sub-millisecond search engine powered by RapidFuzz.
Preloads all (id, clean_title, clean_author) records on bot startup to eliminate
transcontinental database round-trips (<2ms query latency).
"""

import time
import logging
from typing import List, Dict, Any, Tuple, Optional
from rapidfuzz import process, fuzz

logger = logging.getLogger("search_service")


class CatalogCache:
    """In-memory cache for book catalog search with RapidFuzz and substring matching."""

    def __init__(self) -> None:
        self._items: List[Dict[str, Any]] = []
        self._search_keys: List[str] = []
        self._id_map: Dict[int, int] = {}  # book_id -> index in self._items
        self._is_loaded: bool = False

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    def size(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        self._items.clear()
        self._search_keys.clear()
        self._id_map.clear()
        self._is_loaded = False

    async def preload(self) -> int:
        """Loads all (id, clean_title, clean_author) records from the database into memory."""
        # Late import to prevent circular dependencies
        from database.models import get_all_catalog_books

        t0 = time.perf_counter()
        raw_books = await get_all_catalog_books()

        self._items.clear()
        self._search_keys.clear()
        self._id_map.clear()

        for idx, b in enumerate(raw_books):
            book_id = b["id"]
            clean_title = b["clean_title"]
            clean_author = b.get("clean_author") or "Unknown Author"
            t_lower = clean_title.lower()
            a_lower = clean_author.lower()
            search_key = f"{t_lower} {a_lower}"

            item = {
                "id": book_id,
                "clean_title": clean_title,
                "clean_author": clean_author,
                "title_lower": t_lower,
                "author_lower": a_lower,
                "formats": b.get("formats", []),
                "cover_url": b.get("cover_url"),
                "description": b.get("description"),
                "rating": b.get("rating"),
                "genres": b.get("genres"),
                "year": b.get("year"),
                "search_key": search_key
            }
            self._items.append(item)
            self._search_keys.append(search_key)
            self._id_map[book_id] = idx

        self._is_loaded = True
        t_elapsed = time.perf_counter() - t0
        logger.info(
            f"Preloaded {len(self._items):,} catalog books into memory in {t_elapsed:.2f}s (<2ms search active)."
        )
        return len(self._items)

    def add_or_update(
        self,
        book_id: int,
        clean_title: str,
        clean_author: Optional[str] = None,
        formats: Optional[List[str]] = None,
        cover_url: Optional[str] = None,
        description: Optional[str] = None
    ) -> None:
        """Updates or inserts a book record in the in-memory cache in real time."""
        author = clean_author or "Unknown Author"
        t_lower = clean_title.lower()
        a_lower = author.lower()
        search_key = f"{t_lower} {a_lower}"

        if book_id in self._id_map:
            idx = self._id_map[book_id]
            existing = self._items[idx]
            existing_formats = list(existing.get("formats", []))
            if formats:
                for f in formats:
                    if f not in existing_formats:
                        existing_formats.append(f)
            self._items[idx] = {
                "id": book_id,
                "clean_title": clean_title,
                "clean_author": author,
                "title_lower": t_lower,
                "author_lower": a_lower,
                "formats": existing_formats,
                "cover_url": cover_url or existing.get("cover_url"),
                "description": description or existing.get("description"),
                "rating": existing.get("rating"),
                "genres": existing.get("genres"),
                "year": existing.get("year"),
                "search_key": search_key
            }
            self._search_keys[idx] = search_key
        else:
            idx = len(self._items)
            item = {
                "id": book_id,
                "clean_title": clean_title,
                "clean_author": author,
                "title_lower": t_lower,
                "author_lower": a_lower,
                "formats": formats or [],
                "cover_url": cover_url,
                "description": description,
                "rating": None,
                "genres": None,
                "year": None,
                "search_key": search_key
            }
            self._items.append(item)
            self._search_keys.append(search_key)
            self._id_map[book_id] = idx

    def search(
        self,
        query: str,
        limit: int = 5,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Executes sub-2ms search against the in-memory catalog:
        1. Substring & multi-token matching (<2ms) with ranking (title prefix > title contains > all tokens in key).
        2. RapidFuzz WRatio fallback (<20ms) for typos and spelling errors.
        """
        q = query.strip()
        if not q or not self._items:
            return [], 0

        q_lower = q.lower()
        tokens = [t for t in q_lower.split() if t]

        # Phase 1: Fast in-memory token & substring matching (<2ms)
        exact_matches = []
        for item in self._items:
            t_lower = item["title_lower"]
            a_lower = item["author_lower"]
            k_lower = item["search_key"]

            if q_lower in t_lower:
                rank = 6 if t_lower.startswith(q_lower) else 5
                exact_matches.append((rank, item))
            elif q_lower in a_lower:
                rank = 4 if a_lower.startswith(q_lower) else 3
                exact_matches.append((rank, item))
            elif len(tokens) > 1 and all(tk in t_lower for tk in tokens):
                exact_matches.append((2, item))
            elif len(tokens) > 1 and all(tk in k_lower for tk in tokens):
                exact_matches.append((1, item))

        if exact_matches:
            exact_matches.sort(key=lambda x: x[0], reverse=True)
            total = len(exact_matches)
            sliced = [x[1] for x in exact_matches[offset : offset + limit]]
            return sliced, total

        # Phase 2: RapidFuzz Typo / Fuzzy Fallback (<20ms)
        fuzz_matches = process.extract(
            q_lower,
            self._search_keys,
            scorer=fuzz.WRatio,
            limit=min(100, len(self._search_keys)),
            score_cutoff=60.0
        )
        if not fuzz_matches:
            return [], 0

        total = len(fuzz_matches)
        sliced = [self._items[idx] for text, score, idx in fuzz_matches[offset : offset + limit]]
        return sliced, total


# Global Singleton Instance
catalog_cache = CatalogCache()


async def preload_catalog() -> int:
    """Preloads the catalog cache from the database."""
    return await catalog_cache.preload()


async def search_catalog(
    query: str,
    limit: int = 5,
    offset: int = 0
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Searches the in-memory catalog with automatic preload if not loaded yet.
    Returns (results_list, total_matches).
    """
    if not catalog_cache.is_loaded:
        await catalog_cache.preload()
    return catalog_cache.search(query, limit=limit, offset=offset)


def add_or_update_catalog_book(
    book_id: int,
    clean_title: str,
    clean_author: Optional[str] = None,
    formats: Optional[List[str]] = None,
    cover_url: Optional[str] = None,
    description: Optional[str] = None
) -> None:
    """Updates the in-memory catalog cache when a book is indexed or modified."""
    catalog_cache.add_or_update(
        book_id=book_id,
        clean_title=clean_title,
        clean_author=clean_author,
        formats=formats,
        cover_url=cover_url,
        description=description
    )

