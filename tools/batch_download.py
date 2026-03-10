#!/usr/bin/env python3
"""
Batch book search and download from Anna's Archive.

Two modes:

  Search mode (default):
    python3 tools/batch_download.py booklists/2026-03-10.json
    - Searches Anna's Archive for each book in the JSON booklist
    - Scores candidates and auto-selects the best match
    - Saves results to booklists/YYYY-MM-DD-candidates.json
    - Supports resume: re-run to continue where you left off

  Download mode (--confirm):
    python3 tools/batch_download.py booklists/2026-03-10.json --confirm
    - Downloads selected candidates from the candidates file
    - Renames to clean filenames: {title} - {author}.{ext}
    - Uploads to Google Drive via gdrive_upload.sh
    - Tracks failures to -download-failed.json

Stdlib only — no pip packages required.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

# Import annas.py functions directly
TOOLS_DIR = Path(__file__).resolve().parent
ANNAS_DIR = TOOLS_DIR / "annas-archive"
sys.path.insert(0, str(ANNAS_DIR))
from annas import search_books as _annas_search, download_book as _annas_download  # noqa: E402

# Project root
PROJECT_ROOT = TOOLS_DIR.parent
BOOKS_DIR = PROJECT_ROOT / "books"


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _strip_punctuation(text: str) -> str:
    """Remove punctuation and normalize whitespace for fuzzy matching."""
    text = re.sub(r'[·：\-—–""\'\'「」《》、，。！？\s\u00b7\u2018\u2019\u201c\u201d]+', ' ', text)
    text = re.sub(r'[^\w\s]', '', text)
    return text.strip().lower()


def _is_cjk(text: str) -> bool:
    """Check if text contains CJK characters."""
    for ch in text:
        cp = ord(ch)
        # CJK Unified Ideographs
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
            return True
        # Japanese Hiragana/Katakana
        if 0x3040 <= cp <= 0x30FF:
            return True
        # Korean Hangul
        if 0xAC00 <= cp <= 0xD7AF:
            return True
    return False


def _book_exists_in_dir(title: str, author: str, books_dir: Path) -> str | None:
    """
    Fuzzy check if a book already exists in books_dir.
    Returns the matching filename or None.

    Matches if >= 50% of the title keywords appear in any filename.
    """
    if not books_dir.exists():
        return None

    title_clean = _strip_punctuation(title)
    title_keywords = [w for w in title_clean.split() if len(w) > 1]

    if not title_keywords:
        return None

    for f in books_dir.iterdir():
        if not f.is_file():
            continue
        fname = _strip_punctuation(f.stem)
        matched = sum(1 for kw in title_keywords if kw in fname)
        if matched >= max(1, len(title_keywords) * 0.5):
            return f.name

    return None


def _sanitize_filename(text: str) -> str:
    """Remove characters unsafe for filenames, collapse whitespace."""
    # Remove problematic characters
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', text)
    # Remove unicode fancy quotes
    text = text.replace('\u2018', '').replace('\u2019', '')
    text = text.replace('\u201c', '').replace('\u201d', '')
    # Remove dots (extension handled separately)
    text = text.replace('.', '')
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _clean_filename(title: str, author: str, ext: str) -> str:
    """
    Build a clean filename: {title} - {author}.{ext}
    Strips CJK punctuation marks and shell-problematic characters.
    """
    clean_title = re.sub(r'[·：\-—–""\'\'「」《》、，。！？]', '', title)
    clean_title = _sanitize_filename(clean_title)

    clean_author = _sanitize_filename(author)

    if len(clean_title) > 60:
        clean_title = clean_title[:60].rstrip()
    if len(clean_author) > 40:
        clean_author = clean_author[:40].rstrip()

    return f"{clean_title} - {clean_author}.{ext}"


# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------

def _score_candidate(candidate: dict, book: dict) -> int:
    """
    Score a search result against a book entry.

    Scoring rules:
      +100  English original (non-CJK title)
      +50   epub format
      +30   pdf format
      +20   Year match
      +15   Matches requested format exactly
      +5*n  Author name words found in result (5 per word)
      +10   Larger file (filepath length > 50 as proxy)
      +5    Medium file (filepath length > 30)
    """
    score = 0
    c_title = candidate.get('title', '') or ''
    c_format = (candidate.get('format', '') or '').lower()
    c_year = candidate.get('year', '') or ''
    c_author = (candidate.get('author', '') or '').lower()
    c_filepath = candidate.get('filepath', '') or ''

    # English original preferred
    if not _is_cjk(c_title):
        score += 100

    # Format bonus
    if c_format == 'epub':
        score += 50
    elif c_format == 'pdf':
        score += 30

    # Year match
    book_year = (book.get('year', '') or '').strip()
    if book_year and c_year and book_year == c_year:
        score += 20

    # Requested format match
    requested_format = (book.get('format', '') or '').lower()
    if requested_format and c_format == requested_format:
        score += 15

    # Author match
    book_author_lower = _strip_punctuation(book.get('author', ''))
    author_words = [w for w in book_author_lower.split() if len(w) > 2]
    if author_words:
        matched = sum(1 for w in author_words if w in c_author)
        score += 5 * matched

    # File size proxy (longer filepaths tend to have more metadata = bigger files)
    if len(c_filepath) > 50:
        score += 10
    elif len(c_filepath) > 30:
        score += 5

    return score


# ---------------------------------------------------------------------------
# Candidates file management
# ---------------------------------------------------------------------------

def _derive_candidates_path(booklist_path: Path) -> Path:
    """
    booklists/2026-03-10.json -> booklists/2026-03-10-candidates.json
    """
    stem = booklist_path.stem
    return booklist_path.parent / f"{stem}-candidates.json"


def _load_existing_candidates(candidates_path: Path) -> dict:
    """Load existing candidates file for resume support. Returns {id: entry}."""
    if not candidates_path.exists():
        return {}
    try:
        with open(candidates_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {entry['id']: entry for entry in data}
    except (json.JSONDecodeError, KeyError):
        return {}


def _save_candidates(candidates_path: Path, candidates_by_id: dict, booklist: list):
    """Save candidates in booklist order (preserves original ordering)."""
    ordered = []
    for book in booklist:
        bid = book['id']
        if bid in candidates_by_id:
            ordered.append(candidates_by_id[bid])
    with open(candidates_path, 'w', encoding='utf-8') as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Search phase
# ---------------------------------------------------------------------------

def run_search(booklist_path: Path):
    """Search Anna's Archive for each book, score candidates, save results."""
    with open(booklist_path, 'r', encoding='utf-8') as f:
        booklist = json.load(f)

    candidates_path = _derive_candidates_path(booklist_path)
    existing = _load_existing_candidates(candidates_path)

    print(f"Booklist:    {booklist_path.name} ({len(booklist)} books)")
    print(f"Candidates:  {candidates_path.name}")
    print(f"Resumed:     {len(existing)} already processed")
    print(f"Books dir:   {BOOKS_DIR}")
    print()

    skipped_exists = 0
    skipped_resume = 0
    searched = 0
    no_results = 0

    for i, book in enumerate(booklist):
        bid = book['id']
        title = book['title']
        author = book['author']
        search_query = book['search']
        fmt = book.get('format', '')

        prefix = f"[{i+1}/{len(booklist)}] #{bid}"

        # Skip if already in candidates with results (resume)
        if bid in existing and (existing[bid].get('candidates') or existing[bid].get('local_file')):
            skipped_resume += 1
            reason = existing[bid].get('auto_pick_reason', 'resumed')
            print(f"{prefix} {title} -- resumed ({reason})")
            continue

        # Skip if already in books/
        existing_file = _book_exists_in_dir(title, author, BOOKS_DIR)
        if existing_file:
            skipped_exists += 1
            existing[bid] = {
                **book,
                'candidates': [],
                'local_file': existing_file,
                'auto_pick_reason': 'already_downloaded',
            }
            _save_candidates(candidates_path, existing, booklist)
            print(f"{prefix} {title} -- skipped (exists: {existing_file})")
            continue

        # Search with format filter
        print(f"{prefix} Searching: {search_query} (format={fmt})...", end=' ', flush=True)
        results = _annas_search(search_query, format_filter=fmt if fmt else None, limit=10)

        # Retry without format filter if no results
        if not results and fmt:
            print("retry without format...", end=' ', flush=True)
            results = _annas_search(search_query, format_filter=None, limit=10)

        if not results:
            no_results += 1
            existing[bid] = {
                **book,
                'candidates': [],
                'auto_pick_reason': 'no_results',
            }
            _save_candidates(candidates_path, existing, booklist)
            print("NO RESULTS")
            time.sleep(2)
            continue

        # Score each result
        scored = []
        for r in results:
            s = _score_candidate(r, book)
            scored.append({
                'md5': r['md5'],
                'title': r.get('title', ''),
                'author': r.get('author', ''),
                'format': r.get('format', ''),
                'year': r.get('year', ''),
                'filepath': r.get('filepath', ''),
                'score': s,
                'selected': False,
            })

        # Sort by score descending, auto-select the highest
        scored.sort(key=lambda x: x['score'], reverse=True)

        if scored:
            scored[0]['selected'] = True
            best = scored[0]
            reason = f"score={best['score']}"
            print(f"found {len(scored)}, best: {best['title'][:40]} ({reason})")
        else:
            reason = 'no_scored_results'
            print("no scorable results")

        existing[bid] = {
            **book,
            'candidates': scored,
            'auto_pick_reason': reason,
        }

        # Save after EACH book for resume
        _save_candidates(candidates_path, existing, booklist)
        searched += 1

        # Rate limit between searches
        time.sleep(2)

    # Summary
    print()
    print("=" * 60)
    print("Search Summary")
    print("=" * 60)
    print(f"  Total books:      {len(booklist)}")
    print(f"  Searched:         {searched}")
    print(f"  Skipped (exist):  {skipped_exists}")
    print(f"  Skipped (resume): {skipped_resume}")
    print(f"  No results:       {no_results}")
    print(f"  Candidates file:  {candidates_path}")
    print()
    print("Next step: review candidates file, then run:")
    print(f"  python3 tools/batch_download.py {booklist_path} --confirm")


# ---------------------------------------------------------------------------
# Download phase
# ---------------------------------------------------------------------------

def run_download(booklist_path: Path):
    """Download selected candidates, rename, upload to Google Drive."""
    candidates_path = _derive_candidates_path(booklist_path)

    if not candidates_path.exists():
        print(f"Error: candidates file not found: {candidates_path}", file=sys.stderr)
        print(f"Run search first:", file=sys.stderr)
        print(f"  python3 tools/batch_download.py {booklist_path}", file=sys.stderr)
        sys.exit(1)

    with open(candidates_path, 'r', encoding='utf-8') as f:
        entries = json.load(f)

    BOOKS_DIR.mkdir(parents=True, exist_ok=True)

    # Failed downloads log
    failed_stem = candidates_path.stem.replace('-candidates', '')
    failed_path = candidates_path.parent / f"{failed_stem}-download-failed.json"

    downloaded = 0
    skipped = 0
    failed = 0
    uploaded = 0
    failures = []

    print(f"Candidates:  {candidates_path.name} ({len(entries)} entries)")
    print(f"Download to: {BOOKS_DIR}")
    print()

    for i, entry in enumerate(entries):
        bid = entry['id']
        title = entry['title']
        author = entry['author']
        category = entry.get('category', '')
        prefix = f"[{i+1}/{len(entries)}] #{bid}"

        # Skip if local_file already set
        if entry.get('local_file'):
            skipped += 1
            print(f"{prefix} {title} -- skipped (local_file: {entry['local_file']})")
            continue

        # Skip if already in books/
        existing_file = _book_exists_in_dir(title, author, BOOKS_DIR)
        if existing_file:
            skipped += 1
            entry['local_file'] = existing_file
            print(f"{prefix} {title} -- skipped (exists: {existing_file})")
            continue

        # Find selected candidate
        candidates = entry.get('candidates', [])
        selected = None
        for c in candidates:
            if c.get('selected'):
                selected = c
                break

        if not selected:
            skipped += 1
            reason = entry.get('auto_pick_reason', 'no selection')
            print(f"{prefix} {title} -- skipped ({reason})")
            continue

        md5 = selected['md5']
        ext = (selected.get('format', '') or 'epub').lower()

        print(f"{prefix} Downloading: {selected.get('title', title)[:50]} (md5={md5[:12]}...)...",
              end=' ', flush=True)

        # Download via annas.py
        try:
            result_path = _annas_download(md5, output_dir=str(BOOKS_DIR))
        except Exception as e:
            result_path = None
            print(f"ERROR: {e}")

        if not result_path:
            failed += 1
            failures.append({
                'id': bid,
                'title': title,
                'md5': md5,
                'error': 'download returned None',
            })
            print("FAILED")
            time.sleep(1)
            continue

        # Rename to clean filename
        result_path = Path(result_path)
        actual_ext = result_path.suffix.lstrip('.').lower()
        if actual_ext:
            ext = actual_ext

        clean_name = _clean_filename(title, author, ext)
        clean_path = BOOKS_DIR / clean_name

        if result_path != clean_path:
            try:
                result_path.rename(clean_path)
            except OSError:
                # Keep original name on rename failure
                clean_path = result_path
                clean_name = result_path.name

        print(f"OK -> {clean_name}")
        entry['local_file'] = clean_name
        downloaded += 1

        # Save progress after each download
        with open(candidates_path, 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

        # Upload to Google Drive
        if category:
            gdrive_script = TOOLS_DIR / "gdrive_upload.sh"
            if gdrive_script.exists():
                print(f"  -> Google Drive (Books/{category}/)...", end=' ', flush=True)
                try:
                    proc = subprocess.run(
                        ['bash', str(gdrive_script), str(clean_path), category],
                        capture_output=True, text=True, timeout=120,
                    )
                    if proc.returncode == 0:
                        uploaded += 1
                        print("OK")
                    else:
                        print(f"FAILED: {proc.stderr.strip()[:100]}")
                except subprocess.TimeoutExpired:
                    print("TIMEOUT")
                except Exception as e:
                    print(f"ERROR: {e}")

        # Rate limit between downloads
        time.sleep(1)

    # Save failures
    if failures:
        with open(failed_path, 'w', encoding='utf-8') as f:
            json.dump(failures, f, ensure_ascii=False, indent=2)

    # Final save of candidates with local_file updates
    with open(candidates_path, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    # Summary
    print()
    print("=" * 60)
    print("Download Summary")
    print("=" * 60)
    print(f"  Total entries:      {len(entries)}")
    print(f"  Downloaded:         {downloaded}")
    print(f"  Uploaded to Drive:  {uploaded}")
    print(f"  Skipped:            {skipped}")
    print(f"  Failed:             {failed}")
    if failures:
        print(f"  Failures saved to:  {failed_path}")
    print(f"  Candidates updated: {candidates_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Batch book search and download from Anna's Archive",
        epilog="""Examples:
  python3 tools/batch_download.py booklists/2026-03-10.json           # Search phase
  python3 tools/batch_download.py booklists/2026-03-10.json --confirm # Download phase

The search phase creates a -candidates.json file you can review/edit
before running the download phase with --confirm.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        'booklist',
        type=Path,
        help='Path to JSON booklist file (e.g. booklists/2026-03-10.json)',
    )
    parser.add_argument(
        '--confirm',
        action='store_true',
        help='Download selected candidates (default: search only)',
    )

    args = parser.parse_args()

    # Validate booklist exists
    if not args.booklist.exists():
        print(f"Error: booklist not found: {args.booklist}", file=sys.stderr)
        sys.exit(1)

    if not args.booklist.suffix == '.json':
        print(f"Error: booklist must be a .json file: {args.booklist}", file=sys.stderr)
        sys.exit(1)

    if args.confirm:
        run_download(args.booklist)
    else:
        run_search(args.booklist)


if __name__ == '__main__':
    main()
