#!/usr/bin/env bash
# Hourly cron wrapper for the Search term automation (search_term_Automation -> gen_ai_db).
#
#  * resolves the project directory from this script's own location (absolute);
#  * uses the project's virtualenv interpreter;
#  * passes the protected .env explicitly (python-dotenv loads it -- the file is
#    never sourced by the shell, so passwords with $ or quotes are safe);
#  * takes a non-blocking flock so an overrunning run makes the next one exit
#    instead of overlapping;
#  * exits with the Python process's status, so failures are nonzero.
#
# Which report(s) run is decided by the application in the calendar timezone
# (AMAZON_ACCOUNT_TIMEZONE); the server's and cron's timezone are irrelevant.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="${SEARCH_TERM_PYTHON:-$PROJECT_DIR/.venv/bin/python}"
ENV_FILE="${SEARCH_TERM_ENV_FILE:-$PROJECT_DIR/.env}"
LOCK_FILE="$PROJECT_DIR/data/cron.lock"

stamp() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ($(TZ=Asia/Kolkata date +%H:%M) IST)"; }

if [[ ! -x "$PYTHON" ]]; then
    echo "$(stamp) ERROR python interpreter not found or not executable: $PYTHON" >&2
    exit 2
fi
if [[ ! -f "$ENV_FILE" ]]; then
    echo "$(stamp) ERROR env file not found: $ENV_FILE" >&2
    exit 2
fi
perms="$(stat -c %a "$ENV_FILE" 2>/dev/null || echo 600)"
if [[ "${perms: -2}" != "00" ]]; then
    echo "$(stamp) WARNING $ENV_FILE is readable by group/others (mode $perms); run: chmod 600 $ENV_FILE" >&2
fi

mkdir -p "$PROJECT_DIR/data" "$PROJECT_DIR/logs"
cd "$PROJECT_DIR" || exit 2

# fd 9 holds the lock until this process (and the Python child) exits; the
# kernel releases it even if the run is killed.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(stamp) SKIPPED another search term automation run is still in progress (lock $LOCK_FILE)" >&2
    exit 11
fi

echo "$(stamp) START search term automation (pid $$)"
"$PYTHON" "$PROJECT_DIR/run.py" --env-file "$ENV_FILE" "$@"
status=$?
if [[ $status -eq 0 ]]; then
    echo "$(stamp) END status=0 SUCCESS"
else
    echo "$(stamp) END status=$status FAILED" >&2
fi
exit $status
