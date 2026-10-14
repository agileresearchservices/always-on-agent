#!/bin/sh
# Install (or remove) the four aoa services as launchd user agents.
#   deploy/install-launchd.sh install     write plists to ~/Library/LaunchAgents and start them
#   deploy/install-launchd.sh uninstall   stop them and remove the plists
#   deploy/install-launchd.sh status
# Stop any hand-started aoa processes first (they would hold ports 7233 and 8000).
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="${AOA_DATA_DIR:-$ROOT/data}/logs"
SERVICES="temporal worker server slack-bridge"   # start order; worker/server retry until Temporal is up
UID_=$(id -u)
case "${1:-}" in
  install)
    [ -f "$ROOT/.env" ] || { echo ".env missing: copy .env.example and fill it in" >&2; exit 1; }
    mkdir -p "$AGENTS" "$LOGS"
    for s in $SERVICES; do
      f="$AGENTS/com.aoa.$s.plist"
      sed -e "s|__SERVICE__|$s|g" -e "s|__ROOT__|$ROOT|g" -e "s|__LOGS__|$LOGS|g" "$ROOT/deploy/launchd/plist.template" > "$f"
      plutil -lint "$f" >/dev/null
      launchctl bootout "gui/$UID_/com.aoa.$s" 2>/dev/null || true
      launchctl bootstrap "gui/$UID_" "$f"
      echo "started com.aoa.$s"
    done ;;
  uninstall)
    for s in $SERVICES; do
      launchctl bootout "gui/$UID_/com.aoa.$s" 2>/dev/null || true
      rm -f "$AGENTS/com.aoa.$s.plist"; echo "removed com.aoa.$s"
    done ;;
  status)
    for s in $SERVICES; do
      printf '%-24s' "com.aoa.$s"; launchctl print "gui/$UID_/com.aoa.$s" 2>/dev/null | grep -E "^\s+(state|pid) =" | tr -s ' \n' ' '; echo
    done ;;
  *) echo "usage: $0 install|uninstall|status" >&2; exit 2 ;;
esac
