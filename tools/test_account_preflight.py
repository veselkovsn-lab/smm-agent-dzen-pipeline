"""BDD for read-only account preflight; all providers are isolated fakes."""
import subprocess

import pytest
from pytest_bdd import scenarios, given, when, then

from tools import account_preflight as preflight
from tools.publishing_core import ReleaseError

scenarios('features/account_preflight.feature')


class FakeTelegram:
    def __init__(self, calls):
        self.calls = calls

    async def connect(self):
        self.calls.append('connect')

    async def disconnect(self):
        self.calls.append('disconnect')


@pytest.fixture
def configured_env(monkeypatch, tmp_path):
    values = {
        'SMM_TESTING': '1',
        'YOUTUBE_CLIENT_ID': 'test-client',
        'YOUTUBE_CLIENT_SECRET': 'test-secret',
        'YOUTUBE_CHANNEL_ID': 'UC_EXPECTED',
        'YOUTUBE_TOKEN_PATH': str(tmp_path / 'youtube-token.json'),
        'TELEGRAM_API_ID': '12345',
        'TELEGRAM_API_HASH': 'test-hash',
        'TELEGRAM_CHANNEL_ID': '-10012345',
        'TELEGRAM_SESSION_PATH': str(tmp_path / 'telegram.session'),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return {'token': tmp_path / 'youtube-token.json',
            'session': tmp_path / 'telegram.session', 'calls': []}


@given('настройки каналов без сохранённых сессий', target_fixture='context')
def settings(configured_env):
    return configured_env


@given('сохранённая OAuth-сессия другого канала', target_fixture='context')
def wrong_youtube(configured_env, monkeypatch):
    from tools import publishing_youtube
    configured_env['token'].write_text('{}', encoding='utf-8')
    monkeypatch.setattr(publishing_youtube, 'youtube_service', lambda: object())

    def reject(service, expected):
        configured_env['calls'].append('verify_youtube')
        raise ReleaseError('OAuth авторизован для другого YouTube-канала.')

    monkeypatch.setattr(publishing_youtube, 'verify_channel', reject)
    return configured_env


@given('сохранённая Telegram-сессия без права публикации', target_fixture='context')
def no_telegram_rights(configured_env, monkeypatch):
    from tools import publishing_telegram
    configured_env['session'].write_text('test', encoding='utf-8')
    monkeypatch.setattr(publishing_telegram, 'telegram_client',
                        lambda: FakeTelegram(configured_env['calls']))

    async def reject(client, expected):
        configured_env['calls'].append('verify_telegram')
        raise ReleaseError('Нет права публикации в Telegram-канале.')

    monkeypatch.setattr(publishing_telegram, 'verify_channel', reject)
    return configured_env


@given('сохранённые сессии целевых каналов', target_fixture='context')
def ready(configured_env, monkeypatch):
    from tools import publishing_telegram, publishing_youtube
    configured_env['token'].write_text('{}', encoding='utf-8')
    configured_env['session'].write_text('test', encoding='utf-8')
    monkeypatch.setattr(publishing_youtube, 'youtube_service', lambda: object())
    monkeypatch.setattr(publishing_youtube, 'verify_channel',
                        lambda service, expected: configured_env['calls'].append('verify_youtube'))
    monkeypatch.setattr(publishing_telegram, 'telegram_client',
                        lambda: FakeTelegram(configured_env['calls']))

    async def verify(client, expected):
        configured_env['calls'].append('verify_telegram')

    monkeypatch.setattr(publishing_telegram, 'verify_channel', verify)
    return configured_env


@when('выполняется read-only preflight двух аккаунтов')
def both(context):
    context['youtube'] = preflight.worker('youtube')
    context['telegram'] = preflight.worker('telegram')


@when('выполняется read-only preflight YouTube')
def youtube(context):
    context['youtube'] = preflight.worker('youtube')


@when('выполняется read-only preflight Telegram')
def telegram(context):
    context['telegram'] = preflight.worker('telegram')


@then('оба аккаунта требуют локального входа без внешних публикаций')
def missing(context):
    assert context['youtube']['reason'] == 'login_required'
    assert context['telegram']['reason'] == 'login_required'
    assert context['calls'] == []


@then('YouTube заблокирован по несовпадению канала без загрузки')
def youtube_blocked(context):
    assert context['youtube']['reason'] == 'wrong_channel'
    assert context['youtube']['status'] == 'blocked'
    assert context['calls'] == ['verify_youtube']


@then('Telegram заблокирован по правам без сообщения')
def telegram_blocked(context):
    assert context['telegram']['reason'] == 'insufficient_rights'
    assert context['telegram']['status'] == 'blocked'
    assert context['calls'] == ['connect', 'verify_telegram', 'disconnect']


@then('оба аккаунта готовы без внешних публикаций')
def both_ready(context):
    assert context['youtube']['status'] == 'ready'
    assert context['youtube']['oauth_scopes'] is True
    assert context['telegram']['status'] == 'ready'
    assert context['telegram']['posting_rights'] is True
    assert context['calls'] == ['verify_youtube', 'connect', 'verify_telegram', 'disconnect']


def test_worker_timeout_kills_its_process(monkeypatch):
    calls = []

    class HungProcess:
        def communicate(self, timeout):
            calls.append(('communicate', timeout))
            raise subprocess.TimeoutExpired('account worker', timeout)

    monkeypatch.setattr(preflight.subprocess, 'Popen', lambda *args, **kwargs: HungProcess())
    monkeypatch.setattr(preflight, 'stop_worker', lambda process: calls.append(('stop', process)))
    result = preflight.run_check('youtube', timeout=2)
    assert result['reason'] == 'timeout'
    assert calls[0] == ('communicate', 2)
    assert calls[1][0] == 'stop'
