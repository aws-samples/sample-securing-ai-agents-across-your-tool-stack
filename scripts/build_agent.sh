#!/usr/bin/env bash
# Build the agent's code bundle for AgentCore Runtime direct code deployment.
#
#   bash scripts/build_agent.sh            prints the bundle directory, e.g. agent/.build/pkg-<hash>
#
# Dependencies are Linux arm64 wheels for Python 3.12 only (no source builds), pinned in agent/requirements.txt, which
# this script compiles from agent/requirements.in when the .in file is newer. The output directory is named after a hash
# of the pinned requirements and the agent sources, so an unchanged agent gives the same directory and the same CDK asset
# hash (a plain cdk diff stays clean). agent/.build/CURRENT names the directory the stack packages. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/../agent"
PLATFORM=aarch64-manylinux_2_28   # AgentCore Runtime runs on arm64; glibc 2.28 wheels or older
if [ ! -f requirements.txt ] || [ requirements.in -nt requirements.txt ]; then
  uv pip compile requirements.in --python-version 3.12 --python-platform "$PLATFORM" --no-header --quiet -o requirements.txt
fi
src_hash=$( (cat requirements.txt; find main.py gk_mcp -name '*.py' -type f | sort | xargs cat) | sha256sum | cut -c1-16)
out=".build/pkg-$src_hash"
if [ ! -d "$out" ]; then
  tmp=".build/tmp-$src_hash-$$"
  mkdir -p "$tmp"
  uv pip install --quiet --target "$tmp" --python-version 3.12 --python-platform "$PLATFORM" --only-binary :all: --no-compile \
    -r requirements.txt
  cp main.py "$tmp/"
  mkdir -p "$tmp/gk_mcp" && cp gk_mcp/*.py "$tmp/gk_mcp/"
  find "$tmp" -name '__pycache__' -type d -prune -exec rm -r {} +
  mv "$tmp" "$out"
fi
printf '%s\n' "pkg-$src_hash" > .build/CURRENT
echo "agent/$out ($(du -sm "$out" | cut -f1) MB)"
