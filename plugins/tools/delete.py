import re
import sys

from lib.utils.handler import G
from lib.utils.helper import oc


async def handler(m, *, conn):
    if not m.isGroup:
        return await G.dfail('group', m, conn)
    key = {}
    try:
        fake = m.quoted.fakeObj if m.quoted else None
        key['remoteJid'] = oc(fake, 'key', 'remoteJid') if m.quoted else m.key['remoteJid']
        key['fromMe'] = oc(fake, 'key', 'fromMe') if m.quoted else m.key['fromMe']
        key['id'] = oc(fake, 'key', 'id') if m.quoted else m.key['id']
        key['participant'] = oc(fake, 'participant') if m.quoted else m.key.get('participant')
    except Exception as e:
        print(e, file=sys.stderr)
    await conn.sendMessage(m.chat, {'delete': key})


help = ['delete']
tags = ['tools']
command = re.compile(r'^(delete)$', re.I)
