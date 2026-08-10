#!/bin/bash
# =====================================================================
#  PUSH TO GITHUB  —  double-click this file in Finder.
# =====================================================================
#
#  Why this exists: the assistant edits and commits in this folder on
#  the Mac, but cannot push — it has no access to your GitHub
#  credentials, by design. Pushing is the one step that needs a human
#  or a machine holding your keys.
#
#  This does exactly three things and nothing clever:
#    1. commits anything not yet committed
#    2. pushes to origin
#    3. tells you what is now on GitHub
#
#  It deliberately does NOT pull, rebase or merge. Those can produce
#  conflicts, and a conflict discovered by a double-clicked script at
#  11 pm before a collection day is the worst place to meet one.
# =====================================================================

cd "$(dirname "$0")" || exit 1

echo "======================================================"
echo "  PUSHING  $(basename "$PWD")"
echo "======================================================"
echo

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "  This folder is not a git repository."
    echo "  Nothing to push."
    echo
    read -r -p "  Press Return to close." _
    exit 1
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "  branch : $BRANCH"
echo "  remote : $(git remote get-url origin 2>/dev/null || echo 'none set')"
echo

# ---- 1. commit anything outstanding -----------------------------------
if [ -n "$(git status --porcelain)" ]; then
    echo "  Uncommitted changes found:"
    git status --short | sed 's/^/     /'
    echo
    git add -A
    git commit -q -m "Working changes from $(date '+%Y-%m-%d %H:%M')"
    echo "  Committed them."
    echo
else
    echo "  Working tree clean — nothing new to commit."
    echo
fi

# ---- 2. what is about to go up ----------------------------------------
AHEAD=$(git log --oneline "origin/$BRANCH..HEAD" 2>/dev/null | wc -l | tr -d ' ')
if [ "$AHEAD" = "0" ]; then
    echo "  Already up to date with GitHub. Nothing to push."
    echo
    read -r -p "  Press Return to close." _
    exit 0
fi

echo "  $AHEAD commit(s) to push:"
git log --oneline "origin/$BRANCH..HEAD" 2>/dev/null | sed 's/^/     /'
echo

# ---- 3. push ----------------------------------------------------------
if git push origin "$BRANCH"; then
    echo
    echo "  ---------------------------------------------------"
    echo "  DONE. GitHub is now at:"
    git log --oneline -1 | sed 's/^/     /'
    echo
    echo "  On the Windows machine, run:  windows\\update.bat"
    echo "  ---------------------------------------------------"
else
    echo
    echo "  PUSH FAILED."
    echo
    echo "  The usual cause is that git has no saved credentials for"
    echo "  GitHub on this Mac. Fix it once, from Terminal:"
    echo
    echo "      brew install gh && gh auth login"
    echo
    echo "  After that this file will work on its own every time."
fi

echo
read -r -p "  Press Return to close." _
