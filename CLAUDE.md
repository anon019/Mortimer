# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-powered book reading pipeline. The `/read` skill supports two modes:
- **Single**: Interactive 4-stage reading (概览→粗读→精读→深度探索) with free Q&A
- **Batch**: Booklist-driven bulk download, TPM-aware parallel analysis, completeness verification

Architecture: Claude orchestrates, Gemini 3 Flash (1M context) analyzes full text, Obsidian stores notes. Claude never reads book text directly.

## Commands

```bash
# Search & download (Anna's Archive)
python3 tools/annas-archive/annas.py search "title author" --format epub --limit 5
python3 tools/annas-archive/annas.py download <md5> --output books/

# Extract text from EPUB/PDF/TXT/AZW3/MOBI
python3 tools/extract_book.py books/book.epub -o /tmp/book_full.txt

# Gemini analysis (5 commands, all support --no-cache)
python3 tools/gemini_analyzer.py overview-skim --book /tmp/book.txt --title "书名" --author "作者" --category biography
python3 tools/gemini_analyzer.py deep-read     --book /tmp/book.txt --title "书名" --author "作者" --category biography
python3 tools/gemini_analyzer.py deep-dive     --book /tmp/book.txt --title "书名" --author "作者" --category biography --topic "主题"
python3 tools/gemini_analyzer.py ask           --book /tmp/book.txt --title "书名" --author "作者" --question "问题"
python3 tools/gemini_analyzer.py stats                                                                          # Token usage statistics

# Single-book full pipeline (extract → analyze → Obsidian)
bash tools/process_book.sh "books/book.epub" "书名" "作者" "category" "年份" "prefix"

# Batch processing
python3 tools/booklist_to_json.py booklists/list.md            # MD → JSON
python3 tools/batch_download.py booklists/list.json            # Search candidates
python3 tools/batch_download.py booklists/list.json --confirm  # Download selected
python3 tools/batch_read.py booklists/list.json                # TPM-aware parallel read
python3 tools/batch_read.py booklists/list.json --dry-run      # Preview plan
python3 tools/batch_verify.py booklists/list.json              # Verify completeness

# Google Drive backup (optional)
bash tools/gdrive_upload.sh <file_path> <category>
```

No test suite or linter configured. Verify tools by running them directly.

## Architecture

### Tools

| File | Purpose |
|------|---------|
| `tools/gemini_analyzer.py` | 5-command Gemini analysis with context cache, map-reduce, 10 category templates, usage stats |
| `tools/extract_book.py` | EPUB/PDF/TXT/AZW3/MOBI → plain text with chapter markers + TOC index |
| `tools/annas-archive/annas.py` | Book search & download (stdlib-only, mirror fallback) |
| `tools/process_book.sh` | Single-book full pipeline (extract → analyze → Obsidian) |
| `tools/batch_download.py` | Batch search + download with candidate scoring, file validation, and resume |
| `tools/batch_read.py` | TPM-aware parallel scheduling with TokenBucket, LCS matching, and checkpoint |
| `tools/batch_verify.py` | Obsidian note completeness verification with fuzzy matching |
| `tools/booklist_to_json.py` | Markdown booklist → JSON converter |
| `tools/gdrive_upload.sh` | Google Drive backup via `gws` CLI |

### Gemini analysis

| Command | What it does | API Calls |
|---------|-------------|-----------|
| `overview-skim` | 概览 + 粗读 merged (saves 33% tokens) | 1 |
| `deep-read` | 精读: 6-8 cross-chapter themes + 5 exploration angles + 延伸阅读推荐 | 1 |
| `deep-dive` | Single-topic deep exploration | 1 per topic |
| `ask` | Free-form Q&A, no note output | 1 |
| `stats` | Token usage statistics (from JSONL log) | 0 |

Books >800K tokens automatically use Map-Reduce (split → parallel analyze → synthesize).

### Categories

10 categories with specialized prompts: `biography`, `business`, `psychology`, `self-growth`, `technology`, `history`, `philosophy`, `finance`, `literature`, `science`.

### Obsidian output

```
Reading/{category}/{书名} ({作者}, {年份})/
├── 00-概览.md
├── 01-粗读.md
├── 02-精读.md
└── 03-深度-{topic}.md
```

### Skill definition

`skill/SKILL.md` — Claude Code `/read` skill, defines the full orchestration flow for both single and batch modes. Installed to `~/.claude/skills/read/SKILL.md`.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Yes | Google AI Studio API key |
| `GEMINI_MODEL` | No | Override model (default: `gemini-3-flash-preview`) |
| `ANNAS_ARCHIVE_KEY` | No | Anna's Archive membership key for downloads |
| `OBSIDIAN_VAULT_PATH` | No | Obsidian vault path (default: `~/Documents/Obsidian Vault`) |
| `GDRIVE_BOOKS_FOLDER_ID` | No | Google Drive folder ID for backups |

## Important Notes

- All Python tools are stdlib-only (zero pip dependencies)
- `extract_book.py` requires system `pdftotext` (`brew install poppler`); AZW3/MOBI support requires Calibre (`brew install calibre`)
- Anna's Archive filenames may contain unicode characters — `batch_download.py` auto-renames to clean filenames
- Downloaded files are validated (magic bytes + min size) before processing; invalid files are auto-removed
- Book file matching uses LCS (Longest Common Substring) similarity for fuzzy title matching
- Gemini: 1M context window, 65536 max output tokens, 900K TPM limit (with 10% safety margin)
- Result cache: 24h local cache at `/tmp/gemini_cache_*.json`, bypass with `--no-cache`
- Token usage logged to `/tmp/gemini_usage.jsonl`, view with `gemini_analyzer.py stats`
- The `/read` skill orchestrates everything — individual tools can also be run standalone
