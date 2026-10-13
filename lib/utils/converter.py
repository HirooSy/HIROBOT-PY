import datetime
import math
import os
import re
import struct
import zlib

from lib.compat import ffmpeg as ff, http
from lib.utils.canvas import jimp


MAX_INPUT_SIZE = 50 * 1024 * 1024

os.makedirs(ff.TMP_DIR, exist_ok=True)

_NAN = float('nan')


def _field(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _to_bytes(buffer):
    if isinstance(buffer, (bytes, bytearray, memoryview)):
        return bytes(buffer)
    if isinstance(buffer, str):
        return buffer.encode('utf-8')
    if hasattr(buffer, 'read'):
        return bytes(buffer.read())
    return bytes(buffer)


class ConvertResult:
    def __init__(self, data, filename):
        self.data = data
        self.filename = filename

    def __getitem__(self, key):
        if key in ('data', 'filename'):
            return getattr(self, key)
        if key in ('toBuffer', 'clear'):
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    async def toBuffer(self):
        return self.data

    async def clear(self):
        if self.filename:
            try:
                os.unlink(self.filename)
            except OSError:
                pass


async def ffmpeg(buffer, args=None, ext='', ext2='', is_audio=False):
    args = list(args or [])
    src = ff.tmp_path(ext)
    out = f'{src}.{ext2}'
    with open(src, 'wb') as fh:
        fh.write(_to_bytes(buffer))
    try:
        if is_audio and os.path.getsize(src) > MAX_INPUT_SIZE:
            pre = f'{src}.pre.mp3'
            await ff.ffmpeg(['-i', src, '-vn', '-c:a', 'libmp3lame', '-b:a', '96k', pre])
            os.replace(pre, src)
        try:
            await ff.ffmpeg(['-i', src, *args, out])
        except Exception:
            try:
                os.unlink(out)
            except OSError:
                pass
            raise
        with open(out, 'rb') as fh:
            data = fh.read()
        return ConvertResult(data, out)
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


async def ffmpeg_bytes(buffer, args, ext, ext2):
    res = await ffmpeg(buffer, args, ext, ext2)
    data = await res.toBuffer()
    await res.clear()
    return data


async def to_ptt(buffer, ext):
    return await ffmpeg(buffer, ['-vn', '-c:a', 'libopus', '-b:a', '128k', '-vbr', 'on'], ext, 'ogg', True)


async def to_audio(buffer, ext):
    return await ffmpeg(buffer, ['-vn', '-c:a', 'libopus', '-b:a', '128k', '-vbr', 'on', '-compression_level', '10'], ext, 'opus', True)


async def to_opus(buffer, ext, options=None):
    o = {'bitrate': '64k', 'sampleRate': 48000, 'channels': 2, 'vbr': 'on', 'application': 'audio', 'compressionLevel': 10}
    for k, v in (options or {}).items():
        if v is not None:
            o[k] = v
    return await ffmpeg(buffer, [
        '-vn',
        '-c:a', 'libopus',
        '-b:a', str(o['bitrate']),
        '-ar', str(o['sampleRate']),
        '-ac', str(o['channels']),
        '-vbr', str(o['vbr']),
        '-application', str(o['application']),
        '-compression_level', str(o['compressionLevel']),
    ], ext, 'opus', True)


async def to_video(buffer, ext):
    return await ffmpeg(buffer, ['-c:v', 'libx264', '-c:a', 'aac', '-ab', '128k', '-ar', '44100', '-crf', '32', '-preset', 'slow'], ext, 'mp4')


toPTT = to_ptt
toAudio = to_audio
toOpus = to_opus
toVideo = to_video


def _first_int(pattern, text):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def _find_image_objects(buf):
    s = buf.decode('latin-1')
    n = len(s)
    obj_dicts = {}
    obj_streams = {}
    obj_entries = []

    for m in re.finditer(r'(\d+)\s+\d+\s+obj', s):
        dict_start = s.find('<<', m.end())
        if dict_start == -1 or dict_start - m.end() > 20:
            continue

        depth = 0
        i = dict_start
        dict_end = -1
        while i < n - 1:
            if s[i] == '<' and s[i + 1] == '<':
                depth += 1
                i += 2
                continue
            if s[i] == '>' and s[i + 1] == '>':
                depth -= 1
                i += 2
                if depth == 0:
                    dict_end = i
                    break
                continue
            i += 1
        if dict_end == -1:
            continue

        d = s[dict_start:dict_end]
        num = m.group(1)
        obj_dicts[num] = d
        obj_entries.append((num, dict_end, d))

        kw = s.find('stream', dict_end)
        if kw != -1 and kw - dict_end <= 20:
            data_start = kw + len('stream')
            if data_start < n and s[data_start] == '\r':
                data_start += 1
            if data_start < n and s[data_start] == '\n':
                data_start += 1
            length = _first_int(r'/Length\s+(\d+)', d)
            data_end = data_start + length if length is not None else s.find('endstream', data_start)
            if data_end != -1 and data_end > data_start:
                obj_streams[num] = buf[data_start:data_end]

    mask_refs = {m.group(1) for m in re.finditer(r'/(?:SMask|Mask)\s+(\d+)\s+0\s+R', s)}

    def resolve_decode_parms(d):
        m = re.search(r'/(?:DecodeParms|DP)\s*<<([\s\S]*?)>>', d)
        if m:
            return m.group(1)
        m = re.search(r'/(?:DecodeParms|DP)\s*\[\s*<<([\s\S]*?)>>\s*\]', d)
        if m:
            return m.group(1)
        m = re.search(r'/(?:DecodeParms|DP)\s*\[?\s*(\d+)\s+\d+\s+R\s*\]?', d)
        if m:
            ref = obj_dicts.get(m.group(1))
            if ref:
                return ref
        return ''

    def extract_info(d, data):
        width = _first_int(r'/Width\s+(\d+)', d)
        height = _first_int(r'/Height\s+(\d+)', d)
        bpc = _first_int(r'/BitsPerComponent\s+(\d+)', d)
        fm = re.search(r'/Filter\s*(/\w+|\[[^\]]*\])', d)
        cs = re.search(r'/ColorSpace\s*/(\w+)', d)
        dp = resolve_decode_parms(d)
        dp_bpc = _first_int(r'/BitsPerComponent\s+(\d+)', dp)
        predictor = _first_int(r'/Predictor\s+(\d+)', dp)
        columns = _first_int(r'/Columns\s+(\d+)', dp)
        return {
            'width': width,
            'height': height,
            'bpc': bpc if bpc is not None else 8,
            'filter': fm.group(1) if fm else '',
            'colorSpace': cs.group(1) if cs else 'DeviceRGB',
            'predictor': predictor if predictor is not None else 1,
            'predictorColors': _first_int(r'/Colors\s+(\d+)', dp),
            'predictorColumns': columns if columns is not None else width,
            'predictorBpc': dp_bpc if dp_bpc is not None else (bpc if bpc is not None else 8),
            'data': data,
        }

    images = []
    for num, dict_end, d in obj_entries:
        if not re.search(r'/Subtype\s*/Image\b', d):
            continue
        if num in mask_refs:
            continue
        data = obj_streams.get(num)
        if data is None:
            continue
        info = extract_info(d, data)
        sm = re.search(r'/SMask\s+(\d+)\s+0\s+R', d)
        if sm:
            smd = obj_dicts.get(sm.group(1))
            smdata = obj_streams.get(sm.group(1))
            if smd is not None and smdata is not None:
                info['smask'] = extract_info(smd, smdata)
        images.append({'objStart': dict_end, **info})

    images.sort(key=lambda x: x['objStart'])
    return images


def _undo_predictor(data, predictor, colors, bpc, columns):
    if not predictor or predictor == 1:
        return data
    colors = colors or 1
    bpc = bpc or 8
    columns = columns or 1

    bpp = max(1, math.ceil((colors * bpc) / 8))
    row_bytes = math.ceil((colors * bpc * columns) / 8)
    if row_bytes <= 0:
        return data

    if predictor == 2:
        if bpc != 8:
            return data
        out = bytearray(data)
        rows = len(out) // row_bytes
        for r in range(rows):
            start = r * row_bytes
            for i in range(bpp, row_bytes):
                out[start + i] = (out[start + i] + out[start + i - bpp]) & 0xFF
        return bytes(out)

    rows = len(data) // (row_bytes + 1)
    out = bytearray(rows * row_bytes)
    prev = bytes(row_bytes)
    for r in range(rows):
        in_start = r * (row_bytes + 1)
        ftype = data[in_start]
        in_row = data[in_start + 1:in_start + 1 + row_bytes]
        row = bytearray(row_bytes)
        for i in range(row_bytes):
            a = row[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            val = in_row[i]
            if ftype == 1:
                val = (val + a) & 0xFF
            elif ftype == 2:
                val = (val + b) & 0xFF
            elif ftype == 3:
                val = (val + (a + b) // 2) & 0xFF
            elif ftype == 4:
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                val = (val + pred) & 0xFF
            row[i] = val
        out[r * row_bytes:(r + 1) * row_bytes] = row
        prev = bytes(row)
    return bytes(out)


def _parse_filter_list(filter_str):
    if not filter_str:
        return []
    return [m.group(1) for m in re.finditer(r'/(\w+)', filter_str)]


def _compute_channels(length, width, height, color_space):
    px = (width or 0) * (height or 0)
    if px > 0 and length % px == 0:
        bpp = length // px
        if bpp in (1, 3, 4):
            return bpp
    if color_space == 'DeviceGray':
        return 1
    if color_space == 'DeviceCMYK':
        return 4
    return 3


def _decode_stream(img):
    buf = img['data']
    for f in _parse_filter_list(img['filter']):
        if f == 'FlateDecode':
            buf = zlib.decompress(buf)
        elif f == 'DCTDecode':
            return {'kind': 'jpeg', 'buffer': buf}
        else:
            raise ValueError(f'Filter gambar "{f}" belum didukung')

    cs = img['colorSpace']
    default_colors = 1 if cs == 'DeviceGray' else (4 if cs == 'DeviceCMYK' else 3)
    colors = img['predictorColors'] if img['predictorColors'] is not None else default_colors
    buf = _undo_predictor(
        buf,
        img['predictor'],
        colors,
        img['predictorBpc'] or img['bpc'],
        img['predictorColumns'] or img['width'],
    )
    channels = _compute_channels(len(buf), img['width'], img['height'], cs)
    return {'kind': 'raw', 'buffer': buf, 'channels': channels}


def _u8(x):
    if x != x:
        return 0
    return int(x) & 0xFF


def _at(buf, i):
    return buf[i] if 0 <= i < len(buf) else None


def _js_round(x):
    if x != x:
        return x
    return math.floor(x + 0.5)


async def _decode_image_object(img):
    if img['bpc'] and img['bpc'] != 8:
        raise ValueError(f'BitsPerComponent {img["bpc"]} belum didukung, cuma gambar 8-bit yang didukung')
    if not img['width'] or not img['height']:
        raise ValueError('Dimensi gambar (Width/Height) tidak ditemukan di objek PDF')

    width = img['width']
    height = img['height']
    base = _decode_stream(img)

    if base['kind'] == 'jpeg':
        raw = await jimp.toRaw(base['buffer'], {'channels': 3})
        base_buf = _field(raw, 'data')
        base_channels = _field(_field(raw, 'info'), 'channels')
    else:
        base_buf = base['buffer']
        base_channels = base['channels']

    if base_channels == 4:
        px = width * height
        rgb = bytearray(px * 3)
        for i in range(px):
            o = i * 4
            ro = i * 3
            vals = []
            for k in range(4):
                v = _at(base_buf, o + k)
                vals.append(v / 255 if v is not None else _NAN)
            c, mag, y, k = vals
            rgb[ro] = _u8(255 * (1 - c) * (1 - k))
            rgb[ro + 1] = _u8(255 * (1 - mag) * (1 - k))
            rgb[ro + 2] = _u8(255 * (1 - y) * (1 - k))
        base_buf = bytes(rgb)
        base_channels = 3

    smask = img.get('smask')
    if smask is not None:
        mask = _decode_stream(smask)
        if mask['kind'] == 'jpeg':
            alpha = _field(await jimp.toRaw(mask['buffer'], {'channels': 1}), 'data')
        elif mask['channels'] == 1:
            alpha = mask['buffer']
        else:
            m = jimp.fromRaw({'data': mask['buffer'], 'width': smask['width'], 'height': smask['height'], 'channels': mask['channels']})
            alpha = m.greyscaleRaw()

        px = width * height
        out = bytearray(px * base_channels)
        for i in range(px):
            av = _at(alpha, i)
            a = av / 255 if av is not None else _NAN
            for c in range(base_channels):
                idx = i * base_channels + c
                bv = _at(base_buf, idx)
                val = (bv * a + 255 * (1 - a)) if bv is not None else _NAN
                out[idx] = _u8(_js_round(val))
        base_buf = bytes(out)

    target = jimp.fromRaw({'data': base_buf, 'width': width, 'height': height, 'channels': base_channels})
    return await target.getBuffer('image/jpeg', {'quality': 92})


async def img2pdf(input_items):
    items = input_items if isinstance(input_items, (list, tuple)) else [input_items]
    if not items:
        raise ValueError('img2pdf: tidak ada gambar yang diberikan')

    pages = []
    for raw in items:
        buf = raw
        if isinstance(raw, str):
            if re.match(r'^https?://', raw, re.I):
                res = await http.get(raw, timeout=120, raise_for_status=False)
                if not res.ok:
                    raise ValueError(f'img2pdf: gagal download {raw} (status {res.status})')
                buf = res.content
            else:
                with open(raw, 'rb') as fh:
                    buf = fh.read()
        if not isinstance(buf, (bytes, bytearray)):
            raise ValueError('img2pdf: setiap item harus berupa Buffer, URL, atau path string')
        buf = bytes(buf)

        img = await jimp.read(buf)
        jpeg = await img.getBuffer('image/jpeg', {'quality': 92})
        pages.append({'jpeg': jpeg, 'width': _field(img, 'width'), 'height': _field(img, 'height')})

    catalog_num = 1
    pages_num = 2
    page_nums = []
    content_nums = []
    image_nums = []
    nxt = 3
    for _ in pages:
        page_nums.append(nxt)
        content_nums.append(nxt + 1)
        image_nums.append(nxt + 2)
        nxt += 3
    total = nxt - 1

    chunks = []
    offsets = [0] * (total + 1)
    state = {'offset': 0}

    def push(b):
        chunks.append(b)
        state['offset'] += len(b)

    def push_obj(num, b):
        offsets[num] = state['offset']
        push(b)

    def text(s):
        return s.encode('latin-1')

    push(b'%PDF-1.4\n%\xE2\xE3\xCF\xD3\n')
    push_obj(catalog_num, text(f'{catalog_num} 0 obj\n<< /Type /Catalog /Pages {pages_num} 0 R >>\nendobj\n'))

    kids = ' '.join(f'{n} 0 R' for n in page_nums)
    push_obj(pages_num, text(f'{pages_num} 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>\nendobj\n'))

    for i, page in enumerate(pages):
        jpeg = page['jpeg']
        width = page['width']
        height = page['height']
        page_num = page_nums[i]
        content_num = content_nums[i]
        image_num = image_nums[i]
        content_buf = text(f'q {width} 0 0 {height} 0 0 cm /Im0 Do Q')

        push_obj(page_num, text(
            f'{page_num} 0 obj\n<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 {width} {height}] '
            f'/Resources << /XObject << /Im0 {image_num} 0 R >> >> /Contents {content_num} 0 R >>\nendobj\n'
        ))
        push_obj(content_num, b''.join([
            text(f'{content_num} 0 obj\n<< /Length {len(content_buf)} >>\nstream\n'),
            content_buf,
            text('\nendstream\nendobj\n'),
        ]))
        push_obj(image_num, b''.join([
            text(f'{image_num} 0 obj\n<< /Type /XObject /Subtype /Image /Width {width} /Height {height} '
                 f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n'),
            jpeg,
            text('\nendstream\nendobj\n'),
        ]))

    xref_offset = state['offset']
    xref = f'xref\n0 {total + 1}\n0000000000 65535 f \n'
    for i in range(1, total + 1):
        xref += f'{offsets[i]:010d} 00000 n \n'
    push(text(xref))
    push(text(f'trailer\n<< /Size {total + 1} /Root {catalog_num} 0 R >>\nstartxref\n{xref_offset}\n%%EOF'))

    return b''.join(chunks)


def _page_index(p, count):
    try:
        v = float(p) - 1
    except (TypeError, ValueError):
        return None
    if v != int(v) or v < 0 or v >= count:
        return None
    return int(v)


async def pdf2img(buffer, options=None):
    options = options or {}
    page = options.get('page')
    if page is None:
        page = 1

    images = _find_image_objects(_to_bytes(buffer))
    if not images:
        raise ValueError('Tidak ditemukan gambar tertanam di PDF ini (mungkin isinya teks/vector, bukan hasil scan)')

    if page == 'all':
        results = []
        for img in images:
            results.append(await _decode_image_object(img))
        return results

    is_list = isinstance(page, (list, tuple))
    pages = list(page) if is_list else [page]
    results = []
    for p in pages:
        idx = _page_index(p, len(images))
        if idx is None:
            raise ValueError(f'Halaman {p} tidak ditemukan (PDF ini punya {len(images)} gambar)')
        results.append(await _decode_image_object(images[idx]))
    return results if is_list else results[0]


def _dos_datetime(date):
    time_v = ((date.hour << 11) | (date.minute << 5) | (date.second >> 1)) & 0xFFFF
    date_v = ((((date.year - 1980) & 0x7F) << 9) | ((date.month) << 5) | date.day) & 0xFFFF
    return time_v, date_v


class ZipFile:
    def __init__(self):
        self._files = []

    def file(self, name, data):
        if isinstance(data, str):
            data = data.encode('utf-8')
        elif not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        self._files.append((str(name).replace('\\', '/'), bytes(data)))
        return self

    async def generateAsync(self, type='nodebuffer', compression='STORE', compressionOptions=None):
        if type != 'nodebuffer':
            raise ValueError(f'ZipFile.generateAsync: type "{type}" tidak didukung, cuma "nodebuffer"')

        use_deflate = compression == 'DEFLATE'
        level = -1
        if isinstance(compressionOptions, dict) and compressionOptions.get('level') is not None:
            level = int(compressionOptions['level'])
            if not -1 <= level <= 9:
                raise ValueError(f'ZipFile.generateAsync: compressionOptions.level harus antara -1 dan 9, dapat {level}')
        local = bytearray()
        central = bytearray()
        offset = 0
        time_v, date_v = _dos_datetime(datetime.datetime.now())

        for name, data in self._files:
            name_b = name.encode('utf-8')
            crc = zlib.crc32(data) & 0xFFFFFFFF
            comp = data
            method = 0
            if use_deflate:
                co = zlib.compressobj(level, zlib.DEFLATED, -15)
                deflated = co.compress(data) + co.flush()
                if len(deflated) < len(data):
                    comp = deflated
                    method = 8

            lh = struct.pack('<IHHHHHIIIHH', 0x04034b50, 20, 0, method, time_v, date_v, crc, len(comp), len(data), len(name_b), 0)
            local += lh + name_b + comp

            ch = struct.pack('<IHHHHHHIIIHHHHHII', 0x02014b50, 20, 20, 0, method, time_v, date_v, crc,
                             len(comp), len(data), len(name_b), 0, 0, 0, 0, 0, offset)
            central += ch + name_b

            offset += len(lh) + len(name_b) + len(comp)

        eocd = struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, len(self._files), len(self._files), len(central), offset, 0)
        return bytes(local + central + eocd)


def _u16(buf, i):
    return struct.unpack_from('<H', buf, i)[0]


def _u32(buf, i):
    return struct.unpack_from('<I', buf, i)[0]


def _is_safe_relative_path(p):
    norm = p.replace('\\', '/')
    if norm.startswith('/') or re.match(r'^[a-zA-Z]:', norm):
        return False
    depth = 0
    for part in norm.split('/'):
        if part == '..':
            depth -= 1
            if depth < 0:
                return False
        elif part not in ('.', ''):
            depth += 1
    return True


def _read_zip_entry(buf, entry):
    lp = entry['localHeaderOffset']
    if _u32(buf, lp) != 0x04034b50:
        raise ValueError(f'unzip: local file header untuk "{entry["name"]}" tidak valid')
    lnl = _u16(buf, lp + 26)
    lel = _u16(buf, lp + 28)
    ds = lp + 30 + lnl + lel
    comp = buf[ds:ds + entry['compressedSize']]

    if entry['method'] == 0:
        data = bytes(comp)
    elif entry['method'] == 8:
        data = zlib.decompress(comp, -15)
    else:
        raise ValueError(f'unzip: compression method {entry["method"]} untuk "{entry["name"]}" tidak didukung (cuma STORE dan DEFLATE)')

    if (zlib.crc32(data) & 0xFFFFFFFF) != entry['crc']:
        raise ValueError(f'unzip: CRC32 mismatch untuk "{entry["name"]}", file kemungkinan korup')
    return data


async def unzip(buffer, options=None):
    options = options or {}
    output_dir = options.get('outputDir')
    buf = _to_bytes(buffer)

    eocd_sig = 0x06054b50
    max_comment_len = 65535
    search_start = max(0, len(buf) - 22 - max_comment_len)
    eocd_offset = -1
    for i in range(len(buf) - 22, search_start - 1, -1):
        if _u32(buf, i) == eocd_sig:
            eocd_offset = i
            break
    if eocd_offset == -1:
        raise ValueError('unzip: bukan file ZIP yang valid (End Of Central Directory tidak ditemukan)')

    total_entries = _u16(buf, eocd_offset + 10)
    cd_size = _u32(buf, eocd_offset + 12)
    cd_offset = _u32(buf, eocd_offset + 16)
    if cd_offset + cd_size > len(buf):
        raise ValueError('unzip: central directory rusak atau file ZIP terpotong')

    entries = []
    ptr = cd_offset
    for i in range(total_entries):
        if _u32(buf, ptr) != 0x02014b50:
            raise ValueError(f'unzip: central directory entry ke-{i} tidak valid')
        name_len = _u16(buf, ptr + 28)
        extra_len = _u16(buf, ptr + 30)
        comment_len = _u16(buf, ptr + 32)
        entries.append({
            'name': buf[ptr + 46:ptr + 46 + name_len].decode('utf-8', errors='replace'),
            'method': _u16(buf, ptr + 10),
            'crc': _u32(buf, ptr + 16),
            'compressedSize': _u32(buf, ptr + 20),
            'uncompressedSize': _u32(buf, ptr + 24),
            'localHeaderOffset': _u32(buf, ptr + 42),
        })
        ptr += 46 + name_len + extra_len + comment_len

    if output_dir:
        results = []
        for entry in entries:
            name = entry['name']
            is_dir = name.endswith('/')
            if not _is_safe_relative_path(name):
                raise ValueError(f'unzip: entry "{name}" mencurigakan (path traversal), file ditolak')
            if is_dir:
                os.makedirs(os.path.normpath(os.path.join(output_dir, name)), exist_ok=True)
                results.append({'name': name, 'isDirectory': True})
                continue
            data = _read_zip_entry(buf, entry)
            dest = os.path.normpath(os.path.join(output_dir, name))
            os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
            with open(dest, 'wb') as fh:
                fh.write(data)
            results.append({'name': name, 'isDirectory': False, 'path': dest, 'size': len(data)})
        return results

    root = {'dirs': {}, 'files': {}}

    def get_node(parts):
        node = root
        for part in parts:
            if part not in node['dirs']:
                node['dirs'][part] = {'dirs': {}, 'files': {}}
            node = node['dirs'][part]
        return node

    for entry in entries:
        name = entry['name']
        is_dir = name.endswith('/')
        if not _is_safe_relative_path(name):
            raise ValueError(f'unzip: entry "{name}" mencurigakan (path traversal), file ditolak')
        parts = [p for p in re.sub(r'/+$', '', name).split('/') if p]
        if is_dir:
            get_node(parts)
            continue
        file_name = parts.pop()
        data = _read_zip_entry(buf, entry)
        get_node(parts)['files'][file_name] = data

    def collapse(node):
        obj = {}
        for fname, data in node['files'].items():
            obj[fname] = data
        for dname, child in node['dirs'].items():
            obj[dname] = collapse(child)
        return obj

    return {'result': collapse(root)}
