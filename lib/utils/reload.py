import asyncio
import contextlib
import os
import sys
import traceback

from lib.utils import helper, plugins as plugmod
from lib.utils.color import chalk
from lib.utils.handler import G


LIB_DIR = os.path.join(helper.ROOT, 'lib')
EXCLUDED = {'__pycache__', '.git', 'data', '.cache', 'baileys'}
SKIP_LIB_FILES = {'start.py'}
POLL_SECONDS = 1.0
started = False


def _snapshot(base, exts=('.py',)):
    out = {}
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in EXCLUDED]
        for fn in fns:
            if fn.endswith(exts):
                p = os.path.join(dp, fn)
                with contextlib.suppress(OSError):
                    out[p] = os.stat(p).st_mtime_ns
    return out


def _log(kind, rel):
    label = chalk.cyanBright('Add') if kind == 'add' else chalk.green('Edit') if kind == 'edit' else chalk.red('Delete')
    print(f'{label} — {chalk.white(rel)}')


def _fresh_module(path):
    return helper.import_file(path)


async def _reimport_lib_file(path, rel):
    with open(path, 'rb') as fh:
        err = helper.check_syntax(fh.read(), path)
    if err:
        raise SyntaxError(f'{err.message} (line {err.line})')
    mod = _fresh_module(path)
    from lib.utils import connection as Connection
    conn = Connection.state.conn
    if rel == 'utils/simple.py' and conn is not None:
        new_cls = getattr(mod, 'Conn', None)
        if new_cls is not None:
            conn.__class__ = new_cls
    if rel == 'utils/plugins.py':
        mod.load_plugin_files(plugmod.plugin_folder, True)
    if conn is not None:
        await Connection.reload(conn, False)
    return mod


async def _watch():
    plug_snap = _snapshot(plugmod.plugin_folder)
    lib_snap = _snapshot(LIB_DIR)
    while True:
        await asyncio.sleep(POLL_SECONDS)
        new_plug = _snapshot(plugmod.plugin_folder)
        for p in sorted(set(plug_snap) | set(new_plug)):
            if plug_snap.get(p) != new_plug.get(p):
                try:
                    plugmod.reload_file(p)
                except Exception as e:
                    print(f"error while reloading '{plugmod.format_filename(p)}'", e, file=sys.stderr)
        plug_snap = new_plug
        new_lib = _snapshot(LIB_DIR)
        changed = sorted(p for p in set(lib_snap) | set(new_lib) if lib_snap.get(p) != new_lib.get(p))
        for p in changed:
            rel_lib = os.path.relpath(p, LIB_DIR).replace(os.sep, '/')
            if rel_lib in SKIP_LIB_FILES:
                continue
            rel = 'lib/' + rel_lib
            if not os.path.exists(p):
                _log('delete', rel)
                continue
            was_known = p in lib_snap
            try:
                await _reimport_lib_file(p, rel_lib)
            except Exception as e:
                print(chalk.red(f'Reload Failed — [{rel}]:'), e, file=sys.stderr)
                if not isinstance(e, SyntaxError):
                    traceback.print_exception(type(e), e, e.__traceback__)
                continue
            _log('edit' if was_known else 'add', rel)
        lib_snap = new_lib


async def start_reload_system(opts=None):
    global started
    if started:
        return
    started = True
    try:
        plugmod.load_plugin_files(plugmod.plugin_folder, True)
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
    asyncio.ensure_future(_watch())
