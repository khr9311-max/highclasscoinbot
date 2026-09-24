import pytest

from btc_lab.pc import running, start, status, stop
from binance_coinm_v1.runtime.instance_lock import InstanceLock
from btc_lab.forward import main as forward_main


def test_duplicate_start_refused_without_spawning_process(tmp_path, monkeypatch):
    lock = InstanceLock(tmp_path/'instance.lock')
    lock.acquire()
    monkeypatch.setattr('btc_lab.pc.subprocess.Popen', lambda *a, **kw: pytest.fail('must not spawn'))
    try:
        assert running(tmp_path)
        with pytest.raises(RuntimeError, match='active process'):
            start(tmp_path, .003, .0005, 'momentum60_stop20', .004)
    finally:
        lock.release()
    assert not running(tmp_path)


def test_stop_only_writes_normal_shutdown_request(tmp_path):
    lock = InstanceLock(tmp_path/'instance.lock')
    lock.acquire()
    try:
        result = stop(tmp_path, wait_sec=0)
        assert result['status'] == 'stop_pending'
        assert (tmp_path/'stop.request').exists()
        assert result['running_lock_held']
    finally:
        lock.release()
    assert stop(tmp_path)['status'] == 'already_stopped'


def test_no_heartbeat_is_not_a_healthy_process(tmp_path):
    assert not status(tmp_path)['healthy']


def test_preexisting_stop_request_causes_no_network_or_state_write(tmp_path, monkeypatch):
    stop_file = tmp_path/'stop.request'
    stop_file.touch()
    monkeypatch.setattr('btc_lab.forward.PublicClient.get', lambda *a, **kw: pytest.fail('no network'))
    result = forward_main(['--equity', '.003', '--fee', '.0005', '--state-dir', str(tmp_path),
                          '--once', '--stop-file', str(stop_file)])
    assert result == 0
    assert not (tmp_path/'state.json').exists()
