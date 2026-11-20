#!/usr/bin/env bash
# Build the two Lambda layers (Python 3.12, arm64) from layers/<name>/requirements.txt into layers/<name>/python/.
#
#   bash scripts/build_layers.sh            prints one line per layer
#
# Linux arm64 wheels only (no source builds). A layer is rebuilt only when its requirements.txt changes (a hash in
# layers/<name>/python/.requirements-sha256), so an unchanged layer keeps its CDK asset hash. Needs uv.
set -euo pipefail
cd "$(dirname "$0")/../layers"
PLATFORM=aarch64-manylinux_2_28   # the Lambda functions run on arm64; glibc 2.28 wheels or older
for name in boto3 pyjwt; do
  want=$(sha256sum "$name/requirements.txt" | cut -c1-64)
  stamp="$name/python/.requirements-sha256"
  if [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$want" ]; then
    echo "layers/$name: up to date"
    continue
  fi
  tmp="$name/tmp-$$"
  mkdir -p "$tmp"
  uv pip install --quiet --target "$tmp" --python-version 3.12 --python-platform "$PLATFORM" --only-binary :all: --no-compile \
    -r "$name/requirements.txt"
  find "$tmp" -name '__pycache__' -type d -prune -exec rm -r {} +
  rm -f "$tmp/.lock"
  printf '%s\n' "$want" > "$tmp/.requirements-sha256"
  if [ -d "$name/python" ]; then mv "$name/python" "$name/old-$$"; fi
  mv "$tmp" "$name/python"
  if [ -d "$name/old-$$" ]; then rm -r "$name/old-$$"; fi
  echo "layers/$name: built ($(du -sm "$name/python" | cut -f1) MB)"
done
