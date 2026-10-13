import json
import random
import re

from lib.compat import http
from lib.utils.helper import mimetype_of
from lib.utils.handler import PluginError


BASE_URL = 'https://www.iloveimg.com/upscale-image'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'


def parse_int(value):
    mt = re.match(r'\s*([+-]?\d+)', str(value or ''))
    return int(mt.group(1)) if mt else None


async def upscale_image(inp, multiplier=2):
    if not inp:
        raise ValueError('Input image (Buffer or URL) is required')
    scale = str(multiplier) if int(multiplier) in (2, 4) else '2'
    if isinstance(inp, str) and inp.startswith('http'):
        buf = (await http.get(inp, timeout=25)).content
    elif isinstance(inp, (bytes, bytearray)):
        buf = bytes(inp)
    else:
        raise ValueError('Invalid input type. Expected Buffer or image URL')
    html = (await http.get(BASE_URL, headers={'User-Agent': UA}, timeout=15)).text()
    mt = re.search(r'var ilovepdfConfig = ({[^;]+});', html)
    if not mt:
        raise RuntimeError('Failed to extract iLoveIMG config token')
    config = json.loads(mt.group(1))
    token = config['token']
    server = random.choice(config['servers']) if config.get('servers') else 'api1g'
    worker = f'https://{server}.iloveimg.com'
    idx = html.find('ilovepdfConfig.taskId = ')
    task_id = None
    if idx != -1:
        q1 = html.index("'", idx)
        q2 = html.index("'", q1 + 1)
        task_id = html[q1 + 1:q2]
    if not task_id:
        raise RuntimeError('Failed to extract pre-generated taskId')
    headers = {'Authorization': f'Bearer {token}', 'User-Agent': UA, 'Origin': 'https://www.iloveimg.com', 'Referer': 'https://www.iloveimg.com/'}
    up = await http.post(f'{worker}/v1/upload', multipart={'task': task_id, 'file': {'content': buf, 'filename': 'image.jpg', 'contentType': 'image/jpeg'}}, headers=headers, raise_for_status=False)
    try:
        up_data = up.json()
    except Exception:
        up_data = None
    server_filename = up_data.get('server_filename') if isinstance(up_data, dict) else None
    if not server_filename:
        raise RuntimeError('Upload failed: server_filename not returned')
    res = await http.post(f'{worker}/v1/upscale', multipart={'task': task_id, 'server_filename': server_filename, 'scale': scale}, headers=headers, timeout=60)
    return {'status': True, 'scale': f'{scale}x', 'sizeBytes': len(res.content), 'sizeMB': f'{len(res.content) / 1048576:.2f}', 'buffer': res.content}


async def handler(m, *, conn, text, args):
    q = m.quoted if m.quoted else m
    mime = mimetype_of(q) or q.mediaType or ''
    url_input = (text or '').strip().split()[0] if (text or '').strip() else None
    if not mime and not (url_input and url_input.startswith('http')):
        raise PluginError('Reply/caption an image, or give a direct image URL, to upscale!')
    if mime and not re.search(r'image/(jpeg|jpg|png|webp)', mime, re.I):
        raise PluginError('Media must be an image (JPG/PNG/WEBP)!')
    mult = parse_int(args[-1]) if args else None
    if not mult:
        mult = 2
    if mult not in (2, 4):
        mult = 2
    await m.react('⏳')
    try:
        inp = await q.download() if mime else url_input
        r = await upscale_image(inp, mult)
        await conn.sendMessage(m.chat, {'image': r['buffer'], 'caption': f"✅ Upscaled {r['scale']} ({r['sizeMB']} MB)", 'mimetype': 'image/png'}, {'quoted': m})
        await m.react('✅')
    except Exception as error:
        await m.react('❌')
        raise PluginError(f'Error: {error}')


help = [v + ' [2/4]' for v in ('iloveimg', 'ilimg')]
tags = ['tools']
command = re.compile(r'^(iloveimg|ilimg)$', re.I)
limit = True
