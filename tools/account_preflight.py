"""Bounded, read-only YouTube and Telegram account checks without secret output."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NEXT = {
    'youtube': {
        'ready': 'OAuth-права и целевой YouTube-канал подтверждены; видео не загружалось.',
        'configuration_error': 'Проверьте OAuth-клиент и YOUTUBE_CHANNEL_ID в локальном .env.',
        'login_required': 'Запустите python tools/publish_release.py auth-youtube в локальном интерактивном терминале; токен храните вне Git.',
        'wrong_channel': 'Выберите нужный канал при auth-youtube и проверьте YOUTUBE_CHANNEL_ID.',
        'insufficient_scope': 'Повторите auth-youtube и предоставьте запрошенные права YouTube.',
        'account_error': 'Проверьте сохранённый OAuth-токен; если он отозван, повторите auth-youtube.',
        'provider_unavailable': 'Проверьте доступ к Google API и повторите read-only проверку.',
        'timeout': 'YouTube не ответил в срок; видео не загружалось.',
        'internal_error': 'Проверьте зависимости публикации и установку preflight.',
    },
    'telegram': {
        'ready': 'Пользовательская сессия, целевой Telegram-канал и право публикации подтверждены; сообщение не отправлялось.',
        'configuration_error': 'Проверьте TELEGRAM_API_ID, TELEGRAM_API_HASH и TELEGRAM_CHANNEL_ID в локальном .env.',
        'login_required': 'Запустите python tools/publish_release.py auth-telegram в локальном интерактивном терминале; код и 2FA вводите только там.',
        'wrong_channel': 'Проверьте TELEGRAM_CHANNEL_ID и аккаунт, использованный при auth-telegram.',
        'insufficient_rights': 'Дайте этому аккаунту право публикации в целевом Telegram-канале.',
        'account_error': 'Проверьте пользовательскую сессию; если она отозвана, повторите auth-telegram.',
        'provider_unavailable': 'Проверьте доступ к Telegram и повторите read-only проверку.',
        'timeout': 'Telegram не ответил в срок; сообщение не отправлялось.',
        'internal_error': 'Проверьте зависимости публикации и установку preflight.',
    },
}

WORKER_SECONDS = 60


def report(platform, reason, error_type=None):
    result = {'status': 'ready' if reason == 'ready' else 'blocked', 'reason': reason,
              'next': NEXT[platform][reason], 'checked_at': datetime.now(timezone.utc).isoformat(),
              'channel_match': reason == 'ready'}
    result['oauth_scopes' if platform == 'youtube' else 'posting_rights'] = reason == 'ready'
    if error_type:
        result['error_type'] = error_type
    return result


def configured(platform):
    from tools.publishing_core import local_path
    if platform == 'youtube':
        client_id = os.getenv('YOUTUBE_CLIENT_ID', '').strip()
        secret = os.getenv('YOUTUBE_CLIENT_SECRET', '').strip()
        if not os.getenv('YOUTUBE_CHANNEL_ID') or bool(client_id) != bool(secret):
            return 'configuration_error'
        if not client_id and not local_path(os.getenv('YOUTUBE_CLIENT_SECRETS_PATH') or '.local/auth/youtube-client.json').is_file():
            return 'configuration_error'
        if not local_path(os.getenv('YOUTUBE_TOKEN_PATH') or '.local/auth/youtube-token.json').is_file():
            return 'login_required'
    else:
        api_id = os.getenv('TELEGRAM_API_ID', '')
        if (not api_id.isdecimal() or int(api_id) <= 0 or not os.getenv('TELEGRAM_API_HASH')
                or not os.getenv('TELEGRAM_CHANNEL_ID')):
            return 'configuration_error'
        if not local_path(os.getenv('TELEGRAM_SESSION_PATH') or '.local/auth/telegram.session').is_file():
            return 'login_required'
    return None


def youtube_check():
    from tools.publishing_core import ReleaseError
    from tools.publishing_youtube import youtube_service, verify_channel
    try:
        verify_channel(youtube_service(), os.getenv('YOUTUBE_CHANNEL_ID'))
        return report('youtube', 'ready')
    except ReleaseError as error:
        message = str(error)
        if 'другого YouTube-канала' in message or 'YOUTUBE_CHANNEL_ID не совпадает' in message:
            reason = 'wrong_channel'
        elif 'необходимые права' in message:
            reason = 'insufficient_scope'
        elif 'auth-youtube' in message:
            reason = 'login_required'
        else:
            reason = 'account_error'
        return report('youtube', reason, type(error).__name__)
    except (OSError, TimeoutError) as error:
        return report('youtube', 'provider_unavailable', type(error).__name__)
    except Exception as error:
        status = getattr(getattr(error, 'resp', None), 'status', None)
        if status in (401, 403):
            return report('youtube', 'account_error', type(error).__name__)
        if status is not None and status >= 500:
            return report('youtube', 'provider_unavailable', type(error).__name__)
        return report('youtube', 'account_error', type(error).__name__)


async def telegram_check():
    from tools.publishing_core import ReleaseError
    from tools.publishing_telegram import telegram_client, verify_channel
    client = None
    try:
        client = telegram_client()
        await client.connect()
        await verify_channel(client, os.getenv('TELEGRAM_CHANNEL_ID'))
        return report('telegram', 'ready')
    except ReleaseError as error:
        message = str(error)
        if 'Нет права публикации' in message:
            reason = 'insufficient_rights'
        elif 'TELEGRAM_CHANNEL_ID не совпадает' in message or 'Получатель должен быть Telegram-каналом' in message:
            reason = 'wrong_channel'
        elif 'auth-telegram' in message:
            reason = 'login_required'
        else:
            reason = 'account_error'
        return report('telegram', reason, type(error).__name__)
    except (OSError, TimeoutError) as error:
        return report('telegram', 'provider_unavailable', type(error).__name__)
    except Exception as error:
        return report('telegram', 'account_error', type(error).__name__)
    finally:
        if client:
            await client.disconnect()


def worker(platform):
    try:
        from dotenv import load_dotenv
        if os.getenv('SMM_TESTING') != '1':
            load_dotenv(ROOT / '.env', override=False)
        reason = configured(platform)
        if reason:
            return report(platform, reason)
        return youtube_check() if platform == 'youtube' else asyncio.run(telegram_check())
    except Exception as error:
        return report(platform, 'internal_error', type(error).__name__)


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


def run_check(platform, timeout=WORKER_SECONDS):
    if platform not in NEXT:
        raise ValueError('Unsupported account platform')
    kwargs = {'start_new_session': True} if os.name != 'nt' else {}
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', platform],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                               encoding='utf-8', env={**os.environ, 'PYTHONUTF8': '1'}, **kwargs)
    try:
        stdout, _ = process.communicate(timeout=timeout)
        value = json.loads(stdout)
        if process.returncode or value.get('reason') not in NEXT[platform] or value.get('status') not in ('ready', 'blocked'):
            return report(platform, 'internal_error')
        return value
    except subprocess.TimeoutExpired:
        stop_worker(process)
        return report(platform, 'timeout')
    except (ValueError, TypeError):
        return report(platform, 'internal_error')
    except BaseException:
        stop_worker(process)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', choices=tuple(NEXT), required=True)
    args = parser.parse_args()
    print(json.dumps(worker(args.worker), ensure_ascii=False))
