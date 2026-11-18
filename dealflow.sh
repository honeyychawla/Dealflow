#!/bin/sh
# Launcher: ./dealflow <command>. Needs only Python 3.11+ (no packages to install).
cd "$(dirname "$0")" && exec python3 -m dealflow "$@"
