#!/usr/bin/env python3
"""
Batch upload books to Google Drive based on booklist categories.
Usage: python3 tools/batch_upload_gdrive.py booklists/2026-03-09.md
"""

import os
import re
import subprocess
import sys
from pathlib import Path

BOOKS_DIR = Path(__file__).resolve().parent.parent / "books"
GDRIVE_SCRIPT = Path(__file__).resolve().parent / "gdrive_upload.sh"

# Book number → category mapping from booklist structure
CATEGORY_RANGES = {
    'biography': range(1, 21),      # 1-20
    'business': range(21, 36),       # 21-35
    'psychology': range(36, 39),     # 36-38
    'finance': range(39, 42),        # 39-41
    'science': range(42, 45),        # 42-44
    'history': range(45, 48),        # 45-47
    'philosophy': range(48, 50),     # 48-49
    'technology': range(50, 51),     # 50
}


def get_category(num):
    for cat, r in CATEGORY_RANGES.items():
        if num in r:
            return cat
    return 'uncategorized'


def parse_booklist(filepath):
    """Extract book entries from booklist markdown."""
    books = []
    content = Path(filepath).read_text()
    pattern = re.compile(
        r'\*\*(\d+)\.\s+(.+?)\*\*\s*—\s*(.+?)\n',
        re.MULTILINE
    )
    for m in pattern.finditer(content):
        books.append({
            'num': int(m.group(1)),
            'title': m.group(2).strip(),
            'author': m.group(3).strip(),
        })
    return books


def find_book_file(title, author):
    """Find the book file in books/ directory."""
    title_lower = title.lower()
    author_parts = author.lower().split()

    for f in BOOKS_DIR.iterdir():
        fname = f.name.lower()
        # Match by title keywords
        title_keywords = [kw for kw in re.sub(r'[^\w\s]', '', title_lower).split() if len(kw) > 1]
        if any(kw in fname for kw in title_keywords[:3]):
            return f
        # Match by author last name
        if author_parts and author_parts[-1] in fname:
            if any(kw in fname for kw in title_keywords[:1]):
                return f
    return None


def upload(filepath, category):
    """Upload a file to Google Drive."""
    result = subprocess.run(
        ["bash", str(GDRIVE_SCRIPT), str(filepath), category],
        capture_output=True, text=True, timeout=120
    )
    return result.returncode == 0, result.stdout.strip()


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 tools/batch_upload_gdrive.py <booklist.md>")
        sys.exit(1)

    books = parse_booklist(sys.argv[1])
    if not books:
        print("No books found in booklist")
        sys.exit(1)

    print(f"Found {len(books)} books, uploading to Google Drive...\n")

    success = 0
    failed = 0
    not_found = 0

    for book in books:
        num = book['num']
        title = book['title']
        category = get_category(num)
        filepath = find_book_file(title, book['author'])

        if not filepath:
            print(f"[{num:02d}] SKIP {title} — file not found locally")
            not_found += 1
            continue

        print(f"[{num:02d}] Uploading {filepath.name} → Books/{category}/")
        ok, output = upload(filepath, category)

        if ok:
            print(f"      ✓ {output.splitlines()[-1] if output else 'Done'}")
            success += 1
        else:
            print(f"      ✗ Upload failed")
            failed += 1

    print(f"\n{'='*50}")
    print(f"DONE: {success} uploaded, {failed} failed, {not_found} not found")


if __name__ == '__main__':
    main()
