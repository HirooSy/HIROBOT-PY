import asyncio
import re
import sys

from baileys_lite import getBinaryNodeChild, getBinaryNodeChildren
from baileys_lite.foundation.node_runtime import JSObject
from lib.compat import http
from lib.utils.handler import G, PluginError
from lib.utils.helper import maybe_await, oc, Silent


async def handler(m, *, conn, text, participants, isAdmin, isOwner, usedPrefix, command):
    if not (isAdmin or isOwner):
        if callable(G.dfail):
            await maybe_await(G.dfail('admin', m, conn))
        raise Silent()
    if not text and m.quoted:
        text = m.quoted.sender.split('@')[0]
    elif not text:
        owner = oc(G.settings, 'owner', 0, 0) or ''
        raise PluginError(f"_Masukan nomor!_\nContoh:\n\n{usedPrefix + command} {owner}")
    existing = [p.get('id') for p in participants]
    candidates = []
    for v in text.split(','):
        digits = re.sub(r'[^0-9]', '', v)
        if 4 < len(digits) < 20 and digits + '@s.whatsapp.net' not in existing:
            candidates.append(digits)

    async def check(v):
        res = await conn.onWhatsApp(v + '@s.whatsapp.net')
        return v, bool(res and res[0] and res[0].get('exists'))
    checked = await asyncio.gather(*[check(v) for v in candidates])
    users = [v + '@c.us' for v, ok in checked if ok]
    if not users:
        raise PluginError('Nomor tidak tersedia di WhatsApp!')
    await m.reply('_Sedang di proses..._')
    response = await conn.query(JSObject({
        'tag': 'iq',
        'attrs': JSObject({'type': 'set', 'xmlns': 'w:g2', 'to': m.chat}),
        'content': [JSObject({'tag': 'add', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'participant', 'attrs': JSObject({'jid': jid})})]}) for jid in users],
    }))
    pp = None
    try:
        pp = await conn.profilePictureUrl(m.chat, 'image')
    except Exception:
        pp = None
    thumb = (await http.get(pp)).content if pp else b''
    participant = getBinaryNodeChildren(response, 'add')
    anu = [v for v in participant[0]['content'] if v]
    first = anu[0]['attrs']
    num = first['jid'].split('@')[0]
    if str(first.get('error')) == '408':
        msg = f'Tidak dapat menambahkan @{num}!\nKabarnya si @{num} baru keluar dari grup ini'
        try:
            await conn.sendButton(m.chat, {
                'text': msg,
                'footer': oc(G.settings, 'sticker_wm', 0) or '',
                'image': 'https://telegra.ph/file/d5996f3ec87fabe48fdcb.jpg',
                'nativeFlow': [{'text': 'Link', 'id': '.link'}],
            }, m, {'asLocation': True, 'mentions': conn.parseMention(msg)})
        except Exception as err:
            print('[add] sendButton gagal:', err, file=sys.stderr)
    for user in [i for i in participant[0]['content'] if str(i['attrs'].get('error')) == '403']:
        jid = user['attrs']['jid']
        content = getBinaryNodeChild(user, 'add_request')
        await conn.reply(m.chat, f"Mengundang @{jid.split('@')[0]} menggunakan invite...", m)
        await conn.sendGroupV4Invite(m.chat, jid, content['attrs']['code'], content['attrs']['expiration'],
                                     await maybe_await(conn.getName(m.chat)), 'Undangan untuk bergabung ke grup WhatsApp saya', thumb)
    if not first.get('error'):
        await m.reply('Berhasil menambahkan @' + num)


help = ['add @user']
tags = ['group']
command = re.compile(r'^(add)$', re.I)
admin = True
group = True
botAdmin = True
fail = None
