import asyncio
import os
import re

from lib.utils.handler import G


async def handler(m, *, conn):
    if os.environ.get('DATABASE'):
        sent = await m.reply('Saving Database...')
        await G.db.write()
        await asyncio.sleep(2)
        await conn.sendMessage(m.chat, {'text': 'Restarting...', 'edit': sent['key']})
    else:
        await m.reply('✦ Restarting...')
    await G.flush_and_exit(1, 'restart command')


help = ['restart']
tags = ['owner']
command = re.compile(r'^(restart)$', re.I)
rowner = True
