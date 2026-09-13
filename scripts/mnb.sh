#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Fast, single-step runner for the fixed-snapshot personal build ('mnb').
# Fetches origin once, pins origin/main SHA as cutoff, updates from a clean
# detached worktree, then reopens and verifies the installed personal app.
set -euo pipefail

START_TIME="$(date +%s)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${LIGHTTABLE_PERSONAL_APP:-/Applications/LightTable - NPR Installed.app}"
CLI="$APP/Contents/MacOS/lighttable-cli"
PARENT_DIR="$(cd "$ROOT/.." && pwd)"
WORKTREE_DIR="${LIGHTTABLE_MNB_WORKTREE:-$PARENT_DIR/mnb-worktree}"

echo "=== LightTable Personal Build ('mnb') ==="

# 1. Fetch origin once
echo "[1/6] Fetching origin..."
git -C "$ROOT" fetch origin -q

# 2. Pin origin/main cutoff SHA
CUTOFF_SHA="$(git -C "$ROOT" rev-parse origin/main)"
CUTOFF_SHORT="${CUTOFF_SHA:0:7}"
echo "[2/6] Pinned release cutoff: $CUTOFF_SHA ($CUTOFF_SHORT)"

# 3. Quit running personal app if active
if pgrep -f "$APP/Contents/MacOS/LightTable" >/dev/null 2>&1; then
  echo "[3/6] Quitting running LightTable instance..."
  osascript -e 'tell application id "com.reville.filmlab.nprinstalled" to quit' 2>/dev/null || true
  for _ in {1..10}; do
    if ! pgrep -f "$APP/Contents/MacOS/LightTable" >/dev/null 2>&1; then
      break
    fi
    sleep 0.3
  done
  pkill -9 -f "$APP/Contents/MacOS/LightTable" 2>/dev/null || true
  pkill -9 -f "$APP/Contents/Resources/Python/bin/python3.*server.py" 2>/dev/null || true
else
  echo "[3/6] No running LightTable instance found."
fi

# 4. Setup or sync dedicated clean worktree
echo "[4/6] Synchronizing detached worktree at $CUTOFF_SHORT..."
if [[ ! -d "$WORKTREE_DIR" ]]; then
  git -C "$ROOT" worktree add "$WORKTREE_DIR" "$CUTOFF_SHA" --detach --quiet
else
  git -C "$WORKTREE_DIR" checkout --detach "$CUTOFF_SHA" --quiet
  git -C "$WORKTREE_DIR" reset --hard "$CUTOFF_SHA" --quiet
  git -C "$WORKTREE_DIR" clean -fd --quiet
fi

# Overlay fast updater and smoke improvements if worktree revision does not have them yet
if [[ -f "$ROOT/scripts/update-personal-app.sh" ]]; then
  /usr/bin/ditto "$ROOT/scripts/update-personal-app.sh" "$WORKTREE_DIR/scripts/update-personal-app.sh"
fi
if [[ -f "$ROOT/scripts/native-app-smoke.py" ]]; then
  /usr/bin/ditto "$ROOT/scripts/native-app-smoke.py" "$WORKTREE_DIR/scripts/native-app-smoke.py"
fi

# 5. Build and install update
echo "[5/6] Building and installing personal update..."
(
  cd "$WORKTREE_DIR"
  PYTHONDONTWRITEBYTECODE=1 bash scripts/update-personal-app.sh
)

# 6. Reopen app and verify
echo "[6/6] Launching installed app and verifying health..."
open "$APP"

# Wait for local server and discover port via bundled CLI
PORT=""
API_OK=false
HTTP_STATUS=""
for _ in {1..30}; do
  STATUS_JSON="$("$CLI" status 2>/dev/null || true)"
  if [[ -n "$STATUS_JSON" ]] && echo "$STATUS_JSON" | grep -q '"ok":[[:space:]]*true'; then
    PORT="$(echo "$STATUS_JSON" | /usr/bin/sed -n 's/.*"port":[[:space:]]*\([0-9]*\).*/\1/p' | head -1)"
    if [[ -n "$PORT" ]]; then
      HTTP_STATUS="$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
      if [[ "$HTTP_STATUS" == "200" ]]; then
        API_OK=true
        break
      fi
    fi
  fi
  sleep 0.5
done

# Verify signature
CODESIGN_OUTPUT="$(/usr/bin/codesign --verify --deep --strict "$APP" 2>&1 || echo "INVALID")"
SIGNATURE_OK=false
if [[ -z "$CODESIGN_OUTPUT" ]]; then
  SIGNATURE_OK=true
fi

# Verify source revision in plist
INSTALLED_REV="$(/usr/libexec/PlistBuddy -c 'Print :LightTableSourceRevision' "$APP/Contents/Info.plist" 2>/dev/null || echo unknown)"
INSTALLED_VER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist" 2>/dev/null || echo unknown)"

APP_PID="$(pgrep -f "$APP/Contents/MacOS/LightTable" | head -1 || echo "")"
SERVER_PID="$(pgrep -f "$APP/Contents/Resources/Python/bin/python3.*server.py" | head -1 || echo "")"

END_TIME="$(date +%s)"
DURATION=$((END_TIME - START_TIME))

echo ""
echo "=== 'mnb' Verification Summary ==="
echo "Cutoff SHA:          $CUTOFF_SHA"
echo "Installed Revision:  $INSTALLED_REV"
echo "Installed Version:   $INSTALLED_VER"
echo "Signature:           $([ "$SIGNATURE_OK" = true ] && echo "VALID" || echo "FAILED ($CODESIGN_OUTPUT)")"
echo "Native App PID:      ${APP_PID:-NOT RUNNING}"
echo "Server PID:          ${SERVER_PID:-NOT RUNNING}"
echo "Discovered Port:     ${PORT:-UNKNOWN}"
echo "Local API:           $([ "$API_OK" = true ] && echo "HTTP 200 (healthy on port $PORT)" || echo "HTTP ${HTTP_STATUS:-FAILED}")"
echo "Total Elapsed Time:  ${DURATION}s"

if [[ "$INSTALLED_REV" == "$CUTOFF_SHA" && "$SIGNATURE_OK" == true && "$API_OK" == true ]]; then
  echo "Result: SUCCESS"
  exit 0
else
  echo "Result: VERIFICATION FAILED" >&2
  exit 1
fi
