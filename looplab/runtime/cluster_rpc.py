"""Bounded stdio exchange: malformed transports cannot buffer unlimited output or hang a worker."""
from __future__ import annotations

import os
import selectors
import subprocess
import time

from looplab.core.cluster import MAX_REPLY, MAX_REQUEST
from looplab.runtime.sandbox import _kill_tree


def exchange(argv, raw, env, timeout):
    if len(raw) > MAX_REQUEST:
        raise OSError('executor RPC request exceeded limit')
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env, start_new_session=True)
    output, errors, offset = bytearray(), bytearray(), 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, event in ((p.stdin, selectors.EVENT_WRITE), (p.stdout, selectors.EVENT_READ),
                                (p.stderr, selectors.EVENT_READ)):
                os.set_blocking(pipe.fileno(), False); selector.register(pipe, event)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OSError('executor RPC timeout')
                for key, _ in selector.select(min(.1, remaining)):
                    pipe = key.fileobj
                    if pipe is p.stdin:
                        try:
                            offset += os.write(pipe.fileno(), raw[offset:offset + 65536])
                        except BrokenPipeError:
                            offset = len(raw)
                        if offset == len(raw):
                            selector.unregister(pipe); pipe.close()
                    else:
                        data = os.read(pipe.fileno(), 65536)
                        if not data:
                            selector.unregister(pipe); pipe.close(); continue
                        target = output if pipe is p.stdout else errors
                        target.extend(data)
                        if len(target) > (MAX_REPLY if pipe is p.stdout else 8192):
                            raise OSError('executor RPC output exceeded limit')
            p.wait(timeout=max(.001, deadline - time.monotonic()))
        if p.returncode != 0:
            raise OSError('executor RPC failed')
        return bytes(output)
    except BaseException:
        _kill_tree(p)
        p.wait(timeout=5)
        raise
    finally:
        for pipe in (p.stdin, p.stdout, p.stderr):
            if pipe and not pipe.closed:
                pipe.close()
