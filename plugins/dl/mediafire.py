import re

from lib.compat import http
from lib.compat.html import load
from lib.utils.handler import PluginError


async def handler(m, *, conn, args, usedPrefix, command):
    if not args:
        raise PluginError(f"- *Example:* {usedPrefix + command} <url>")
    c = load((await http.get(args[0], raise_for_status=False)).text())
    url = (c('#downloadButton').attr('href') or '').strip()
    intro = c('div.dl-info > div.intro')
    filename = intro.find('div.filename').text().strip()
    filetype = intro.find('div.filetype > span').eq(0).text().strip()
    mt = re.search(r'\(\.(.*?)\)', intro.find('div.filetype > span').eq(1).text())
    ext = (mt.group(1).strip() if mt else '') or 'bin'
    li = c('div.dl-info > ul.details > li')
    upload = li.eq(1).find('span').text().strip()
    size = li.eq(0).find('span').text().strip()
    await conn.sendMessage(m.chat, {
        'document': {'url': url}, 'fileName': filename, 'mimetype': 'application/' + ext.lower(),
        'caption': f'- *File Name :* {filename}\n- *Type :* {filetype}\n- *Size :* {size}\n- *Uploaded :* {upload}',
    }, {'quoted': m})


help = [v + ' <url>' for v in ('mediafire', 'mf')]
tags = ['downloader']
command = re.compile(r'^(mediafire|mf)$', re.I)
