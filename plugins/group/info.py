import re

from lib.utils.handler import G


async def handler(m, *, conn, participants, groupMetadata):
    participants, meta = participants, groupMetadata
    pp = None
    try:
        pp = await conn.profilePictureUrl(m.chat, 'image')
    except Exception:
        pass
    pp = pp or 'https://files.catbox.moe/ifx2y7.png'
    c = G.db.data['chats'][m.chat]
    admins = [p for p in participants if p.get('admin')]
    listing = '\n'.join(f"{i + 1}. @{v['id'].split('@')[0]}" for i, v in enumerate(admins))
    owner = meta.get('owner') or next((p['id'] for p in admins if p['admin'] == 'superadmin'), None) or m.chat.split('-')[0] + '@s.whatsapp.net'

    def mark(v):
        return '✅' if v else '❌'

    def js(v):
        return 'undefined' if v is None else str(v)
    text = (
        f"*「 Group Information 」*\n\n*ID:*\n{js(meta.get('id'))}\n*Name:*\n{js(meta.get('subject'))}\n*Description:*\n{str(meta.get('desc') or 'unknown')}\n"
        f"*Total Members:*\n{len(participants)} Members\n*Group Owner:*\n@{owner.split('@')[0]}\n*Group Admins:*\n{listing}\n"
        f"*Group Settings:*\n{mark(c.get('isBanned'))} Banned\n{mark(c.get('welcome'))} Welcome\n{mark(c.get('detect'))} Detect\n"
        f"{mark(not c.get('delete'))} Anti Delete\n{mark(c.get('antiLink'))} Anti Link\n*Message Settings:*\n"
        f"Welcome: {js(c.get('sWelcome'))}\nBye: {js(c.get('sBye'))}\nPromote: {js(c.get('sPromote'))}\nDemote: {js(c.get('sDemote'))}"
    )
    await conn.sendFile(m.chat, pp, 'pp.jpg', text, m, False, {'mentions': [v['id'] for v in admins] + [owner]})


help = ['infogrup']
tags = ['group']
command = re.compile(r'^(gro?upinfo|info(gro?up|gc))$', re.I)
group = True
