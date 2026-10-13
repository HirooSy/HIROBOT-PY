import asyncio
import base64
import copy
import inspect
import json
import re
import sys
import time

from lib.utils.handler import G, PluginError
from lib.utils.helper import oc


IDENT = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')
DROP_CTX = ('deviceListMetadata', 'messageSecret')
CACHE_MAX = 300
INLINE_MAX_BYTES = 2 * 1024
DATA_URI = re.compile(r'data:([\w.+\-]+/[\w.+\-]+);base64,([A-Za-z0-9+/=]+)', re.ASCII)
FETCH_BUF_HEADER = 'const _fetchBuf = async url => Buffer.from(await (await fetch(url)).arrayBuffer())'
FETCH_URI_HEADER = "const _fetchUri = async (url, mime) => `data:${mime};base64,${Buffer.from(await (await fetch(url)).arrayBuffer()).toString('base64')}`"


def _cache():
    store = getattr(G, 'crmCache', None)
    if store is None:
        store = {}
        setattr(G, 'crmCache', store)
    return store


def _clone(value):
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _field(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _call(obj, name, *args):
    fn = getattr(obj, name, None)
    if callable(fn):
        return fn(*args)
    return None


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_long(v):
    return isinstance(v, dict) and _is_int(v.get('low')) and _is_int(v.get('high'))


def _long_to_num(v):
    big = ((v['high'] & 0xFFFFFFFF) << 32) | (v['low'] & 0xFFFFFFFF)
    return big if big <= 2 ** 53 - 1 else str(big)


def _ts_of(c):
    t = _field(c, 'messageTimestamp')
    if not t:
        return 0
    value = _long_to_num(t) if _is_long(t) else t
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def _b64decode(s):
    clean = re.sub(r'[^A-Za-z0-9+/]', '', s)
    clean += '=' * (-len(clean) % 4)
    try:
        return base64.b64decode(clean)
    except Exception:
        return b''


def _decode_unified(s):
    try:
        return json.loads(_b64decode(s).decode('utf-8', errors='replace'))
    except Exception:
        return None


def _json_str(s):
    return json.dumps(s, ensure_ascii=False)


def _js_num(v):
    if v != v:
        return 'NaN'
    if v in (float('inf'), float('-inf')):
        return 'Infinity' if v > 0 else '-Infinity'
    if v.is_integer() and abs(v) < 1e21:
        return str(int(v))
    r = repr(v)
    if 'e' in r:
        mant, exp = r.split('e')
        e = int(exp)
        return f"{mant}e{'+' if e > 0 else '-'}{abs(e)}"
    return r


def _guess_ext(buf):
    if buf[0:2] == b'\xff\xd8':
        return 'jpg'
    if buf[0:2] == b'\x89\x50':
        return 'png'
    if buf[0:4] == b'RIFF':
        return 'webp'
    return 'bin'


def _content_type(msg):
    if not isinstance(msg, dict):
        return None
    for k, v in msg.items():
        if k != 'messageContextInfo' and v is not None:
            return k
    return None


def _collect_data_uris(s, out):
    for mt in DATA_URI.finditer(s):
        b64 = mt.group(2)
        if len(b64) * 0.75 <= INLINE_MAX_BYTES:
            continue
        out[b64] = _b64decode(b64)


def _collect_big(value, out, key=''):
    if value is None:
        return
    buf = None
    if isinstance(value, (bytes, bytearray)):
        buf = bytes(value)
    elif isinstance(value, dict) and value.get('type') == 'Buffer' and isinstance(value.get('data'), list):
        buf = bytes(value['data'])
    if buf is not None:
        if len(buf) > INLINE_MAX_BYTES:
            out[base64.b64encode(buf).decode()] = buf
        return
    if isinstance(value, str):
        if key == 'data':
            decoded = _decode_unified(value)
            if isinstance(decoded, (dict, list)):
                _collect_big(decoded, out, key)
                return
        if ';base64,' in value:
            _collect_data_uris(value, out)
        return
    if isinstance(value, list):
        for v in value:
            _collect_big(v, out, key)
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if (k == 'messageContextInfo' or key == 'messageContextInfo') and k in DROP_CTX:
                continue
            _collect_big(v, out, k)


def _string_to_code(s, opts):
    urls = opts.get('urls')
    if urls is None or ';base64,' not in s:
        return _json_str(s)
    parts = []
    last = 0
    for mt in DATA_URI.finditer(s):
        url = urls.get(mt.group(2))
        if not url:
            continue
        parts.append(_json_str(s[last:mt.start()]))
        parts.append(f"await _fetchUri('{url}', '{mt.group(1)}')")
        last = mt.end()
    if not parts:
        return _json_str(s)
    parts.append(_json_str(s[last:]))
    return ' + '.join(parts)


def _buffer_to_code(buf, opts):
    b64 = base64.b64encode(buf).decode()
    urls = opts.get('urls')
    if len(buf) > INLINE_MAX_BYTES and urls is not None:
        url = urls.get(b64)
        if url:
            return f"await _fetchBuf('{url}')"
    return f"Buffer.from('{b64}', 'base64')"


def to_code(value, opts=None, indent=0, key=''):
    opts = opts or {}
    pad = '\t' * (indent + 1)
    end = '\t' * indent
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _js_num(value)
    if isinstance(value, str):
        if key == 'data':
            decoded = _decode_unified(value)
            if isinstance(decoded, (dict, list)):
                code = to_code(decoded, opts, indent, key)
                if code is not None:
                    return code
        return _string_to_code(value, opts)
    if isinstance(value, (bytes, bytearray)):
        if not len(value):
            return None
        return _buffer_to_code(bytes(value), opts)
    if isinstance(value, dict) and value.get('type') == 'Buffer' and isinstance(value.get('data'), list):
        if not value['data']:
            return None
        return _buffer_to_code(bytes(value['data']), opts)
    if _is_long(value):
        return str(_long_to_num(value))
    if isinstance(value, list):
        items = [c for c in (to_code(v, opts, indent + 1, key) for v in value) if c is not None]
        if not items:
            return None
        return '[\n' + ',\n'.join(pad + v for v in items) + '\n' + end + ']'
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            if (k == 'messageContextInfo' or key == 'messageContextInfo') and k in DROP_CTX:
                continue
            code = to_code(v, opts, indent + 1, k)
            if code is None:
                continue
            name = k if IDENT.fullmatch(k) else _json_str(k)
            lines.append(f'{pad}{name}: {code}')
        if not lines:
            return '{}'
        return '{\n' + ',\n'.join(lines) + '\n' + end + '}'
    return None


async def _build_album_items(conn, children, upload):
    if not upload or not children:
        return None
    items = []
    for c in children:
        cm = _field(c, 'message')
        kind = 'image' if _field(cm, 'imageMessage') else 'video' if _field(cm, 'videoMessage') else None
        if not kind:
            return None
        media = _field(cm, kind + 'Message')
        try:
            buf = await conn.downloadM(media, kind, {})
        except Exception:
            return None
        if not isinstance(buf, (bytes, bytearray)) or not len(buf):
            return None
        ext = _guess_ext(buf) if kind == 'image' else 'mp4'
        try:
            url = await upload(bytes(buf), f'crm_{int(time.time() * 1000)}_{len(items)}.{ext}')
        except Exception:
            return None
        if not url:
            return None
        parts = [f'{kind}: await _fetchBuf({_json_str(url)})']
        caption = _field(media, 'caption')
        if caption:
            parts.append(f'caption: {_json_str(caption)}')
        items.append('\t\t{ ' + ', '.join(parts) + ' }')
    return items


def hook(conn):
    if conn is None:
        return
    orig = getattr(conn, 'relayMessage', None)
    if not callable(orig) or getattr(orig, 'crm_hooked', False):
        return
    cache = _cache()

    async def wrapped(jid, message, opts=None):
        opts = {} if opts is None else opts
        res = orig(jid, message, opts)
        if inspect.isawaitable(res):
            res = await res
        key = res if isinstance(res, str) else opts.get('messageId')
        if key:
            cache[key] = _clone(message)
            if len(cache) > CACHE_MAX:
                cache.pop(next(iter(cache)))
        return res

    wrapped.crm_hooked = True
    setattr(conn, 'relayMessage', wrapped)


async def _all(m):
    hook(m.conn)


all = _all


async def handler(m, *, conn):
    if not m.quoted:
        raise PluginError('Reply pesan yang mau diambil kodenya.\nContoh: reply pesan button lalu ketik *.crm*')
    qid = m.quoted.id
    cache = _cache()
    message = cache.get(qid)
    if message is None:
        stored = _call(conn, 'loadMessage', m.quoted.sender, qid) or _call(conn, 'loadMessage', qid)
        message = _field(stored, 'message') if stored else None
    if message is None:
        raw = _field(_field(m.msg, 'contextInfo'), 'quotedMessage')
        t = _content_type(raw)
        if t and not (t == 'conversation' and not raw.get('conversation')):
            message = raw
    if message is None:
        raise PluginError('Isi pesan tidak ketemu. Pesan ini dikirim lewat relayMessage sebelum crm aktif, kirim ulang pesannya lalu coba lagi.')

    children = []
    if _field(message, 'albumMessage') is not None:
        loaded = _call(conn, 'loadAlbum', m.chat, qid) or []
        children = sorted([c for c in loaded if _field(c, 'message') is not None], key=_ts_of)
    if _field(message, 'albumMessage') is not None and children:
        kinds = ['video' if _field(_field(c, 'message'), 'videoMessage') else 'image' for c in children]
        album = dict(_field(message, 'albumMessage'))
        album['expectedImageCount'] = album.get('expectedImageCount') or kinds.count('image')
        album['expectedVideoCount'] = album.get('expectedVideoCount') or kinds.count('video')
        message = {**message, 'albumMessage': album}

    upload = oc(G, 'scraper', 'upload', 'default')
    album_items = await _build_album_items(conn, children, upload)

    child_messages = []
    if not album_items:
        for c in children:
            child_messages.append({**dict(_field(c, 'message')), 'messageContextInfo': {
                'messageAssociation': {
                    'parentMessageKey': {'remoteJid': '__CHAT__', 'fromMe': True, 'id': '__ALBUM__'},
                    'associationType': 1,
                }}})

    big = {}
    _collect_big(message, big)
    for cm in child_messages:
        _collect_big(cm, big)

    urls = {}
    if big and upload:
        async def _upload_one(b64, buf):
            try:
                url = await upload(buf, f'crm_{int(time.time() * 1000)}.{_guess_ext(buf)}')
                if url:
                    urls[b64] = url
            except Exception as e:
                print('[crm] upload failed, falling back to inline base64:', e, file=sys.stderr)

        await asyncio.gather(*(_upload_one(b64, buf) for b64, buf in big.items()))

    body = to_code(message, {'urls': urls}, 0)
    body_s = 'undefined' if body is None else body
    if album_items:
        code = 'await conn.sendMessage(m.chat, {\n\talbum: [\n' + ',\n'.join(album_items) + '\n\t]\n}, {})'
    elif child_messages:
        parts = [f'const _album = await conn.relayMessage(m.chat, {body_s}, {{}})']
        for cm in child_messages:
            child_body = to_code(cm, {'urls': urls}, 0)
            child_body = child_body.replace('"__CHAT__"', 'm.chat', 1).replace('"__ALBUM__"', '_album', 1)
            parts.append(f'await conn.relayMessage(m.chat, {child_body}, {{}})')
        code = '\n\n'.join(parts)
    else:
        code = f'await conn.relayMessage(m.chat, {body_s}, {{}})'

    header_lines = []
    if '_fetchBuf(' in code:
        header_lines.append(FETCH_BUF_HEADER)
    if '_fetchUri(' in code:
        header_lines.append(FETCH_URI_HEADER)
    header = '\n'.join(header_lines) + '\n\n' if header_lines else ''
    out = header + code + '\n'
    limit = 60000
    for i in range(0, len(out), limit):
        await conn.sendMessage(m.chat, {'text': out[i:i + limit]}, {'quoted': m})


help = ['crm']
tags = ['tools']
command = re.compile(r'^(crm|copyrelay)$', re.I)
ai = {'risk': 'low', 'description': 'inspect message recipe, look up how to build that message'}
