import asyncio
import json
import os
import re
import time
from urllib.parse import urlparse

from lib.compat import http
from lib.utils.helper import mimetype_of
from lib.utils.handler import PluginError


API_BASE = 'https://api.ezremove.ai/api/ez-remove/v3/background-remove'
VALID_MODES = ['general_v1', 'general_v2', 'logo', 'text', 'anime', 'custom']


def _fail(code, error, inp=None):
    return {'Status': False, 'Code': code, 'Input': inp, 'Result': None, 'Error': error}


async def ez_remove(inp):
    try:
        raw_mode = inp.get('mode')
        mode = (raw_mode if raw_mode is not None else 'general_v1').lower()
        if mode not in VALID_MODES:
            return _fail(400, f'Invalid mode "{mode}". Use: {", ".join(VALID_MODES)}', inp)
        if isinstance(inp.get('imageBuffer'), (bytes, bytearray)):
            data, ctype, fname = bytes(inp['imageBuffer']), inp.get('contentType') or 'image/png', 'input.png'
        elif isinstance(inp.get('imageUrl'), str) and inp['imageUrl'].strip():
            r = await http.get(inp['imageUrl'], raise_for_status=False)
            if not r.ok:
                return _fail(r.status, f'Image download HTTP {r.status}', inp)
            data, ctype = r.content, r.headers.get('content-type') or 'image/png'
            segments = [s for s in urlparse(inp['imageUrl']).path.split('/') if s]
            fname = segments[-1] if segments else 'input.png'
        else:
            return _fail(400, "Provide 'imageBuffer' or 'imageUrl'.", inp)
        if len(data) < 100:
            return _fail(400, 'Image too small (min ~64x64 pixels recommended).', inp)
        serial = os.urandom(16).hex()
        headers = {'Product-Serial': serial, 'Origin': 'https://ezremove.ai', 'Referer': 'https://ezremove.ai/'}
        fields = {'image_file': {'content': data, 'filename': fname, 'contentType': ctype}, 'mode': mode}
        if isinstance(inp.get('params'), dict):
            fields['params'] = json.dumps(inp['params'], separators=(',', ':'), ensure_ascii=False)
        create = await http.post(f'{API_BASE}/create-job', multipart=fields, headers=headers, raise_for_status=False)
        cd = create.json()
        job = (cd.get('result') or {}).get('job_id')
        if create.status != 200 or not job:
            return _fail(create.status, f'Create job failed: {json.dumps(cd, separators=(",", ":"))[:250]}', inp)
        started, delay, result, last = time.time(), 1.0, None, None
        while time.time() - started < 40:
            await asyncio.sleep(delay)
            delay = min(delay + 0.5, 3)
            try:
                pr = await http.get(f'{API_BASE}/get-job/{job}', headers=headers, raise_for_status=False)
                if not pr.ok:
                    continue
                pd = pr.json()
                status = (pd.get('result') or {}).get('status')
                last = status
                if status == 2:
                    result = pd['result']
                    break
                if status == 3:
                    return _fail(502, f"Processing error: {(pd.get('result') or {}).get('error') or 'unknown'}", inp)
            except Exception:
                pass
        if not result:
            return {'Status': False, 'Code': 504, 'Input': inp,
                    'Error': f'Polling timeout (>40s). lastStatus={last}. Job ID: {job}',
                    'Result': {'jobId': job, 'inputUrl': (cd.get('result') or {}).get('image_url'), 'productSerial': serial}}
        out = result.get('output') or {}
        previews = out.get('preview') if isinstance(out.get('preview'), list) else []
        maxq = out.get('max') if isinstance(out.get('max'), list) else []
        return {'Status': True, 'Code': 200, 'Input': {'mode': mode, 'params': inp.get('params')}, 'Error': None,
                'Result': {
                    'message': f'Background removed (mode: {mode}) — {len(previews)} preview URL ready',
                    'jobId': job, 'mode': mode, 'inputUrl': (cd.get('result') or {}).get('image_url'),
                    'preview': previews[0] if previews else None, 'previewAll': previews,
                    'maxQuality': maxq[0] if maxq else None, 'maxQualityAll': maxq,
                    'productSerial': serial,
                }}
    except Exception as e:
        return {'Status': False, 'Code': getattr(getattr(e, 'response', None), 'status', None) or 500,
                'Input': inp, 'Result': None, 'Error': str(e)}


async def handler(m, *, conn, text, args, usedPrefix, command):
    text, args, p, command = text, args, usedPrefix, command
    q = m.quoted if m.quoted else m
    mime = mimetype_of(q) or q.mediaType or ''
    raw = (text or '').strip().split()[0] if (text or '').strip() else ''
    is_url = raw.startswith('http')
    mode_arg = next((a.lower() for a in args if a.lower() in VALID_MODES), None)
    if not mime and not is_url:
        return await conn.reply(m.chat, f"Reply/caption an image, or give a direct image URL, to remove its background.\n\n- {p + command} (reply to an image)\n- {p + command} <image_url>\n- {p + command} <mode> (reply to an image)\n\nModes: {', '.join(VALID_MODES)}", m)
    if mime and not re.search(r'image/(jpeg|jpg|png|webp)', mime, re.I):
        raise PluginError('Media must be an image (JPG/PNG/WEBP)!')
    await m.react('⏳')
    try:
        inp = {'imageBuffer': await q.download(), 'mode': mode_arg} if mime else {'imageUrl': raw, 'mode': mode_arg}
        result = await ez_remove(inp)
        if not result['Status']:
            raise RuntimeError(result['Error'])
        url = result['Result']['maxQuality'] or result['Result']['preview']
        if not url:
            raise RuntimeError('The server did not return a result URL.')
        res = await http.get(url, timeout=60)
        await conn.sendMessage(m.chat, {'image': res.content, 'caption': f"✅ Background removed ({result['Result']['mode']})", 'mimetype': 'image/png'}, {'quoted': m})
        await m.react('✅')
    except Exception as error:
        await m.react('❌')
        raise PluginError(f'Error: {error}')


help = ['removebg']
tags = ['tools']
command = re.compile(r'^(removebg)$', re.I)
limit = True
