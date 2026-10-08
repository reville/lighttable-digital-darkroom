import base64
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('store_access_test', ROOT / 'macos_store_access.py')
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)

@pytest.fixture(autouse=True)
def reset(monkeypatch):
    access._active.clear()
    monkeypatch.delenv('LIGHTTABLE_STORE_GRANTS_FILE', raising=False)

def grants(tmp_path, monkeypatch, values):
    path = tmp_path / 'grants.json'
    path.write_text(json.dumps(values))
    monkeypatch.setenv('LIGHTTABLE_STORE_GRANTS_FILE', str(path))
    return path

def test_direct_channel_never_loads_corefoundation(monkeypatch):
    monkeypatch.setattr(access, '_resolve', lambda _: pytest.fail('direct channel acquired grant'))
    access.refresh()

def test_new_grants_after_child_launch_are_acquired_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(access, '_resolve', lambda data: calls.append(data) or len(calls))
    path = grants(tmp_path, monkeypatch, {'/one': base64.b64encode(b'one').decode()})
    access.refresh(); access.refresh()
    path.write_text(json.dumps({'/one': base64.b64encode(b'one').decode(), '/two': base64.b64encode(b'two').decode()}))
    access.refresh()
    assert calls == [b'one', b'two']

def test_resolution_denial_is_not_recorded(tmp_path, monkeypatch):
    grants(tmp_path, monkeypatch, {'/one': base64.b64encode(b'one').decode()})
    def deny(_): raise PermissionError('reselect')
    monkeypatch.setattr(access, '_resolve', deny)
    with pytest.raises(PermissionError): access.refresh()
    assert not access._active

def test_invalid_bookmark_fails_closed(tmp_path, monkeypatch):
    grants(tmp_path, monkeypatch, {'/one': 'invalid!'})
    monkeypatch.setattr(access, '_resolve', lambda _: pytest.fail('invalid bookmark resolved'))
    with pytest.raises(ValueError): access.refresh()

def test_scope_is_held_through_worker_lifetime_and_balanced(tmp_path, monkeypatch):
    grants(tmp_path, monkeypatch, {'/one': base64.b64encode(b'one').decode()})
    monkeypatch.setattr(access, '_resolve', lambda _: 42)
    calls = []
    class CF:
        def CFURLStopAccessingSecurityScopedResource(self, url): calls.append(('stop', url))
        def CFRelease(self, url): calls.append(('release', url))
    monkeypatch.setattr(access, '_cf', CF())
    access.refresh()
    assert calls == []
    access.close(); access.close()
    assert calls == [('release', 42)]

def test_missing_grant_bridge_stops_worker_before_application_code(tmp_path):
    import os
    import subprocess
    import sys
    (tmp_path / 'sitecustomize.py').write_bytes((ROOT / 'sitecustomize.py').read_bytes())
    env = dict(os.environ, PYTHONPATH=str(tmp_path), LIGHTTABLE_STORE_GRANTS_FILE=str(tmp_path/'grants.json'))
    result = subprocess.run([sys.executable, '-c', 'print("application-started")'], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 78
    assert 'application-started' not in result.stdout
    assert 'ModuleNotFoundError' in result.stderr


def test_direct_worker_bootstrap_without_grant_environment_is_unchanged(tmp_path):
    import os
    import subprocess
    import sys
    (tmp_path / 'sitecustomize.py').write_bytes((ROOT / 'sitecustomize.py').read_bytes())
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    env.pop('LIGHTTABLE_STORE_GRANTS_FILE', None)
    result = subprocess.run([sys.executable, '-c', 'print("application-started")'], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == 'application-started'
