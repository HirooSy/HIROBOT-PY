import asyncio
import re
import sys

from lib.compat import http
from lib.utils.handler import G, PluginError
from lib.utils.helper import maybe_await, mimetype_of
from lib.utils.simple import _get


URL_RE = re.compile(r'https?://(www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_+.~#?&/=]*)(jpe?g|gif|png)', re.I)


def is_url(text):
    return bool(URL_RE.search(text))


def field(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def keep_media(v):
    buf = v.get('buffer')
    if not isinstance(buf, (bytes, bytearray)) or not len(buf):
        return False
    if v.get('type') == 'video':
        vm = (((v.get('message') or {}).get('message') or {}).get('videoMessage')) or {}
        if (vm.get('seconds') or 0) > 10:
            return False
    return True


async def handler(m, *, conn, args):
    upload = G.scraper.upload.upload
    webp2png = G.scraper.ezgif.webp2png
    user = G.db.data['users'][m.sender]
    wm = G.settings['sticker_wm']
    custom_name = custom_author = None
    if args and '|' in args[0]:
        parts = ' '.join(args).split('|')
        custom_name = parts[0].strip() or None
        custom_author = (parts[1].strip() if len(parts) > 1 else '') or None
    elif args and not is_url(args[0]) and not args[0].startswith('http'):
        custom_name = ' '.join(args).strip()
    level = user.get('level') or 0
    scap = {
        'name': custom_name if custom_name is not None else ('' if level == 1 else (user.get('sname') if level >= 2 else wm[0])),
        'author': custom_author if custom_author is not None else ('' if level == 1 else (user.get('sauth') if level >= 2 else wm[1])),
    }
    sent = False
    try:
        q = m.quoted if m.quoted else m
        mime = mimetype_of(q) or _get(q, 'mediaType') or ''
        url_arg = next((a for a in args if is_url(a)), None)
        if _get(q, 'mtype') == 'albumMessage':
            medias = [v for v in await q.downloadAlbum() if keep_media(v)]
            if not medias:
                sent = True
                return await m.reply('> Album is empty or media has expired')
            per = 60
            total = -(-len(medias) // per)
            for i in range(total):
                chunk = medias[i * per:(i + 1) * per]
                base = scap['name'] or 'Sticker Pack'
                await conn.sendStickerPack(m.chat, {
                    'cover': chunk[0]['buffer'],
                    'stickers': [{'data': v['buffer']} for v in chunk],
                    'name': f'{base} ({i + 1}/{total})' if total > 1 else base,
                    'publisher': scap['author'] or '',
                    'description': '',
                }, m)
                sent = True
                if i < total - 1:
                    await asyncio.sleep(1)
        elif re.search('webp|image|video', mime):
            if re.search('video', mime):
                src = _get(q, 'msg') or q
                if (field(src, 'seconds') or 0) > 11:
                    return await m.reply('Maksimal 10 detik!')
            img = await maybe_await(q.download()) if callable(getattr(q, 'download', None)) else None
            if not img:
                raise PluginError('> Reply or caption image/video/stiker')
            try:
                await conn.sendSticker(m.chat, img, {'packname': scap['name'], 'author': scap['author']}, m)
                sent = True
            except Exception as e:
                print(e, file=sys.stderr)
            if not sent:
                out = None
                if re.search('webp', mime):
                    out = await webp2png(img)
                elif re.search('video|image', mime):
                    out = await upload(img)
                if not out or not isinstance(out, str):
                    out = await upload(img)
                await conn.sendSticker(m.chat, False, {'packname': scap['name'], 'author': scap['author'], 'url': out}, m)
                sent = True
        elif url_arg:
            buf = (await http.get(url_arg)).content
            out = await upload(buf)
            await conn.sendSticker(m.chat, False, {'packname': scap['name'], 'author': scap['author'], 'url': out}, m)
            sent = True
        elif args and '|' not in args[0]:
            return await m.reply('> URL tidak valid!')
    except Exception as e:
        print(e, file=sys.stderr)
    finally:
        if not sent:
            raise PluginError('> !  Conversion failed')


help = [v + ' (caption|reply media|reply album|url) [name|author]' for v in ('sticker', 's')]
tags = ['sticker']
command = re.compile(r'^s(tic?ker)?(gif)?(wm)?$', re.I)
limit = True
