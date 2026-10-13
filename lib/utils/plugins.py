import os
import sys
import traceback

from lib.utils import helper
from lib.utils.color import chalk


ROOT = helper.ROOT
plugin_folder = os.path.join(ROOT, 'plugins')
plugins = {}
plugin_folders = []

_PUNCT_ORDER = '_-,;:!?.\'"()[]{}@*/\\&#%`^+<=>|~$'


def plugin_filter(filename):
    base = os.path.basename(filename)
    return base.endswith('.py') and not base.startswith('_') and base != '__init__.py'


def format_filename(path):
    rel = os.path.relpath(path, ROOT)
    return rel.replace(os.sep, '/')


def _collate_key(text):
    primary = []
    tertiary = []
    for ch in text:
        low = ch.lower()
        if ch in _PUNCT_ORDER:
            primary.append(_PUNCT_ORDER.index(ch))
        elif ch.isdigit():
            primary.append(100 + int(ch) if ch.isdecimal() and ch.isascii() else 200 + ord(low))
        elif ch.isalpha() and ch.isascii():
            primary.append(300 + ord(low))
        else:
            primary.append(1000 + ord(low))
        tertiary.append(1 if ch != low else 0)
    return (primary, tertiary)


def sorted_plugins(d):
    return dict(sorted(d.items(), key=lambda kv: _collate_key(kv[0])))


def load_one(path):
    module = helper.import_file(path)
    return helper.Plugin(module, format_filename(path))


def load_plugin_files(folder=None, recursive=True):
    global plugins
    folder = os.path.abspath(folder or plugin_folder)
    if folder in plugin_folders:
        return plugins
    plugin_folders.append(folder)
    for entry in sorted(os.listdir(folder)):
        full = os.path.join(folder, entry)
        name = format_filename(full)
        try:
            if os.path.isdir(full):
                if recursive:
                    load_plugin_files(full, recursive)
                continue
            if not plugin_filter(entry):
                continue
            plugins[name] = load_one(full)
        except Exception as e:
            print(f'error while requiring {name}', file=sys.stderr)
            traceback.print_exception(type(e), e, e.__traceback__)
            plugins.pop(name, None)
    plugins = sorted_plugins(plugins)
    return plugins


def delete_plugin_folder(folder):
    resolved = os.path.abspath(folder)
    if resolved in plugin_folders:
        plugin_folders.remove(resolved)


def reload_file(path):
    global plugins
    filename = os.path.basename(path)
    if not plugin_filter(filename):
        return
    file = os.path.abspath(path)
    name = format_filename(file)
    exists = os.path.exists(file)
    if name in plugins:
        if exists:
            print(chalk.green('Edit') + f' — {chalk.white(name)}')
        else:
            print(chalk.red('Delete') + f' — {chalk.white(name)}')
            plugins.pop(name, None)
            return
    else:
        if not exists:
            return
        print(chalk.cyanBright('Add') + f' — {chalk.white(name)}')
    with open(file, 'rb') as fh:
        err = helper.check_syntax(fh.read(), filename)
    if err:
        print(f"syntax error while loading '{name}':", err.message, f'(line {err.line})', file=sys.stderr)
        return
    try:
        plugins[name] = load_one(file)
    except Exception as e:
        print(f"error require plugin '{name}':", file=sys.stderr)
        traceback.print_exception(type(e), e, e.__traceback__)
        plugins.pop(name, None)
    finally:
        plugins = sorted_plugins(plugins)
