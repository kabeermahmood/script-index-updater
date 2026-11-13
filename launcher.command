#!/bin/bash
# Launches the Script Index Updater on macOS. Double-click this file in Finder.
# First run: python3 -m pip install -r requirements.txt
cd "$(dirname "$0")"
nohup python3 gui/app.py "$@" >/dev/null 2>&1 &
