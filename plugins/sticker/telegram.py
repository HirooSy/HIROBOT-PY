import asyncio
import os
import re
import sys

from lib.compat import http
from lib.utils.handler import G, PluginError
from lib.utils.helper import oc


FALLBACK_TOKEN = '7935827856:AAGdbLXArulCigWyi6gqR07gi--ZPm7ewhc'
UA = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'


def bot_token():
    return os.environ.get('TELEGRAM_BOT_TOKEN') or FALLBACK_TOKEN


def tg_api():
    return f'https://api.telegram.org/bot{bot_token()}'


def tg_file():
    return f'https://api.telegram.org/file/bot{bot_token()}'


async def telesticker(url):
    try:
        mt = re.search(r'https://t\.me/addstickers/([^/?#]+)', url)
        if not mt:
            raise ValueError('Invalid url')
        a = (await http.get(f'{tg_api()}/getStickerSet', params={'name': mt.group(1)}, headers={'user-agent': UA})).json()
        if not a.get('ok'):
            raise RuntimeError('Sticker set not found')
        skipped = [0]

        async def one(sticker):
            try:
                b = (await http.get(f'{tg_api()}/getFile', params={'file_id': sticker['file_id']}, headers={'user-agent': UA})).json()
                path = ((b or {}).get('result') or {}).get('file_path')
                if not path or re.search(r'\.tgs$', path, re.I):
                    skipped[0] += 1
                    return None
                return {'emoji': sticker.get('emoji'), 'is_video': sticker.get('is_video') or bool(re.search(r'\.webm$', path, re.I)),
                        'is_animated': sticker.get('is_animated'), 'url': f'{tg_file()}/{path}'}
            except Exception:
                skipped[0] += 1
                return None
        stickers = [s for s in await asyncio.gather(*[one(s) for s in a['result']['stickers']]) if s]
        return {'name': a['result']['name'], 'title': a['result']['title'], 'link': f"t.me/addstickers/{mt.group(1)}",
                'sticker_type': a['result'].get('sticker_type'), 'stickers': stickers, 'skipped': skipped[0]}
    except Exception as error:
        print(error, file=sys.stderr)
        raise PluginError("Can't get the sticker data, try another link")


async def handler(m, *, conn, args):
    if not args or not re.search(r'https://t\.me/addstickers/', args[0], re.I):
        raise PluginError('Input Telesticker Url')
    result = await telesticker(args[0])
    stickers = result['stickers']
    if not stickers:
        return "Can't get sticker data, try another link"
    if len(stickers) > 300:
        return await m.reply(f'[ *{len(stickers)}* Stickers ] Request cancelled, too risk for bot.')
    if result['skipped'] > 0:
        await m.reply(f"⚠️ {result['skipped']} sticker(s) skipped (unsupported animated format).")
    per = 60
    total = -(-len(stickers) // per)
    for i in range(total):
        pack = stickers[i * per:(i + 1) * per]
        name = f"{result['title']} ({i + 1}/{total})" if total > 1 else result['title']
        server = oc(G.opts, 'server')
        server = 'undefined' if server is None else server
        await conn.sendStickerPack(m.chat, {
            'cover': {'url': pack[0]['url']},
            'stickers': [{'data': {'url': s['url']}} for s in pack],
            'name': name,
            'publisher': result['link'],
            'description': f"{G.settings['botname']} — {server}",
        }, m)
        if i < total - 1:
            await asyncio.sleep(1)


help = ['telesticker <url>']
tags = ['sticker']
command = re.compile(r'^(telestic?ker|stic?kertele|stele)$', re.I)
limit = 3
