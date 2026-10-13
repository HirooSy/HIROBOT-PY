import asyncio
import importlib
import re
import sys
import textwrap
import traceback

from lib.utils import connection, helper
from lib.utils.handler import G
from lib.utils.helper import format_value, spawn


PARAMS = ['print', 'm', 'handler', 'require', 'conn', 'db', 'store', 'connection', 'Array', 'process', 'args', 'groupMetadata', 'module', 'exports', 'argument', 'opts']
UNDEFINED = object()


class CustomArray(list):
    def __init__(self, *args):
        if args and isinstance(args[0], (int, float)) and not isinstance(args[0], bool):
            super().__init__([None] * min(int(args[0]), 10000))
        else:
            super().__init__(args)


async def run_shell(command_text):
    proc = await asyncio.create_subprocess_shell(command_text, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')


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