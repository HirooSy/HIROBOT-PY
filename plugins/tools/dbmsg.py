import json
import re

from baileys_lite import proto
from lib.utils.handler import G, PluginError


async def handler(m, *, conn, command, text, usedPrefix):
    command, text, p = command, text, usedPrefix
    msgs = G.db.data['msgs']
    kinds = '(msg|vn|video|audio|img|sticker|gif)'
    key = {'remoteJid': m.key['remoteJid'], 'id': m.key['id'], 'fromMe': False, 'participant': m.key.get('participant')}
    if re.search('add' + kinds, command, re.I):
        if not m.quoted:
            return
        if not text:
            raise PluginError(f'Usage: {p + command} <teks>\nExample:\n{p + command} tes')
        if text in msgs:
            raise PluginError(f"'{text}' telah terdaftar!")
        quoted = m.getQuotedObj()
        msgs[text] = json.loads(json.dumps(proto.WebMessageInfo.fromObject(quoted).toJSON(), default=_default))
        await conn.react(m.chat, '✅', key)
    if re.search('del' + kinds, command, re.I):
        if not text:
            raise PluginError(f'Use *{p}listmsg* to see the list')
        if text not in msgs:
            raise PluginError(f'Data `{text}` not found.')
        del msgs[text]
        await conn.react(m.chat, '🗑️', key)
    if re.search('get' + kinds, command, re.I):
        which = re.sub('get', '', command, flags=re.I)
        if not text:
            raise PluginError(f'use *{p}list{which}* to see the data list')
        if text not in msgs:
            raise PluginError(f"'{text}' not listed in the message list")
        revived = json.loads(json.dumps(msgs[text]), object_hook=_revive)
        _m = conn.serializeM(revived)
        await _m.copyNForward(m.chat, True)
    if re.search('list' + kinds, command, re.I):
        listing = '\n'.join('- ' + name for name in msgs)
        await conn.reply(m.chat, f'*List Message*\n{listing}\n─────────────────────\n> Get :  ```.getmsg [text]``` ', m)


def _default(o):
    if isinstance(o, (bytes, bytearray)):
        return {'type': 'Buffer', 'data': list(o)}
    if hasattr(o, 'toNumber'):
        return o.toNumber()
    return str(o)


def _revive(d):
    if d.get('type') == 'Buffer' and isinstance(d.get('data'), list):
        return bytes(d['data'])
    return d


help = ['addmsg <teks>', 'delmsg <teks>', 'listmsg <teks>', 'getmsg <teks>']
tags = ['database']
command = re.compile(r'^(get|add|del|list)(vn|msg|video|audio|img|stic?ker|gif)$')
