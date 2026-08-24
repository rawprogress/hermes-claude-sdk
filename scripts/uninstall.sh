#!/bin/sh
# Stop and remove the LaunchAgent. The database, worktrees and logs are
# preserved on purpose; this script never deletes them.
#
# HERMES_CLAUDE_RUNNER_LABEL selects the LaunchAgent label; set it to an older
# label to remove an install made before the default was renamed.
set -eu

LABEL="${HERMES_CLAUDE_RUNNER_LABEL:-com.hermes-claude-sdk.runner}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WRAPPER="$HOME/.local/bin/hermes-claude-runner"
DATA_DIR="${HERMES_CLAUDE_RUNNER_HOME:-$HOME/Library/Application Support/HermesClaudeRunner}"
PROJECTS_ROOT="${HERMES_CLAUDE_RUNNER_PROJECTS_ROOT:-$HOME/Projects}"
WORKTREES="${HERMES_CLAUDE_RUNNER_WORKTREES_ROOT:-$PROJECTS_ROOT/.hermes-claude-worktrees}"
LOG_DIR="${HERMES_CLAUDE_RUNNER_LOG_DIR:-$HOME/Library/Logs/HermesClaudeRunner}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

echo "==> Stopping the LaunchAgent $LABEL"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

[ -f "$PLIST" ] && mv "$PLIST" "$PLIST.removed-$STAMP" && echo "    moved $PLIST"
[ -f "$WRAPPER" ] && mv "$WRAPPER" "$WRAPPER.removed-$STAMP" && echo "    moved $WRAPPER"

cat <<TXT

Preserved on purpose — delete by hand only if you really mean it:
  database : $DATA_DIR/data.db
  worktrees: $WORKTREES
  logs     : $LOG_DIR
TXT
