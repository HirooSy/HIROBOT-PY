import asyncio
import base64
import json
import math
import os
import re
import struct
import urllib.request
import zlib
from array import array
from types import SimpleNamespace

from lib.compat import ffmpeg as ff, http
from lib.utils.helper import file_type_from_buffer


def parse_riff_chunks(buf):
    if buf[0:4] != b'RIFF' or buf[8:12] != b'WEBP':
        raise ValueError('Not a valid WebP file')
    chunks = []
    off = 12
    while off < len(buf):
        four = buf[off:off + 4].decode('ascii', errors='replace')
        size = struct.unpack_from('<I', buf, off + 4)[0]
        start = off + 8
        chunks.append((four, buf[start:start + size]))
        off = start + size + (size % 2)
    return chunks


def build_webp(chunks):
    parts = []
    for four, data in chunks:
        parts.append(four.encode('ascii') + struct.pack('<I', len(data)) + data)
        if len(data) % 2:
            parts.append(b'\x00')
    body = b''.join(parts)
    return b'RIFF' + struct.pack('<I', len(body) + 4) + b'WEBP' + body


def webp_dimensions(four, data):
    if four == 'VP8 ':
        w, h = struct.unpack_from('<HH', data, 6)
        return w & 0x3fff, h & 0x3fff
    if four == 'VP8L':
        b = data
        w = 1 + (((b[2] & 0x3f) << 8) | b[1])
        h = 1 + (((b[4] & 0x0f) << 10) | (b[3] << 2) | ((b[2] & 0xc0) >> 6))
        return w, h
    return 512, 512


def set_exif(webp, exif):
    chunks = [c for c in parse_riff_chunks(webp) if c[0] != 'EXIF']
    chunks.append(('EXIF', exif))
    idx = next((i for i, c in enumerate(chunks) if c[0] == 'VP8X'), -1)
    if idx != -1:
        vp8x = bytearray(chunks[idx][1])
        vp8x[0] |= 0x08
        chunks[idx] = ('VP8X', bytes(vp8x))
    else:
        vp = next((c for c in chunks if c[0] in ('VP8 ', 'VP8L')), None)
        if vp:
            w, h = webp_dimensions(*vp)
            vp8x = bytearray(10)
            vp8x[0] = 0x08
            vp8x[4:7] = (w - 1).to_bytes(3, 'little')
            vp8x[7:10] = (h - 1).to_bytes(3, 'little')
            chunks.insert(0, ('VP8X', bytes(vp8x)))
    chunks.sort(key=lambda c: 0 if c[0] == 'VP8X' else 1)
    return build_webp(chunks)


def get_exif(webp):
    for four, data in parse_riff_chunks(webp):
        if four == 'EXIF':
            return bytes(data)
    return None


async def to_webp(img, url=None):
    if url:
        r = await http.get(url, timeout=120, raise_for_status=False)
        if r.status != 200:
            raise RuntimeError(r.text())
        img = r.content
    kind = file_type_from_buffer(img) or {'mime': 'application/octet-stream', 'ext': 'bin'}
    if kind['ext'] == 'bin':
        raise ValueError('Unsupported file type')
    if re.search('webp', kind['mime'], re.I):
        return img
    vf = ("scale='min(320,iw)':min'(320,ih)':force_original_aspect_ratio=decrease,fps=15, pad=320:320:-1:-1:color=white@0.0, "
          "split [a][b]; [a] palettegen=reserve_transparent=on:transparency_color=ffffff [p]; [b][p] paletteuse")
    src = ff.tmp_path(kind['ext'])
    dst = src + '.webp'
    with open(src, 'wb') as fh:
        fh.write(img)
    try:
        args = []
        if re.search('video', kind['mime'], re.I):
            args += ['-f', kind['ext']]
        args += ['-i', src, '-vcodec', 'libwebp', '-vf', vf, dst]
        await ff.ffmpeg(args)
        with open(dst, 'rb') as fh:
            return fh.read()
    finally:
        for p in (src, dst):
            try:
                os.unlink(p)
            except OSError:
                pass


def add_exif(webp, packname='', author='', categories=None, extra=None):
    meta = {
        'sticker-pack-id': os.urandom(32).hex(), 'sticker-pack-name': packname,
        'sticker-pack-publisher': author, 'emojis': categories if categories is not None else [''], **(extra or {}),
    }
    attr = bytes([0x49, 0x49, 0x2A, 0x00, 0x08, 0x00, 0x00, 0x00, 0x01, 0x00, 0x41, 0x57, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x16, 0x00, 0x00, 0x00])
    jb = json.dumps(meta, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    exif = bytearray(attr + jb)
    exif[14:18] = len(jb).to_bytes(4, 'little')
    try:
        return set_exif(webp, bytes(exif))
    except Exception:
        return webp


def is_webp(buf):
    return len(buf) >= 12 and buf[:4] == b'RIFF' and buf[8:12] == b'WEBP'


def is_animated_webp(buf):
    if not is_webp(buf):
        return False
    offset = 12
    while offset < len(buf) - 8:
        chunk = bytes(buf[offset:offset + 4]).decode('ascii', errors='replace')
        size = struct.unpack_from('<I', buf, offset + 4)[0]
        if chunk == 'VP8X' and (buf[offset + 8] & 0x02 if offset + 8 < len(buf) else 0):
            return True
        if chunk in ('ANIM', 'ANMF'):
            return True
        offset += 8 + size + (size % 2)
    return False

SS = 4
PNG_SIG = b'\x89PNG\r\n\x1a\n'
FORMAT_MIME = {'png': 'image/png', 'jpeg': 'image/jpeg', 'gif': 'image/gif', 'bmp': 'image/bmp', 'webp': 'image/webp'}
MIME = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'BMP': 'image/bmp', 'WEBP': 'image/webp'}
NAMED_COLORS = {
    'black': '#000000', 'white': '#ffffff', 'red': '#ff0000', 'green': '#008000',
    'blue': '#0000ff', 'yellow': '#ffff00', 'gray': '#808080', 'grey': '#808080',
    'orange': '#ffa500', 'purple': '#800080', 'pink': '#ffc0cb', 'brown': '#a52a2a',
    'cyan': '#00ffff', 'magenta': '#ff00ff', 'lime': '#00ff00', 'navy': '#000080',
    'transparent': 'rgba(0,0,0,0)',
}
MAX_PIXELS = 100_000_000
PASSES7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
SRGB_TO_LIN = [((i / 255) / 12.92 if i / 255 <= 0.04045 else (((i / 255) + 0.055) / 1.055) ** 2.4) for i in range(256)]
REGISTERED_FONTS = {}
FONT_CACHE = {}
DEFAULT_FONT = {'obj': None}
DEFAULT_FONT_URL = 'https://github.com/HirooSy/HirooSy/raw/refs/heads/main/arial.ttf'
FONT_SIGNATURES = (b'\x00\x01\x00\x00', b'OTTO', b'true', b'ttcf')


def clamp255(v):
    return 0 if v < 0 else 255 if v > 255 else int(v)


def lin_to_srgb8(lum):
    v = lum * 12.92 if lum <= 0.0031308 else 1.055 * (lum ** (1 / 2.4)) - 0.055
    return clamp255(int(v * 255 + 0.5))


def _int_prefix(s, cast):
    m = re.match(r'\s*[+-]?(?:\d+\.?\d*|\.\d+)', s)
    return cast(float(m.group(0))) if m else 0


def parse_color(value):
    if value is None:
        return (0, 0, 0, 1.0)
    s = str(value).strip().lower()
    s = NAMED_COLORS.get(s, s)
    m = re.fullmatch(r'#([0-9a-f]{3})', s)
    if m:
        r, g, b = (int(c * 2, 16) for c in m.group(1))
        return (r, g, b, 1.0)
    m = re.fullmatch(r'#([0-9a-f]{6})', s)
    if m:
        n = int(m.group(1), 16)
        return ((n >> 16) & 255, (n >> 8) & 255, n & 255, 1.0)
    m = re.fullmatch(r'#([0-9a-f]{8})', s)
    if m:
        n = int(m.group(1), 16)
        return ((n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, (n & 255) / 255)
    m = re.fullmatch(r'rgba?\((.*)\)', s)
    if m:
        parts = [p.strip() for p in m.group(1).split(',')]
        get = lambda i: parts[i] if len(parts) > i else ''
        a = _int_prefix(get(3), float) if len(parts) > 3 and get(3) else 1.0
        return (clamp255(_int_prefix(get(0), int)), clamp255(_int_prefix(get(1), int)), clamp255(_int_prefix(get(2), int)), a)
    return (0, 0, 0, 1.0)


def color_int(c):
    if isinstance(c, (int, float)):
        return int(c) & 0xFFFFFFFF
    if isinstance(c, (tuple, list)):
        r = clamp255(c[0])
        g = clamp255(c[1]) if len(c) > 1 else 0
        b = clamp255(c[2]) if len(c) > 2 else 0
        if len(c) > 3:
            a = c[3]
            a255 = int(a * 255 + 0.5) if isinstance(a, float) and 0 <= a <= 1 else clamp255(a)
        else:
            a255 = 255
        return ((r << 24) | (g << 16) | (b << 8) | clamp255(a255)) & 0xFFFFFFFF
    if isinstance(c, dict):
        r = int(c.get('r', 0)) & 255
        g = int(c.get('g', 0)) & 255
        b = int(c.get('b', 0)) & 255
        a_val = c.get('a')
        alpha = c.get('alpha')
        any_a = a_val if a_val is not None else (alpha if alpha is not None else 1)
        if alpha is not None and any_a <= 1:
            a255 = int(alpha * 255 + 0.5)
        else:
            a255 = a_val if a_val is not None else 255
        return ((r << 24) | (g << 16) | (b << 8) | (clamp255(a255))) & 0xFFFFFFFF
    r, g, b, a = parse_color(c)
    return ((r << 24) | (g << 16) | (b << 8) | int(a * 255 + 0.5)) & 0xFFFFFFFF


def unpack_color(c):
    v = color_int(c)
    return ((v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255)


def read_be(buf, off, n):
    return int.from_bytes(buf[off:off + n], 'big')


def read_le(buf, off, n):
    return int.from_bytes(buf[off:off + n], 'little')


def read_signed_le(buf, off, n):
    return int.from_bytes(buf[off:off + n], 'little', signed=True)


def detect_format(buf):
    if not buf or len(buf) < 4:
        return None
    head = bytes(buf[:12])
    if head[:4] == b'\x89PNG':
        return 'png'
    if head[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if head[:3] == b'GIF':
        return 'gif'
    if head[:2] == b'BM':
        return 'bmp'
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    return None


def read_dimensions(buf):
    fmt = detect_format(buf)
    try:
        if fmt == 'png':
            return {'width': read_be(buf, 16, 4), 'height': read_be(buf, 20, 4)}
        if fmt == 'gif':
            return {'width': read_le(buf, 6, 2), 'height': read_le(buf, 8, 2)}
        if fmt == 'bmp':
            if read_le(buf, 14, 4) == 12:
                return {'width': read_le(buf, 18, 2), 'height': read_le(buf, 20, 2)}
            return {'width': read_signed_le(buf, 18, 4), 'height': abs(read_signed_le(buf, 22, 4))}
        if fmt == 'jpeg':
            p = 2
            while p < len(buf):
                if buf[p] != 0xFF:
                    p += 1
                    continue
                m = buf[p + 1]
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    return {'width': read_be(buf, p + 7, 2), 'height': read_be(buf, p + 5, 2)}
                p += 2 + read_be(buf, p + 2, 2)
        if fmt == 'webp':
            t = bytes(buf[12:16])
            if t == b'VP8X':
                return {'width': 1 + read_le(buf, 24, 3), 'height': 1 + read_le(buf, 27, 3)}
            if t == b'VP8L':
                b = read_le(buf, 21, 4)
                return {'width': (b & 0x3FFF) + 1, 'height': ((b >> 14) & 0x3FFF) + 1}
            if t == b'VP8 ':
                return {'width': read_le(buf, 26, 2) & 0x3FFF, 'height': read_le(buf, 28, 2) & 0x3FFF}
    except IndexError:
        return None
    return None


def get_exif_orientation(buf):
    if detect_format(buf) != 'jpeg':
        return 1
    p = 2
    while p + 4 < len(buf):
        if buf[p] != 0xFF:
            return 1
        marker = buf[p + 1]
        length = read_be(buf, p + 2, 2)
        if marker == 0xE1 and bytes(buf[p + 4:p + 8]) == b'Exif':
            t = p + 10
            le = bytes(buf[t:t + 2]) == b'II'
            r16 = (lambda o: read_le(buf, o, 2)) if le else (lambda o: read_be(buf, o, 2))
            r32 = (lambda o: read_le(buf, o, 4)) if le else (lambda o: read_be(buf, o, 4))
            ifd = t + r32(t + 4)
            n = r16(ifd)
            for i in range(n):
                e = ifd + 2 + i * 12
                if r16(e) == 0x0112:
                    return r16(e + 8)
            return 1
        if marker == 0xDA:
            return 1
        p += 2 + length
    return 1


def png_chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_encode(width, height, data, level=6, has_alpha=True):
    data = bytes(data)
    if has_alpha:
        ctype = 6
        src = data
        stride = width * 4
    else:
        ctype = 2
        rgb = bytearray(width * height * 3)
        rgb[0::3] = data[0::4]
        rgb[1::3] = data[1::4]
        rgb[2::3] = data[2::4]
        src = bytes(rgb)
        stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += src[y * stride:(y + 1) * stride]
    ihdr = struct.pack('>IIBBBBB', width, height, 8, ctype, 0, 0, 0)
    return PNG_SIG + png_chunk(b'IHDR', ihdr) + png_chunk(b'IDAT', zlib.compress(bytes(raw), level)) + png_chunk(b'IEND', b'')


def png_unfilter(raw, pos, sw, sh, bpp, stride):
    rows = []
    prev = bytearray(stride)
    for _ in range(sh):
        if pos >= len(raw):
            raise ValueError('PNG: data IDAT kurang')
        ft = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if len(line) != stride:
            raise ValueError('PNG: data IDAT kurang')
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                if i >= bpp:
                    a = line[i - bpp]
                    c = prev[i - bpp]
                else:
                    a = 0
                    c = 0
                b = prev[i]
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        elif ft != 0:
            raise ValueError(f'PNG: filter type {ft} tidak valid')
        rows.append(line)
        prev = line
    return rows, pos


def png_samples(row, count, depth):
    if depth == 8:
        return row[:count]
    if depth == 16:
        return [(row[2 * i] << 8) | row[2 * i + 1] for i in range(count)]
    per = 8 // depth
    mask = (1 << depth) - 1
    out = []
    for byte in row:
        for k in range(per):
            out.append((byte >> (8 - depth * (k + 1))) & mask)
    return out[:count]


def png_rows_to_rgba(rows, sw, ctype, depth, pal, trns):
    chans = PNG_CHANNELS[ctype]
    out = bytearray(sw * len(rows) * 4)
    maxv = (1 << depth) - 1
    pal4 = []
    if ctype == 3:
        palette = pal or b''
        for i in range(len(palette) // 3):
            alpha = trns[i] if trns and i < len(trns) else 255
            pal4.append(bytes((palette[3 * i], palette[3 * i + 1], palette[3 * i + 2], alpha)))
    gray_key = None
    rgb_key = None
    if trns:
        if ctype == 0 and len(trns) >= 2:
            gray_key = int.from_bytes(trns[:2], 'big')
        elif ctype == 2 and len(trns) >= 6:
            rgb_key = tuple(int.from_bytes(trns[k:k + 2], 'big') for k in (0, 2, 4))
    for y, row in enumerate(rows):
        base = y * sw * 4
        if ctype == 3:
            idx = png_samples(row, sw, depth)
            for i in range(sw):
                v = idx[i]
                px = pal4[v] if v < len(pal4) else b'\x00\x00\x00\xff'
                out[base + 4 * i:base + 4 * i + 4] = px
            continue
        if depth == 8 and gray_key is None and rgb_key is None:
            if ctype == 6:
                out[base:base + sw * 4] = row[:sw * 4]
                continue
            if ctype == 2:
                out[base:base + sw * 4:4] = row[0:sw * 3:3]
                out[base + 1:base + sw * 4:4] = row[1:sw * 3:3]
                out[base + 2:base + sw * 4:4] = row[2:sw * 3:3]
                out[base + 3:base + sw * 4:4] = b'\xff' * sw
                continue
            if ctype == 0:
                out[base:base + sw * 4:4] = row[:sw]
                out[base + 1:base + sw * 4:4] = row[:sw]
                out[base + 2:base + sw * 4:4] = row[:sw]
                out[base + 3:base + sw * 4:4] = b'\xff' * sw
                continue
        samples = png_samples(row, sw * chans, depth)
        for i in range(sw):
            s = samples[i * chans:(i + 1) * chans]
            if ctype == 0:
                g = s[0]
                a = 0 if (gray_key is not None and g == gray_key) else 255
                v8 = clamp255(g * 255 // maxv) if depth < 8 else (g >> 8 if depth == 16 else g)
                px = (v8, v8, v8, a)
            elif ctype == 4:
                if depth == 16:
                    px = (s[0] >> 8, s[0] >> 8, s[0] >> 8, s[1] >> 8)
                elif depth < 8:
                    v8 = s[0] * 255 // maxv
                    px = (v8, v8, v8, s[1] * 255 // maxv)
                else:
                    px = (s[0], s[0], s[0], s[1])
            elif ctype == 2:
                if rgb_key is not None and tuple(s) == rgb_key:
                    a = 0
                else:
                    a = 255
                if depth == 16:
                    px = (s[0] >> 8, s[1] >> 8, s[2] >> 8, a)
                elif depth < 8:
                    px = (s[0] * 255 // maxv, s[1] * 255 // maxv, s[2] * 255 // maxv, a)
                else:
                    px = (s[0], s[1], s[2], a)
            else:
                if depth == 16:
                    px = (s[0] >> 8, s[1] >> 8, s[2] >> 8, s[3] >> 8)
                else:
                    px = (s[0], s[1], s[2], s[3])
            out[base + 4 * i:base + 4 * i + 4] = bytes(px)
    return out


def png_decode(buf):
    buf = bytes(buf)
    if buf[:8] != PNG_SIG:
        raise ValueError('PNG: signature tidak valid')
    pos = 8
    idat = []
    ihdr = None
    pal = None
    trns = None
    while pos + 8 <= len(buf):
        ln = read_be(buf, pos, 4)
        kind = buf[pos + 4:pos + 8]
        data = buf[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if kind == b'IHDR':
            ihdr = struct.unpack('>IIBBBBB', data[:13])
        elif kind == b'PLTE':
            pal = data
        elif kind == b'tRNS':
            trns = data
        elif kind == b'IDAT':
            idat.append(data)
        elif kind == b'IEND':
            break
    if ihdr is None:
        raise ValueError('PNG: IHDR tidak ada')
    w, h, depth, ctype, _, _, interlace = ihdr
    if w <= 0 or h <= 0:
        raise ValueError('PNG: dimensi tidak valid')
    if ctype not in PNG_CHANNELS:
        raise ValueError('PNG: color type tidak valid')
    chans = PNG_CHANNELS[ctype]
    bpp = max(1, chans * depth // 8)
    raw = zlib.decompress(b''.join(idat))
    if interlace == 0:
        stride = (w * chans * depth + 7) // 8
        rows, _ = png_unfilter(raw, 0, w, h, bpp, stride)
        return {'data': png_rows_to_rgba(rows, w, ctype, depth, pal, trns), 'width': w, 'height': h}
    out = bytearray(w * h * 4)
    pos = 0
    for x0, y0, dx, dy in PASSES7:
        pw = (w - x0 + dx - 1) // dx
        ph = (h - y0 + dy - 1) // dy
        if pw <= 0 or ph <= 0:
            continue
        stride = (pw * chans * depth + 7) // 8
        rows, pos = png_unfilter(raw, pos, pw, ph, bpp, stride)
        sub = png_rows_to_rgba(rows, pw, ctype, depth, pal, trns)
        for j in range(ph):
            yy = y0 + j * dy
            for i in range(pw):
                xx = x0 + i * dx
                d = (yy * w + xx) * 4
                s = (j * pw + i) * 4
                out[d:d + 4] = sub[s:s + 4]
    return {'data': out, 'width': w, 'height': h}


def bmp_encode(bm):
    width, height, data = bm['width'], bm['height'], bm['data']
    stride = ((24 * width + 31) // 32) * 4
    size = 54 + stride * height
    out = bytearray(size)
    out[0:2] = b'BM'
    struct.pack_into('<I', out, 2, size)
    struct.pack_into('<I', out, 10, 54)
    struct.pack_into('<I', out, 14, 40)
    struct.pack_into('<i', out, 18, width)
    struct.pack_into('<i', out, 22, height)
    struct.pack_into('<H', out, 26, 1)
    struct.pack_into('<H', out, 28, 24)
    struct.pack_into('<I', out, 34, stride * height)
    for y in range(height):
        drow = 54 + (height - 1 - y) * stride
        srow = y * width * 4
        for x in range(width):
            s = srow + x * 4
            d = drow + x * 3
            a = data[s + 3] / 255
            out[d] = clamp255(data[s + 2] * a + 255 * (1 - a) + 0.5)
            out[d + 1] = clamp255(data[s + 1] * a + 255 * (1 - a) + 0.5)
            out[d + 2] = clamp255(data[s] * a + 255 * (1 - a) + 0.5)
    return bytes(out)


def flatten_bitmap(bm, bg=0xFFFFFFFF):
    c = color_int(bg)
    br = (c >> 24) & 255
    bgc = (c >> 16) & 255
    bb = (c >> 8) & 255
    d = bm['data']
    for i in range(0, len(d), 4):
        a = d[i + 3]
        if a == 255:
            continue
        af = a / 255
        d[i] = clamp255(d[i] * af + br * (1 - af) + 0.5)
        d[i + 1] = clamp255(d[i + 1] * af + bgc * (1 - af) + 0.5)
        d[i + 2] = clamp255(d[i + 2] * af + bb * (1 - af) + 0.5)
        d[i + 3] = 255
    return bm


def over_px(D, i, r, g, b, sa):
    da = D[i + 3] / 255
    oa = sa + da - sa * da
    if oa <= 0:
        return
    k = sa / oa
    inv = 1 - k
    D[i] = clamp255(r * k + D[i] * inv + 0.5)
    D[i + 1] = clamp255(g * k + D[i + 1] * inv + 0.5)
    D[i + 2] = clamp255(b * k + D[i + 2] * inv + 0.5)
    D[i + 3] = clamp255(oa * 255 + 0.5)


def blit(dst, src, x, y, opacity=1.0):
    x = int(round(x))
    y = int(round(y))
    dw, dh = dst['width'], dst['height']
    sw, sh = src['width'], src['height']
    D = dst['data']
    S = src['data']
    for sy in range(sh):
        dy = y + sy
        if dy < 0 or dy >= dh:
            continue
        for sx in range(sw):
            dx = x + sx
            if dx < 0 or dx >= dw:
                continue
            si = (sy * sw + sx) * 4
            sa = (S[si + 3] / 255) * opacity
            if sa <= 0:
                continue
            over_px(D, (dy * dw + dx) * 4, S[si], S[si + 1], S[si + 2], sa)


def scale_bitmap_sync(bm, dw, dh, kernel='lanczos3'):
    if bm['width'] == dw and bm['height'] == dh:
        return {'data': bytearray(bm['data']), 'width': dw, 'height': dh}
    flags = 'bilinear' if kernel == 'bilinear' else 'lanczos'
    out = ff.filter_raw_sync(bm['data'], bm['width'], bm['height'], f'scale={dw}:{dh}:flags={flags}')
    if len(out) != dw * dh * 4:
        raise RuntimeError('resize: ukuran hasil ffmpeg tidak sesuai')
    return {'data': bytearray(out), 'width': dw, 'height': dh}


def gaussian_blur_sync(bm, sigma):
    if sigma <= 0:
        return bm
    out = ff.filter_raw_sync(bm['data'], bm['width'], bm['height'], f'gblur=sigma={sigma}:steps=3')
    return {'data': bytearray(out), 'width': bm['width'], 'height': bm['height']}


def encode_sync(bm, mime, options=None):
    options = options or {}
    w, h = bm['width'], bm['height']
    if mime == 'image/png':
        level = options.get('deflateLevel')
        return png_encode(w, h, bm['data'], 6 if level is None else level, options.get('alpha') is not False)
    if mime in ('image/jpeg', 'image/jpg'):
        q = options.get('quality')
        q = 80 if q is None else q
        bg = options.get('background')
        flat = flatten_bitmap({'data': bytearray(bm['data']), 'width': w, 'height': h}, 0xFFFFFFFF if bg is None else bg)
        D = flat['data']
        rgb = bytearray(w * h * 3)
        rgb[0::3] = D[0::4]
        rgb[1::3] = D[1::4]
        rgb[2::3] = D[2::4]
        qv = max(2, min(31, int(round(2 + (100 - q) * 29 / 100))))
        return ff.encode_raw_sync(bytes(rgb), w, h, ['-f', 'mjpeg', '-q:v', str(qv)], pix_fmt='rgb24')
    if mime == 'image/bmp':
        return bmp_encode(bm)
    if mime == 'image/webp':
        q = options.get('quality')
        q = 80 if q is None else q
        if options.get('lossless'):
            opts = ['-lossless', '1']
        else:
            opts = ['-quality', str(int(q))]
        return ff.encode_raw_sync(bytes(bm['data']), w, h, ['-c:v', 'libwebp', *opts, '-f', 'webp'], pix_fmt='rgba')
    raise ValueError(f'Jimp.getBuffer: MIME tidak didukung: {mime}')


def from_buffer_sync(buf):
    fmt = detect_format(buf)
    if not fmt:
        raise ValueError('Jimp: format gambar tidak dikenali')
    if fmt == 'webp':
        raise ValueError('Jimp: WebP diproses lewat ffmpeg (gunakan Jimp.read yang otomatis mengonversi)')
    dim = read_dimensions(buf)
    if not dim or not (dim['width'] > 0 and dim['height'] > 0):
        raise ValueError('Jimp: dimensi gambar tidak valid')
    if dim['width'] * dim['height'] > MAX_PIXELS:
        raise ValueError(f"Jimp: gambar terlalu besar ({dim['width']}x{dim['height']} = {dim['width'] * dim['height'] / 1e6:.0f} MP, batas {MAX_PIXELS / 1e6:.0f} MP)")
    if fmt == 'png':
        bm = png_decode(buf)
    else:
        data = ff.decode_rgba_sync(buf)
        if len(data) != dim['width'] * dim['height'] * 4:
            raise ValueError('Jimp: hasil decode tidak sesuai dimensi gambar')
        bm = {'data': bytearray(data), 'width': dim['width'], 'height': dim['height']}
    img = Jimp({'data': bm['data'], 'width': bm['width'], 'height': bm['height']})
    img.mime = FORMAT_MIME[fmt]
    if fmt == 'jpeg':
        img._apply_exif(get_exif_orientation(buf))
    return img


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as res:
        if res.status != 200:
            raise ValueError(f'Jimp.read: HTTP {res.status} untuk {url}')
        return res.read()


def read_sync(src):
    if isinstance(src, (bytes, bytearray, memoryview)):
        buf = bytes(src)
    elif isinstance(src, str):
        if re.match(r'https?://', src, re.I):
            buf = fetch_bytes(src)
        elif re.match(r'data:', src, re.I):
            buf = base64.b64decode(src.split(',')[1] if ',' in src else '')
        else:
            with open(src, 'rb') as fh:
                buf = fh.read()
    else:
        raise ValueError('Jimp.read: sumber harus Buffer, path, URL, atau data URI')
    if detect_format(buf) == 'webp':
        buf = webp_to_png_sync(buf)
    return from_buffer_sync(buf)


def webp_to_png_sync(buf):
    return ff.transcode_sync(buf, ['-f', 'webp_pipe'], ['-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png'])


def png_to_webp_sync(png, quality=80, lossless=False):
    if lossless:
        opts = ['-lossless', '1']
    else:
        opts = ['-quality', str(int(80 if quality is None else quality))]
    return ff.transcode_sync(png, ['-f', 'png_pipe'], ['-vcodec', 'libwebp', *opts, '-f', 'webp'])


def to_raw_sync(input_, channels=4):
    img = read_sync(input_)
    src = img.bitmap['data']
    w, h = img.width, img.height
    n = w * h
    if channels == 4:
        data = bytes(src)
    elif channels == 3:
        out = bytearray(n * 3)
        out[0::3] = src[0::4]
        out[1::3] = src[1::4]
        out[2::3] = src[2::4]
        data = bytes(out)
    elif channels == 1:
        out = bytearray(n)
        for i in range(n):
            out[i] = clamp255(src[i * 4] * 0.2126 + src[i * 4 + 1] * 0.7152 + src[i * 4 + 2] * 0.0722 + 0.5)
        data = bytes(out)
    else:
        raise ValueError('toRaw: channels harus 1, 3, atau 4')
    return {'data': data, 'info': {'width': w, 'height': h, 'channels': channels}}


class Jimp:
    MAX_PIXELS = MAX_PIXELS

    def __init__(self, opts=None):
        opts = opts or {}
        if opts.get('data') is not None:
            data = opts['data']
            if not isinstance(data, bytearray):
                data = bytearray(data)
            self.bitmap = {'data': data, 'width': opts['width'], 'height': opts['height']}
        else:
            width = opts.get('width')
            height = opts.get('height')
            if not (width and height and width > 0 and height > 0):
                raise ValueError('Jimp: width dan height harus > 0')
            c = unpack_color(opts.get('color', 0))
            self.bitmap = {'data': bytearray(bytes(c) * (width * height)), 'width': width, 'height': height}
        self.mime = None
        self.background = 0

    @property
    def width(self):
        return self.bitmap['width']

    @property
    def height(self):
        return self.bitmap['height']

    async def getBuffer(self, mime='image/png', options=None):
        return await asyncio.to_thread(encode_sync, self.bitmap, mime, options)

    async def getBase64(self, mime, options=None):
        return 'data:' + mime + ';base64,' + base64.b64encode(await self.getBuffer(mime, options)).decode()

    async def write(self, path, options=None):
        ext = str(path).split('.')[-1].lower()
        mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'bmp': 'image/bmp', 'webp': 'image/webp'}.get(ext)
        if not mime:
            raise ValueError('Jimp.write: ekstensi tidak didukung: ' + ext)
        data = await self.getBuffer(mime, options)
        await asyncio.to_thread(_write_file, path, data)

    def clone(self):
        return Jimp({'data': bytearray(self.bitmap['data']), 'width': self.width, 'height': self.height})

    def _apply_exif(self, o):
        if o == 2:
            self.flip(horizontal=True)
        elif o == 3:
            self.rotate(180)
        elif o == 4:
            self.flip(vertical=True)
        elif o == 5:
            self.rotate(90)
            self.flip(horizontal=True)
        elif o == 6:
            self.rotate(90)
        elif o == 7:
            self.rotate(270)
            self.flip(horizontal=True)
        elif o == 8:
            self.rotate(270)
        return self

    def getPixelColor(self, x, y):
        i = (round(y) * self.width + round(x)) * 4
        return int.from_bytes(bytes(self.bitmap['data'][i:i + 4]), 'big')

    def setPixelColor(self, color, x, y):
        i = (round(y) * self.width + round(x)) * 4
        self.bitmap['data'][i:i + 4] = color_int(color).to_bytes(4, 'big')
        return self

    def scan(self, x, y, w, h, fn):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                fn(self, xx, yy, (self.width * yy + xx) << 2)
        return self

    def resize(self, a, b=None, c=None):
        if isinstance(a, dict):
            w = a.get('w')
            h = a.get('h')
            opts = {k: v for k, v in a.items() if k not in ('w', 'h')}
        else:
            w = a
            h = b
            opts = c or {}
        if w is None and h is None:
            raise ValueError('resize: w atau h wajib diisi')
        if w is None:
            w = round(self.width * (h / self.height))
        if h is None:
            h = round(self.height * (w / self.width))
        w = max(1, int(round(w)))
        h = max(1, int(round(h)))
        fit = opts.get('fit') or 'fill'
        bg = opts.get('background')
        bg = (0, 0, 0, 0) if bg is None else unpack_color(bg)
        kernel = opts.get('kernel') or 'lanczos3'
        position = str(opts.get('position') or 'center').lower()
        fx = 0 if re.search(r'left|west', position) else 1 if re.search(r'right|east', position) else 0.5
        fy = 0 if re.search(r'top|north', position) else 1 if re.search(r'bottom|south', position) else 0.5
        if fit == 'fill':
            self.bitmap = scale_bitmap_sync(self.bitmap, w, h, kernel)
        elif fit in ('inside', 'outside'):
            sc = min(w / self.width, h / self.height) if fit == 'inside' else max(w / self.width, h / self.height)
            if fit == 'inside' and opts.get('withoutEnlargement') and sc > 1:
                return self
            self.bitmap = scale_bitmap_sync(self.bitmap, max(1, round(self.width * sc)), max(1, round(self.height * sc)), kernel)
        elif fit == 'cover':
            sc = max(w / self.width, h / self.height)
            rw = max(w, round(self.width * sc))
            rh = max(h, round(self.height * sc))
            self.bitmap = scale_bitmap_sync(self.bitmap, rw, rh, kernel)
            self.crop(math.ceil((rw - w) * fx), math.ceil((rh - h) * fy), w, h)
        elif fit == 'contain':
            sc = min(w / self.width, h / self.height)
            rw = max(1, round(self.width * sc))
            rh = max(1, round(self.height * sc))
            scaled = scale_bitmap_sync(self.bitmap, rw, rh, kernel)
            canvas_bm = {'data': bytearray(bytes(bg) * (w * h)), 'width': w, 'height': h}
            blit(canvas_bm, scaled, math.floor((w - rw) * fx), math.floor((h - rh) * fy))
            self.bitmap = canvas_bm
        else:
            raise ValueError('resize: fit tidak dikenal: ' + fit)
        return self

    def scale(self, f):
        return self.resize(round(self.width * f), round(self.height * f))

    def crop(self, x, y=None, w=None, h=None):
        if isinstance(x, dict):
            x, y, w, h = x['x'], x['y'], x['w'], x['h']
        x, y, w, h = int(round(x)), int(round(y)), int(round(w)), int(round(h))
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > self.width or y + h > self.height:
            raise ValueError(f'crop: area di luar gambar (gambar {self.width}x{self.height}, area {x},{y} {w}x{h})')
        D = self.bitmap['data']
        W = self.width
        out = bytearray(w * h * 4)
        for r in range(h):
            s = ((y + r) * W + x) * 4
            out[r * w * 4:(r + 1) * w * 4] = D[s:s + w * 4]
        self.bitmap = {'data': out, 'width': w, 'height': h}
        return self

    def flatten(self, bg=0xFFFFFFFF):
        flatten_bitmap(self.bitmap, bg)
        return self

    def greyscale(self):
        d = self.bitmap['data']
        for i in range(0, len(d), 4):
            y = 0.2126 * SRGB_TO_LIN[d[i]] + 0.7152 * SRGB_TO_LIN[d[i + 1]] + 0.0722 * SRGB_TO_LIN[d[i + 2]]
            g = lin_to_srgb8(y)
            d[i] = d[i + 1] = d[i + 2] = g
        return self

    def grayscale(self):
        return self.greyscale()

    def invert(self):
        d = self.bitmap['data']
        for i in range(0, len(d), 4):
            d[i] = 255 - d[i]
            d[i + 1] = 255 - d[i + 1]
            d[i + 2] = 255 - d[i + 2]
        return self

    def opacity(self, f):
        d = self.bitmap['data']
        for i in range(3, len(d), 4):
            d[i] = int(round(d[i] * f))
        return self

    def brightness(self, v):
        d = self.bitmap['data']
        for i in range(0, len(d), 4):
            d[i] = clamp255(d[i] * v)
            d[i + 1] = clamp255(d[i + 1] * v)
            d[i + 2] = clamp255(d[i + 2] * v)
        return self

    def blur(self, sigma):
        self.bitmap = gaussian_blur_sync(self.bitmap, sigma)
        return self

    def gaussian(self, sigma):
        return self.blur(sigma)

    def flip(self, horizontal=False, vertical=False):
        W, H = self.width, self.height
        D = self.bitmap['data']
        out = bytearray(len(D))
        for y in range(H):
            sy = H - 1 - y if vertical else y
            src = D[sy * W * 4:(sy + 1) * W * 4]
            if horizontal:
                src = b''.join(src[i:i + 4] for i in range(W * 4 - 4, -4, -4))
            out[y * W * 4:(y + 1) * W * 4] = src
        self.bitmap['data'] = out
        return self

    def mirror(self, h, v):
        return self.flip(horizontal=h, vertical=v)

    def rotate(self, deg):
        d = ((round(deg) % 360) + 360) % 360
        if d % 90 != 0:
            raise ValueError('rotate: hanya kelipatan 90 derajat')
        if d == 0:
            return self
        W, H = self.width, self.height
        D = self.bitmap['data']
        nw = H if d != 180 else W
        nh = W if d != 180 else H
        out = bytearray(len(D))
        for y in range(H):
            for x in range(W):
                if d == 90:
                    nx, ny = H - 1 - y, x
                elif d == 180:
                    nx, ny = W - 1 - x, H - 1 - y
                else:
                    nx, ny = y, W - 1 - x
                si = (y * W + x) * 4
                di = (ny * nw + nx) * 4
                out[di:di + 4] = D[si:si + 4]
        self.bitmap = {'data': out, 'width': nw, 'height': nh}
        return self

    def composite(self, src, x=0, y=0, opts=None):
        opts = opts or {}
        s = src.bitmap if isinstance(src, Jimp) else src
        opacity = opts.get('opacitySource', opts.get('opacity', 1))
        blit(self.bitmap, s, x, y, 1 if opacity is None else opacity)
        return self

    def maskAlpha(self, mask_img, x=0, y=0):
        m = mask_img.bitmap
        d = self.bitmap
        W = d['width']
        D = d['data']
        for py in range(d['height']):
            for px in range(W):
                mx = px - x
                my = py - y
                ma = (m['data'][(my * m['width'] + mx) * 4 + 3] / 255) if (0 <= mx < m['width'] and 0 <= my < m['height']) else 0
                i = (py * W + px) * 4
                D[i + 3] = int(round(D[i + 3] * ma))
        return self

    def greyscaleRaw(self):
        d = self.bitmap['data']
        n = self.width * self.height
        out = bytearray(n)
        for i in range(n):
            out[i] = lin_to_srgb8(0.2126 * SRGB_TO_LIN[d[i * 4]] + 0.7152 * SRGB_TO_LIN[d[i * 4 + 1]] + 0.0722 * SRGB_TO_LIN[d[i * 4 + 2]])
        return bytes(out)

    def toRaw(self):
        return {'data': bytes(self.bitmap['data']), 'width': self.width, 'height': self.height, 'channels': 4}


def _write_file(path, data):
    with open(path, 'wb') as fh:
        fh.write(data)


def rasterize(polys, width, height, rule='nonzero', ox=0.0, oy=0.0):
    cov = array('f', bytes(4 * width * height))
    if width <= 0 or height <= 0:
        return cov
    diff = array('f', bytes(4 * (width + 2) * height))
    edges = []
    for poly in polys:
        n = len(poly)
        if n < 3:
            continue
        for i in range(n):
            ax, ay = poly[i]
            bx, by = poly[i + 1] if i + 1 < n else poly[0]
            ax -= ox
            ay -= oy
            bx -= ox
            by -= oy
            if ay == by:
                continue
            if ay < by:
                x0, y0, x1, y1, d = ax, ay, bx, by, 1
            else:
                x0, y0, x1, y1, d = bx, by, ax, ay, -1
            ys0 = y0 * SS
            ys1 = y1 * SS
            edges.append((ys0, ys1, x0, (x1 - x0) / ((y1 - y0) * SS), d))
    if not edges:
        return cov
    edges.sort()
    min_y = edges[0][0]
    max_y = max(e[1] for e in edges)
    start = max(0, math.floor(min_y))
    end = min(height * SS - 1, math.ceil(max_y))
    evenodd = rule == 'evenodd'
    weight = 1.0 / SS
    active = []
    ei = 0
    ne = len(edges)
    for sy in range(start, end + 1):
        yc = sy + 0.5
        while ei < ne and edges[ei][0] <= yc:
            active.append(edges[ei])
            ei += 1
        if active:
            active = [e for e in active if e[1] > yc]
        if not active:
            continue
        xs = sorted((e[2] + (yc - e[0]) * e[3], e[4]) for e in active)
        row = sy // SS
        cb = row * width
        db = row * (width + 2)
        winding = 0
        for i in range(len(xs) - 1):
            winding += xs[i][1]
            inside = (i % 2 == 0) if evenodd else winding != 0
            if not inside:
                continue
            xa = xs[i][0]
            xb = xs[i + 1][0]
            if xb <= 0 or xa >= width:
                continue
            a = xa if xa > 0 else 0.0
            b = xb if xb < width else float(width)
            ia = int(a)
            ib = int(b)
            if ia == ib:
                cov[cb + ia] += (b - a) * weight
                continue
            cov[cb + ia] += (ia + 1 - a) * weight
            diff[db + ia + 1] += weight
            diff[db + ib] -= weight
            if ib < width:
                cov[cb + ib] += (b - ib) * weight
    for row in range(height):
        cb = row * width
        db = row * (width + 2)
        s = 0.0
        for x in range(width):
            s += diff[db + x]
            v = cov[cb + x] + s
            cov[cb + x] = 1.0 if v > 1 else (0.0 if v < 0 else v)
    return cov


def fill_polygons(polys, width, height, rule='nonzero'):
    return rasterize(polys, width, height, rule, 0.0, 0.0)


def flatten_quad(out, p0, pc, p1, tol=0.15):
    x0, y0 = p0
    cx, cy = pc
    x1, y1 = p1
    dx = x0 - 2 * cx + x1
    dy = y0 - 2 * cy + y1
    steps = min(32, max(2, math.ceil(math.sqrt(math.sqrt(dx * dx + dy * dy) / tol))))
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        out.append((mt * mt * x0 + 2 * mt * t * cx + t * t * x1, mt * mt * y0 + 2 * mt * t * cy + t * t * y1))


def flatten_arc(out, cx, cy, r, a0, a1, ccw=False):
    two_pi = math.pi * 2
    da = a1 - a0
    if not ccw and da < 0:
        da += two_pi * math.ceil(-da / two_pi)
    if ccw and da > 0:
        da -= two_pi * math.ceil(da / two_pi)
    if abs(da) > two_pi:
        da = math.copysign(two_pi, da)
    steps = max(8, math.ceil(abs(da) * max(r, 1) / 1.2))
    for i in range(steps + 1):
        a = a0 + da * (i / steps)
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))


def rect_poly(x, y, w, h):
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def rounded_poly(x, y, w, h, radius):
    r = max(0.0, min(radius, w / 2, h / 2))
    if r <= 0:
        return rect_poly(x, y, w, h)
    pts = []
    pi = math.pi
    flatten_arc(pts, x + r, y + r, r, pi, pi * 1.5)
    flatten_arc(pts, x + w - r, y + r, r, pi * 1.5, pi * 2)
    flatten_arc(pts, x + w - r, y + h - r, r, 0, pi / 2)
    flatten_arc(pts, x + r, y + h - r, r, pi / 2, pi)
    return pts


def circle_poly(cx, cy, r):
    pts = []
    flatten_arc(pts, cx, cy, r, 0, math.pi * 2)
    return pts


def stroke_to_polygons(subpaths, width, cap='butt', join='miter'):
    hw = width / 2
    polys = []

    def circle(c, r):
        n = max(8, math.ceil(r * 3))
        return [(c[0] + r * math.cos((i / n) * math.pi * 2), c[1] + r * math.sin((i / n) * math.pi * 2)) for i in range(n)]

    for sp in subpaths:
        raw = sp['pts']
        closed = sp.get('closed', False)
        P = [p for i, p in enumerate(raw) if i == 0 or math.hypot(p[0] - raw[i - 1][0], p[1] - raw[i - 1][1]) > 1e-6]
        if len(P) < 2:
            if len(P) == 1 and cap == 'round':
                polys.append(circle(P[0], hw))
            continue
        seg_count = len(P) if closed else len(P) - 1
        for i in range(seg_count):
            a = P[i]
            b = P[(i + 1) % len(P)]
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            ln = math.hypot(dx, dy)
            nx = (-dy / ln) * hw
            ny = (dx / ln) * hw
            ax, ay = a
            bx, by = b
            if not closed and cap == 'square':
                if i == 0:
                    ax -= (dx / ln) * hw
                    ay -= (dy / ln) * hw
                if i == seg_count - 1:
                    bx += (dx / ln) * hw
                    by += (dy / ln) * hw
            polys.append([(ax + nx, ay + ny), (bx + nx, by + ny), (bx - nx, by - ny), (ax - nx, ay - ny)])

        def join_at(i):
            p = P[i]
            prev = P[(i - 1 + len(P)) % len(P)]
            nxt = P[(i + 1) % len(P)]
            if join == 'round':
                polys.append(circle(p, hw))
                return
            d1x = p[0] - prev[0]
            d1y = p[1] - prev[1]
            d2x = nxt[0] - p[0]
            d2y = nxt[1] - p[1]
            l1 = math.hypot(d1x, d1y)
            l2 = math.hypot(d2x, d2y)
            cross = d1x * d2y - d1y * d2x
            if abs(cross) < 1e-9:
                return
            s = -1 if cross > 0 else 1
            n1x, n1y = (-d1y / l1) * hw * s, (d1x / l1) * hw * s
            n2x, n2y = (-d2y / l2) * hw * s, (d2x / l2) * hw * s
            tri = [p, (p[0] + n1x, p[1] + n1y), (p[0] + n2x, p[1] + n2y)]
            if join == 'miter':
                mx = n1x + n2x
                my = n1y + n2y
                ml = math.hypot(mx, my)
                if ml > 1e-9:
                    cos_half = ml / (2 * hw)
                    if 1 / cos_half <= 10:
                        k = hw / cos_half / ml
                        tri.insert(2, (p[0] + mx * k, p[1] + my * k))
            polys.append(tri)

        frm = 0 if closed else 1
        to = len(P) if closed else len(P) - 1
        for i in range(frm, to):
            join_at(i)
        if not closed and cap == 'round':
            polys.append(circle(P[0], hw))
            polys.append(circle(P[-1], hw))
    for poly in polys:
        area = 0.0
        for i in range(len(poly)):
            a = poly[i]
            b = poly[(i + 1) % len(poly)]
            area += a[0] * b[1] - b[0] * a[1]
        if area < 0:
            poly.reverse()
    return polys


def build_gradient_lut(stops):
    items = []
    n = len(stops)
    for i, st in enumerate(stops):
        if isinstance(st, (list, tuple)) and len(st) == 2 and isinstance(st[0], (int, float)):
            items.append((float(st[0]), unpack_color(st[1])))
        else:
            items.append((i / (n - 1) if n > 1 else 0.0, unpack_color(st)))
    items.sort(key=lambda t: t[0])
    lut = []
    for k in range(256):
        t = k / 255
        if t <= items[0][0]:
            lut.append(items[0][1])
            continue
        if t >= items[-1][0]:
            lut.append(items[-1][1])
            continue
        for j in range(len(items) - 1):
            p0, c0 = items[j]
            p1, c1 = items[j + 1]
            if p0 <= t <= p1:
                f = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
                lut.append(tuple(int(round(c0[q] + (c1[q] - c0[q]) * f)) for q in range(4)))
                break
    return lut


class TTFont:
    def __init__(self, buf):
        buf = bytes(buf)
        self.buf = buf
        n = read_be(buf, 4, 2)
        self.tables = {}
        for i in range(n):
            r = 12 + i * 16
            self.tables[buf[r:r + 4].decode('latin1')] = (read_be(buf, r + 8, 4), read_be(buf, r + 12, 4))
        for t in ('head', 'hhea', 'hmtx', 'maxp', 'cmap', 'loca', 'glyf'):
            if t not in self.tables:
                raise ValueError(f'TTF: table wajib "{t}" tidak ada (font CFF/OTF belum didukung)')
        head = self.tables['head'][0]
        hhea = self.tables['hhea'][0]
        self.unitsPerEm = read_be(buf, head + 18, 2)
        self.locaFormat = struct.unpack_from('>h', buf, head + 50)[0]
        self.numGlyphs = read_be(buf, self.tables['maxp'][0] + 4, 2)
        self.ascent = struct.unpack_from('>h', buf, hhea + 4)[0]
        self.descent = struct.unpack_from('>h', buf, hhea + 6)[0]
        self.lineGap = struct.unpack_from('>h', buf, hhea + 8)[0]
        n_hm = read_be(buf, hhea + 34, 2)
        hmtx = self.tables['hmtx'][0]
        self.advance = []
        last = 0
        for g in range(self.numGlyphs):
            if g < n_hm:
                last = read_be(buf, hmtx + g * 4, 2)
            self.advance.append(last)
        self.cmap = self._parse_cmap()
        self.kern_map = self._parse_kern()
        self.outline_cache = {}

    def _parse_cmap(self):
        buf = self.buf
        base = self.tables['cmap'][0]
        num = read_be(buf, base + 2, 2)
        best = -1
        best_score = -1
        for i in range(num):
            r = base + 4 + i * 8
            pid = read_be(buf, r, 2)
            eid = read_be(buf, r + 2, 2)
            off = read_be(buf, r + 4, 4)
            sc = 0
            if pid == 3 and eid == 10:
                sc = 5
            elif pid == 0 and eid >= 4:
                sc = 5
            elif pid == 3 and eid == 1:
                sc = 4
            elif pid == 0:
                sc = 3
            if sc > best_score:
                best_score = sc
                best = base + off
        if best < 0:
            raise ValueError('TTF: cmap unicode tidak ada')
        fmt = read_be(buf, best, 2)
        mp = {}
        if fmt == 4:
            sx2 = read_be(buf, best + 6, 2)
            seg = sx2 // 2
            end_o = best + 14
            start_o = end_o + sx2 + 2
            delta_o = start_o + sx2
            range_o = delta_o + sx2
            for s in range(seg):
                end = read_be(buf, end_o + s * 2, 2)
                start = read_be(buf, start_o + s * 2, 2)
                delta = struct.unpack_from('>h', buf, delta_o + s * 2)[0]
                ro = read_be(buf, range_o + s * 2, 2)
                if start == 0xFFFF:
                    continue
                for c in range(start, end + 1):
                    if ro == 0:
                        g = (c + delta) & 0xFFFF
                    else:
                        g = read_be(buf, range_o + s * 2 + ro + (c - start) * 2, 2)
                        if g:
                            g = (g + delta) & 0xFFFF
                    if g:
                        mp[c] = g
        elif fmt == 12:
            ng = read_be(buf, best + 12, 4)
            for g in range(ng):
                r = best + 16 + g * 12
                s = read_be(buf, r, 4)
                e = read_be(buf, r + 4, 4)
                gi = read_be(buf, r + 8, 4)
                for c in range(s, e + 1):
                    mp[c] = gi + (c - s)
        else:
            raise ValueError(f'TTF: cmap format {fmt} tidak didukung')
        return mp

    def _parse_kern(self):
        t = self.tables.get('kern')
        if not t:
            return None
        buf = self.buf
        off = t[0]
        mp = {}
        try:
            n_tables = read_be(buf, off + 2, 2)
            p = off + 4
            for _ in range(n_tables):
                length = read_be(buf, p + 2, 2)
                cov = read_be(buf, p + 4, 2)
                if (cov >> 8) == 0 and (cov & 1):
                    np_ = read_be(buf, p + 6, 2)
                    for k in range(np_):
                        o = p + 14 + k * 6
                        mp[(read_be(buf, o, 2) << 16) | read_be(buf, o + 2, 2)] = struct.unpack_from('>h', buf, o + 4)[0]
                p += length
        except (IndexError, struct.error):
            return None
        return mp

    def glyphIndex(self, cp):
        return self.cmap.get(cp, 0)

    def kerning(self, g1, g2):
        if not self.kern_map:
            return 0
        return self.kern_map.get((g1 << 16) | g2, 0)

    def measure(self, text, size, kerning=True):
        sc = size / self.unitsPerEm
        w = 0
        prev = 0
        for ch in str(text):
            g = self.glyphIndex(ord(ch))
            if kerning and prev:
                w += self.kerning(prev, g)
            w += self.advance[g]
            prev = g
        return w * sc

    def glyph_offset(self, g):
        loca = self.tables['loca'][0]
        if self.locaFormat == 0:
            return (read_be(self.buf, loca + g * 2, 2) * 2, read_be(self.buf, loca + g * 2 + 2, 2) * 2)
        return (read_be(self.buf, loca + g * 4, 4), read_be(self.buf, loca + g * 4 + 4, 4))

    def outline(self, g, depth=0):
        if g in self.outline_cache:
            return self.outline_cache[g]
        if g < 0 or g >= self.numGlyphs or depth > 6:
            return []
        s, e = self.glyph_offset(g)
        if s == e:
            self.outline_cache[g] = []
            return []
        buf = self.buf
        base = self.tables['glyf'][0] + s
        nc = struct.unpack_from('>h', buf, base)[0]
        contours = []
        if nc >= 0:
            end_pts = [read_be(buf, base + 10 + i * 2, 2) for i in range(nc)]
            n_pts = end_pts[-1] + 1 if nc else 0
            p = base + 10 + nc * 2
            ins = read_be(buf, p, 2)
            p += 2 + ins
            flags = []
            while len(flags) < n_pts:
                f = buf[p]
                p += 1
                flags.append(f)
                if f & 8:
                    r = buf[p]
                    p += 1
                    flags.extend([f] * r)
            xs = []
            v = 0
            for i in range(n_pts):
                f = flags[i]
                if f & 2:
                    d = buf[p]
                    p += 1
                    v += d if f & 16 else -d
                elif not (f & 16):
                    v += struct.unpack_from('>h', buf, p)[0]
                    p += 2
                xs.append(v)
            ys = []
            v = 0
            for i in range(n_pts):
                f = flags[i]
                if f & 4:
                    d = buf[p]
                    p += 1
                    v += d if f & 32 else -d
                elif not (f & 32):
                    v += struct.unpack_from('>h', buf, p)[0]
                    p += 2
                ys.append(v)
            st = 0
            for en in end_pts:
                contours.append([(xs[i], ys[i], bool(flags[i] & 1)) for i in range(st, en + 1)])
                st = en + 1
        else:
            p = base + 10
            more = True
            while more:
                fl = read_be(buf, p, 2)
                gi = read_be(buf, p + 2, 2)
                p += 4
                if fl & 1:
                    dx, dy = struct.unpack_from('>hh', buf, p)
                    p += 4
                else:
                    dx, dy = struct.unpack_from('>bb', buf, p)
                    p += 2
                if not (fl & 2):
                    dx = 0
                    dy = 0
                a, b, c, d = 1.0, 0.0, 0.0, 1.0
                if fl & 8:
                    a = d = struct.unpack_from('>h', buf, p)[0] / 16384
                    p += 2
                elif fl & 0x40:
                    a = struct.unpack_from('>h', buf, p)[0] / 16384
                    d = struct.unpack_from('>h', buf, p + 2)[0] / 16384
                    p += 4
                elif fl & 0x80:
                    a, b, c, d = (v / 16384 for v in struct.unpack_from('>hhhh', buf, p))
                    p += 8
                for con in self.outline(gi, depth + 1):
                    contours.append([(a * x + c * y + dx, b * x + d * y + dy, on) for x, y, on in con])
                more = bool(fl & 0x20)
        self.outline_cache[g] = contours
        return contours

    def glyph_polys(self, g, scale, ox, oy):
        polys = []
        for c in self.outline(g):
            if len(c) < 2:
                continue
            first_on = next((i for i, p in enumerate(c) if p[2]), -1)
            if first_on == -1:
                a = c[0]
                b = c[-1]
                start = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                seq = list(c)
            else:
                pts = c[first_on:] + c[:first_on]
                start = (pts[0][0], pts[0][1])
                seq = pts[1:]
            T = lambda x, y: (ox + x * scale, oy - y * scale)
            out = [T(start[0], start[1])]
            cur = start
            i = 0
            while i < len(seq):
                p = seq[i]
                if p[2]:
                    out.append(T(p[0], p[1]))
                    cur = (p[0], p[1])
                    i += 1
                else:
                    nx = seq[i + 1] if i + 1 < len(seq) else None
                    if nx is not None and not nx[2]:
                        endp = ((p[0] + nx[0]) / 2, (p[1] + nx[1]) / 2)
                        adv = 1
                    elif nx is not None:
                        endp = (nx[0], nx[1])
                        adv = 2
                    else:
                        endp = start
                        adv = 1
                    flatten_quad(out, T(cur[0], cur[1]), T(p[0], p[1]), T(endp[0], endp[1]), 0.1)
                    cur = endp
                    i += adv
            polys.append(out)
        return polys

    def text_polys(self, text, size, x, baseline, kerning=True):
        sc = size / self.unitsPerEm
        polys = []
        pen = x
        prev = 0
        for ch in str(text):
            g = self.glyphIndex(ord(ch))
            if kerning and prev:
                pen += self.kerning(prev, g) * sc
            polys.extend(self.glyph_polys(g, sc, pen, baseline))
            pen += self.advance[g] * sc
            prev = g
        return polys, pen - x

    def renderInk(self, text, size, kerning=True):
        sc = size / self.unitsPerEm
        polys = []
        pen = 0.0
        prev = 0
        for ch in str(text):
            g = self.glyphIndex(ord(ch))
            if kerning and prev:
                pen += self.kerning(prev, g) * sc
            polys.extend(self.glyph_polys(g, sc, pen, 0))
            pen += self.advance[g] * sc
            prev = g
        pts = [p for poly in polys for p in poly]
        if not pts:
            return {'data': bytearray(), 'width': 0, 'height': 0}
        x0 = min(p[0] for p in pts)
        x1 = max(p[0] for p in pts)
        y0 = min(p[1] for p in pts)
        y1 = max(p[1] for p in pts)
        W = max(1, round(x1 - x0))
        H = max(1, round(y1 - y0))
        cov = rasterize(polys, W, H, 'nonzero', x0, y0)
        data = bytearray(W * H * 4)
        data[3::4] = bytes(clamp255(c * 255 + 0.5) for c in cov)
        return {'data': data, 'width': W, 'height': H}


def default_font_path():
    return os.path.join(os.getcwd(), 'data', 'brat-arial.ttf')


async def ensure_default_font():
    if DEFAULT_FONT['obj'] is not None:
        return DEFAULT_FONT['obj']
    path = default_font_path()
    if not os.path.isfile(path):
        res = await http.get(DEFAULT_FONT_URL, timeout=20, raise_for_status=False)
        data = res.content if res.ok else None
        if not data or len(data) < 10000 or bytes(data[:4]) not in FONT_SIGNATURES:
            raise RuntimeError(f'Font default tidak ada di {path} dan gagal diunduh')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(data)
    DEFAULT_FONT['obj'] = load_font_file(path)
    return DEFAULT_FONT['obj']


def load_font_file(path):
    if path not in FONT_CACHE:
        with open(path, 'rb') as fh:
            FONT_CACHE[path] = TTFont(fh.read())
    return FONT_CACHE[path]


def resolve_font(font):
    if font is None:
        if DEFAULT_FONT['obj'] is None:
            DEFAULT_FONT['obj'] = load_font_file(default_font_path())
        return DEFAULT_FONT['obj']
    if isinstance(font, TTFont):
        return font
    if isinstance(font, str):
        reg = REGISTERED_FONTS.get(font.lower())
        if reg is not None:
            return reg
        if os.path.isfile(font):
            return load_font_file(font)
    raise ValueError(f'font tidak ditemukan: {font}')


def register_font(path, options=None):
    family = (options or {}).get('family')
    if not family:
        raise ValueError('registerFont: opsi "family" wajib diisi')
    REGISTERED_FONTS[family.lower()] = load_font_file(path)
    return None


def load_font_buffer(buf):
    return TTFont(buf)


async def webp_to_png(buf):
    return await asyncio.to_thread(webp_to_png_sync, bytes(buf))


async def png_to_webp(png, options=None):
    options = options or {}
    return await asyncio.to_thread(png_to_webp_sync, bytes(png), options.get('quality', 80), options.get('lossless', False))


async def read(src):
    return await asyncio.to_thread(read_sync, src)


async def from_buffer(buf):
    return await asyncio.to_thread(from_buffer_sync, bytes(buf))


def metadata(buf):
    fmt = detect_format(buf)
    if not fmt:
        raise ValueError('metadata: format gambar tidak dikenali')
    d = read_dimensions(buf)
    if not d:
        raise ValueError('metadata: dimensi tidak terbaca')
    return {'format': fmt, 'width': d['width'], 'height': d['height']}


def create(width, height, color=0x00000000):
    return Jimp({'width': width, 'height': height, 'color': color})


def from_raw(opts):
    data = bytes(opts['data'])
    w = opts['width']
    h = opts['height']
    ch = opts.get('channels', 4)
    if ch == 4:
        return Jimp({'data': bytearray(data), 'width': w, 'height': h})
    n = w * h
    out = bytearray(n * 4)
    out[3::4] = b'\xff' * n
    if ch == 3:
        out[0::4] = data[0:n * 3:3]
        out[1::4] = data[1:n * 3:3]
        out[2::4] = data[2:n * 3:3]
    elif ch == 1:
        out[0::4] = data[0:n]
        out[1::4] = data[0:n]
        out[2::4] = data[0:n]
    elif ch == 2:
        out[0::4] = data[0:n * 2:2]
        out[1::4] = data[0:n * 2:2]
        out[2::4] = data[0:n * 2:2]
        out[3::4] = data[1:n * 2:2]
    else:
        raise ValueError('fromRaw: channels harus 1-4')
    return Jimp({'data': out, 'width': w, 'height': h})


def from_bitmap(bm):
    return Jimp({'data': bytearray(bm['data']), 'width': bm['width'], 'height': bm['height']})


async def resize(input_, w, h, opts=None):
    opts = opts or {}
    return await asyncio.to_thread(_resize_job, input_, w, h, opts)


def _resize_job(input_, w, h, opts):
    img = read_sync(input_)
    img.resize(w, h, {k: opts.get(k) for k in ('fit', 'position', 'background', 'kernel')})
    mime = FORMAT_MIME.get(opts.get('out'), 'image/png')
    return encode_sync(img.bitmap, mime, {'quality': opts.get('quality')})


async def extract(input_, rect, out='jpeg', quality=None):
    return await asyncio.to_thread(_extract_job, input_, rect, out, quality)


def _extract_job(input_, rect, out, quality):
    img = read_sync(input_)
    img.crop(rect['left'], rect['top'], rect['width'], rect['height'])
    mime = FORMAT_MIME.get(out, 'image/png')
    return encode_sync(img.bitmap, mime, {'quality': quality})


async def to_raw(input_, opts=None):
    opts = opts or {}
    return await asyncio.to_thread(to_raw_sync, input_, opts.get('channels', 4))


class Canvas:
    def __init__(self, width, height, background=None, transparent=False):
        if not (width > 0 and height > 0):
            raise ValueError('Canvas: width dan height harus > 0')
        self.width = int(width)
        self.height = int(height)
        if transparent:
            bg = (0, 0, 0, 0)
        else:
            bg = unpack_color(background if background is not None else '#ffffff')
        self.bitmap = {'data': bytearray(bytes(bg) * (self.width * self.height)), 'width': self.width, 'height': self.height}

    def _paint(self, bx, by, bw, bh, cov, rgba, opacity=1.0):
        W = self.width
        H = self.height
        D = self.bitmap['data']
        r, g, b, a255 = rgba
        ca = a255 / 255 * opacity
        if ca <= 0:
            return
        for row in range(bh):
            y = by + row
            if y < 0 or y >= H:
                continue
            cbase = row * bw
            for col in range(bw):
                c = cov[cbase + col]
                if c <= 0:
                    continue
                x = bx + col
                if x < 0 or x >= W:
                    continue
                over_px(D, (y * W + x) * 4, r, g, b, c * ca)

    def fill_polygons(self, polys, color, rule='nonzero', opacity=1.0):
        pts = [p for poly in polys for p in poly]
        if not pts:
            return self
        x0 = max(0, math.floor(min(p[0] for p in pts)))
        y0 = max(0, math.floor(min(p[1] for p in pts)))
        x1 = min(self.width, math.ceil(max(p[0] for p in pts)))
        y1 = min(self.height, math.ceil(max(p[1] for p in pts)))
        if x1 <= x0 or y1 <= y0:
            return self
        bw = x1 - x0
        bh = y1 - y0
        cov = rasterize(polys, bw, bh, rule, x0, y0)
        self._paint(x0, y0, bw, bh, cov, unpack_color(color), opacity)
        return self

    def fill(self, color, opacity=1.0):
        return self.fill_polygons([rect_poly(0, 0, self.width, self.height)], color, 'nonzero', opacity)

    def rect(self, x, y, w, h, color):
        return self.fill_polygons([rect_poly(x, y, w, h)], color)

    def rounded_rect(self, x, y, w, h, radius, color):
        return self.fill_polygons([rounded_poly(x, y, w, h, radius)], color)

    def circle(self, cx, cy, r, color):
        return self.fill_polygons([circle_poly(cx, cy, r)], color)

    def gradient(self, x, y, w, h, stops, angle=180.0, radius=0):
        poly = rounded_poly(x, y, w, h, radius) if radius else rect_poly(x, y, w, h)
        pts = poly
        x0 = max(0, math.floor(min(p[0] for p in pts)))
        y0 = max(0, math.floor(min(p[1] for p in pts)))
        x1 = min(self.width, math.ceil(max(p[0] for p in pts)))
        y1 = min(self.height, math.ceil(max(p[1] for p in pts)))
        if x1 <= x0 or y1 <= y0:
            return self
        bw = x1 - x0
        bh = y1 - y0
        cov = rasterize([poly], bw, bh, 'nonzero', x0, y0)
        lut = build_gradient_lut(stops)
        rad = math.radians(float(angle))
        dxn = math.sin(rad)
        dyn = -math.cos(rad)
        length = abs(w * dxn) + abs(h * dyn) or 1.0
        cx = x + w / 2
        cy = y + h / 2
        W = self.width
        D = self.bitmap['data']
        for row in range(bh):
            py = y0 + row + 0.5
            for col in range(bw):
                c = cov[row * bw + col]
                if c <= 0:
                    continue
                px = x0 + col + 0.5
                t = ((px - cx) * dxn + (py - cy) * dyn) / length + 0.5
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                r, g, b, a = lut[int(t * 255 + 0.5)]
                over_px(D, ((y0 + row) * W + x0 + col) * 4, r, g, b, c * (a / 255))
        return self

    def stroke(self, points, width, color, closed=False, cap='butt', join='miter'):
        polys = stroke_to_polygons([{'pts': [tuple(p) for p in points], 'closed': closed}], width, cap, join)
        return self.fill_polygons(polys, color)

    def paste(self, img, x=0, y=0, opacity=1.0):
        if isinstance(img, Canvas):
            src = img.bitmap
        elif isinstance(img, Jimp):
            src = img.bitmap
        else:
            src = read_sync(img).bitmap
        blit(self.bitmap, src, x, y, opacity)
        return self

    def measure(self, text, size, font=None):
        f = resolve_font(font)
        sc = size / f.unitsPerEm
        return {
            'width': f.measure(text, size),
            'height': (f.ascent - f.descent) * sc,
            'ascent': f.ascent * sc,
            'descent': -f.descent * sc,
        }

    def text(self, text, x, y, size, color, font=None, kerning=True):
        f = resolve_font(font)
        sc = size / f.unitsPerEm
        polys, _ = f.text_polys(text, size, x, y + f.ascent * sc, kerning)
        return self.fill_polygons(polys, color)

    def to_png(self):
        return png_encode(self.width, self.height, self.bitmap['data'], 6, True)

    def to_bytes(self, out='png', quality=None):
        if str(out).startswith('image/'):
            mime = out
        else:
            mime = FORMAT_MIME.get('jpeg' if out == 'jpg' else out, 'image/png')
        return encode_sync(self.bitmap, mime, {'quality': quality})

    def to_jimp(self):
        return Jimp({'data': self.bitmap['data'], 'width': self.width, 'height': self.height})


def create_canvas(width, height):
    return Canvas(width, height, transparent=True)


async def load_image(source):
    return await read(source)


jimp = SimpleNamespace(
    Jimp=Jimp,
    MIME=MIME,
    read=read,
    fromBuffer=from_buffer,
    create=create,
    fromRaw=from_raw,
    fromBitmap=from_bitmap,
    toRaw=to_raw,
    metadata=metadata,
    resize=resize,
    extract=extract,
    detectFormat=detect_format,
    readDimensions=read_dimensions,
    getExifOrientation=get_exif_orientation,
    webpToPng=webp_to_png,
    pngToWebp=png_to_webp,
    TTFont=TTFont,
    loadFontBuffer=load_font_buffer,
    fillPolygons=fill_polygons,
)

canvas = SimpleNamespace(
    createCanvas=create_canvas,
    loadImage=load_image,
    registerFont=register_font,
    html=Canvas,
    Canvas=Canvas,
)
