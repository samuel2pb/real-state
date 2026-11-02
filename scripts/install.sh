#!/usr/bin/env bash
# rental-finder installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/samuel2pb/real-state/main/scripts/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --ref my-branch --no-init
#
# Options:
#   --ref <branch>   branch or tag to install (default: main)
#   --no-init        skip the `rental-finder init` wizard
# Environment:
#   RENTAL_FINDER_HOME   where to clone (default: $HOME/rental-finder)
set -euo pipefail

REPO_URL="https://github.com/samuel2pb/real-state.git"
REF="main"
RUN_INIT=1
DEST="${RENTAL_FINDER_HOME:-$HOME/rental-finder}"

while [ $# -gt 0 ]; do
  case "$1" in
    --ref)
      [ $# -ge 2 ] || { echo "error: --ref needs a value" >&2; exit 2; }
      REF="$2"; shift 2 ;;
    --ref=*) REF="${1#--ref=}"; shift ;;
    --no-init) RUN_INIT=0; shift ;;
    -h|--help) sed -n '2,12p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "error: unknown option $1 (use --ref <branch> or --no-init)" >&2; exit 2 ;;
  esac
done

say() { printf '==> %s\n' "$*"; }

if ! command -v git >/dev/null 2>&1; then
  echo "error: git is required but was not found on PATH." >&2
  echo "       macOS: run 'xcode-select --install'. Debian/Ubuntu: 'sudo apt install git'." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (https://astral.sh/uv)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# The uv installer drops the binary here; make it visible for the rest of this script.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv was installed but is not on PATH; open a new shell and re-run." >&2
  exit 1
fi

if [ -d "$DEST/.git" ]; then
  say "Updating $DEST ($REF)"
  git -C "$DEST" fetch --quiet origin "$REF"
  git -C "$DEST" checkout --quiet "$REF"
  git -C "$DEST" pull --ff-only --quiet origin "$REF"
elif [ -e "$DEST" ]; then
  echo "error: $DEST exists and is not a git checkout; move it or set RENTAL_FINDER_HOME." >&2
  exit 1
else
  say "Cloning $REPO_URL ($REF) into $DEST"
  git clone --quiet --branch "$REF" "$REPO_URL" "$DEST"
fi

cd "$DEST"
say "Installing dependencies (uv sync)"
uv sync

# `curl ... | bash` makes stdin the script itself, so a TTY on stdin is rare;
# borrow the controlling terminal when there is one.
if [ "$RUN_INIT" -eq 1 ] && [ -f .env ]; then
  say ".env already exists; skipping the wizard (re-run: uv run rental-finder init --force)"
elif [ "$RUN_INIT" -eq 1 ] && [ -t 0 ]; then
  say "Starting the setup wizard"
  uv run rental-finder init
elif [ "$RUN_INIT" -eq 1 ] && (exec < /dev/tty) 2>/dev/null; then
  say "Starting the setup wizard"
  uv run rental-finder init < /dev/tty
elif [ "$RUN_INIT" -eq 1 ]; then
  say "No terminal attached; run the wizard yourself:"
  echo "    cd \"$DEST\" && uv run rental-finder init"
fi

cat <<EOF

rental-finder is installed in $DEST

Next steps:
  cd "$DEST"
  uv run rental-finder init                 # if you skipped it: writes .env, creates the Notion databases
  uv run rental-finder doctor               # checks .env, Notion access, uv / claude on PATH
  uv run rental-finder run-once             # one rent cycle now (run-buy-once for buy)
  uv run rental-finder install-scheduler    # run it on SCHEDULE_CRON_TIMES via cron / launchd
EOF
