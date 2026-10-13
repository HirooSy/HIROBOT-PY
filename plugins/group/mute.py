import asyncio
import re
import sys

from baileys_lite.foundation.node_runtime import JSObject
from lib.utils import connection as Connection
from lib.utils.handler import G
from lib.utils.helper import oc, sleep


def find_participant(group_metadata, tagged_jid):
    for p in (group_metadata or {}).get('participants') or []:
        if p.get('id') == tagged_jid or p.get('phoneNumber') == tagged_jid:
            return p
    return None


def is_muted(chat, jid):
    for u in chat.get('mutedMembers') or []:
        if u.get('id') == jid or u.get('phoneNumber') == jid:
            return True
    return False


def short(jid):
    return str(jid).split('@')[0]


async def handler(m, *, conn, command, groupMetadata):
    command, group_metadata = command, groupMetadata
    if not m.mentionedJid:
        return await m.reply(f'Tag someone you want to {command}. Example: /{command} @user')
    try:
        fresh_meta = await conn.groupMetadata(m.chat)
    except Exception:
        fresh_meta = group_metadata
    chats = G.db.data['chats']
    if chats.get(m.chat) is None:
        chats[m.chat] = {}
    chat = chats[m.chat]
    if chat.get('mutedMembers') is None:
        chat['mutedMembers'] = []
    results = []
    for tagged in m.mentionedJid:
        participant = find_participant(fresh_meta, tagged)
        if not participant:
            results.append(f'[FAIL] @{short(tagged)} not found in this group.')
            continue
        if participant.get('admin'):
            results.append(f"[WARNING] @{short(tagged)} admin, can't be muted.")
            continue
        if command == 'mute':
            if is_muted(chat, participant.get('id')):
                results.append(f'@{short(tagged)} already muted.')
                continue
            chat['mutedMembers'].append({'id': participant.get('id'), 'phoneNumber': participant.get('phoneNumber')})
            results.append(f'@{short(tagged)} muted.')
        else:
            before = len(chat['mutedMembers'])
            chat['mutedMembers'] = [u for u in chat['mutedMembers'] if not (u.get('id') == participant.get('id') or u.get('phoneNumber') == participant.get('phoneNumber'))]
            if len(chat['mutedMembers']) < before:
                results.append(f'@{short(tagged)} unmuted.')
            else:
                results.append(f'[INFO] @{short(tagged)} not muted yet.')
    await conn.sendMessage(m.chat, {'text': '\n'.join(results), 'mentions': m.mentionedJid}, {'quoted': m})


async def all(m, *, conn):
    conn = getattr(m, 'conn', None) or Connection.state.conn
    try:
        if not m.isGroup or m.fromMe or not m.sender:
            return
        if not m.id:
            return
        chat = ((G.db.data or {}).get('chats') or {}).get(m.chat)
        if not chat or not chat.get('mutedMembers'):
            return
        if not is_muted(chat, m.sender):
            return
        chat_id = m.chat
        stanza_id = oc(m, 'key', 'id')
        mute_text = 'muted'
        await sleep(500)
        temp_id = await conn.relayMessage(chat_id, JSObject({'extendedTextMessage': JSObject({'text': '', 'contextInfo': JSObject({'isGroupStatus': True})})}), JSObject({}))
        temp_id2 = await conn.relayMessage(chat_id, JSObject({'protocolMessage': JSObject({
            'key': JSObject({'jid': chat_id, 'fromMe': True, 'id': temp_id}),
            'type': 14,
            'editedMessage': JSObject({'extendedTextMessage': JSObject({'text': mute_text, 'contextInfo': JSObject({'isGroupStatus': False})})}),
        })}), JSObject({'messageId': stanza_id}))
        await sleep(100)
        await asyncio.gather(
            conn.sendMessage(chat_id, {'delete': {'remoteJid': chat_id, 'id': temp_id, 'fromMe': True}}),
            conn.sendMessage(chat_id, {'delete': {'remoteJid': chat_id, 'id': temp_id2, 'fromMe': True}}),
            return_exceptions=True,
        )
    except Exception as e:
        print('[Mute Delete Error]', e, file=sys.stderr)
        try:
            await conn.sendMessage(m.chat, {'text': '[MUTE ERR] ' + str(getattr(e, 'message', None) or e)})
        except Exception:
            pass


help = ['mute @user', 'unmute @user']
tags = ['group']
command = re.compile(r'^(mute|unmute)$', re.I)
group = True
admin = True
