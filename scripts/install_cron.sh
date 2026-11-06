#!/usr/bin/env bash
# Verify the deployment, then install (or print) the hourly cron entry.
#
#   scripts/install_cron.sh           # verify + install into the current user's crontab
#   scripts/install_cron.sh --print   # verify + print the entry only
#
# Every path in the entry is derived on this server -- nothing is assumed.
# Re-running replaces the previous entry (matched by its marker comment).

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
WRAPPER="$PROJECT_DIR/scripts/run_search_term_automation.sh"
ENV_FILE="$PROJECT_DIR/.env"
CRON_LOG="$PROJECT_DIR/logs/search_term_cron.log"
MARKER="# search-term-automation: search_term_Automation -> gen_ai_db.searchtermreport"
# Runs every hour at minute 45 IST (12:45, 13:45, ... Asia/Kolkata): 15 minutes
# after the Targeting job, so the two browsers do not start together.  Cron
# reads the schedule on the SERVER's clock, so the IST minute is converted
# below: minute 15 on a UTC server, 45 on an IST one.
# Override with CRON_SCHEDULE="..." (server time) if needed.
CRON_TIMEZONE="${CRON_TIMEZONE:-Asia/Kolkata}"
CRON_MINUTE="${CRON_MINUTE:-45}"
# What each hourly run does: --load runs search_term_Automation (period Today),
# plus the optional Yesterday report in the first two runs of the US day,
# saves the CSV in data/reports/ and replaces its dates/marketplaces in
# gen_ai_db.searchtermreport.
# CRON_RUN_ARGS="--save-reports" keeps files only (no MySQL).
RUN_ARGS="--save-reports" keeps files only (no MySQL).
RUN_ARGS="${CRON_RUN_ARGS---load}"

fail() { echo "ERROR: $*" >&2; exit 1; }
ok() { echo "  ok  $*"; }

# UTC offset in minutes of a timezone ("" = the server's own), e.g. +0530 -> 330.
offset_minutes() {
    local z
    if [[ -n "$1" ]]; then z="$(TZ="$1" date +%z)"; else z="$(date +%z)"; fi
    local total=$(( 10#${z:1:2} * 60 + 10#${z:3:2} ))
    [[ "${z:0:1}" == "-" ]] && total=$(( -total ))
    echo "$total"
}

[[ -f "/usr/share/zoneinfo/$CRON_TIMEZONE" ]] || fail "unknown timezone $CRON_TIMEZONE (install tzdata)"
# Whole-hour DST changes on the server do not move the minute, so it is stable.
SERVER_MINUTE=$(( ((CRON_MINUTE - ($(offset_minutes "$CRON_TIMEZONE") - $(offset_minutes "")) ) % 60 + 60) % 60 ))
SCHEDULE="${CRON_SCHEDULE-$SERVER_MINUTE * * * *}"

echo "Verifying deployment in $PROJECT_DIR"
[[ -f "$PROJECT_DIR/run.py" ]] || fail "entry point missing: $PROJECT_DIR/run.py"
ok "entry point $PROJECT_DIR/run.py"
[[ -x "$PYTHON" ]] || fail "virtualenv interpreter missing: $PYTHON (create it: uv sync --no-dev)"
"$PYTHON" -c "import playwright, pandas, sqlalchemy, mysql.connector, dotenv" \
    || fail "dependencies not installed in $PYTHON"
ok "interpreter $PYTHON ($("$PYTHON" --version 2>&1))"
[[ -x "$WRAPPER" ]] || fail "wrapper not executable: chmod +x $WRAPPER"
ok "wrapper $WRAPPER"
[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE (cp .env.example .env && chmod 600 .env)"
[[ "$(stat -c %a "$ENV_FILE")" == "600" ]] || fail "$ENV_FILE must be mode 600: chmod 600 $ENV_FILE"
ok "env file $ENV_FILE (mode 600)"
command -v flock >/dev/null || fail "flock not found (install util-linux)"
ok "flock $(command -v flock)"
"$PYTHON" "$PROJECT_DIR/run.py" --env-file "$ENV_FILE" --check-config --no-lock >/dev/null \
    || fail "configuration check failed; run: $PYTHON $PROJECT_DIR/run.py --check-config"
ok "configuration and report IDs"
if [[ -z "${CRON_SCHEDULE+x}" ]]; then
    ok "schedule: every hour at :$(printf %02d "$CRON_MINUTE") $CRON_TIMEZONE = minute $SERVER_MINUTE on this server's clock ($(date +%Z), UTC$(date +%z))"
fi

ENTRY="$SCHEDULE $WRAPPER${RUN_ARGS:+ $RUN_ARGS} >> $CRON_LOG 2>&1"

echo
echo "Cron entry:"
echo "  $MARKER"
echo "  $ENTRY"

if [[ "${1:-}" == "--print" ]]; then
    exit 0
fi

current="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$current" | awk -v m="$MARKER" '
    $0 == m { skip = 1; next }
    skip == 1 { skip = 0; next }
    { print }')"
printf '%s\n%s\n%s\n' "$filtered" "$MARKER" "$ENTRY" | sed '/^$/N;/^\n$/D' | crontab -
echo
echo "Installed for user $(id -un). Inspect with: crontab -l"
