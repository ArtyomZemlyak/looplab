"""Render measured script selection INSIDE one normally admitted native evaluation.

Rendering executes no candidate. The generated self-contained program owns every pilot and
full execution, under its existing node's resource/timeout/stop and wall-time accounting.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import PurePosixPath

from looplab.core.best_of_n import normalize_pilot
from looplab.core.errors import ConfigRefusal


def asset_manifest(assets):
    if (not isinstance(assets, dict) or len(assets) > 64
            or any(not isinstance(name, str) for name in assets)):
        raise ConfigRefusal('pilot supports at most64 declared text assets')
    rows = []
    for name, content in sorted(assets.items()):
        if (not isinstance(name, str) or not name or len(name) > 256 or '\\' in name
                or any(ord(c) < 32 for c in name) or not isinstance(content, str)):
            raise ConfigRefusal('invalid pilot asset')
        path = PurePosixPath(name)
        if (path.is_absolute() or str(path) != name or '..' in path.parts
                or any(part.startswith('.') for part in path.parts) or name == 'solution.py'):
            raise ConfigRefusal('pilot assets must be normalized relative files without links')
        rows.append({'path': name, 'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
    return rows


def require_pilot_runtime():
    if os.name != 'posix' or not hasattr(os, 'waitid') or not hasattr(os, 'WNOWAIT'):
        raise ConfigRefusal('native script pilot requires POSIX waitid WNOWAIT process identity')


def pilot_rank(candidates, *, direction, pilot, assets):
    """Return an immutable execution bundle; ranking runs only at native eval admission."""
    pilot = normalize_pilot(pilot)
    if pilot is None or direction not in ('min', 'max') or not 2 <= len(candidates) <= 8:
        raise ConfigRefusal('native script pilot requires2..8 candidates and a declared objective')
    require_pilot_runtime()
    for code in candidates:
        if not isinstance(code, str) or not code.strip() or len(code.encode('utf-8')) > 128 * 1024:
            raise ConfigRefusal('pilot candidate code exceeds bounded script contract')
    payload = {'schema': 'looplab.native-script-pilot/v1', 'candidates': candidates,
               'direction': direction, 'pilot': pilot, 'assets': asset_manifest(assets)}
    return ('# LoopLab native Best-of-N: no execution at build time.\n' + inspect.getsource(_pilot_entry)
            + '\n_pilot_entry(' + repr(payload) + ')\n')


def _pilot_entry(payload):
    # Self-contained: the evaluation tier may have no installed LoopLab package.
    import hashlib
    import json
    import math
    import os
    import selectors
    import signal
    import subprocess
    import sys
    import tempfile
    import time
    from pathlib import Path

    root = Path.cwd().resolve()
    area = Path(tempfile.mkdtemp(prefix='bestofn-', dir=root))
    cap = 60000

    def stage(index, code):
        work = area / str(index)
        work.mkdir()
        for asset in payload['assets']:
            source = root / asset['path']
            if source.is_symlink() or any(parent.is_symlink() for parent in source.parents if parent != root):
                raise RuntimeError('pilot asset link refused')
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != asset['sha256']:
                raise RuntimeError('pilot asset changed')
            target = work / asset['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (work / 'solution.py').write_text(code, encoding='utf-8')
        return work

    def run(work, env, timeout):
        start = time.monotonic()
        process = subprocess.Popen([sys.executable, 'solution.py'], cwd=work, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        streams, total, output = selectors.DefaultSelector(), [0, 0], [b'', b'']
        for index, stream in enumerate([process.stdout, process.stderr]):
            os.set_blocking(stream.fileno(), False)
            streams.register(stream, selectors.EVENT_READ, index)
        timed_out = False
        def read_ready(wait):
            for key, _ in streams.select(wait):
                data = os.read(key.fileobj.fileno(), 16384)
                if not data:
                    streams.unregister(key.fileobj)
                else:
                    index = key.data
                    total[index] += len(data)
                    output[index] = (output[index] + data)[-cap:]
        try:
            while True:
                read_ready(.02)
                # Do not reap/recycle the group leader's identity before cleanup.
                if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                    break
                if timeout is not None and time.monotonic() - start >= timeout:
                    timed_out = True
                    break
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            until = time.monotonic() + 1
            while streams.get_map() and time.monotonic() < until:
                read_ready(.01)
            streams.close()
            process.stdout.close(); process.stderr.close()
        text = [data.decode('utf-8', errors='replace') for data in output]
        for name, value in zip(['stdout.log', 'stderr.log'], output):
            (work / name).write_bytes(value)
        return {'exit_code': process.returncode, 'timed_out': timed_out,
                'seconds': time.monotonic() - start, 'output_truncated': [n > cap for n in total],
                'output_sha256': [hashlib.sha256(data).hexdigest() for data in output]}, text

    def measured(result, text):
        if result['timed_out'] or result['exit_code'] != 0:
            return None
        for line in reversed(text.splitlines()):
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(record, dict) or 'metric' not in record:
                continue
            value = record['metric']
            if type(value) not in (int, float):
                return None
            try:
                return float(value) if math.isfinite(float(value)) else None
            except (ValueError, OverflowError):
                return None
        return None

    receipts, successful = [], []
    env = dict(os.environ)
    for index, code in enumerate(payload['candidates']):
        work = stage('pilot-' + str(index), code)
        result, text = run(work, {**env, **payload['pilot']['env']}, payload['pilot']['timeout_seconds'])
        metric = measured(result, text[0])
        result.update(index=index, code_sha256=hashlib.sha256(code.encode()).hexdigest(), metric=metric,
                      output_ref=str(work.relative_to(root)))
        receipts.append(result)
        if metric is not None:
            successful.append((index, metric))
    if not successful:
        print(json.dumps({'best_of_n_pilot': {'schema': payload['schema'], 'pilots': receipts,
            'chosen': None, 'full': None, 'fidelity_review': payload['pilot']['fidelity_review']}}), flush=True)
        raise RuntimeError('no usable pilot metric; no static fallback or full evaluation')
    chosen = (min(successful, key=lambda item: item[1]) if payload['direction'] == 'min'
              else max(successful, key=lambda item: item[1]))[0]
    # A fresh full workspace and ORIGINAL environment: no pilot outputs, mutation or
    # reduced-fidelity env can become the full result.
    full_work = stage('full', payload['candidates'][chosen])
    full, text = run(full_work, env, None)  # the native outer timeout owns the whole method
    sys.stdout.write(text[0] + '\n'); sys.stderr.write(text[1])
    full.update(code_sha256=hashlib.sha256(payload['candidates'][chosen].encode()).hexdigest(),
                output_ref=str(full_work.relative_to(root)))
    print(json.dumps({'best_of_n_pilot': {'schema': payload['schema'], 'pilots': receipts,
        'chosen': chosen, 'full': full, 'fidelity_review': payload['pilot']['fidelity_review'],
        'meaning': 'Original full metric; pilot selection is exploratory until fidelity is measured.'}}), flush=True)
    raise SystemExit(full['exit_code'])
