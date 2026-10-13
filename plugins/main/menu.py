import platform
import re
import sys

from lib.utils import plugins as plugmod
from lib.utils.handler import G


ARRAY_TAGS = ['all', 'main', 'group', 'sticker', 'ai', 'internet', 'adult', 'subbot', 'tools', 'downloader', 'owner', 'info']
DEFAULT_MENU = {'before': '', 'header': '*ᗢ %category*', 'body': '- %cmd %isPremium %islimit', 'footer': '', 'after': ''}
TAG_MAP = {
    'all': {'main': 'Main', 'group': 'Grup', 'internet': 'Internet', 'adult': 'Adult', 'downloader': 'Downloader', 'database': 'Database',
            'sticker': 'Stiker', 'tools': 'Tools', 'subbot': 'Sub-bot', 'ai': 'Artificial Intelligence', 'owner': 'Owner', 'info': 'Info'},
    'main': {'main': 'Main'},
    'ai': {'ai': 'Artificial Intelligence'},
    'sticker': {'sticker': 'Stiker'},
    'group': {'group': 'Group'},
    'adult': {'adult': 'Adult'},
    'subbot': {'subbot': 'Sub-bot'},
    'tools': {'tools': 'Tools', 'ai': 'Artificial Intelligence', 'database': 'Database'},
    'downloader': {'downloader': 'Downloader'},
    'internet': {'internet': 'Internet'},
    'info': {'info': 'Info'},
    'owner': {'owner': 'Owner', 'advanced': 'Advanced'},
}


def js_str(v):
    if v is None:
        return 'undefined'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def visible():
    return [p for p in plugmod.plugins.values() if not p.disabled]


def command_names(tag=None):
    out = []
    for p in visible():
        if (tag is None or tag in as_list(p.tags)) and p.help:
            for h in as_list(p.help):
                name = str(h).split(' ')[0]
                if name not in out:
                    out.append(name)
    return out


async def handler(m, *, conn, usedPrefix, args, isPrems, isOwner):
    _p, args = usedPrefix, args
    is_prems, is_owner = isPrems, isOwner
    teks = (args[0] if args else 'undefined').lower()
    if teks not in ARRAY_TAGS:
        teks = '404'
    tags = TAG_MAP.get(teks)
    if teks == 'group':
        tags = {'group': 'Group', 'admin': f"Admin {'' if (G.opts or {}).get('restrict') else '(Disabled)'}"}
    try:
        opts = G.opts or {}
        mode = 'Private' if opts.get('self') else 'Publik'
        pkg = G.package or {}
        users = G.db.data['users']
        user = users[m.sender]
        name = user.get('name')
        prems = 'Premium' if (user.get('premiumTime') or 0) > 0 else 'Free'
        dev = next((o[0] for o in G.settings['owner'] if o[0] and len(o) > 2 and o[2]), None)
        number = m.sender.split('@')[0]
        quoted = {'key': {'remoteJid': '0@s.whatsapp.net'}, 'message': {'contactMessage': {
            'displayName': js_str(name),
            'vcard': f"BEGIN:VCARD\nVERSION:3.0\nN:;a,;;;\nFN:{js_str(name)}\nitem1.TEL;waid={number}:{number}\nitem1.X-ABLabel:Ponsel\nEND:VCARD"}}}
        status = 'Developer' if number == dev else 'Owner' if is_owner else 'Premium User' if is_prems else 'User'
        before_text = (f"──────────────────\n\n*U S E R*\n- `Name:` {js_str(name)}\n- `Status:` {status}\n"
                       f"- `Registered:` {'Yes' if user.get('registered') else 'No'}\n\n──────────────────\n\n"
                       f"*I N F O R M A T I O N*\n- `Bot Name:` {js_str(G.settings['botname'])}\n- `Developer:` @{js_str(dev)}\n"
                       f"- `Version:` {js_str(pkg.get('version'))}\n\n──────────────────")
        if teks == '404':
            rows = []
            for v in [t for t in ARRAY_TAGS if t != 'all']:
                cmds = command_names(v)
                shown = ', '.join(cmds[:5])
                desc = '-' if not cmds else shown if len(cmds) <= 5 else f'{shown}, ...{len(cmds) - 5}+'
                rows.append({'header': '\u0000', 'title': '• ' + v[:1].upper() + v[1:], 'description': desc, 'id': '.menu ' + v})
            total = len(command_names())
            bot_pp = G.img['profile']['bot']
            thumb = await conn.resize(bot_pp, 100, 100)
            await conn.sendButton(m.chat, {
                'document': {'url': bot_pp},
                'jpegThumbnail': thumb,
                'mimetype': 'image/webp',
                'caption': before_text,
                'fileName': js_str(G.settings['botname']),
                'fileLength': '665666646645000',
                'nativeFlow': [{}, {'text': '📢', 'url': 'https://whatsapp.com/channel/0029VaVJo460bIdoxTVoJY3e'},
                               {'text': '🌐', 'url': G.getServerUrl(), 'useWebview': False},
                               {'text': '\u0000', 'sections': [{'highlight_label': '✦ SHOW ALL MENU', 'rows': [
                                   {'header': '\u0000', 'title': '• All', 'description': f'{total} commands', 'id': '.menu all'}, *rows]}]}],
                'mentions': [m.sender, f'{js_str(dev)}@s.whatsapp.net'],
            }, quoted)
            return 0

        total_reg = len(users)
        rtotal_reg = sum(1 for u in users.values() if u.get('registered') is True)
        helps = [{'help': as_list(p.help), 'tags': as_list(p.tags), 'prefix': p.customPrefix is not None,
                  'limit': p.limit, 'premium': p.premium} for p in visible()]
        if conn.get('menu') is None:
            conn['menu'] = {}
        cfg = conn['menu']
        before, header, body, footer, after = (
            (cfg.get(k) if isinstance(cfg, dict) else None) or DEFAULT_MENU[k]
            for k in ('before', 'header', 'body', 'footer', 'after'))
        blocks = []
        for tag, label in tags.items():
            lines = []
            for menu in helps:
                if tag in menu['tags'] and menu['help']:
                    for h in menu['help']:
                        lines.append(body.replace('%cmd', h if menu['prefix'] else _p + h).replace('%islimit', 'Ⓛ' if menu['limit'] else '')
                                     .replace('%isPremium', 'Ⓟ' if menu['premium'] else '').strip())
            blocks.append(header.replace('%category', label) + '\n' + '\n'.join([*lines, footer]))
        if isinstance(cfg, str):
            text = cfg
        elif isinstance(cfg, dict):
            text = '\n'.join([before, *blocks, after])
        else:
            text = ''
        sender_name = conn.getName(conn.user['jid'])
        if hasattr(sender_name, '__await__'):
            sender_name = await sender_name
        homepage = pkg.get('homepage')
        github = (homepage.get('url') if isinstance(homepage, dict) else None) or homepage or '[unknown github url]'
        rep = {'%': '%', 'me': sender_name, 'npmname': pkg.get('name'), 'npmdesc': pkg.get('description'), 'version': pkg.get('version'),
               'github': github, 'platform': platform.system().lower(), 'mode': mode, '_p': _p, 'money': user.get('money'),
               'age': user.get('age'), 'name': name, 'prems': prems, 'limit': user.get('limit'),
               'totalreg': total_reg, 'rtotalreg': rtotal_reg}
        pattern = re.compile('%(' + '|'.join(re.escape(k) for k in sorted(rep, key=len, reverse=True)) + ')')
        text = pattern.sub(lambda mt: js_str(rep[mt.group(1)]), text)
        order = {'orderId': '780642630945098', 'thumbnail': await conn.resize(G.img['profile']['sender'], 150, 150), 'itemCount': 666, 'status': 1,
                 'surface': 1, 'message': '• LIST COMMANDS', 'orderTitle': 'Channel.', 'sellerJid': '6283143393763@s.whatsapp.net',
                 'token': 'AR6pyJ/fz5vRFxggGxURL7EA/vCtjKrhcJSNhHqX1iJh8A==', 'totalAmount1000': '0', 'totalCurrencyCode': 'IDR'}
        await conn.reply(m.chat, text, {'key': {'remoteJid': '0@s.whatsapp.net'}, 'message': {'orderMessage': order}})
    except Exception as e:
        m.error = e
        print(e, file=sys.stderr)
        await conn.reply(m.chat, '> *Sorry*, the menu is currently error', m)


help = ['menu', 'help']
tags = ['main']
command = re.compile(r'^(menu|help|\?)$', re.I)
ai = {'risk': 'low', 'description': 'Show menu list / command list'}
