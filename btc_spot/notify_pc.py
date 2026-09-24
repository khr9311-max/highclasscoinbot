"""Control the separate read-only Spot Telegram notifier on this PC."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from binance_coinm_v1.runtime.instance_lock import InstanceLock
from .config import DEFAULT_CREDENTIALS, ROOT, state_directory
from .runtime import safe_error


def running(directory):
    lock = InstanceLock(Path(directory) / 'telegram.lock')
    try:
        lock.acquire()
    except RuntimeError:
        return True
    lock.release()
    return False


def status(directory):
    return {'running': running(directory), 'state_dir': str(directory),
            'log': str(Path(directory) / 'telegram.log')}


def start(directory, credentials_file):
    directory = Path(directory).resolve()
    if not (directory / 'status.json').exists():
        raise ValueError('Live Spot status does not exist')
    if running(directory):
        return {'status': 'ALREADY_RUNNING', **status(directory)}
    (directory / 'telegram.stop').unlink(missing_ok=True)
    command = [sys.executable, '-u', '-m', 'btc_spot.notify', '--state-dir', str(directory),
               '--credentials-file', str(Path(credentials_file).resolve())]
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    with (directory / 'telegram.log').open('ab', buffering=0) as stream:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, **options)
    time.sleep(1)
    return {'status': 'STARTED' if process.poll() is None else 'PROCESS_EXITED',
            'pid': process.pid, **status(directory)}


def stop(directory):
    directory = Path(directory)
    if not running(directory):
        return {'status': 'ALREADY_STOPPED', **status(directory)}
    (directory / 'telegram.stop').write_text('stop', encoding='utf-8')
    deadline = time.monotonic() + 40
    while running(directory) and time.monotonic() < deadline:
        time.sleep(.2)
    return {'status': 'STOPPED' if not running(directory) else 'STOP_PENDING', **status(directory)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'status'))
    parser.add_argument('--state-dir', type=Path, default=state_directory('live'))
    parser.add_argument('--credentials-file', type=Path, default=DEFAULT_CREDENTIALS)
    args = parser.parse_args()
    try:
        directory = args.state_dir.resolve()
        result = (start(directory, args.credentials_file) if args.action == 'start' else
                  stop(directory) if args.action == 'stop' else status(directory))
        print(json.dumps(result, ensure_ascii=True))
        return 1 if result.get('status') == 'PROCESS_EXITED' else 0
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', **safe_error(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
