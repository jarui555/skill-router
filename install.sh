#!/usr/bin/env bash
# skill-router 一键安装（macOS / Linux）
#
#   bash install.sh
#
# 真正干活的逻辑在 install.py，这里只负责找到 Python。

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo
  echo "  没找到 Python 3.10+"
  echo "  macOS:  brew install python@3.12"
  echo "  Ubuntu: sudo apt install python3 python3-pip"
  echo
  exit 1
fi

exec "$PY" "$DIR/install.py"
