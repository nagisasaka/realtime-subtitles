#!/usr/bin/env bash
# Copy source to NTFS, never share a Linux venv with Windows Python.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
target_dir="${1:-/mnt/c/workspace/realtime-subtitles}"
mkdir -p "$target_dir"
rsync -a --exclude='.venv/' --exclude='.venv-win/' --exclude='.git/' \
  --exclude='__pycache__/' --exclude='.pytest_cache/' --exclude='.ruff_cache/' \
  --exclude='build/' --exclude='dist/' --exclude='*.egg-info/' \
  --exclude='diagnostics/' --exclude='recordings/' --exclude='transcripts/' \
  --exclude='*.wav' --exclude='*.pcm' --exclude='*.raw' \
  --exclude='.env*' --exclude='*.jsonl' --exclude='settings.json' \
  "$repo_dir/" "$target_dir/"
echo "Windows source copy: $target_dir"
