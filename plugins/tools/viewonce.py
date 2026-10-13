import re

from lib.utils.helper import oc
from lib.utils.handler import PluginError


async def handler(m, *, conn):
    q = m.quoted
    if not q:
        raise PluginError("where's message?")
    is_vo = q.viewOnce is True or oc(q, 'msg', 'viewOnce') is True or any(
        oc(q, 'message', k, 'viewOnce') is True for k in ('imageMessage', 'videoMessage', 'audioMessage'))
    if not is_vo:
        raise PluginError("That's not a viewOnce message")
    buffer = await q.download()
    mtype = q.mtype
    media = (q.mediaMessage or {}).get(q.mediaType) or {}
    caption = media.get('caption') or ''
    name = {'videoMessage': 'video.mp4', 'audioMessage': 'audio.ogg'}.get(mtype, 'image.jpg')
    await conn.sendFile(m.chat, buffer, name, '' if mtype == 'audioMessage' else caption, m)


help = ['readviewonce', 'rvo']
tags = ['tools']
command = re.compile(r'^(readviewonce|rvo)', re.I)
ai = {'risk': 'low', 'description': 'To read/download viewonce message, and send it to user'}
