#!/bin/sh
# Run this ON THE HERMES HOST. Pulls the plugin from the Mac into
# ~/.hermes/plugins/ and validates it with Hermes' own plugin doctor.
# Copies only; deletes nothing.
#
# Override any of these before running:
#   MAC_HOST    ssh destination of the Mac                 (default: macbook)
#   MAC_REPO    checkout path on the Mac, relative to its
#               home unless it starts with /               (default: Projects/hermes-claude-sdk)
#   REMOTE_CMD  runner wrapper on the Mac                  (default: ~/.local/bin/hermes-claude-runner)
set -eu

MAC_HOST="${MAC_HOST:-macbook}"
MAC_REPO="${MAC_REPO:-Projects/hermes-claude-sdk}"
# Single quotes on purpose: the tilde must survive this shell and be expanded
# by the Mac's login shell, so the script works for any macOS account.
if [ -z "${REMOTE_CMD:-}" ]; then
    REMOTE_CMD='~/.local/bin/hermes-claude-runner'
fi
TARGET="$HOME/.hermes/plugins/hermes-claude-sdk"

echo "==> Copying the plugin from $MAC_HOST:$MAC_REPO"
mkdir -p "$HOME/.hermes/plugins"
if [ -d "$TARGET" ]; then
    BACKUP="$TARGET.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    echo "    existing install backed up to $BACKUP"
    cp -a "$TARGET" "$BACKUP"
fi
mkdir -p "$TARGET"
scp -r "$MAC_HOST:$MAC_REPO/hermes_plugin/hermes-claude-sdk/." "$TARGET/"
find "$TARGET" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

echo "==> Validating with the Hermes plugin loader"
# Keep the output: it already reports the registrations, so it is also the
# only verification here that can genuinely fail.
DOCTOR_OUTPUT="$(hermes plugins doctor "$TARGET" --ci)"
printf '%s\n' "$DOCTOR_OUTPUT"

echo "==> Verifying the seven tools are registered"
# `hermes tools` needs a terminal and refuses to run through a pipe, so the
# check it used to perform always printed 0 under a heading claiming success.
printf '%s' "$DOCTOR_OUTPUT" | grep -q "7 tool(s)"
echo "    7 tool(s) registered"

echo "==> Enabling the plugin"
# Hermes may ask whether the plugin can replace built-in tools; it defaults to
# no, which is correct here — this plugin adds tools and replaces none.
hermes plugins enable hermes-claude-sdk || \
    echo "    enable it manually: add 'hermes-claude-sdk' to plugins.enabled in ~/.hermes/config.yaml"

echo "==> Checking the ssh path to the Mac"
ssh -o BatchMode=yes "$MAC_HOST" "$REMOTE_CMD" health

echo "==> Done."
