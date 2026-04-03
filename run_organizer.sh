#!/bin/bash
# run_organizer.sh
# Desktop organizer launcher with lock file to prevent concurrent execution.

LOCK_FILE="/tmp/com.hexa.desktop.organizer.lock"
LOG_DIR="/Users/hexa/Library/Logs/DesktopOrganizer"
LOG_FILE="${LOG_DIR}/organizer.log"
PYTHON="/usr/bin/python3"
SCRIPT="/Users/hexa/Python/smart_organize.py"
TARGET_DIR="/Users/hexa/Desktop"

# Create log directory if it doesn't exist
mkdir -p "${LOG_DIR}"

# Timestamp function
timestamp() {
    date "+%Y-%m-%d %H:%M:%S"
}

# Prevent concurrent execution using a lock file
if [ -e "${LOCK_FILE}" ]; then
    PID=$(cat "${LOCK_FILE}" 2>/dev/null)
    if [ -n "${PID}" ] && kill -0 "${PID}" 2>/dev/null; then
        echo "$(timestamp) [SKIP] Already running (PID=${PID}). Exiting." >> "${LOG_FILE}"
        exit 0
    else
        echo "$(timestamp) [INFO] Stale lock file found. Removing." >> "${LOG_FILE}"
        rm -f "${LOCK_FILE}"
    fi
fi

# Write current PID to lock file
echo $$ > "${LOCK_FILE}"

# Ensure lock file is removed on exit (normal or error)
trap 'rm -f "${LOCK_FILE}"' EXIT

echo "$(timestamp) [START] Running smart_organize.py" >> "${LOG_FILE}"

"${PYTHON}" "${SCRIPT}" --target-dir "${TARGET_DIR}" --verbose >> "${LOG_FILE}" 2>&1
EXIT_CODE=$?

if [ ${EXIT_CODE} -eq 0 ]; then
    echo "$(timestamp) [DONE] Finished successfully (exit=${EXIT_CODE})" >> "${LOG_FILE}"
else
    echo "$(timestamp) [ERROR] Finished with error (exit=${EXIT_CODE})" >> "${LOG_FILE}"
fi

exit ${EXIT_CODE}
