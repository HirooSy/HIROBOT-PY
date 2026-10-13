import re

from lib.utils import helper
from lib.utils.handler import G
from lib.utils.helper import Silent


OPTIONS = ['welcome', 'silent', 'antidelete', 'antilink', 'public', 'detect', 'restrict', 'autoread', 'pconly', 'gconly', 'aiChat']


async def handler(m, *, conn, args, command, isOwner, isAdmin, isROwner, usedPrefix):
    is_owner, is_admin, is_rowner = isOwner, isAdmin, isROwner
    is_enable = bool(re.search(r'true|enable|(turn)?on|1', command, re.I))
    chat = G.db.data['chats'][m.chat]
    bot = G.db.data['settings'].get(conn.user['jid']) or {}
    kind = (args[0] if args else '').lower()
    opts = helper.runtime_opts

    async def need(flag, key):
        if not flag:
            await G.dfail(key, m, conn)
            raise Silent()

    group_toggle = {'welcome': 'welcome', 'detect': 'detect'}
    admin_toggle = {'antidelete': 'delete', 'antinsfw': 'antinsfw', 'aichat': 'aiChat', 'antilink': 'antiLink'}
    opt_toggle = {'silent': 'nyimak', 'nyimak': 'nyimak', 'mewing': 'nyimak', 'autoread': 'autoread', 'pconly': 'pconly',
                  'privateonly': 'pconly', 'gconly': 'gconly', 'grouponly': 'gconly', 'swonly': 'swonly', 'statusonly': 'swonly'}
    if kind in group_toggle:
        if not m.isGroup:
            await need(is_owner, 'group')
        else:
            await need(is_admin, 'admin')
        chat[group_toggle[kind]] = is_enable
    elif kind in admin_toggle:
        if m.isGroup:
            await need(is_admin or is_owner, 'admin')
        chat[admin_toggle[kind]] = is_enable
    elif kind == 'document':
        chat['useDocument'] = is_enable
    elif kind == 'public':
        await need(is_rowner, 'rowner')
        opts['self'] = not is_enable
    elif kind == 'restrict':
        await need(is_owner, 'owner')
        bot['restrict'] = is_enable
    elif kind in opt_toggle:
        await need(is_rowner, 'rowner')
        opts[opt_toggle[kind]] = is_enable
    else:
        if not re.search(r'[01]', command):
            p = usedPrefix + command
            return await conn.sendButton(m.chat, {
                'text': '', 'ltoText': 'Enable' if is_enable else 'Disable', 'ltoUrl': '\u0000',
                'optionText': 'Select Options', 'optionTitle': 'Select Options',
                'nativeFlow': [{'text': o, 'id': f'{p} {o}'} for o in OPTIONS],
            }, m)
        raise Silent()
    await m.react('🟢' if is_enable else '🔴')


help = ['enable <option>', 'disable <option>']
tags = ['group', 'owner']
command = re.compile(r'^((en|dis)able|(tru|fals)e|(turn)?o(n|ff)|[01])$', re.I)
ai = {'risk': 'low', 'summarize': True, 'description': 'Enable/disable option'}
