import asyncio
import math
import os
import re
import shutil
import struct
import sys
import time
import zlib

from lib.compat import ffmpeg as ffmpeg_mod
from lib.package.ai import mcp
from lib.utils import converter as converter_mod


ROOT = os.getcwd()
MB = 1024 * 1024
MAX_SEND_BYTES = 200 * MB
MAX_ZIP_INPUT_BYTES = 300 * MB
MAX_ZIP_FILE_BYTES = 300 * MB
MAX_EXTRACT_TOTAL_BYTES = 800 * MB
MAX_EXTRACT_ENTRIES = 10000
MAX_MULTI_FILES = 40
MAX_ZIP_ENTRIES = 20000
TMP_MAX_AGE_MS = 24 * 60 * 60 * 1000
EXEC_TIMEOUT_S = 10 * 60
SEND_GAP_S = 0.7
LISTING_LIMIT = 60
ALWAYS_SKIP_DIRS = {'.git'}
MODULE_DIR = 'node_modules'
HOLDER_DIRS = {'attached', 'converted', 'zips'}

MIME = {
    'pdf': 'application/pdf', 'zip': 'application/zip', 'txt': 'text/plain', 'md': 'text/markdown', 'html': 'text/html', 'htm': 'text/html',
    'json': 'application/json', 'csv': 'text/csv', 'xml': 'application/xml', 'js': 'text/javascript',
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp', 'gif': 'image/gif', 'bmp': 'image/bmp', 'tiff': 'image/tiff', 'ico': 'image/x-icon', 'avif': 'image/avif',
    'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'ogg': 'audio/ogg', 'opus': 'audio/opus', 'm4a': 'audio/mp4', 'flac': 'audio/flac', 'aac': 'audio/aac',
    'mp4': 'video/mp4', 'mkv': 'video/x-matroska', 'webm': 'video/webm', 'avi': 'video/x-msvideo', 'mov': 'video/quicktime', '3gp': 'video/3gpp',
    'doc': 'application/msword', 'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xls': 'application/vnd.ms-excel', 'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'ppt': 'application/vnd.ms-powerpoint', 'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'odt': 'application/vnd.oasis.opendocument.text', 'ods': 'application/vnd.oasis.opendocument.spreadsheet', 'odp': 'application/vnd.oasis.opendocument.presentation',
    'rtf': 'application/rtf', 'epub': 'application/epub+zip',
}
EXT_BY_MIME = {
    'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif', 'video/mp4': 'mp4',
    'audio/ogg': 'ogg', 'audio/mpeg': 'mp3', 'audio/mp4': 'm4a', 'application/zip': 'zip', 'application/pdf': 'pdf',
    'application/x-zip-compressed': 'zip', 'text/plain': 'txt',
}
IMAGE_EXT = {'png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'tiff', 'tif', 'ico', 'avif', 'heic'}
AUDIO_EXT = {'mp3', 'wav', 'ogg', 'opus', 'm4a', 'flac', 'aac', 'wma', 'amr'}
VIDEO_EXT = {'mp4', 'mkv', 'webm', 'avi', 'mov', '3gp', 'flv'}
OFFICE_EXT = {'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp', 'rtf'}
PANDOC_EXT = {'md', 'markdown', 'html', 'htm', 'docx', 'odt', 'epub', 'rst', 'tex', 'txt'}
PANDOC_OUT = {'md', 'html', 'docx', 'odt', 'epub', 'rst', 'tex', 'txt'}
TEXT_EXT = {
    'txt', 'md', 'markdown', 'log', 'json', 'csv', 'tsv', 'xml', 'yaml', 'yml', 'ini', 'conf', 'toml',
    'js', 'mjs', 'cjs', 'ts', 'jsx', 'tsx', 'py', 'java', 'c', 'cpp', 'h', 'hpp', 'cs', 'go', 'rs', 'php', 'rb', 'sh', 'sql', 'css', 'lua', 'kt', 'swift',
}
PLAIN_EXT = {'txt', 'log'}
MD_EXT = {'md', 'markdown'}

_bin_cache = {}


def _warn(*parts):
    print(*parts, file=sys.stderr)


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _g(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def _msg(err):
    return str(err)


def _js_round(x):
    return int(math.floor(x + 0.5))


def _basename(p):
    return str(p).rstrip('/').split('/')[-1]


def _ext_of(p):
    return os.path.splitext(str(p))[1][1:].lower()


def _strip_ext(name):
    stem = os.path.splitext(name)[0]
    return stem or name


def _fmt_size(n):
    if n >= MB:
        return f'{n / MB:.1f} MB'
    return f'{max(1, _js_round(n / 1024))} KB'


def _rel_path(p):
    rel = os.path.relpath(p, ROOT)
    return rel if rel and rel != '' else '.'


def _is_inside(child, parent):
    r = os.path.relpath(child, parent)
    return r in ('', '.') or (not r.startswith('..') and not os.path.isabs(r))


def _sanitize_name(name, fallback='file'):
    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', _basename(str(name or '')))
    clean = re.sub(r'^\.+', '', clean)[:120]
    return clean or fallback


def _ensure_dir(d):
    os.makedirs(d, exist_ok=True)
    return d


def _unique_dir(base):
    if not os.path.exists(base):
        return base
    for i in range(2, 100):
        candidate = f'{base}-{i}'
        if not os.path.exists(candidate):
            return candidate
    return f'{base}-{int(time.time() * 1000)}'


def _rm(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def _clean_tmp():
    try:
        tmp = mcp.AI_TMP_DIR
        if not os.path.exists(tmp):
            return
        now = time.time() * 1000

        def expired(full):
            try:
                return now - os.stat(full).st_mtime * 1000 > TMP_MAX_AGE_MS
            except OSError:
                return False

        for name in os.listdir(tmp):
            full = os.path.join(tmp, name)
            if name in HOLDER_DIRS:
                for child in os.listdir(full):
                    child_path = os.path.join(full, child)
                    if expired(child_path):
                        _rm(child_path)
            elif expired(full):
                _rm(full)
    except Exception as e:
        _warn('[convert] tmp cleanup failed:', _msg(e))


def _resolve_allowed(p):
    abs_path = os.path.abspath(os.path.join(ROOT, str(p or '').strip()))
    if not mcp.ctx()['isOwner'] and not _is_inside(abs_path, mcp.AI_TMP_DIR):
        raise Exception('Only the bot owner may use paths outside the tmp folder.')
    return abs_path


async def _get_attached_media():
    c = mcp.ctx()
    m = c['currentM']
    if m is None:
        return None
    try:
        from baileys_lite import downloadMediaMessage
        types = ['documentMessage', 'imageMessage', 'videoMessage', 'audioMessage', 'stickerMessage']
        message = _g(m, 'message') or {}
        direct = next((t for t in message if t in types), None)
        info = mcp.get_context_info(m)
        quoted = _g(info, 'quotedMessage')
        quoted_type = next((t for t in types if quoted and _g(quoted, t)), None) if quoted else None
        mtype = direct or quoted_type
        if not mtype:
            return None
        if direct:
            target = m
        else:
            key = dict(_g(m, 'key') or {})
            key['id'] = _g(info, 'stanzaId')
            target = {'message': quoted, 'key': key}
        opts_ctx = {'reuploadRequest': getattr(c['conn'], 'updateMediaMessage', None)} if c['conn'] is not None else {}
        buffer = await _maybe_await(downloadMediaMessage(target, 'buffer', {}, opts_ctx))
        if not buffer:
            return None
        node = _g(_g(target, 'message') or {}, mtype) or {}
        mime = str(_g(node, 'mimetype') or '').split(';')[0]
        ext = EXT_BY_MIME.get(mime, 'bin')
        file_name = _sanitize_name(_g(node, 'fileName') or f"{mtype.replace('Message', '')}-{int(time.time() * 1000)}.{ext}")
        return {'buffer': bytes(buffer), 'fileName': file_name}
    except Exception as e:
        _warn('[convert] could not read attached media:', _msg(e))
        return None


async def _maybe_await(value):
    if hasattr(value, '__await__'):
        return await value
    return value


async def _save_attached():
    media = await _get_attached_media()
    if not media:
        return None
    d = _ensure_dir(os.path.join(mcp.AI_TMP_DIR, 'attached'))
    full = os.path.join(d, f"{int(time.time() * 1000)}-{media['fileName']}")
    with open(full, 'wb') as f:
        f.write(media['buffer'])
    return {'full': full, 'fileName': media['fileName']}


async def _send_doc(buffer, file_name, caption=None):
    c = mcp.ctx()
    if c['conn'] is None or not c['currentJid']:
        raise Exception('WA connection not ready')
    if len(buffer) > MAX_SEND_BYTES:
        raise Exception(f'File is too large to send ({_fmt_size(len(buffer))}, limit {_fmt_size(MAX_SEND_BYTES)}).')
    content = {
        'document': buffer,
        'mimetype': MIME.get(_ext_of(file_name), 'application/octet-stream'),
        'fileName': file_name,
    }
    if caption:
        content['caption'] = caption
    await c['conn'].sendMessage(c['currentJid'], content, {'quoted': c['currentM']})


async def _run(cmd, args, timeout=EXEC_TIMEOUT_S):
    proc = await asyncio.create_subprocess_exec(
        cmd, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise Exception('command timed out')
    stdout = out.decode('utf-8', 'replace')
    stderr = err.decode('utf-8', 'replace')
    if proc.returncode != 0:
        raw = stderr.strip() or f'Command failed: {cmd}'
        detail = ' '.join(raw.split('\n')[-3:])[:300]
        raise Exception(detail or 'command failed')
    return {'stdout': stdout, 'stderr': stderr}


def _has_bin(name):
    if name in _bin_cache:
        return _bin_cache[name]
    ok = shutil.which(name) is not None
    _bin_cache[name] = ok
    return ok


def _first_bin(names):
    for n in names:
        if _has_bin(n):
            return n
    return None


def _collect_zip_entries(inputs):
    entries = []
    used = set()
    state = {'total': 0}

    def unique_name(name):
        if name not in used:
            used.add(name)
            return name
        ext = os.path.splitext(name)[1]
        stem = name[:len(name) - len(ext)]
        for i in range(2, 10000):
            candidate = f'{stem}-{i}{ext}'
            if candidate not in used:
                used.add(candidate)
                return candidate
        return name

    def add_file(abs_path, name):
        size = os.path.getsize(abs_path)
        state['total'] += size
        if state['total'] > MAX_ZIP_INPUT_BYTES:
            raise Exception(f'Input is too large to zip (limit {_fmt_size(MAX_ZIP_INPUT_BYTES)}).')
        if len(entries) >= MAX_ZIP_ENTRIES:
            raise Exception(f'Too many files to zip (limit {MAX_ZIP_ENTRIES}).')
        entries.append({'abs': abs_path, 'name': unique_name(name)})

    def walk(dir_abs, prefix, keep_modules):
        for child in sorted(os.listdir(dir_abs)):
            full = os.path.join(dir_abs, child)
            if os.path.islink(full):
                continue
            if os.path.isdir(full):
                if child in ALWAYS_SKIP_DIRS:
                    continue
                if child == MODULE_DIR and not keep_modules:
                    continue
                walk(full, f'{prefix}/{child}', keep_modules)
            elif os.path.isfile(full):
                add_file(full, f'{prefix}/{child}')

    for inp in inputs:
        label = _basename(inp) or 'root'
        real = os.path.realpath(inp)
        keep_modules = MODULE_DIR in inp.split(os.sep) or MODULE_DIR in real.split(os.sep)
        if os.path.isdir(real):
            walk(real, label, keep_modules)
        elif os.path.isfile(real):
            add_file(real, label)
    return {'entries': entries, 'total': state['total']}


async def _build_zip_buffer(entries):
    zf = converter_mod.ZipFile()
    for e in entries:
        with open(e['abs'], 'rb') as f:
            zf.file(e['name'], f.read())
    return await zf.generateAsync(type='nodebuffer', compression='DEFLATE')


def _inspect_zip(buf):
    eocd = -1
    for i in range(len(buf) - 22, max(0, len(buf) - 65557) - 1, -1):
        if struct.unpack_from('<I', buf, i)[0] == 0x06054B50:
            eocd = i
            break
    if eocd < 0:
        raise Exception('This is not a valid zip file.')
    count = struct.unpack_from('<H', buf, eocd + 10)[0]
    off = struct.unpack_from('<I', buf, eocd + 16)[0]
    total = 0
    for _ in range(count):
        if off + 46 > len(buf) or struct.unpack_from('<I', buf, off)[0] != 0x02014B50:
            break
        total += struct.unpack_from('<I', buf, off + 24)[0]
        off += 46 + struct.unpack_from('<H', buf, off + 28)[0] + struct.unpack_from('<H', buf, off + 30)[0] + struct.unpack_from('<H', buf, off + 32)[0]
    return {'count': count, 'total': total}


HELV = {
    ' ': 278, '!': 278, '"': 355, '#': 556, '$': 556, '%': 889, '&': 667, "'": 191, '(': 333, ')': 333, '*': 389, '+': 584, ',': 278, '-': 333, '.': 278, '/': 278,
    ':': 278, ';': 278, '<': 584, '=': 584, '>': 584, '?': 556, '@': 1015,
    'A': 667, 'B': 667, 'C': 722, 'D': 722, 'E': 667, 'F': 611, 'G': 778, 'H': 722, 'I': 278, 'J': 500, 'K': 667, 'L': 556, 'M': 833, 'N': 722, 'O': 778, 'P': 667, 'Q': 778, 'R': 722, 'S': 667, 'T': 611, 'U': 722, 'V': 667, 'W': 944, 'X': 667, 'Y': 667, 'Z': 611,
    '[': 278, '\\': 278, ']': 278, '^': 469, '_': 556, '`': 333,
    'a': 556, 'b': 556, 'c': 500, 'd': 556, 'e': 556, 'f': 278, 'g': 556, 'h': 556, 'i': 222, 'j': 222, 'k': 500, 'l': 222, 'm': 833, 'n': 556, 'o': 556, 'p': 556, 'q': 556, 'r': 333, 's': 500, 't': 278, 'u': 556, 'v': 500, 'w': 722, 'x': 500, 'y': 500, 'z': 500,
    '{': 334, '|': 260, '}': 334, '~': 584,
}
for _d in range(10):
    HELV[str(_d)] = 556

WINANSI = {
    '‘': 0x91, '’': 0x92, '“': 0x93, '”': 0x94, '–': 0x96, '—': 0x97, '•': 0x95, '…': 0x85, '€': 0x80, ' ': 0x20,
}


def _to_winansi(s):
    out = []
    for ch in str(s).replace('\t', '    '):
        code = ord(ch)
        if ch in WINANSI:
            out.append(chr(WINANSI[ch]))
        elif 32 <= code < 127:
            out.append(ch)
        elif 160 <= code <= 255:
            out.append(ch)
        else:
            out.append('?')
    return ''.join(out)


def _char_width(ch, font, size):
    if font == 'F3':
        return 0.6 * size
    w = HELV.get(ch, 556) / 1000 * size
    return w * 1.08 if font == 'F2' else w


def _wrap_text(text, font, size, max_w):
    if font == 'F3':
        cols = max(10, int(math.floor(max_w / (0.6 * size))))
        if not text:
            return ['']
        return [text[i:i + cols] for i in range(0, len(text), cols)]
    out = []
    space_w = _char_width(' ', font, size)
    cur = ''
    cur_w = 0.0
    for word in text.split(' '):
        word_w = sum(_char_width(ch, font, size) for ch in word)
        if word_w > max_w:
            if cur:
                out.append(cur)
                cur = ''
                cur_w = 0.0
            piece = ''
            piece_w = 0.0
            for ch in word:
                w = _char_width(ch, font, size)
                if piece_w + w > max_w:
                    out.append(piece)
                    piece = ''
                    piece_w = 0.0
                piece += ch
                piece_w += w
            cur = piece
            cur_w = piece_w
            continue
        need = cur_w + space_w + word_w if cur else word_w
        if need > max_w:
            out.append(cur)
            cur = word
            cur_w = word_w
        else:
            cur = f'{cur} {word}' if cur else word
            cur_w = need
    out.append(cur)
    return out


def _esc_pdf(s):
    return s.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


class _TextPdf:
    def __init__(self):
        self.W = 595
        self.H = 842
        self.M = 50
        self.pages = []
        self.ops = []
        self.y = self.H - self.M

    def new_page(self):
        if self.ops:
            self.pages.append('\n'.join(self.ops))
        self.ops = []
        self.y = self.H - self.M

    def line(self, text, font, size, leading, indent=0):
        if self.y - leading < self.M:
            self.new_page()
        self.y -= leading
        if text:
            self.ops.append(f'BT /{font} {size} Tf {self.M + indent} {self.y:.2f} Td ({_esc_pdf(text)}) Tj ET')

    def gap(self, h):
        self.y -= h
        if self.y < self.M:
            self.new_page()

    def block(self, text, font, size, leading, indent=0):
        max_w = self.W - self.M * 2 - indent
        for ln in _wrap_text(_to_winansi(text), font, size, max_w):
            self.line(ln, font, size, leading, indent)

    def add_document(self, name, text, ext, show_name):
        if show_name:
            self.gap(6)
            self.block(name, 'F2', 13, 17)
            self.gap(4)
        lines = str(text).replace('\r\n', '\n').replace('\r', '\n').split('\n')
        if ext not in PLAIN_EXT and ext not in MD_EXT:
            for ln in lines:
                self.block(ln, 'F3', 9, 11)
            return
        if ext in PLAIN_EXT:
            for ln in lines:
                self.block(ln, 'F1', 11, 14)
            return
        in_code = False
        for raw in lines:
            if re.match(r'^\s*```', raw):
                in_code = not in_code
                continue
            if in_code:
                self.block(raw, 'F3', 9, 11, 10)
                continue
            heading = re.match(r'^(#{1,6})\s+(.*)$', raw)
            if heading:
                size = [20, 17, 15, 13, 12, 11][len(heading.group(1)) - 1]
                self.gap(5)
                self.block(_md_clean(heading.group(2)), 'F2', size, size + 4)
                self.gap(2)
            elif re.match(r'^\s*[-*+]\s+', raw):
                self.block('• ' + _md_clean(re.sub(r'^\s*[-*+]\s+', '', raw, count=1)), 'F1', 11, 14, 8)
            elif not raw.strip():
                self.gap(7)
            else:
                self.block(_md_clean(raw), 'F1', 11, 14)

    def finish(self):
        if self.ops or not self.pages:
            self.pages.append('\n'.join(self.ops))
        objs = []

        def add(body):
            if isinstance(body, str):
                body = body.encode('latin-1', 'replace')
            objs.append(body)
            return len(objs)

        add('<< /Type /Catalog /Pages 2 0 R >>')
        add('')
        add('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>')
        add('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>')
        add('<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>')
        kids = []
        for content in self.pages:
            data = zlib.compress(content.encode('latin-1', 'replace'))
            cid = add(b''.join([
                f'<< /Length {len(data)} /Filter /FlateDecode >>\nstream\n'.encode('latin-1'),
                data,
                b'\nendstream',
            ]))
            pid = add(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.W} {self.H}] /Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> /Contents {cid} 0 R >>')
            kids.append(f'{pid} 0 R')
        objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode('latin-1')
        chunks = [b'%PDF-1.4\n']
        offsets = []
        pos = len(chunks[0])
        for i, body in enumerate(objs):
            offsets.append(pos)
            part = b''.join([f'{i + 1} 0 obj\n'.encode('latin-1'), body, b'\nendobj\n'])
            chunks.append(part)
            pos += len(part)
        xref = f'xref\n0 {len(objs) + 1}\n0000000000 65535 f \n'
        for o in offsets:
            xref += f'{o:010d} 00000 n \n'
        xref += f'trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{pos}\n%%EOF\n'
        chunks.append(xref.encode('latin-1'))
        return b''.join(chunks)


def _md_clean(s):
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
    s = re.sub(r'__(.+?)__', r'\1', s)
    s = re.sub(r'`([^`]+)`', r'\1', s)
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', s)
    return s


def _html_to_text(html):
    s = str(html)
    s = re.sub(r'<(script|style)[\s\S]*?</\1>', '', s, flags=re.I)
    s = re.sub(r'</(p|div|h[1-6]|li|tr|section|article|header|footer)>', '\n', s, flags=re.I)
    s = re.sub(r'<(br|hr)\s*/?>', '\n', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    s = s.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")
    return re.sub(r'\n{3,}', '\n\n', s)


def _text_files_to_pdf(paths):
    pdf = _TextPdf()
    for i, p in enumerate(paths):
        if i > 0:
            pdf.new_page()
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
        pdf.add_document(_basename(p), text, _ext_of(p), len(paths) > 1)
    return pdf.finish()


async def _office_convert(in_path, to, out_dir):
    binary = _first_bin(['soffice', 'libreoffice'])
    if not binary:
        raise Exception('LibreOffice (soffice) is not installed on the server, so office documents cannot be converted.')
    profile = os.path.join(mcp.AI_TMP_DIR, '.lo-profile')
    _ensure_dir(profile)
    await _run(binary, [f'-env:UserInstallation=file://{profile}', '--headless', '--convert-to', to, '--outdir', out_dir, in_path])
    stem = os.path.splitext(_basename(in_path))[0]
    produced = os.path.join(out_dir, f"{stem}.{to.split(':')[0]}")
    if not os.path.exists(produced):
        raise Exception('LibreOffice finished but produced no output file.')
    return produced


async def _html_to_pdf(in_path, out):
    if _has_bin('wkhtmltopdf'):
        await _run('wkhtmltopdf', ['--quiet', '--enable-local-file-access', in_path, out])
        return ''
    chrome = _first_bin(['chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable'])
    if chrome:
        await _run(chrome, ['--headless', '--no-sandbox', '--disable-gpu', f'--print-to-pdf={out}', f'file://{in_path}'])
        return ''
    pdf = _TextPdf()
    with open(in_path, 'r', encoding='utf-8', errors='replace') as f:
        html = f.read()
    pdf.add_document(_basename(in_path), _html_to_text(html), 'txt', False)
    with open(out, 'wb') as f:
        f.write(pdf.finish())
    return 'No HTML renderer (wkhtmltopdf/chromium) is installed, so the HTML was converted as plain text without layout.'


async def _convert_to_pdf(paths, out_dir, base_name):
    exts = [_ext_of(p) for p in paths]
    out = os.path.join(out_dir, f'{base_name}.pdf')
    if all(e in IMAGE_EXT for e in exts):
        data = await converter_mod.img2pdf(paths)
        with open(out, 'wb') as f:
            f.write(data)
        return {'out': out, 'note': ''}
    if all(e in TEXT_EXT for e in exts):
        with open(out, 'wb') as f:
            f.write(_text_files_to_pdf(paths))
        return {'out': out, 'note': ''}
    if len(paths) > 1:
        raise Exception('Multiple inputs for PDF must be all images or all text files. Convert HTML/office files one at a time.')
    if exts[0] in ('html', 'htm'):
        note = await _html_to_pdf(paths[0], out)
        return {'out': out, 'note': note}
    if exts[0] in OFFICE_EXT:
        produced = await _office_convert(paths[0], 'pdf', out_dir)
        if produced != out:
            os.replace(produced, out)
        return {'out': out, 'note': ''}
    raise Exception(f"Cannot convert .{exts[0] or 'unknown'} to PDF. Supported: images, text/code files, html, and office documents.")


async def _convert_generic(in_path, to, out_dir, out_name):
    ext = _ext_of(in_path)
    out = os.path.join(out_dir, out_name)
    if ext in TEXT_EXT and to == 'txt' and ext != 'html':
        shutil.copyfile(in_path, out)
        return out
    if ext in IMAGE_EXT and to in IMAGE_EXT:
        magick = _first_bin(['magick', 'convert'])
        if magick:
            await _run(magick, [in_path if to in ('gif', 'webp') else f'{in_path}[0]', out])
            return out
        if _has_bin('ffmpeg'):
            await _run(ffmpeg_mod.FFMPEG, ['-y', '-i', in_path, out])
            return out
        raise Exception('Neither ImageMagick nor ffmpeg is installed, so images cannot be converted.')
    if (ext in AUDIO_EXT or ext in VIDEO_EXT) and (to in AUDIO_EXT or to in VIDEO_EXT or to == 'gif'):
        if not _has_bin('ffmpeg'):
            raise Exception('ffmpeg is not installed on the server, so audio/video cannot be converted.')
        drop_video = ext in VIDEO_EXT and to in AUDIO_EXT
        await _run(ffmpeg_mod.FFMPEG, ['-y', '-i', in_path] + (['-vn'] if drop_video else []) + [out])
        return out
    if ext == 'pdf' and to == 'txt':
        if not _has_bin('pdftotext'):
            raise Exception('pdftotext (poppler-utils) is not installed on the server.')
        await _run('pdftotext', ['-layout', in_path, out])
        return out
    if ext == 'pdf' and to in ('png', 'jpg'):
        if not _has_bin('pdftoppm'):
            raise Exception('pdftoppm (poppler-utils) is not installed on the server.')
        prefix = os.path.join(out_dir, _strip_ext(out_name))
        await _run('pdftoppm', ['-png' if to == 'png' else '-jpeg', '-r', '150', '-f', '1', '-l', '1', '-singlefile', in_path, prefix])
        return f'{prefix}.{to}'
    if ext in PANDOC_EXT and to in PANDOC_OUT and _has_bin('pandoc'):
        await _run('pandoc', [in_path, '-o', out])
        return out
    if ext in OFFICE_EXT or to in OFFICE_EXT or to == 'csv' or to == 'odt':
        produced = await _office_convert(in_path, to, out_dir)
        if produced != out:
            os.replace(produced, out)
        return out
    raise Exception(f"Conversion from .{ext or 'unknown'} to .{to} is unavailable (or the needed tool is not installed on the server).")


async def _resolve_inputs(inputs):
    items = [str(s or '').strip() for s in (inputs if isinstance(inputs, list) else [])]
    items = [s for s in items if s]
    if items:
        resolved = []
        for p in items:
            abs_path = _resolve_allowed(p)
            if not os.path.exists(abs_path):
                raise Exception(f'File not found: {p}')
            if os.path.isdir(abs_path):
                raise Exception(f'{p} is a folder, not a file. Use create_zip for folders.')
            resolved.append(abs_path)
        return resolved
    saved = await _save_attached()
    if not saved:
        raise Exception('No input: pass "inputs" or attach/reply to a file.')
    return [saved['full']]


async def _exec_create_zip(args=None, **kw):
    a = _args(args, kw)
    paths = a.get('paths')
    file_name = a.get('file_name')
    send = a.get('send')
    if send is None:
        send = True
    _clean_tmp()
    try:
        if not isinstance(paths, list) or not paths:
            return 'Failed: paths is required (at least one file or folder).'
        inputs = []
        for p in paths:
            abs_path = _resolve_allowed(p)
            if not os.path.exists(abs_path):
                raise Exception(f'Not found: {p}')
            inputs.append(abs_path)
        collected = _collect_zip_entries(inputs)
        entries = collected['entries']
        total = collected['total']
        if not entries:
            return 'Failed: there are no files to zip (the folders are empty or only contain skipped items).'
        buffer = await _build_zip_buffer(entries)
        base = _sanitize_name(_strip_ext(file_name) if file_name else _basename(inputs[0]), 'archive')
        name = f'{base}.zip'
        d = _ensure_dir(os.path.join(mcp.AI_TMP_DIR, 'zips'))
        saved = os.path.join(d, f'{int(time.time() * 1000)}-{name}')
        with open(saved, 'wb') as f:
            f.write(buffer)
        if not send:
            return f'Zip saved: {_rel_path(saved)} ({len(entries)} files, {_fmt_size(total)} before compression, {_fmt_size(len(buffer))} zipped).'
        await _send_doc(buffer, name)
        return f'[ALREADY SENT] {name} sent ({len(entries)} files, {_fmt_size(len(buffer))}).'
    except Exception as e:
        _warn('[create_zip] Error:', _msg(e))
        return f'Failed: {_msg(e)}'


async def _exec_extract_zip(args=None, **kw):
    a = _args(args, kw)
    file_path = a.get('file_path')
    folder_name = a.get('folder_name')
    _clean_tmp()
    try:
        if str(file_path or '').strip():
            abs_path = _resolve_allowed(file_path)
            if not os.path.isfile(abs_path):
                return f'Failed: file not found: {file_path}'
            if os.path.getsize(abs_path) > MAX_ZIP_FILE_BYTES:
                return f'Failed: zip is too large (limit {_fmt_size(MAX_ZIP_FILE_BYTES)}).'
            with open(abs_path, 'rb') as f:
                buffer = f.read()
            zip_name = re.sub(r'^\d{10,}-', '', _basename(abs_path))
        else:
            media = await _get_attached_media()
            if not media:
                return 'Failed: no zip given. Attach/reply to a zip file or pass file_path.'
            buffer = media['buffer']
            zip_name = media['fileName']
        if len(buffer) > MAX_ZIP_FILE_BYTES:
            return f'Failed: zip is too large (limit {_fmt_size(MAX_ZIP_FILE_BYTES)}).'
        info = _inspect_zip(buffer)
        if info['count'] > MAX_EXTRACT_ENTRIES:
            return f"Failed: zip has too many entries ({info['count']}, limit {MAX_EXTRACT_ENTRIES})."
        if info['total'] > MAX_EXTRACT_TOTAL_BYTES:
            return f"Failed: zip would expand to {_fmt_size(info['total'])} (limit {_fmt_size(MAX_EXTRACT_TOTAL_BYTES)})."
        folder = _sanitize_name(folder_name or _strip_ext(zip_name), 'extracted')
        dest = _unique_dir(os.path.join(mcp.AI_TMP_DIR, folder))
        _ensure_dir(dest)
        results = await converter_mod.unzip(buffer, {'outputDir': dest})
        files = [r for r in results if not r.get('isDirectory')]
        total_size = sum((r.get('size') or 0) for r in files)
        shown = '\n'.join(f"{r['name']} ({_fmt_size(r.get('size') or 0)})" for r in files[:LISTING_LIMIT])
        more = f'\n... and {len(files) - LISTING_LIMIT} more files' if len(files) > LISTING_LIMIT else ''
        return (
            f'[Listing for YOU TO READ/ANALYZE only — summarize it for the user in your own words, never paste it raw]\n'
            f'Extracted {len(files)} files ({_fmt_size(total_size)}) from {zip_name} to {_rel_path(dest)}\n{shown}{more}'
        )
    except Exception as e:
        _warn('[extract_zip] Error:', _msg(e))
        return f'Failed: {_msg(e)}'


async def _exec_convert_file(args=None, **kw):
    a = _args(args, kw)
    to = a.get('to')
    inputs = a.get('inputs')
    file_name = a.get('file_name')
    send = a.get('send')
    if send is None:
        send = True
    _clean_tmp()
    try:
        target = str(to or '').lower()
        target = re.sub(r'^\.', '', target).strip()
        if target == 'jpeg':
            target = 'jpg'
        if not target:
            return 'Failed: "to" (target format) is required.'
        paths = await _resolve_inputs(inputs)
        out_dir = _ensure_dir(os.path.join(mcp.AI_TMP_DIR, 'converted', str(int(time.time() * 1000))))
        first_base = _strip_ext(re.sub(r'^\d{10,}-', '', _basename(paths[0])))
        base_name = _sanitize_name(_strip_ext(file_name) if file_name else first_base, 'converted')
        note = ''
        if target == 'zip':
            entries = _collect_zip_entries(paths)['entries']
            out = os.path.join(out_dir, f'{base_name}.zip')
            with open(out, 'wb') as f:
                f.write(await _build_zip_buffer(entries))
        elif target == 'pdf':
            res = await _convert_to_pdf(paths, out_dir, base_name)
            out = res['out']
            note = res['note']
        else:
            if len(paths) > 1:
                return 'Failed: this conversion accepts only one input file. Only PDF and ZIP accept several.'
            out = await _convert_generic(paths[0], target, out_dir, f'{base_name}.{target}')
        with open(out, 'rb') as f:
            buffer = f.read()
        out_name = _basename(out)
        note_text = f' Note: {note}' if note else ''
        if not send:
            return f'Converted: {_rel_path(out)} ({_fmt_size(len(buffer))}).{note_text}'
        await _send_doc(buffer, out_name)
        return f'[ALREADY SENT] {out_name} sent ({_fmt_size(len(buffer))}).{note_text}'
    except Exception as e:
        _warn('[convert_file] Error:', _msg(e))
        return f'Failed: {_msg(e)}'


async def _exec_send_multiple_files(args=None, **kw):
    a = _args(args, kw)
    paths = a.get('paths')
    caption = a.get('caption')
    _clean_tmp()
    try:
        if not isinstance(paths, list) or not paths:
            return 'Failed: paths is required (at least one file or folder).'
        files = []

        def walk(d):
            for child in sorted(os.listdir(d)):
                full = os.path.join(d, child)
                if os.path.islink(full):
                    continue
                if os.path.isdir(full):
                    if child not in ALWAYS_SKIP_DIRS and child != MODULE_DIR:
                        walk(full)
                elif os.path.isfile(full):
                    files.append(full)

        for p in paths:
            abs_path = _resolve_allowed(p)
            if not os.path.exists(abs_path):
                raise Exception(f'Not found: {p}')
            if os.path.islink(abs_path):
                continue
            if os.path.isdir(abs_path):
                walk(abs_path)
            elif os.path.isfile(abs_path):
                files.append(abs_path)
        if not files:
            return 'Failed: no files found to send.'
        if len(files) > MAX_MULTI_FILES:
            return f'Failed: {len(files)} files is too many for one call (limit {MAX_MULTI_FILES}). Use create_zip to send them as one archive.'
        sent = 0
        skipped = []
        for file_path in files:
            try:
                size = os.path.getsize(file_path)
                if size > 100 * MB:
                    skipped.append(f'{_basename(file_path)} (too large)')
                    continue
                if not size:
                    skipped.append(f'{_basename(file_path)} (empty)')
                    continue
                with open(file_path, 'rb') as f:
                    data = f.read()
                await _send_doc(data, _basename(file_path), caption)
                sent += 1
                await asyncio.sleep(SEND_GAP_S)
            except Exception as e:
                skipped.append(f'{_basename(file_path)} ({_msg(e)[:40]})')
        skip_note = f" Skipped: {', '.join(skipped[:5])}{', ...' if len(skipped) > 5 else ''}." if skipped else ''
        return f'[ALREADY SENT] Sent {sent} of {len(files)} files.{skip_note}'
    except Exception as e:
        _warn('[send_multiple_files] Error:', _msg(e))
        return f'Failed: {_msg(e)}'


default = [
    {
        'name': 'create_zip',
        'description': (
            'Pack files and/or whole folders into one .zip archive and send it to the user as a document. Use it whenever the '
            'user wants a folder or many files delivered together (e.g. "send me the whole plugins folder", "zip these files", '
            '"send all of lib/package/ai"). Folders are included recursively. When zipping a parent folder (e.g. the project root) '
            'the nested node_modules and .git folders are skipped automatically, but if a path itself points inside node_modules '
            '(e.g. "node_modules/baileys") that package is zipped normally, so installed libraries/modules CAN be sent: every '
            'installed package lives at node_modules/<package name>. Limits: ~300 MB of input, ~200 MB zip to send. Regular users '
            'can only zip things inside the tmp folder (e.g. extracted archives); the bot owner can zip any server path.'
        ),
        'parameters': {
            'paths': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Files and/or folders to include. Relative to the bot root or absolute.', 'required': True},
            'file_name': {'type': 'string', 'description': 'Name of the zip file, e.g. "ai-folder.zip" (optional, defaults to the first path name).', 'required': False},
            'send': {'type': 'boolean', 'description': 'Send the zip to the chat (default true). false = only save it under the tmp folder/zips and report the path.', 'required': False},
        },
        'execute': _exec_create_zip,
    },
    {
        'name': 'extract_zip',
        'description': (
            'Extract a .zip archive into the tmp folder/<zip name>/ on the server. Use the zip the user attached or replied to '
            '(leave file_path empty), or a zip that already exists on the server. Returns the folder it was extracted to and a '
            'listing of its contents, so you can then read files, send some of them (send_multiple_files / send_as_file), or '
            're-pack them (create_zip). Zip bombs and path traversal are blocked (max 10000 entries, ~800 MB uncompressed). '
            'Extracted folders are auto-deleted after 24 hours.'
        ),
        'parameters': {
            'file_path': {'type': 'string', 'description': 'Path of a zip file on the server (optional). Leave empty to use the zip attached to / replied to in this message.', 'required': False},
            'folder_name': {'type': 'string', 'description': 'Folder name under the tmp folder to extract into (optional, defaults to the zip name without extension).', 'required': False},
        },
        'execute': _exec_extract_zip,
    },
    {
        'name': 'convert_file',
        'description': (
            'Convert files to another format and send the result as a document. Input is one or more server files ("inputs") or, '
            'when empty, the file/image/audio/video attached to or replied to in this message. Supported targets: "pdf" (from '
            'images — several images become a multi-page PDF; from text/code/markdown/log/json/csv files, built in; from html and '
            'office documents docx/xlsx/pptx/odt when wkhtmltopdf/chromium/LibreOffice exist on the server); "zip" (packs the '
            'inputs); image to image (png/jpg/webp/gif/bmp/tiff, needs ImageMagick or ffmpeg); audio/video conversion such as mp4 '
            'to mp3 or wav to ogg (needs ffmpeg); documents such as md/html/docx/odt/epub/txt (needs pandoc or LibreOffice); pdf to '
            'txt/png/jpg (needs poppler-utils). If a needed tool is missing on the server, the result says so: tell the user plainly '
            'instead of retrying. Regular users can only use files inside the tmp folder or attached to the message; the owner can '
            'use any server path. Text PDFs use a built-in font, so non-Latin characters (e.g. emoji, CJK) become "?".'
        ),
        'parameters': {
            'to': {'type': 'string', 'description': 'Target format without dot, e.g. "pdf", "zip", "png", "mp3", "docx", "txt".', 'required': True},
            'inputs': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Files to convert (relative to the bot root or absolute). Leave empty to use the file attached to / replied to in this message.', 'required': False},
            'file_name': {'type': 'string', 'description': 'Name for the output file (optional, defaults to the input name with the new extension).', 'required': False},
            'send': {'type': 'boolean', 'description': 'Send the result to the chat (default true). false = only save it under the tmp folder/converted and report the path.', 'required': False},
        },
        'execute': _exec_convert_file,
    },
    {
        'name': 'send_multiple_files',
        'description': (
            'Send many files to the chat one by one as separate document attachments (up to 40 per call, with a short pause '
            'between them). Folders in the list are expanded to the files inside them (not recursive into node_modules/.git). Use '
            'it when the user wants several individual files, e.g. after extract_zip. If the user would rather have everything in a '
            'single archive or there are more than 40 files, use create_zip instead. Regular users can only send files inside the '
            'tmp folder; the owner can send any server path. Files over 100 MB are skipped.'
        ),
        'parameters': {
            'paths': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Files and/or folders to send. Relative to the bot root or absolute.', 'required': True},
            'caption': {'type': 'string', 'description': 'Optional caption attached to each file.', 'required': False},
        },
        'execute': _exec_send_multiple_files,
    },
]
