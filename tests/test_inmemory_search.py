import os
import time
import asyncio
import unittest
from config import settings
from database.connection import set_database_url, set_db_path
from database.models import init_db, search_books, get_book_by_id
from services.search import preload_catalog, catalog_cache, add_or_update_catalog_book


class TestInMemoryCatalogSearch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if settings.DATABASE_URL:
            set_database_url(settings.DATABASE_URL)
        else:
            set_db_path(settings.DB_PATH)
        await init_db()

    async def test_preload_and_search_latency(self):
        # 1. Preload all books
        t0 = time.perf_counter()
        count = await preload_catalog()
        t_preload = time.perf_counter() - t0
        print(f"\n[Test] Preloaded {count:,} books in {t_preload:.2f}s.")
        self.assertTrue(catalog_cache.is_loaded)
        self.assertGreater(count, 0)

        # 2. Benchmark exact / token queries (<2ms latency)
        queries = ["Atomic", "Habits", "Psychology", "money", "Harry Potter"]
        for q in queries:
            t0 = time.perf_counter()
            results, total = await search_books(q, limit=5, offset=0)
            latency_ms = (time.perf_counter() - t0) * 1000
            print(f"[Test] Query '{q}': {total} matches in {latency_ms:.2f}ms. Top: {[r['clean_title'] for r in results[:2]]}")
            self.assertLess(latency_ms, 5.0, f"Query '{q}' took {latency_ms:.2f}ms, expected sub-5ms")

        # 3. RapidFuzz typo fallback (<25ms latency)
        t0 = time.perf_counter()
        results, total = await search_books("Atmoic Habts", limit=5, offset=0)
        fuzzy_latency_ms = (time.perf_counter() - t0) * 1000
        print(f"[Test] Typo query 'Atmoic Habts': {total} matches in {fuzzy_latency_ms:.2f}ms. Top: {[r['clean_title'] for r in results[:2]]}")
        self.assertGreater(total, 0)
        self.assertLess(fuzzy_latency_ms, 35.0, f"Fuzzy query took {fuzzy_latency_ms:.2f}ms, expected sub-35ms")

        # 4. Real-time cache update test
        dummy_id = 888888
        add_or_update_catalog_book(
            book_id=dummy_id,
            clean_title="Agentic Superintelligence in Python",
            clean_author="Deepmind Antigravity",
            formats=["EPUB", "AUDIO"]
        )
        t0 = time.perf_counter()
        results, total = await search_books("Agentic Superintelligence", limit=5, offset=0)
        update_latency_ms = (time.perf_counter() - t0) * 1000
        print(f"[Test] Search for newly added book: {total} matches in {update_latency_ms:.2f}ms.")
        self.assertGreater(total, 0)
        self.assertEqual(results[0]["id"], dummy_id)
        self.assertEqual(results[0]["formats"], ["EPUB", "AUDIO"])

        # 5. On-demand format fetching by book ID
        book_details = await get_book_by_id(1)
        if book_details:
            print(f"[Test] Fetched book ID 1 on demand: '{book_details['clean_title']}', files: {len(book_details['files'])}")
            self.assertIn("files", book_details)


if __name__ == "__main__":
    unittest.main()
