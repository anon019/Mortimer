#!/bin/bash
# process_book.sh - Full pipeline for one book: extract → overview-skim → deep-read → Obsidian
# Usage: bash tools/process_book.sh <book_file> <title> <author> <category> <year> <tmp_prefix>
# Example: bash tools/process_book.sh "books/苹果内幕 - Adam Lashinsky.epub" "苹果内幕" "Adam Lashinsky" "business" "2012" "apple"

set -euo pipefail

BOOK_FILE="$1"
TITLE="$2"
AUTHOR="$3"
CATEGORY="$4"
YEAR="$5"
TMP_PREFIX="$6"
DATE=$(date +%Y-%m-%d)

BOOK_DIR="Reading/${CATEGORY}/${TITLE} (${AUTHOR}, ${YEAR})"
TMP_BOOK="/tmp/book_${TMP_PREFIX}.txt"
TMP_OS="/tmp/gemini_${TMP_PREFIX}_os.txt"
TMP_OVERVIEW="/tmp/${TMP_PREFIX}_overview.txt"
TMP_SKIM="/tmp/${TMP_PREFIX}_skim.txt"
TMP_DEEPREAD="/tmp/${TMP_PREFIX}_deepread.txt"
LOG="/tmp/gemini_${TMP_PREFIX}.log"

cd "$(dirname "$0")/.."

echo "[${TITLE}] Step 1/6: Extracting text..."
if [ ! -f "$TMP_BOOK" ] || [ ! -s "$TMP_BOOK" ]; then
  python3 tools/extract_book.py "$BOOK_FILE" --output "$TMP_BOOK" 2>&1 | tail -1
fi
echo "[${TITLE}] Text size: $(du -k "$TMP_BOOK" | cut -f1)K"

echo "[${TITLE}] Step 2/6: Gemini overview-skim..."
python3 tools/gemini_analyzer.py overview-skim \
  --book "$TMP_BOOK" \
  --title "$TITLE" \
  --author "$AUTHOR" \
  --category "$CATEGORY" 2>>"$LOG" > "$TMP_OS"
echo "[${TITLE}] Overview-skim done ($(wc -c < "$TMP_OS") bytes)"

echo "[${TITLE}] Step 3/6: Splitting output..."
if ! grep -q '====SPLIT====' "$TMP_OS"; then
  echo "[${TITLE}] ERROR: ====SPLIT==== marker not found in Gemini output" >&2
  exit 1
fi
sed -n '1,/====SPLIT====/p' "$TMP_OS" | sed '$d' > "$TMP_OVERVIEW"
sed -n '/====SPLIT====/,$p' "$TMP_OS" | sed '1d' > "$TMP_SKIM"

echo "[${TITLE}] Step 4/6: Writing overview + skim to Obsidian..."

# Extract themes from first line of overview (if present) and move into frontmatter
THEMES_YAML=""
if head -1 "$TMP_OVERVIEW" | grep -q '^themes:'; then
  THEMES_YAML=$(head -1 "$TMP_OVERVIEW" | sed 's/^themes: *//' | awk -F', ' '{for(i=1;i<=NF;i++) { gsub(/^ *| *$/, "", $i); printf "  - %s\n", $i }}')
fi

# Write overview via temp file to avoid shell escaping issues
{
cat <<FRONTMATTER_HEAD
---
title: ${TITLE}
author: ${AUTHOR}
category: ${CATEGORY}
date: ${DATE}
status: reading
source: full
FRONTMATTER_HEAD
if [ -n "$THEMES_YAML" ]; then
  echo "themes:"
  echo "$THEMES_YAML"
fi
echo "---"
echo
} > "/tmp/${TMP_PREFIX}_obs_overview.md"

# Append body, skipping themes line if it was extracted
if head -1 "$TMP_OVERVIEW" | grep -q '^themes:'; then
  tail -n +2 "$TMP_OVERVIEW" >> "/tmp/${TMP_PREFIX}_obs_overview.md"
else
  cat "$TMP_OVERVIEW" >> "/tmp/${TMP_PREFIX}_obs_overview.md"
fi
cat >> "/tmp/${TMP_PREFIX}_obs_overview.md" <<'PROGRESS'

## 阅读进度

- [x] 📖 检视阅读
- [x] 📝 粗读
- [ ] 🔬 精读
- [ ] 💡 深度探索（0/5 个视角已完成）
PROGRESS

if ! obsidian create path="${BOOK_DIR}/00-概览.md" content="$(cat "/tmp/${TMP_PREFIX}_obs_overview.md")" overwrite 2>&1; then
  echo "[${TITLE}] ERROR: Failed to write 00-概览.md to Obsidian" >&2
  exit 1
fi

# Write skim via temp file
cat > "/tmp/${TMP_PREFIX}_obs_skim.md" <<FRONTMATTER
---
title: ${TITLE} - 粗读
parent: "[[00-概览]]"
---

FRONTMATTER
cat "$TMP_SKIM" >> "/tmp/${TMP_PREFIX}_obs_skim.md"

if ! obsidian create path="${BOOK_DIR}/01-粗读.md" content="$(cat "/tmp/${TMP_PREFIX}_obs_skim.md")" overwrite 2>&1; then
  echo "[${TITLE}] ERROR: Failed to write 01-粗读.md to Obsidian" >&2
  exit 1
fi

echo "[${TITLE}] Step 5/6: Gemini deep-read..."
python3 tools/gemini_analyzer.py deep-read \
  --book "$TMP_BOOK" \
  --title "$TITLE" \
  --author "$AUTHOR" \
  --category "$CATEGORY" 2>>"$LOG" > "$TMP_DEEPREAD"
echo "[${TITLE}] Deep-read done ($(wc -c < "$TMP_DEEPREAD") bytes)"

echo "[${TITLE}] Step 6/6: Writing deep-read to Obsidian..."

# Write deep-read via temp file
cat > "/tmp/${TMP_PREFIX}_obs_deepread.md" <<FRONTMATTER
---
title: ${TITLE} - 精读
parent: "[[00-概览]]"
---

FRONTMATTER
cat "$TMP_DEEPREAD" >> "/tmp/${TMP_PREFIX}_obs_deepread.md"

if ! obsidian create path="${BOOK_DIR}/02-精读.md" content="$(cat "/tmp/${TMP_PREFIX}_obs_deepread.md")" overwrite 2>&1; then
  echo "[${TITLE}] ERROR: Failed to write 02-精读.md to Obsidian" >&2
  exit 1
fi

echo "[${TITLE}] Complete! All 3 notes written to ${BOOK_DIR}/"
