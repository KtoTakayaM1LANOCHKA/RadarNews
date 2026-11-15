import os
from pathlib import Path
import tempfile

# Never read developer credentials or touch the application's data in tests.
BOOT_DIR = tempfile.TemporaryDirectory(prefix='newsradar-tests-')
os.environ.update(DB_PATH=str(Path(BOOT_DIR.name)/'bootstrap.db'), UPLOAD_DIR=str(Path(BOOT_DIR.name)/'uploads'), NEWS_WORKER_ENABLED='0', TELEGRAM_BOT_TOKEN='123:test-token', ADMIN_PHONE='+79999999999', DEMO_MODE='1', DEMO_OTP='1',
                  # test_workflow.py импортирует приложение после conftest, поэтому его секреты задаются здесь
                  EDITOR_PASSWORD='test-secret-editor', INGEST_SECRET='test-secret-ingest', MAX_WEBHOOK_SECRET='test-max-secret',
                  VK_WEBHOOK_SECRET='test-vk-secret', VK_CONFIRMATION_CODE='test-vk-confirm')

import pytest
from fastapi.testclient import TestClient
from app import main


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_FILE', tmp_path/'test.db')
    monkeypatch.setattr(main, 'UPLOAD_DIR', tmp_path/'uploads')
    monkeypatch.setattr(main, 'AVATAR_DIR', tmp_path/'uploads'/'avatars')
    main.AVATAR_DIR.mkdir(parents=True)
    monkeypatch.setenv('NEWS_WORKER_ENABLED', '0')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123:test-token')
    main.init_db()
    main.newsroom.init()
    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def accounts(api):
    result = {}
    for key, phone in [('admin', '+79999999999'), ('user', '+79990000001'), ('editor', '+79990000002')]:
        code = api.post('/api/auth/request-code', json={'phone': phone}).json()['demo_code']
        d = api.post('/api/auth/verify-code', json={'phone': phone, 'code': code}).json()
        result[key] = {'id': d['user']['id'], 'token': d['token'], 'headers': {'X-Session': d['token']}}
    r = api.patch(f"/api/admin/users/{result['editor']['id']}/role", json={'role':'editor'}, headers=result['admin']['headers'])
    assert r.status_code == 200
    return result
