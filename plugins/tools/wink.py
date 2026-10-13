import asyncio
import os
import re

from lib.compat import http
from lib.utils.helper import mimetype_of
from lib.utils.handler import PluginError


HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
           'Referer': 'https://www.designkit.cn/quality', 'Origin': 'https://www.designkit.cn'}


async def enhance(inp):
    if not inp:
        raise ValueError('Input gambar wajib diisi (URL atau path file lokal).')
    buf = None
    if isinstance(inp, str) and inp.startswith('http'):
        buf = (await http.get(inp, timeout=25)).content
    elif isinstance(inp, (bytes, bytearray)):
        buf = bytes(inp)
    if not buf:
        raise ValueError('Gagal membaca file/URL gambar.')
    policy = (await http.get('https://strategy.app.meitudata.com/upload/policy?app=xiuxiu-pro&count=1&suffix=jpeg&type=ai_quality', headers=HEADERS, timeout=15)).json()
    first = policy[0] if isinstance(policy, list) and policy else {}
    qiniu = (first or {}).get('qiniu') or {}
    if not qiniu.get('token'):
        raise RuntimeError('Gagal mendapatkan token upload.')
    fields = {'token': qiniu['token']}
    if qiniu.get('key'):
        fields['key'] = qiniu['key']
    fields['file'] = {'content': buf, 'filename': 'image.jpg', 'contentType': 'image/jpeg'}
    up = (await http.post('https://up-qagw.meitudata.com/', multipart=fields, raise_for_status=False)).json()
    cloud = up.get('data')
    if not cloud:
        raise RuntimeError('Gagal upload gambar ke cloud Meitu.')
    gid = '1a08f8' + os.urandom(6).hex()
    task = (await http.post(f'https://webapi.designkit.cn/v3/mtlab/image_restoration_async?gid={gid}', json={
        'parameter': {'rsp_media_type': 'url', 'custom_size_flag': 1, 'create_value': 100, 'hdr_value': 0, 'resemblance_value': 80, 'save_photo_format': 1},
        'media_info_list': [{'media_data': cloud, 'media_extra': {}, 'media_profiles': {'media_data_type': 'url'}}], 'extra': {}},
        headers={**HEADERS, 'Content-Type': 'application/json'}, timeout=15)).json()
    msg_id = ((task.get('data') or {}).get('msg_id')) or task.get('msg_id')
    if not msg_id:
        raise RuntimeError('Gagal membuat tugas AI.')
    result = None
    for _ in range(30):
        await asyncio.sleep(1.2)
        q = (await http.get(f'https://webapi.designkit.cn/v1/mtlab/query_multi?msg_ids={msg_id}', headers=HEADERS, timeout=15)).json()
        data = q.get('data') or []
        media = ((data[0] if data else {}).get('media_info_list') or [None])[0]
        if media and media.get('media_data'):
            result = media
            break
    if not result:
        raise RuntimeError('Proses AI timeout.')
    prof = result.get('media_profiles') or {}
    return {'status': True, 'url': result['media_data'], 'width': prof.get('media_data_width'), 'height': prof.get('media_data_height'), 'size_bytes': prof.get('media_data_size')}


async def handler(m, *, conn, text):
    q = m.quoted if m.quoted else m
    mime = mimetype_of(q) or q.mediaType or ''
    url_input = (text or '').strip()
    if not mime and not url_input:
        raise PluginError('Reply/caption an image, or give a direct image URL, to enhance!')
    if mime and not re.search(r'image/(jpeg|jpg|png|webp)', mime, re.I):
        raise PluginError('Media must be an image (JPG/PNG/WEBP)!')
    await m.react('⏳')
    try:
        inp = await q.download() if mime else url_input
        r = await enhance(inp)
        res = await http.get(r['url'], timeout=120)
        dims = f" ({r['width']}x{r['height']})" if r.get('width') and r.get('height') else ''
        await conn.sendMessage(m.chat, {'image': res.content, 'caption': f'✅ Enhanced{dims}', 'mimetype': 'image/png'}, {'quoted': m})
        await m.react('✅')
    except Exception as error:
        await m.react('❌')
        raise PluginError(f'Error: {error}')


help = ['wink (Reply/Url Image)']
tags = ['tools']
command = re.compile(r'^(wink)$', re.I)
limit = True
