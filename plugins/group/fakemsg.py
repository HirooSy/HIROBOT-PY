import asyncio
import re
import sys

from lib.utils.helper import sleep


async def handler(m, *, conn, text):
    if not m.quoted:
        return await m.reply('Reply to the message you want to process.')
    if not text:
        return await m.reply('Masukkan teks pengganti.')
    stanza_id = m.quoted.id
    try:
        temp_id = await conn.relayMessage(m.chat, {'extendedTextMessage': {'text': '', 'contextInfo': {'isGroupStatus': True}}}, {})
        temp_id2 = await conn.relayMessage(m.chat, {'protocolMessage': {
            'key': {'jid': m.chat, 'fromMe': True, 'id': temp_id}, 'type': 14,
            'editedMessage': {'extendedTextMessage': {'text': text, 'contextInfo': {'isGroupStatus': False}}},
        }}, {'messageId': stanza_id})
        await sleep(100)
        await asyncio.gather(
            conn.sendMessage(m.chat, {'delete': {'remoteJid': m.chat, 'id': temp_id, 'fromMe': True}}),
            conn.sendMessage(m.chat, {'delete': {'remoteJid': m.chat, 'id': temp_id2, 'fromMe': True}}),
            return_exceptions=True,
        )
    except Exception as e:
        print('[fakemsg]', e, file=sys.stderr)
        await m.reply('Error: ' + str(e))


help = ['fakemsg']
tags = ['group']
command = re.compile(r'^fakemsg$', re.I)
group = True
