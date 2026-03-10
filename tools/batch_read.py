#!/usr/bin/env python3
"""
batch_read.py - TPM-aware batch reading with dynamic token bucket.

Reads a JSON booklist, extracts text, estimates tokens, then schedules
parallel process_book.sh runs within Gemini's TPM limit.

Usage:
  python3 tools/batch_read.py booklists/2026-03-10.json           # Execute
  python3 tools/batch_read.py booklists/2026-03-10.json --dry-run # Preview plan

Features:
  - TokenBucket tracks 60-second rolling window for TPM compliance
  - Up to MAX_CONCURRENT=5 parallel process_book.sh processes
  - Small books scheduled first to maximize throughput
  - Checkpoint/resume: skips books already complete in Obsidian
  - Auto-retry failures (1 round) after all books finish
  - Saves failures to booklists/YYYY-MM-DD-read-failed.json
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TPM_LIMIT = 900_000  # 1M - 10% safety margin
MAX_CONCURRENT = 5
BOOK_TOKEN_LIMIT = 800_000  # triggers Map-Reduce above this

OBSIDIAN_VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH", Path.home() / "Documents" / "Obsidian Vault")) / "Reading"
BOOKS_DIR = Path(__file__).resolve().parent.parent / "books"
TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path(__file__).resolve().parent.parent

REQUIRED_FILES = ["00-概览.md", "01-粗读.md", "02-精读.md"]


# ---------------------------------------------------------------------------
# Token estimation (mirrors gemini_analyzer.py)
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Estimate token count. Chinese ~1.5 chars/token, English ~4 chars/token."""
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    non_cjk = len(text) - cjk
    return int(cjk / 1.5 + non_cjk / 4)


def estimate_tpm_cost(book_tokens: int) -> int:
    """Estimate total TPM cost for processing one book.

    process_book.sh makes 2 Gemini calls: overview-skim + deep-read.
    Each call sends the full book text as input.

    For books <= 800K tokens (direct path):
      tokens * 2 + 40_000 (2 calls + output overhead)

    For books > 800K tokens (Map-Reduce path):
      n_chunks * 800_000 + n_chunks * 30_000 * 2
      (each chunk is ~800K input + ~30K output, times 2 phases)
    """
    if book_tokens <= BOOK_TOKEN_LIMIT:
        return book_tokens * 2 + 40_000
    else:
        n_chunks = (book_tokens + BOOK_TOKEN_LIMIT - 1) // BOOK_TOKEN_LIMIT
        return n_chunks * 800_000 + n_chunks * 30_000 * 2


# ---------------------------------------------------------------------------
# TokenBucket - rolling 60-second window
# ---------------------------------------------------------------------------

class TokenBucket:
    """Tracks token consumption within a 60-second rolling window."""

    def __init__(self, tpm_limit: int = TPM_LIMIT):
        self.tpm_limit = tpm_limit
        self._records: list[tuple[float, int]] = []  # (timestamp, tokens)

    def _cleanup(self):
        """Remove records older than 60 seconds."""
        cutoff = time.time() - 60.0
        self._records = [(t, n) for t, n in self._records if t > cutoff]

    def current_usage(self) -> int:
        """Return total tokens consumed in the current 60-second window."""
        self._cleanup()
        return sum(n for _, n in self._records)

    def available(self) -> int:
        """Return available token budget in the current window."""
        return self.tpm_limit - self.current_usage()

    def acquire(self, estimated_tokens: int):
        """Block until the token budget can accommodate estimated_tokens."""
        while True:
            self._cleanup()
            usage = self.current_usage()
            if usage + estimated_tokens <= self.tpm_limit:
                self._records.append((time.time(), estimated_tokens))
                return
            # Need to wait
            wait_needed = self._time_until_available(estimated_tokens)
            print(
                f"  [TPM] 当前使用 {usage:,}/{self.tpm_limit:,}，"
                f"需要 {estimated_tokens:,}，等待 {wait_needed:.0f}s...",
                file=sys.stderr,
            )
            time.sleep(min(wait_needed, 5.0))  # Check every 5s at most

    def _time_until_available(self, needed: int) -> float:
        """Estimate seconds until enough budget is freed."""
        self._cleanup()
        if not self._records:
            return 0.0
        # Find oldest records that need to expire to free enough budget
        usage = self.current_usage()
        excess = usage + needed - self.tpm_limit
        if excess <= 0:
            return 0.0
        # Walk through records oldest-first to find when enough expires
        freed = 0
        now = time.time()
        for ts, tokens in sorted(self._records, key=lambda x: x[0]):
            freed += tokens
            if freed >= excess:
                # This record expires at ts + 60
                return max(0.0, (ts + 60.0) - now)
        # Should not happen, but fallback
        return 60.0


# ---------------------------------------------------------------------------
# Obsidian completeness check
# ---------------------------------------------------------------------------

def find_obsidian_dir(category: str, title: str, author: str) -> Path | None:
    """Find the Obsidian directory for a book, with fuzzy matching on title.

    The Obsidian dirs look like: Reading/{category}/{title} ({author}, {year})/
    But the title may be shortened (e.g., booklist has "李光耀回忆录：从第三世界到第一世界"
    but Obsidian dir is "李光耀回忆录 (李光耀, 2000)").
    """
    cat_dir = OBSIDIAN_VAULT / category
    if not cat_dir.exists():
        return None

    # Extract key words from the title for matching
    # Remove punctuation and split
    clean_title = re.sub(r'[：:·\-—\s]', '', title)

    for d in cat_dir.iterdir():
        if not d.is_dir():
            continue
        dir_name = d.name
        # Extract the title part (before the parenthesized author/year)
        m = re.match(r'^(.+?)\s*\(', dir_name)
        dir_title = m.group(1) if m else dir_name
        clean_dir_title = re.sub(r'[：:·\-—\s]', '', dir_title)

        # Check if the essential part of the title matches
        # Use the shorter title as the basis for matching
        shorter = min(clean_title, clean_dir_title, key=len)
        longer = max(clean_title, clean_dir_title, key=len)
        if shorter and shorter in longer:
            return d
        # Also try: first few chars match (for abbreviated titles)
        if len(clean_title) >= 3 and len(clean_dir_title) >= 3:
            if clean_title[:3] == clean_dir_title[:3]:
                return d

    return None


def is_book_complete(category: str, title: str, author: str) -> bool:
    """Check if a book has all 3 required Obsidian files and they are non-empty."""
    book_dir = find_obsidian_dir(category, title, author)
    if book_dir is None:
        return False

    for fname in REQUIRED_FILES:
        fpath = book_dir / fname
        if not fpath.exists() or fpath.stat().st_size == 0:
            return False
    return True


# ---------------------------------------------------------------------------
# Book file matching
# ---------------------------------------------------------------------------

def find_book_file(title: str, author: str, fmt: str) -> Path | None:
    """Find a book file in books/ directory by fuzzy matching on title keywords."""
    # Clean title: remove punctuation for matching
    clean_title = re.sub(r'[：:·\-—「」（）\(\)\s]', '', title)

    candidates = []
    for f in BOOKS_DIR.iterdir():
        if f.is_dir():
            continue
        fname = f.name
        clean_fname = re.sub(r'[：:·\-—「」（）\(\)\s]', '', fname)

        # Check if significant portion of title appears in filename
        # Try first 2-3 characters of cleaned title
        if len(clean_title) >= 2 and clean_title[:2] in clean_fname:
            candidates.append(f)
        elif len(clean_title) >= 3 and clean_title[:3] in clean_fname:
            candidates.append(f)

    if not candidates:
        # Try author name
        clean_author = re.sub(r'[\s\(\)编]', '', author)
        for f in BOOKS_DIR.iterdir():
            if f.is_dir():
                continue
            if clean_author and clean_author in f.name:
                candidates.append(f)

    if len(candidates) == 1:
        return candidates[0]
    elif len(candidates) > 1:
        # Prefer the one that matches format
        for c in candidates:
            if c.suffix.lstrip('.') == fmt:
                return c
        return candidates[0]

    return None


# ---------------------------------------------------------------------------
# tmp_prefix generation
# ---------------------------------------------------------------------------

def make_tmp_prefix(title: str) -> str:
    """Generate a safe ASCII prefix from book title for temp files."""
    safe = re.sub(r'[^a-zA-Z0-9]', '', title)[:20].lower()
    if not safe:
        # Chinese-only title: use hex of first few chars
        safe = title[:5].encode('utf-8').hex()[:16]
    return safe


# ---------------------------------------------------------------------------
# Task preparation
# ---------------------------------------------------------------------------

class BookTask:
    """Represents a single book processing task."""

    def __init__(self, entry: dict):
        self.id = entry["id"]
        self.title = entry["title"]
        self.author = entry["author"]
        self.category = entry["category"]
        self.year = entry.get("year", "")
        self.fmt = entry.get("format", "epub")
        self.search = entry.get("search", "")

        self.tmp_prefix = make_tmp_prefix(self.title)
        self.tmp_txt = Path(f"/tmp/book_{self.tmp_prefix}.txt")
        self.book_file: Path | None = None
        self.book_tokens: int = 0
        self.tpm_cost: int = 0
        self.status: str = "pending"  # pending, skip, ready, running, done, failed
        self.skip_reason: str = ""
        self.process: subprocess.Popen | None = None
        self.log_path: str = ""
        self.error_msg: str = ""

    def __repr__(self):
        return f"BookTask(#{self.id} {self.title})"


def prepare_tasks(booklist: list[dict]) -> list[BookTask]:
    """Prepare tasks: check completeness, find files, estimate tokens."""
    tasks = []

    for entry in booklist:
        task = BookTask(entry)

        # 1. Check if already complete in Obsidian
        if is_book_complete(task.category, task.title, task.author):
            task.status = "skip"
            task.skip_reason = "已完成 (Obsidian 中有 3 个文件)"
            tasks.append(task)
            continue

        # 2. Find book file
        task.book_file = find_book_file(task.title, task.author, task.fmt)
        if task.book_file is None:
            task.status = "failed"
            task.error_msg = f"未找到书籍文件 (title={task.title})"
            tasks.append(task)
            continue

        # 3. Check/estimate tokens
        if task.tmp_txt.exists() and task.tmp_txt.stat().st_size > 0:
            # Reuse existing extracted text
            with open(task.tmp_txt, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
            task.book_tokens = estimate_tokens(text)
        else:
            # Estimate from book file size (rough: 1 byte ~= 0.3-0.5 tokens for mixed content)
            file_size = task.book_file.stat().st_size
            # More conservative estimate from file size
            # EPUB: compressed, actual text ~2-3x compressed size, then tokens
            # We'll use a rough multiplier; actual extraction will refine this
            task.book_tokens = int(file_size * 0.6)  # rough estimate

        task.tpm_cost = estimate_tpm_cost(task.book_tokens)
        task.status = "ready"
        tasks.append(task)

    return tasks


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_text(task: BookTask) -> bool:
    """Extract book text if not already cached. Returns True on success."""
    if task.tmp_txt.exists() and task.tmp_txt.stat().st_size > 0:
        print(f"  [提取] 跳过 (已有缓存: {task.tmp_txt})", file=sys.stderr)
        return True

    if task.book_file is None:
        return False

    print(f"  [提取] {task.book_file.name} → {task.tmp_txt}", file=sys.stderr)
    try:
        result = subprocess.run(
            ["python3", str(TOOLS_DIR / "extract_book.py"),
             str(task.book_file), "--output", str(task.tmp_txt)],
            capture_output=True, text=True, timeout=120,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode != 0:
            task.error_msg = f"提取失败: {result.stderr[:200]}"
            return False

        # Re-estimate tokens from actual extracted text
        if task.tmp_txt.exists() and task.tmp_txt.stat().st_size > 0:
            with open(task.tmp_txt, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
            task.book_tokens = estimate_tokens(text)
            task.tpm_cost = estimate_tpm_cost(task.book_tokens)
            return True
        else:
            task.error_msg = "提取后文件为空"
            return False
    except subprocess.TimeoutExpired:
        task.error_msg = "提取超时 (120s)"
        return False
    except Exception as e:
        task.error_msg = f"提取异常: {e}"
        return False


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def launch_process(task: BookTask) -> bool:
    """Launch process_book.sh for a task. Returns True if launched."""
    if task.book_file is None:
        return False

    task.log_path = f"/tmp/batch_read_{task.tmp_prefix}.log"
    cmd = [
        "bash", str(TOOLS_DIR / "process_book.sh"),
        str(task.book_file),
        task.title,
        task.author,
        task.category,
        task.year,
        task.tmp_prefix,
    ]

    try:
        log_f = open(task.log_path, 'w')
        task.process = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_DIR),
        )
        task._log_fh = log_f  # Store for cleanup in reap_finished
        task.status = "running"
        print(f"  [启动] PID={task.process.pid} | {task.title}", file=sys.stderr)
        return True
    except Exception as e:
        log_f.close()
        task.error_msg = f"启动失败: {e}"
        task.status = "failed"
        return False


def reap_finished(running: list[BookTask]) -> tuple[list[BookTask], list[BookTask]]:
    """Check running processes, return (still_running, newly_finished)."""
    still_running = []
    finished = []
    for task in running:
        if task.process is None:
            finished.append(task)
            continue
        ret = task.process.poll()
        if ret is None:
            still_running.append(task)
        else:
            if ret == 0:
                task.status = "done"
                print(f"  [完成] {task.title} (exit=0)", file=sys.stderr)
            else:
                task.status = "failed"
                # Read last few lines of log for error context
                try:
                    with open(task.log_path, 'r') as f:
                        lines = f.readlines()
                        task.error_msg = ''.join(lines[-5:]).strip()[:300]
                except Exception:
                    task.error_msg = f"exit code {ret}"
                print(
                    f"  [失败] {task.title} (exit={ret}): {task.error_msg[:100]}",
                    file=sys.stderr,
                )
            # Close log file handle
            if hasattr(task, '_log_fh') and task._log_fh:
                task._log_fh.close()
                task._log_fh = None
            finished.append(task)
    return still_running, finished


# ---------------------------------------------------------------------------
# Main execution loop
# ---------------------------------------------------------------------------

def run_batch(tasks: list[BookTask], bucket: TokenBucket,
              max_concurrent: int = MAX_CONCURRENT, is_retry: bool = False):
    """Run all ready tasks with parallel scheduling."""
    ready = [t for t in tasks if t.status == "ready"]
    if not ready:
        print("没有待处理的任务。", file=sys.stderr)
        return

    # Sort by token count ascending (small books first)
    ready.sort(key=lambda t: t.book_tokens)

    label = "重试" if is_retry else "批处理"
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"[{label}] 共 {len(ready)} 本书待处理", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    running: list[BookTask] = []
    queue = list(ready)  # copy
    done_count = 0
    fail_count = 0
    total = len(ready)

    while queue or running:
        # Reap finished processes
        running, finished = reap_finished(running)
        for t in finished:
            if t.status == "done":
                done_count += 1
            else:
                fail_count += 1
            print(
                f"  [进度] {done_count + fail_count}/{total} "
                f"(成功={done_count}, 失败={fail_count}, 运行中={len(running)})",
                file=sys.stderr,
            )

        # Launch new tasks if slots and budget available
        while queue and len(running) < max_concurrent:
            task = queue[0]

            # Extract text first (synchronous, quick for cached files)
            print(f"\n[#{task.id}] {task.title}", file=sys.stderr)
            if not extract_text(task):
                task.status = "failed"
                queue.pop(0)
                fail_count += 1
                print(
                    f"  [失败] 提取错误: {task.error_msg}",
                    file=sys.stderr,
                )
                continue

            # Acquire TPM budget
            print(
                f"  [TPM] 预估 {task.book_tokens:,} tokens, "
                f"TPM成本 {task.tpm_cost:,}",
                file=sys.stderr,
            )
            bucket.acquire(task.tpm_cost)

            # Launch
            if launch_process(task):
                running.append(task)
            else:
                fail_count += 1

            queue.pop(0)

        # Brief sleep to avoid busy-waiting
        if running:
            time.sleep(2.0)

    print(f"\n[{label}完成] 成功={done_count}, 失败={fail_count}", file=sys.stderr)


def save_failures(tasks: list[BookTask], booklist_path: str):
    """Save failed tasks to a dated JSON file."""
    failed = [t for t in tasks if t.status == "failed"]
    if not failed:
        return None

    # Derive failure filename from booklist path
    base = os.path.basename(booklist_path)
    name, _ = os.path.splitext(base)
    fail_path = os.path.join(os.path.dirname(booklist_path), f"{name}-read-failed.json")

    entries = []
    for t in failed:
        entries.append({
            "id": t.id,
            "title": t.title,
            "author": t.author,
            "search": t.search,
            "format": t.fmt,
            "category": t.category,
            "year": t.year,
            "error": t.error_msg,
        })

    with open(fail_path, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    print(f"\n失败记录已保存: {fail_path}", file=sys.stderr)
    return fail_path


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def dry_run(tasks: list[BookTask]):
    """Print task plan without executing."""
    ready = [t for t in tasks if t.status == "ready"]
    skipped = [t for t in tasks if t.status == "skip"]
    failed = [t for t in tasks if t.status == "failed"]

    # Sort ready by token count ascending
    ready.sort(key=lambda t: t.book_tokens)

    print(f"\n{'='*70}")
    print(f"  Batch Read Plan — {len(tasks)} 本书")
    print(f"{'='*70}")

    if skipped:
        print(f"\n  SKIP ({len(skipped)} 本 — 已完成):")
        for t in skipped:
            print(f"    #{t.id:2d}  {t.title}")

    if failed:
        print(f"\n  ERROR ({len(failed)} 本 — 无法处理):")
        for t in failed:
            print(f"    #{t.id:2d}  {t.title} — {t.error_msg}")

    if ready:
        print(f"\n  READY ({len(ready)} 本 — 按 token 大小排序):")
        print(f"  {'#':>4s}  {'书名':<30s}  {'预估tokens':>12s}  {'TPM成本':>12s}")
        print(f"  {'—'*4}  {'—'*30}  {'—'*12}  {'—'*12}")
        total_tpm = 0
        for t in ready:
            total_tpm += t.tpm_cost
            print(
                f"  {t.id:4d}  {t.title:<30s}  {t.book_tokens:>12,}  {t.tpm_cost:>12,}"
            )
        print(f"\n  总 TPM 成本: {total_tpm:,}")
        print(f"  TPM 限制: {TPM_LIMIT:,}/min")
        print(f"  最大并发: {MAX_CONCURRENT}")
    else:
        print(f"\n  所有书籍已完成，无需处理。")

    print(f"\n{'='*70}\n")


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_interrupted = False


def handle_sigint(sig, frame):
    global _interrupted
    if _interrupted:
        # Second Ctrl+C: force exit
        print("\n强制退出。", file=sys.stderr)
        sys.exit(1)
    _interrupted = True
    print("\n收到中断信号，等待当前任务完成后退出...", file=sys.stderr)
    print("再次 Ctrl+C 强制退出。", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="TPM-aware batch book reading with parallel scheduling",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 tools/batch_read.py booklists/2026-03-10.json           # 执行
  python3 tools/batch_read.py booklists/2026-03-10.json --dry-run # 预览计划
        """,
    )
    parser.add_argument("booklist", help="JSON booklist 文件路径")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="预览任务计划，不执行",
    )
    parser.add_argument(
        "--max-concurrent", type=int, default=MAX_CONCURRENT,
        help=f"最大并发数 (默认: {MAX_CONCURRENT})",
    )
    parser.add_argument(
        "--tpm-limit", type=int, default=TPM_LIMIT,
        help=f"TPM 限制 (默认: {TPM_LIMIT:,})",
    )
    args = parser.parse_args()

    # Load booklist
    booklist_path = args.booklist
    if not os.path.exists(booklist_path):
        print(f"错误: 文件不存在: {booklist_path}", file=sys.stderr)
        sys.exit(1)

    with open(booklist_path, 'r', encoding='utf-8') as f:
        booklist = json.load(f)

    print(f"加载书单: {booklist_path} ({len(booklist)} 本书)", file=sys.stderr)

    # Prepare tasks
    tasks = prepare_tasks(booklist)

    if args.dry_run:
        dry_run(tasks)
        return

    # Execute
    signal.signal(signal.SIGINT, handle_sigint)

    max_conc = args.max_concurrent
    tpm = args.tpm_limit

    bucket = TokenBucket(tpm_limit=tpm)

    # First pass
    run_batch(tasks, bucket, max_concurrent=max_conc)

    # Auto-retry failures (1 round)
    failed_tasks = [t for t in tasks if t.status == "failed" and t.book_file is not None]
    if failed_tasks and not _interrupted:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"[重试] {len(failed_tasks)} 本书失败，进行自动重试...", file=sys.stderr)
        print(f"{'='*60}\n", file=sys.stderr)
        for t in failed_tasks:
            t.status = "ready"
            t.error_msg = ""
            t.process = None
        run_batch(tasks, bucket, max_concurrent=max_conc, is_retry=True)

    # Summary
    done = [t for t in tasks if t.status == "done"]
    skipped = [t for t in tasks if t.status == "skip"]
    failed_final = [t for t in tasks if t.status == "failed"]

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  批处理总结", file=sys.stderr)
    print(f"  完成: {len(done)} | 跳过: {len(skipped)} | 失败: {len(failed_final)}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    if failed_final:
        fail_path = save_failures(tasks, booklist_path)
        if done:
            print(f"部分成功。失败记录: {fail_path}", file=sys.stderr)
        else:
            print(f"全部失败。失败记录: {fail_path}", file=sys.stderr)

    # Exit code: 0 if all succeeded or skipped, 1 if any failed
    if failed_final:
        sys.exit(1)


if __name__ == "__main__":
    main()
