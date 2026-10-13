import importlib
import importlib.util
import os
import site
import subprocess
import sys
import threading

from baileys.foundation.node_runtime import JSError

PACKAGE = 'wasmtime'
BOT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..'))
PYLIBS_DIR = os.environ.get('BAILEYS_PYLIBS_DIR') or os.path.join(BOT_ROOT, 'data', 'python', 'modules')
PIP_CACHE_DIR = os.environ.get('BAILEYS_PIP_CACHE_DIR') or os.path.join(BOT_ROOT, 'data', 'python', 'cache', 'pip')
PIP_FLAGS = ['--quiet', '--disable-pip-version-check', '--upgrade']
PIP_TIMEOUT_SECONDS = 600
INSTALL_VARIANTS = [
    ('target', ['--target', PYLIBS_DIR]),
    ('target+break', ['--target', PYLIBS_DIR, '--break-system-packages']),
    ('user', ['--user']),
    ('user+break', ['--user', '--break-system-packages']),
]
_lock = threading.Lock()


def activatePath(path):
    if path not in sys.path:
        sys.path.insert(0, path)
    site.addsitedir(path)
    importlib.invalidate_caches()


def importWasmtime():
    try:
        return importlib.import_module(PACKAGE)
    except ImportError:
        return None


def runPip(args):
    cmd = [sys.executable, '-m', 'pip'] + args
    env = dict(os.environ)
    env['PIP_CACHE_DIR'] = PIP_CACHE_DIR
    return subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_SECONDS, env=env)


def ensurePip(errors):
    if importlib.util.find_spec('pip') is not None:
        return
    result = subprocess.run([sys.executable, '-m', 'ensurepip', '--upgrade'], capture_output=True, text=True, timeout=PIP_TIMEOUT_SECONDS)
    if result.returncode != 0:
        errors.append((result.stderr or result.stdout).strip()[-300:])


def ensureWasmtime():
    with _lock:
        if os.path.isdir(PYLIBS_DIR):
            activatePath(PYLIBS_DIR)
        module = importWasmtime()
        if module is not None:
            return module
        errors = []
        ensurePip(errors)
        for name, flags in INSTALL_VARIANTS:
            try:
                result = runPip(['install'] + PIP_FLAGS + flags + [PACKAGE])
            except (OSError, subprocess.SubprocessError) as err:
                errors.append(f'{name}: {err}')
                continue
            if result.returncode != 0:
                errors.append(f'{name}: ' + (result.stderr or result.stdout).strip()[-300:])
                continue
            if name.startswith('target'):
                activatePath(PYLIBS_DIR)
            else:
                activatePath(site.getusersitepackages())
            module = importWasmtime()
            if module is not None:
                return module
            errors.append(f'{name}: pip succeeded but {PACKAGE} is still not importable')
        raise JSError('could not install the wasmtime module automatically: ' + ' | '.join(errors[-3:]))
