import re

from baileys_lite import areJidsSameUser
from lib.utils.handler import PluginError
from lib.utils.simple import _get


GROUP_RE = r'^[0-9]{5,16}-?[0-9]+@g\.us$'


async def handler(m, *, conn, args):
    group_jid = m.chat
    if args and re.match(GROUP_RE, args[0]):
        group_jid = args[0]
    if not re.match(GROUP_RE, group_jid):
        raise PluginError('Hanya bisa dibuka di grup')
    meta = await conn.groupMetadata(group_jid)
    if not meta:
        raise PluginError('groupMetadata is undefined :\\')
    if 'participants' not in meta:
        raise PluginError('participants is not defined :(')
    my_id = (_get(conn, 'user') or {}).get('id')
    me = next((u for u in meta['participants'] if areJidsSameUser(u['id'], my_id)), None)
    if not me:
        raise PluginError('Aku tidak ada di grup itu :(')
    if not me.get('admin'):
        raise PluginError('Aku bukan admin T_T')
    await m.reply('https://chat.whatsapp.com/' + await conn.groupInviteCode(group_jid))


help = ['linkgroup']
tags = ['group']
command = re.compile(r'^link(gro?up)?$', re.I)
