import asyncio
import importlib
import os
import re
import shutil
import sys
import textwrap
import traceback

from lib.utils import connection, helper
from lib.utils.handler import G
from lib.utils.helper import format_value, spawn


ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
PYLIBS_DIR = os.path.join(ROOT, 'data', 'python', 'modules')
PIP_CACHE_DIR = os.path.join(ROOT, 'data', 'python', 'cache', 'pip')
PIP_CMD_RE = re.compile(r'^\s*(?:(?:\S*python3?)\s+-m\s+pip|pip3?)\s+(?P<sub>[A-Za-z][A-Za-z-]*)\b(?P<rest>.*)$', re.S)
PIP_MODULE_SUBCOMMANDS = ('show', 'list', 'freeze', 'check')
PIP_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
PARAMS = ['print', 'm', 'handler', 'require', 'conn', 'db', 'store', 'connection', 'Array', 'process', 'args', 'groupMetadata', 'module', 'exports', 'argument', 'opts']
UNDEFINED = object()


class CustomArray(list):
    def __init__(self, *args):
        if args and isinstance(args[0], (int, float)) and not isinstance(args[0], bool):
            super().__init__([None] * min(int(args[0]), 10000))
        else:
            super().__init__(args)


def parse_pip(command_text):
    match = PIP_CMD_RE.match(command_text)
    if not match:
        return None
    return match.group('sub').lower(), match.group('rest')


def pip_shell(args_text):
    return f'"{sys.executable}" -m pip{args_text}'


def pylibs_env(extra=None):
    env = dict(os.environ)
    env['PIP_CACHE_DIR'] = PIP_CACHE_DIR
    existing = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = PYLIBS_DIR + (os.pathsep + existing if existing else '')
    if extra:
        env.update(extra)
    return env


def activate_pylibs():
    if PYLIBS_DIR not in sys.path:
        sys.path.insert(0, PYLIBS_DIR)
    importlib.invalidate_caches()


def normalize_name(name):
    return re.sub(r'[-_.]+', '_', name).lower()


def remove_from_pylibs(name):
    if not os.path.isdir(PYLIBS_DIR):
        return False
    target = normalize_name(name)
    found = False
    for entry in os.listdir(PYLIBS_DIR):
        full = os.path.join(PYLIBS_DIR, entry)
        if entry.endswith('.dist-info') or entry.endswith('.egg-info'):
            project = entry.rsplit('.', 1)[0].rsplit('-', 1)[0]
            if normalize_name(project) == target:
                shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
                found = True
        elif entry.endswith('.data') and os.path.isdir(full):
            project = entry[:-len('.data')].rsplit('-', 1)[0]
            if normalize_name(project) == target:
                shutil.rmtree(full)
                found = True
        else:
            module = entry[:-3] if entry.endswith('.py') else entry
            if normalize_name(module) == target:
                shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
                found = True
    return found


async def run_pip_shell(shell_command, env):
    proc = await asyncio.create_subprocess_shell(shell_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    out, err = await proc.communicate()
    return proc.returncode, out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')


async def pip_uninstall(rest):
    tokens = rest.split()
    if any(t in ('-r', '--requirement', '-e', '--editable') or t.startswith('--requirement=') for t in tokens):
        return None
    names = [t for t in tokens if not t.startswith('-')]
    if not names or not all(PIP_NAME_RE.match(n) for n in names):
        return None
    removed = [n for n in names if remove_from_pylibs(n)]
    leftover = [n for n in names if n not in removed]
    out, err = '', ''
    if removed:
        out += 'removed from data/python/modules: ' + ', '.join(removed) + '\n'
    if leftover:
        _, o, e = await run_pip_shell(pip_shell(' uninstall -y ' + ' '.join(leftover)), pylibs_env())
        out += o
        err += e
    return out, err


async def run_shell(command_text):
    pip = parse_pip(command_text)
    if pip and pip[0] == 'uninstall':
        result = await pip_uninstall(pip[1])
        if result is not None:
            out, err = result
            return out, err
    shell_command, env, activate = command_text, pylibs_env(), False
    if pip and pip[0] == 'install' and not any(t in ('--target', '-t', '--user') or t.startswith('--target=') for t in pip[1].split()):
        shell_command = pip_shell(f' install --upgrade --target "{PYLIBS_DIR}"{pip[1]}')
        activate = True
    elif pip and pip[0] in PIP_MODULE_SUBCOMMANDS:
        shell_command = pip_shell(f' {pip[0]}{pip[1]}')
    returncode, out, err = await run_pip_shell(shell_command, env)
    if activate and returncode == 0:
        activate_pylibs()
    return out, err


async def run_code(source, env, expression):
    body = ('return ' if expression else '') + source
    wrapped = f'async def __jsexec__({", ".join(PARAMS)}):\n' + textwrap.indent(body, '    ') + '\n    return _UNDEFINED\n'
    code = compile(wrapped, 'Execution Function', 'exec')
    exec(code, env)
    fn = env.pop('__jsexec__')
    return await fn(*(env[name] for name in PARAMS))


def render(value):
    if value is UNDEFINED:
        return 'undefined'
    if isinstance(value, BaseException):
        return ''.join(traceback.format_exception(type(value), value, value.__traceback__))
    if value is None:
        return 'null'
    return format_value(value)


async def handler(m, *, conn, match, usedPrefix, noPrefix, _args, args, command, text, participants, groupMetadata, user, bot, isROwner, isOwner, isRAdmin, isAdmin, isBotAdmin, isPrems, chatUpdate, __dirname, __filename):
    if m.sender == conn.user['jid'] and (m.id or '').startswith('3EB0'):
        return
    ctx = {
        'match': match, 'usedPrefix': usedPrefix, 'noPrefix': noPrefix, '_args': _args, 'args': args,
        'command': command, 'text': text, 'conn': conn, 'participants': participants,
        'groupMetadata': groupMetadata, 'user': user, 'bot': bot, 'isROwner': isROwner, 'isOwner': isOwner,
        'isRAdmin': isRAdmin, 'isAdmin': isAdmin, 'isBotAdmin': isBotAdmin, 'isPrems': isPrems,
        'chatUpdate': chatUpdate, '__dirname': __dirname, '__filename': __filename,
    }
    used_prefix, no_prefix = usedPrefix or '', noPrefix or ''
    command, text = command or '', text or ''
    if re.match(r'^[$] ', used_prefix + no_prefix) or (command and re.match(r'^[$]$', command)):
        try:
            out, err = await run_shell(command.lstrip() + ' ' + text.rstrip())
        except Exception:
            out, err = '', ''
        if out.strip():
            await conn.sendMessage(m.chat, {'text': out}, {'quoted': m})
        if err.strip():
            await conn.sendMessage(m.chat, {'text': err}, {'quoted': m})
        return

    expression = used_prefix.startswith('<<')
    source = no_prefix
    old = m.exp
    counter = {'i': 15}

    async def printer(*args):
        counter['i'] -= 1
        if counter['i'] < 1:
            return
        print(*args)
        return await conn.reply(m.chat, ' '.join(format_value(a) for a in args), m)

    def pr(*args):
        spawn(printer(*args))
    module = {'exports': {}}
    env = {
        'print': pr, 'm': m, 'handler': handler, 'require': importlib.import_module, 'conn': conn, 'db': G.db,
        'store': getattr(connection, 'store', None), 'connection': connection, 'Array': CustomArray, 'process': sys,
        'args': args, 'groupMetadata': groupMetadata, 'module': module, 'exports': module['exports'],
        'argument': [conn, ctx], 'ctx': ctx, 'opts': G.opts if G.opts is not None else helper.runtime_opts, 'G': G, 'asyncio': asyncio, '_UNDEFINED': UNDEFINED, '__builtins__': __builtins__,
    }
    result, syntax = None, ''
    try:
        result = await run_code(source, env, expression)
    except SyntaxError as e:
        syntax = f'```{e}```\n\n'
        result = e
    except Exception as e:
        result = e
    finally:
        await conn.reply(m.chat, syntax + render(result), m)
        m.exp = old


help = ['< ', '<<', '$ ']
tags = ['advanced']
customPrefix = re.compile(r'^(<?< |[$] )')
command = re.compile(r'(?:)', re.I)
rowner = True
ai = {'risk': 'low', 'description': 'run javascript code or shell command (owner only)'}