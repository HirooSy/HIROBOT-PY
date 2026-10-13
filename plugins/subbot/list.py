import importlib
import re

from lib.utils import connection as Connection, database as dbmod
from lib.utils.handler import G
from lib.utils.simple import _get


async def handler(m, *, conn):
    sub = importlib.import_module('plugins.subbot.connect')
    saved = sub.list_saved_session_jids()
    if not saved and len(Connection.conns) == 0:
        return await conn.reply(m.chat, '📭 No active or saved sessions found.', m)
    users = (G.db.data or {}).get('users') or {}
    lines = []
    for jid in saved:
        name = (users.get(jid) or {}).get('name') or jid.split('@')[0]
        lines.append(f"{'🟢' if jid in Connection.conns else '🔴'} {name}")
    for jid, sub_conn in list(Connection.conns.items()):
        if jid in saved:
            continue
        name = (users.get(jid) or {}).get('name') or (_get(sub_conn, 'user') or {}).get('name') or jid.split('@')[0]
        lines.append(f"🟢 {name} ({jid.split('@')[0]})")
    max_slots = sub.get_config()['max']
    await m.reply(f"*Session List ({len(Connection.conns)}/{max_slots} active)*\n\n" + '\n'.join(lines))


help = ['listsubbot']
tags = ['subbot']
command = re.compile(r'^(list(sub)?bot)$', re.I)
ai = {'risk': 'low', 'description': 'show active sub-bots'}
