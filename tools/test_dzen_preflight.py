"""BDD acceptance contracts for issue #1; no production accounts or .env."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools import chrome_cdp as chrome
from tools import dzen_preflight as access
from tools import doctor

CHANNEL = 'https://dzen.ru/profile/editor/example'


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    for key in list(os.environ):
        if key.startswith(('DZEN_', 'YOUTUBE_', 'TELEGRAM_')):
            monkeypatch.delenv(key)
    monkeypatch.setenv('SMM_TESTING', '1')
    monkeypatch.setenv('DZEN_CHANNEL_URL', CHANNEL)
    monkeypatch.setenv('DZEN_CDP_URL', 'http://127.0.0.1:19222')
    monkeypatch.setenv('DZEN_CDP_MODE', 'external')


@pytest.mark.parametrize('endpoint', [
    'https://127.0.0.1:9222', 'http://example.org:9222',
    'http://user:secret@127.0.0.1:9222', 'http://127.0.0.1:9222?secret=x',
    'http://127.0.0.1:9222/path', 'http://127.0.0.1:99999',
    'http://127.0.0.1:9222#secret', 'http://127.0.0.1:0',
])
def test_given_invalid_endpoint_when_checked_then_blocked_without_secret(endpoint, monkeypatch):
    monkeypatch.setenv('DZEN_CDP_URL', endpoint)
    result = access.run_check()
    assert result['reason'] == 'configuration_error'
    assert endpoint not in json.dumps(result)


@pytest.mark.parametrize('channel', ['', 'https://dzen.ru/example', CHANNEL+'?token=secret',
                                     'https://dzen.ru.evil/profile/editor/example'])
def test_given_invalid_channel_when_checked_then_no_worker(channel, monkeypatch):
    monkeypatch.setenv('DZEN_CHANNEL_URL', channel)
    spawn = MagicMock(side_effect=AssertionError('worker must not start'))
    monkeypatch.setattr(access.subprocess, 'Popen', spawn)
    assert access.run_check()['reason'] == 'configuration_error'
    spawn.assert_not_called()


def test_given_external_tunnel_down_when_start_requested_then_no_local_browser(monkeypatch):
    monkeypatch.setattr(chrome, 'healthcheck', MagicMock(side_effect=OSError()))
    launch = MagicMock(side_effect=AssertionError('must not launch'))
    monkeypatch.setattr(chrome.subprocess, 'Popen', launch)
    with pytest.raises(RuntimeError, match='External CDP'):
        chrome.ensure_chrome()
    launch.assert_not_called()


def test_given_existing_external_cdp_when_checked_then_profile_untouched(monkeypatch):
    monkeypatch.setattr(chrome, 'healthcheck', lambda: True)
    monkeypatch.setattr(chrome, 'chrome_binary', lambda: pytest.fail('must not inspect local Chrome'))
    assert chrome.ensure_chrome() == {'cdp': 'ready', 'started': False}


def fake_transport(page, error=None):
    context = NS(new_page=AsyncMock(return_value=page))
    browser = NS(contexts=[context], close=AsyncMock())
    playwright = NS(stop=AsyncMock())
    module = NS(
        async_playwright=lambda: NS(start=AsyncMock(return_value=playwright)),
        connect_cdp=AsyncMock(return_value=browser, side_effect=error),
        open_channel=AsyncMock(), PlaywrightTimeoutError=TimeoutError,
        open_new_article=AsyncMock(side_effect=AssertionError('no mutation')),
    )
    return module, playwright, browser


def test_given_valid_access_when_preflight_then_uses_upstream_and_closes_only_own_page(monkeypatch):
    page = NS(close=AsyncMock())
    module, playwright, browser = fake_transport(page)
    monkeypatch.setattr(access, 'inspect_page', AsyncMock(return_value=access.report('ready', CHANNEL, 'editor')))
    result = asyncio.run(access.check_access(CHANNEL, 'http://127.0.0.1:19222', 'external', module))
    assert result['status'] == 'ready'
    module.open_channel.assert_awaited_once_with(page, CHANNEL)
    assert module.CDP_URL == 'http://127.0.0.1:19222'
    page.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()
    browser.close.assert_not_awaited()
    module.open_new_article.assert_not_awaited()


def test_given_transport_error_when_preflight_then_sanitized_and_no_navigation():
    module, playwright, browser = fake_transport(NS(close=AsyncMock()), RuntimeError('secret-token'))
    result = asyncio.run(access.check_access(CHANNEL, 'http://127.0.0.1:19222', 'external', module))
    assert result['reason'] == 'cdp_unavailable'
    assert 'secret-token' not in json.dumps(result)
    playwright.stop.assert_awaited_once()
    module.open_channel.assert_not_awaited()
    browser.close.assert_not_awaited()


def test_given_navigation_timeout_when_preflight_then_own_page_is_closed():
    page = NS(close=AsyncMock())
    module, playwright, _ = fake_transport(page)
    module.open_channel.side_effect = TimeoutError('private URL')
    result = asyncio.run(access.check_access(CHANNEL, 'http://127.0.0.1:19222', 'external', module))
    assert result['reason'] == 'timeout'
    assert 'private URL' not in json.dumps(result)
    page.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()


@pytest.mark.parametrize('failure', ['close_error', 'close_hang', 'stop_error'])
def test_given_cleanup_failure_when_access_ready_then_report_is_blocked(monkeypatch, failure):
    page = NS(close=AsyncMock())
    module, playwright, browser = fake_transport(page)
    async def hung_close():
        await asyncio.Event().wait()
    if failure == 'close_error':
        page.close.side_effect = RuntimeError('private URL')
    elif failure == 'close_hang':
        page.close.side_effect = hung_close
        monkeypatch.setattr(access, 'PAGE_CLOSE_SECONDS', .01)
    else:
        playwright.stop.side_effect = RuntimeError('private URL')
    monkeypatch.setattr(access, 'inspect_page', AsyncMock(return_value=access.report('ready', CHANNEL)))
    result = asyncio.run(access.check_access(CHANNEL, 'http://127.0.0.1:19222', 'external', module))
    assert result['status'] == 'blocked'
    assert result['reason'] == 'cleanup_failed'
    assert 'private URL' not in json.dumps(result)
    playwright.stop.assert_awaited_once()
    browser.close.assert_not_awaited()


def test_given_worker_budget_then_startup_and_cleanup_have_reserved_time():
    assert access.WORKER_SECONDS - (access.OPERATION_SECONDS + access.PAGE_CLOSE_SECONDS
                                   + access.DRIVER_STOP_SECONDS) >= 10


def test_given_hung_worker_when_deadline_then_process_tree_stopped(monkeypatch):
    worker = NS(pid=9876, communicate=MagicMock(side_effect=subprocess.TimeoutExpired('worker', 85)),
                wait=MagicMock())
    monkeypatch.setattr(access.subprocess, 'Popen', MagicMock(return_value=worker))
    stop = MagicMock()
    monkeypatch.setattr(access, 'stop_worker', stop)
    assert access.run_check()['reason'] == 'timeout'
    worker.communicate.assert_called_once_with(timeout=85)
    stop.assert_called_once_with(worker)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process group contract')
def test_given_worker_descendants_when_stopped_then_entire_group_is_killed(monkeypatch):
    kill = MagicMock()
    monkeypatch.setattr(access.os, 'killpg', kill)
    worker = NS(pid=9876, wait=MagicMock())
    access.stop_worker(worker)
    kill.assert_called_once_with(9876, access.signal.SIGKILL)
    worker.wait.assert_called_once_with(timeout=2)


def test_given_dzen_only_when_doctor_then_other_platforms_not_contacted(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(access, 'run_check', lambda: access.report('ready', CHANNEL, 'editor'))
    monkeypatch.setattr(doctor, 'local_checks', lambda: pytest.fail('unrelated local probes'))
    output = tmp_path / 'sub/report.json'
    assert doctor.main(['--online', '--platform', 'dzen', '--output', str(output)]) == 0
    value = json.loads(capsys.readouterr().out)
    assert list(value['accounts']) == ['dzen']
    assert json.loads(output.read_text()) == value


@pytest.mark.browser
@pytest.mark.parametrize('url,html,expected', [
    (CHANNEL, '<button data-testid="add-publication-button">+</button>', 'ready'),
    (CHANNEL, '<button>Создать</button>', 'unknown_layout'),
    (CHANNEL, '<button hidden data-testid="add-publication-button">+</button>', 'unknown_layout'),
    (CHANNEL, '<p>Войдите в Дзен Студию</p>', 'login_required'),
    (CHANNEL, '<p>У вас нет доступа к этому каналу</p>', 'insufficient_rights'),
    ('https://dzen.ru/example', '<button data-testid="add-publication-button">+</button>', 'unknown_layout'),
    ('https://dzen.ru/profile/editor/other', '<button data-testid="add-publication-button">+</button>', 'wrong_channel'),
    ('https://id.dzen.ru/login?secret=private', '<p>Login</p>', 'login_required'),
])
def test_given_page_state_when_inspected_then_conservative_result(url, html, expected):
    async def scenario():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=html))
            page = await context.new_page()
            await page.goto(url)
            result = await access.inspect_page(page, CHANNEL)
            await browser.close()
            return result
    result = asyncio.run(scenario())
    assert result['reason'] == expected
    assert result['status'] == ('ready' if expected == 'ready' else 'blocked')
    assert 'private' not in json.dumps(result)


@pytest.mark.browser
def test_given_upstream_navigation_when_read_only_then_no_draft_click():
    async def scenario():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html',
                body='<button data-testid="add-publication-button" onclick="window.clicked=true">+</button>'))
            page = await context.new_page()
            module = access.transport_module()
            await module.open_channel(page, CHANNEL)
            assert page.url == CHANNEL
            assert not await page.evaluate('Boolean(window.clicked)')
            await browser.close()
    asyncio.run(scenario())
