"""Start/status/normal stop for one local PAPER observer. No keys or orders."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from binance_coinm_v1.runtime.instance_lock import InstanceLock
from .forward import CASES, ForwardRunner, _atomic_json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / 'btc_lab/state/pc_momentum60'


def running(directory):
    lock = InstanceLock(directory / 'instance.lock')
    try:
        lock.acquire()
    except RuntimeError:
        return True
    else:
        lock.release()
        return False


def status(directory):
    active = running(directory)
    result = {'mode': 'PAPER_BAR_CLOSE_REPLAY', 'orders_enabled': False,
              'running_lock_held': active, 'state_dir': str(directory)}
    path = directory / 'state.json'
    if path.exists():
        # Read an atomic snapshot; a heartbeat alone is not evidence of a process.
        state = json.loads(path.read_text(encoding='utf-8'))
        age = max(0, time.time() - state['last_refresh_server_ms']/1000)
        observation = state['observation'] or {}
        result.update(candidate=state['manifest']['candidate'], heartbeat_age_sec=round(age, 1),
                      healthy=active and age < 300,
                      activation_utc=datetime.fromtimestamp(state['activation_time'], timezone.utc).isoformat(),
                      equity_btc=observation.get('equity_btc', state['manifest']['initial_btc']),
                      observed_hours=len(state['bars']),
                      position_contracts=observation.get('position_contracts', 0),
                      ledger_events=len(observation.get('ledger', [])))
    else:
        result.update(healthy=False, status='initializing' if active else 'not_started')
    return result


def start(directory, equity, fee, candidate, maintenance, wait_sec=15):
    # Validate parameters and any prior ledger before launching another process.
    runner = ForwardRunner(directory, equity, fee, candidate, maint_margin_rate=maintenance)
    if running(directory):
        raise RuntimeError('This paper state already has an active process')
    runner._load()
    stop_path = directory / 'stop.request'
    stop_path.unlink(missing_ok=True)
    command = [sys.executable, '-u', '-m', 'btc_lab.forward', '--equity', str(equity),
               '--fee', str(fee), '--candidate', candidate, '--maint-margin-rate', str(maintenance),
               '--state-dir', str(directory), '--stop-file', str(stop_path)]
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    log_path = directory / 'observer.log'
    with log_path.open('ab', buffering=0) as log:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, **options)
    _atomic_json(directory / 'control.json', {'pid': process.pid, 'created_utc': datetime.now(timezone.utc).isoformat(),
                                             'command': command, 'mode': 'PAPER'})
    deadline = time.monotonic() + wait_sec
    started = time.time()
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'Paper observer exited with code {process.returncode}; inspect {log_path}')
        if runner.path.exists():
            state = json.loads(runner.path.read_text(encoding='utf-8'))
            if state['last_refresh_server_ms']/1000 >= started - 1 and running(directory):
                break
        time.sleep(.2)
    return {'started_pid': process.pid, 'log': str(log_path), **status(directory)}


def stop(directory, wait_sec=20):
    if not running(directory):
        return {**status(directory), 'status': 'already_stopped'}
    (directory / 'stop.request').write_text(datetime.now(timezone.utc).isoformat(), encoding='utf-8')
    deadline = time.monotonic() + wait_sec
    while running(directory) and time.monotonic() < deadline:
        time.sleep(.2)
    return {**status(directory), 'status': 'stop_pending' if running(directory) else 'stopped_normally'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'status', 'stop'))
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--equity', type=float, default=.003, help='PAPER capital only')
    parser.add_argument('--fee', type=float, default=.0005)
    parser.add_argument('--maint-margin-rate', type=float, default=.004)
    parser.add_argument('--candidate', choices=CASES, default='momentum60_stop20')
    args = parser.parse_args()
    directory = args.state_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manager = InstanceLock(directory/'control.lock')
    manager.acquire()
    try:
        if args.action == 'start':
            result = start(directory, args.equity, args.fee, args.candidate, args.maint_margin_rate)
        elif args.action == 'stop':
            result = stop(directory)
        else:
            result = status(directory)
        print(json.dumps(result, ensure_ascii=True, indent=2))
    finally:
        manager.release()


if __name__ == '__main__':
    main()
