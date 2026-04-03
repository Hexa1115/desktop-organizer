#!/usr/bin/env python3
"""
run_organizer.py
Wrapper for smart_organize.py with lock file and logging.
Called directly by launchd so that python3.9 is the TCC-responsible process.

--all-dirs を渡すことで Desktop + Downloads の両方を処理する。
"""
import os
import sys
import subprocess
import datetime

LOCK_FILE = "/tmp/com.hexa.desktop.organizer.lock"
LOG_DIR   = os.path.expanduser("~/Library/Logs/DesktopOrganizer")
LOG_FILE  = os.path.join(LOG_DIR, "organizer.log")
PYTHON    = sys.executable
SCRIPT    = os.path.join(os.path.dirname(__file__), "smart_organize.py")


def ts():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts()} {msg}\n")


def acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            pid = int(open(LOCK_FILE).read().strip())
            os.kill(pid, 0)           # raises if process is gone
            log(f"[SKIP] Already running (PID={pid}). Exiting.")
            return False
        except (ProcessLookupError, ValueError):
            log("[INFO] Stale lock file found. Removing.")
            os.remove(LOCK_FILE)
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


def main():
    if not acquire_lock():
        sys.exit(0)
    try:
        log("[START] Running smart_organize.py --all-dirs")
        result = subprocess.run(
            [PYTHON, SCRIPT, "--all-dirs", "--verbose"],
            stdout=open(LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
        )
        if result.returncode == 0:
            log(f"[DONE] Finished successfully (exit={result.returncode})")
        else:
            log(f"[ERROR] Finished with error (exit={result.returncode})")
        sys.exit(result.returncode)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
