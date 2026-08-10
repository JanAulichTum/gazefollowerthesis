#!/bin/bash
# =====================================================================
#  AUTO-PUSH  —  run by launchd every few minutes. Not for hand use.
# =====================================================================
#
#  Why this exists: the assistant can edit and commit in this folder but
#  cannot push, because it has no access to your GitHub credentials —
#  by design. A launchd agent runs as YOU, so it does have them. That
#  closes the loop without anyone holding a key they should not.
#
#  It is deliberately conservative. An automatic push that ships broken
#  code to the machine you collect data on is worse than no automation
#  at all, so:
#
#    * it only acts when there are unpushed commits
#    * it runs the test suite FIRST and refuses to push if it fails
#    * it never commits anything itself — uncommitted work is yours
#    * everything it does is logged, including its refusals
#
#  Log:  data/autopush.log
# =====================================================================

cd "$(dirname "$0")" || exit 1
LOG="data/autopush.log"
mkdir -p data

say() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" >> "$LOG"; }

# Keep the log from growing without bound (last ~500 lines).
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 800 ]; then
    tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

git rev-parse --git-dir >/dev/null 2>&1 || { say "not a git repo — stopping"; exit 1; }

BRANCH=$(git rev-parse --abbrev-ref HEAD)

# Nothing to do is the common case. Stay silent so the log stays
# readable — a log that records every no-op is a log nobody reads.
git fetch --quiet origin "$BRANCH" 2>/dev/null
AHEAD=$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 0)
[ "$AHEAD" = "0" ] && exit 0

say "$AHEAD unpushed commit(s) on $BRANCH:"
git log --oneline "origin/$BRANCH..HEAD" | sed 's/^/            /' >> "$LOG"

# ---- Gate on the tests -------------------------------------------------
# The whole point of the suite is that it has caught real faults on this
# project. Pushing past it automatically would make it decorative.
PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"
if [ -f run_tests.py ]; then
    if ! "$PY" run_tests.py > /tmp/autopush_tests.txt 2>&1; then
        say "REFUSING TO PUSH — run_tests.py failed:"
        grep -E "^  ✗|RESULT:" /tmp/autopush_tests.txt | sed 's/^/            /' >> "$LOG"
        exit 1
    fi
    say "tests pass"
fi

# ---- Push --------------------------------------------------------------
if git push origin "$BRANCH" >> "$LOG" 2>&1; then
    say "pushed -> $(git log --oneline -1)"
else
    say "PUSH FAILED — see above. If this says 'could not read Username',"
    say "  the agent has no stored credential: run 'gh auth login' once."
fi
