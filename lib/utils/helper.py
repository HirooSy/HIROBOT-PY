import asyncio
import hashlib
import importlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import sys
import time
import traceback
import types


def oc(obj, *path, default=None):
    cur = obj
    for key in path:
        if cur is None:
            return default
        try:
            if isinstance(cur, (list, tuple)):
                cur = cur[key] if isinstance(key, int) and -len(cur) <= key < len(cur) else None
            elif isinstance(cur, dict):
                cur = cur.get(key)
            else:
                cur = getattr(cur, key, None)
        except Exception:
            return default
    return default if cur is None else cur


def truthy(v):
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)):
        return v != 0 and v == v
    if isinstance(v, (str, bytes, list, dict, tuple)):
        return True if not isinstance(v, (str, bytes)) else len(v) > 0
    return True


def now_ms():
    return int(time.time() * 1000)


def to_number(value):
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if hasattr(value, 'toNumber'):
        return value.toNumber()
    try:
        return float(str(value))
    except ValueError:
        return 0


def int_or(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def format_error(err):
    if isinstance(err, BaseException):
        return ''.join(traceback.format_exception(type(err), err, err.__traceback__)).rstrip()
    return format_value(err)


def format_value(value, indent=2):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=indent, ensure_ascii=False, default=_default)
    except Exception:
        return str(value)


def _default(value):
    if isinstance(value, (bytes, bytearray)):
        return {'type': 'Buffer', 'length': len(value)}
    if hasattr(value, 'toNumber'):
        return value.toNumber()
    return str(value)


def str_to_regex(s):
    return '^' + re.sub(r'([|\\{}()\[\]^$+*?.])', r'\\\1', s)


def escape_regex(s):
    return re.escape(s)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def chunk(lst, size):
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def unique(seq):
    seen, out = set(), []
    for x in seq:
        k = x if isinstance(x, (str, int, float, tuple)) else id(x)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def ceil_div(a, b):
    return math.ceil(a / b)


def shuffle_pick(seq):
    import random
    return seq[random.randrange(len(seq))] if seq else None


def format_bytes(n):
    n = float(n or 0)
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f'{n:.2f} {units[i]}' if i else f'{int(n)} B'


def format_duration(seconds):
    seconds = int(seconds or 0)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f'{d}d')
    if h:
        parts.append(f'{h}h')
    if m:
        parts.append(f'{m}m')
    parts.append(f'{s}s')
    return ' '.join(parts)


class Mutex:
    def __init__(self):
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self._lock.acquire()

    async def __aexit__(self, *a):
        self._lock.release()


class KeyedLock:
    def __init__(self):
        self._locks = {}

    async def run_many(self, keys, task):
        uniq = sorted(set(keys))
        locks = [self._locks.setdefault(k, asyncio.Lock()) for k in uniq]
        for lk in locks:
            await lk.acquire()
        try:
            return await task()
        finally:
            for lk in reversed(locks):
                lk.release()
            for k in uniq:
                lk = self._locks.get(k)
                if lk is not None and not lk.locked():
                    self._locks.pop(k, None)

    async def run(self, key, task):
        return await self.run_many([key], task)


def spawn(coro):
    task = asyncio.ensure_future(coro)

    def done(t):
        if not t.cancelled() and t.exception():
            traceback.print_exception(type(t.exception()), t.exception(), t.exception().__traceback__)

    task.add_done_callback(done)
    return task


async def maybe_await(value):
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


class Silent(Exception):
    pass


def mimetype_of(q):
    msg = getattr(q, 'msg', None)
    if isinstance(msg, dict) and msg.get('mimetype'):
        return msg['mimetype']
    mt = getattr(q, 'mimetype', None)
    if mt:
        return mt
    if isinstance(msg, dict):
        return msg.get('mimetype') or ''
    return ''


def to_simple_number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if n < 1000:
        return str(int(n)) if n == int(n) else str(n)
    suffixes = ['', 'K', 'M', 'B', 'T', 'Qr', 'Qt', 'Sx']
    i = 0
    while n >= 1000 and i < len(suffixes) - 1:
        n /= 1000
        i += 1
    return f'{n:.2f}{suffixes[i]}'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def js_str(value):
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    return str(value)


def parse_args(argv=None, options=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    options = options or {}
    aliases = {v['alias']: k for k, v in options.items() if v.get('alias')}
    result = {'_': []}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith('--'):
            name = arg[2:]
            if '=' in name:
                key, value = name.split('=', 1)
                result[aliases.get(key, key)] = value
                i += 1
            elif i + 1 < len(argv) and not argv[i + 1].startswith('-'):
                result[aliases.get(name, name)] = argv[i + 1]
                i += 2
            else:
                result[aliases.get(name, name)] = True
                i += 1
        elif arg.startswith('-') and len(arg) > 1:
            name = arg[1:]
            if i + 1 < len(argv) and not argv[i + 1].startswith('-'):
                result[aliases.get(name, name)] = argv[i + 1]
                i += 2
            else:
                result[aliases.get(name, name)] = True
                i += 1
        else:
            result['_'].append(arg)
            i += 1
    return result


options = {'prefix': {'alias': 'p', 'description': 'Prefix characters to filter'}}

opts = parse_args(sys.argv[1:], options)

BOOLEAN_TOGGLE_KEYS = ['queue', 'autoread', 'restrict', 'self', 'nyimak', 'pconly', 'gconly', 'swonly']


def _system():
    settings = G.settings or {}
    return settings.get('system') or {}


def resolve_prefix():
    if opts.get('prefix'):
        return list(js_str(opts['prefix']))
    pre = _system().get('prefix')
    if isinstance(pre, list) and pre:
        return pre
    return ['.', '/', '!']


def get_prefix(chars):
    if isinstance(chars, (list, tuple)):
        chars = ''.join(str(c) for c in chars)
    body = ''.join('\\' + c if c in '|\\{}()[]^$+*?.-' else c for c in str(chars or ''))
    return re.compile('^[' + body + ']')


def resolve_opts():
    sys_cfg = _system()
    only = sys_cfg.get('OnlyRespondTo')
    toggles = {
        'queue': opts.get('queue', bool(sys_cfg.get('queue'))),
        'autoread': opts.get('autoread', bool(sys_cfg.get('autoRead'))),
        'restrict': opts.get('restrict', False),
        'self': opts.get('self', False),
        'nyimak': opts.get('nyimak', False),
        'pconly': opts.get('pconly', only == 'dm'),
        'gconly': opts.get('gconly', only == 'group'),
        'swonly': opts.get('swonly', only == 'status'),
    }
    merged = dict(opts)
    for key in BOOLEAN_TOGGLE_KEYS:
        if toggles[key]:
            merged[key] = True
    return merged


from lib.utils.handler import G

prefix_list = resolve_prefix()
prefix = get_prefix(prefix_list)
runtime_opts = resolve_opts()


def refresh():
    global prefix_list, prefix, runtime_opts
    prefix_list = resolve_prefix()
    prefix = get_prefix(prefix_list)
    runtime_opts = resolve_opts()


def filename_of(path):
    return os.path.abspath(str(path))


def dirname_of(path):
    full = os.path.abspath(str(path))
    if str(path).endswith(os.sep) or str(path).endswith('/'):
        return full
    if os.path.isdir(full):
        return full
    return os.path.dirname(full)


def require_from(base):
    base_dir = dirname_of(base)

    def require(name):
        if isinstance(name, str) and name.startswith('.'):
            return import_file(os.path.join(base_dir, name))
        return importlib.import_module(name)
    return require


async def check_file_exists(path):
    return await asyncio.to_thread(os.path.exists, path)


def hash_file_content(path):
    try:
        with open(path, 'rb') as fh:
            return hashlib.md5(fh.read()).hexdigest()[:12]
    except Exception:
        return f'fallback-{int(time.time() * 1000)}'


class Plugin:
    def __init__(self, module, name):
        self.module = module
        self.name = name
        self.run = getattr(module, 'run', None) or getattr(module, 'handler', None)
        self.before = getattr(module, 'before', None)
        self.after = getattr(module, 'after', None)

    def __getattr__(self, item):
        if item in ('module', 'name', 'run', 'before', 'after'):
            raise AttributeError(item)
        return getattr(self.module, item, None)

    def __call__(self, m, ctx):
        from lib.utils.handler import call_plugin
        return call_plugin(self.run, m, ctx)

    @property
    def file(self):
        return getattr(self.module, '__file__', None)


def import_file(path):
    path = os.path.abspath(path)
    key = hash_file_content(path)
    rel = os.path.relpath(path, ROOT).replace(os.sep, '.')
    if rel.endswith('.py'):
        rel = rel[:-3]
    mod_name = f'_plug_{rel}_{key}'
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class SyntaxProblem:
    def __init__(self, message, line, column, filename):
        self.message = message
        self.line = line
        self.column = column
        self.filename = filename

    def __str__(self):
        return f'{self.filename}:{self.line}\n{self.message}'


def check_syntax(source, filename='unknown'):
    if isinstance(source, (bytes, bytearray)):
        source = bytes(source).decode('utf-8', errors='replace')
    try:
        compile(source, filename, 'exec')
        return None
    except SyntaxError as e:
        return SyntaxProblem(e.msg, e.lineno or 0, e.offset or 0, filename)


def _ftyp(b, brands):
    return len(b) >= 12 and b[4:8] == b'ftyp' and b[8:12].decode('latin1') in brands


FILE_SIGNATURES = [
    ('png', 'image/png', lambda b: len(b) >= 8 and b[0] == 0x89 and b[1] == 0x50 and b[2] == 0x4e and b[3] == 0x47),
    ('jpg', 'image/jpeg', lambda b: len(b) >= 3 and b[0] == 0xff and b[1] == 0xd8 and b[2] == 0xff),
    ('gif', 'image/gif', lambda b: len(b) >= 6 and b[:4] == b'GIF8' and b[4] in (0x37, 0x39) and b[5] == 0x61),
    ('webp', 'image/webp', lambda b: len(b) >= 12 and b[:4] == b'RIFF' and b[8:12] == b'WEBP'),
    ('bmp', 'image/bmp', lambda b: len(b) >= 2 and b[:2] == b'BM'),
    ('tif', 'image/tiff', lambda b: len(b) >= 4 and (b[:4] == b'II*\x00' or b[:4] == b'MM\x00*')),
    ('ico', 'image/x-icon', lambda b: len(b) >= 4 and b[:4] == b'\x00\x00\x01\x00'),
    ('heic', 'image/heic', lambda b: len(b) >= 12 and b[4:8] == b'ftyp' and b[8:12].decode('latin1') in ('heic', 'heix', 'heim', 'heis', 'hevc', 'hevx')),
    ('avif', 'image/avif', lambda b: len(b) >= 12 and b[4:8] == b'ftyp' and b[8:12].decode('latin1') in ('avif', 'avis')),
    ('ebml', 'video/webm', lambda b: len(b) >= 4 and b[:4] == b'\x1a\x45\xdf\xa3'),
    ('mp4', 'video/mp4', lambda b: len(b) >= 12 and b[4:8] == b'ftyp' and b[8:12].decode('latin1') in ('isom', 'iso2', 'mp41', 'mp42', 'mp4v', 'avc1', 'M4V ', 'M4A ', 'dash')),
    ('mov', 'video/quicktime', lambda b: len(b) >= 12 and b[4:8] == b'ftyp' and b[8:12] == b'qt  '),
    ('avi', 'video/x-msvideo', lambda b: len(b) >= 12 and b[:4] == b'RIFF' and b[8:12] == b'AVI '),
    ('mp3', 'audio/mpeg', lambda b: len(b) >= 3 and (b[:3] == b'ID3' or (b[0] == 0xff and (b[1] & 0xe0) == 0xe0))),
    ('ogg', 'audio/ogg', lambda b: len(b) >= 4 and b[:4] == b'OggS'),
    ('wav', 'audio/wav', lambda b: len(b) >= 12 and b[:4] == b'RIFF' and b[8:12] == b'WAVE'),
    ('m4a', 'audio/mp4', lambda b: len(b) >= 12 and b[4:8] == b'ftyp' and b[8:12] == b'M4A '),
    ('flac', 'audio/x-flac', lambda b: len(b) >= 4 and b[:4] == b'fLaC'),
    ('amr', 'audio/amr', lambda b: len(b) >= 6 and b[:6] == b'#!AMR\n'),
    ('pdf', 'application/pdf', lambda b: len(b) >= 4 and b[:4] == b'%PDF'),
    ('zip', 'application/zip', lambda b: len(b) >= 4 and b[0] == 0x50 and b[1] == 0x4b and b[2] in (0x03, 0x05, 0x07) and b[3] in (0x04, 0x06, 0x08)),
    ('rar', 'application/x-rar-compressed', lambda b: len(b) >= 6 and b[:6] == b'Rar!\x1a\x07'),
    ('7z', 'application/x-7z-compressed', lambda b: len(b) >= 6 and b[:6] == b'7z\xbc\xaf\x27\x1c'),
    ('gz', 'application/gzip', lambda b: len(b) >= 2 and b[0] == 0x1f and b[1] == 0x8b),
]


def file_type_from_buffer(buf):
    if not buf:
        return None
    buf = bytes(buf)
    for ext, mime, test in FILE_SIGNATURES:
        if test(buf):
            if ext == 'ebml':
                if b'matroska' in buf[:4096]:
                    return {'ext': 'mkv', 'mime': 'video/x-matroska'}
                return {'ext': 'webm', 'mime': 'video/webm'}
            return {'ext': ext, 'mime': mime}
    return None


FILE_TYPE_PEEK_BYTES = 4100


class PeekedStream:
    def __init__(self, head, rest, file_type):
        self._head = head
        self._rest = rest
        self.file_type = file_type

    def read(self, size=-1):
        if not self._head:
            return self._rest.read(size)
        if size is None or size < 0:
            data = self._head + self._rest.read()
            self._head = b''
            return data
        if len(self._head) >= size:
            data = self._head[:size]
            self._head = self._head[size:]
            return data
        data = self._head
        self._head = b''
        return data + self._rest.read(size - len(data))


def file_type_stream(readable):
    head = readable.read(FILE_TYPE_PEEK_BYTES) or b''
    detected = file_type_from_buffer(head)
    return PeekedStream(bytes(head), readable, detected)


async def save_stream_to_file(stream, path):
    def write():
        with open(path, 'wb') as fh:
            if hasattr(stream, 'read'):
                shutil.copyfileobj(stream, fh)
            else:
                for chunk in stream:
                    fh.write(chunk)
    await asyncio.to_thread(write)


def is_readable_stream(obj):
    if obj is None:
        return False
    if getattr(obj, 'closed', False):
        return False
    if hasattr(obj, 'readable'):
        try:
            return bool(obj.readable())
        except Exception:
            return False
    return hasattr(obj, 'read')


LOG_LEVELS = {'silent': 70, 'fatal': 60, 'error': 50, 'warn': 40, 'info': 30, 'debug': 20, 'trace': 10}


def _serialize_error(err):
    if not isinstance(err, BaseException):
        return err
    import traceback
    return {'type': type(err).__name__, 'message': str(err),
            'stack': ''.join(traceback.format_exception(type(err), err, err.__traceback__))}


class Logger:
    def __init__(self, bindings=None, level='info', timestamp=None):
        self.bindings = dict(bindings or {})
        self.level = level
        self.timestamp = timestamp

    def _write(self, level_name, args):
        if LOG_LEVELS[level_name] < LOG_LEVELS.get(self.level, 30):
            return
        obj = {}
        msg = ''
        if args and isinstance(args[0], dict):
            obj = dict(args[0])
            msg = args[1] if len(args) > 1 and args[1] is not None else ''
        else:
            msg = args[0] if args and args[0] is not None else ''
        if 'err' in obj:
            obj['err'] = _serialize_error(obj['err'])
        line = {'level': LOG_LEVELS[level_name]}
        if not self.timestamp:
            line['time'] = int(time.time() * 1000)
        line.update(self.bindings)
        line.update(obj)
        line['msg'] = msg
        text = json.dumps(line, ensure_ascii=False, default=str)
        if self.timestamp:
            text = text[:1] + self.timestamp() + text[1:]
        stream = sys.stderr if level_name in ('error', 'fatal') else sys.stdout
        stream.write(text + '\n')
        stream.flush()

    def trace(self, *a):
        self._write('trace', a)

    def debug(self, *a):
        self._write('debug', a)

    def info(self, *a):
        self._write('info', a)

    def warn(self, *a):
        self._write('warn', a)

    def error(self, *a):
        self._write('error', a)

    def fatal(self, *a):
        self._write('fatal', a)

    def child(self, child_bindings=None):
        return Logger({**self.bindings, **(child_bindings or {})}, self.level, self.timestamp)


def P(opts=None):
    opts = opts or {}
    return Logger({}, opts.get('level') or 'info', opts.get('timestamp'))


def sleep(ms):
    return asyncio.sleep(ms / 1000)


def register_interval(task):
    if G.active_intervals is None:
        G.active_intervals = set()
    G.active_intervals.add(task)
    return task


async def run_every(seconds, fn):
    while True:
        await asyncio.sleep(seconds)
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as err:
            print(err, file=sys.stderr)
