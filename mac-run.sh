#!/bin/bash
# Mac half of Job Watch: checks the boards that block GitHub's servers, then pushes data/mac.json.
cd "$(dirname "$0")" || exit 1
git pull --rebase -q
/Library/Frameworks/Python.framework/Versions/3.15/bin/python3 watch.py --mac
git add data/mac.json
git diff --cached --quiet || git commit -q -m "Mac boards $(date '+%Y-%m-%d %H:%M')"
git push -q
