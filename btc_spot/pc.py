"""Start, inspect, and normally stop a single local spot runtime."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from binance_coinm_v1.runtime.instance_lock import InstanceLock
from .config import ROOT, DEFAULT_CAPITAL, DEFAULT_CREDENTIALS, LIVE_CONFIRMATION, capital, state_directory
from .runtime import atomic_json, safe_error


def running(directory):
    lock = InstanceLock(Path(directory)/"instance.lock")
    try:
        lock.acquire()
    except RuntimeError:
        return True
    lock.release()
    return False


def status(directory):
    directory = Path(directory)
    result = {"state_dir": str(directory), "running": running(directory), "healthy": False}
    path = directory/"status.json"
    if path.exists():
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        age = max(0, time.time() - snapshot["updated_at_ms"]/1000)
        result.update(snapshot=snapshot, heartbeat_age_sec=round(age, 1),
                      healthy=result["running"] and age < 120 and snapshot.get("status") not in
                      {"ERROR", "BLOCKED", "UNKNOWN", "HALTED"})
    return result


def start(directory, mode, initial_btc, credentials_file=DEFAULT_CREDENTIALS, confirm="", wait_sec=20):
    if mode == "live" and confirm != LIVE_CONFIRMATION:
        raise ValueError("Explicit live confirmation required")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if running(directory):
        raise RuntimeError("This spot runtime is already running")
    (directory/"stop.request").unlink(missing_ok=True)
    command = [sys.executable, "-u", "-m", "btc_spot", "run", "--mode", mode,
               "--state-dir", str(directory), "--initial-btc", str(initial_btc)]
    if mode == "live":
        command += ["--credentials-file", str(Path(credentials_file).resolve()), "--confirm", confirm]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    started = time.time()
    with (directory/"runtime.log").open("ab", buffering=0) as stream:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, **options)
    atomic_json(directory/"control.json", {"pid": process.pid, "mode": mode,
                "started_utc": datetime.now(timezone.utc).isoformat(), "command": command})
    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return {"status": "PROCESS_EXITED", "exit_code": process.returncode, **status(directory)}
        path = directory/"status.json"
        if path.exists() and json.loads(path.read_text(encoding="utf-8"))["updated_at_ms"]/1000 >= started:
            break
        time.sleep(.2)
    return {"started_pid": process.pid, **status(directory)}


def stop(directory, wait_sec=20):
    directory = Path(directory)
    if not running(directory):
        return {"status": "ALREADY_STOPPED", **status(directory)}
    (directory/"stop.request").write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    deadline = time.monotonic() + wait_sec
    while running(directory) and time.monotonic() < deadline:
        time.sleep(.2)
    return {"status": "STOP_PENDING" if running(directory) else "STOPPED", **status(directory)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    parser.add_argument("--mode", choices=("paper", "live"), default="paper")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--initial-btc", type=capital, default=DEFAULT_CAPITAL)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    directory = (args.state_dir or state_directory(args.mode)).resolve()
    lock = InstanceLock(directory/"control.lock")
    lock.acquire()
    try:
        if args.action == "start":
            result = start(directory, args.mode, args.initial_btc, args.credentials_file, args.confirm)
        elif args.action == "stop":
            result = stop(directory)
        else:
            result = status(directory)
        print(json.dumps(result, ensure_ascii=True, indent=2, default=str))
        return 1 if result.get("status") == "PROCESS_EXITED" else 0
    except Exception as exc:
        print(json.dumps({"status": "ERROR", **safe_error(exc)}))
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
