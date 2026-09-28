"""Read-only environment and optional account checks, without secret values."""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def local_checks():
    checks = {'python_3_12': sys.version_info[:2] == (3, 12)}
    for module in ('pytest', 'pytest_bdd', 'docx', 'PIL', 'playwright', 'faster_whisper', 'dotenv', 'telethon', 'googleapiclient'):
        checks[module] = importlib.util.find_spec(module) is not None
    for tool in ('ffmpeg', 'ffprobe', 'codex'):
        checks[tool] = bool(shutil.which(os.getenv(tool.upper() + '_BINARY', tool)))
    from tools.chrome_cdp import chrome_binary, healthcheck
    checks['chrome'] = bool(chrome_binary())
    try:
        checks['cdp'] = healthcheck()
    except Exception:
        checks['cdp'] = False
    checks['display'] = os.name == 'nt' or bool(os.getenv('DISPLAY') or os.getenv('WAYLAND_DISPLAY'))
    checks['word'] = False
    if os.name == 'nt' and shutil.which('powershell'):
        try:
            result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                '$ErrorActionPreference="Stop"; $w=New-Object -ComObject Word.Application; $w.Quit()'], capture_output=True, timeout=30)
            checks['word'] = result.returncode == 0
        except Exception:
            pass
    return checks


async def online_checks(platform=None):
    result = {}
    if platform in (None, 'youtube'):
        try:
            from tools.publishing_youtube import youtube_service, verify_channel
            verify_channel(youtube_service(), os.getenv('YOUTUBE_CHANNEL_ID'))
            result['youtube'] = {'status': 'ready'}
        except Exception as error:
            result['youtube'] = {'status': 'blocked', 'reason': type(error).__name__,
                                 'next': 'auth-youtube and verify channel locally'}
    if platform in (None, 'telegram'):
        client = None
        try:
            from tools.publishing_telegram import telegram_client, verify_channel
            client = telegram_client()
            await client.connect()
            await verify_channel(client, os.getenv('TELEGRAM_CHANNEL_ID'))
            result['telegram'] = {'status': 'ready'}
        except Exception as error:
            result['telegram'] = {'status': 'blocked', 'reason': type(error).__name__,
                                  'next': 'auth-telegram and verify posting rights locally'}
        finally:
            if client:
                await client.disconnect()
    if platform in (None, 'dzen'):
        from tools.dzen_preflight import run_check
        result['dzen'] = run_check()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--platform', choices=('dzen', 'youtube', 'telegram'))
    parser.add_argument('--output', type=Path, help='Save a sanitized JSON report locally')
    args = parser.parse_args(argv)
    if args.platform and not args.online:
        parser.error('--platform requires --online')
    from dotenv import load_dotenv
    if os.getenv('SMM_TESTING') != '1':
        load_dotenv(ROOT / '.env', override=False)
    if args.platform:
        report = {'accounts': asyncio.run(online_checks(args.platform))}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if all(v['status'] == 'ready' for v in report['accounts'].values()) else 2
    checks = local_checks()
    report = {'os': platform.platform(), 'python': platform.python_version(), 'checks': checks,
              'env_file_exists': (ROOT / '.env').is_file(), 'auth_directory_exists': (ROOT / '.local/auth').is_dir()}
    report['profiles'] = {
        'local': all(checks[k] for k in ('python_3_12','pytest','pytest_bdd','docx','PIL','playwright','dotenv','telethon','googleapiclient')),
        'media': all(checks[k] for k in ('docx','PIL','ffmpeg','ffprobe')),
        'asr': checks['faster_whisper'] and checks['ffmpeg'], 'agent': checks['codex'],
        'dzen_browser': checks['chrome'] and checks['cdp'] and checks['display']}
    if args.online:
        report['accounts'] = asyncio.run(online_checks())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['profiles']['local'] and (not args.online or all(v['status']=='ready' for v in report['accounts'].values())) else 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
