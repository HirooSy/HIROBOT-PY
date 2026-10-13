import re

from lib.utils.handler import PluginError


async def handler(m, *, conn, args, usedPrefix, command):
    mode = {'open': 'not_announcement', 'close': 'announcement'}.get(args[0] if args else '')
    if mode is None:
        p = usedPrefix + command
        raise PluginError(f'*Example :*\n- {p} close\n- {p} open')
    await conn.groupSettingUpdate(m.chat, mode)


help = ['group <open/close>']
tags = ['group']
command = re.compile(r'^(group)$', re.I)
admin = True
botAdmin = True
