#!/bin/bash
# =====================================================================
#  INSTALL AUTO-PUSH  —  double-click once. Then never think about it.
# =====================================================================
#
#  Installs a launchd agent that checks this repository every five
#  minutes and pushes any commits the assistant has made — after the
#  test suite passes. It runs as you, so it uses your GitHub
#  credentials; nothing else gains access to them.
#
#  Double-click again to see its status. To remove it:
#      ./install_autopush.command --uninstall
# =====================================================================

cd "$(dirname "$0")" || exit 1
REPO="$PWD"
LABEL="com.janaulich.gazefollower.autopush"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "======================================================"
echo "  AUTO-PUSH"
echo "======================================================"
echo "  repository : $REPO"
echo

if [ "$1" = "--uninstall" ]; then
    launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "  Removed. Pushing is manual again (push.command)."
    echo
    read -r -p "  Press Return to close." _
    exit 0
fi

chmod +x "$REPO/autopush.sh" 2>/dev/null

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO/autopush.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$REPO/data/autopush.err</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null
if launchctl load "$PLIST" 2>/dev/null; then
    echo "  Installed. It will check every 5 minutes and push when there"
    echo "  are new commits AND the test suite passes."
    echo
    echo "  It will NOT push past a failing test, and it never commits"
    echo "  anything itself — uncommitted work stays yours."
    echo
    echo "  Watch it work:"
    echo "      tail -f \"$REPO/data/autopush.log\""
    echo
    echo "  Remove it:"
    echo "      \"$REPO/install_autopush.command\" --uninstall"
else
    echo "  Could not load the agent. You can still push by hand with"
    echo "  push.command."
fi

# First run now, so you find out immediately whether credentials work
# rather than in five minutes.
echo
echo "  Running once now…"
echo "  ----------------------------------------------------"
bash "$REPO/autopush.sh"
if [ -f "$REPO/data/autopush.log" ]; then
    tail -n 12 "$REPO/data/autopush.log" | sed 's/^/  /'
else
    echo "  (nothing to push — already up to date)"
fi
echo "  ----------------------------------------------------"
echo
read -r -p "  Press Return to close." _
