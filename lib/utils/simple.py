import asyncio
import base64
import copy as _copy
import hashlib
import hmac
import inspect
import json
import math
import os
import random
import re
import sys
import time
import uuid
from urllib.parse import quote

from baileys.foundation.node_crypto import createCipheriv
from lib.compat import ffmpeg as ff, http
from lib.utils import converter, database as dbmod
from lib.utils.canvas import add_exif, is_animated_webp, is_webp, jimp, to_webp
from lib.utils.handler import G
from lib.utils.helper import file_type_from_buffer, maybe_await, oc, spawn, to_simple_number
from baileys import (
    S_WHATSAPP_NET, USyncQuery, USyncUser, areJidsSameUser, downloadContentFromMessage,
    extractMessageContent, generateForwardMessageContent, generateMessageIDV2,
    generateWAMessageFromContent, getContentType, jidDecode, jidNormalizedUser,
    prepareWAMessageMedia, proto, toReadable,
)


ANIM_MAX_BYTES = 480 * 1024


ANIM_PROFILES = [
    {'fps': 12, 'quality': 50},
    {'fps': 10, 'quality': 35},
    {'fps': 8, 'quality': 25},
]


NICE_PATH = '/usr/bin/nice' if os.path.exists('/usr/bin/nice') else None


TRAY_ICON_FILE_NAME = 'tray_icon.webp'


def b64url(data):
    return base64.b64encode(data).decode().replace('+', '-').replace('/', '_').rstrip('=')


def hkdf_sha256(key, salt, info, length):
    prk = hmac.new(salt, key, hashlib.sha256).digest()
    out, block, counter = b'', b'', 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _is_lottie(sticker):
    return isinstance(sticker, dict) and bool(sticker.get('isLottie'))


def classify(buffer, sticker):
    if _is_lottie(sticker):
        return {'ext': 'json', 'mimetype': 'application/json', 'isAnimated': True, 'isLottie': True}
    return {'ext': 'webp', 'mimetype': 'image/webp', 'isAnimated': is_animated_webp(buffer), 'isLottie': False}


def _is_video_like(kind):
    return bool(kind) and (bool(re.search('video', kind['mime'], re.I)) or kind['ext'] == 'gif')


def _temp_base():
    os.makedirs(ff.TMP_DIR, exist_ok=True)
    return os.path.join(ff.TMP_DIR, f'stk_{int(time.time() * 1000)}_{os.urandom(4).hex()}')


async def _run_sticker_ffmpeg(args):
    if NICE_PATH:
        binary = NICE_PATH
        pre = ['-n', '10', ff.FFMPEG]
    else:
        binary = ff.FFMPEG
        pre = []
    await ff.run(binary, [*pre, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-threads', '1', '-filter_threads', '1', *args])


def _read_file(path):
    with open(path, 'rb') as fh:
        return fh.read()


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


async def _with_sticker_tmp(buffer, ext, fn):
    base = _temp_base()
    input_path = f'{base}.{ext or "bin"}'
    cleanup = [input_path]
    try:
        with open(input_path, 'wb') as fh:
            fh.write(bytes(buffer))

        def tmp_name(suffix):
            path = f'{base}{suffix}'
            cleanup.append(path)
            return path

        return await fn(input_path, tmp_name)
    finally:
        for path in cleanup:
            _unlink(path)


async def to_animated_webp(buffer, ext):
    is_webm = ext in ('webm', 'mkv')

    async def work(input_path, tmp_name):
        out = tmp_name('.webp')
        best = None
        for profile in ANIM_PROFILES:
            vf = ','.join([
                f'fps={profile["fps"]}',
                'scale=512:512:force_original_aspect_ratio=decrease:flags=bilinear',
                'format=rgba',
                'pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0',
                'setsar=1',
            ])
            encode = ['-vf', vf, '-vcodec', 'libwebp_anim', '-lossless', '0', '-q:v', str(profile['quality']),
                      '-compression_level', '0', '-loop', '0', '-an', '-vsync', '0', out]
            if is_webm:
                try:
                    await _run_sticker_ffmpeg(['-t', '10', '-c:v', 'libvpx-vp9', '-i', input_path, *encode])
                except Exception:
                    await _run_sticker_ffmpeg(['-t', '10', '-i', input_path, *encode])
            else:
                await _run_sticker_ffmpeg(['-t', '10', '-i', input_path, *encode])
            best = _read_file(out)
            if len(best) <= ANIM_MAX_BYTES:
                break
        return best

    return await _with_sticker_tmp(buffer, ext, work)


async def first_frame_png(buffer, ext):
    is_webm = ext in ('webm', 'mkv')

    async def work(input_path, tmp_name):
        out = tmp_name('.png')
        tail = ['-frames:v', '1', out]
        if is_webm:
            try:
                await _run_sticker_ffmpeg(['-c:v', 'libvpx-vp9', '-i', input_path, *tail])
            except Exception:
                await _run_sticker_ffmpeg(['-i', input_path, *tail])
        else:
            await _run_sticker_ffmpeg(['-i', input_path, *tail])
        return _read_file(out)

    return await _with_sticker_tmp(buffer, ext, work)


async def ensure_webp(buffer, sticker):
    if _is_lottie(sticker):
        return buffer
    if is_webp(buffer):
        return buffer
    kind = _safe_kind(buffer)
    if _is_video_like(kind):
        return await to_animated_webp(buffer, kind['ext'])
    return await jimp.resize(buffer, 512, 512, {
        'fit': 'contain',
        'background': {'r': 0, 'g': 0, 'b': 0, 'alpha': 0},
        'out': 'webp',
    })


def _safe_kind(buffer):
    try:
        return file_type_from_buffer(buffer)
    except Exception:
        return None


async def resolve_to_buffer(inp):
    if inp is None or inp is False or inp == '' or inp == 0:
        raise ValueError('Media kosong')
    if isinstance(inp, (bytes, bytearray)):
        return bytes(inp)
    if isinstance(inp, str):
        return await resolve_to_buffer({'url': inp})
    buffer = inp.get('buffer')
    if isinstance(buffer, (bytes, bytearray)):
        return bytes(buffer)
    url = inp.get('url')
    if url:
        if not isinstance(url, str):
            raise ValueError('url tidak valid')
        if re.match(r'^data:', url, re.I):
            parts = url.split(',')
            return base64.b64decode(parts[1] if len(parts) > 1 else '')
        if re.match(r'^https?://', url, re.I):
            res = await http.get(url, timeout=120, raise_for_status=False)
            if not res.ok:
                raise RuntimeError(f'Gagal fetch {url}: {res.status}')
            return res.content
        with open(url, 'rb') as fh:
            return fh.read()
    raise ValueError('Format media tidak dikenali (butuh url/buffer)')


async def make_tray_webp(buffer):
    return await jimp.resize(buffer, 252, 252, {'fit': 'cover', 'out': 'webp'})


async def make_tray_from_any(buffer):
    try:
        kind = _safe_kind(buffer)
        is_video = bool(kind) and (bool(re.search('video', kind['mime'], re.I)) or kind['ext'] in ('gif', 'webm'))
        if is_video:
            frame = await first_frame_png(buffer, kind['ext'])
            return await make_tray_webp(frame)
        return await make_tray_webp(buffer)
    except Exception as e:
        print('Tray gagal dibuat dari media, pakai fallback:', getattr(e, 'message', None) or e, file=sys.stderr)
        return None


async def make_blank_tray_webp():
    canvas_img = jimp.create(252, 252, 0x00000000)
    return await canvas_img.getBuffer('image/webp')


async def make_thumbnail_jpeg(buffer):
    return await jimp.resize(buffer, 252, 252, {'fit': 'cover', 'out': 'jpeg'})


async def upload_to_server(conn, buffer, hkdf, media_path, media_key=None):
    media_key = media_key or os.urandom(32)
    expanded = hkdf_sha256(media_key, bytes(32), hkdf.encode(), 112)
    iv, cipher_key, mac_key = expanded[:16], expanded[16:48], expanded[48:80]
    c = createCipheriv('aes-256-cbc', cipher_key, iv)
    encrypted = b''.join([c.update(buffer), c.final()])
    mac = hmac.new(mac_key, iv + encrypted, hashlib.sha256).digest()[:10]
    enc = encrypted + mac
    file_sha256 = hashlib.sha256(buffer).digest()
    enc_sha256 = hashlib.sha256(enc).digest()

    generate_tag = getattr(conn, 'generateMessageTag', None)
    tag = generate_tag() if callable(generate_tag) else None
    iq = await conn.query({'tag': 'iq', 'attrs': {'id': tag if tag is not None else str(int(time.time() * 1000)),
                                                  'to': 's.whatsapp.net', 'type': 'set', 'xmlns': 'w:m'},
                           'content': [{'tag': 'media_conn', 'attrs': {}}]})
    media_conn = next((v for v in (iq.get('content') or []) if v.get('tag') == 'media_conn'), None)
    if not media_conn:
        raise RuntimeError('media_conn tidak ditemukan')
    auth = (media_conn.get('attrs') or {}).get('auth')
    if not auth:
        raise RuntimeError('auth media_conn tidak ditemukan')
    hosts = [(v.get('attrs') or {}).get('hostname') for v in (media_conn.get('content') or []) if v.get('tag') == 'host']
    hosts = [h for h in hosts if h]
    if not hosts:
        raise RuntimeError('host upload tidak ditemukan')

    token = quote(b64url(enc_sha256), safe='')
    last = None
    for host in hosts:
        try:
            url = f'https://{host}{media_path}/{token}?auth={quote(auth, safe="")}&token={token}'
            res = await http.post(url, data=enc, headers={'Origin': 'https://web.whatsapp.com', 'Referer': 'https://web.whatsapp.com/',
                                                           'Content-Type': 'application/octet-stream'}, timeout=120, raise_for_status=False)
            if not 200 <= res.status < 300:
                raise RuntimeError(f'Upload gagal {res.status}: {res.text()}')
            try:
                js = res.json()
            except ValueError:
                raise RuntimeError(f'Response bukan JSON: {res.text()}')
            direct = js.get('direct_path') or js.get('directPath') or js.get('url') or js.get('path')
            if not direct:
                raise RuntimeError('directPath tidak ditemukan')
            return {'mediaKey': media_key, 'fileLength': len(buffer), 'fileSha256': file_sha256, 'fileEncSha256': enc_sha256,
                    'directPath': direct, **js}
        except Exception as e:
            last = e
    raise last or RuntimeError('Semua host upload gagal')


async def build_message(conn, items, tray=None, name=None, publisher=None, description=None):
    if not items:
        raise ValueError('Pack tidak boleh kosong')
    zf = converter.ZipFile()
    meta = []
    for item in items:
        stamped = add_exif(item['buffer'], name or 'Sticker Pack', publisher or '') if (not item['isLottie'] and item['mimetype'] == 'image/webp') else item['buffer']
        fname = f"{b64url(hashlib.sha256(stamped).digest())}.{item['ext']}"
        zf.file(fname, stamped)
        meta.append({'fileName': fname, 'isAnimated': item['isAnimated'], 'emojis': [''], 'accessibilityLabel': '',
                     'isLottie': item['isLottie'], 'mimetype': item['mimetype']})

    resolved = await make_tray_from_any(tray) if tray else None
    if not resolved:
        for item in items:
            if item['isLottie']:
                continue
            source = item.get('source')
            resolved = await make_tray_from_any(source if source is not None else item['buffer'])
            if resolved:
                break
    if not resolved:
        resolved = await make_blank_tray_webp()
    zf.file(TRAY_ICON_FILE_NAME, resolved)

    archive = await zf.generateAsync(type='nodebuffer', compression='STORE')
    pack = await upload_to_server(conn, archive, 'WhatsApp Sticker Pack Keys', '/mms/sticker-pack')
    thumb_buf = await make_thumbnail_jpeg(resolved)
    thumb = await upload_to_server(conn, thumb_buf, 'WhatsApp Sticker Pack Thumbnail Keys', '/mms/thumbnail-sticker-pack', pack['mediaKey'])
    return {
        'messageContextInfo': {'messageSecret': os.urandom(32)},
        'stickerPackMessage': {
            'stickerPackId': 'Pack_' + os.urandom(8).hex(), 'name': name or 'Sticker Pack', 'publisher': publisher or '',
            'packDescription': description or '', 'stickers': meta, 'fileLength': pack['fileLength'], 'fileSha256': pack['fileSha256'],
            'fileEncSha256': pack['fileEncSha256'], 'mediaKey': pack['mediaKey'], 'directPath': pack['directPath'],
            'mediaKeyTimestamp': int(time.time()), 'stickerPackSize': pack['fileLength'], 'stickerPackOrigin': 2,
            'trayIconFileName': TRAY_ICON_FILE_NAME, 'thumbnailDirectPath': thumb['directPath'], 'thumbnailSha256': thumb['fileSha256'],
            'thumbnailEncSha256': thumb['fileEncSha256'], 'thumbnailHeight': 252, 'thumbnailWidth': 252,
            'imageDataHash': base64.b64encode(thumb['fileSha256']).decode(),
        },
    }


async def send_sticker_pack(conn, jid, options, quoted=None):
    options = options or {}
    stickers = options.get('stickers')
    if not stickers:
        raise ValueError('stickers tidak boleh kosong')
    concurrency = options.get('concurrency', 1)

    results = [None] * len(stickers)
    cursor = {'i': 0}

    async def worker():
        while cursor['i'] < len(stickers):
            i = cursor['i']
            cursor['i'] += 1
            s = stickers[i]
            descriptor = s.get('data') if isinstance(s, dict) and s.get('data') is not None else s
            try:
                source = await resolve_to_buffer(descriptor)
                buffer = await ensure_webp(source, s)
                kind = classify(buffer, s)
                results[i] = {'buffer': buffer, 'source': source if (i < 3 and buffer is not source) else None, **kind}
            except Exception as e:
                print('Skip sticker (gagal convert):', getattr(e, 'message', None) or e, file=sys.stderr)

    workers = max(1, min(int(concurrency), len(stickers)))
    await asyncio.gather(*[worker() for _ in range(workers)])

    items = [r for r in results if r]
    if not items:
        raise RuntimeError('Semua sticker gagal diproses')

    tray = None
    cover = options.get('cover')
    if cover:
        try:
            first_raw = stickers[0]
            first = first_raw.get('data', first_raw) if isinstance(first_raw, dict) else first_raw
            first_url = first.get('url') if isinstance(first, dict) else None
            cover_url = cover.get('url') if isinstance(cover, dict) else None
            if cover_url and first_url == cover_url and results[0]:
                tray = results[0]['source'] if results[0]['source'] is not None else results[0]['buffer']
            else:
                tray = await resolve_to_buffer(cover)
        except Exception as e:
            print('Cover gagal diambil, pakai sticker pertama:', getattr(e, 'message', None) or e, file=sys.stderr)

    message = await build_message(conn, items, tray, options.get('name'), options.get('publisher'), options.get('description'))
    return await conn.relayMessage(jid, message, {'quoted': quoted} if quoted else {})

COUNTRY_CALLING_CODES = sorted([
    '211', '212', '213', '216', '218', '220', '221', '222', '223', '224', '225', '226', '227', '228', '229',
    '230', '231', '232', '233', '234', '235', '236', '237', '238', '239', '240', '241', '242', '243', '244',
    '245', '246', '247', '248', '249', '250', '251', '252', '253', '254', '255', '256', '257', '258', '260',
    '261', '262', '263', '264', '265', '266', '267', '268', '269', '290', '291', '297', '298', '299',
    '350', '351', '352', '353', '354', '355', '356', '357', '358', '359', '370', '371', '372', '373', '374',
    '375', '376', '377', '378', '380', '381', '382', '383', '385', '386', '387', '389',
    '420', '421', '423', '500', '501', '502', '503', '504', '505', '506', '507', '508', '509',
    '590', '591', '592', '593', '594', '595', '596', '597', '598', '599',
    '670', '672', '673', '674', '675', '676', '677', '678', '679', '680', '681', '682', '683', '685', '686',
    '687', '688', '689', '690', '691', '692',
    '850', '852', '853', '855', '856', '880', '886', '960', '961', '962', '963', '964', '965', '966', '967',
    '968', '970', '971', '972', '973', '974', '975', '976', '977', '992', '993', '994', '995', '996', '998',
    '20', '27', '30', '31', '32', '33', '34', '36', '39', '40', '41', '43', '44', '45', '46', '47', '48', '49',
    '51', '52', '53', '54', '55', '56', '57', '58', '60', '61', '62', '63', '64', '65', '66', '81', '82', '84',
    '86', '90', '91', '92', '93', '94', '95', '98',
    '1', '7',
], key=lambda c: -len(c))


class _JsNull:
    __slots__ = ()

    def __repr__(self):
        return 'null'


NULL = _JsNull()


def _nullish(v):
    return v is None or v is NULL


def _truthy(v):
    if v is None or v is NULL:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0 and not (isinstance(v, float) and math.isnan(v))
    if isinstance(v, str):
        return len(v) > 0
    return True


def _or(*vals):
    for v in vals[:-1]:
        if _truthy(v):
            return v
    return vals[-1] if vals else None


def _nn(*vals):
    for v in vals[:-1]:
        if not _nullish(v):
            return v
    return vals[-1] if vals else None


def _at(seq, i):
    if seq is None:
        return None
    try:
        return seq[i] if -len(seq) <= i < len(seq) else None
    except TypeError:
        return None


def _get(obj, key):
    if obj is None or obj is NULL:
        return None
    if isinstance(obj, dict):
        value = obj.get(key)
        return value.get() if isinstance(value, _Lazy) else value
    if isinstance(obj, (list, tuple, str, bytes)):
        return None
    try:
        return getattr(obj, key, None)
    except Exception:
        return None


def _attr(obj, name):
    return _get(obj, name)


def _num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if v is NULL:
        return 0.0
    if v is None:
        return float('nan')
    if isinstance(v, str):
        s = v.strip()
        if s == '':
            return 0.0
        try:
            return float(s)
        except ValueError:
            return float('nan')
    return float('nan')


def _str(v):
    if v is None:
        return 'undefined'
    if v is NULL:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float):
        if math.isnan(v):
            return 'NaN'
        if v.is_integer():
            return str(int(v))
        return repr(v)
    if isinstance(v, (list, tuple)):
        return ','.join('' if x is None or x is NULL else _str(x) for x in v)
    if isinstance(v, dict):
        return '[object Object]'
    return str(v)


def _parse_int(v):
    m = re.match(r'\s*[+-]?[0-9]+', _str(v)) if v is not None else None
    if not m:
        return float('nan')
    return int(m.group(0))


def _json_prep(v):
    if isinstance(v, dict):
        return {k: _json_prep(x) for k, x in v.items() if x is not None}
    if isinstance(v, (list, tuple)):
        return [None if x is None else _json_prep(x) for x in v]
    if v is NULL:
        return None
    if isinstance(v, (bytes, bytearray)):
        return {'type': 'Buffer', 'data': list(v)}
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        if v.is_integer():
            return int(v)
    return v


def _json_default(value):
    if hasattr(value, 'toNumber'):
        return value.toNumber()
    if isinstance(value, (bytes, bytearray)):
        return {'type': 'Buffer', 'data': list(value)}
    return str(value)


def js_json(value):
    return json.dumps(_json_prep(value), separators=(',', ':'), ensure_ascii=False, default=_json_default)


def _is_str(v):
    return isinstance(v, str)


def _is_buf(v):
    return isinstance(v, (bytes, bytearray))


def _is_plain_obj(v):
    return isinstance(v, dict)


def _log_warn(logger, *args):
    fn = _get(logger, 'warn')
    if callable(fn):
        try:
            fn(' '.join(_str(a) for a in args))
            return
        except Exception:
            pass
    print(*args, file=sys.stderr)


def _emit(conn, event, payload):
    ev = _attr(conn, 'ev')
    fn = ev.get('emit') if isinstance(ev, dict) else getattr(ev, 'emit', None)
    if callable(fn):
        return fn(event, payload)
    return None


def _connection_mod():
    from lib.utils import connection
    return connection


def _store():
    return _connection_mod().store


def _db_users():
    data = dbmod.database.data
    if not isinstance(data, dict):
        return None
    return data.get('users')


def _promise(value):
    if inspect.isawaitable(value) and not isinstance(value, asyncio.Future):
        return asyncio.ensure_future(value)
    return value


async def _then(value, fn):
    if inspect.isawaitable(value):
        value = await value
    return await maybe_await(fn(value))


async def wait_all_promises(value):
    if inspect.isawaitable(value):
        value = await value
    if isinstance(value, (list, tuple)):
        return list(await asyncio.gather(*[wait_all_promises(v) for v in value]))
    if isinstance(value, dict):
        keys = list(value.keys())
        results = await asyncio.gather(*[wait_all_promises(value[k]) for k in keys])
        return dict(zip(keys, results))
    return value


def _strip_message(mtype):
    return re.sub(r'message', '', mtype, count=1, flags=re.I)


def _to_js_decode(value):
    if not isinstance(value, str):
        return value
    if re.search(r':\d+@', value, re.I):
        decoded = jidDecode(value) or {}
        user, server = _get(decoded, 'user'), _get(decoded, 'server')
        if user and server:
            return (f'{user}@{server}' or value).strip()
        return value.strip()
    return value.strip()


def string_decode_jid(value):
    return _to_js_decode(value)


def group_digits(digits):
    return '-'.join(digits[i:i + 4] for i in range(0, len(digits), 4))


def format_intl_number(number):
    digits = re.sub(r'[^0-9]', '', _str(number))
    if not digits:
        return '+' + _str(number)
    code = next((c for c in COUNTRY_CALLING_CODES if digits.startswith(c)), None)
    subscriber = digits[len(code):] if code else ''
    if not code or len(subscriber) < 4:
        return '+' + digits
    return f'+{code} {group_digits(subscriber)}'


def decode_jid(jid):
    if not jid:
        return jid
    if re.search(r':\d+@', jid, re.I):
        decoded = jidDecode(jid) or {}
        user, server = _get(decoded, 'user'), _get(decoded, 'server')
        return f'{user}@{server}' if (user and server) else jid
    try:
        return jidNormalizedUser(jid)
    except Exception:
        return jid


def _decode_jid_method(conn, jid):
    if not _is_str(jid):
        return jid if _truthy(jid) else None
    jid = jid.strip()
    if jid.endswith('@lid'):
        return jid
    try:
        decoded = jidDecode(jid)
        user, server = _get(decoded, 'user'), _get(decoded, 'server')
        if user and server:
            jid = f'{user}@{server}'
    except Exception:
        pass
    try:
        jid = jidNormalizedUser(jid) or jid
    except Exception:
        pass
    return jid


def is_lid_jid(jid):
    return _is_str(jid) and decode_jid(jid).endswith('@lid')


def is_phone_jid(jid):
    return _is_str(jid) and decode_jid(jid).endswith('@s.whatsapp.net')


def normalize_participant(participant):
    if not _truthy(participant):
        return {'lid': None, 'number': None}
    lid = None
    number = None
    raw_id = _get(participant, 'id')
    if isinstance(raw_id, dict):
        inner = raw_id
        inner_id = decode_jid(_or(inner.get('id'), inner.get('jid'), ''))
        if is_lid_jid(inner_id):
            lid = inner_id
        inner_pn = _or(inner.get('phoneNumber'), inner.get('pn'), inner.get('phone_number'), '')
        if is_phone_jid(inner_pn):
            number = decode_jid(inner_pn)
        elif _truthy(inner_pn):
            cleaned = re.sub(r'[^0-9]', '', _str(inner_pn))
            if len(cleaned) >= 7:
                number = cleaned + '@s.whatsapp.net'
    elif _is_str(raw_id):
        decoded = decode_jid(raw_id)
        if is_lid_jid(decoded):
            lid = decoded
        elif is_phone_jid(decoded):
            number = decoded
    pn = _or(_get(participant, 'phoneNumber'), _get(participant, 'pn'), _get(participant, 'phone_number'))
    if _truthy(pn):
        if is_phone_jid(pn):
            number = decode_jid(pn)
        else:
            cleaned = re.sub(r'[^0-9]', '', _str(pn))
            if len(cleaned) >= 7:
                number = cleaned + '@s.whatsapp.net'
    if is_lid_jid(_get(participant, 'lid')):
        lid = decode_jid(_get(participant, 'lid'))
    return {'lid': lid, 'number': number}


def participant_matches_lid(participant, lid_number):
    lid_num = lid_number.split('@')[0]
    lid = normalize_participant(participant)['lid']
    if lid == lid_number:
        return True
    if lid is not None and lid.split('@')[0] == lid_num:
        return True
    raw_id = _get(participant, 'id')
    if isinstance(raw_id, dict):
        inner_id = _str(_or(raw_id.get('id'), raw_id.get('jid'), ''))
        if inner_id == lid_number or inner_id.split('@')[0] == lid_num:
            return True
    return False


def match_participant(conn, p, target_jid):
    if not _truthy(p) or not _truthy(target_jid):
        return False
    pid = _get(p, 'id')
    decoded = decode_jid(pid)
    if not _truthy(decoded):
        fn = _attr(conn, 'decodeJid')
        decoded = fn(pid) if callable(fn) else None
    if decoded == target_jid:
        return True
    pn = _or(_get(p, 'phoneNumber'), _get(p, 'pn'), _get(p, 'phone_number'), '')
    if _truthy(pn) and pn == target_jid:
        return True
    norm = normalize_participant(p)
    number, lid = norm['number'], norm['lid']
    if number and number == target_jid:
        return True
    if lid and lid == target_jid:
        return True
    target_num = _str(target_jid).split('@')[0]
    if number and number.split('@')[0] == target_num:
        return True
    if _truthy(pn) and _str(pn).split('@')[0] == target_num:
        return True
    return False


async def _resolve_from_group(lid_number, group_jid, conn):
    def try_metadata(metadata):
        participants = _get(metadata, 'participants')
        if not _truthy(participants):
            return None
        for p in participants:
            if not participant_matches_lid(p, lid_number):
                continue
            number = normalize_participant(p)['number']
            if number:
                return number
        return None

    try:
        cached = await _store().fetchGroupMetadata(group_jid, _attr(conn, 'groupMetadata'))
        found = try_metadata(cached)
        if found:
            return found
    except Exception as e:
        print(f'[LID] fetchGroupMetadata (cache) gagal {group_jid}:', e, file=sys.stderr)
    try:
        fresh = await maybe_await(_attr(conn, 'groupMetadata')(group_jid))
        found = try_metadata(fresh)
        if found:
            return found
        parts = _get(fresh, 'participants') or []
        sample = [
            {'id': _get(p, 'id'), 'phoneNumber': _or(_get(p, 'phoneNumber'), _get(p, 'pn'), None), 'lid': _or(_get(p, 'lid'), None)}
            for p in parts[:3]
        ]
        print(f'[LID] {group_jid} fetched fresh, tapi {lid_number} tidak match. Contoh bentuk participant:', js_json(sample), file=sys.stderr)
    except Exception as e:
        print(f'[LID] Failed fresh group fetch {group_jid}:', e, file=sys.stderr)
    return None


async def _resolve_from_all_groups(lid_number, conn):
    chats = _get(_store(), 'chats') or {}
    known = [j for j in chats.keys() if j.endswith('@g.us')]
    for group_jid in known:
        try:
            cached = _get(chats.get(group_jid), 'metadata')
            participants = _get(cached, 'participants')
            if _truthy(participants):
                for p in participants:
                    if not participant_matches_lid(p, lid_number):
                        continue
                    number = normalize_participant(p)['number']
                    if number:
                        return number
        except Exception:
            pass
    return None


async def _resolve_lid_via_usync(lid_number, conn):
    query = _attr(conn, 'query')
    if not callable(query):
        print(f'[LID] conn.query tidak tersedia — skip USync lookup ({lid_number})', file=sys.stderr)
        return None
    try:
        usync_query = USyncQuery()
        usync_query.withContext('interactive')
        usync_query.withMode('query')
        usync_query.withLIDProtocol()
        usync_query.withUser(USyncUser().withLid(lid_number))
        user_nodes = []
        for user in usync_query.users:
            content = [e for e in (p.getUserElement(user) for p in usync_query.protocols) if e]
            if content:
                user_nodes.append({'tag': 'user', 'attrs': {'jid': user.lid}, 'content': content})
        if not user_nodes:
            return None
        gen_tag = _attr(conn, 'generateMessageTag')
        tag = gen_tag() if callable(gen_tag) else f'{int(time.time() * 1000)}.{random.randrange(1000)}'
        iq = {
            'tag': 'iq',
            'attrs': {'to': S_WHATSAPP_NET or '@s.whatsapp.net', 'type': 'get', 'xmlns': 'usync'},
            'content': [{
                'tag': 'usync',
                'attrs': {'context': usync_query.context, 'mode': usync_query.mode, 'sid': tag, 'last': 'true', 'index': '0'},
                'content': [
                    {'tag': 'query', 'attrs': {}, 'content': [p.getQueryElement() for p in usync_query.protocols]},
                    {'tag': 'list', 'attrs': {}, 'content': user_nodes},
                ],
            }],
        }
        result = await maybe_await(query(iq))
        parsed = usync_query.parseUSyncQueryResult(result)
        entry = next((item for item in (_get(parsed, 'list') or []) if _get(item, 'lid') or _get(item, 'id') == lid_number), None)
        raw_pn = _get(entry, 'lid')
        if not raw_pn:
            print(f'[LID] USync lid-protocol query({lid_number}) tidak balikin PN', js_json(_get(parsed, 'list')), file=sys.stderr)
            return None
        normalized_pn = decode_jid(raw_pn) if '@' in raw_pn else decode_jid(f'{raw_pn}@s.whatsapp.net')
        if not normalized_pn.endswith('@s.whatsapp.net'):
            return None
        try:
            repo = _get(_attr(conn, 'signalRepository'), 'lidMapping')
            store_fn = _get(repo, 'storeLIDPNMappings')
            if callable(store_fn):
                await maybe_await(store_fn([{'lid': lid_number, 'pn': normalized_pn}]))
        except Exception as e:
            print('[LID] gagal nyimpen hasil USync ke lidMapping cache:', e, file=sys.stderr)
        return normalized_pn
    except Exception as e:
        print(f'[LID] USync lid-protocol query({lid_number}) error:', e, file=sys.stderr)
        return None


async def resolve_lid_to_number(lid_number, conn, chat_id=None):
    if not lid_number or not lid_number.endswith('@lid'):
        return None
    try:
        lid_store = _get(_attr(conn, 'signalRepository'), 'lidMapping')
        get_pn = _get(lid_store, 'getPNForLID')
        if not callable(get_pn):
            print(f'[LID] signalRepository.lidMapping.getPNForLID tidak tersedia di versi baileys ini — skip step 1 ({lid_number})', file=sys.stderr)
        else:
            pn_result = await maybe_await(get_pn(lid_number))
            raw_pn = pn_result if isinstance(pn_result, str) else (_or(_get(pn_result, 'pn'), _get(pn_result, 'phoneNumber')) if pn_result is not None else None)
            if _truthy(raw_pn):
                normalized_pn = decode_jid(raw_pn) if '@' in raw_pn else decode_jid(f'{raw_pn}@s.whatsapp.net')
                if normalized_pn.endswith('@s.whatsapp.net'):
                    return normalized_pn
            print(f'[LID] getPNForLID({lid_number}) tidak balikin PN valid:', js_json(pn_result), file=sys.stderr)
    except Exception as e:
        print(f'[LID] getPNForLID({lid_number}) error:', e, file=sys.stderr)
    try:
        find_user_id = _attr(conn, 'findUserId')
        if callable(find_user_id):
            result = await maybe_await(find_user_id(lid_number))
            pn = _get(result, 'phoneNumber')
            if _is_str(pn) and pn.endswith('@s.whatsapp.net'):
                return pn
            print(f'[LID] findUserId({lid_number}) tidak balikin phoneNumber valid:', js_json(result), file=sys.stderr)
    except Exception as e:
        print(f'[LID] findUserId({lid_number}) error:', e, file=sys.stderr)
    from_usync = await _resolve_lid_via_usync(lid_number, conn)
    if from_usync:
        return from_usync
    if _is_str(chat_id) and chat_id.endswith('@g.us'):
        found = await _resolve_from_group(lid_number, chat_id, conn)
        if found:
            return found
        print(f'[LID] Tidak ketemu di group metadata chat {chat_id} untuk {lid_number}', file=sys.stderr)
    from_all = await _resolve_from_all_groups(lid_number, conn)
    if from_all:
        return from_all
    print(f'[LID] GAGAL resolve {lid_number} lewat semua metode (lidMapping, findUserId, USync lid-protocol, group metadata chat ini, scan semua grup dikenal)', file=sys.stderr)
    return None


async def update_user_mapping(sender_jid, actual_number, lid_number):
    if not sender_jid:
        return sender_jid
    primary_key = actual_number if _is_str(actual_number) and actual_number.endswith('@s.whatsapp.net') else (
        sender_jid if sender_jid.endswith('@s.whatsapp.net') else None)
    working_key = primary_key or sender_jid
    users = _db_users()
    if not isinstance(users.get(working_key), dict):
        users[working_key] = {}
    user = users[working_key]
    changed = False
    if _is_str(actual_number) and actual_number.endswith('@s.whatsapp.net') and user.get('number') != actual_number:
        user['number'] = actual_number
        changed = True
    if _is_str(lid_number) and lid_number.endswith('@lid') and user.get('lid') != lid_number:
        user['lid'] = lid_number
        changed = True
    if working_key.endswith('@lid') and primary_key and primary_key != working_key:
        if not isinstance(users.get(primary_key), dict):
            users[primary_key] = {}
        for k, v in list(user.items()):
            if users[primary_key].get(k) is None:
                users[primary_key][k] = v
        users[primary_key]['number'] = primary_key
        users[primary_key]['lid'] = lid_number or working_key
        del users[working_key]
        changed = True
    if primary_key and lid_number and isinstance(users.get(lid_number), dict) and primary_key != lid_number:
        lid_entry = users[lid_number]
        target = users.get(primary_key) or {}
        for k, v in list(lid_entry.items()):
            if target.get(k) is None:
                target[k] = v
        users[primary_key] = target
        del users[lid_number]
        changed = True
    if changed:
        try:
            await dbmod.database.write()
        except Exception as e:
            print('[MAPPING] Failed to save:', e, file=sys.stderr)
    return primary_key or working_key


def _is_num_value(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and math.isnan(v))


async def auto_merge_lid_users():
    try:
        users = _db_users() if isinstance(dbmod.database.data, dict) else None
        if users is None:
            return 0
        to_delete = []
        for key, user in list(users.items()):
            number = user.get('number')
            if key.endswith('@lid') and _truthy(number) and _is_str(number) and number.endswith('@s.whatsapp.net'):
                target_key = number
                if not isinstance(users.get(target_key), dict):
                    users[target_key] = {}
                target = users[target_key]
                source = user
                for field, value in list(source.items()):
                    if field in ('number', 'lid'):
                        continue
                    if target.get(field) is None or target.get(field) == '':
                        target[field] = value
                    elif _is_num_value(value):
                        if field == 'exp':
                            target[field] = _num(target.get(field) or 0) + value
                        elif field in ('limit', 'level', 'warn'):
                            if value > _num(target.get(field) or 0):
                                target[field] = value
                        elif field == 'regTime' and value != -1:
                            if target.get(field) == -1 or value < _num(target.get(field) or 0):
                                target[field] = value
                        elif field in ('daily', 'premiumTime') and value > _num(target.get(field) or 0):
                            target[field] = value
                    elif isinstance(value, bool):
                        if value is True and target.get(field) is not True:
                            target[field] = True
                    elif _is_str(value) and value and not target.get(field):
                        target[field] = value
                target['number'] = target_key
                if source.get('lid') and not target.get('lid'):
                    target['lid'] = source['lid']
                to_delete.append(key)
        for key in to_delete:
            del users[key]
        if to_delete:
            await dbmod.database.write()
        return len(to_delete)
    except Exception as err:
        print('[Merge] Error:', getattr(err, 'message', err), file=sys.stderr)
        return 0


def _ch(text, i):
    if i < 0 or i >= len(text):
        return None
    return text[i]


_WS_RE = re.compile(r'\s')


async def to_image_buffer(value):
    if _is_buf(value):
        return value
    if _is_str(value) and re.match(r'https?://', value, re.I):
        res = await http.get(value, raise_for_status=False)
        if not res.ok:
            raise RuntimeError(f'Failed to fetch image: {res.status} {res.statusText}')
        return bytes(res.content)
    if _is_str(value):
        return value
    raise TypeError('Unsupported input type for image buffer (expected Buffer, URL, or file path)')


async def fetch_buffer(url, silent=True):
    try:
        res = await http.get(url, raise_for_status=False)
        if not res.ok:
            raise RuntimeError(f'HTTP {res.status}')
        return bytes(res.content)
    except Exception:
        if silent:
            return b''
        raise


async def toolkit_resize(buffer, x, y, fit='cover'):
    return await jimp.resize(buffer, x, y, {
        'fit': fit,
        'position': 'center',
        'background': {'r': 0, 'g': 0, 'b': 0, 'alpha': 0},
        'out': 'png',
    })


async def to_url(client, path, media_type='document'):
    if not _truthy(path):
        raise RuntimeError('Url or buffer needed')
    media = await prepareWAMessageMedia(
        {media_type: bytes(path) if _is_buf(path) else {'url': path}},
        {'upload': _attr(client, 'waUploadToServer'), 'jid': '@newsletter'},
    )
    first = next(iter(media.values()), None) if isinstance(media, dict) else None
    return _get(first, 'url')


def _lenient_b64decode(text):
    cleaned = re.sub(r'[^A-Za-z0-9+/]', '', text)
    if len(cleaned) % 4 == 1:
        cleaned = cleaned[:-1]
    cleaned += '=' * (-len(cleaned) % 4)
    return base64.b64decode(cleaned)


async def resolve_media(client, media, media_type='image', resolve_url=False, resolve_wa_url=False,
                        result='url', resize=False, width=300, height=300):
    if isinstance(media, list):
        return list(await asyncio.gather(*[
            resolve_media(client, item, media_type, resolve_url, resolve_wa_url, result, resize, width, height)
            for item in media
        ]))
    is_url = lambda s: re.match(r'https?://.+', s, re.I) is not None
    is_wa_url = lambda s: re.match(r'https?://[^/]*\.whatsapp\.net/', s, re.I) is not None
    if _is_str(media) and is_url(media):
        if is_wa_url(media):
            if resolve_wa_url:
                media = await fetch_buffer(media)
            elif not resolve_url:
                if result == 'url':
                    return media
                media = await fetch_buffer(media)
        else:
            if not resolve_url:
                if result == 'url':
                    return media
                media = await fetch_buffer(media)
            else:
                media = await fetch_buffer(media)
    if _is_str(media) and not is_url(media):
        media = _lenient_b64decode(media)
    if not _is_buf(media) or not len(media):
        return None
    if resize and _is_buf(media):
        media = await toolkit_resize(media, width, height)
    if result == 'buffer':
        return media
    if result == 'base64':
        return base64.b64encode(bytes(media)).decode()
    return await to_url(client, media, media_type)


def get_mp4_duration(buffer, silent=True):
    try:
        if not _is_buf(buffer) or len(buffer) < 8:
            if silent:
                return 0
            raise ValueError('Invalid buffer')
        offset = 0
        while offset < len(buffer) - 8:
            size = int.from_bytes(buffer[offset:offset + 4], 'big')
            if size < 8 or offset + size > len(buffer):
                if silent:
                    return 0
                raise ValueError('Invalid atom size')
            atom = bytes(buffer[offset + 4:offset + 8]).decode('latin1')
            if atom == 'moov':
                moov_offset = offset + 8
                moov_end = offset + size
                while moov_offset < moov_end - 8:
                    child_size = int.from_bytes(buffer[moov_offset:moov_offset + 4], 'big')
                    if child_size < 8 or moov_offset + child_size > moov_end:
                        if silent:
                            return 0
                        raise ValueError('Invalid child atom size')
                    child_type = bytes(buffer[moov_offset + 4:moov_offset + 8]).decode('latin1')
                    if child_type == 'mvhd':
                        version = buffer[moov_offset + 8]
                        if version == 0:
                            timescale = int.from_bytes(buffer[moov_offset + 20:moov_offset + 24], 'big')
                            duration = int.from_bytes(buffer[moov_offset + 24:moov_offset + 28], 'big')
                            if not timescale:
                                if silent:
                                    return 0
                                raise ValueError('Invalid timescale')
                            return duration / timescale
                        if version == 1:
                            timescale = int.from_bytes(buffer[moov_offset + 32:moov_offset + 36], 'big')
                            duration = int.from_bytes(buffer[moov_offset + 36:moov_offset + 44], 'big')
                            if not timescale:
                                if silent:
                                    return 0
                                raise ValueError('Invalid timescale')
                            return duration / timescale
                    moov_offset += child_size
            offset += size
        if silent:
            return 0
        raise ValueError('No mvhd found!')
    except Exception:
        if silent:
            return 0
        raise


def _ensure_ffmpeg():
    try:
        if not os.path.exists(ff.FFMPEG):
            raise RuntimeError(f'Binary tidak ditemukan di {ff.FFMPEG}')
    except Exception as err:
        raise RuntimeError(f'ffmpeg tidak tersedia (fitur preview video tidak tersedia): {err}')


async def get_mp4_preview(video_buffer, time=None, result='buffer', resize=True, width=300, height=300, silent=True):
    def fail(err):
        if silent:
            return '' if result == 'base64' else b''
        raise err

    try:
        if not _is_buf(video_buffer) or not len(video_buffer):
            return fail(ValueError('videoBuffer tidak valid atau kosong'))
        _ensure_ffmpeg()
        if time is None:
            time = min(get_mp4_duration(video_buffer) * 0.2, 10)
        try:
            output, _ = await ff.run(ff.FFMPEG, [
                '-i', 'pipe:0', '-ss', _str(time), '-vframes', '1', '-vcodec', 'png', '-f', 'image2pipe', 'pipe:1',
            ], input_bytes=bytes(video_buffer))
        except Exception as e:
            return fail(RuntimeError(f'ffmpeg exited with code 1: {e}'))
        if not output:
            return fail(ValueError('Output kosong — cek format atau timestamp video'))
        if resize:
            output = await toolkit_resize(output, width, height)
        return base64.b64encode(bytes(output)).decode() if result == 'base64' else output
    except Exception as err:
        return fail(err)


def extract_ie(text, extract=True, hyperlink=True, citation=True, latex=True):
    if not extract:
        return {'text': text, 'ie': [], 'inline_entities': []}

    def create_ie(kind, ie):
        if kind == 'hyperlink':
            return {'key': ie['key'], 'metadata': {
                'display_name': ie['text'], 'is_trusted': ie['is_trusted'], 'url': ie['url'],
                '__typename': 'GenAIInlineLinkItem',
            }}
        if kind == 'citation':
            return {'key': ie['key'], 'metadata': {
                'reference_id': ie['reference_id'], 'reference_url': ie['url'], 'reference_title': ie['url'],
                'reference_display_name': ie['url'], 'sources': [], '__typename': 'GenAISearchCitationItem',
            }}
        if kind == 'latex':
            return {'key': ie['key'], 'metadata': {
                'latex_expression': ie['text'],
                'latex_image': {
                    'url': ie['url'],
                    'width': _or(_num(ie['width']), 100),
                    'height': _or(_num(ie['height']), 100),
                },
                'font_height': _or(_num(ie['font_height']), 83.333333333333),
                'padding': _or(_num(ie['padding']), 15),
                '__typename': 'GenAILatexItem',
            }}
        return None

    ie = []
    inline_entities = []
    result = ''
    last = 0
    citation_index = 1
    hyperlink_index = 0
    latex_index = 0
    stack = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == '[' and _ch(text, i - 1) != '\\':
            stack.append(i)
        elif ch == ']' and _ch(text, i + 1) in ('(', '<'):
            start = stack.pop() if stack else None
            if start is None:
                i += 1
                continue
            opener = text[i + 1]
            closer = ')' if opener == '(' else '>'
            kind = 'link' if opener == '(' else 'latex'
            end = i + 2
            depth = 1
            while end < n and depth:
                if text[end] == opener and _ch(text, end - 1) != '\\':
                    depth += 1
                elif text[end] == closer and _ch(text, end - 1) != '\\':
                    depth -= 1
                end += 1
            if depth:
                i += 1
                continue
            raw = text[start + 1:i].strip()
            url = text[i + 2:end - 1].strip()
            if kind == 'latex':
                if not latex:
                    i += 1
                    continue
                parts = raw.split('|')
                txt = parts[0]
                key = f'_LATEX_{latex_index}'
                latex_index += 1
                tag = '{{' + key + '}}' + (txt or 'image') + '{{/' + key + '}}'
                data = {'type': 'latex', 'ie': {
                    'key': key, 'text': txt, 'url': url,
                    'width': _at(parts, 1), 'height': _at(parts, 2),
                    'font_height': _at(parts, 3), 'padding': _at(parts, 4),
                }}
            elif raw:
                if not hyperlink:
                    i += 1
                    continue
                trusted = not url.startswith('!')
                if not trusted:
                    url = url[1:]
                key = f'_HYPERLINK_{hyperlink_index}'
                hyperlink_index += 1
                tag = '{{' + key + '}}' + url + '{{/' + key + '}}'
                data = {'type': 'hyperlink', 'ie': {'key': key, 'text': raw, 'url': url, 'is_trusted': trusted}}
            else:
                if not citation:
                    i += 1
                    continue
                key = f'_CITATION_{citation_index - 1}'
                tag = '{{' + key + '}}' + url + '{{/' + key + '}}'
                data = {'type': 'citation', 'ie': {'reference_id': citation_index, 'key': key, 'text': '', 'url': url}}
                citation_index += 1
            result += text[last:start] + tag
            last = end
            ie.append(data)
            entity = create_ie(data['type'], data['ie'])
            if entity:
                inline_entities.append(entity)
            i = end
            continue
        i += 1
    result += text[last:]
    return {'text': result, 'ie': ie, 'inline_entities': inline_entities}


KEYWORDS_MAP = {
    'javascript': frozenset(['break', 'case', 'catch', 'continue', 'debugger', 'delete', 'do', 'else', 'finally', 'for', 'function', 'if', 'in', 'instanceof', 'new', 'return', 'switch', 'this', 'throw', 'typeof', 'var', 'void', 'while', 'with', 'true', 'false', 'null', 'undefined', 'class', 'const', 'let', 'super', 'extends', 'export', 'import', 'yield', 'static', 'constructor', 'async', 'await', 'get', 'set']),
    'typescript': frozenset(['abstract', 'any', 'as', 'asserts', 'bigint', 'boolean', 'declare', 'enum', 'implements', 'infer', 'interface', 'is', 'keyof', 'module', 'namespace', 'never', 'readonly', 'require', 'number', 'object', 'override', 'private', 'protected', 'public', 'satisfies', 'string', 'symbol', 'type', 'unknown', 'using', 'from', 'break', 'case', 'catch', 'continue', 'do', 'else', 'finally', 'for', 'function', 'if', 'new', 'return', 'switch', 'this', 'throw', 'try', 'var', 'void', 'while', 'class', 'const', 'let', 'extends', 'import', 'export', 'async', 'await']),
    'python': frozenset(['False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try', 'while', 'with', 'yield']),
    'java': frozenset(['abstract', 'assert', 'boolean', 'break', 'byte', 'case', 'catch', 'char', 'class', 'const', 'continue', 'default', 'do', 'double', 'else', 'enum', 'extends', 'final', 'finally', 'float', 'for', 'goto', 'if', 'implements', 'import', 'instanceof', 'int', 'interface', 'long', 'native', 'new', 'package', 'private', 'protected', 'public', 'return', 'short', 'static', 'strictfp', 'super', 'switch', 'synchronized', 'this', 'throw', 'throws', 'transient', 'try', 'void', 'volatile', 'while']),
    'golang': frozenset(['break', 'case', 'chan', 'const', 'continue', 'default', 'defer', 'else', 'fallthrough', 'for', 'func', 'go', 'goto', 'if', 'import', 'interface', 'map', 'package', 'range', 'return', 'select', 'struct', 'switch', 'type', 'var']),
    'c': frozenset(['auto', 'break', 'case', 'char', 'const', 'continue', 'default', 'do', 'double', 'else', 'enum', 'extern', 'float', 'for', 'goto', 'if', 'int', 'long', 'register', 'return', 'short', 'signed', 'sizeof', 'static', 'struct', 'switch', 'typedef', 'union', 'unsigned', 'void', 'volatile', 'while']),
    'cpp': frozenset(['alignas', 'alignof', 'and', 'auto', 'bool', 'break', 'case', 'catch', 'class', 'const', 'constexpr', 'continue', 'delete', 'do', 'double', 'else', 'enum', 'explicit', 'export', 'extern', 'false', 'float', 'for', 'friend', 'if', 'inline', 'int', 'long', 'mutable', 'namespace', 'new', 'noexcept', 'nullptr', 'operator', 'private', 'protected', 'public', 'return', 'short', 'signed', 'sizeof', 'static', 'struct', 'switch', 'template', 'this', 'throw', 'true', 'try', 'typedef', 'typename', 'union', 'unsigned', 'using', 'virtual', 'void', 'while']),
    'php': frozenset(['abstract', 'and', 'array', 'as', 'break', 'callable', 'case', 'catch', 'class', 'clone', 'const', 'continue', 'declare', 'default', 'do', 'echo', 'else', 'elseif', 'empty', 'enddeclare', 'endfor', 'endforeach', 'endif', 'endswitch', 'endwhile', 'extends', 'final', 'finally', 'fn', 'for', 'foreach', 'function', 'global', 'goto', 'if', 'implements', 'include', 'include_once', 'instanceof', 'interface', 'match', 'namespace', 'new', 'null', 'or', 'private', 'protected', 'public', 'require', 'require_once', 'return', 'static', 'switch', 'throw', 'trait', 'try', 'use', 'var', 'while', 'yield']),
    'rust': frozenset(['as', 'break', 'const', 'continue', 'crate', 'else', 'enum', 'extern', 'false', 'fn', 'for', 'if', 'impl', 'in', 'let', 'loop', 'match', 'mod', 'move', 'mut', 'pub', 'ref', 'return', 'self', 'Self', 'static', 'struct', 'super', 'trait', 'true', 'type', 'unsafe', 'use', 'where', 'while']),
    'html': frozenset(['html', 'head', 'body', 'div', 'span', 'p', 'a', 'img', 'video', 'audio', 'script', 'style', 'link', 'meta', 'form', 'input', 'button', 'table', 'tr', 'td', 'th', 'ul', 'ol', 'li', 'section', 'article', 'header', 'footer', 'nav', 'main']),
    'bash': frozenset(['if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'do', 'done', 'case', 'esac', 'function', 'in', 'select', 'until', 'break', 'continue', 'return', 'export', 'readonly', 'local', 'declare']),
    'markdown': frozenset(['#', '##', '###', '####', '#####', '######']),
}

_TOKEN_TYPE_MAP = {0: 'DEFAULT', 1: 'KEYWORD', 2: 'METHOD', 3: 'STR', 4: 'NUMBER', 5: 'COMMENT'}
_ASCII_ID_START = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$')
_ASCII_ID = _ASCII_ID_START | frozenset('0123456789')
_ASCII_ID_CSS = _ASCII_ID | frozenset('-')
_ASCII_ID_HTML = _ASCII_ID | frozenset(':-')
_DIGIT_CONT = frozenset('0123456789._')


def tokenizer(code, lang='javascript'):
    if not _truthy(lang) or lang in ('txt', 'text', 'plaintext'):
        return {
            'codeBlock': [{'codeContent': code, 'highlightType': 0}],
            'unified_codeBlock': [{'content': code, 'type': 'DEFAULT'}],
        }
    lower = lang.lower()
    keywords = KEYWORDS_MAP.get(lower, frozenset())
    if lower == 'css':
        id_chars = _ASCII_ID_CSS
    elif lower == 'html':
        id_chars = _ASCII_ID_HTML
    else:
        id_chars = _ASCII_ID
    tokens = []

    def push(content, kind):
        if not content:
            return
        if tokens and tokens[-1]['highlightType'] == kind:
            tokens[-1]['codeContent'] += content
        else:
            tokens.append({'codeContent': content, 'highlightType': kind})

    n = len(code)
    i = 0
    while i < n:
        c = code[i]
        if _WS_RE.match(c):
            s = i
            while i < n and _WS_RE.match(code[i]):
                i += 1
            push(code[s:i], 0)
            continue
        if (c == '/' and _ch(code, i + 1) == '/') or (c == '#' and lang in ('python', 'bash')):
            s = i
            while i < n and code[i] != '\n':
                i += 1
            push(code[s:i], 5)
            continue
        if c in ('"', "'", '`'):
            s = i
            q = c
            i += 1
            while i < n:
                if code[i] == '\\' and i + 1 < n:
                    i += 2
                elif code[i] == q:
                    i += 1
                    break
                else:
                    i += 1
            push(code[s:i], 3)
            continue
        if c in '0123456789':
            s = i
            while i < n and code[i] in _DIGIT_CONT:
                i += 1
            push(code[s:i], 4)
            continue
        if c in _ASCII_ID_START:
            s = i
            while i < n and code[i] in id_chars:
                i += 1
            word = code[s:i]
            kind = 0
            if word in keywords:
                kind = 1
            elif lang == 'css':
                j = i
                while j < n and _WS_RE.match(code[j]):
                    j += 1
                if _ch(code, j) == ':':
                    kind = 1
            elif lang == 'html':
                p = s - 1
                while p >= 0 and _WS_RE.match(code[p]):
                    p -= 1
                if _ch(code, p) == '<' or (_ch(code, p) == '/' and _ch(code, p - 1) == '<'):
                    kind = 1
            if kind == 0:
                j = i
                while j < n and _WS_RE.match(code[j]):
                    j += 1
                if _ch(code, j) == '(':
                    kind = 2
            push(word, kind)
            continue
        push(c, 0)
        i += 1
    return {
        'codeBlock': tokens,
        'unified_codeBlock': [{'content': t['codeContent'], 'type': _TOKEN_TYPE_MAP[t['highlightType']]} for t in tokens],
    }


def to_table_metadata(arr, hyperlink=True, citation=True, latex=True):
    if not isinstance(arr, list) or not all(
        isinstance(row, list) and all(_is_str(cell) for cell in row) for row in arr
    ):
        raise TypeError('Table must be a nested array of strings')
    if not arr:
        raise TypeError("Cannot read properties of undefined (reading 'length')")
    header, rows = arr[0], arr[1:]
    max_len = max([len(header)] + [len(r) for r in rows])

    def normalize(r):
        return list(r) + [''] * (max_len - len(r))

    raw_rows = [{'is_header': True, 'cells': normalize(header)}]
    raw_rows += [{'is_header': False, 'cells': normalize(r)} for r in rows]
    unified_rows = []
    for row in raw_rows:
        markdown_cells = []
        for cell in row['cells']:
            extracted = extract_ie(cell, True, hyperlink, citation, latex)
            md = {'text': extracted['text']}
            if extracted['inline_entities']:
                md['inline_entities'] = extracted['inline_entities']
            markdown_cells.append(md)
        out = {'is_header': row['is_header'], 'cells': row['cells']}
        if any(c.get('inline_entities') for c in markdown_cells):
            out['markdown_cells'] = markdown_cells
        unified_rows.append(out)
    rows_meta = []
    for r in unified_rows:
        item = {'items': r['cells']}
        if r['is_header']:
            item['isHeading'] = True
        rows_meta.append(item)
    return {'title': '', 'rows': rows_meta, 'unified_rows': unified_rows}


def new_layout(name, data, extra=None):
    out = dict(extra) if extra else {}
    out['view_model'] = {
        ('primitives' if isinstance(data, list) else 'primitive'): data,
        '__typename': f'GenAI{name}LayoutViewModel',
    }
    return out


class BaseBuilder:
    def __init__(self):
        self._title = ''
        self._subtitle = ''
        self._body = ''
        self._footer = ''
        self._contextInfo = {}
        self._extraPayload = {}

    def setTitle(self, title):
        if not _is_str(title):
            raise TypeError('Title must be a string')
        self._title = title
        return self

    def setSubtitle(self, subtitle):
        if not _is_str(subtitle):
            raise TypeError('Subtitle must be a string')
        self._subtitle = subtitle
        return self

    def setBody(self, body):
        if not _is_str(body):
            raise TypeError('Body must be a string')
        self._body = body
        return self

    def setFooter(self, footer):
        if not _is_str(footer):
            raise TypeError('Footer must be a string')
        self._footer = footer
        return self

    def setContextInfo(self, obj):
        if not isinstance(obj, dict):
            raise TypeError('ContextInfo must be a plain object')
        self._contextInfo = obj
        return self

    def addPayload(self, obj):
        if not isinstance(obj, dict):
            raise TypeError('Payload must be a plain object')
        self._extraPayload.update(obj)
        return self


def _plain_items(value, message):
    items = value if isinstance(value, list) else [value]
    for item in items:
        if not isinstance(item, dict):
            raise TypeError(message)
    return items


class AIRich(BaseBuilder):
    def __init__(self, client):
        if not client:
            raise RuntimeError('Socket is required')
        super().__init__()
        self._client = client
        self._contextInfo = {}
        self._submessages = []
        self._sections = []
        self._richResponseSources = []
        self._embeddedScreens = []
        self._lastMessageKey = None

    def addSubmessage(self, submessage):
        self._submessages.extend(_plain_items(submessage, 'Submessage must be a plain object or array of plain objects'))
        return self

    def addSection(self, section):
        self._sections.extend(_plain_items(section, 'Section must be a plain object or array of plain objects'))
        return self

    def addText(self, text, options=None):
        if not _is_str(text):
            raise TypeError('Text must be a string')
        o = options or {}
        ex = extract_ie(text, True, _nn(o.get('hyperlink'), True), _nn(o.get('citation'), True), _nn(o.get('latex'), True))
        extracted = ex['text']
        self._submessages.append({'messageType': 2, 'messageText': extracted})
        layout = {'text': extracted}
        if ex['inline_entities']:
            layout['inline_entities'] = ex['inline_entities']
        layout['__typename'] = 'GenAIMarkdownTextUXPrimitive'
        self._sections.append(new_layout('Single', layout))
        return self

    def addCode(self, language, code):
        if not _is_str(language) or not _is_str(code):
            raise TypeError('Language and code must be a string')
        meta = tokenizer(code, language)
        self._submessages.append({
            'messageType': 5,
            'codeMetadata': {'codeLanguage': language, 'codeBlocks': meta['codeBlock']},
        })
        self._sections.append(new_layout('Single', {
            'language': language,
            'code_blocks': meta['unified_codeBlock'],
            '__typename': 'GenAICodeUXPrimitive',
        }))
        return self

    def addTable(self, table, options=None):
        if not isinstance(table, list):
            raise TypeError('Table must be an array')
        o = options or {}
        meta = to_table_metadata(table, _nn(o.get('hyperlink'), True), _nn(o.get('citation'), True), _nn(o.get('latex'), True))
        self._submessages.append({
            'messageType': 4,
            'tableMetadata': {'title': meta['title'], 'rows': meta['rows']},
        })
        self._sections.append(new_layout('Single', {'rows': meta['unified_rows'], '__typename': 'GenATableUXPrimitive'}))
        return self

    def addHtml(self, *args):
        args = list(args)
        options = {}
        if args and isinstance(args[-1], dict):
            options = args.pop()
        server_opt = _get(_get(G, 'opts'), 'server')
        default_server = server_opt if _is_str(server_opt) else ''
        url = _nn(options.get('url'), default_server)
        trusted_sources = _nn(
            options.get('trustedSources'),
            [re.sub(r'^https?://', '', default_server)] if _truthy(default_server) else [],
        )
        items = args
        if len(items) == 1 and _is_str(items[0]):
            payload = items[0]
            self._submessages.append({'messageType': 2, 'messageText': '[ CANNOT_LOAD_HTML ]'})
            self._sections.append(new_layout('Single', {
                'payload': payload,
                'url': url,
                'trusted_sources': trusted_sources,
                '__typename': 'GenAIaeacdsnwHtmlPrimitive',
            }))
            return self
        if not all(isinstance(item, list) and len(item) > 0 and _is_str(item[0]) for item in items):
            raise TypeError('addHtml() expects either a single HTML string, or one or more [html, title] pairs, with an optional trailing options object')
        if not self._embeddedScreens:
            self._embeddedScreens.append({
                'title': 'Preview',
                'content': [{'__typename': 'FOAIDNixelButtonSheets', 'tabs': []}],
            })
        tabs = self._embeddedScreens[0]['content'][0]['tabs']
        self._submessages.append({'messageType': 2, 'messageText': '[ CANNOT_LOAD_HTML ]'})
        for item in items:
            payload = item[0]
            title = _at(item, 1)
            tab_id = f'tab_{len(tabs)}'
            header = _nn(title, f'Tab {len(tabs) + 1}')
            tabs.append({
                'id': tab_id,
                'tab_header': header,
                'sections': [new_layout(
                    'Single',
                    {
                        'payload': payload,
                        'url': url,
                        'trusted_sources': trusted_sources,
                        '__typename': 'GenAIaeacdsnwHtmlPrimitive',
                    },
                    {'__typename': 'GenAIUnifiedResponseSection'},
                )],
                'step_entries': [],
            })
        return self

    def addProcess(self, title):
        if not _is_str(title):
            raise TypeError('Process title must be a string')
        self._submessages.append({'messageType': 2, 'messageText': title})
        self._sections.append(new_layout('Single', {
            'icon': NULL,
            'is_in_progress': True,
            'meta_search_apps': NULL,
            'target_secondary_screen_id': NULL,
            'target_secondary_screen_tab_id': NULL,
            'title': title,
            '__typename': 'GenAIBotProgressStatusPrimitive',
        }))
        return self

    def addSource(self, sources=None):
        if sources is None:
            sources = []
        is_list = isinstance(sources, list)
        all_str = is_list and all(_is_str(x) for x in sources)
        all_str_lists = is_list and all(isinstance(x, list) and all(_is_str(v) for v in x) for x in sources)
        if not (is_list and (all_str or all_str_lists)):
            raise TypeError('Sources must be a string array or an array of string arrays')
        if all_str:
            sources = [sources]
        items = []
        for entry in sources:
            icon = _nn(_at(entry, 0), '')
            url = _nn(_at(entry, 1), '')
            text = _nn(_at(entry, 2), '')
            items.append({
                'source_type': 'THIRD_PARTY',
                'source_display_name': text,
                'source_subtitle': 'AI',
                'source_url': url,
                'favicon': {
                    'url': _promise(resolve_media(self._client, icon, 'image')),
                    'mime_type': 'image/jpeg',
                    'width': 16,
                    'height': 16,
                },
            })
        self._sections.append(new_layout('Single', {'sources': items, '__typename': 'GenAISearchResultPrimitive'}))
        return self

    def addReels(self, reels_items=None):
        if reels_items is None:
            reels_items = []
        is_obj = isinstance(reels_items, dict)
        is_list = isinstance(reels_items, list) and all(isinstance(x, dict) for x in reels_items)
        if not (is_obj or is_list):
            raise TypeError('Reels items must be an object or an array of objects')
        items = reels_items if isinstance(reels_items, list) else [reels_items]
        reels = []
        for item in items:
            r = dict(item)
            r['_avatar'] = _promise(resolve_media(self._client, _nn(
                item.get('profileIconUrl'), item.get('profile_url'), item.get('profile'), ''), 'image'))
            r['_thumbnail'] = _promise(resolve_media(self._client, _nn(
                item.get('thumbnailUrl'), item.get('thumbnail'), ''), 'image'))
            reels.append(r)
        self._submessages.append({
            'messageType': 9,
            'contentItemsMetadata': {
                'contentType': 1,
                'itemsMetadata': [{
                    'reelItem': {
                        'title': _nn(r.get('username'), ''),
                        'profileIconUrl': r['_avatar'],
                        'thumbnailUrl': r['_thumbnail'],
                        'videoUrl': _nn(r.get('videoUrl'), r.get('url'), ''),
                    },
                } for r in reels],
            },
        })
        for idx, r in enumerate(reels):
            self._richResponseSources.append({
                'provider': '',
                'thumbnailCdnUrl': r['_thumbnail'],
                'sourceProviderUrl': _nn(r.get('videoUrl'), r.get('url'), ''),
                'sourceQuery': '',
                'faviconCdnUrl': r['_avatar'],
                'citationNumber': idx + 1,
                'sourceTitle': _nn(r.get('username'), ''),
            })
        self._sections.append(new_layout('HScroll', [{
            'reels_url': _nn(r.get('videoUrl'), r.get('url'), ''),
            'thumbnail_url': r['_thumbnail'],
            'creator': _nn(r.get('username'), r.get('title'), ''),
            'avatar_url': r['_avatar'],
            'reels_title': _nn(r.get('reels_title'), r.get('title'), ''),
            'likes_count': _nn(r.get('likes_count'), r.get('like'), 0),
            'shares_count': _nn(r.get('shares_count'), r.get('share'), 0),
            'view_count': _nn(r.get('view_count'), r.get('view'), 0),
            'reel_source': _nn(r.get('reel_source'), r.get('source'), 'IG'),
            'is_verified': _truthy(_or(r.get('is_verified'), r.get('verified'))),
            '__typename': 'GenAIReelPrimitive',
        } for r in reels]))
        return self

    def addImage(self, image_url, options=None):
        o = options or {}
        resolve_url = _truthy(o.get('resolveUrl'))
        ok = _is_str(image_url) or _is_buf(image_url) or (
            isinstance(image_url, list) and all(_is_str(v) or _is_buf(v) for v in image_url))
        if not ok:
            raise TypeError('imageUrl must be string | buffer | array of string/buffer')
        values = image_url if isinstance(image_url, list) else [image_url]
        lst = []
        for v in values:
            url = _promise(resolve_media(self._client, v, 'image', resolve_url=resolve_url))
            lst.append({'imagePreviewUrl': url, 'imageHighResUrl': url, 'sourceUrl': url})
        self._submessages.append({
            'messageType': 1,
            'gridImageMetadata': {
                'gridImageUrl': {'imagePreviewUrl': _get(_at(lst, 0), 'imagePreviewUrl')},
                'imageUrls': lst,
            },
        })
        for item in lst:
            self._sections.append(new_layout('Single', {
                'media': {'url': item['imagePreviewUrl'], 'mime_type': 'image/png'},
                'imagine_type': 'IMAGE',
                'status': {'status': 'READY'},
                '__typename': 'GenAIImaginePrimitive',
            }))
        return self

    def addVideo(self, video_url, options=None):
        o = options or {}
        auto_fill = _nn(o.get('autoFill'), True)

        def is_object_video(v):
            return isinstance(v, dict) and _truthy(v.get('url'))

        valid = _is_str(video_url) or _is_buf(video_url) or is_object_video(video_url) or (
            isinstance(video_url, list) and all(_is_str(v) or _is_buf(v) or is_object_video(v) for v in video_url))
        if not valid:
            raise TypeError('videoUrl must be string | buffer | object | array')
        items = video_url if isinstance(video_url, list) else [video_url]
        self._submessages.append({'messageType': 2, 'messageText': '[ CANNOT_LOAD_VIDEO ]'})
        for item in items:
            is_obj = is_object_video(item)
            if is_obj:
                url = _promise(resolve_media(self._client, _nn(item.get('url'), ''), 'video'))
            else:
                url = _promise(resolve_media(self._client, item, 'video'))
            buffer_promise = _promise(_then(url, lambda u: fetch_buffer(u))) if auto_fill else None
            if is_obj and item.get('file_length') is not None:
                file_length = item.get('file_length')
            elif auto_fill:
                file_length = _promise(_then(buffer_promise, lambda b: len(b) if b is not None else 0))
            else:
                file_length = 0
            if is_obj and item.get('duration') is not None:
                duration = item.get('duration')
            elif auto_fill:
                duration = _promise(_then(buffer_promise, lambda b: get_mp4_duration(b, silent=True)))
            else:
                duration = 0
            if is_obj and _truthy(item.get('thumbnail')):
                thumbnail = _promise(resolve_media(
                    self._client, item.get('thumbnail'), 'image', result='base64', resize=True, width=300, height=300))
            elif auto_fill:
                thumbnail = _promise(_then(buffer_promise, lambda b: get_mp4_preview(b, time=0, result='base64')))
            else:
                thumbnail = NULL
            self._sections.append(new_layout('Single', {
                'media': {
                    'url': url,
                    'mime_type': _nn(item.get('mime_type'), 'video/mp4') if is_obj else 'video/mp4',
                    'file_length': file_length,
                    'duration': duration,
                },
                'imagine_type': 'ANIMATE',
                'status': {'status': 'READY'},
                'thumbnail': {'raw_media': thumbnail},
                '__typename': 'GenAIImaginePrimitive',
            }))
        return self

    def addProduct(self, data=None):
        if data is None:
            data = {}
        is_obj = isinstance(data, dict)
        is_list = isinstance(data, list) and all(isinstance(x, dict) for x in data)
        if not (is_obj or is_list):
            raise TypeError('Product items must be an object or an array of objects')
        self._submessages.append({'messageType': 2, 'messageText': '[ CANNOT_LOAD_PRODUCT ]'})
        items = data if isinstance(data, list) else [data]
        product = []
        for item in items:
            product.append({
                'title': item.get('title'),
                'brand': item.get('brand'),
                'price': item.get('price'),
                'sale_price': item.get('sale_price'),
                'product_url': _nn(item.get('product_url'), item.get('url')),
                'image': {'url': _promise(resolve_media(self._client, _nn(item.get('image_url'), item.get('image')), 'image'))},
                'additional_images': [{'url': _promise(resolve_media(self._client, _nn(item.get('icon_url'), item.get('icon')), 'image'))}],
                '__typename': 'GenAIProductItemCardPrimitive',
            })
        if isinstance(data, list):
            self._sections.append(new_layout('HScroll', product))
        else:
            self._sections.append(new_layout('Single', _at(product, 0)))
        return self

    def addPost(self, data=None):
        if data is None:
            data = {}
        is_obj = isinstance(data, dict)
        is_list = isinstance(data, list) and all(isinstance(x, dict) for x in data)
        if not (is_obj or is_list):
            raise TypeError('Post items must be an object or an array of objects')
        posts = data if isinstance(data, list) else [data]
        self._submessages.append({'messageType': 2, 'messageText': '[ CANNOT_LOAD_POST ]'})
        primitives = []
        for p in posts:
            primitives.append({
                'title': _nn(p.get('title'), ''),
                'subtitle': _nn(p.get('subtitle'), ''),
                'username': _nn(p.get('username'), ''),
                'profile_picture_url': _promise(resolve_media(self._client, _nn(
                    p.get('profile_picture_url'), p.get('profile_url'), p.get('profile'), ''), 'image')),
                'is_verified': _truthy(_or(p.get('is_verified'), p.get('verified'))),
                'thumbnail_url': _promise(resolve_media(self._client, _nn(p.get('thumbnail_url'), p.get('thumbnail'), ''), 'image')),
                'post_caption': _nn(p.get('post_caption'), p.get('caption'), ''),
                'likes_count': _nn(p.get('likes_count'), p.get('like'), 0),
                'comments_count': _nn(p.get('comments_count'), p.get('comment'), 0),
                'shares_count': _nn(p.get('shares_count'), p.get('share'), 0),
                'post_url': _nn(p.get('post_url'), p.get('url'), ''),
                'post_deeplink': _nn(p.get('post_deeplink'), p.get('deeplink'), ''),
                'source_app': _or(p.get('source_app'), p.get('source'), 'INSTAGRAM'),
                'footer_label': _nn(p.get('footer_label'), p.get('footer'), ''),
                'footer_icon': _promise(resolve_media(self._client, _nn(p.get('footer_icon'), p.get('icon'), ''), 'image')),
                'is_carousel': len(posts) > 1,
                'orientation': _nn(p.get('orientation'), 'LANDSCAPE'),
                'post_type': _nn(p.get('post_type'), 'VIDEO'),
                '__typename': 'GenAIPostPrimitive',
            })
        self._sections.append(new_layout('HScroll', primitives))
        return self

    def addTip(self, text):
        self._submessages.append({'messageType': 2, 'messageText': text})
        self._sections.append(new_layout('Single', {'text': text, '__typename': 'GenAIMetadataTextPrimitive'}))
        return self

    def addSuggest(self, suggestion, options=None):
        o = options or {}
        if not (_is_str(suggestion) or (isinstance(suggestion, list) and all(_is_str(v) for v in suggestion))):
            raise TypeError('Suggestion must be a string or array of strings')

        def pill(text):
            return {'prompt_text': text, 'prompt_type': 'SUGGESTED_PROMPT', '__typename': 'GenAIFollowUpSuggestionPillPrimitive'}

        if isinstance(suggestion, list):
            suggest = [pill(t) for t in suggestion]
        else:
            suggest = [pill(suggestion)]
        scroll = _nn(o.get('scroll'), True)
        kind = _nn(o.get('layout'), 'Single' if len(suggest) == 1 else ('HScroll' if scroll else 'ActionRow'))
        self._sections.append(new_layout(
            kind,
            _at(suggest, 0) if kind == 'Single' else suggest,
            {'__typename': 'GenAIUnifiedResponseSection'},
        ))
        return self

    async def build(self, options=None):
        o = options or {}
        forwarded = _nn(o.get('forwarded'), True)
        notification = _nn(o.get('notification'), False)
        includes_unified = _nn(o.get('includesUnifiedResponse'), True)
        includes_submessages = _nn(o.get('includesSubmessages'), True)
        quoted = o.get('quoted')
        quoted_participant = o.get('quotedParticipant')
        forward = {
            'forwardingScore': 1,
            'isForwarded': True,
            'forwardedAiBotMessageInfo': {'botJid': '0@bot'},
            'forwardOrigin': 4,
        } if _truthy(forwarded) else {}
        notif = {
            'sessionTransparencyMetadata': {
                'disclaimerText': '~ Ahmad tumbuh kembang',
                'hcaId': f'hca_{int(time.time() * 1000)}',
                'sessionTransparencyType': 1,
            },
        } if _truthy(notification) else {}
        if _truthy(quoted):
            key = _get(quoted, 'key')
            qobj = {
                'stanzaId': _or(_get(key, 'id'), _get(quoted, 'id')),
                'participant': _or(quoted_participant, _get(key, 'participant'), _get(key, 'remoteJid')),
                'quotedType': 0,
                'quotedMessage': None if _is_str(quoted) else _nn(_get(quoted, 'message'), quoted),
            }
        else:
            qobj = {}
        sections = list(await wait_all_promises(self._sections))
        if _truthy(self._footer):
            sections.append(new_layout('Single', {'text': self._footer, '__typename': 'GenAIMetadataTextPrimitive'}))
        response_data = {'response_id': str(uuid.uuid4()), 'sections': sections}
        if self._embeddedScreens:
            response_data['embedded_screens'] = self._embeddedScreens
        has_renderable = len(sections) > 0 or len(self._embeddedScreens) > 0
        submessages = (await wait_all_promises(self._submessages)) if includes_submessages else []
        rich = {
            'messageType': 1,
            'submessages': submessages,
            'contextInfo': {**forward, **qobj, **self._contextInfo},
        }
        if includes_unified and has_renderable:
            rich['unifiedResponse'] = {'data': base64.b64encode(js_json(response_data).encode('utf-8')).decode()}
        sources = await wait_all_promises(self._richResponseSources)
        return {
            'messageContextInfo': {
                'deviceListMetadata': {},
                'deviceListMetadataVersion': 2,
                'botMetadata': {
                    'messageDisclaimerText': self._title,
                    'richResponseSourcesMetadata': {'sources': sources},
                    **notif,
                },
            },
            **self._extraPayload,
            'botForwardedMessage': {'message': {'richResponseMessage': rich}},
        }

    async def buildEdit(self, target_jid, target_id, options=None):
        o = dict(options or {})
        msg = o.pop('msg', None)
        message_id = o.pop('messageId', None)
        if not _truthy(msg):
            built = await self.build({'forwardWrapper': True, **o})
            msg = _get(_get(built, 'botForwardedMessage'), 'message')
        if not _truthy(msg):
            raise RuntimeError('buildEdit: msg tidak mengandung rich response')
        protocol_message = {
            'key': {'remoteJid': target_jid, 'fromMe': True, 'id': target_id},
            'type': 14,
            'editedMessage': msg,
        }
        return generateWAMessageFromContent(
            target_jid,
            {'botForwardedMessage': {'message': {'protocolMessage': protocol_message}}},
            {'messageId': _or(message_id, generateMessageIDV2(None)), **o},
        )

    async def sendEdit(self, jid=None, msg_id=None, options=None):
        o = dict(options or {})
        msg = o.pop('msg', None)
        message_id = o.pop('messageId', None)
        additional_nodes = _nn(o.pop('additionalNodes', None), [])
        jid = _nn(jid, _get(self._lastMessageKey, 'remoteJid'))
        msg_id = _nn(msg_id, _get(self._lastMessageKey, 'id'))
        if not _truthy(jid):
            raise RuntimeError('JID diperlukan')
        if not _truthy(msg_id):
            raise RuntimeError('Message id diperlukan')
        msg_edit = await self.buildEdit(jid, msg_id, {
            'msg': msg,
            'messageId': _or(message_id, generateMessageIDV2(None)),
            **o,
        })
        await maybe_await(_attr(self._client, 'relayMessage')(jid, msg_edit['message'], {
            'messageId': msg_edit['key']['id'],
            'additionalNodes': additional_nodes,
        }))
        return msg_edit

    async def send(self, jid, options=None):
        o = dict(options or {})
        forwarded = o.pop('forwarded', None)
        notification = o.pop('notification', None)
        includes_unified = _nn(o.pop('includesUnifiedResponse', None), True)
        includes_submessages = o.pop('includesSubmessages', None)
        message_id = o.pop('messageId', None)
        additional_nodes = _nn(o.pop('additionalNodes', None), [])
        msg = await self.build({
            'forwarded': forwarded,
            'notification': notification,
            'includesUnifiedResponse': includes_unified,
            'includesSubmessages': includes_submessages,
            **o,
        })
        relayed = await maybe_await(_attr(self._client, 'relayMessage')(jid, msg, {
            'messageId': message_id,
            'additionalNodes': additional_nodes,
            **o,
        }))
        self._lastMessageKey = {'remoteJid': jid, 'id': _or(message_id, relayed)}
        if includes_unified:
            try:
                await self.sendEdit(jid, self._lastMessageKey['id'], {'msg': msg})
            except Exception as e:
                e.relayedKey = self._lastMessageKey
                raise
        return relayed

    @staticmethod
    def tokenizer(code, lang='javascript'):
        return tokenizer(code, lang)

    @staticmethod
    def toTableMetadata(arr, options=None):
        o = options or {}
        return to_table_metadata(arr, _nn(o.get('hyperlink'), True), _nn(o.get('citation'), True), _nn(o.get('latex'), True))

    @staticmethod
    def newLayout(name, data, extra=None):
        return new_layout(name, data, extra)


ICON_ASCII_MAP = {
    'accountCircle': '[user]', 'add': '[+]', 'arrowBack': '[<]', 'arrowForward': '[>]', 'attachFile': '[attach]',
    'calendarToday': '[date]', 'call': '[call]', 'camera': '[camera]', 'check': '[ok]', 'close': '[x]', 'delete': '[delete]',
    'download': '[download]', 'edit': '[edit]', 'event': '[event]', 'error': '[!]', 'fastForward': '[>>]', 'favorite': '[fav]',
    'favoriteOff': '[fav]', 'folder': '[folder]', 'help': '[?]', 'home': '[home]', 'info': '[i]', 'locationOn': '[location]',
    'lock': '[lock]', 'lockOpen': '[unlock]', 'mail': '[mail]', 'menu': '[menu]', 'moreVert': '[more]', 'moreHoriz': '[more]',
    'notificationsOff': '[muted]', 'notifications': '[alert]', 'pause': '[pause]', 'payment': '[pay]', 'person': '[user]',
    'phone': '[phone]', 'photo': '[photo]', 'play': '[play]', 'print': '[print]', 'refresh': '[refresh]', 'rewind': '[<<]',
    'search': '[search]', 'send': '[send]', 'settings': '[settings]', 'share': '[share]', 'shoppingCart': '[cart]',
    'skipNext': '[next]', 'skipPrevious': '[prev]', 'star': '[star]', 'starHalf': '[star]', 'starOff': '[star]',
    'stop': '[stop]', 'upload': '[upload]', 'visibility': '[show]', 'visibilityOff': '[hide]', 'volumeDown': '[vol-]',
    'volumeMute': '[mute]', 'volumeOff': '[mute]', 'volumeUp': '[vol+]', 'warning': '[!]',
}

ICON_SYMBOL_MAP = {
    'accountCircle': '☺', 'add': '+', 'arrowBack': '<', 'arrowForward': '>', 'attachFile': '@',
    'calendarToday': '#', 'call': '☎', 'camera': '◉', 'check': '✓', 'close': '✕', 'delete': '✕',
    'download': '↓', 'edit': '✎', 'event': '#', 'error': '!', 'fastForward': '»', 'favorite': '♥',
    'favoriteOff': '♡', 'folder': '▤', 'help': '?', 'home': '⌂', 'info': 'i', 'locationOn': '⚑',
    'lock': '⚿', 'lockOpen': '⚿', 'mail': '✉', 'menu': '≡', 'moreVert': '⋮', 'moreHoriz': '…',
    'notificationsOff': '⌇', 'notifications': '☏', 'pause': '‖', 'payment': '$', 'person': '☺',
    'phone': '☎', 'photo': '▨', 'play': '▶', 'print': '⎙', 'refresh': '↻', 'rewind': '«',
    'search': '⚲', 'send': '➤', 'settings': '⚙', 'share': '⤴', 'shoppingCart': '⛁',
    'skipNext': '»', 'skipPrevious': '«', 'star': '★', 'starHalf': '★', 'starOff': '☆',
    'stop': '■', 'upload': '↑', 'visibility': '◉', 'visibilityOff': '◎', 'volumeDown': '◑',
    'volumeMute': '◌', 'volumeOff': '◌', 'volumeUp': '◕', 'warning': '!',
}


def _nf_normalize_row(row):
    row = row or {}
    return {
        'header': _str(_or(row.get('header'), '')),
        'title': _str(_or(row.get('title'), row.get('id'), '')),
        'description': _str(_or(row.get('description'), '')),
        'id': _str(_or(row.get('id'), row.get('rowId'), row.get('title'), '')),
    }


def _nf_normalize_sections(sections):
    out = []
    for section in (sections or []):
        entry = {'title': _str(_or(section.get('title'), ''))}
        if _truthy(section.get('highlight_label')):
            entry['highlight_label'] = _str(section.get('highlight_label'))
        entry['rows'] = [_nf_normalize_row(r) for r in (section.get('rows') or [])]
        out.append(entry)
    return out


def _nf_build_payment_button(item):
    p = item.get('payment') or {}
    amount = {
        'value': _num(_nn(p.get('value'), p.get('amount'), 0)),
        'offset': _num(_nn(p.get('offset'), 100)),
    }
    item_name = _str(_or(p.get('itemName'), p.get('item_name'), item.get('text'), 'Pembayaran'))
    order = _or(p.get('order'), {
        'status': 'pending',
        'subtotal': amount,
        'order_type': 'ORDER',
        'items': [{
            'name': item_name,
            'amount': amount,
            'quantity': _num(_or(p.get('quantity'), 1)),
            'sale_amount': amount,
        }],
    })
    default_settings = [{
        'type': 'payment_key',
        'payment_key': {
            'type': _str(_or(p.get('accountType'), p.get('institutionType'), 'IDPAYMENTACCOUNT')),
            'key': _str(_or(p.get('accountKey'), p.get('key'), '')),
            'name': _str(_or(p.get('accountName'), p.get('name'), '')),
            'institution_name': _str(_or(p.get('institution'), p.get('institution_name'), '')),
            'full_name_on_account': _str(_or(p.get('fullName'), p.get('full_name_on_account'), '')),
        },
    }]
    payment_settings = _or(p.get('paymentSettings'), p.get('payment_settings'), default_settings)
    return {
        'name': 'payment_key_info',
        'buttonParamsJson': js_json({
            'currency': _str(_or(p.get('currency'), 'IDR')),
            'total_amount': amount,
            'reference_id': _str(_or(p.get('referenceId'), p.get('reference_id'), f'INV-{int(time.time() * 1000)}')),
            'type': _str(_or(p.get('type'), 'physical-goods')),
            'order': order,
            'payment_settings': payment_settings,
            'share_payment_status': _truthy(p.get('sharePaymentStatus')),
            'is_soft_deleted': False,
            'referral': _str(_or(p.get('referral'), 'chat_attachment')),
        }),
    }


def _nf_build_button(item):
    if _truthy(item.get('payment')):
        return _nf_build_payment_button(item)
    if _truthy(item.get('sections')):
        return {'name': 'single_select', 'buttonParamsJson': js_json({
            'title': _str(_or(item.get('text'), '')),
            'sections': _nf_normalize_sections(item.get('sections')),
        })}
    if _truthy(item.get('call')):
        return {'name': 'cta_call', 'buttonParamsJson': js_json({
            'display_text': _str(_or(item.get('text'), '')),
            'phone_number': _str(item.get('call')),
        })}
    if _truthy(item.get('copy')):
        return {'name': 'cta_copy', 'buttonParamsJson': js_json({
            'display_text': _str(_or(item.get('text'), '')),
            'copy_code': _str(item.get('copy')),
        })}
    if _truthy(item.get('url')):
        return {'name': 'cta_url', 'buttonParamsJson': js_json({
            'display_text': _str(_or(item.get('text'), '')),
            'url': _str(item.get('url')),
            'merchant_url': _str(item.get('url')),
            'webview_interaction': _truthy(item.get('useWebview')),
        })}
    return {'name': 'quick_reply', 'buttonParamsJson': js_json({
        'display_text': _str(_or(item.get('text'), '')),
        'id': _str(_or(item.get('id'), item.get('text'), '')),
    })}


_NF_BUTTON_KEYS = ('text', 'id', 'call', 'copy', 'url', 'sections', 'payment')


def _nf_build_buttons(native_flow=None, option_text=None, option_title=None):
    items = native_flow if isinstance(native_flow, list) else [native_flow]
    items = [i for i in items if isinstance(i, dict) and any(_truthy(i.get(k)) for k in _NF_BUTTON_KEYS)]
    if not items:
        return []
    built = [_nf_build_button(i) for i in items]
    if _truthy(option_title) and _truthy(option_text):
        return built
    return [{}] + built


def _nf_build_message_params(buttons, option_text=None, option_title=None, lto_text=None, lto_url=None,
                             lto_code=None, lto_expiration=None):
    params = {}
    if _truthy(option_title) or _truthy(option_text):
        list_title = _str(_or(option_text, option_title, 'Select'))
        button_title = _str(_or(option_title, option_text, 'Select'))
        named = [i for i, b in enumerate(buttons or []) if isinstance(b, dict) and _truthy(b.get('name'))]
        params['bottom_sheet'] = {
            'in_thread_buttons_limit': 1,
            'divider_indices': named if named else [0],
            'list_title': list_title,
            'button_title': button_title,
        }
    if _truthy(lto_text) or _truthy(lto_url) or _truthy(lto_code):
        lto = {'text': _str(_or(lto_text, ''))}
        if _truthy(lto_url):
            lto['url'] = _str(lto_url)
        if _truthy(lto_code):
            lto['copy_code'] = _str(lto_code)
        if _truthy(lto_expiration):
            lto['expiration_time'] = _num(lto_expiration)
        params['limited_time_offer'] = lto
    return js_json(params) if params else None


def _nf_native_flow_attrs(buttons=None):
    return {'name': 'mixed', 'v': '9'}


def _nf_build_offer(offer_text=None, offer_code=None, offer_url=None, offer_expiration=None):
    if not (_truthy(offer_text) or _truthy(offer_code) or _truthy(offer_url)):
        return None
    out = {'text': _str(_or(offer_text, ''))}
    if _truthy(offer_code):
        out['code'] = _str(offer_code)
    if _truthy(offer_url):
        out['url'] = _str(offer_url)
    if _truthy(offer_expiration):
        out['expiration'] = _num(offer_expiration)
    return out


async def _nf_read_source(source):
    if isinstance(source, dict) and _truthy(source.get('url')):
        source = source.get('url')
    if _is_buf(source):
        return bytes(source)
    if _is_str(source):
        if re.match(r'https?://', source, re.I):
            res = await http.get(source, raise_for_status=False)
            return bytes(res.content)
        with open(source, 'rb') as fh:
            return fh.read()
    return None


def _nf_resolve_thumb(thumb):
    if not _truthy(thumb):
        return None
    if _is_buf(thumb):
        return base64.b64encode(bytes(thumb)).decode()
    if _is_str(thumb):
        return thumb
    return None


async def _nf_build_header(conn, image=None, video=None, document=None, location=None, title='', subtitle='',
                           mimetype=None, file_name=None, file_length=None, jpeg_thumbnail=None):
    upload = _attr(conn, 'waUploadToServer')
    if _truthy(location):
        loc = location if isinstance(location, dict) else {}
        raw_source = _nn(loc.get('buffer'), loc.get('image'))
        thumb = None
        if _truthy(raw_source):
            raw = bytes((await http.get(raw_source, raise_for_status=False)).content) if _is_str(raw_source) else raw_source
            thumb = base64.b64encode(bytes(await _acall(conn, 'resize', raw, 300, 300))).decode()
        location_message = {
            'degreesLatitude': _nn(loc.get('latitude'), 0),
            'degreesLongitude': _nn(loc.get('longitude'), 0),
            'name': _nn(loc.get('name'), ''),
            'address': _nn(loc.get('address'), ''),
            'url': _nn(loc.get('url'), ''),
        }
        if thumb:
            location_message['jpegThumbnail'] = thumb
        return {'title': title, 'subtitle': subtitle, 'hasMediaAttachment': True, 'locationMessage': location_message}
    if _truthy(image):
        buf = await _nf_read_source(image)
        media = await prepareWAMessageMedia({'image': buf}, {'upload': upload})
        if _truthy(mimetype) and _truthy(_get(media, 'imageMessage')):
            media['imageMessage']['mimetype'] = mimetype
        return {'title': title, 'subtitle': subtitle, 'hasMediaAttachment': True, **media}
    if _truthy(video):
        buf = await _nf_read_source(video)
        media = await prepareWAMessageMedia({'video': buf}, {'upload': upload})
        if _truthy(mimetype) and _truthy(_get(media, 'videoMessage')):
            media['videoMessage']['mimetype'] = mimetype
        return {'title': title, 'subtitle': subtitle, 'hasMediaAttachment': True, **media}
    if _truthy(document):
        source = document if isinstance(document, dict) else {'url': document}
        buf = await _nf_read_source(source)
        media = await prepareWAMessageMedia({
            'document': buf,
            'mimetype': _or(mimetype, source.get('mimetype'), 'application/octet-stream'),
            'fileName': _or(file_name, source.get('fileName'), source.get('filename'), 'file'),
        }, {'upload': upload})
        thumb = _nf_resolve_thumb(_or(jpeg_thumbnail, source.get('jpegThumbnail'), source.get('thumbnail')))
        doc = _get(media, 'documentMessage')
        if _truthy(doc):
            if thumb:
                doc['jpegThumbnail'] = thumb
            if _truthy(file_length):
                doc['fileLength'] = _str(file_length)
            if _truthy(file_name):
                doc['fileName'] = file_name
            if _truthy(mimetype):
                doc['mimetype'] = mimetype
        return {'title': title, 'subtitle': subtitle, 'hasMediaAttachment': True, **media}
    return {'title': title, 'subtitle': subtitle, 'hasMediaAttachment': False}


async def _nf_build_interactive(conn, opts):
    o = opts or {}
    header = await _nf_build_header(
        conn,
        image=o.get('image'), video=o.get('video'), document=o.get('document'), location=o.get('location'),
        mimetype=o.get('mimetype'), file_name=o.get('fileName'), file_length=o.get('fileLength'),
        jpeg_thumbnail=o.get('jpegThumbnail'),
    )
    buttons = _nf_build_buttons(_nn(o.get('nativeFlow'), []), o.get('optionText'), o.get('optionTitle'))
    params_json = _nf_build_message_params(
        buttons, o.get('optionText'), o.get('optionTitle'),
        o.get('ltoText'), o.get('ltoUrl'), o.get('ltoCode'), o.get('ltoExpiration'),
    )
    offer = _nf_build_offer(o.get('offerText'), o.get('offerCode'), o.get('offerUrl'), o.get('offerExpiration'))
    mentions = o.get('mentions')
    native = {'buttons': buttons}
    if params_json:
        native['messageParamsJson'] = params_json
    result = {
        'header': header,
        'body': {'text': _str(_or(o.get('caption'), o.get('text'), o.get('body'), ''))},
        'footer': {'text': _str(_or(o.get('footer'), ''))},
        'nativeFlowMessage': native,
    }
    if _truthy(mentions):
        result['contextInfo'] = {'mentionedJid': mentions}
    if offer:
        context_info = {}
        if _truthy(mentions):
            context_info['mentionedJid'] = mentions
        context_info['externalAdReply'] = {
            'title': offer['text'],
            'body': _or(offer.get('code'), ''),
            'thumbnailUrl': offer.get('url'),
            'mediaType': 1,
            'renderLargerThumbnail': False,
        }
        result['contextInfo'] = context_info
    return result


def _nf_build_widget_item(item, item_id, components):
    item = item or {}
    if _truthy(item.get('image')):
        entry = {'id': item_id, 'component': 'Image', 'url': item.get('image'),
                 'variant': _or(item.get('variant'), 'header'), 'fit': _or(item.get('fit'), 'cover')}
        if _truthy(item.get('description')):
            entry['description'] = _str(item.get('description'))
        components.append(entry)
    elif _truthy(item.get('text')):
        components.append({'id': item_id, 'component': 'Text', 'text': _str(item.get('text')),
                           'variant': _or(item.get('variant'), 'body')})
    elif _truthy(item.get('icon')) or _truthy(item.get('name')):
        icon_name = _or(item.get('icon'), item.get('name'))
        if _truthy(item.get('native')):
            components.append({'id': item_id, 'component': 'Icon', 'name': icon_name})
        else:
            symbol = item.get('style') == 'symbol'
            mapping = ICON_SYMBOL_MAP if symbol else ICON_ASCII_MAP
            fallback = '•' if symbol else f'[{_str(icon_name)}]'
            components.append({'id': item_id, 'component': 'Text', 'text': _or(mapping.get(icon_name), fallback),
                               'variant': _or(item.get('variant'), 'body')})
    elif _truthy(item.get('video')):
        components.append({'id': item_id, 'component': 'Video', 'url': item.get('video')})
    elif _truthy(item.get('audio')):
        entry = {'id': item_id, 'component': 'AudioPlayer', 'url': item.get('audio')}
        if _truthy(item.get('description')):
            entry['description'] = _str(item.get('description'))
        components.append(entry)
    elif _truthy(item.get('divider')):
        axis = item.get('divider') if _is_str(item.get('divider')) else 'horizontal'
        components.append({'id': item_id, 'component': 'Divider', 'axis': axis})
    elif _truthy(item.get('button')):
        text_id = f'{item_id}_text'
        components.append({'id': item_id, 'component': 'Button', 'child': text_id,
                           'variant': _or(item.get('variant'), 'primary'),
                           'action': {'call': 'openUrl', 'args': {'url': _str(_or(item.get('url'), ''))}}})
        components.append({'id': text_id, 'component': 'Text', 'text': _str(item.get('button')), 'variant': 'body'})
    elif _truthy(item.get('input')):
        entry = {'id': item_id, 'component': 'TextField', 'label': _str(_or(item.get('label'), item.get('input')))}
        if item.get('value') is not None:
            entry['value'] = _str(item.get('value'))
        entry['variant'] = _or(item.get('variant'), 'shortText')
        if _truthy(item.get('validationRegexp')):
            entry['validationRegexp'] = item.get('validationRegexp')
        components.append(entry)
    elif _truthy(item.get('checkbox')):
        components.append({'id': item_id, 'component': 'CheckBox', 'label': _str(item.get('checkbox')),
                           'value': _truthy(item.get('value'))})
    elif _truthy(item.get('choice')):
        entry = {'id': item_id, 'component': 'ChoicePicker'}
        if _truthy(item.get('label')):
            entry['label'] = _str(item.get('label'))
        options = []
        for o in item.get('choice'):
            if _is_str(o):
                options.append({'label': o, 'value': o})
            else:
                options.append({'label': _str(_get(o, 'label')), 'value': _str(_get(o, 'value'))})
        entry['options'] = options
        val = item.get('value')
        if _truthy(val):
            entry['value'] = [_str(v) for v in val] if isinstance(val, list) else [_str(val)]
        else:
            entry['value'] = []
        entry['variant'] = _or(item.get('variant'), 'mutuallyExclusive')
        if _truthy(item.get('displayStyle')):
            entry['displayStyle'] = item.get('displayStyle')
        if item.get('filterable') is not None:
            entry['filterable'] = _truthy(item.get('filterable'))
        components.append(entry)
    elif item.get('slider') is not None:
        entry = {'id': item_id, 'component': 'Slider'}
        if _truthy(item.get('label')):
            entry['label'] = _str(item.get('label'))
        entry['min'] = _nn(item.get('min'), 0)
        entry['max'] = item.get('slider')
        entry['value'] = _nn(item.get('value'), item.get('min'), 0)
        components.append(entry)
    elif _truthy(item.get('datetime')):
        entry = {
            'id': item_id, 'component': 'DateTimeInput',
            'value': _str(item.get('value')) if item.get('value') is not None else '',
            'enableDate': _truthy(item.get('enableDate')) if item.get('enableDate') is not None else True,
            'enableTime': _truthy(item.get('enableTime')),
        }
        if _truthy(item.get('min')):
            entry['min'] = _str(item.get('min'))
        if _truthy(item.get('max')):
            entry['max'] = _str(item.get('max'))
        if _truthy(item.get('label')):
            entry['label'] = _str(item.get('label'))
        components.append(entry)
    elif _truthy(item.get('row')):
        children = [c for c in (_nf_build_widget_item(sub, f'{item_id}_{j}', components) for j, sub in enumerate(item.get('row'))) if c]
        components.append({'id': item_id, 'component': 'Row', 'children': children,
                           'justify': _or(item.get('justify'), 'start'), 'align': _or(item.get('align'), 'stretch')})
    elif _truthy(item.get('column')):
        children = [c for c in (_nf_build_widget_item(sub, f'{item_id}_{j}', components) for j, sub in enumerate(item.get('column'))) if c]
        components.append({'id': item_id, 'component': 'Column', 'children': children,
                           'justify': _or(item.get('justify'), 'start'), 'align': _or(item.get('align'), 'stretch')})
    elif _truthy(item.get('list')):
        children = [c for c in (_nf_build_widget_item(sub, f'{item_id}_{j}', components) for j, sub in enumerate(item.get('list'))) if c]
        components.append({'id': item_id, 'component': 'List', 'children': children,
                           'direction': _or(item.get('direction'), 'vertical'), 'align': _or(item.get('align'), 'stretch')})
    else:
        return None
    return item_id


def _nf_widget_item_fallback(item):
    item = item or {}
    if _truthy(item.get('image')):
        return '[foto]'
    if _truthy(item.get('text')):
        return _str(item.get('text'))
    if _truthy(item.get('icon')) or _truthy(item.get('name')):
        name = _or(item.get('icon'), item.get('name'))
        return _or(ICON_ASCII_MAP.get(name), f'[{_str(name)}]')
    if _truthy(item.get('video')):
        return '[video]'
    if _truthy(item.get('audio')):
        return '[audio]'
    if _truthy(item.get('button')):
        return f"{_str(item.get('button'))}: {_str(_or(item.get('url'), ''))}"
    if _truthy(item.get('input')):
        return f"{_str(_or(item.get('label'), item.get('input')))}: {_str(_nn(item.get('value'), ''))}"
    if _truthy(item.get('checkbox')):
        return f"[ ] {_str(item.get('checkbox'))}"
    if _truthy(item.get('choice')):
        val = item.get('value')
        vals = val if isinstance(val, list) else [val]
        joined = ', '.join(_str(v) for v in vals if _truthy(v))
        return f"{_str(_or(item.get('label'), ''))} {joined}".strip()
    if item.get('slider') is not None:
        return f"{_str(_or(item.get('label'), 'slider'))}: {_str(_nn(item.get('value'), item.get('min'), 0))}/{_str(item.get('slider'))}"
    if _truthy(item.get('datetime')):
        return f"{_str(_or(item.get('label'), 'tanggal'))}: {_str(_or(item.get('value'), ''))}"
    if _truthy(item.get('row')):
        return ' | '.join(f for f in (_nf_widget_item_fallback(s) for s in item.get('row')) if f)
    if _truthy(item.get('column')) or _truthy(item.get('list')):
        seq = item.get('column') or item.get('list')
        return '\n'.join(f for f in (_nf_widget_item_fallback(s) for s in seq) if f)
    return ''


def _nf_build_bloks_widget(widget):
    widget = widget or {}
    align = _nn(widget.get('align'), 'center')
    items = _nn(widget.get('items'), [])
    fallback = widget.get('fallback')
    surface_id = str(uuid.uuid4())
    components = []
    children = [c for c in (_nf_build_widget_item(item, f'item_{i}', components) for i, item in enumerate(items)) if c]
    components.insert(0, {'id': 'root', 'component': 'Column', 'justify': 'center', 'align': align, 'children': children})
    data = js_json({
        'version': 'v0.9',
        'createSurface': {
            'surfaceId': surface_id,
            'catalogId': 'https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json',
            'components': components,
        },
    })
    auto_fallback = '\n'.join(f for f in (_nf_widget_item_fallback(i) for i in items) if f)
    return {
        'uuid': surface_id,
        'data': data,
        'type': 'im_a2ui',
        'fallback': _or(fallback, auto_fallback, 'Cannot load Widget A2UI'),
    }


def _is_native_flow_style(opts):
    o = opts or {}
    return (
        _truthy(o.get('nativeFlow')) or _truthy(o.get('cards')) or _truthy(o.get('image'))
        or _truthy(o.get('video')) or _truthy(o.get('document')) or _truthy(o.get('location'))
        or _truthy(o.get('caption'))
        or (_truthy(o.get('text')) and not _truthy(o.get('body')) and not _truthy(o.get('buttons')))
    )


def _user_id(conn):
    return _get(_attr(conn, 'user'), 'id')


def _user_jid(conn):
    return _get(_attr(conn, 'user'), 'jid')


def _cc(conn, name, *args):
    if conn is None:
        return None
    fn = _attr(conn, name)
    if not callable(fn):
        raise TypeError(f'{name} is not a function')
    return fn(*args)


def _conn_decode(conn, jid):
    if conn is None:
        return None
    fn = _attr(conn, 'decodeJid')
    if callable(fn):
        return fn(jid)
    return _decode_jid_method(None, jid)


def _raw_of(value):
    if isinstance(value, (Msg, Quoted, PollUpdateQuoted)):
        return value._raw if isinstance(value, Msg) else value.vM
    return value


def _lid_to_pn(raw):
    users = _db_users()
    if isinstance(users, dict):
        for jid, u in users.items():
            if jid.endswith('@s.whatsapp.net') and _get(u, 'lid') == raw:
                return jid
        num = _get(users.get(raw), 'number')
        if _is_str(num) and num.endswith('@s.whatsapp.net'):
            return num
    return raw


def _resolve_mention_jid(conn, jid, chat):
    if not (_is_str(jid) and jid.endswith('@lid')):
        return jid
    users = _db_users() or {}
    number = _get(users.get(jid), 'number')
    if _truthy(number):
        return number
    try:
        store = _store()
    except Exception:
        store = None
    contact = _get(_get(store, 'contacts'), jid)
    if _truthy(_get(contact, 'pn')):
        return _str(contact.get('pn')) + '@s.whatsapp.net'
    lid_num = jid.split('@')[0]
    gm = _get(conn, 'groupMetadata')
    gmd = gm.get(chat) if isinstance(gm, dict) else None
    participants = _get(gmd, 'participants')
    if _truthy(participants):
        for p in participants:
            pid = _get(p, 'id')
            plid = _get(p, 'lid')
            if (_is_str(pid) and pid.split('@')[0] == lid_num) or (_is_str(plid) and plid.split('@')[0] == lid_num):
                if _is_str(pid) and pid.endswith('@s.whatsapp.net'):
                    return pid
                break
    return jid


def _ts_key(c):
    ts = _get(c, 'messageTimestamp')
    if ts is None:
        return 0.0
    return _num(_str(ts))


class Msg:
    def __init__(self, conn, raw):
        self.__dict__['conn'] = conn
        self.__dict__['_raw'] = raw
        self.__dict__['_text'] = None

    def __getitem__(self, k):
        return self._raw[k]

    def __contains__(self, k):
        return k in self._raw

    def get(self, k, default=None):
        value = _get(self._raw, k)
        return default if value is None else value

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        raw = self.__dict__.get('_raw')
        if raw is None:
            return None
        return _get(raw, name)

    def __setattr__(self, name, value):
        if name in ('conn', '_raw', '_text'):
            self.__dict__[name] = value
            return
        if isinstance(getattr(type(self), name, None), property):
            object.__setattr__(self, name, value)
            return
        self._raw[name] = value

    @property
    def key(self):
        return _get(self._raw, 'key')

    @property
    def message(self):
        return _get(self._raw, 'message')

    @property
    def pushName(self):
        return _get(self._raw, 'pushName')

    @property
    def participant(self):
        return _get(self._raw, 'participant')

    @property
    def messageTimestamp(self):
        return _get(self._raw, 'messageTimestamp')

    @property
    def id(self):
        return _get(self.key, 'id')

    @property
    def isBaileys(self):
        i = self.id
        return _is_str(i) and i.startswith('BAE5')

    @property
    def chat(self):
        msg = self.message
        sdm = _get(_get(msg, 'senderKeyDistributionMessage'), 'groupId')
        group = sdm if (_truthy(sdm) and sdm != 'status@broadcast') else None
        base = _or(_get(self.key, 'remoteJid'), group, '')
        return string_decode_jid(base)

    @property
    def isGroup(self):
        return self.chat.endswith('@g.us')

    @property
    def sender(self):
        conn = self.conn
        chat = self.chat
        if chat.endswith('@newsletter'):
            p = _or(_get(self.key, 'participant'), self.participant)
            if not _truthy(p) or p == chat:
                return _or(_conn_decode(conn, _user_id(conn)), chat)
            return _or(_conn_decode(conn, p), p)
        if _truthy(_get(self.key, 'participantPn')):
            return decode_jid(_get(self.key, 'participantPn') + '@s.whatsapp.net')
        if _truthy(_get(self.key, 'senderPn')):
            return decode_jid(_get(self.key, 'senderPn') + '@s.whatsapp.net')
        from_me = _get(self.key, 'fromMe')
        first = _user_id(conn) if _truthy(from_me) else from_me
        raw_sender = _conn_decode(conn, _or(first, self.participant, _get(self.key, 'participant'), chat, ''))
        if _is_str(raw_sender) and raw_sender.endswith('@lid'):
            return _lid_to_pn(raw_sender)
        return raw_sender

    @property
    def fromMe(self):
        v = _get(self.key, 'fromMe')
        if _truthy(v):
            return v
        return bool(areJidsSameUser(_user_id(self.conn), self.sender))

    @property
    def mtype(self):
        if not _truthy(self.message):
            return None
        return getContentType(self.message)

    @property
    def msg(self):
        if not _truthy(self.message):
            return None
        return _get(self.message, self.mtype)

    @property
    def mediaMessage(self):
        if not _truthy(self.message):
            return None
        if _truthy(_get(self.msg, 'url')) or _truthy(_get(self.msg, 'directPath')):
            message = dict(self.message)
        else:
            message = extractMessageContent(self.message)
        if not _truthy(message):
            return None
        mtype = next(iter(message.keys()), None)
        return message if mtype in MEDIA_TYPES else None

    @property
    def mediaType(self):
        message = self.mediaMessage
        if not _truthy(message):
            return None
        return next(iter(message.keys()), None)

    @property
    def quoted(self):
        return _build_quoted(self)

    @property
    def text(self):
        if _is_str(self._text):
            return self._text
        msg = self.msg
        text = _or(msg if _is_str(msg) else _get(msg, 'text'), _get(msg, 'caption'), _get(msg, 'contentText'), '')
        if _is_str(text):
            return text
        return _or(_get(text, 'selectedDisplayText'), _get(_get(text, 'hydratedTemplate'), 'hydratedContentText'), text) or ''

    @text.setter
    def text(self, value):
        self._text = value

    @property
    def mentionedJid(self):
        jids = _get(self.msg, 'contextInfo')
        jids = _get(jids, 'mentionedJid')
        if not (isinstance(jids, list) and len(jids)):
            jids = []
        return [_resolve_mention_jid(self.conn, j, self.chat) for j in jids]

    @property
    def name(self):
        if not _nullish(self.pushName) and _truthy(self.pushName):
            return self.pushName
        return _cc(self.conn, 'getName', self.sender)

    def download(self, saveToFile=False):
        mtype = self.mediaType
        return _cc(self.conn, 'downloadM', self.mediaMessage[mtype], _strip_message(mtype), {'saveToFile': saveToFile})

    def reply(self, text, chatId=None, options=None):
        return _cc(self.conn, 'reply', chatId if _truthy(chatId) else self.chat, text, self, options)

    def copy(self):
        M = proto.WebMessageInfo
        return smsg(self.conn, M.fromObject(M.toObject(self._raw)))

    def forward(self, jid, force=False, options=None):
        o = options or {}
        return _cc(self.conn, 'sendMessage', jid, {'forward': self._raw, 'force': force, **o}, {**o})

    def copyNForward(self, jid, forceForward=False, options=None):
        return _cc(self.conn, 'copyNForward', jid, self._raw, forceForward, options or {})

    def cMod(self, jid, text='', sender=None, options=None):
        if sender is None:
            sender = self.sender
        return _cc(self.conn, 'cMod', jid, self._raw, text, sender, options or {})

    def delete(self):
        return _cc(self.conn, 'sendMessage', self.chat, {'delete': self.key})

    def react(self, text):
        return _cc(self.conn, 'sendMessage', self.chat, {'react': {'text': text, 'key': self.key}})

    def getQuotedObj(self):
        q = self.quoted
        if not _truthy(_get(q, 'id')):
            return None
        conn = self.conn
        loaded = _cc(conn, 'loadMessage', q.sender, q.id) or _cc(conn, 'loadMessage', q.id) or q.vM
        return smsg(conn, proto.WebMessageInfo.fromObject(loaded))

    @property
    def getQuotedMessage(self):
        return self.getQuotedObj


class PollUpdateQuoted:
    def __init__(self, conn, parent, ctx, text, poll_name, selected, vm_message, voter, with_extra):
        self.__dict__['conn'] = conn
        self.__dict__['_parent'] = parent
        self.__dict__['_ctx'] = ctx
        self.__dict__['_text'] = text
        self.__dict__['_poll_name'] = poll_name
        self.__dict__['_selected'] = selected
        self.__dict__['_vm_message'] = vm_message
        self.__dict__['_voter'] = voter
        self.__dict__['_with_extra'] = with_extra

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        raise AttributeError(name)

    @property
    def text(self):
        return self._text

    @property
    def pollName(self):
        return self._poll_name

    @property
    def selectedOptions(self):
        return self._selected

    @property
    def mtype(self):
        return 'pollUpdateMessage'

    @property
    def mediaMessage(self):
        return None

    @property
    def mediaType(self):
        return None

    @property
    def id(self):
        return _get(self._ctx, 'stanzaId')

    @property
    def chat(self):
        return _or(_get(self._ctx, 'remoteJid'), self._parent.chat)

    @property
    def isBaileys(self):
        i = self.id
        return _is_str(i) and i.startswith('BAE5')

    @property
    def sender(self):
        return self._voter

    @property
    def fromMe(self):
        return bool(areJidsSameUser(self._voter, _user_jid(self.conn)))

    @property
    def vM(self):
        message = {'key': {'fromMe': self.fromMe, 'remoteJid': self.chat, 'id': self.id}, 'message': self._vm_message}
        if self._parent.isGroup:
            message['participant'] = self.sender
        return proto.WebMessageInfo.fromObject(message)


class Quoted:
    def __init__(self, parent, ctx, qtype, quoted, q_orig, data, text0):
        self.__dict__['_parent'] = parent
        self.__dict__['conn'] = parent.conn
        self.__dict__['_ctx'] = ctx
        self.__dict__['_type'] = qtype
        self.__dict__['_quoted'] = quoted
        self.__dict__['_q_orig'] = q_orig
        self.__dict__['_data'] = data
        self.__dict__['_text0'] = text0

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        data = self.__dict__.get('_data')
        if data is None:
            return None
        return data.get(name)

    @property
    def mtype(self):
        return self._type

    @property
    def mediaMessage(self):
        if _truthy(_get(self._q_orig, 'url')) or _truthy(_get(self._q_orig, 'directPath')):
            message = dict(self._quoted)
        else:
            message = extractMessageContent(self._quoted)
        if not _truthy(message):
            return None
        mtype = next(iter(message.keys()), None)
        return message if mtype in MEDIA_TYPES else None

    @property
    def mediaType(self):
        message = self.mediaMessage
        if not _truthy(message):
            return None
        return next(iter(message.keys()), None)

    @property
    def id(self):
        return _get(self._ctx, 'stanzaId')

    @property
    def chat(self):
        return _or(_get(self._ctx, 'remoteJid'), self._parent.chat)

    @property
    def isBaileys(self):
        i = self.id
        return _is_str(i) and i.startswith('BAE5')

    @property
    def sender(self):
        qpn = _get(self._ctx, 'quotedParticipantPn')
        if _truthy(qpn):
            return decode_jid(qpn + '@s.whatsapp.net')
        raw = string_decode_jid(_or(_get(self._ctx, 'participant'), self.chat, ''))
        if _is_str(raw) and raw.endswith('@lid'):
            return _lid_to_pn(raw)
        return raw

    @property
    def fromMe(self):
        return bool(areJidsSameUser(self.sender, _user_jid(self.conn)))

    @property
    def text(self):
        return _or(self._text0, self._data.get('caption'), self._data.get('contentText'),
                   self._data.get('selectedDisplayText'), '') or ''

    @property
    def mentionedJid(self):
        jids = _or(_get(self._q_orig, 'contextInfo') and _get(self._q_orig['contextInfo'], 'mentionedJid'),
                   _get(self._parent.getQuotedObj(), 'mentionedJid'), [])
        return [_resolve_mention_jid(self.conn, j, self.chat) for j in jids]

    @property
    def pollName(self):
        return self._data.get('name') or ''

    @property
    def name(self):
        sender = self.sender
        return _cc(self.conn, 'getName', sender) if _truthy(sender) else None

    @property
    def album(self):
        if self._type != 'albumMessage':
            return []
        items = _cc(self.conn, 'loadAlbum', self.chat, self.id) or []
        kept = [c for c in items if _get(_get(c, 'message'), 'imageMessage') or _get(_get(c, 'message'), 'videoMessage')]
        return sorted(kept, key=_ts_key)

    async def downloadAlbum(self):
        out = []
        for c in self.album:
            mtype = 'imageMessage' if _get(c['message'], 'imageMessage') else 'videoMessage'
            media = c['message'][mtype]
            buffer = await maybe_await(_cc(self.conn, 'downloadM', media, _strip_message(mtype), {}))
            out.append({
                'type': _strip_message(mtype),
                'buffer': buffer,
                'caption': _or(_get(media, 'caption'), ''),
                'message': c,
            })
        return out

    @property
    def vM(self):
        children = self.album if self._type == 'albumMessage' else []
        raw = self._data
        if self._type == 'albumMessage' and children:
            raw = {
                **raw,
                'expectedImageCount': _or(raw.get('expectedImageCount'),
                                          sum(1 for c in children if _get(c['message'], 'imageMessage'))),
                'expectedVideoCount': _or(raw.get('expectedVideoCount'),
                                          sum(1 for c in children if _get(c['message'], 'videoMessage'))),
            }
        message = {'key': {'fromMe': self.fromMe, 'remoteJid': self.chat, 'id': self.id}, 'message': {self._type: raw}}
        if self._parent.isGroup:
            message['participant'] = self.sender
        vm = proto.WebMessageInfo.fromObject(message)
        if children:
            vm['message']['albumMessage']['medias'] = [c['message'] for c in children]
        return vm

    @property
    def fakeObj(self):
        return self.vM

    def download(self, saveToFile=False):
        if self._type == 'albumMessage':
            tasks = []
            for c in self.album:
                mtype = 'imageMessage' if _get(c['message'], 'imageMessage') else 'videoMessage'
                tasks.append(_cc(self.conn, 'downloadM', c['message'][mtype], _strip_message(mtype), {'saveToFile': saveToFile}))
            return asyncio.gather(*tasks)
        mtype = self.mediaType
        if not _truthy(mtype) or not _truthy(self.mediaMessage):
            raise RuntimeError('Quoted message is not a media message, cannot download.')
        return _cc(self.conn, 'downloadM', self.mediaMessage[mtype], _strip_message(mtype), {'saveToFile': saveToFile})

    def reply(self, text, chatId=None, options=None):
        return _cc(self.conn, 'reply', chatId if _truthy(chatId) else self.chat, text, self.vM, options)

    def copy(self):
        M = proto.WebMessageInfo
        return smsg(self.conn, M.fromObject(M.toObject(self.vM)))

    def forward(self, jid, force=False, options=None):
        o = options or {}
        return _cc(self.conn, 'sendMessage', jid, {'forward': self.vM, 'force': force, **o}, {**o})

    def copyNForward(self, jid, forceForward=False, options=None):
        return _cc(self.conn, 'copyNForward', jid, self.vM, forceForward, options or {})

    def cMod(self, jid, text='', sender=None, options=None):
        if sender is None:
            sender = self.sender
        return _cc(self.conn, 'cMod', jid, self.vM, text, sender, options or {})

    def delete(self):
        return _cc(self.conn, 'sendMessage', self.chat, {'delete': self.vM['key']})

    def react(self, text):
        return _cc(self.conn, 'sendMessage', self.chat, {'react': {'text': text, 'key': self.vM['key']}})


QUOTED_WRAPPERS = ('ephemeralMessage', 'viewOnceMessage', 'documentWithCaptionMessage', 'viewOnceMessageV2',
                   'viewOnceMessageV2Extension', 'editedMessage', 'associatedChildMessage',
                   'groupStatusMessage', 'groupStatusMessageV2')


def _build_quoted(parent):
    msg = parent.msg
    context_info = _get(msg, 'contextInfo')
    quoted = _get(context_info, 'quotedMessage')
    conn = parent.conn
    if not _truthy(quoted) and _truthy(_get(context_info, 'stanzaId')):
        store = None
        try:
            store = _store()
        except Exception:
            store = None
        loader = _get(store, 'loadMessage')
        poll_msg = loader(context_info.get('stanzaId')) if callable(loader) else None
        pm = _get(poll_msg, 'message')
        poll_type = None
        if _truthy(poll_msg):
            for candidate in ('pollCreationMessage', 'pollCreationMessageV2', 'pollCreationMessageV3'):
                if _truthy(_get(pm, candidate)):
                    poll_type = candidate
                    break
        participant = context_info.get('participant')
        voter_jid = _or(string_decode_jid(_or(participant, '')), participant)
        if _truthy(poll_msg) and poll_type:
            update = None
            for u in (_get(poll_msg, 'pollUpdates') or []):
                v = _or(string_decode_jid(_or(_get(u, 'voter'), '')), _get(u, 'voter'))
                if v == voter_jid:
                    update = u
                    break
            poll_name = _or(_get(_get(pm, poll_type), 'name'), '')
            selected = _get(update, 'selectedOptions')
            vote_text = ', '.join(_str(x) for x in selected) if (isinstance(selected, list) and len(selected)) else None
            return PollUpdateQuoted(
                conn, parent, context_info, _or(vote_text, ''), poll_name,
                _or(selected, []), pm, voter_jid, True,
            )
        return PollUpdateQuoted(
            conn, parent, context_info, '', None, None,
            {'pollUpdateMessage': {}}, voter_jid, False,
        )
    if not _truthy(msg) or not _truthy(context_info) or not _truthy(quoted):
        return None
    for _ in range(5):
        inner = None
        for key in QUOTED_WRAPPERS:
            candidate = _get(quoted, key)
            if _truthy(candidate):
                inner = candidate
                break
        if not _truthy(inner):
            break
        quoted = _get(inner, 'message')
    if not _truthy(quoted):
        return None
    qtype = next(iter(quoted.keys()), None)
    q = quoted.get(qtype)
    if not _truthy(q):
        q = {}
    if _is_str(q):
        data = {'text': q}
        text0 = q
    else:
        data = _copy.deepcopy(q) if isinstance(q, dict) else {}
        text0 = _or(_get(q, 'text'), '')
    return Quoted(parent, context_info, qtype, quoted, q, data, text0)


MEDIA_TYPES = ['imageMessage', 'videoMessage', 'audioMessage', 'stickerMessage', 'documentMessage']


def smsg(conn, m, has_parent=None):
    if not m:
        return m
    raw = proto.WebMessageInfo.fromObject(m)
    wrapped = Msg(conn, raw)
    pkey = None
    if _truthy(wrapped.message):
        if wrapped.mtype == 'protocolMessage' and _truthy(_get(wrapped.msg, 'key')):
            pkey = wrapped.msg['key']
            if not _truthy(pkey.get('participant')) or pkey.get('participant') == 'status_me':
                pkey['participant'] = wrapped.sender
            pkey['fromMe'] = areJidsSameUser(pkey.get('participant'), _user_id(conn))
            if not pkey['fromMe'] and areJidsSameUser(pkey.get('remoteJid'), _user_id(conn)):
                pkey['remoteJid'] = wrapped.sender
    try:
        if pkey and wrapped.mtype == 'protocolMessage':
            _emit(conn, 'messages.delete', {'keys': [pkey]})
    except Exception as e:
        print(e, file=sys.stderr)
    return wrapped


def serialize():
    return Msg


def logic(check, inp, out):
    if len(inp) != len(out):
        raise ValueError('Input and Output must have same length')
    for i in range(len(inp)):
        if check == inp[i]:
            return out[i]
    return None


def _is_number_str(value):
    r = _parse_int(value)
    return not (isinstance(r, float) and math.isnan(r))


def _capitalize(s):
    return s[:1].upper() + s[1:]


def _capitalize_v2(s):
    return ' '.join(_capitalize(v) for v in s.split(' '))


def _sensor_text(s):
    s = _str(s)
    if not s:
        return ''
    return s[0] + re.sub(r'[a-zA-Z0-9]', '*', s[1:-1]) + s[-1]


def _to_time_string(ms):
    seconds = math.floor((ms / 1000) % 60)
    minutes = math.floor((ms / (60 * 1000)) % 60)
    hours = math.floor((ms / (60 * 60 * 1000)) % 24)
    days = math.floor(ms / (24 * 60 * 60 * 1000))
    return (
        (f'{days} day(s) ' if days else '')
        + (f'{hours} hour(s) ' if hours else '')
        + (f'{minutes} minute(s) ' if minutes else '')
        + (f'{seconds} second(s)' if seconds else '')
    ).strip()


def _get_random(value):
    if isinstance(value, (list, tuple, str)):
        return value[random.randrange(len(value))] if len(value) else None
    return math.floor(random.random() * value)


def proto_type():
    return {
        'toArrayBuffer': lambda buf: bytes(buf),
        'toArrayBufferV2': lambda buf: bytes(buf),
        'toBuffer': lambda ab: bytes(ab),
        'getFileType': lambda buf: file_type_from_buffer(buf),
        'isNumber': _is_number_str,
        'capitalize': _capitalize,
        'capitalizeV2': _capitalize_v2,
        'sensorText': _sensor_text,
        'decodeJid': string_decode_jid,
        'toTimeString': _to_time_string,
        'toSimpleNumber': to_simple_number,
        'getRandom': _get_random,
    }


class _Lazy:
    def __init__(self, getter):
        self.getter = getter

    def get(self):
        return self.getter()


def _put(obj, name, value):
    if isinstance(obj, dict):
        obj[name] = value
    else:
        setattr(obj, name, value)


def _bind(fn, *pre):
    return lambda *args, **kwargs: fn(*pre, *args, **kwargs)


def _store_call(store, name, *args):
    fn = _get(store, name)
    if callable(fn):
        return fn(*args)
    return None


def _try_store():
    try:
        return _store()
    except Exception:
        return None


def _tpl(value):
    return 'undefined' if value is None else _str(value)


def _opt(opts, key, default):
    value = _get(opts, key)
    return default if value is None else value


def _err_text(e):
    return _str(_or(getattr(e, 'message', None), str(e)))


def _is_js_number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return not (isinstance(v, float) and math.isnan(v))


def _round_half_up(x):
    return math.floor(x + 0.5)


def _esc_nl(s):
    return _str(s).replace('\n', '\\n')


async def _acall(conn, name, *args):
    return await maybe_await(_cc(conn, name, *args))


async def _collect(stream):
    out = bytearray()
    if hasattr(stream, '__aiter__'):
        async for chunk in stream:
            out.extend(chunk)
    else:
        for chunk in stream:
            out.extend(chunk)
    return bytes(out)


async def _fetch_bytes(source):
    if _is_buf(source):
        return bytes(source)
    res = await http.get(source, raise_for_status=False)
    return bytes(res.content)


def _b64(buf):
    return base64.b64encode(bytes(buf)).decode()


class FileInfo:
    def __init__(self, res, filename, mime, ext, data):
        self.res = res
        self.filename = filename
        self.mime = mime
        self.ext = ext
        self.data = data

    async def toBuffer(self):
        return self.data

    async def clear(self):
        if self.filename:
            try:
                os.unlink(self.filename)
            except OSError:
                pass


async def get_file(path, save_to_file=False):
    res = None
    filename = None
    data = b''
    if _is_buf(path):
        data = bytes(path)
    elif _is_str(path) and re.match(r'data:.*?/.*?;base64,', path, re.I):
        data = base64.b64decode(path.split(',', 1)[1])
    elif _is_str(path) and re.match(r'https?://', path):
        res = await http.get(path, raise_for_status=False)
        if not res.ok:
            raise RuntimeError(f'Failed to fetch {path}: {res.status} {res.statusText}')
        data = bytes(res.content)
    elif _is_str(path) and os.path.exists(path):
        filename = path
        with open(path, 'rb') as fh:
            data = fh.read()
    kind = file_type_from_buffer(data) or {}
    mime = _or(kind.get('mime'), 'application/octet-stream')
    ext = _or(kind.get('ext'), 'bin')
    if save_to_file and not filename:
        filename = os.path.join(os.getcwd(), 'data', 'tmp', f'{int(time.time() * 1000)}.{ext}')
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        with open(filename, 'wb') as fh:
            fh.write(data)
    return FileInfo(res, filename, mime, ext, data)


async def _send_file(conn, jid, path, filename='', caption='', quoted=None, ptt=False, options=None):
    options = options if options is not None else {}
    file = await get_file(path)
    stream = file.data
    mimetype = _or(options.get('mimetype'), file.mime)
    convert = None
    retry_file = None
    opt = {}
    if quoted:
        opt['quoted'] = quoted
    mime = file.mime
    if re.search('webp', mime) or (re.search('image', mime) and options.get('asSticker')):
        mtype = 'sticker'
    elif re.search('image', mime) or (re.search('webp', mime) and options.get('asImage')):
        mtype = 'image'
    elif re.search('video', mime):
        mtype = 'video'
    elif re.search('audio', mime):
        convert = await converter.to_audio(stream, file.ext)
        stream = convert.data
        mtype = 'audio'
        mimetype = _or(options.get('mimetype'), 'audio/ogg; codecs=opus')
    else:
        mtype = 'document'
    if options.get('asDocument'):
        mtype = 'document'
    for key in ('asSticker', 'asLocation', 'asVideo', 'asDocument', 'asImage'):
        options.pop(key, None)
    message = {**options, 'caption': caption, 'ptt': ptt, mtype: stream, 'mimetype': mimetype, 'fileName': filename or ''}
    error = None
    try:
        return await _acall(conn, 'sendMessage', jid, message, {**opt, **options})
    except Exception as e:
        print('sendFile: percobaan pertama gagal, mencoba ulang dengan stream baru (tanpa buffer penuh ke RAM):', _err_text(e), file=sys.stderr)
        try:
            retry_file = await get_file(path)
            retry_stream = retry_file.data
            if mtype == 'audio':
                retry_convert = await converter.to_audio(retry_stream, retry_file.ext)
                retry_stream = retry_convert.data
                convert = retry_convert
            return await _acall(conn, 'sendMessage', jid, {**message, mtype: retry_stream}, {**opt, **options})
        except Exception as e2:
            error = e2
    finally:
        await file.clear()
        if retry_file is not None:
            await retry_file.clear()
        if convert is not None:
            await convert.clear()
    if error is not None:
        raise error
    return None


_PRODUCT_KEYS = ('businessOwnerJid', 'productId', 'currencyCode', 'priceAmount1000', 'retailerId', 'url', 'salePriceAmount1000')


async def _send_product(conn, jid, media, title='', description='', quoted=None, options=None):
    options = options or {}
    business_owner_jid = _opt(options, 'businessOwnerJid', _user_jid(conn))
    product_id = _opt(options, 'productId', str(int(time.time() * 1000)))
    currency_code = options.get('currencyCode')
    price = options.get('priceAmount1000')
    retailer_id = options.get('retailerId')
    url = options.get('url')
    sale_price = options.get('salePriceAmount1000')
    rest = {k: v for k, v in options.items() if k not in _PRODUCT_KEYS}
    items = media if isinstance(media, list) else [media]
    if not items:
        raise TypeError('conn.sendProduct requires at least one media item')
    count = len(items)
    results = []
    for item in items:
        file = await get_file(item)
        try:
            buffer = await file.toBuffer()
            prepared = await prepareWAMessageMedia({'image': buffer}, {'upload': _attr(conn, 'waUploadToServer')})
            product = {
                'productImage': _get(prepared, 'imageMessage'),
                'productId': _str(product_id),
                'title': title,
                'description': description,
                'productImageCount': count,
                **({'currencyCode': currency_code} if _truthy(currency_code) else {}),
                **({'priceAmount1000': _str(price)} if price is not None else {}),
                **({'retailerId': retailer_id} if _truthy(retailer_id) else {}),
                **({'url': url} if _truthy(url) else {}),
                **({'salePriceAmount1000': _str(sale_price)} if sale_price is not None else {}),
            }
            content = {'productMessage': {'product': product, 'businessOwnerJid': business_owner_jid, **rest}}
            results.append(await _acall(conn, 'relayMessage', jid, content, {'quoted': quoted} if quoted else {}))
        finally:
            await file.clear()
    return results if isinstance(media, list) else results[0]


async def _send_sticker(conn, jid, img, options=None, quoted=None):
    if not isinstance(options, dict):
        options = {}
    packname = _opt(options, 'packname', '')
    author = _opt(options, 'author', '')
    categories = _opt(options, 'categories', [''])
    extra = _opt(options, 'extra', {})
    url = options.get('url')
    opt = {k: v for k, v in options.items() if k not in ('packname', 'author', 'categories', 'extra', 'url')}
    webp_buf = await to_webp(img, url)
    stiker = add_exif(webp_buf, packname, author, categories, extra)
    return await _acall(conn, 'sendMessage', jid, {'sticker': stiker, **opt}, {'quoted': quoted, **opt})


async def _resize(buffer, width, height):
    source = await to_image_buffer(buffer)
    return await jimp.resize(source, width, height, {'out': 'png'})


async def _crop(buffer, x, y, w, h):
    source = await to_image_buffer(buffer)
    return await jimp.extract(source, {'left': x, 'top': y, 'width': w, 'height': h}, 'jpeg')


async def _send_contact(conn, store, jid, data, quoted=None, options=None):
    options = options or {}
    if not isinstance(_at(data, 0), (list, tuple)) and _is_str(_at(data, 0)):
        data = [data]
    contacts = []
    for row in data:
        number, name = row[0], row[1]
        number = re.sub(r'[^0-9]', '', _str(number))
        njid = number + '@s.whatsapp.net'
        biz = await _acall(conn, 'getBusinessProfile', njid) or {}
        picture_url = await _acall(conn, 'profilePictureUrl', njid)
        try:
            await get_file(picture_url)
        except Exception:
            pass
        photo = 'undefined'
        name_s = _str(name)
        nm = _esc_nl(name_s)
        vcard = (
            f'BEGIN:VCARD\nVERSION:3.0\nN:;{nm};;;\nFN:{nm}\nORG:\n'
            f'item1.TEL;waid={number}:{format_intl_number(number)}\nitem1.X-ABLabel:Ponsel'
        )
        description = _get(biz, 'description')
        if _truthy(description):
            vname = _get(_store_call(_try_store(), 'getContact', njid), 'vname')
            biz_name = _str(_or(vname, _get_name(conn, store, njid), name_s)).replace('\n', '\\n', 1)
            vcard += (
                f'\nitem2.EMAIL;type=INTERNET:{_esc_nl(_or(_get(biz, "email"), ""))}'
                f'\nitem2.X-ABLabel:Email\nPHOTO;BASE64:{photo}\nX-WA-BIZ-NAME:{biz_name}'
                f'\nX-WA-BIZ-DESCRIPTION:{_esc_nl(description)}\n'
            )
        vcard += '\nEND:VCARD'
        contacts.append({'vcard': vcard, 'displayName': name})
    display = (f'{len(contacts)} kontak' if len(contacts) >= 2 else contacts[0]['displayName']) or None
    return await _acall(
        conn, 'sendMessage', jid,
        {**options, 'contacts': {**options, 'displayName': display, 'contacts': contacts}},
        {'quoted': quoted, **options},
    )


async def _send_array_contact(conn, store, jid, data, quoted=None, options=None):
    options = options or {}
    contacts = []
    for row in data:
        row = list(row) + [None] * (4 - len(row))
        number, _nama, ponsel, email = row[0], row[1], row[2], row[3]
        number = re.sub(r'[^0-9]', '', _str(number))
        njid = number + '@s.whatsapp.net'
        users = _db_users()
        user = users.get(njid) if isinstance(users, dict) else None
        name = _get(user, 'name') if _truthy(user) else _get_name(conn, store, njid)
        biz = await _acall(conn, 'getBusinessProfile', njid) or {}
        nm = _esc_nl(name)
        vcard = (
            f'BEGIN:VCARD\nVERSION:3.0\nFN:{nm}\nORG:\n'
            f'item1.TEL;waid={number}:{format_intl_number(number)}\n'
            f'item1.X-ABLabel:\U0001F4CC {_tpl(ponsel)}\n'
            f'item2.EMAIL;type=INTERNET:{_tpl(email)}\n'
            f'item2.X-ABLabel:✉️ Email\n'
            f'X-WA-BIZ-DESCRIPTION:{_esc_nl(_or(_get(biz, "description"), ""))}\n'
            f'X-WA-BIZ-NAME:{nm}\nEND:VCARD'
        )
        contacts.append({'vcard': vcard, 'displayName': name})
    display = (f'{len(contacts)} kontak' if len(contacts) > 1 else contacts[0]['displayName']) or None
    return await _acall(
        conn, 'sendMessage', jid,
        {'contacts': {**options, 'displayName': display, 'contacts': contacts}},
        {'quoted': quoted, **options, 'ephemeralExpiration': _get(G, 'ephemeral')},
    )


def _parse_mention(text=''):
    return [m.group(1) + '@s.whatsapp.net' for m in re.finditer(r'@([0-9]{5,16}|0)', _str(text))]


async def _reply(conn, jid, text='', quoted=None, options=None):
    if _is_buf(text):
        return await _send_file(conn, jid, text, 'file', '', quoted, False, options)
    options = options or {}
    return await _acall(
        conn, 'sendMessage', jid,
        {**options, 'text': text, 'mentions': _parse_mention(text)},
        {'quoted': quoted, **options},
    )


def _react(conn, jid, text='', key=None):
    return spawn(_acall(conn, 'sendMessage', jid, {'react': {'text': text, 'key': key}}))


async def _send_location(conn, jid, buffer, title='', address='', quoted=None, options=None):
    thumb = None
    if buffer:
        raw = await _fetch_bytes(buffer)
        thumb = _b64(await _acall(conn, 'resize', raw, 300, 300))
    location = {'degreesLatitude': 0, 'degreesLongitude': 0, 'name': title, 'address': address}
    if thumb:
        location['jpegThumbnail'] = thumb
    return await _acall(conn, 'relayMessage', jid, {'locationMessage': location}, {'quoted': quoted, **(options or {})})


def _send_footer(conn, jid, text, footer, options=None):
    content = {'interactiveMessage': {
        'body': {'text': text},
        'footer': {'text': footer},
        'nativeFlowMessage': {'messageParamsJson': ''},
        'contextInfo': {
            'groupMentions': [],
            'businessMessageForwardInfo': {'businessOwnerJid': _user_jid(conn)},
        },
    }}
    return spawn(_acall(conn, 'relayMessage', jid, content, {}))


def _normalize_preview_type(v):
    if _is_str(v):
        return v.strip().upper() or None
    if _is_js_number(v):
        return v
    return None


async def _send_url_preview(conn, jid, image, text, title, description, preview=0, quoted=None, options=None):
    options = options or {}
    preview_type = 0
    high_quality = False
    if _is_str(preview) or _is_js_number(preview):
        norm = _normalize_preview_type(preview)
        if norm is not None:
            preview_type = norm
    elif isinstance(preview, bool):
        high_quality = preview
    elif isinstance(preview, list):
        for v in preview:
            if _is_str(v) or _is_js_number(v):
                norm = _normalize_preview_type(v)
                if norm is not None:
                    preview_type = norm
            elif isinstance(v, bool):
                high_quality = v
    elif isinstance(preview, dict):
        ptype = preview.get('type')
        if _is_str(ptype) or _is_js_number(ptype):
            norm = _normalize_preview_type(ptype)
            if norm is not None:
                preview_type = norm
        if isinstance(preview.get('highQuality'), bool):
            high_quality = preview['highQuality']
    found = re.search(r'(https?://[^\s]+)', _str(text), re.I)
    matched = _or(options.get('matchedText'), found.group(1) if found else None, text)
    if _is_buf(image):
        source = bytes(image)
    elif _is_str(image):
        if re.match(r'https?://', image, re.I):
            source = await _fetch_bytes(image)
        else:
            with open(image, 'rb') as fh:
                source = fh.read()
    else:
        source = image
    jpeg_thumbnail = None
    thumb_w = None
    thumb_h = None
    try:
        img = await jimp.read(source)
        thumb_w = img.width
        thumb_h = img.height
        thumb = img.clone().resize(32, _round_half_up(32 * img.height / img.width))
        jpeg_thumbnail = await thumb.getBuffer('image/jpeg', {'quality': 50})
    except Exception as e:
        _log_warn(_get(conn, 'logger'), 'sendUrlPreview: gagal generate thumbnail via jimp native: ' + _err_text(e))
    media_input = {'image': source}
    if jpeg_thumbnail:
        media_input['jpegThumbnail'] = jpeg_thumbnail
    if thumb_w and thumb_h:
        media_input['width'] = thumb_w
        media_input['height'] = thumb_h
    prepared = await prepareWAMessageMedia(media_input, {
        'upload': _attr(conn, 'waUploadToServer'),
        'mediaTypeOverride': 'thumbnail-link',
    })
    image_msg = _get(prepared, 'imageMessage')
    link = {
        'matched-text': matched,
        'title': title,
        'description': description,
        'previewType': preview_type,
        'jpegThumbnail': _get(image_msg, 'jpegThumbnail'),
    }
    if high_quality:
        link['highQualityThumbnail'] = image_msg
    link.update(options)
    return await _acall(
        conn, 'sendMessage', jid,
        {'text': text, 'linkPreview': link, 'contextInfo': {**(options.get('contextInfo') or {})}},
        {'quoted': quoted},
    )


def _native_additional_nodes(jid, buttons):
    nodes = [{'tag': 'biz', 'attrs': {}, 'content': [{
        'tag': 'interactive',
        'attrs': {'type': 'native_flow', 'v': '1'},
        'content': [{'tag': 'native_flow', 'attrs': _nf_native_flow_attrs(buttons)}],
    }]}]
    if not (_is_str(jid) and jid.endswith('@g.us')):
        nodes.append({'tag': 'bot', 'attrs': {'biz_bot': '1'}})
    return nodes


async def _send_native_flow_button(conn, jid, opts, quoted, options):
    options = options or {}
    raw_quoted = _raw_of(quoted) if _truthy(quoted) else None
    safe_quoted = None
    if isinstance(raw_quoted, dict) and _truthy(raw_quoted.get('message')):
        qk = raw_quoted.get('key') or {}
        key = {'fromMe': False, 'id': _or(qk.get('id'), 'BAE5' + os.urandom(4).hex().upper())}
        key.update(qk)
        safe_quoted = {**raw_quoted, 'key': key}

    def build_context(base=None):
        context_info = {'mentionedJid': opts.get('mentions') or [], 'groupMentions': [], 'statusAttributions': []}
        context_info.update(base or {})
        if safe_quoted:
            context_info['stanzaId'] = safe_quoted['key']['id']
            context_info['participant'] = jidNormalizedUser(
                _or(safe_quoted['key'].get('participant'), safe_quoted['key'].get('remoteJid'), jid)
            )
            context_info['quotedMessage'] = safe_quoted.get('message')
        return context_info

    def gen_id():
        return _or(_get(options, 'messageId'), 'BAE5' + os.urandom(8).hex().upper())

    cards_in = opts.get('cards')
    if isinstance(cards_in, list) and cards_in:
        cards = []
        for card in cards_in:
            interactive = await _nf_build_interactive(conn, card)
            cards.append({**interactive, 'footer': {'text': _str(card.get('footer') or opts.get('footer') or '')}})
        content = {'interactiveMessage': {
            'body': {'text': _str(opts.get('text') or opts.get('caption') or opts.get('body') or '')},
            'footer': {'text': _str(opts.get('footer') or '')},
            'carouselMessage': {'cards': cards},
            'contextInfo': build_context(),
        }}
        message_id = gen_id()
        await _acall(conn, 'relayMessage', jid, content, {
            'messageId': message_id,
            'additionalNodes': _native_additional_nodes(jid, []),
            **options,
        })
        return {'key': {'remoteJid': jid, 'fromMe': True, 'id': message_id}, 'message': content}

    interactive = await _nf_build_interactive(conn, opts)
    content = {'interactiveMessage': {
        **interactive,
        'contextInfo': build_context(interactive.get('contextInfo') or {}),
        **({'interactiveAsTemplate': True} if _truthy(opts.get('interactiveAsTemplate')) else {}),
        **({'bloksWidget': _nf_build_bloks_widget(opts['widget'])} if _truthy(opts.get('widget')) else {}),
    }}
    buttons = _get(interactive.get('nativeFlowMessage'), 'buttons') or []
    message_id = gen_id()
    try:
        await _acall(conn, 'relayMessage', jid, content, {
            'messageId': message_id,
            'additionalNodes': _native_additional_nodes(jid, buttons),
            **options,
        })
    except Exception as e:
        print('[sendButton:nativeFlow] relayMessage gagal:', _err_text(e), file=sys.stderr)
        print('[sendButton:nativeFlow] payload yang dikirim:', js_json(content), file=sys.stderr)
        raise RuntimeError(
            f'sendButton (native flow) gagal relayMessage: {_err_text(e) or "unknown error (cek log server)"}'
        ) from e
    return {'key': {'remoteJid': jid, 'fromMe': True, 'id': message_id}, 'message': content}


async def _send_legacy_button(conn, jid, opts, quoted, options):
    head = _opt(opts, 'head', '')
    body = _opt(opts, 'body', '')
    footer = _opt(opts, 'footer', '')
    buttons = _opt(opts, 'buttons', [])
    sections = _opt(opts, 'sections', [])
    copy_list = _opt(opts, 'copy', [])
    url_list = _opt(opts, 'url', [])
    order = _opt(opts, 'order', ['list', 'button', 'copy', 'url'])
    attachment = opts.get('attachment')
    btype = opts.get('type')
    has_buttons = len(buttons) > 0
    has_sections = len(sections) > 0
    has_attachment = _truthy(attachment)
    first = _at(sections, 0)
    is_multi = has_sections and isinstance(first, list) and (_is_str(_at(first, 0)) or isinstance(_at(first, 0), list))
    parsed = []
    if is_multi:
        for item in sections:
            title = ' '
            if _is_str(_at(item, 0)):
                title = item[0]
                section_list = _at(item, 1)
            else:
                section_list = item
            parsed.append({'name': 'single_select', 'buttonParamsJson': js_json({'title': title, 'sections': section_list})})
    elif has_sections:
        select_title = ' '
        if _is_str(first):
            select_title = first
            parsed_sections = _at(sections, 1)
        else:
            parsed_sections = sections
        parsed = [{'name': 'single_select', 'buttonParamsJson': js_json({'title': select_title, 'sections': parsed_sections})}]
    quick = [{'name': 'quick_reply', 'buttonParamsJson': js_json({'display_text': _at(b, 0), 'id': _at(b, 1)})}
             for b in buttons] if has_buttons else []
    copies = [{'name': 'cta_copy', 'buttonParamsJson': js_json({'display_text': _at(c, 0), 'copy_code': _at(c, 1)})}
              for c in copy_list] if len(copy_list) > 0 else []
    urls = []
    if len(url_list) > 0:
        for u in url_list:
            merchant = _at(u, 2)
            if merchant is None:
                merchant = _at(u, 1)
            urls.append({'name': 'cta_url', 'buttonParamsJson': js_json({
                'display_text': _at(u, 0),
                'url': _at(u, 1),
                'merchant_url': merchant,
            })})
    groups = {'list': parsed, 'button': quick, 'copy': copies, 'url': urls}
    mapped = []
    for key in order:
        if key in groups:
            mapped.extend(groups[key])
    additional = [{'tag': 'biz', 'attrs': {}, 'content': [{
        'tag': 'interactive',
        'attrs': {'type': 'native_flow', 'v': '1'},
        'content': [{'tag': 'native_flow', 'attrs': _nf_native_flow_attrs(mapped)}],
    }]}]
    type_key = None
    type_extra = None
    if isinstance(btype, list):
        type_key = _at(btype, 0)
        type_extra = _at(btype, 1) if isinstance(_at(btype, 1), list) else None
    elif _is_str(btype):
        type_key = btype
    is_location = type_key == 'location'
    is_document = type_key == 'document'
    header = {}
    if is_location:
        loc_name = _nn(_at(type_extra, 0), '')
        loc_address = _nn(_at(type_extra, 1), '')
        thumb = None
        if has_attachment:
            thumb = await _acall(conn, 'resize', await _fetch_bytes(attachment), 300, 300)
        location = {'degreesLatitude': 0, 'degreesLongitude': 0, 'name': loc_name, 'address': loc_address}
        if thumb:
            location['jpegThumbnail'] = _b64(thumb)
        header = {'hasMediaAttachment': True, 'locationMessage': location}
    elif has_attachment:
        if is_document:
            file_name = _nn(_at(type_extra, 0), '')
            mimetype = _at(type_extra, 1)
            buffer = await _fetch_bytes(attachment)
            if not _truthy(mimetype):
                try:
                    meta = jimp.metadata(buffer)
                    mimetype = f"image/{meta['format']}" if _truthy(meta.get('format')) else 'application/octet-stream'
                except Exception:
                    mimetype = 'application/octet-stream'
            thumb = await _acall(conn, 'resize', buffer, 300, 300)
            media = await prepareWAMessageMedia({'document': buffer}, {'upload': _attr(conn, 'waUploadToServer')})
            document = {
                **(_get(media, 'documentMessage') or {}),
                'mimetype': mimetype,
                'jpegThumbnail': _b64(thumb),
                'fileLength': '99999999999999',
                **({'fileName': file_name} if _truthy(file_name) else {}),
            }
            header = {'title': head, 'hasMediaAttachment': True, 'documentMessage': document}
        else:
            resolved = type_key
            if not _truthy(resolved):
                try:
                    probe = await _fetch_bytes(attachment)
                    hx = probe[:12].hex()
                    is_video = (
                        (hx.startswith('000000') and ('66747970' in hx or '6d6f6f76' in hx or '6d646174' in hx))
                        or probe[:4] == b'RIFF'
                        or probe[:4].hex() == '1a45dfa3'
                    )
                    if is_video:
                        resolved = 'video'
                    else:
                        jimp.metadata(probe)
                        resolved = 'image'
                except Exception:
                    src_url = attachment if _is_str(attachment) else ''
                    resolved = 'video' if re.search(r'\.(mp4|mkv|avi|mov|webm)$', src_url, re.I) else 'document'
            value = bytes(attachment) if _is_buf(attachment) else {'url': attachment}
            media = await prepareWAMessageMedia({resolved: value}, {'upload': _attr(conn, 'waUploadToServer')})
            header = {'title': head, 'hasMediaAttachment': True, **media}
    else:
        header = {'title': head, 'subtitle': head, 'hasMediaAttachment': False}
    return await _acall(conn, 'relayMessage', jid, {'interactiveMessage': {
        'header': header,
        'body': {'text': body},
        'footer': {'text': footer},
        'nativeFlowMessage': {'buttons': mapped},
        'contextInfo': {'mentionedJid': [], 'groupMentions': [], 'statusAttributions': []},
    }}, {'quoted': quoted, 'additionalNodes': additional})


async def _send_button(conn, jid, opts=None, quoted=None, options=None):
    opts = opts or {}
    if _is_native_flow_style(opts):
        return await _send_native_flow_button(conn, jid, opts, quoted, options)
    return await _send_legacy_button(conn, jid, opts, quoted, options)


def _ai_rich(conn):
    return AIRich(conn)


def _cmod(conn, jid, message, text='', sender=None, options=None):
    options = options if options is not None else {}
    if _truthy(options.get('mentions')) and not isinstance(options['mentions'], list):
        options['mentions'] = [options['mentions']]
    copy = _copy.deepcopy(_raw_of(message))
    msg = copy.get('message') or {}
    msg.pop('messageContextInfo', None)
    msg.pop('senderKeyDistributionMessage', None)
    mtype = next(iter(msg), None)
    content = msg.get(mtype)
    if not _truthy(content):
        raise RuntimeError('Pesan yang di-quote tidak memiliki konten yang bisa dimodifikasi (misal: poll, reaksi, atau pesan sistem)')
    if _is_str(content):
        msg[mtype] = text or content
    elif _truthy(_get(content, 'caption')):
        content['caption'] = text or content['caption']
    elif _truthy(_get(content, 'text')):
        content['text'] = text or content['text']
    if not _is_str(content):
        base_ctx = _get(content, 'contextInfo')
        msg[mtype] = {**content, **options}
        msg[mtype]['contextInfo'] = {
            **(base_ctx or {}),
            'mentionedJid': _or(options.get('mentions'), _get(base_ctx, 'mentionedJid'), []),
        }
    if sender is None:
        sender = _user_jid(conn)
    if _truthy(copy.get('participant')):
        copy['participant'] = sender or copy['participant']
        sender = copy['participant']
    elif _truthy(_get(copy.get('key'), 'participant')):
        copy['key']['participant'] = sender or copy['key']['participant']
        sender = copy['key']['participant']
    if not isinstance(copy.get('key'), dict):
        copy['key'] = {}
    remote = _or(copy['key'].get('remoteJid'), jid)
    if '@s.whatsapp.net' in remote:
        sender = sender or remote
    elif '@broadcast' in remote:
        sender = sender or remote
    copy['key']['remoteJid'] = jid
    copy['key']['fromMe'] = bool(areJidsSameUser(sender, _user_id(conn)))
    return proto.WebMessageInfo.fromObject(copy)


async def _copy_n_forward(conn, jid, message, forwarding_score=True, options=None):
    options = options or {}
    vtype = None
    inner = _get(message, 'message')
    view_once = _get(inner, 'viewOnceMessage')
    if _truthy(options.get('readViewOnce')) and _truthy(_get(view_once, 'message')):
        view_once_ctx = view_once.get('contextInfo')
        body = view_once['message']
        vtype = next(iter(body), None)
        body[vtype].pop('viewOnce', None)
        message['message'] = proto.Message.fromObject(_copy.deepcopy(body))
        message['message'][vtype]['contextInfo'] = view_once_ctx
    mtype = getContentType(message.get('message') or {})
    m = generateForwardMessageContent(message, bool(forwarding_score))
    ctype = getContentType(m)
    if _truthy(forwarding_score) and _is_js_number(forwarding_score) and forwarding_score > 1:
        ci = m[ctype].setdefault('contextInfo', {})
        ci['forwardingScore'] = (ci.get('forwardingScore') or 0) + forwarding_score
    m[ctype]['contextInfo'] = {
        **(_get(_get(message, 'message').get(mtype) if _truthy(_get(message, 'message')) else None, 'contextInfo') or {}),
        **(_get(m[ctype], 'contextInfo') or {}),
    }
    m = generateWAMessageFromContent(jid, m, {**options, 'userJid': _user_jid(conn)})
    await _acall(conn, 'relayMessage', jid, m['message'], {
        'messageId': m['key']['id'],
        'additionalAttributes': {**options},
    })
    return m


async def _download_m(conn, m, mtype, opts=None):
    opts = opts or {}
    if not _truthy(m) or not (_get(m, 'url') or _get(m, 'directPath')):
        return b''
    stream = await downloadContentFromMessage(m, mtype)
    buffers = await _collect(stream)
    filename = None
    if _truthy(opts.get('saveToFile')):
        filename = (await get_file(buffers, True)).filename
    if _truthy(opts.get('saveToFile')) and filename and os.path.exists(filename):
        return filename
    return buffers


async def _group_name(conn, store, jid):
    meta = (await maybe_await(_store_call(store, 'fetchGroupMetadata', jid, _attr(conn, 'groupMetadata')))) or {}
    return _or(_get(meta, 'name'), _get(meta, 'subject'), format_intl_number(jid.replace('@s.whatsapp.net', '')))


def _get_name(conn, store, jid='', without_contact=False):
    jid = _decode_jid_method(None, jid) or ''
    without_contact = bool(_get(conn, 'withoutContact')) or bool(without_contact)
    if jid.endswith('@g.us'):
        return _group_name(conn, store, jid)
    if jid == '0@s.whatsapp.net':
        v = {'jid': jid, 'vname': 'WhatsApp'}
    elif areJidsSameUser(jid, _or(_get(_get(conn, 'user'), 'id'), '')):
        v = _get(conn, 'user')
    else:
        v = _store_call(store, 'getContact', jid) or {}
    shown = '' if without_contact else _get(v, 'name')
    return (
        shown or _get(v, 'subject') or _get(v, 'vname') or _get(v, 'notify') or _get(v, 'verifiedName')
        or format_intl_number(jid.replace('@s.whatsapp.net', ''))
    )


def _load_message(store, jid, mid=None):
    if not _truthy(jid) and not _truthy(mid):
        return None
    if _truthy(jid) and not _truthy(mid):
        mid, jid = jid, None
    if _truthy(jid) and _truthy(mid):
        return _store_call(store, 'loadMessage', jid, mid)
    return _store_call(store, 'loadMessage', mid)


def _load_album(store, jid, parent_id=None):
    if _truthy(jid) and not _truthy(parent_id):
        parent_id, jid = jid, None
    return _store_call(store, 'loadAlbum', jid, parent_id) or []


async def _send_group_v4_invite(conn, store, jid, participant, invite_code, invite_expiration,
                                group_name='unknown subject', caption='Invitation to join my WhatsApp group',
                                jpeg_thumbnail=None, options=None):
    options = options or {}
    expiration = _parse_int(invite_expiration)
    if not _truthy(expiration) or (isinstance(expiration, float) and math.isnan(expiration)):
        expiration = int(time.time() * 1000) + 3 * 86400000
    name = group_name if _truthy(group_name) else await maybe_await(_get_name(conn, store, jid))
    msg = proto.Message.fromObject({'groupInviteMessage': proto.GroupInviteMessage.fromObject({
        'inviteCode': invite_code,
        'inviteExpiration': expiration,
        'groupJid': jid,
        'groupName': name or None,
        'jpegThumbnail': _b64(jpeg_thumbnail) if _is_buf(jpeg_thumbnail) else None,
        'caption': caption,
    })})
    message = generateWAMessageFromContent(participant, msg, options)
    await _acall(conn, 'relayMessage', participant, message['message'], {
        'messageId': message['key']['id'],
        'additionalAttributes': {**options},
    })
    return message


def _serialize_m(conn, m):
    return smsg(conn, m)


def _bound_logger(logger):
    out = {}
    if isinstance(logger, dict):
        out.update(logger)
    elif logger is not None and hasattr(logger, '__dict__'):
        out.update(vars(logger))
    for name in ('info', 'error', 'warn', 'fatal', 'debug', 'trace'):
        fn = _get(logger, name)
        if callable(fn):
            out[name] = fn
    return out


def install_send_sticker_pack(conn):
    async def send(jid, options=None, quoted=None):
        return await send_sticker_pack(conn, jid, options or {}, quoted)

    _put(conn, 'sendStickerPack', send)
    return conn


def helper_connection(conn, store=None, logger=None):
    if store is None:
        store = _try_store()
    existing = _get(conn, 'user')
    bot_user = dict(existing) if isinstance(existing, dict) else {}

    def user_getter():
        creds = _get(_get(conn, 'authState'), 'creds')
        bot_user.update(_get(creds, 'me') or {})
        result = dict(bot_user)
        ident = result.get('id')
        result['jid'] = string_decode_jid(ident) if _is_str(ident) else ident
        return result

    _put(conn, 'user', _Lazy(user_getter))
    _put(conn, 'decodeJid', _bind(_decode_jid_method, None))
    _put(conn, 'logger', _bound_logger(logger))
    _put(conn, 'getFile', get_file)
    _put(conn, 'sendFile', _bind(_send_file, conn))
    _put(conn, 'sendProduct', _bind(_send_product, conn))
    _put(conn, 'sendSticker', _bind(_send_sticker, conn))
    _put(conn, 'resize', _resize)
    _put(conn, 'crop', _crop)
    _put(conn, 'sendContact', _bind(_send_contact, conn, store))
    _put(conn, 'sendArrayContact', _bind(_send_array_contact, conn, store))
    _put(conn, 'reply', _bind(_reply, conn))
    _put(conn, 'react', _bind(_react, conn))
    _put(conn, 'sendLocation', _bind(_send_location, conn))
    _put(conn, 'sendFooter', _bind(_send_footer, conn))
    _put(conn, 'sendUrlPreview', _bind(_send_url_preview, conn))
    _put(conn, 'sendButton', _bind(_send_button, conn))
    _put(conn, 'aiRich', _bind(_ai_rich, conn))
    _put(conn, 'cMod', _bind(_cmod, conn))
    _put(conn, 'copyNForward', _bind(_copy_n_forward, conn))
    _put(conn, 'downloadM', _bind(_download_m, conn))
    _put(conn, 'parseMention', _parse_mention)
    _put(conn, 'getName', _bind(_get_name, conn, store))
    _put(conn, 'loadMessage', _bind(_load_message, store))
    _put(conn, 'loadAlbum', _bind(_load_album, store))
    _put(conn, 'sendGroupV4Invite', _bind(_send_group_v4_invite, conn, store))
    _put(conn, 'serializeM', _bind(_serialize_m, conn))
    install_send_sticker_pack(conn)
    return conn
