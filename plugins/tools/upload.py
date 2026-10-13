import re
import time

from lib.utils.handler import G, PluginError
from lib.utils.helper import mimetype_of, oc


def format_size(size):
    if size >= 1024 ** 3:
        return f'{size / 1024 ** 3:.2f} GB'
    if size >= 1024 ** 2:
        return f'{size / 1024 ** 2:.2f} MB'
    if size >= 1024:
        return f'{size / 1024:.2f} KB'
    return f'{size} B'


async def handler(m, *, conn):
    upload = G.scraper.upload.default
    q = m.quoted if m.quoted else m
    if not q:
        raise PluginError('- Reply or caption an image or video')
    mime = mimetype_of(q)
    if not mime:
        raise PluginError('- File type not supported')
    buffer = await q.download()
    ext = (mime.split('/')[1] or 'bin').split(';')[0] if '/' in mime else 'bin'
    filename = f'file_{int(time.time() * 1000)}.{ext}'
    url = await upload(buffer, filename)
    server = oc(G.opts, 'server')
    status = 'Expired In 7 Days' if server and url.startswith(server) else 'Never Expires'
    await conn.sendButton(m.chat, {
        'text': f'{format_size(len(buffer))} • {status}',
        'nativeFlow': [{}, {'text': 'Copy', 'copy': url}, {'text': 'View', 'url': url, 'useWebview': True}],
    }, m)


help = ['upload (reply media)', 'tourl (reply media)']
tags = ['tools']
command = re.compile(r'^(tourl|upload)$', re.I)
ai = {'risk': 'banned', 'description': 'upload a media into url'}
