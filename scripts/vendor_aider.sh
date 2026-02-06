#!/usr/bin/env bash
set -euo pipefail

AIDER_REF=${AIDER_REF:-4bf56b77145b0be593ed48c3c90cdecead217496}
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VENDOR_DIR="$ROOT_DIR/vendor/aider"

FILES=(
  "aider/coders/editblock_coder.py"
  "aider/coders/editblock_prompts.py"
  "aider/coders/editblock_fenced_prompts.py"
)

mkdir -p "$VENDOR_DIR/aider/coders"

for file in "${FILES[@]}"; do
  dest="$VENDOR_DIR/$file"
  mkdir -p "$(dirname "$dest")"
  curl -sSL "https://raw.githubusercontent.com/Aider-AI/aider/${AIDER_REF}/${file}" -o "$dest"
  echo "Updated $file"
done

curl -sSL "https://raw.githubusercontent.com/Aider-AI/aider/${AIDER_REF}/LICENSE" -o "$VENDOR_DIR/LICENSE.txt"

cat > "$VENDOR_DIR/manifest.json" <<MANIFEST
{
  "repo": "https://github.com/Aider-AI/aider",
  "ref": "${AIDER_REF}",
  "files": [
    "aider/coders/editblock_coder.py",
    "aider/coders/editblock_prompts.py",
    "aider/coders/editblock_fenced_prompts.py",
    "LICENSE.txt"
  ]
}
MANIFEST

cat > "$VENDOR_DIR/README.md" <<README
# Aider Vendor Snapshot

This directory vendors a minimal subset of Aider (https://github.com/Aider-AI/aider)
needed for the Meeseeks edit-block adapter. It is intentionally small and updated
via scripts/vendor_aider.sh.

Pinned upstream commit: ${AIDER_REF}

Included files:
- aider/coders/editblock_coder.py
- aider/coders/editblock_prompts.py
- aider/coders/editblock_fenced_prompts.py
- LICENSE.txt

See NOTICE.md for attribution details.
README

cat > "$VENDOR_DIR/NOTICE.md" <<NOTICE
Aider is licensed under the Apache License 2.0.

Upstream repository: https://github.com/Aider-AI/aider
Upstream commit: ${AIDER_REF}

This repository vendors a small subset of files for edit-block parsing and
application. The original copyright and license remain with the Aider authors.
NOTICE
