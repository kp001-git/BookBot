import os
import re
from typing import Optional, Tuple, NamedTuple


class ParsedMetadata(NamedTuple):
    clean_title: str
    clean_author: str
    canonical_title: str  # Lowercase normalized for grouping
    canonical_author: str # Lowercase normalized for grouping
    format_type: str      # 'AUDIO', 'EPUB', 'PDF', 'ZIP'
    file_name: str


EXT_MAP = {
    ".m4b": "AUDIO",
    ".mp3": "AUDIO",
    ".m4a": "AUDIO",
    ".flac": "AUDIO",
    ".aac": "AUDIO",
    ".ogg": "AUDIO",
    ".opus": "AUDIO",
    ".epub": "EPUB",
    ".pdf": "PDF",
    ".zip": "AUDIO",
    ".rar": "AUDIO",
    ".7z": "AUDIO",
    ".tar": "AUDIO",
    ".gz": "AUDIO",
}


def detect_format(file_name: Optional[str], mime_type: Optional[str] = None, is_audio_msg: bool = False) -> str:
    """Detects format type (AUDIO, EPUB, PDF) from file extension, mime_type, or Telegram message type.
    Reclassifies archive files (.zip, .rar, .tar) as AUDIO (Audiobooks)."""
    if is_audio_msg:
        return "AUDIO"

    if file_name:
        _, ext = os.path.splitext(file_name.lower())
        if ext in EXT_MAP:
            return EXT_MAP[ext]

    if mime_type:
        mime = mime_type.lower()
        if "audio" in mime or "zip" in mime or "rar" in mime or "tar" in mime or "7z" in mime:
            return "AUDIO"
        if "epub" in mime:
            return "EPUB"
        if "pdf" in mime:
            return "PDF"

    return "PDF"


def clean_text_noise(text: str) -> str:
    """Removes telegram handles, bracketed tags, URLs, and file extensions while preserving author/title separators."""
    if not text:
        return ""

    # Replace underscores with spaces
    cleaned = text.replace("_", " ")

    # Strip URLs
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = re.sub(r"t\.me/\S+", " ", cleaned)
    cleaned = re.sub(r"bit\.ly/\S+", " ", cleaned)

    # Strip bracketed prefixes/tags, e.g. [L&A]., [Audiobook], [EPUB], (@channel)
    cleaned = re.sub(r"\[.*?\][\.]?", " ", cleaned)
    cleaned = re.sub(r"\(@\w+\)", " ", cleaned)

    # Strip telegram handles, e.g. @heyuqt, .@heyuqt
    cleaned = re.sub(r"\.?@\w+", " ", cleaned)

    # Strip known book & audio extensions
    extensions = [
        r"\.m4b\b", r"\.mp3\b", r"\.m4a\b", r"\.flac\b", r"\.aac\b", r"\.ogg\b",
        r"\.epub\b", r"\.pdf\b", r"\.zip\b", r"\.rar\b", r"\.7z\b", r"\.mobi\b", r"\.azw3\b"
    ]
    for ext in extensions:
        cleaned = re.sub(ext, " ", cleaned, flags=re.IGNORECASE)

    # Normalize repeated dots, spaces
    cleaned = re.sub(r"[\.]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ ]{2,}", " ", cleaned)

    # Strip leading/trailing punctuation and spaces, preserving hyphens in between
    cleaned = cleaned.strip(" ._~|/:")
    return cleaned


def normalize_casing(text: str) -> str:
    """Provides a human-friendly casing while preserving standard titles."""
    text = text.strip(" -–—:;,.")
    if not text:
        return ""
    # If entirely uppercase or lowercase, convert to Title Case
    if text.isupper() or text.islower():
        words = text.split()
        lowers = {"a", "an", "the", "and", "but", "or", "for", "nor", "on", "at", "to", "by", "in", "of"}
        capitalized = []
        for i, w in enumerate(words):
            if i == 0 or i == len(words) - 1 or w.lower() not in lowers:
                capitalized.append(w.capitalize())
            else:
                capitalized.append(w.lower())
        return " ".join(capitalized)
    return text


def split_title_author(raw_str: str) -> Tuple[str, str]:
    """
    Heuristically splits raw string into (title, author).
    Handles patterns like:
    - "Author - Title" or "Title - Author"
    - "Title by Author"
    - "ALL CAPS TITLE Title Case Author"
    """
    raw = clean_text_noise(raw_str).strip()
    if not raw:
        return "Unknown Title", "Unknown Author"

    # Pattern 1: Hyphen/Dash separator e.g. "Wariner Ruth - The Sound of Gravel" or "Brandon Sanderson - Mistborn"
    sep_match = re.search(r"\s+[-–—]\s+", raw)
    if sep_match:
        parts = re.split(r"\s+[-–—]\s+", raw, maxsplit=1)
        part1 = parts[0].strip()
        part2 = parts[1].strip()

        # In book libraries, "Author - Title" is the dominant standard (e.g. Wariner Ruth - The Sound of Gravel)
        author = normalize_casing(part1)
        title = normalize_casing(part2)
        return title, author

    # Pattern 2: All-Caps Title followed by Title-Case Author
    # e.g. "RAISED BY A SERIAL KILLER April Balascio"
    caps_transition = re.search(r"^([A-Z0-9\s,'&!:?]+?)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)$", raw)
    if caps_transition:
        title = caps_transition.group(1).strip()
        author = caps_transition.group(2).strip()
        if len(title) > 2 and len(author) > 2:
            return normalize_casing(title), normalize_casing(author)

    # Pattern 3: "Title by Author" (where Author starts with capital letter, not "by a ...")
    by_match = re.search(r"^(.*?)\s+by\s+([A-Z][a-zA-Z\.\s]+)$", raw)
    if by_match:
        title = by_match.group(1).strip()
        author = by_match.group(2).strip()
        return normalize_casing(title), normalize_casing(author)

    # Pattern 4: Check if last 2 words are Author Name like "Title Title April Balascio"
    words = raw.split()
    if len(words) >= 4:
        if (words[-2][0].isupper() and words[-2][1:].islower() and
            words[-1][0].isupper() and words[-1][1:].islower() and
            not any(char.isdigit() for char in words[-2:])):
            author = f"{words[-2]} {words[-1]}"
            title = " ".join(words[:-2]).strip(" -–—:")
            return normalize_casing(title), normalize_casing(author)

    return normalize_casing(raw), "Unknown Author"


def parse_media_metadata(
    file_name: Optional[str] = None,
    caption: Optional[str] = None,
    audio_title: Optional[str] = None,
    audio_performer: Optional[str] = None,
    mime_type: Optional[str] = None,
    is_audio_msg: bool = False
) -> ParsedMetadata:
    """
    Parses media attributes from Telegram message objects, applies the cleaning pipeline,
    and returns a standardized ParsedMetadata named tuple.
    """
    format_type = detect_format(file_name, mime_type, is_audio_msg)

    # 1. Native audio metadata if present and clean
    if audio_title and audio_title.strip():
        clean_t = clean_text_noise(audio_title)
        clean_a = clean_text_noise(audio_performer or "")
        if clean_t:
            if not clean_a or clean_a.lower() in ["unknown", "unknown artist", "various artists"]:
                title, author = split_title_author(clean_t)
            else:
                title = normalize_casing(clean_t)
                author = normalize_casing(clean_a)
        else:
            title, author = split_title_author(file_name or caption or "Untitled")
    else:
        # 2. Extract from file_name or caption
        text_source = file_name or caption or "Untitled"
        title, author = split_title_author(text_source)

    if not title:
        title = "Untitled"
    if not author:
        author = "Unknown Author"

    # Canonical representations for database indexing and matching
    canonical_title = re.sub(r"[^\w\s]", "", title.lower()).strip()
    canonical_author = re.sub(r"[^\w\s]", "", author.lower()).strip()

    safe_file_name = file_name or f"{title}.{format_type.lower()}"

    return ParsedMetadata(
        clean_title=title,
        clean_author=author,
        canonical_title=canonical_title,
        canonical_author=canonical_author,
        format_type=format_type,
        file_name=safe_file_name
    )
