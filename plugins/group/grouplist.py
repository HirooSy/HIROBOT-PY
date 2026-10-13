import re

from lib.utils import connection as Connection


async def handler(m, *, conn):
    txt = ''
    for jid, chat in list(Connection.store.chats.items()):
        if jid.endswith('@g.us') and chat.get('isChats'):
            state = 'Left' if ((chat.get('metadata') or {}).get('read_only')) else 'Joined'
            txt += f'{await conn.getName(jid)}\n🪪{jid} [{state}]\n\n'
    await m.reply(f'List Groups:\n{txt}\n'.strip())


help = ['groups', 'grouplist']
tags = ['info']
command = re.compile(r'^(group(s|list))$', re.I)
ai = {'risk': 'low', 'isTool': True, 'description': 'Group list info'}
