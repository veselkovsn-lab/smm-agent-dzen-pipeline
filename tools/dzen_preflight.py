"""Read-only Dzen access check using the project's dzen-publish transport."""
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NEXT = {
    'ready': 'Доступ к целевому редактору подтверждён.',
    'configuration_error': 'Проверьте DZEN_CHANNEL_URL, DZEN_CDP_URL и DZEN_CDP_MODE локально.',
    'cdp_unavailable': 'Проверьте выделенный Chrome и SSH-туннель; повторите проверку.',
    'browser_start_failed': 'Запустите выделенный Chrome под пользователем рабочего стола или подключите внешний браузер.',
    'login_required': 'Войдите в Дзен в выделенном Chrome; коды вводите только в браузере.',
    'insufficient_rights': 'Войдите аккаунтом с правами редактора целевого канала.',
    'wrong_channel': 'Проверьте выбранный аккаунт и DZEN_CHANNEL_URL.',
    'unknown_layout': 'Проверьте страницу вручную: разметка или состояние входа не распознаны.',
    'timeout': 'Проверьте соединение и страницу Chrome; проверка превысила лимит времени.',
    'cleanup_failed': 'Не удалось завершить очистку проверки; осмотрите служебную вкладку Chrome перед повтором.',
    'internal_error': 'Проверьте установку зависимостей и версию инструмента.',
}
CHANNEL = re.compile(r'https://dzen\.ru/profile/editor/[A-Za-z0-9_.-]+/?\Z')
OPERATION_SECONDS = 70
PAGE_CLOSE_SECONDS = 2
DRIVER_STOP_SECONDS = 3
WORKER_SECONDS = 85  # Includes interpreter startup and >=10 seconds beyond async budgets.


def report(reason, channel=None, observed=None):
    return {'status': 'ready' if reason == 'ready' else 'blocked', 'reason': reason,
            'next': NEXT[reason], 'checked_at': datetime.now(timezone.utc).isoformat(),
            'channel': channel, 'observed_kind': observed}


def configuration():
    from tools.chrome_cdp import endpoint
    channel = os.getenv('DZEN_CHANNEL_URL', '')
    mode = os.getenv('DZEN_CDP_MODE', 'local')
    if not CHANNEL.fullmatch(channel) or mode not in ('local', 'external'):
        raise ValueError('Invalid Dzen configuration')
    return channel.rstrip('/'), endpoint(), mode


def transport_module():
    spec = importlib.util.spec_from_file_location('dzen_access_transport', ROOT / 'dzen-publish/publish_dzen.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Transport exceptions and logs can contain URLs; only emit our allowlisted report.
    module.log = lambda message: None
    return module


async def visible(locator):
    return await locator.count() > 0 and await locator.first.is_visible()


async def inspect_page(page, channel):
    url = urlparse(page.url)
    if url.hostname in ('passport.yandex.ru', 'id.vk.com', 'id.dzen.ru'):
        return report('login_required', channel, 'login')
    # Specific login/access-denied messages, not any generic header sign-in button.
    for text in ('Войдите, чтобы управлять каналом', 'Войдите в Дзен Студию'):
        if await visible(page.get_by_text(text, exact=True)):
            return report('login_required', channel, 'login')
    for text in ('У вас нет доступа к этому каналу', 'У вас нет прав для управления этим каналом'):
        if await visible(page.get_by_text(text, exact=True)):
            return report('insufficient_rights', channel, 'denied')
    current = f'{url.scheme}://{url.netloc}{url.path}'.rstrip('/')
    if CHANNEL.fullmatch(current) and current != channel:
        return report('wrong_channel', channel, 'other_editor')
    if current == channel and await visible(page.locator('[data-testid="add-publication-button"]:visible')):
        return report('ready', channel, 'editor')
    return report('unknown_layout', channel, 'editor' if current == channel else 'other')


async def check_access(channel, endpoint, mode, module=None):
    module = module or transport_module()
    module.CDP_URL = endpoint
    playwright = None
    page = None
    stage = 'connect'
    result = report('internal_error', channel)
    try:
        async with asyncio.timeout(OPERATION_SECONDS):
            if mode == 'local':
                from tools.chrome_cdp import ensure_chrome
                try:
                    await asyncio.to_thread(ensure_chrome)
                except Exception:
                    return report('browser_start_failed', channel)
            playwright = await module.async_playwright().start()
            # connect_cdp owns the only retry (2 attempts); external never starts Chrome.
            browser = await module.connect_cdp(playwright)
            stage = 'navigate'
            page = await browser.contexts[0].new_page()
            await module.open_channel(page, channel)
            stage = 'inspect'
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                result = await inspect_page(page, channel)
                if result['reason'] != 'unknown_layout' or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(.25)
    except (TimeoutError, module.PlaywrightTimeoutError):
        result = report('cdp_unavailable' if stage == 'connect' else 'timeout', channel)
    except Exception:
        result = report('cdp_unavailable' if stage == 'connect' else 'unknown_layout', channel)
    finally:
        # Disconnect, never browser.close(): the user owns the authorized browser.
        if page:
            try:
                await asyncio.wait_for(page.close(), timeout=PAGE_CLOSE_SECONDS)
            except Exception:
                result = report('cleanup_failed', channel)
        if playwright:
            try:
                await asyncio.wait_for(playwright.stop(), timeout=DRIVER_STOP_SECONDS)
            except Exception:
                result = report('cleanup_failed', channel)
    return result


def stop_worker(process):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=2)


def run_check(timeout=90):
    """Worker boundary enforces the deadline even when driver cleanup hangs."""
    try:
        channel, _, _ = configuration()
    except (ValueError, TypeError):
        return report('configuration_error')
    kwargs = {'start_new_session': True} if os.name != 'nt' else {}
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker'],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                               encoding='utf-8', env={**os.environ, 'PYTHONUTF8': '1'}, **kwargs)
    try:
        stdout, _ = process.communicate(timeout=min(WORKER_SECONDS, max(1, timeout - 5)))
        value = json.loads(stdout)
        if process.returncode or value.get('reason') not in NEXT:
            return report('internal_error', channel)
        return value
    except subprocess.TimeoutExpired:
        stop_worker(process)
        return report('timeout', channel)
    except (ValueError, TypeError):
        return report('internal_error', channel)
    except BaseException:
        stop_worker(process)
        raise


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('Use tools/doctor.py --online --platform dzen')
    try:
        channel, endpoint, mode = configuration()
        result = asyncio.run(check_access(channel, endpoint, mode))
    except Exception:
        result = report('internal_error')
    print(json.dumps(result, ensure_ascii=False))
