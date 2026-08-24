#!/bin/sh
# Install the Mac runner: verify, provision the managed runtime, write the
# wrapper and LaunchAgent, start it. Idempotent, never deletes state.
# Run from the repository root.
#
# This checkout is only the *source* of the install. The service runs from a
# managed runtime under the runner's own data directory, so the checkout can
# be moved or deleted afterwards without stopping the daemon.
#
#   ./scripts/install_runner.sh                 human install
#   ./scripts/install_runner.sh --dry-run       show what would change, change nothing
#   ./scripts/install_runner.sh --json          one JSON object on stdout, progress on stderr
#   ./scripts/install_runner.sh --skip-verify   trust an already-green suite
#
# HERMES_CLAUDE_RUNNER_LABEL selects the LaunchAgent label; set it to an older
# label to manage an install made before the default was renamed.
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="${HERMES_CLAUDE_RUNNER_LABEL:-com.hermes-claude-sdk.runner}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WRAPPER="$HOME/.local/bin/hermes-claude-runner"

DRY_RUN=0
JSON=0
VERIFY=1

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --json) JSON=1 ;;
        --skip-verify) VERIFY=0 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# In --json mode stdout carries exactly one object, so progress goes to stderr.
say() {
    if [ "$JSON" -eq 1 ]; then printf '%s\n' "$*" >&2; else printf '%s\n' "$*"; fi
}

cd "$REPO_ROOT"

say "==> Syncing dependencies"
uv sync --all-groups >&2

if [ "$VERIFY" -eq 1 ]; then
    say "==> Verifying (tests, lint, typecheck)"
    uv run pytest -q >&2
    uv run ruff check . >&2
    uv run mypy >&2
else
    say "==> Skipping verification (--skip-verify)"
fi

say "==> Provisioning the managed runtime, wrapper and LaunchAgent plist"
if [ "$DRY_RUN" -eq 1 ]; then
    INSTALL_REPORT="$(uv run hermes-claude-runner install --dry-run --repo-root "$REPO_ROOT")"
else
    INSTALL_REPORT="$(uv run hermes-claude-runner install --repo-root "$REPO_ROOT")"
fi
[ "$JSON" -eq 1 ] || printf '%s\n' "$INSTALL_REPORT"

HEALTH=null
OK=true
if [ "$DRY_RUN" -eq 1 ]; then
    say "==> Dry run: the LaunchAgent was not touched"
    say "    load it with: launchctl bootstrap gui/\$(id -u) $PLIST"
else
    say "==> (Re)loading the LaunchAgent $LABEL"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl kickstart -k "gui/$(id -u)/$LABEL"

    say "==> Waiting for the daemon"
    i=0
    while [ "$i" -lt 50 ]; do
        if "$WRAPPER" health >/dev/null 2>&1; then break; fi
        sleep 0.2
        i=$((i + 1))
    done

    say "==> Service state"
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | sed -n '1,12p' >&2 || true

    say "==> RPC health"
    # The wrapper prints its own envelope *and* exits non-zero when the daemon
    # is down, so its exit status decides the verdict while its stdout is used
    # verbatim. Appending a second envelope here would produce output no
    # caller could parse.
    if HEALTH="$("$WRAPPER" health 2>/dev/null)"; then
        :
    else
        OK=false
    fi
    if [ -z "$HEALTH" ]; then
        HEALTH='{"ok":false,"error":"daemon_unavailable","detail":"the wrapper produced no answer"}'
        OK=false
    fi

    say "==> Checking the install outlives this checkout"
    # The installed wrapper must name the managed runtime and nothing in this
    # tree: a wrapper that points back here stops working the moment the
    # checkout is moved or deleted. Recorded in the verdict rather than
    # exited on, so --json still prints exactly one object.
    if grep -qF "$REPO_ROOT" "$WRAPPER" 2>/dev/null; then
        say "    the installed wrapper still names $REPO_ROOT"
        OK=false
    fi
    [ "$JSON" -eq 1 ] || printf '%s\n' "$HEALTH"
fi

say "==> Done. Logs: $HOME/Library/Logs/HermesClaudeRunner/"
[ "$DRY_RUN" -eq 1 ] || say "    The runtime is installed; running it no longer needs this checkout."

if [ "$JSON" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then DRY=true; else DRY=false; fi
    printf '{"ok":%s,"dry_run":%s,"label":"%s","plist":"%s","wrapper":"%s","install":%s,"health":%s}\n' \
        "$OK" "$DRY" "$LABEL" "$PLIST" "$WRAPPER" "$INSTALL_REPORT" "$HEALTH"
fi

# An install whose daemon never answered is not a successful install.
[ "$OK" = true ] || exit 1
