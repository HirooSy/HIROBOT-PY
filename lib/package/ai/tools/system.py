import asyncio
import json
import os
import platform
import sys
import time

from lib.package.ai import mcp
from lib.utils.handler import G


ROOT = os.getcwd()


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clamp_seconds(value, default, maximum):
    secs = _num(value) or default
    return min(max(secs, 1), maximum)


def _read_text(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return ''


def _meminfo():
    info = {}
    for line in _read_text('/proc/meminfo').splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            info[parts[0].rstrip(':')] = int(parts[1]) * 1024
    return info


def _cpu_model():
    for line in _read_text('/proc/cpuinfo').splitlines():
        if line.lower().startswith('model name'):
            return line.split(':', 1)[1].strip() or 'Unknown'
    return 'Unknown'


def _rss_bytes():
    for line in _read_text('/proc/self/status').splitlines():
        if line.startswith('VmRSS:'):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return 0


def _process_uptime():
    try:
        system_up = float(_read_text('/proc/uptime').split()[0])
        stat = _read_text('/proc/self/stat')
        fields = stat.rsplit(')', 1)[1].split()
        start_ticks = int(fields[19])
        return max(system_up - start_ticks / os.sysconf('SC_CLK_TCK'), 0.0)
    except (OSError, IndexError, ValueError):
        return time.time() - _BOOT_TIME


_BOOT_TIME = time.time()


def _supervised():
    try:
        with open(f'/proc/{os.getppid()}/cmdline', 'rb') as f:
            return b'lib/start.py' in f.read()
    except OSError:
        return callable(getattr(G, 'flush_and_exit', None))


async def _run(command=None, argv=None, cwd=ROOT, timeout=120):
    if argv is not None:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    else:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        return {'code': None, 'stdout': out.decode('utf-8', 'replace'), 'stderr': err.decode('utf-8', 'replace'), 'killed': True}
    return {'code': proc.returncode, 'stdout': out.decode('utf-8', 'replace'), 'stderr': err.decode('utf-8', 'replace'), 'killed': False}


async def _exec_system_time(args=None, **kw):
    tz = mcp.ctx().get('timezone') or 'Asia/Jakarta'
    info = mcp.format_date_time_in_zone(tz)
    return f"Day: {info['weekday']}\nDate: {info['date']}\nTime: {info['time']} {mcp.short_tz_label(tz)}"


async def _exec_shell_exec(args=None, **kw):
    a = _args(args, kw)
    command = a.get('command')
    cwd = a.get('cwd')
    timeout = a.get('timeout', 120)
    if not isinstance(command, str):
        return 'Error: command must be a string'
    work_dir = os.path.abspath(os.path.join(ROOT, cwd)) if cwd else ROOT
    secs = _clamp_seconds(timeout, 120, 900)
    try:
        res = await _run(command=command, cwd=work_dir, timeout=secs)
    except OSError as e:
        return f'Error: {str(e)[:500]}'
    if res['killed'] or res['code'] != 0:
        msg = f"Command failed: {command}\n{res['stderr']}"
        return f'Error: {msg[:500]}'
    out = res['stdout'] + (f"\n[stderr]\n{res['stderr']}" if res['stderr'] else '')
    return out.strip()[:4000] or '(no output)'


async def _exec_run_python(args=None, **kw):
    a = _args(args, kw)
    code = a.get('code')
    timeout = a.get('timeout', 60)
    tmp_dir = os.path.join(ROOT, 'data', 'tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_file = os.path.join(tmp_dir, f'_ai_py_{int(time.time() * 1000)}.py')
    secs = _clamp_seconds(timeout, 60, 600)
    try:
        with open(tmp_file, 'w', encoding='utf-8') as f:
            f.write(code if isinstance(code, str) else str(code or ''))
        res = await _run(argv=[sys.executable, tmp_file], cwd=ROOT, timeout=secs)
        if res['killed'] or res['code'] != 0:
            if res['killed']:
                err_msg = f"Timeout (>{timeout if timeout is not None else 60}s)"
            else:
                err_msg = res['stderr'] or f'Command failed: {sys.executable} "{tmp_file}"\n'
            return json.dumps({
                '__type': 'codeblock',
                'title': 'Python Error',
                'language': 'python',
                'code': f'# Code:\n{code}\n\n# Error:\n{err_msg}',
                'description': err_msg[:150],
            }, ensure_ascii=False)
        parts = [p for p in [res['stdout'].strip(), res['stderr'].strip()] if p]
        output = '\n[stderr]\n'.join(parts) or '(no output)'
        return json.dumps({
            '__type': 'codeblock',
            'title': 'Python Output',
            'language': 'python',
            'code': f'# Code:\n{code}\n\n# Output:\n{output}',
            'description': output[:200],
        }, ensure_ascii=False)
    except OSError as e:
        err_msg = str(e)
        return json.dumps({
            '__type': 'codeblock',
            'title': 'Python Error',
            'language': 'python',
            'code': f'# Code:\n{code}\n\n# Error:\n{err_msg}',
            'description': err_msg[:150],
        }, ensure_ascii=False)
    finally:
        try:
            os.unlink(tmp_file)
        except OSError:
            pass


async def _exec_system_info(args=None, **kw):
    mem = _meminfo()
    up = _process_uptime()
    botname = G.settings.get('botname') if G.settings else None
    cores = os.cpu_count() or 0
    total_mem = mem.get('MemTotal', 0)
    free_mem = mem.get('MemAvailable', mem.get('MemFree', 0))
    return '\n'.join([
        f'*{botname} — System Info*',
        f'OS: {platform.system()} {platform.release()} ({platform.machine()})',
        f'CPU: {_cpu_model()} × {cores} cores',
        f'RAM Total: {total_mem / 1073741824:.2f} GB',
        f'RAM Free: {free_mem / 1073741824:.2f} GB',
        f'RAM Bot: {_rss_bytes() / 1048576:.1f} MB (RSS)',
        f'Uptime: {int(up // 3600)}h {int((up % 3600) // 60)}m {int(up % 60)}s',
        f'Python: {platform.python_version()}',
        f'CWD: {ROOT}',
    ])


async def _exec_restart_bot(args=None, **kw):
    if not _supervised():
        return 'FAILED to restart: this process was not started through lib/start.py (e.g. it was run directly with "python3 lib/main.py"), so there is no process manager to send a reset signal to.'
    c = mcp.ctx()
    botname = G.settings.get('botname') if G.settings else None
    if c['conn'] is not None and c['currentJid']:
        await c['conn'].sendMessage(c['currentJid'], {'text': f'{botname} is restarting for a moment~'}, {'quoted': c['currentM']})
    if os.environ.get('DATABASE') and G.db is not None and hasattr(G.db, 'write'):
        try:
            await G.db.write()
        except Exception as e:
            print(f'[restart_bot] db.write gagal: {e}', file=sys.stderr)
    await asyncio.sleep(2)
    flush = getattr(G, 'flush_and_exit', None)
    if callable(flush):
        await flush(1, 'restart_bot')
    os._exit(1)


async def _exec_install_package(args=None, **kw):
    a = _args(args, kw)
    package_name = a.get('package_name')
    c = mcp.ctx()
    if c['conn'] is not None and c['currentJid']:
        await c['conn'].sendMessage(c['currentJid'], {'text': f'Installing {package_name}... please wait a moment'}, {'quoted': c['currentM']})
    argv = [sys.executable, '-m', 'pip', 'install', '--no-input', '--disable-pip-version-check', '--', str(package_name)]
    try:
        res = await _run(argv=argv, cwd=ROOT, timeout=600)
    except OSError as e:
        return f'Install failed for {package_name}: {str(e)[:300]}'
    if res['killed'] or res['code'] != 0:
        msg = f"Command failed: pip install {package_name}\n{res['stderr']}"
        return f'Install failed for {package_name}: {msg[:300]}'
    return f"{package_name} installed.\n\n{res['stdout'][-500:]}"


default = [
    {
        'name': 'system_time',
        'description': (
            "Get the current date and time in the sender's time zone (adjusts automatically to the country of "
            "the sender's number; defaults to Asia/Jakarta if the sender is from Indonesia or the country cannot "
            'be detected).'
        ),
        'parameters': {},
        'execute': _exec_system_time,
    },
    {
        'name': 'shell_exec',
        'description': (
            'Run a shell command on the server (ls, cat, grep, ps, npm, git, etc). The command runs at the OS '
            'level, so it can automatically access the whole server filesystem (not just the project folder) — '
            'commands with an absolute path (e.g. "ls /", "cat /etc/hosts") work normally without any setup. '
            'Long-running commands are allowed: pass a larger timeout for builds, installs or other slow jobs.'
        ),
        'parameters': {
            'command': {'type': 'string', 'description': 'The shell command to run', 'required': True},
            'cwd': {
                'type': 'string',
                'description': (
                    'Working directory (optional, defaults to the bot project root). An absolute path (e.g. "/", '
                    '"/home") may be given to move the working directory anywhere on the server.'
                ),
                'required': False,
            },
            'timeout': {
                'type': 'number',
                'description': 'Execution timeout in seconds (default: 120, maximum: 900)',
                'required': False,
            },
        },
        'execute': _exec_shell_exec,
    },
    {
        'name': 'run_python',
        'description': (
            'Run Python code on the server and return its output. Good for: math calculations, data manipulation, '
            'utility scripts, text analysis, etc. The code is written to a temporary file and then executed with '
            'python3. The output (stdout + stderr) is returned to the AI and sent to the user as a codeblock. '
            'IMPORTANT about HTTP requests: do NOT use "requests" (it is not built in and needs to be installed '
            'first) — use Python\'s built-in "urllib.request" for simple GET/POST, which is enough and always '
            'available without installing anything. Other external libraries (pandas, numpy, etc) may not exist '
            'either; only if you really need one and there is no built-in alternative, install it with '
            'install_package or shell_exec. Long-running scripts are allowed: pass a larger timeout.'
        ),
        'parameters': {
            'code': {'type': 'string', 'description': 'The Python code to run', 'required': True},
            'timeout': {
                'type': 'number',
                'description': 'Execution timeout in seconds (default: 60, maximum: 600)',
                'required': False,
            },
        },
        'execute': _exec_run_python,
    },
    {
        'name': 'system_info',
        'description': 'Check server info: RAM, uptime, OS, Python version.',
        'parameters': {},
        'execute': _exec_system_info,
    },
    {
        'name': 'restart_bot',
        'description': 'Restart the bot. Useful after installing a new package or editing an important file.',
        'parameters': {},
        'execute': _exec_restart_bot,
    },
    {
        'name': 'install_package',
        'description': 'Install a new Python (pip) package in the bot.',
        'parameters': {
            'package_name': {
                'type': 'string',
                'description': 'The pip package name. Example: "requests", "pillow"',
                'required': True,
            },
        },
        'execute': _exec_install_package,
    },
]
