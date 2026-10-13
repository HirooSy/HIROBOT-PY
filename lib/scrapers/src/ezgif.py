import re
from urllib.parse import urljoin

from lib.compat import http
from lib.compat.html import load


async def _convert(source, page, selector):
    isUrl = isinstance(source, str) and re.search(r'https?://', source) is not None
    fields = {'new-image-url': source if isUrl else '', 'new-image': ''}
    if not isUrl:
        fields['new-image'] = {'content': source, 'filename': 'image.webp', 'contentType': 'image/webp'}
    res = await http.post(f'https://ezgif.com/{page}', multipart=fields, raise_for_status=False)
    c = load(res.text())
    obj = {}
    for el in c('form input[name]'):
        i = c(el)
        obj[i.attr('name')] = i.attr('value') or ''
    res2 = await http.post(f"https://ezgif.com/{page}/{obj.get('file')}", multipart=obj, raise_for_status=False)
    c2 = load(res2.text())
    src = c2(selector).attr('src')
    if not src:
        raise ValueError('Invalid URL')
    return urljoin(res2.url, src)


async def webp2mp4(source):
    return await _convert(source, 'webp-to-mp4', 'div#output > p.outfile > video > source')


async def webp2png(source):
    return await _convert(source, 'webp-to-png', 'div#output > p.outfile > img')
