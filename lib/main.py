import asyncio
import builtins
import collections
import contextlib
import gc
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
for _p in (ROOT, os.path.join(ROOT, 'lib', 'package')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import lib.config
from lib.utils import helper
from lib.utils import database as dbmod
from lib.utils import connection as Connection
from lib.utils import plugins as plugmod
from lib.utils.color import chalk
from lib.utils.handler import G
from lib.utils.helper import format_error, oc, register_interval, run_every
from lib.utils.reload import start_reload_system
from lib.utils.simple import auto_merge_lid_users


TUNNEL_URL_PATH = os.path.join(os.getcwd(), 'data', 'tunnel', 'tunnel.txt')


TUNNEL_PID_PATH = os.path.join(os.getcwd(), 'data', 'tunnel', 'tunnel.pid')


TUNNEL_HEALTH_INTERVAL_S = 60


TUNNEL_HEALTH_TIMEOUT_S = 15


TUNNEL_PREVENTIVE_RESTART_MS = 4 * 60 * 60 * 1000


TUNNEL_URL_WAIT_S = 25


MAX_TAIL_LINES = 20


NAMED_TUNNEL_TOKEN = os.environ.get('CF_TOKEN') or ''


NAMED_TUNNEL_HOSTNAME = (os.environ.get('CF_HOSTNAME') or '')


NAMED_TUNNEL_HOSTNAME = NAMED_TUNNEL_HOSTNAME.replace('https://', '', 1) if NAMED_TUNNEL_HOSTNAME.startswith('https://') else NAMED_TUNNEL_HOSTNAME


NAMED_TUNNEL_HOSTNAME = NAMED_TUNNEL_HOSTNAME.replace('http://', '', 1) if NAMED_TUNNEL_HOSTNAME.startswith('http://') else NAMED_TUNNEL_HOSTNAME


if NAMED_TUNNEL_HOSTNAME.endswith('/'):
    NAMED_TUNNEL_HOSTNAME = NAMED_TUNNEL_HOSTNAME[:-1]


CLOUDFLARED_BIN_PATH = os.path.join(os.getcwd(), 'data', 'tunnel', 'cloudflared.exe' if sys.platform == 'win32' else 'cloudflared')


CLOUDFLARED_DOWNLOAD_URLS = {
    'linux-x64': 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64',
    'linux-arm64': 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64',
    'linux-arm': 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm',
    'win32-x64': 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe',
    'win32-arm64': 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-arm64.exe',
    'darwin-x64': 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz',
    'darwin-arm64': 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz',
}


URL_REGEX = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com', re.I)


NAMED_READY_REGEX = re.compile(r'registered tunnel connection|connection [a-f0-9-]+ registered', re.I)


_pending = set()


_restart_state = {'busy': False}


def _warn(text):
    print(chalk.yellow('Tunnel') + chalk.gray(text), file=sys.stderr)


def _error(text):
    print(chalk.red('Tunnel') + chalk.gray(text), file=sys.stderr)


def _fire(coro):
    task = asyncio.ensure_future(coro)
    _pending.add(task)

    def done(t):
        _pending.discard(t)
        if not t.cancelled() and t.exception() is not None:
            print(t.exception(), file=sys.stderr)
    task.add_done_callback(done)
    return task


def _js_json(value):
    if value is None:
        return 'undefined'
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _request(method, url, headers=None, body=None, timeout=30):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode('utf-8', 'replace')
        except Exception:
            text = ''
        return e.code, text


def _parse_json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def _ping(url, timeout):
    req = urllib.request.Request(url, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


async def ping_url(url, timeout_s):
    return await asyncio.to_thread(_ping, url, timeout_s)


def _download_file(url, dest):
    req = urllib.request.Request(url, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=300) as res:
            status = res.status
            data = res.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Download failed with status {e.code}')
    if status < 200 or status >= 300:
        raise RuntimeError(f'Download failed with status {status}')
    with open(dest, 'wb') as fh:
        fh.write(data)


def _extract_mac_tarball(tarball_path, dest_bin_path):
    dest_dir = os.path.dirname(dest_bin_path)
    proc = subprocess.run(['tar', '-xzf', tarball_path, '-C', dest_dir])
    if proc.returncode != 0:
        raise RuntimeError(f'tar exited with code {proc.returncode}')
    os.unlink(tarball_path)
    extracted = os.path.join(dest_dir, 'cloudflared')
    if extracted != dest_bin_path:
        os.replace(extracted, dest_bin_path)


def remove_broken_cloudflared_binary(bin_path):
    if bin_path != CLOUDFLARED_BIN_PATH:
        return
    try:
        if os.path.exists(bin_path):
            os.unlink(bin_path)
            _warn(' Removed cloudflared binary after spawn failure, will re-download next attempt.')
    except Exception as e:
        _error(f' Failed to remove broken cloudflared binary: {e}')


async def ensure_cloudflared_binary():
    if os.path.exists(CLOUDFLARED_BIN_PATH):
        return CLOUDFLARED_BIN_PATH
    plat = sys.platform
    machine = os.uname().machine.lower() if hasattr(os, 'uname') else os.environ.get('PROCESSOR_ARCHITECTURE', '').lower()
    if machine in ('amd64',):
        machine = 'x86_64'
    arm64 = machine in ('arm64', 'aarch64')
    arm32 = (not arm64) and machine.startswith('arm')
    if plat == 'linux':
        key = 'linux-arm64' if arm64 else 'linux-arm' if arm32 else 'linux-x64'
    elif plat == 'win32':
        key = 'win32-arm64' if arm64 else 'win32-x64'
    elif plat == 'darwin':
        key = 'darwin-arm64' if arm64 else 'darwin-x64'
    else:
        _warn(f" Unsupported platform {plat}. Falling back to 'cloudflared' on PATH.")
        return 'cloudflared'
    url = CLOUDFLARED_DOWNLOAD_URLS[key]
    print(chalk.red('Tunnel') + chalk.gray(f' binary not found, downloading ({key})...'))
    try:
        directory = os.path.dirname(CLOUDFLARED_BIN_PATH)
        os.makedirs(directory, exist_ok=True)
        if plat == 'darwin':
            tarball = os.path.join(directory, 'cloudflared.tgz')
            await asyncio.to_thread(_download_file, url, tarball)
            await asyncio.to_thread(_extract_mac_tarball, tarball, CLOUDFLARED_BIN_PATH)
        else:
            await asyncio.to_thread(_download_file, url, CLOUDFLARED_BIN_PATH)
        if plat != 'win32':
            os.chmod(CLOUDFLARED_BIN_PATH, 0o755)
        print(chalk.greenBright('Tunnel') + chalk.gray(' cloudflared downloaded successfully.'))
        return CLOUDFLARED_BIN_PATH
    except Exception as e:
        _error(f" Failed to auto-download cloudflared: {e}. Falling back to 'cloudflared' on PATH.")
        return 'cloudflared'


def read_saved_tunnel():
    try:
        if not os.path.exists(TUNNEL_URL_PATH) or not os.path.exists(TUNNEL_PID_PATH):
            return None
        with open(TUNNEL_URL_PATH, 'r', encoding='utf-8') as fh:
            url = fh.read().strip()
        with open(TUNNEL_PID_PATH, 'r', encoding='utf-8') as fh:
            match = re.match(r'\s*([+-]?\d+)', fh.read())
        pid = int(match.group(1)) if match else 0
        if not url or not url.startswith('https://') or not pid:
            return None
        return {'url': url, 'pid': pid}
    except Exception:
        return None


def save_tunnel(url, pid):
    try:
        data_dir = os.path.dirname(TUNNEL_URL_PATH)
        os.makedirs(data_dir, exist_ok=True)
        with open(TUNNEL_URL_PATH, 'w', encoding='utf-8') as fh:
            fh.write(url)
        with open(TUNNEL_PID_PATH, 'w', encoding='utf-8') as fh:
            fh.write(str(pid))
    except Exception as e:
        _warn(f' Failed saving tunnel.txt/pid: {e}')


def clear_saved_tunnel():
    for path in (TUNNEL_URL_PATH, TUNNEL_PID_PATH):
        with contextlib.suppress(Exception):
            os.unlink(path)


def is_pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def get_published_url(tunnel_url):
    cf_host = os.environ.get('CF_HOSTNAME')
    if cf_host:
        return f'https://{cf_host}'
    return tunnel_url


async def push_tunnel_url_to_github(url):
    token = os.environ.get('GIT_CLASSIC_KEY')
    if not token:
        return
    git_user = os.environ.get('GIT_USER')
    git_repo = os.environ.get('GIT_REPO')
    if not git_user or not git_repo:
        _warn(' GIT_USER/GIT_REPO belum diset di .env, skip push tunnel URL')
        return
    try:
        api_url = f'https://api.github.com/repos/{git_user}/{git_repo}'
        status, text = await asyncio.to_thread(
            _request, 'PATCH', api_url,
            {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json', 'Content-Type': 'application/json'},
            json.dumps({'homepage': url}).encode('utf-8'),
        )
        if not 200 <= status < 300:
            _warn(f' Failed to update repo homepage on GitHub ({status}): {text}')
    except Exception as e:
        _warn(f' Failed to update repo homepage on Github: {e}')


async def push_tunnel_url_to_workers_kv(url):
    api_token = os.environ.get('CF_KV_TOKEN')
    account_id = os.environ.get('CF_ACCOUNT_ID')
    namespace_id = os.environ.get('CF_KV_NAMESPACE_ID')
    if not api_token or not account_id or not namespace_id:
        return
    try:
        status, text = await asyncio.to_thread(
            _request, 'PUT',
            f'https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/current_url',
            {'Authorization': f'Bearer {api_token}', 'Content-Type': 'text/plain'},
            url.encode('utf-8'),
        )
        data = _parse_json(text)
        ok = 200 <= status < 300
        if not ok or not (isinstance(data, dict) and data.get('success')):
            errors = _js_json(data.get('errors')) if isinstance(data, dict) else ''
            _warn(f' Failed to update Workers KV ({status}): {errors}')
        else:
            print(chalk.greenBright('Tunnel') + chalk.gray(' Workers KV updated'))
    except Exception as e:
        _warn(f' Failed to update Workers KV: {e}')


async def push_tunnel_url_to_uptimerobot(url):
    api_key = os.environ.get('UPTIMEROBOT_API_KEY')
    monitor_id = os.environ.get('UPTIMEROBOT_MONITOR_ID')
    if not api_key or not monitor_id:
        return
    try:
        body = urllib.parse.urlencode({
            'api_key': api_key,
            'id': monitor_id,
            'url': url if url.endswith('/api/health') else f'{url}/api/health',
        }).encode('utf-8')
        status, text = await asyncio.to_thread(
            _request, 'POST', 'https://api.uptimerobot.com/v2/editMonitor',
            {'Content-Type': 'application/x-www-form-urlencoded', 'Cache-Control': 'no-cache'},
            body,
        )
        data = _parse_json(text)
        if not (200 <= status < 300) or not (isinstance(data, dict) and data.get('stat') == 'ok'):
            detail = _js_json(data) if data is not None else str(status)
            _warn(f' Failed to update UptimeRobot monitor: {detail}')
        else:
            print(chalk.greenBright('Tunnel') + chalk.gray(' UptimeRobot monitor updated'))
    except Exception as e:
        _warn(f' Failed to update UptimeRobot monitor: {e}')


def _set_website_url(url):
    if G.website_state is None:
        G.website_state = {'mode': 'tunnel', 'url': url}
    else:
        G.website_state['url'] = url


def _kill_proc(proc):
    with contextlib.suppress(Exception):
        if proc is not None:
            proc.terminate()


def _spawn_process(bin_path, args):
    return subprocess.Popen(
        [bin_path, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _pump(stream, on_line):
    with contextlib.suppress(Exception):
        for raw in iter(stream.readline, b''):
            on_line(raw.decode('utf-8', 'replace'))


def _start_readers(proc, on_line, on_exit):
    for stream in (proc.stdout, proc.stderr):
        threading.Thread(target=_pump, args=(stream, on_line), daemon=True).start()

    def waiter():
        code = proc.wait()
        on_exit(code)
    threading.Thread(target=waiter, daemon=True).start()


def _exit_code_text(code):
    return str(code) if code is not None and code >= 0 else 'null'


class TunnelSession:
    def __init__(self, opts):
        self.opts = opts
        self.reconnect_attempts = 0
        self.is_reconnecting = False
        self.retry_handle = None
        self.last_start_at = 0

    def _clear_retry(self):
        if self.retry_handle is not None:
            self.retry_handle.cancel()
            self.retry_handle = None

    def _schedule_retry(self, delay_ms):
        self._clear_retry()
        loop = asyncio.get_running_loop()
        self.retry_handle = loop.call_later(delay_ms / 1000, lambda: _fire(self._spawn_guarded()))

    async def _spawn_guarded(self):
        try:
            await self.spawn_tunnel()
        except Exception as e:
            print(e, file=sys.stderr)

    async def spawn_tunnel(self):
        if self.is_reconnecting:
            return
        if PUBLIC_HOSTNAME:
            public_url = f'http://{PUBLIC_HOSTNAME}'
            if G.tunnel is None:
                G.tunnel = {'proc': None, 'url': public_url, 'pid': None, 'reused': True, 'static': True}
                print(chalk.greenBright('Server ') + chalk.gray(PUBLIC_HOSTNAME))
                _fire(push_tunnel_url_to_github(public_url))
                _fire(push_tunnel_url_to_uptimerobot(public_url))
            return
        self._clear_retry()
        if NAMED_TUNNEL_TOKEN:
            return await self.spawn_named_tunnel()
        saved = read_saved_tunnel()
        if saved and is_pid_alive(saved['pid']):
            G.tunnel = {'proc': None, 'url': saved['url'], 'pid': saved['pid'], 'reused': True}
            published = get_published_url(saved['url'])
            self.opts['server'] = published
            _set_website_url(published)
            self.last_start_at = time.time() * 1000
            print(chalk.greenBright('Tunnel ') + chalk.gray(published.replace('https://', '')))
            _fire(push_tunnel_url_to_github(published))
            _fire(push_tunnel_url_to_uptimerobot(published))
            _fire(push_tunnel_url_to_workers_kv(saved['url']))
            return
        if saved and not is_pid_alive(saved['pid']):
            clear_saved_tunnel()
        if saved and is_pid_alive(saved['pid']):
            with contextlib.suppress(Exception):
                os.kill(saved['pid'], 15)
            clear_saved_tunnel()
        self.is_reconnecting = True
        try:
            if G.tunnel and G.tunnel.get('proc'):
                _kill_proc(G.tunnel['proc'])
                G.tunnel = None
            cloudflared_bin = await ensure_cloudflared_binary()
            try:
                proc = _spawn_process(cloudflared_bin, ['tunnel', '--url', f'http://127.0.0.1:{PORT}', '--no-autoupdate'])
            except Exception:
                remove_broken_cloudflared_binary(cloudflared_bin)
                raise
            url = await self._wait_for_url(proc)
            G.tunnel = {'proc': proc, 'url': url, 'pid': proc.pid, 'reused': False}
            published = get_published_url(url)
            self.opts['server'] = published
            _set_website_url(published)
            self.reconnect_attempts = 0
            self.is_reconnecting = False
            self.last_start_at = time.time() * 1000
            save_tunnel(url, proc.pid)
            _fire(push_tunnel_url_to_github(published))
            _fire(push_tunnel_url_to_uptimerobot(published))
            _fire(push_tunnel_url_to_workers_kv(url))
            print(chalk.greenBright('Tunnel ') + chalk.gray(published.replace('https://', '')))
        except Exception as err:
            print(chalk.red('Tunnel Failed to spawn:'), err, file=sys.stderr)
            self.reconnect_attempts += 1
            delay = min(30000, 5000 * self.reconnect_attempts)
            self.is_reconnecting = False
            self._schedule_retry(delay)

    async def _wait_for_url(self, proc):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        tail = collections.deque(maxlen=MAX_TAIL_LINES)
        state = {'resolved': False, 'abandoned': False}

        def set_url(url):
            if not fut.done():
                fut.set_result(url)

        def set_error(err):
            if not fut.done():
                fut.set_exception(err)

        def on_line(text):
            for line in text.split('\n'):
                if line.strip():
                    tail.append(line)
            match = URL_REGEX.search(text)
            if match and not state['resolved']:
                state['resolved'] = True
                loop.call_soon_threadsafe(set_url, match.group(0))

        def on_exit_thread(code):
            loop.call_soon_threadsafe(on_exit, code)

        def on_exit(code):
            if not state['resolved']:
                lines = list(tail)
                suffix = '\n' + '\n'.join(lines) if lines else ' (no output captured - likely failed to even start; check binary architecture/permissions)'
                set_error(RuntimeError(f'cloudflared exited early with code {_exit_code_text(code)}:{suffix}'))
                return
            if state['abandoned']:
                return
            clear_saved_tunnel()
            self.is_reconnecting = False
            self._schedule_retry(5000)

        _start_readers(proc, on_line, on_exit_thread)
        try:
            url = await asyncio.wait_for(fut, TUNNEL_URL_WAIT_S)
        except asyncio.TimeoutError:
            state['abandoned'] = True
            lines = list(tail)
            suffix = '\n' + '\n'.join(lines) if lines else ''
            raise RuntimeError(f'Timed out waiting for tunnel URL{suffix}')
        return url

    async def spawn_named_tunnel(self):
        self._clear_retry()
        self.is_reconnecting = True
        try:
            if G.tunnel and G.tunnel.get('proc'):
                _kill_proc(G.tunnel['proc'])
                G.tunnel = None
            cloudflared_bin = await ensure_cloudflared_binary()
            proc = _spawn_process(cloudflared_bin, ['tunnel', 'run', '--token', NAMED_TUNNEL_TOKEN])
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            state = {'resolved': False, 'abandoned': False}

            def set_ready():
                if not fut.done():
                    fut.set_result(None)

            def set_error(err):
                if not fut.done():
                    fut.set_exception(err)

            def on_line(text):
                if not state['resolved'] and NAMED_READY_REGEX.search(text):
                    state['resolved'] = True
                    loop.call_soon_threadsafe(set_ready)

            def on_exit(code):
                loop.call_soon_threadsafe(exit_handler, code)

            def exit_handler(code):
                if not state['resolved']:
                    set_error(RuntimeError(f'cloudflared (named tunnel) exited early with code {_exit_code_text(code)}'))
                    return
                if state['abandoned']:
                    return
                self.is_reconnecting = False
                G.tunnel = None
                self._schedule_retry(5000)

            _start_readers(proc, on_line, on_exit)
            try:
                await asyncio.wait_for(fut, TUNNEL_URL_WAIT_S)
            except asyncio.TimeoutError:
                state['abandoned'] = True
                raise RuntimeError('Timed out waiting for named tunnel to register')

            url = f'https://{NAMED_TUNNEL_HOSTNAME}' if NAMED_TUNNEL_HOSTNAME else None
            G.tunnel = {'proc': proc, 'url': url, 'pid': proc.pid, 'reused': False, 'named': True}
            if url:
                self.opts['server'] = url
                _set_website_url(url)
            self.reconnect_attempts = 0
            self.is_reconnecting = False
            self.last_start_at = time.time() * 1000
            label = url.replace('https://', '') if url else 'Connected (set CF_HOSTNAME di .env untuk menampilkan URL & aktifkan health check publik)'
            print(chalk.greenBright('Tunnel (named) ') + chalk.gray(label))
            if url:
                _fire(push_tunnel_url_to_github(url))
                _fire(push_tunnel_url_to_uptimerobot(url))
        except Exception as err:
            print(chalk.red('Tunnel (named) Failed to spawn:'), err, file=sys.stderr)
            self.reconnect_attempts += 1
            delay = min(30000, 5000 * self.reconnect_attempts)
            self.is_reconnecting = False
            self._schedule_retry(delay)

    async def check_tunnel_health(self):
        tunnel = G.tunnel or {}
        url = tunnel.get('url')
        pid = tunnel.get('pid')
        is_named = bool(tunnel.get('named'))
        if not pid:
            return
        if not is_pid_alive(pid):
            print(chalk.red('Tunnel') + chalk.gray(' Proses cloudflared sudah mati, spawn ulang...'), file=sys.stderr)
            _kill_proc(tunnel.get('proc'))
            G.tunnel = None
            clear_saved_tunnel()
            _fire(self._spawn_guarded())
            return
        local_ok = await ping_url(f'http://127.0.0.1:{PORT}/api/health', TUNNEL_HEALTH_TIMEOUT_S)
        if not local_ok:
            return
        now = time.time() * 1000
        due = bool(self.last_start_at) and (now - self.last_start_at >= TUNNEL_PREVENTIVE_RESTART_MS)
        if is_named and not url:
            if not due:
                return
        elif url:
            public_ok = await ping_url(url, TUNNEL_HEALTH_TIMEOUT_S)
            if public_ok and not due:
                return
        _kill_proc(tunnel.get('proc'))
        G.tunnel = None
        clear_saved_tunnel()
        _fire(self._spawn_guarded())


async def setup_tunnel(opts):
    session = TunnelSession(opts)
    G.startTunnel = session.spawn_tunnel
    G.start_tunnel = session.spawn_tunnel
    register_interval(asyncio.ensure_future(run_every(TUNNEL_HEALTH_INTERVAL_S, session.check_tunnel_health)))
    return await session.spawn_tunnel()


async def restart_tunnel():
    if not callable(G.startTunnel):
        return
    if _restart_state['busy']:
        return
    if G.tunnel is not None:
        return
    _restart_state['busy'] = True
    try:
        await G.startTunnel()
    finally:
        asyncio.get_running_loop().call_later(5, lambda: _restart_state.update(busy=False))


G.restartTunnel = restart_tunnel


G.restart_tunnel = restart_tunnel

helper.refresh()
G.opts = helper.runtime_opts
G.prefix = helper.prefix_list

BAILEYS_STACK_MARKERS = ('/baileys/', '@whiskeysockets/baileys', 'undici', '/baileys-lite/', '/baileys_lite/')
IGNORED_ERROR_WINDOW_S = 120
IGNORED_ERROR_THRESHOLD = 10
FORCE_RECONNECT_DEBOUNCE_S = 5
MUTED_LOG_PATTERNS = ('Closing session',)
HEAP_WARN_MB = 350
HEAP_EXIT_MB = 512
MEMORY_CHECK_S = 10 * 60
TMP_CLEAR_S = 20 * 60
TMP_AGE_LIMIT_MS = 60 * 60 * 1000
DB_AUTOSAVE_S = 20 * 60
LIMIT_REFILL_S = 10 * 60
LID_MERGE_FIRST_S = 5
LID_MERGE_S = 30 * 60
SHUTDOWN_WRITE_TIMEOUT_S = 12
SHUTDOWN_RETRY_TIMEOUT_S = 5
SUPPORT_PROBE_TIMEOUT_S = 30
PORT = os.environ.get('PORT') or os.environ.get('SERVER_PORT') or '8080'
PUBLIC_HOSTNAME = os.environ.get('HOSTNAME_PUBLIC') or os.environ.get('PUBLIC_HOSTNAME') or ''

state = {
    'shutting_down': False,
    'loop': None,
    'ignored': [],
    'last_force_reconnect': 0.0,
    'pending': set(),
    'kept': set(),
}


def fire(coro):
    task = asyncio.ensure_future(coro)
    state['pending'].add(task)
    task.add_done_callback(state['pending'].discard)
    return task


def call_later_threadsafe(delay_s, fn):
    loop = state['loop']
    if loop is None:
        return
    loop.call_soon_threadsafe(lambda: loop.call_later(delay_s, fn))


def error_message(err):
    return str(err)


def error_stack(err):
    if isinstance(err, BaseException):
        return ''.join(traceback.format_exception(type(err), err, err.__traceback__))
    return ''


def is_baileys_internal_error(err):
    if not err:
        return False
    stack = error_stack(err)
    return any(marker in stack for marker in BAILEYS_STACK_MARKERS)


def is_muted_log(args):
    text = ' '.join(a if isinstance(a, str) else '' for a in args)
    return any(pattern in text for pattern in MUTED_LOG_PATTERNS)


def install_console_mute():
    if getattr(builtins.print, '_hiro_quiet', False):
        return
    original = builtins.print

    def quiet_print(*args, **kwargs):
        if is_muted_log(args):
            return
        original(*args, **kwargs)
    quiet_print._hiro_quiet = True
    builtins.print = quiet_print


async def force_reconnect_guarded(conn):
    try:
        await Connection.force_reconnect(conn, {
            'store': Connection.store,
            'authState': Connection.auth_state,
            'logger': Connection.logger,
        }, 'baileys internal error')
    except Exception as err:
        print(chalk.red('Forced reconnect error:'), error_message(err), file=sys.stderr)


def note_ignored_and_maybe_restart(label, msg):
    now = time.time()
    stamps = state['ignored']
    stamps.append(now)
    stamps[:] = [t for t in stamps if now - t <= IGNORED_ERROR_WINDOW_S]
    print(chalk.yellow(label) + chalk.gray(f' Baileys internal error ({len(stamps)}/{IGNORED_ERROR_THRESHOLD} in window): ') + chalk.gray(msg), file=sys.stderr)
    if len(stamps) >= IGNORED_ERROR_THRESHOLD:
        print(chalk.red('Watchdog') + chalk.gray(f' Detected {len(stamps)}x internal errors in {IGNORED_ERROR_WINDOW_S}s — connection looks stuck. Restarting process...'), file=sys.stderr)
        stamps.clear()
        call_later_threadsafe(0.2, lambda: fire(flush_and_exit(1, 'watchdog restart')))
        return
    if now - state['last_force_reconnect'] < FORCE_RECONNECT_DEBOUNCE_S:
        return
    state['last_force_reconnect'] = now
    conn = Connection.state.conn
    if not conn:
        return
    fire(force_reconnect_guarded(conn))


def loop_exception_handler(loop, context):
    err = context.get('exception')
    if err is not None:
        if is_baileys_internal_error(err):
            note_ignored_and_maybe_restart('Ignored', error_message(err))
            return
        label = '[ UnhandledRejection ]' if context.get('future') is not None else '[ UncaughtException ]'
        print(chalk.red(label), format_error(err), file=sys.stderr)
        return
    print(chalk.red('[ UncaughtException ]'), context.get('message'), file=sys.stderr)


def sys_excepthook(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    if is_baileys_internal_error(exc):
        note_ignored_and_maybe_restart('Ignored', error_message(exc))
        return
    print(chalk.red('[ UncaughtException ]'), ''.join(traceback.format_exception(exc_type, exc, tb)), file=sys.stderr)


def thread_excepthook(args):
    if issubclass(args.exc_type, SystemExit):
        return
    sys_excepthook(args.exc_type, args.exc_value, args.exc_traceback)


def install_error_hooks(loop):
    state['loop'] = loop
    loop.set_exception_handler(loop_exception_handler)
    sys.excepthook = sys_excepthook
    threading.excepthook = thread_excepthook


async def write_database():
    if G.db is not None and G.db.data is not None:
        await G.db.write()


async def wait_task(task, seconds):
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if not done:
        raise TimeoutError('timeout')
    return task.result()


async def flush_and_exit(code, signal_name=None):
    if state['shutting_down']:
        return
    state['shutting_down'] = True
    if signal_name:
        print(chalk.yellow('[ Shutdown ]') + chalk.gray(f' Received {signal_name}, saving database...'))
    try:
        Connection.store.writeToFile(Connection.store_file)
    except Exception as err:
        print(chalk.red('[ Shutdown ]'), 'Failed to save message store:', error_message(err), file=sys.stderr)
    pending = asyncio.ensure_future(write_database())
    try:
        await wait_task(pending, SHUTDOWN_WRITE_TIMEOUT_S)
    except Exception as err:
        print(chalk.red('[ Shutdown ]'), 'Database save slow, giving it one more moment:', error_message(err), file=sys.stderr)
        try:
            await wait_task(pending, SHUTDOWN_RETRY_TIMEOUT_S)
        except Exception as err2:
            print(chalk.red('[ Shutdown ]'), 'Failed to save database before exit:', error_message(err2), file=sys.stderr)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)


def install_graceful_shutdown(loop):
    for sig, name in ((signal.SIGTERM, 'SIGTERM'), (signal.SIGINT, 'SIGINT')):
        def on_signal(n=name):
            fire(flush_and_exit(0, n))
        try:
            loop.add_signal_handler(sig, on_signal)
        except Exception:
            signal.signal(sig, lambda *_, n=name: call_later_threadsafe(0, lambda: fire(flush_and_exit(0, n))))


def proc_rss_mb():
    try:
        with open('/proc/self/status', 'r', encoding='utf-8') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def rss_mb():
    value = proc_rss_mb()
    if value is not None:
        return value
    try:
        import resource
    except ImportError:
        return 0
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak // (1024 * 1024) if sys.platform == 'darwin' else peak // 1024


def force_gc():
    gc.collect()
    return rss_mb()


def check_memory():
    heap = force_gc()
    if heap > HEAP_EXIT_MB:
        print(chalk.red('Memory') + chalk.gray(f' Heap {heap}MB > {HEAP_EXIT_MB}MB! Restarting...'), file=sys.stderr)
        fire(flush_and_exit(1, 'memory limit'))
    elif heap > HEAP_WARN_MB:
        print(chalk.yellow('Memory') + chalk.gray(f' Heap {heap}MB almost reaching the limit'), file=sys.stderr)


def clear_tmp():
    dirs = [tempfile.gettempdir(), os.path.join(os.getcwd(), 'data', 'tmp')]
    now_ms = time.time() * 1000
    for directory in dirs:
        if not os.path.exists(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            file_path = os.path.join(directory, name)
            try:
                st = os.stat(file_path)
                if stat.S_ISREG(st.st_mode) and now_ms - st.st_mtime * 1000 >= TMP_AGE_LIMIT_MS:
                    os.unlink(file_path)
            except OSError:
                continue


def cap_for_level(caps, level):
    try:
        value = caps[level]
    except (IndexError, TypeError, KeyError):
        value = None
    return value or 10


def limit_refill():
    data = G.db.data if G.db is not None else None
    if data is None or data.get('users') is None or data.get('settings') is None:
        return
    setting = next(iter(data['settings'].values()), None)
    if setting is None:
        return
    if time.time() * 1000 - (setting.get('resetlimit') or 0) < LIMIT_REFILL_S * 1000:
        return
    caps = oc(G.settings, 'tier', 'limit_capacity') or []
    for user in data['users'].values():
        if not user.get('registered'):
            continue
        cap = cap_for_level(caps, user.get('level'))
        current = user.get('limit') or 0
        if current >= cap:
            continue
        user['limit'] = min(current + 1, cap)
    setting['resetlimit'] = int(time.time() * 1000)


def patch_store_write_to_file():
    store = Connection.store
    original = getattr(store, 'writeToFile', None)
    if not callable(original) or getattr(original, '_hiro_patched', False):
        return

    def patched(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except Exception as err:
            message = error_message(err)
            print('Store writeToFile error:', message, file=sys.stderr)
            if 'isZero' in message or 'toJSON' in message:
                return None
            raise
    patched._hiro_patched = True
    store.writeToFile = patched


async def db_autosave():
    if G.db is not None and G.db.data is not None:
        await G.db.write()
    Connection.store.writeToFile(Connection.store_file)


async def merge_lids_once():
    try:
        await auto_merge_lid_users()
    except Exception as err:
        print(error_message(err), file=sys.stderr)


async def lid_merge_loop():
    await asyncio.sleep(LID_MERGE_FIRST_S)
    await merge_lids_once()
    while True:
        await asyncio.sleep(LID_MERGE_S)
        await merge_lids_once()


def start_periodic(seconds, fn, kept=False):
    task = helper.register_interval(asyncio.ensure_future(helper.run_every(seconds, fn)))
    if kept:
        state['kept'].add(task)
    return task


def cleanup_connection_intervals():
    registry = G.active_intervals or set()
    for task in list(registry):
        if task in state['kept']:
            continue
        task.cancel()
        registry.discard(task)


def on_connection_update(update):
    connection = oc(update, 'connection')
    if connection in ('close', 'disconnecting'):
        cleanup_connection_intervals()
        force_gc()


def plugin_timestamp():
    now = time.time()
    millis = int((now - int(now)) * 1000)
    base = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now))
    return f',"time":"{base}.{millis:03d}Z"'


def expose_helper_globals():
    G.__filename = helper.filename_of
    G.__dirname = helper.dirname_of
    G.__require = helper.require_from
    G.checkFileExists = helper.check_file_exists
    G.saveStreamToFile = helper.save_stream_to_file
    G.isReadableStream = helper.is_readable_stream
    G.importFile = helper.import_file
    G.checkSyntax = helper.check_syntax
    G.resolvePrefix = helper.resolve_prefix
    G.resolveOpts = helper.resolve_opts
    G.getPrefix = helper.get_prefix
    G.P = helper.P
    G.fileTypeFromBuffer = helper.file_type_from_buffer
    G.fileTypeStream = helper.file_type_stream


async def subbot_autoconnect():
    try:
        entry = plugmod.plugins.get('plugins/subbot/connect.py')
        if entry is None:
            return
        await entry.module.auto_connect_sub_bots()
    except Exception as err:
        print('[Subbot] Autoconnect error:', Connection.safe_err(err), file=sys.stderr)


async def start_http_server(conn):
    from lib.package.website import server as website_server
    website_server.connect(conn, PORT)
    await asyncio.sleep(0.5)


def probe_binary(args):
    try:
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=SUPPORT_PROBE_TIMEOUT_S)
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        return True
    return proc.returncode != 127


async def check_binary_support():
    ffmpeg, ffprobe, ffmpeg_webp, find = await asyncio.gather(
        asyncio.to_thread(probe_binary, ['ffmpeg']),
        asyncio.to_thread(probe_binary, ['ffprobe']),
        asyncio.to_thread(probe_binary, ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-filter_complex', 'color', '-frames:v', '1', '-f', 'webp', '-']),
        asyncio.to_thread(probe_binary, ['find', '--version']),
    )
    G.support = types.MappingProxyType({'ffmpeg': ffmpeg, 'ffprobe': ffprobe, 'ffmpegWebp': ffmpeg_webp, 'find': find})
    if not ffmpeg:
        print(chalk.red('FFMPEG') + chalk.gray(' Not installed'), file=sys.stderr)
    if ffmpeg and not ffmpeg_webp:
        print(chalk.red('FFMPEG WEBP') + chalk.gray(' Not installed'), file=sys.stderr)


def is_registered():
    return bool(oc(Connection.auth_state, 'state', 'creds', 'registered'))


async def bootstrap(loop):
    G.flush_and_exit = flush_and_exit
    G.timestamp = {'start': int(time.time() * 1000), 'connect': None}
    expose_helper_globals()
    await dbmod.load_database()
    await Connection.init()
    patch_store_write_to_file()
    Connection.on_connection_update(on_connection_update)
    plugin_logger = helper.P({'timestamp': plugin_timestamp}).child({})
    await start_reload_system({'logger': plugin_logger})
    fire(subbot_autoconnect())
    start_periodic(LIMIT_REFILL_S, limit_refill, kept=True)
    if os.environ.get('DATABASE'):
        start_periodic(DB_AUTOSAVE_S, db_autosave, kept=True)
    if G.opts.get('autocleartmp') or G.opts.get('cleartmp'):
        start_periodic(TMP_CLEAR_S, clear_tmp, kept=True)
    fire(lid_merge_loop())
    public_url = f'http://{PUBLIC_HOSTNAME}' if PUBLIC_HOSTNAME else None
    G.website_state = {'mode': 'public-hostname' if public_url else 'tunnel', 'url': public_url}
    G.getServerUrl = lambda: (G.website_state or {}).get('url') or 'https://not-loaded.yet'
    G.opts['server'] = public_url or True
    await start_http_server(Connection.state.conn)
    if is_registered():
        await setup_tunnel(G.opts)
    else:
        G.onBotOpen = lambda: setup_tunnel(G.opts)
    await check_binary_support()


async def main():
    loop = asyncio.get_running_loop()
    install_console_mute()
    install_error_hooks(loop)
    install_graceful_shutdown(loop)
    start_periodic(MEMORY_CHECK_S, check_memory, kept=True)
    try:
        await bootstrap(loop)
    except Exception as err:
        print(format_error(err), file=sys.stderr)
    while True:
        if G.force_exit:
            await flush_and_exit(1, 'reconnect exhausted')
        await asyncio.sleep(1)


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        return


if __name__ == '__main__':
    run()
