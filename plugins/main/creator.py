import re

from lib.utils.handler import G


async def handler(m, *, conn):
    owner = (G.settings['owner'][0] if G.settings['owner'] else [''])[0]
    await conn.sendContact(m.chat, [[owner, 'OWNER']], m)


help = ['owner', 'creator']
tags = ['info']
command = re.compile(r'^(owner|creator)$', re.I)
ai = {'risk': 'low', 'description': 'Send owner contact'}
