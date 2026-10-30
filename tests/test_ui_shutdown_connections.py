"""Real Uvicorn process with actual LoopLab SSE and agent long poll on isolated roots."""
import http.client
from concurrent.futures import ThreadPoolExecutor
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token


def _port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _start(root, port, mode, log):
    code = '''
import os, sys, time
from pathlib import Path
from looplab.engine import resources
resources.default_gpu_host_lease_path = lambda: Path(sys.argv[1]) / "fixture-gpu.lock"
from looplab.serve.server import make_app, serve
if sys.argv[3] in ("slow", "very_slow"):
    from looplab.events.eventstore import EventStore
    original = EventStore.append
    def delayed(self, event_type, *args, **kwargs):
        if event_type == "hint":
            (Path(sys.argv[1]) / "command-in-append").write_text("inside command transaction")
            time.sleep(12 if sys.argv[3] == "very_slow" else 2)
        return original(self, event_type, *args, **kwargs)
    EventStore.append = delayed
if sys.argv[3] == "old":
    import uvicorn
    uvicorn.run(make_app(sys.argv[1], bind_host="127.0.0.1"), host="127.0.0.1", port=int(sys.argv[2]), log_level="info")
else:
    serve(sys.argv[1], host="127.0.0.1", port=int(sys.argv[2]))
'''
    env = os.environ.copy()
    env.update(LOOPLAB_UI_ANONYMOUS='1', LOOPLAB_AGENT_EVENTS='0')
    for name in ('LOOPLAB_UI_TOKEN', 'LOOPLAB_UI_REQUIRE_AUTH', 'LOOPLAB_UI_REAP_ON_EXIT', 'JUPYTERHUB_SERVICE_PREFIX', 'JUPYTERHUB_API_TOKEN'):
        env.pop(name, None)
    proc = subprocess.Popen([sys.executable, '-c', code, str(root), str(port), mode], stdout=log, stderr=subprocess.STDOUT, env=env)
    for _ in range(100):
        if proc.poll() is not None:
            raise AssertionError(f'server failed to start: {proc.returncode}')
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/runs', timeout=.3) as response:
                if response.status == 200:
                    return proc
        except OSError:
            time.sleep(.05)
    proc.kill(); proc.wait()
    raise AssertionError('isolated server did not start')


def _open_waiters(port):
    sse = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    sse.request('GET', '/api/runs/demo/events')
    response = sse.getresponse()
    assert response.status == 200 and response.getheader('content-type').startswith('text/event-stream')
    poll = socket.create_connection(('127.0.0.1', port), timeout=3)
    poll.sendall(b'GET /api/agent-events?after=999999&timeout=600 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
    poll.settimeout(.5)
    try:
        data = poll.recv(1)
        raise AssertionError(f'long poll returned before shutdown: {data!r}')
    except TimeoutError:
        pass
    return sse, poll


def _command(port, generation):
    body = json.dumps({'type': 'hint', 'data': {'text': 'synthetic shutdown test'}, 'expected_generation': generation}).encode()
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/runs/demo/commands', data=body,
                                     headers={'Content-Type': 'application/json', 'Idempotency-Key': 'shutdown-fixture'}, method='POST')
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def test_real_sse_and_long_poll_drain_then_idempotent_command_retry(tmp_path):
    rd = tmp_path / 'demo'; rd.mkdir()
    (rd / 'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
    (rd / 'task.snapshot.json').write_text('{"kind":"quadratic","goal":"g","direction":"min"}')
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'demo', 'run_uid': 'fixture', 'task_id': 't', 'goal': 'g', 'direction': 'min'})
    generation = run_generation_token(store.read_all())
    log_path = tmp_path / 'server.log'
    with log_path.open('w') as log:
        port = _port(); proc = _start(tmp_path, port, 'slow', log)
        try:
            sse, poll = _open_waiters(port)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(_command, port, generation)
                for _ in range(100):
                    if (tmp_path / 'command-in-append').exists():
                        break
                    time.sleep(.05)
                assert (tmp_path / 'command-in-append').exists(), 'command did not enter append'
                assert not pending.done(), 'command unexpectedly finished before shutdown'
                started = time.monotonic(); proc.send_signal(signal.SIGTERM)
                before = pending.result(timeout=8)
                assert before['status'] == 'succeeded'
            proc.wait(timeout=17)
            assert time.monotonic() - started < 16
            sse.close(); poll.close()
            assert 'Application shutdown complete' in log_path.read_text()
            assert sum(e.type == 'hint' for e in store.read_all()) == 1
            retry_port = _port(); resumed = _start(tmp_path, retry_port, 'new', log)
            try:
                assert _command(retry_port, generation)['id'] == before['id']
                assert sum(e.type == 'hint' for e in store.read_all()) == 1
            finally:
                resumed.send_signal(signal.SIGTERM); resumed.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()


def test_old_unbounded_default_remains_waiting_with_real_clients(tmp_path):
    rd = tmp_path / 'demo'; rd.mkdir()
    (rd / 'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
    (rd / 'task.snapshot.json').write_text('{"kind":"quadratic","goal":"g","direction":"min"}')
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'demo', 'run_uid': 'fixture', 'task_id': 't', 'goal': 'g', 'direction': 'min'})
    log_path = tmp_path / 'old-server.log'
    with log_path.open('w') as log:
        port = _port(); proc = _start(tmp_path, port, 'old', log)
        try:
            sse, poll = _open_waiters(port)
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=2)
                raise AssertionError('baseline unexpectedly exited while connections stayed open')
            except subprocess.TimeoutExpired:
                pass
            assert 'Waiting for connections to close' in log_path.read_text()
            sse.close(); poll.close()
        finally:
            proc.kill(); proc.wait()


def test_long_transaction_after_deadline_retries_by_original_identity(tmp_path):
    rd = tmp_path / 'demo'; rd.mkdir()
    (rd / 'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
    (rd / 'task.snapshot.json').write_text('{"kind":"quadratic","goal":"g","direction":"min"}')
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'demo', 'run_uid': 'fixture', 'task_id': 't', 'goal': 'g', 'direction': 'min'})
    generation = run_generation_token(store.read_all()); log_path = tmp_path / 'server.log'
    with log_path.open('w') as log:
        port = _port(); proc = _start(tmp_path, port, 'very_slow', log)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(_command, port, generation)
                for _ in range(100):
                    if (tmp_path / 'command-in-append').exists():
                        break
                    time.sleep(.05)
                assert (tmp_path / 'command-in-append').exists() and not pending.done()
                proc.send_signal(signal.SIGTERM)
                try:
                    pending.result(timeout=20)
                except (OSError, http.client.HTTPException):
                    pass  # Response can be lost after the grace deadline; outcome is unknown.
            proc.wait(timeout=20)
            retry_port = _port(); resumed = _start(tmp_path, retry_port, 'new', log)
            try:
                receipt = _command(retry_port, generation)
                assert receipt['status'] == 'succeeded'
                assert sum(e.type == 'hint' for e in store.read_all()) == 1
            finally:
                resumed.send_signal(signal.SIGTERM); resumed.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()
