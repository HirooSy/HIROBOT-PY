import re

from lib.utils.canvas import jimp
from lib.utils.helper import mimetype_of
from lib.utils.handler import PluginError


async def handler(m, *, conn, args, usedPrefix, command):
    towidth = args[0] if len(args) > 0 else None
    toheight = args[1] if len(args) > 1 else None
    if not towidth or not toheight:
        raise PluginError(f"- *Example:* {usedPrefix + command} <width> <height>")
    q = m.quoted if m.quoted else m
    mime = mimetype_of(q)
    if not mime:
        raise PluginError('- Please Reply/caption the image you want to resize.')
    media = await q.download()
    if not re.search(r'image/(png|jpe?g)', mime):
        raise PluginError(f'- Mime {mime} not Supported')
    source = jimp.metadata(media)
    size = {'before': {'height': source['height'], 'width': source['width']},
            'after': {'height': toheight, 'width': towidth}}
    tw, th = float(towidth), float(toheight)
    tw = int(tw) if tw.is_integer() else tw
    th = int(th) if th.is_integer() else th
    compres = await conn.resize(media, tw, th)
    await conn.sendFile(m.chat, compres, None,
                        f"                 *`Resize Image`*\n- *Width  :* {size['before']['width']} > {size['after']['width']}\n- *Height:* {size['before']['height']} > {size['after']['height']}", m)


help = ['resize [ width ] [ height]']
tags = ['tools']
command = re.compile(r'^(resize)$', re.I)
