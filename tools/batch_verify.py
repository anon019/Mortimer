#!/usr/bin/env python3
"""
batch_verify.py - Verify Obsidian reading note completeness for a JSON booklist.

Scans the Obsidian vault and checks that each book in the booklist has:
  - A matching directory under Reading/{category}/
  - All 3 required files: 00-概览.md, 01-粗读.md, 02-精读.md
  - Files are non-empty (size > 0)
  - 00-概览.md has frontmatter with title, author, category fields

Usage:
  python3 tools/batch_verify.py booklists/2026-03-09.json
  python3 tools/batch_verify.py booklists/2026-03-09.json --verbose

Exit code: 0 if all books complete, 1 if any issues found.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OBSIDIAN_VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH", Path.home() / "Documents" / "Obsidian Vault")) / "Reading"
REQUIRED_FILES = ["00-概览.md", "01-粗读.md", "02-精读.md"]
FRONTMATTER_FIELDS = ["title", "author", "category"]


# ---------------------------------------------------------------------------
# Fuzzy directory matching (improved scoring over batch_read.py)
# ---------------------------------------------------------------------------

def _clean_title(title: str) -> str:
    """Strip punctuation and whitespace from a title for comparison."""
    return re.sub(r'[：:·\-—\s「」《》（）\(\)]', '', title)


def _extract_dir_title(dir_name: str) -> str:
    """Extract the title portion from a directory name like '书名 (作者, 年)'."""
    m = re.match(r'^(.+?)\s*\(', dir_name)
    return m.group(1) if m else dir_name


def _longest_common_substring_len(a: str, b: str) -> int:
    """Return the length of the longest common substring between a and b."""
    if not a or not b:
        return 0
    # Simple O(n*m) DP approach; titles are short so this is fine
    max_len = 0
    prev = [0] * (len(b) + 1)
    for i in range(len(a)):
        curr = [0] * (len(b) + 1)
        for j in range(len(b)):
            if a[i] == b[j]:
                curr[j + 1] = prev[j] + 1
                if curr[j + 1] > max_len:
                    max_len = curr[j + 1]
        prev = curr
    return max_len


def _title_similarity(title_a: str, title_b: str) -> float:
    """Score how well two cleaned titles match. Returns 0.0-1.0.

    Strategies (in priority order):
      1. Exact match -> 1.0
      2. One is a proper substring of the other -> len(shorter)/len(longer)
      3. Longest common substring ratio (LCS / max(len_a, len_b))
         Handles cases like "创始人们PayPal黑帮传奇" vs "创始人们PayPal传奇"
    """
    if not title_a or not title_b:
        return 0.0
    if title_a == title_b:
        return 1.0

    longer_len = max(len(title_a), len(title_b))

    # Proper substring check (one must be strictly shorter)
    if len(title_a) != len(title_b):
        shorter = title_a if len(title_a) < len(title_b) else title_b
        longer = title_b if len(title_a) < len(title_b) else title_a
        if shorter in longer:
            return len(shorter) / len(longer)

    # Longest common substring fallback
    lcs = _longest_common_substring_len(title_a, title_b)
    return lcs / longer_len


# Minimum similarity score to accept a directory match
_MIN_SCORE = 0.35


def _search_category(cat_dir: Path, clean_title: str) -> tuple[Path | None, float]:
    """Search a single category directory for the best matching book dir."""
    best_dir: Path | None = None
    best_score: float = 0.0

    if not cat_dir.exists():
        return None, 0.0

    for d in cat_dir.iterdir():
        if not d.is_dir():
            continue
        dir_title = _extract_dir_title(d.name)
        clean_dir_title = _clean_title(dir_title)

        score = _title_similarity(clean_title, clean_dir_title)
        if score > best_score:
            best_score = score
            best_dir = d

    return best_dir, best_score


def find_obsidian_dir(category: str, title: str, author: str) -> Path | None:
    """Find the Obsidian directory for a book, with fuzzy matching on title.

    The Obsidian dirs look like: Reading/{category}/{title} ({author}, {year})/
    But the title may be shortened (e.g., booklist has "李光耀回忆录：从第三世界到第一世界"
    but Obsidian dir is "李光耀回忆录 (李光耀, 2000)").

    Uses a scoring approach: picks the best-matching directory above a minimum
    threshold. Falls back to searching all categories when the specified one
    has no match (booklist category may differ from actual Obsidian category).
    """
    clean_title = _clean_title(title)

    # First: search the specified category
    best_dir, best_score = _search_category(OBSIDIAN_VAULT / category, clean_title)
    if best_score >= _MIN_SCORE:
        return best_dir

    # Fallback: search all other categories
    for cat in OBSIDIAN_VAULT.iterdir():
        if not cat.is_dir() or cat.name == category:
            continue
        d, score = _search_category(cat, clean_title)
        if score > best_score:
            best_score = score
            best_dir = d

    if best_score >= _MIN_SCORE:
        return best_dir

    return None


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def parse_frontmatter(filepath: Path) -> dict:
    """Parse YAML frontmatter from a markdown file. Returns dict of fields."""
    fields = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read(4096)  # Read enough for frontmatter
    except Exception:
        return fields

    # Check for frontmatter delimiters
    if not content.startswith('---'):
        return fields

    end = content.find('---', 3)
    if end == -1:
        return fields

    fm_block = content[3:end].strip()
    for line in fm_block.split('\n'):
        line = line.strip()
        if ':' in line:
            key, _, value = line.partition(':')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                fields[key] = value

    return fields


# ---------------------------------------------------------------------------
# Verification logic
# ---------------------------------------------------------------------------

class BookResult:
    """Verification result for a single book."""

    def __init__(self, entry: dict):
        self.id = entry["id"]
        self.title = entry["title"]
        self.author = entry["author"]
        self.category = entry["category"]
        self.year = entry.get("year", "")

        self.dir_found: bool = False
        self.dir_path: Path | None = None
        self.missing_files: list[str] = []
        self.empty_files: list[tuple[str, int]] = []  # (filename, size)
        self.frontmatter_issues: list[str] = []
        self.file_sizes: dict[str, int] = {}

    @property
    def is_complete(self) -> bool:
        return (
            self.dir_found
            and not self.missing_files
            and not self.empty_files
            and not self.frontmatter_issues
        )

    @property
    def short_name(self) -> str:
        """Short display name for the book."""
        return self.title


def verify_book(entry: dict) -> BookResult:
    """Verify a single book's Obsidian notes."""
    result = BookResult(entry)

    # Find directory
    book_dir = find_obsidian_dir(result.category, result.title, result.author)
    if book_dir is None:
        result.dir_found = False
        result.missing_files = list(REQUIRED_FILES)
        return result

    result.dir_found = True
    result.dir_path = book_dir

    # Check each required file
    for fname in REQUIRED_FILES:
        fpath = book_dir / fname
        if not fpath.exists():
            result.missing_files.append(fname)
        else:
            size = fpath.stat().st_size
            result.file_sizes[fname] = size
            if size == 0:
                result.empty_files.append((fname, size))

    # Check frontmatter of 00-概览.md
    overview_path = book_dir / "00-概览.md"
    if overview_path.exists() and overview_path.stat().st_size > 0:
        fm = parse_frontmatter(overview_path)
        for field in FRONTMATTER_FIELDS:
            if field not in fm or not fm[field]:
                result.frontmatter_issues.append(f"缺少 frontmatter 字段: {field}")

    return result


def verify_booklist(booklist: list[dict], verbose: bool = False) -> list[BookResult]:
    """Verify all books in a booklist."""
    results = []
    for entry in booklist:
        result = verify_book(entry)
        results.append(result)
        if verbose:
            status = "OK" if result.is_complete else "ISSUE"
            dir_info = f" -> {result.dir_path.name}" if result.dir_path else " -> NOT FOUND"
            print(f"  [{status}] #{result.id:2d} {result.title}{dir_info}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(results: list[BookResult]) -> str:
    """Generate the verification summary report."""
    lines = []

    total = len(results)
    complete = [r for r in results if r.is_complete]
    incomplete = [r for r in results if not r.is_complete]

    # Per-file completeness counts
    overview_ok = sum(1 for r in results if r.dir_found and "00-概览.md" not in r.missing_files and not any(f == "00-概览.md" for f, _ in r.empty_files))
    skim_ok = sum(1 for r in results if r.dir_found and "01-粗读.md" not in r.missing_files and not any(f == "01-粗读.md" for f, _ in r.empty_files))
    deep_ok = sum(1 for r in results if r.dir_found and "02-精读.md" not in r.missing_files and not any(f == "02-精读.md" for f, _ in r.empty_files))

    # Header
    if not incomplete:
        lines.append(f"✅ {len(complete)}/{total} 完成 (概览 {overview_ok}/{total}, 粗读 {skim_ok}/{total}, 精读 {deep_ok}/{total})")
    else:
        lines.append(f"{'✅' if len(complete) > 0 else '❌'} {len(complete)}/{total} 完成 (概览 {overview_ok}/{total}, 粗读 {skim_ok}/{total}, 精读 {deep_ok}/{total})")

    # Missing directories (no Obsidian dir found at all)
    no_dir = [r for r in results if not r.dir_found]
    if no_dir:
        lines.append(f"❌ 未找到目录 ({len(no_dir)}):")
        for r in no_dir:
            lines.append(f"  - {r.title} [{r.category}]")

    # Missing files (dir exists but files missing)
    missing = []
    for r in results:
        if r.dir_found:
            for fname in r.missing_files:
                missing.append((r.short_name, fname))
    if missing:
        lines.append(f"❌ 缺少文件 ({len(missing)}):")
        for title, fname in missing:
            lines.append(f"  - {title}/{fname}")

    # Empty files
    empty = []
    for r in results:
        for fname, size in r.empty_files:
            empty.append((r.short_name, fname, size))
    if empty:
        lines.append(f"⚠️ 空文件 ({len(empty)}):")
        for title, fname, size in empty:
            lines.append(f"  - {title}/{fname} ({size} bytes)")

    # Frontmatter issues
    fm_issues = []
    for r in results:
        for issue in r.frontmatter_issues:
            fm_issues.append((r.short_name, issue))
    if fm_issues:
        lines.append(f"⚠️ Frontmatter 问题 ({len(fm_issues)}):")
        for title, issue in fm_issues:
            lines.append(f"  - {title}: {issue}")

    # Stats
    total_files = sum(
        len(REQUIRED_FILES) - len(r.missing_files)
        for r in results
        if r.dir_found
    )
    categories = set(r.category for r in results if r.dir_found)

    lines.append(f"📊 总计: {total_files} 个笔记文件, 覆盖 {len(categories)} 个分类")

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Verify Obsidian reading note completeness for a JSON booklist",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 tools/batch_verify.py booklists/2026-03-09.json
  python3 tools/batch_verify.py booklists/2026-03-09.json --verbose
        """,
    )
    parser.add_argument("booklist", help="JSON booklist 文件路径")
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="显示每本书的详细验证状态",
    )
    args = parser.parse_args()

    # Load booklist
    booklist_path = args.booklist
    if not os.path.exists(booklist_path):
        print(f"错误: 文件不存在: {booklist_path}", file=sys.stderr)
        sys.exit(1)

    with open(booklist_path, 'r', encoding='utf-8') as f:
        booklist = json.load(f)

    print(f"验证书单: {booklist_path} ({len(booklist)} 本书)\n", file=sys.stderr)

    # Verify
    results = verify_booklist(booklist, verbose=args.verbose)

    # Generate and print report
    report = generate_report(results)
    print(report)

    # Exit code
    has_issues = any(not r.is_complete for r in results)
    sys.exit(1 if has_issues else 0)


if __name__ == "__main__":
    main()
