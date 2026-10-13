import asyncio
import math
import os
import re
import shutil
import struct
import tempfile
import time

from lib.compat import ffmpeg as ff, http
from lib.utils.canvas import jimp


PNG_CDN = 'https://unpkg.com/emoji-datasource-apple@16.0.0/img/apple/64/'
PNG_CDN_FALLBACK = 'https://cdn.jsdelivr.net/npm/emoji-datasource-apple@16.0.0/img/apple/64/'
EMOJI_JSON_URLS = [
    'https://unpkg.com/emoji-datasource-apple@16.0.0/emoji.json',
    'https://cdn.jsdelivr.net/npm/emoji-datasource-apple@16.0.0/emoji.json',
]
ZWJ, FE0F, KEYCAP = 0x200D, 0xFE0F, 0x20E3
WIDTH = HEIGHT = 512
MARGIN = WORD_SPACING = 25
FONT_URL = 'https://github.com/HirooSy/HirooSy/raw/refs/heads/main/arial.ttf'
FONT_SIGNATURES = (b'\x00\x01\x00\x00', b'OTTO', b'true', b'ttcf')
FETCH_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
WHITE = '#ffffff'
EMOJI_RANGES = (
    (0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B),
    (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x25AA, 0x25AB), (0x25B6, 0x25B6), (0x25C0, 0x25C0),
    (0x25FB, 0x25FE), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55),
    (0x2934, 0x2935), (0x3297, 0x3297), (0x3299, 0x3299),
)
EMOJI_SINGLE = {0xA9, 0xAE, 0x203C, 0x2049, 0x2122, 0x2139, 0x2328, 0x23CF, 0x24C2, 0x3030, 0x303D}

_state = {'emoji_map': None, 'font_path': None, 'loaded': None}
_font_lock = asyncio.Lock()


def js_round(v):
    return math.floor(v + 0.5)


async def with_timeout(coro, seconds, label):
    try:
        return await asyncio.wait_for(coro, seconds)
    except asyncio.TimeoutError:
        raise RuntimeError(f'Timeout after {int(seconds * 1000)}ms: {label}') from None


def parse_font(buf):
    num_tables = struct.unpack_from('>H', buf, 4)[0]
    tables = {}
    for i in range(num_tables):
        rec = 12 + i * 16
        tag = bytes(buf[rec:rec + 4]).decode('latin1')
        offset, length = struct.unpack_from('>II', buf, rec + 8)
        tables[tag] = (offset, length)
    for t in ('head', 'hhea', 'hmtx', 'maxp', 'cmap'):
        if t not in tables:
            raise ValueError(f'Font is missing required table "{t}"')
    units_per_em = struct.unpack_from('>H', buf, tables['head'][0] + 18)[0]
    n_hmetrics = struct.unpack_from('>H', buf, tables['hhea'][0] + 34)[0]
    num_glyphs = struct.unpack_from('>H', buf, tables['maxp'][0] + 4)[0]
    advances = []
    last = 0
    hmtx = tables['hmtx'][0]
    for g in range(num_glyphs):
        if g < n_hmetrics:
            last = struct.unpack_from('>H', buf, hmtx + g * 4)[0]
        advances.append(last)
    cmap = parse_cmap(buf, tables['cmap'][0])
    family = parse_family_name(buf, tables['name'][0]) if 'name' in tables else None
    return {'unitsPerEm': units_per_em, 'familyName': family, 'cmap': cmap, 'advances': advances}


def parse_family_name(buf, off):
    count = struct.unpack_from('>H', buf, off + 2)[0]
    string_off = off + struct.unpack_from('>H', buf, off + 4)[0]
    best, best_score = None, -1
    for i in range(count):
        rec = off + 6 + i * 12
        plat, enc, lang, name_id, length, str_off = struct.unpack_from('>HHHHHH', buf, rec)
        if name_id not in (1, 16):
            continue
        score = 10 if name_id == 16 else 0
        if plat == 3 and enc == 1 and lang == 0x409:
            score += 5
        elif plat == 3:
            score += 3
        elif plat == 1 and enc == 0:
            score += 1
        if score <= best_score:
            continue
        start = string_off + str_off
        raw = bytes(buf[start:start + length])
        best = raw.decode('utf-16-be', errors='replace') if plat == 3 else raw.decode('latin1')
        best_score = score
    return best


def parse_cmap(buf, off):
    n = struct.unpack_from('>H', buf, off + 2)[0]
    best, best_score = -1, -1
    for i in range(n):
        rec = off + 4 + i * 8
        plat, enc, sub = struct.unpack_from('>HHI', buf, rec)
        if plat == 3 and enc == 10:
            score = 5
        elif plat == 0 and enc >= 4:
            score = 5
        elif plat == 3 and enc == 1:
            score = 4
        elif plat == 0:
            score = 3
        elif plat == 1 and enc == 0:
            score = 1
        else:
            score = 0
        if score > best_score:
            best_score, best = score, off + sub
    if best == -1:
        raise ValueError('cmap: no supported unicode subtable found')
    fmt = struct.unpack_from('>H', buf, best)[0]
    cmap = {}
    if fmt == 4:
        seg2 = struct.unpack_from('>H', buf, best + 6)[0]
        seg = seg2 // 2
        end_off = best + 14
        start_off = end_off + seg2 + 2
        delta_off = start_off + seg2
        range_off = delta_off + seg2
        for s in range(seg):
            end = struct.unpack_from('>H', buf, end_off + s * 2)[0]
            start = struct.unpack_from('>H', buf, start_off + s * 2)[0]
            delta = struct.unpack_from('>h', buf, delta_off + s * 2)[0]
            ro = struct.unpack_from('>H', buf, range_off + s * 2)[0]
            if start == 0xFFFF and end == 0xFFFF:
                continue
            c = start
            while c <= end and c != 0xFFFF:
                if ro == 0:
                    gi = (c + delta) & 0xFFFF
                else:
                    addr = range_off + s * 2 + ro + (c - start) * 2
                    gi = struct.unpack_from('>H', buf, addr)[0]
                    if gi:
                        gi = (gi + delta) & 0xFFFF
                if gi:
                    cmap[c] = gi
                c += 1
    elif fmt == 12:
        groups = struct.unpack_from('>I', buf, best + 12)[0]
        for g in range(groups):
            s, e, gid = struct.unpack_from('>III', buf, best + 16 + g * 12)
            for c in range(s, e + 1):
                cmap[c] = gid + (c - s)
    else:
        raise ValueError(f'cmap format {fmt} is not supported (only format 4 and 12)')
    return cmap


def glyph_advance(metrics, gi):
    adv = metrics['advances']
    if gi < len(adv) and adv[gi]:
        return adv[gi]
    return adv[0] if adv and adv[0] else 0


def measure_text(metrics, text, size):
    scale = size / metrics['unitsPerEm']
    total = 0
    for ch in text:
        total += glyph_advance(metrics, metrics['cmap'].get(ord(ch), 0))
    return total * scale


def is_pictographic(cp):
    return cp in EMOJI_SINGLE or any(a <= cp <= b for a, b in EMOJI_RANGES)


def is_regional(cp):
    return 0x1F1E6 <= cp <= 0x1F1FF


def graphemes(text):
    cps = list(text)
    i = 0
    n = len(cps)
    while i < n:
        j = i + 1
        first = ord(cps[i])
        if is_regional(first) and j < n and is_regional(ord(cps[j])):
            j += 1
        while j < n:
            cp = ord(cps[j])
            if cp in (FE0F, KEYCAP) or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F or 0x0300 <= cp <= 0x036F:
                j += 1
            elif cp == ZWJ and j + 1 < n:
                j += 2
            else:
                break
        yield ''.join(cps[i:j])
        i = j


def is_emoji_cluster(cluster):
    cps = [ord(c) for c in cluster]
    if any(is_pictographic(c) for c in cps):
        return True
    return any(is_regional(c) for c in cps) or KEYCAP in cps or any(0xE0020 <= c <= 0xE007F for c in cps)


def add_entry(mp, entry):
    if entry.get('has_img_apple') is False:
        return
    if entry.get('unified'):
        mp[entry['unified'].upper()] = entry['image']
    if entry.get('non_qualified'):
        mp[entry['non_qualified'].upper()] = entry['image']
    for var in (entry.get('skin_variations') or {}).values():
        add_entry(mp, var)


async def get_emoji_map():
    if _state['emoji_map'] is not None:
        return _state['emoji_map']
    for url in EMOJI_JSON_URLS:
        try:
            res = await with_timeout(http.get(url, timeout=10, validate_status=lambda s: s == 200), 12, 'emoji.json download')
            mp = {}
            for entry in res.json():
                add_entry(mp, entry)
            _state['emoji_map'] = mp
            return mp
        except Exception:
            continue
    return None


def guess_filename(cps):
    keep = ZWJ in cps or KEYCAP in cps
    use = cps if keep else [c for c in cps if c != FE0F]
    return '-'.join(f'{c:04x}' for c in use) + '.png'


async def cluster_to_url(cluster):
    cps = [ord(c) for c in cluster]
    key = '-'.join(f'{c:04X}' for c in cps)
    key2 = '-'.join(f'{c:04X}' for c in cps if c != FE0F)
    mp = await get_emoji_map()
    filename = None
    if mp is not None:
        filename = mp.get(key) or mp.get(key2) or None
    known = bool(filename)
    if not filename:
        filename = guess_filename(cps)
    return {'filename': filename, 'url': PNG_CDN + filename, 'fallbackUrl': PNG_CDN_FALLBACK + filename,
            'likelyTooNew': mp is not None and not known}


async def parse_segments(s):
    if not s:
        return [{'type': 'text', 'value': s}]
    segs, cur = [], ''
    for cl in graphemes(s):
        if is_emoji_cluster(cl):
            if cur:
                segs.append({'type': 'text', 'value': cur})
                cur = ''
            info = await cluster_to_url(cl)
            segs.append({'type': 'emoji', 'value': cl, 'key': info['filename'], 'url': info['url'],
                         'fallbackUrl': info['fallbackUrl'], 'likelyTooNew': info['likelyTooNew']})
        else:
            cur += cl
    if cur:
        segs.append({'type': 'text', 'value': cur})
    return segs


async def fetch_once(url):
    res = await http.get(url, timeout=8, headers={'User-Agent': FETCH_UA, 'Accept': 'image/png,image/*;q=0.8,*/*;q=0.5'},
                         validate_status=lambda s: s == 200)
    ct = res.headers.get('content-type', '')
    if 'image' not in ct:
        raise RuntimeError(f'Unexpected content-type "{ct}" (server likely returned an error page, not a PNG)')
    return res.content


async def fetch_emoji_png(urls):
    last = None
    for url in urls:
        for attempt in range(2):
            try:
                if attempt:
                    await asyncio.sleep(0.7)
                return await fetch_once(url)
            except Exception as e:
                last = e
    raise last


async def load_emoji_images(word_segs):
    segs = {}
    for w in word_segs:
        for s in w:
            if s['type'] == 'emoji':
                segs[s['key']] = s
    images, failures = {}, []

    async def one(seg):
        try:
            raw = await fetch_emoji_png([seg['url'], seg['fallbackUrl']])
            flat = await jimp.read(raw)
            flat.flatten(WHITE)
            images[seg['key']] = await flat.getBuffer('image/png')
        except Exception as e:
            failures.append({'emoji': seg['value'], 'url': seg['url'], 'message': str(e), 'likelyTooNew': seg['likelyTooNew']})

    await asyncio.gather(*[one(s) for s in segs.values()])
    if segs and not images:
        if all(f['likelyTooNew'] for f in failures):
            raise RuntimeError(f"This emoji ({' '.join(f['emoji'] for f in failures)}) is probably too new and not yet supported by the emoji data used by the bot.")
        first = failures[0]
        raise RuntimeError(f"All emoji failed to load from the CDN ({len(failures)} failed). Example: {first['url']} -> {first['message']}")
    return images, failures


def buildFailureWarning(failures):
    too_new = [f for f in failures if f['likelyTooNew']]
    other = [f for f in failures if not f['likelyTooNew']]
    parts = []
    if too_new:
        parts.append(' '.join(f['emoji'] for f in too_new) + ' is probably too new and not yet supported')
    if other:
        parts.append(' '.join(f['emoji'] for f in other) + ' failed to load from the CDN')
    return f"⚠️ {len(failures)} emoji didn't show up in the sticker: " + '; '.join(parts)


def measure_segs(metrics, segs, emoji_size, font_size):
    return sum(measure_text(metrics, s['value'], font_size) if s['type'] == 'text' else emoji_size for s in segs)


def build_lines(metrics, word_segs, width, margin, spacing, size):
    lines, cur, cur_w = [], [], 0
    for i, ws in enumerate(word_segs):
        ww = measure_segs(metrics, ws, size, size)
        if ww > width - 2 * margin:
            return None
        test = cur_w + (spacing if cur else 0) + ww
        if test < width - 2 * margin or not cur:
            cur.append(i)
            cur_w = test
        else:
            lines.append(cur)
            cur = [i]
            cur_w = ww
    if cur:
        lines.append(cur)
    return lines


def calc_layout(metrics, word_segs, width, height, margin, spacing, start=110):
    size = start
    mult = 1.3
    while size > 8:
        lines = build_lines(metrics, word_segs, width, margin, spacing, size)
        if lines is not None and len(lines) * size * mult <= height - 2 * margin:
            return lines, size, size * mult
        size -= 2
    lines = build_lines(metrics, word_segs, width, margin, spacing, size)
    return (lines if lines is not None else []), size, size * mult


async def render_frame(loaded, word_segs, emoji_images, width=WIDTH, height=HEIGHT, margin=MARGIN, spacing=WORD_SPACING):
    metrics = loaded['metrics']
    lines, size, line_h = calc_layout(metrics, word_segs, width, height, margin, spacing)
    emoji_size = size
    y = (height - len(lines) * line_h) / 2
    layers = []
    resized = {}
    for idxs in lines:
        total = sum(measure_segs(metrics, word_segs[wi], emoji_size, size) for wi in idxs)
        space = (width - 2 * margin - total) / (len(idxs) - 1) if len(idxs) > 1 else 0
        x = margin
        for wi in idxs:
            for s in word_segs[wi]:
                if s['type'] == 'text':
                    ink = loaded['ttf'].renderInk(s['value'], size)
                    if ink['width'] > 0:
                        layers.append((jimp.fromBitmap(ink), js_round(x), js_round(y + 8)))
                    x += ink['width']
                else:
                    png = emoji_images.get(s['key'])
                    if png:
                        if s['key'] not in resized:
                            resized[s['key']] = await jimp.resize(png, js_round(emoji_size), js_round(emoji_size), {'out': 'png'})
                        layers.append((await jimp.read(resized[s['key']]), js_round(x), js_round(y)))
                    x += emoji_size
            x += space
        y += line_h
    base = jimp.create(width, height, WHITE)
    for src, lx, ly in layers:
        base.composite(src, lx, ly)
    base.blur(4)
    return await base.getBuffer('image/png', {'alpha': False})


def font_dir():
    d = os.path.join(os.getcwd(), 'data')
    os.makedirs(d, exist_ok=True)
    return d


def looks_like_font(buf):
    head = bytes(buf[:4])
    return any(head[:len(sig)] == sig for sig in FONT_SIGNATURES)


async def get_google_font():
    if _state['font_path']:
        return _state['font_path']
    path = os.path.join(font_dir(), 'brat-arial.ttf')
    if os.path.exists(path):
        try:
            with open(path, 'rb') as fh:
                existing = fh.read()
            if len(existing) >= 10000 and looks_like_font(existing):
                _state['font_path'] = path
                return path
        except OSError:
            pass
    res = await with_timeout(http.get(FONT_URL, timeout=10, validate_status=lambda s: s == 200), 12, 'Arial ttf download')
    data = res.content
    size = len(data) if data else 0
    if size < 10000:
        raise RuntimeError(f'Downloaded font looks invalid (size={size} bytes)')
    if not looks_like_font(data):
        ct = res.headers.get('content-type') or 'unknown'
        raise RuntimeError(f'Downloaded file is not a valid font (bad signature, content-type: {ct})')
    with open(path, 'wb') as fh:
        fh.write(data)
    _state['font_path'] = path
    return path


def warm_up_font(metrics):
    w = measure_text(metrics, 'a', 20)
    if not math.isfinite(w) or w <= 0:
        raise RuntimeError(f'Font warm-up failed: measureText returned an invalid width ({w})')


async def ensure_font_registered():
    if _state['loaded'] is not None:
        return _state['loaded']
    async with _font_lock:
        if _state['loaded'] is not None:
            return _state['loaded']
        last = None
        for attempt in range(1, 4):
            try:
                if attempt > 1:
                    _state['font_path'] = None
                path = await get_google_font()
                with open(path, 'rb') as fh:
                    data = fh.read()
                metrics = parse_font(data)
                loaded = {'metrics': metrics, 'path': path, 'ttf': jimp.loadFontBuffer(data)}
                warm_up_font(metrics)
                _state['loaded'] = loaded
                return loaded
            except Exception as e:
                last = e
                _state['font_path'] = None
                _state['loaded'] = None
                if attempt < 3:
                    await asyncio.sleep(1.0 * attempt)
        raise last


async def generateBratImage(text):
    loaded = await ensure_font_registered()
    words = text.split(' ')
    word_segs = list(await asyncio.gather(*[parse_segments(w) for w in words]))
    emoji_images, failures = await load_emoji_images(word_segs)
    buffer = await render_frame(loaded, word_segs, emoji_images)
    return {'buffer': buffer, 'failures': failures}


async def generateBratVideo(text):
    loaded = await ensure_font_registered()
    words = text.split(' ')
    word_segs = list(await asyncio.gather(*[parse_segments(w) for w in words]))
    tmp_dir = os.path.join(tempfile.gettempdir(), f'bratv_{int(time.time() * 1000)}_{os.urandom(3).hex()}')
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        fps = 10
        frame_duration = 0.5
        repeat = js_round(fps * frame_duration)
        emoji_images, failures = await load_emoji_images(word_segs)
        for i in range(len(words)):
            frame = await render_frame(loaded, word_segs[:i + 1], emoji_images)
            for r in range(repeat):
                idx = i * repeat + r
                with open(os.path.join(tmp_dir, f'frame_{idx:05d}.png'), 'wb') as fh:
                    fh.write(frame)
        out = os.path.join(tmp_dir, 'brat.webp')
        try:
            await ff.ffmpeg(['-framerate', str(fps), '-i', os.path.join(tmp_dir, 'frame_%05d.png'),
                             '-vf', 'scale=512:512', '-loop', '0', out], timeout=30)
        except asyncio.TimeoutError:
            raise RuntimeError('Timeout after 30000ms: ffmpeg encode') from None
        with open(out, 'rb') as fh:
            buffer = fh.read()
        return {'buffer': buffer, 'failures': failures}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


parseSegments = parse_segments
loadEmojiImages = load_emoji_images
