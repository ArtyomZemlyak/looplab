"""Quick start is open by default; URL guards and owner login are explicit options."""
import pytest
from fastapi.testclient import TestClient

from looplab.serve.owner_token import owner_token_path
from looplab.serve.server import make_app
from looplab.serve.jupyter import setup_looplab


@pytest.mark.parametrize('host,hub', [('127.0.0.1', False), ('0.0.0.0', False),
                                    ('127.0.0.1', True)])
def test_default_start_needs_neither_login_nor_proxy_url_configuration(tmp_path, monkeypatch, host, hub):
    if hub:
        monkeypatch.setenv('JUPYTERHUB_SERVICE_PREFIX', '/user/quick/')
    app = make_app(tmp_path, bind_host=host)
    with TestClient(app, base_url='http://quick.proxy.example:8080') as client:
        assert client.get('/api/auth/status').json() == {'required': False, 'authenticated': True}
        assert client.get('/api/runs').status_code == 200
        # An actual POST reaches the handler despite a different forwarded browser Origin.
        assert client.post('/api/auth/verify', headers={'Origin': 'https://public.proxy.example'}).status_code == 200
    assert not owner_token_path().exists()
    assert setup_looplab()['new_browser_tab'] is False


def test_opt_in_origin_guard_refuses_before_side_effect_and_accepts_configured_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOPLAB_UI_CHECK_ORIGIN', '1')
    monkeypatch.setenv('LOOPLAB_UI_HOSTS', 'quick.proxy.example')
    app = make_app(tmp_path)
    writes = []

    @app.post('/api/quick-start-test')
    def write():
        writes.append(1)
        return {'ok': True}

    with TestClient(app, base_url='http://quick.proxy.example') as client:
        assert client.post('/api/quick-start-test', headers={'Origin': 'https://other.example'}).status_code == 403
        assert client.get('/api/runs', headers={'Host': 'other.example'}).status_code == 421
        assert writes == []
        assert client.post('/api/quick-start-test', headers={'Origin': 'https://quick.proxy.example'}).status_code == 200
        assert writes == [1]


def test_default_start_does_not_activate_an_old_token_file(tmp_path, monkeypatch):
    path = tmp_path / 'old-ui-token'
    path.write_text('previous-protected-start', encoding='utf-8')
    monkeypatch.setenv('LOOPLAB_UI_TOKEN_FILE', str(path))
    with TestClient(make_app(tmp_path, bind_host='0.0.0.0')) as client:
        assert client.get('/api/auth/status').json() == {'required': False, 'authenticated': True}
        assert client.get('/api/runs').status_code == 200
    assert path.read_text(encoding='utf-8') == 'previous-protected-start'


@pytest.mark.parametrize('host', ['127.0.0.1', '0.0.0.0'])
def test_opt_in_login_mints_recoverable_token_and_refuses_untokened_owner_reads(tmp_path, monkeypatch, host):
    monkeypatch.setenv('LOOPLAB_UI_REQUIRE_AUTH', '1')
    with TestClient(make_app(tmp_path, bind_host=host)) as client:
        assert client.get('/api/auth/status').json() == {'required': True, 'authenticated': False}
        assert client.get('/api/runs').status_code == 401
        token = owner_token_path().read_text().strip()
        assert token
        assert client.get('/api/runs', headers={'X-LoopLab-Token': token}).status_code == 200


def test_scoped_harness_can_coexist_with_open_owner_ui_without_becoming_owner(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOPLAB_HARNESS_TOKEN', 'test-agent-credential')
    with TestClient(make_app(tmp_path)) as client:
        assert client.get('/api/auth/status').json() == {'required': False, 'authenticated': True}
        assert client.get('/api/runs').status_code == 200
        agent = {'X-LoopLab-Token': 'test-agent-credential'}
        assert client.get('/api/auth/status', headers=agent).json()['authenticated'] is False
        for route in ['/api/start', '/api/settings', '/api/auth/verify']:
            assert client.post(route, headers=agent, json={}).status_code == 403
        assert client.get('/api/runs', headers={'X-LoopLab-Token': 'wrong'}).status_code == 401


def test_open_quick_start_still_refuses_a_browser_cross_site_mutation(tmp_path):
    """The configuration-free floor: a page on another site cannot fire a no-preflight POST at the
    open control plane, while the proxied same-origin SPA and header-less CLI/TUI clients pass."""
    app = make_app(tmp_path)
    writes = []

    @app.post('/api/quick-start-test')
    def write():
        writes.append(1)
        return {'ok': True}

    with TestClient(app, base_url='http://quick.proxy.example') as client:
        attack = {'Origin': 'https://evil.example', 'Sec-Fetch-Site': 'cross-site',
                  'Content-Type': 'text/plain'}
        assert client.post('/api/quick-start-test', headers=attack).status_code == 403
        assert writes == []
        assert client.post('/api/quick-start-test', headers={
            'Origin': 'https://public.proxy.example', 'Sec-Fetch-Site': 'same-origin'}).status_code == 200
        assert client.post('/api/quick-start-test').status_code == 200
        # The configured cross-origin dev reader (the Vite default) is not a cross-site attack.
        assert client.post('/api/quick-start-test', headers={
            'Origin': 'http://127.0.0.1:5173', 'Sec-Fetch-Site': 'cross-site'}).status_code == 200
        assert writes == [1, 1, 1]
