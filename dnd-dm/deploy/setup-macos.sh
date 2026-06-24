#!/usr/bin/env bash
# One-shot setup for the always-on Mac mini host. Run it ON the Mac, from inside the copied
# project: `bash dnd-dm/deploy/setup-macos.sh`. Expects the bundle laid out as siblings:
#     <base>/dnd-dm/ and <base>/DndAgent/frontend/
# It rebuilds the platform-specific bits (Python venv, node_modules), points the UI at this
# Mac's Tailscale IP, and installs launchd services so both servers auto-start at login and
# restart on crash. Idempotent — safe to re-run.
set -euo pipefail

DND="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # .../dnd-dm
BASE="$(dirname "$DND")"
FE="$BASE/DndAgent/frontend"

command -v python3 >/dev/null || { echo "Install Python 3.12+ first (brew install python@3.12)"; exit 1; }
command -v node    >/dev/null || { echo "Install Node first (brew install node)"; exit 1; }
command -v tailscale >/dev/null || { echo "Install Tailscale first and log in"; exit 1; }
TS_IP="$(tailscale ip -4 | head -1)"

echo "▸ dnd-dm:   $DND"
echo "▸ frontend: $FE"
echo "▸ this Mac's Tailscale IP: $TS_IP"
[ -d "$FE" ] || { echo "Frontend not found at $FE — check the bundle layout."; exit 1; }

echo "▸ Python venv + deps…"
cd "$DND"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install -r requirements.txt

echo "▸ Frontend deps + API URL…"
cd "$FE"
printf 'NEXT_PUBLIC_API_URL=http://%s:8000\n' "$TS_IP" > .env.local
npm install

echo "▸ Installing launchd services…"
LA="$HOME/Library/LaunchAgents"; mkdir -p "$LA"

cat > "$LA/com.dnd.backend.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.dnd.backend</string>
  <key>ProgramArguments</key><array>
    <string>$DND/.venv/bin/uvicorn</string>
    <string>web.adapter:app</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8000</string>
  </array>
  <key>WorkingDirectory</key><string>$DND</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DND/data/backend.log</string>
  <key>StandardErrorPath</key><string>$DND/data/backend.log</string>
</dict></plist>
EOF

cat > "$LA/com.dnd.frontend.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.dnd.frontend</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>-lc</string>
    <string>cd "$FE" && exec npx next dev -H 0.0.0.0 -p 3000</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DND/data/frontend.log</string>
  <key>StandardErrorPath</key><string>$DND/data/frontend.log</string>
</dict></plist>
EOF

launchctl unload "$LA/com.dnd.backend.plist"  2>/dev/null || true
launchctl unload "$LA/com.dnd.frontend.plist" 2>/dev/null || true
launchctl load "$LA/com.dnd.backend.plist"
launchctl load "$LA/com.dnd.frontend.plist"

echo
echo "✅ Done. Both services are running and will auto-start at login + restart on crash."
echo "   Play at:  http://$TS_IP:3000/table   (from any device on your tailnet)"
echo
echo "Keep the Mac awake (so it stays a server even with the lid/display off):"
echo "   sudo pmset -a sleep 0 disablesleep 1     # or System Settings ▸ Energy ▸ 'Prevent sleeping'"
echo "Logs:   tail -f $DND/data/backend.log   $DND/data/frontend.log"
echo "Stop:   launchctl unload $LA/com.dnd.backend.plist $LA/com.dnd.frontend.plist"
