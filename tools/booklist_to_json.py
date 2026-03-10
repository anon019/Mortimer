#!/usr/bin/env python3
"""Parse a markdown booklist into JSON format for batch processing.

Usage:
    python3 tools/booklist_to_json.py booklists/2026-03-09.md
    python3 tools/booklist_to_json.py booklists/2026-03-09.md --output booklists/custom.json
"""

import json
import re
import sys
from pathlib import Path

# Section header keyword → category slug
CATEGORY_MAP = {
    "biography": "biography",
    "business": "business",
    "psychology": "psychology",
    "self-growth": "self-growth",
    "technology": "technology",
    "history": "history",
    "philosophy": "philosophy",
    "finance": "finance",
    "literature": "literature",
    "science": "science",
    # Chinese fallbacks
    "传记": "biography",
    "商业": "business",
    "心理": "psychology",
    "成长": "self-growth",
    "科技": "technology",
    "历史": "history",
    "哲学": "philosophy",
    "金融": "finance",
    "文学": "literature",
    "科学": "science",
}

# Regex: match ## header lines to extract category
# e.g. "## 👤 Biography 人物传记 (20本)" or "## 🏢 大公司/商业帝国 (15本)"
HEADER_RE = re.compile(r"^##\s+\S+\s+(.+?)(?:\s*\(\d+本\))?$")

# Regex: match book entry line
# e.g. "**1. 黄仁勋：英伟达之芯** — Tae Kim"
# Also handles edge case where closing ** is missing: "**12. 原则 — Ray Dalio"
# Also handles ✅ suffix: "**25. 刷新：微软重生** — Satya Nadella ✅ 已完成阅读"
BOOK_RE = re.compile(
    r"^\*\*(\d+)\.\s+(.+?)(?:\*\*)?\s*—\s*(.+?)(?:\s*✅.*)?$"
)

# Regex: match search + format line
# e.g. "`The Nvidia Way Tae Kim` | epub"
SEARCH_RE = re.compile(r"^`(.+?)`\s*\|\s*(\w+)")

# Try to extract a 4-digit year from search query
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def detect_category(header_text: str) -> str:
    """Map a section header to a category slug."""
    text = header_text.lower().strip()
    for keyword, slug in CATEGORY_MAP.items():
        if keyword.lower() in text:
            return slug
    # Fallback: return cleaned header
    return text.split()[0].lower()


def parse_booklist(md_path: Path) -> list[dict]:
    """Parse a markdown booklist file into a list of book entries."""
    lines = md_path.read_text(encoding="utf-8").splitlines()
    books = []
    current_category = "unknown"
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Check for category header (## level only, skip ### sub-headers)
        if line.startswith("## ") and not line.startswith("### "):
            m = HEADER_RE.match(line)
            if m:
                current_category = detect_category(m.group(1))

        # Check for book entry
        book_match = BOOK_RE.match(line)
        if book_match:
            book_id = int(book_match.group(1))
            title = book_match.group(2).strip().rstrip("*")
            author = book_match.group(3).strip()

            # Next line should be search + format
            search_query = ""
            fmt = "epub"
            if i + 1 < len(lines):
                search_match = SEARCH_RE.match(lines[i + 1].strip())
                if search_match:
                    search_query = search_match.group(1)
                    fmt = search_match.group(2).lower()
                    i += 1  # consume the search line

            # Extract year from search query if present
            year_match = YEAR_RE.search(search_query)
            year = year_match.group(1) if year_match else ""

            books.append({
                "id": book_id,
                "title": title,
                "author": author,
                "search": search_query,
                "format": fmt,
                "category": current_category,
                "year": year,
            })

        i += 1

    return books


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <booklist.md> [--output <output.json>]")
        sys.exit(1)

    md_path = Path(sys.argv[1])
    if not md_path.exists():
        print(f"Error: {md_path} not found")
        sys.exit(1)

    # Determine output path
    output_path = md_path.with_suffix(".json")
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_path = Path(sys.argv[idx + 1])

    books = parse_booklist(md_path)

    # Write JSON
    output_path.write_text(
        json.dumps(books, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Summary
    categories = {}
    for b in books:
        categories[b["category"]] = categories.get(b["category"], 0) + 1

    print(f"Parsed {len(books)} books → {output_path}")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")


if __name__ == "__main__":
    main()
