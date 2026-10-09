# BookBot - Telegram Digital Library & Audiobook Indexer

A high-performance, asynchronous Telegram bot built with **Python 3.11+**, **aiogram 3.x**, and **Dual-Mode Database Persistence** (**PostgreSQL** via **asyncpg** or **SQLite FTS5** via **aiosqlite** with automated cloud backups).

The bot automatically indexes multimedia files (Audiobooks, EPUBs, PDFs, ZIP archives) forwarded or posted into a designated storage channel, sanitizes messy metadata, enriches book records with Google Books / Open Library APIs, groups multiple file formats under a unified book card, and provides sub-second search with instant file delivery.

---

## 🚀 Key Features

1. **Dual-Mode Persistence Architecture (Cloud-Resilient):**
   - **Mode A (Managed PostgreSQL / Supabase / Neon / Render Postgres):** Pass `DATABASE_URL` for full multi-container persistence and sub-second full-text search indexing (`tsvector` and `pg_trgm`).
   - **Mode B (SQLite with Automated Telegram Cloud Backups):** If `DATABASE_URL` is omitted, operates with optimized WAL SQLite. Includes an automated background worker taking vacuum snapshots every 6 hours and immediately on bot shutdown, dispatching them to admins as Telegram documents (`#DatabaseBackup`). Includes instant `/backup` command for admins.

2. **Automatic Storage Channel Ingestion:**
   - Listens to `channel_post` events in `STORAGE_CHANNEL_ID`.
   - Cleans messy titles, removes promotional handles (e.g. `@channel`, `.@heyuqt`), bracketed tags (e.g. `[L&A].`), and file extensions.
   - Detects formats: `AUDIO` (`.m4b`, `.mp3`), `EPUB`, `PDF`, and `ZIP`.
   - Groups multiple formats belonging to the same book under a single logical entity.

3. **Metadata Enrichment & Channel Broadcast Worker:**
   - Enriches indexed books with Cover Art, Synopsis, Rating, Genres, and Year from Google Books API and Open Library API.
   - Automatically posts formatted release cards to `UPDATES_CHANNEL_ID` with deep-link download buttons.

4. **Grouped Card UX & Instant Delivery:**
   - Direct text search with sub-second full-text search and prefix matching.
   - Paginated search results (5 per page).
   - Unified Book Card with dynamic download buttons (`[ 🎧 Audiobook (.m4b) ]`, `[ 📄 PDF ]`, `[ 📱 EPUB ]`, `[ 🗜 ZIP ]`).
   - Zero-bandwidth direct delivery leveraging Telegram's cached `file_id`.

5. **Inline Mode & Deep-Linking:**
   - Inline mode (`@AudioSoulBot <query>`) returning interactive articles with preview cards and deep-links.
   - `/start book_<id>` deep-links to directly jump into book cards from channel broadcasts or inline shares.

6. **Admin Analytics & Diagnostics:**
   - `/stats` command restricted to `ADMIN_IDS` with total indexed books, format breakdown, total users, total file deliveries served, active database engine (`PostgreSQL` or `SQLite FTS5`), and uptime duration.
   - `/backup` command for instant SQLite database snapshot export.

7. **Attribution & Support Backlinks:**
   - Persistent developer attribution footer and inline keyboard button linking directly to [Soul](https://t.me/souldumpp).

---

## 📁 Project Structure

```text
├── .env.example              # Environment variables template
├── requirements.txt          # Python dependencies
├── config.py                 # Pydantic Settings configuration (strict validation)
├── database/
│   ├── connection.py         # Dual-mode DB connection manager (PostgreSQL pool & SQLite WAL)
│   ├── models.py             # Unified schema, FTS5/tsvector indices, and queries
│   └── backup.py             # Vacuum-copy snapshot generator & automated Telegram backup worker
├── utils/
│   ├── parser.py             # Regex sanitizer and format detector
│   └── enricher.py           # Google Books & Open Library metadata enrichment
├── handlers/
│   ├── channel.py            # Channel ingestion, enrichment, and broadcast worker
│   ├── search.py             # Direct search, pagination, book cards, /start, and /help
│   ├── inline.py             # Inline query mode handler (@AudioSoulBot <query>)
│   └── admin.py              # /stats and /backup commands for admins
├── tests/
│   └── test_pipeline.py      # Automated unit and integration test suite
└── main.py                   # Bot startup lifecycle, background backup worker, and dispatcher
```

---

## ⚙️ Setup & Installation

### 1. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Edit `.env` with your credentials:
```env
BOT_TOKEN=1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ
ADMIN_IDS=123456789,987654321
STORAGE_CHANNEL_ID=-1001234567890
UPDATES_CHANNEL_ID=-1001987654321
DB_PATH=data/library.db
BOT_USERNAME=AudioSoulBot
```

> **Note on Channel IDs:** Add your bot as an administrator to both the storage channel and updates channel so it can read posts and send broadcasts.

### 2. Activate Virtual Environment & Run
```powershell
# In PowerShell:
.\venv\Scripts\Activate.ps1

# Run tests:
python -m unittest tests/test_pipeline.py

# Launch the bot:
python main.py
```
