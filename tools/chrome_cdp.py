"""Start the dedicated Dzen Chrome profile without touching ordinary windows."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import urlopen
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def endpoint():
    value = os.getenv('DZEN_CDP_URL', 'http://127.0.0.1:9222')
    parsed = urlparse(value)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
            or parsed.username or parsed.password or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment or parsed.port == 0):
        raise ValueError('CDP must be a local HTTP endpoint (use an SSH tunnel for remote access).')
    return value.rstrip('/')


def healthcheck():
    with urlopen(endpoint() + '/json/version', timeout=2) as response:
        data = json.load(response)
    return bool(data.get('Browser') and data.get('webSocketDebuggerUrl'))


def chrome_binary():
    configured = os.getenv('CHROME_BINARY')
    candidates = [configured] if configured else []
    if os.name == 'nt':
        candidates += [str(Path(os.getenv(key, '')) / 'Google/Chrome/Application/chrome.exe')
                       for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')]
    candidates += [shutil.which(name) for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser')]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def ensure_chrome(timeout=20):
    mode = os.getenv('DZEN_CDP_MODE', 'local')
    if mode not in ('local', 'external'):
        raise ValueError('DZEN_CDP_MODE must be local or external.')
    try:
        if healthcheck():
            return {'cdp': 'ready', 'started': False}
    except (OSError, ValueError):
        pass
    if mode == 'external':
        raise RuntimeError('External CDP unavailable; reconnect the SSH tunnel and Windows Chrome.')
    binary = chrome_binary()
    if not binary:
        raise RuntimeError('Chrome not installed; set CHROME_BINARY or install Google Chrome.')
    default_profile = 'C:/temp/chrome-cdp-profile' if os.name == 'nt' else str(ROOT / '.local/auth/chrome-dzen')
    profile = Path(os.getenv('DZEN_CHROME_PROFILE') or default_profile)
    profile = (ROOT / profile).resolve() if not profile.is_absolute() else profile.resolve()
    profile.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt' and not os.getenv('DISPLAY') and not os.getenv('WAYLAND_DISPLAY'):
        raise RuntimeError('Linux needs a desktop or persistent Xvfb DISPLAY for interactive Dzen login.')
    if os.name != 'nt' and os.geteuid() == 0:
        raise RuntimeError('Start Chrome as a desktop user, or use an external authorized browser.')
    port = urlparse(endpoint()).port or 80
    if os.name == 'nt':
        helper = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-File',
                                 str(ROOT / 'dzen-publish/ensure_cdp_chrome.ps1'),
                                 '-Port', str(port), '-TimeoutSeconds', str(timeout), '-ProfilePath', str(profile)],
                                capture_output=True, timeout=timeout + 10)
        if helper.returncode or not healthcheck():
            raise RuntimeError('Windows CDP starter failed; inspect the dedicated Chrome profile.')
        return {'cdp': 'ready', 'started': True}
    command = [binary, f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1',
               f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check', 'https://dzen.ru']
    kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if healthcheck():
                return {'cdp': 'ready', 'started': True}
        except (OSError, ValueError):
            pass
        time.sleep(.5)
    raise RuntimeError('Chrome CDP did not become ready; inspect the dedicated profile locally.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    from dotenv import load_dotenv
    if os.getenv('SMM_TESTING') != '1':
        load_dotenv(ROOT / '.env', override=False)
    try:
        print(json.dumps({'cdp': 'ready' if healthcheck() else 'unavailable'} if args.check else ensure_chrome()))
    except Exception as error:
        print(json.dumps({'error': type(error).__name__, 'ready': False}))
        raise SystemExit(2)
