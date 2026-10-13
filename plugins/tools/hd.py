import json
import random
import re
import string

from lib.compat import http
from lib.utils.helper import mimetype_of
from lib.utils.handler import PluginError


BASE = 'https://salman555-upscale-images.hf.space'
MODES = {2: {'api_name': 'upscale_x2', 'label': '×2'}, 4: {'api_name': 'standard_upscale', 'label': '×4'}, 8: {'api_name': 'premium_upscale', 'label': '×8 🚀'}}


def parse_int(value):
    mt = re.match(r'\s*([+-]?\d+)', str(value or ''))
    return int(mt.group(1)) if mt else None


async def upload_to_gradio(buf):
    res = await http.post(f'{BASE}/gradio_api/upload', multipart={'files': {'content': buf, 'filename': 'input.jpg', 'contentType': 'image/jpeg'}}, timeout=60)
    return res.json()[0]


async def run_upscale(path, api_name):
    session_hash = ''.join(random.choices(string.ascii_lowercase + string.digits, k=11))
    payload = {'data': [{'path': path, 'url': f'{BASE}/gradio_api/file={path}', 'orig_name': 'input.jpg', 'size': None, 'mime_type': 'image/jpeg',
                         'is_stream': False, 'meta': {'_type': 'gradio.FileData'}}], 'event_data': None,
               'fn_index': 0 if api_name == 'upscale_x2' else 1 if api_name == 'standard_upscale' else 2, 'trigger_id': None, 'session_hash': session_hash}
    join = await http.post(f'{BASE}/gradio_api/queue/join', json=payload, timeout=60)
    event_id = join.json()['event_id']
    sse = await http.get(f'{BASE}/gradio_api/queue/data', params={'session_hash': session_hash}, headers={'Accept': 'text/event-stream'}, timeout=300)
    completed = None
    for line in sse.text().split('\n'):
        if not line.startswith('data: '):
            continue
        try:
            js = json.loads(line[6:])
            if js.get('event_id') == event_id and js.get('msg') == 'process_completed':
                completed = js
        except Exception:
            pass
    if not completed:
        raise RuntimeError('Tidak menerima hasil dari server (timeout/SSE terputus)')
    if not completed.get('success'):
        raise RuntimeError('Server gagal memproses gambar')
    data = (completed.get('output') or {}).get('data')
    if not data or not data[0]:
        raise RuntimeError('Format hasil tidak sesuai ekspektasi')
    return data[0]['url']


async def handler(m, *, conn, args):
    q = m.quoted if m.quoted else m
    mime = mimetype_of(q) or q.mediaType or ''
    if not mime:
        raise PluginError('Reply/caption an image to upscale!')
    if not re.search(r'image/(jpeg|jpg|png|webp)', mime, re.I):
        raise PluginError('Media must be an image (JPG/PNG/WEBP)!')
    factor = parse_int(args[0]) if args else None
    if not factor:
        factor = 2
    if factor not in MODES:
        factor = 2
    mode = MODES[factor]
    await m.react('⏳')
    try:
        buf = await q.download()
        path = await upload_to_gradio(buf)
        url = await run_upscale(path, mode['api_name'])
        res = await http.get(url, timeout=120)
        await conn.sendMessage(m.chat, {'image': res.content, 'caption': f"✅ Upscale {mode['label']}", 'mimetype': 'image/png'}, {'quoted': m})
        await m.react('✅')
    except Exception as error:
        await m.react('❌')
        raise PluginError(f'Error: {error}')


help = ['hd [2/4/8]']
tags = ['tools']
command = re.compile(r'^(hd|upscale)$', re.I)
