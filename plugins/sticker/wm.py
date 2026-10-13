import re
import sys

from lib.utils.handler import PluginError


async def handler(m, *, conn, text):
    if not m.quoted:
        raise PluginError('Quoted the sticker!')
    sent = False
    try:
        packname, *author = text.split('|')
        mime = m.quoted.mimetype or ''
        if not re.search('webp', mime):
            raise PluginError('Reply sticker!')
        img = await m.quoted.download()
        if not img:
            raise PluginError('Reply a sticker!')
        await conn.sendSticker(m.chat, img, {'packname': packname or '', 'author': '|'.join(author)}, m)
        sent = True
    except Exception as e:
        print(e, file=sys.stderr)
    finally:
        if not sent:
            raise PluginError('Conversion failed')


help = ['wm <packname>|<author>']
tags = ['sticker']
command = re.compile(r'^wm$', re.I)
limit = True
