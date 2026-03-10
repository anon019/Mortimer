#!/bin/bash
# Upload a book file to Google Drive Books/<category>/ folder
# Usage: gdrive_upload.sh <file_path> <category>
# Example: gdrive_upload.sh books/elon-musk.epub biography

set -e

FILE_PATH="$1"
CATEGORY="$2"

if [ -z "$FILE_PATH" ] || [ -z "$CATEGORY" ]; then
  echo "Usage: gdrive_upload.sh <file_path> <category>"
  echo "Categories: biography, business, psychology, self-growth, technology, history, philosophy, finance, literature, science"
  exit 1
fi

if [ ! -f "$FILE_PATH" ]; then
  echo "Error: File not found: $FILE_PATH"
  exit 1
fi

BOOKS_FOLDER_ID="${GDRIVE_BOOKS_FOLDER_ID:?Error: Set GDRIVE_BOOKS_FOLDER_ID to your Google Drive Books folder ID}"

# Find or create category subfolder
CATEGORY_ID=$(gws drive files list \
  --params "{\"q\": \"name='${CATEGORY}' and '${BOOKS_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false\", \"fields\": \"files(id)\"}" \
  2>/dev/null | python3 -c "import sys,json; files=json.load(sys.stdin).get('files',[]); print(files[0]['id'] if files else '')")

if [ -z "$CATEGORY_ID" ]; then
  echo "Creating folder: Books/${CATEGORY}/"
  CATEGORY_ID=$(gws drive files create \
    --json "{\"name\": \"${CATEGORY}\", \"mimeType\": \"application/vnd.google-apps.folder\", \"parents\": [\"${BOOKS_FOLDER_ID}\"]}" \
    2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
fi

# Upload file
FILENAME=$(basename "$FILE_PATH")
echo "Uploading: ${FILENAME} → Books/${CATEGORY}/"
RESULT=$(gws drive +upload "$FILE_PATH" --parent "$CATEGORY_ID" 2>/dev/null)
FILE_ID=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Done: https://drive.google.com/file/d/${FILE_ID}"
