"""Remote harness discovery must work without importing the optional UI server stack."""
import subprocess
import sys

import pytest


@pytest.mark.parametrize("phase", ["implementation", "research", "report", "recovery", "genesis"])
def test_remote_phase_discovery_without_fastapi_or_uvicorn(phase):
    code = '''
import builtins, sys, anyio, httpx
original = builtins.__import__
def without_ui(name, *args, **kwargs):
    if name.split('.')[0] in {'fastapi', 'uvicorn'}:
        raise ModuleNotFoundError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = without_ui
from looplab.harness.mcp_server import HarnessAPI, build_server
api = HarnessAPI('http://localhost', transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError('local discovery contacted server'))))
server = build_server(api)
async def check():
    result = await server.call_tool('phase_info', {'phase_id': sys.argv[1]})
    assert not getattr(result, 'isError', False), result
    assert 'commands' in str(result), result
anyio.run(check)
'''
    result = subprocess.run([sys.executable, "-c", code, phase], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
