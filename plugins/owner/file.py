import os
import re

from lib.utils import plugins as plugmod
from lib.utils.handler import G, PluginError


def plugin_names():
    return [re.sub(r'\.py$', '', re.sub(r'^plugins/', '', k)) for k in plugmod.plugins]


def list_grouped():
    grouped = {}
    for name in plugin_names():
        cat, pn = name.split('/', 1) if '/' in name else ('general', name)
        grouped.setdefault(cat, []).append(pn)
    return '\n\n'.join(f"*[ {cat.upper()} ]*\n" + '\n'.join('- ' + v for v in sorted(names)) for cat, names in sorted(grouped.items()))


async def handler(m, *, conn, usedPrefix, command, args, text):
    p, command, args, text = usedPrefix, command, args, text
    cmd = command.lower()
    if re.match(r'^(save|s)(m(edia)?|v?p(lugin)?|f(ile)?)$', cmd, re.I):
        if not text:
            raise PluginError(f'Penggunaan: {p}{command} <name file>\nContoh: {p}savefile main.py\n        {p}saveplugin owner\n        {p}savemedia img.png')
        if not m.quoted:
            raise PluginError('Balas/quote media/text yang ingin disimpan')
        if re.search(r'v?p(lugin)?', cmd, re.I):
            name = f'plugins/{text}.py'
            with open(name, 'w', encoding='utf-8') as fh:
                fh.write(m.quoted.text)
            return await m.reply(f'tersimpan di {name}')
        if re.search(r'f(ile)?', cmd, re.I):
            with open(text, 'w', encoding='utf-8') as fh:
                fh.write(m.quoted.text)
            return await m.reply(f'Saved {text} to file!')
        if re.search(r'm(edia)?', cmd, re.I) and m.quoted.mediaMessage:
            media = await m.quoted.download()
            with open(text, 'wb') as fh:
                fh.write(media)
            return await m.reply(f'Successfully saved media to *{text}*')
        return
    if cmd == 'dfp':
        if not text:
            raise PluginError(f'uhm.. where the text?\n\nexample:\n{p + command} info')
        if args[0] not in plugin_names():
            return await m.reply(f'*🗃️ NOT FOUND!*\n==================================\n\n{list_grouped()}')
        f = os.path.join(os.getcwd(), 'plugins', f'{args[0]}.py')
        if not os.path.exists(f):
            return await m.reply(f'File "{args[0]}" tidak ditemukan.')
        os.unlink(f)
        return await conn.reply(m.chat, f'Succes deleted "plugins/{args[0]}.py"', m)
    if cmd == 'df':
        if not args:
            raise PluginError('Nama File?')
        try:
            f = os.path.join(os.getcwd(), args[0])
            if not os.path.exists(f):
                return await m.reply(f'File "{args[0]}" tidak ditemukan.')
            os.unlink(f)
            return await conn.reply(m.chat, f'Succes deleted "{args[0]}"', m)
        except Exception as e:
            return await m.reply('Terjadi error: ' + str(e))
    if cmd in ('getplugin', 'gp'):
        await m.react('🔍')
        names = plugin_names()
        if not text:
            raise PluginError(f'uhm.. where the text?\n\nexample:\n{p + command} tools/sticker\n\n───────────────\n{list_grouped()}')
        if text not in names:
            return await m.reply(f'> *NOT FOUND!*\n───────────────\n{G.readmore}\n\n{list_grouped()}')
        try:
            with open(f'plugins/{text}.py', 'r', encoding='utf-8') as fh:
                content = fh.read()
            if content.strip():
                await m.reply(content)
        except Exception as e:
            await m.reply(str(e))


help = [f'save{v} <name file>' for v in ('plugin', 'file', 'media')] + ['dfp <plugin name>', 'df <file name>', 'getplugin <kategori/nama>']
tags = ['owner']
command = re.compile(r'^((save|s)(m(edia)?|v?p(lugin)?|f(ile)?)|dfp?|getplugin|gp)$', re.I)
rowner = True
