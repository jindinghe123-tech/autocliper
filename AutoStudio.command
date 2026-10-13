#!/bin/bash
# AutoStudio for macOS / Linux: double-click in Finder (or run ./AutoStudio.command).
# First run installs everything into this folder; see scripts/start.sh.
cd "$(dirname "$0")" && exec bash scripts/start.sh "$@"
