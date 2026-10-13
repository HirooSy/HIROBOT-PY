import os
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)
sys.pycache_prefix = os.path.join(ROOT, 'data', 'python', 'cache')

from lib.config import load_env_file

load_env_file(os.path.join(os.getcwd(), '.env'))

import lib.config
from lib.utils.color import chalk
from lib.utils.handler import G
from lib.utils import helper

MAIN_FILE = os.path.join(ROOT, 'lib', 'main.py')
RESTART_WINDOW_S = 5 * 60
MAX_RESTART_COUNT = 5
RESTART_DELAY_S = 3
MAX_DELAY_S = 60
WATCH_POLL_S = 0.2

state = {'proc': None, 'intentional': False, 'count': 0, 'last': time.time()}


def clear_console():
    if sys.stdout.isatty():
        sys.stdout.write('\x1b[1;1H\x1b[0J')
        sys.stdout.flush()


def child_env():
    env = dict(os.environ)
    env['PYTHONPYCACHEPREFIX'] = os.path.join(ROOT, 'data', 'python', 'cache')
    env['PYTHONPATH'] = ROOT + os.pathsep + os.path.join(ROOT, 'lib', 'package')
    return env


def spawn_main():
    args = [sys.executable, '-u', MAIN_FILE, '--cleartmp', *sys.argv[1:]]
    proc = subprocess.Popen(args, cwd=ROOT, env=child_env(), stdin=subprocess.PIPE)
    state['proc'] = proc
    return proc


def signal_name(num):
    try:
        return signal.Signals(num).name
    except ValueError:
        return str(num)


def exit_text(code):
    if code < 0:
        return f' with code: null (signal: {signal_name(-code)})'
    return f' with code: {code}'


def mtime_of(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def wait_for_change_or_timeout(seconds):
    initial = mtime_of(MAIN_FILE)
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(WATCH_POLL_S)
        current = mtime_of(MAIN_FILE)
        if current is not None and current != initial:
            return True
    return False


def stdin_reader():
    for raw in sys.stdin:
        line = raw.strip()
        proc = state['proc']
        if proc is None:
            continue
        if line == 'reset':
            state['intentional'] = True
            try:
                proc.terminate()
            except OSError:
                pass
            continue
        try:
            proc.stdin.write(raw.encode('utf-8'))
            proc.stdin.flush()
        except (OSError, ValueError):
            pass


def supervise():
    clear_console()
    print(chalk.yellow(chalk.bold(f"✦ {G.settings['botname']}")) + '\n─────────────────────────')
    if not helper.opts.get('test'):
        threading.Thread(target=stdin_reader, daemon=True).start()
    while True:
        proc = spawn_main()
        code = proc.wait()
        state['proc'] = None
        print(chalk.red('# [ Exited ]') + exit_text(code), file=sys.stderr)
        if state['intentional']:
            state['intentional'] = False
            continue
        if code == 1:
            print(chalk.cyan('# [ Exited ] Memory exit detected, restart immediately...'))
            continue
        if code == 0:
            return
        now = time.time()
        if now - state['last'] > RESTART_WINDOW_S:
            state['count'] = 0
        state['count'] += 1
        state['last'] = now
        if state['count'] > MAX_RESTART_COUNT:
            delay = min(MAX_DELAY_S, state['count'] * 5)
            print(chalk.yellow(f"# [ Restart ] Too many restarts ({state['count']}x). Wait {delay}s..."), file=sys.stderr)
            time.sleep(delay)
            continue
        print(chalk.yellow(f"# [ Restart ] Restart—{state['count']}... (Waiting for file changes or {RESTART_DELAY_S}s)"))
        wait_for_change_or_timeout(RESTART_DELAY_S)


if __name__ == '__main__':
    try:
        supervise()
    except KeyboardInterrupt:
        sys.exit(0)
