#!/usr/bin/env python3
"""One-time cleanup of Obsidian reading notes.

Fixes:
1. Remove Gemini debug lines (Book text:, Calling Gemini, Tokens - input:, [Map-Reduce], [Map X/Y], [Reduce])
2. Remove "## 任务一/二" task headers
3. Update status: reading → status: read in 00-概览.md files
4. Clean up resulting double/triple blank lines
"""

import re
from pathlib import Path

VAULT = Path.home() / "Documents" / "Obsidian Vault" / "Reading"

# Debug lines to remove (full lines)
DEBUG_PATTERNS = [
    re.compile(r'^Book text: ~[\d,]+ estimated tokens\s*$'),
    re.compile(r'^Calling Gemini .*$'),
    re.compile(r'^Tokens - input: .*$'),
    re.compile(r'^\[Map-Reduce\].*$'),
    re.compile(r'^\[Map \d+/\d+\].*$'),
    re.compile(r'^\[Reduce\].*$'),
]

# Task headers to remove
TASK_HEADER_PATTERN = re.compile(r'^## 任务[一二].*$')

# Stats
stats = {
    'files_scanned': 0,
    'files_modified': 0,
    'debug_lines_removed': 0,
    'task_headers_removed': 0,
    'status_updated': 0,
}


def is_debug_line(line: str) -> bool:
    """Check if a line matches any debug pattern."""
    stripped = line.strip()
    if not stripped:
        return False
    for pattern in DEBUG_PATTERNS:
        if pattern.match(stripped):
            return True
    return False


def is_task_header(line: str) -> bool:
    """Check if a line is a task header like '## 任务一：书籍概览'."""
    return bool(TASK_HEADER_PATTERN.match(line.strip()))


def clean_double_blanks(text: str) -> str:
    """Collapse 3+ consecutive newlines into 2 (one blank line)."""
    return re.sub(r'\n{3,}', '\n\n', text)


def process_file(filepath: Path) -> None:
    """Process a single markdown file."""
    stats['files_scanned'] += 1

    content = filepath.read_text(encoding='utf-8')
    original = content

    is_overview = filepath.name == '00-概览.md'
    lines = content.split('\n')
    new_lines = []
    debug_removed = 0
    headers_removed = 0

    for line in lines:
        if is_debug_line(line):
            debug_removed += 1
            continue
        if is_task_header(line):
            headers_removed += 1
            continue
        # Handle edge case: "---## 任务二" (frontmatter closer glued to task header)
        task_glued = re.match(r'^(---)\s*## 任务[一二].*$', line)
        if task_glued:
            new_lines.append(task_glued.group(1))
            headers_removed += 1
            continue
        new_lines.append(line)

    content = '\n'.join(new_lines)

    # Update status in frontmatter for 概览 files
    status_updated = False
    if is_overview and 'status: reading' in content:
        # Only replace within frontmatter (between --- markers)
        parts = content.split('---', 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            if 'status: reading' in frontmatter:
                frontmatter = frontmatter.replace('status: reading', 'status: read')
                content = '---' + frontmatter + '---' + parts[2]
                status_updated = True

    # Clean up multiple blank lines
    content = clean_double_blanks(content)

    if content != original:
        filepath.write_text(content, encoding='utf-8')
        stats['files_modified'] += 1
        stats['debug_lines_removed'] += debug_removed
        stats['task_headers_removed'] += headers_removed
        if status_updated:
            stats['status_updated'] += 1

        # Report what changed
        changes = []
        if debug_removed:
            changes.append(f'{debug_removed} debug lines')
        if headers_removed:
            changes.append(f'{headers_removed} task headers')
        if status_updated:
            changes.append('status → read')
        rel = filepath.relative_to(VAULT)
        print(f'  Fixed: {rel} ({", ".join(changes)})')


def main():
    if not VAULT.exists():
        print(f'Error: Vault not found at {VAULT}')
        return

    print(f'Scanning {VAULT} ...\n')

    # Process all .md files under Reading/
    md_files = sorted(VAULT.rglob('*.md'))
    for f in md_files:
        process_file(f)

    print(f'\n--- Summary ---')
    print(f'Files scanned:        {stats["files_scanned"]}')
    print(f'Files modified:       {stats["files_modified"]}')
    print(f'Debug lines removed:  {stats["debug_lines_removed"]}')
    print(f'Task headers removed: {stats["task_headers_removed"]}')
    print(f'Status updated:       {stats["status_updated"]}')


if __name__ == '__main__':
    main()
