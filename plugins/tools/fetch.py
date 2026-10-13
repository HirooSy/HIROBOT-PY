import json
import math
import re
from urllib.parse import quote, urlsplit, urlunsplit

from lib.compat import http
from lib.utils.handler import PluginError


NODE_BREAK = 80
NODE_COMPACT = 3
NODE_DEPTH = 2
NODE_MAX_ITEMS = 100
_IDENT = re.compile(r'[A-Za-z_][A-Za-z_0-9]*')
_CTRL = {8: '\\b', 9: '\\t', 10: '\\n', 12: '\\f', 13: '\\r'}


class _Ctx:
    def __init__(self):
        self.cur = 0
        self.indent = 0


def _quote(s):
    q = "'"
    if "'" in s:
        if '"' not in s:
            q = '"'
        elif '`' not in s and '${' not in s:
            q = '`'
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "'" and q == "'":
            out.append("\\'")
        elif ch == '\\':
            out.append('\\\\')
        elif o < 0x20 or 0x7f <= o <= 0x9f:
            out.append(_CTRL.get(o, '\\x%02X' % o))
        else:
            out.append(ch)
    return q + ''.join(out) + q


def _num(v):
    if math.isnan(v):
        return 'NaN'
    if math.isinf(v):
        return 'Infinity' if v > 0 else '-Infinity'
    if v.is_integer() and abs(v) < 1e21:
        return str(int(v))
    r = repr(v)
    if 'e' in r:
        mant, exp = r.split('e')
        e = int(exp)
        return f"{mant}e{'+' if e > 0 else '-'}{abs(e)}"
    return r


def _key(k):
    return k if _IDENT.fullmatch(k) else _quote(k)


def _group_array(output, values, ctx):
    out_len = len(output) - 1 if len(output) > NODE_MAX_ITEMS else len(output)
    sep = 2
    data_len = []
    total = 0
    max_len = 0
    for i in range(out_len):
        n = len(output[i])
        data_len.append(n)
        total += n + sep
        max_len = max(max_len, n)
    actual_max = max_len + sep
    if not (actual_max * 3 + ctx.indent < NODE_BREAK and (total / actual_max > 5 or max_len <= 6)):
        return output
    average_bias = math.sqrt(actual_max - total / len(output))
    biased_max = max(actual_max - 3 - average_bias, 1)
    columns = min(
        math.floor(math.sqrt(2.5 * biased_max * out_len) / biased_max + 0.5),
        math.floor((NODE_BREAK - ctx.indent) / actual_max),
        NODE_COMPACT * 4,
        15,
    )
    if columns <= 1:
        return output
    max_line_len = []
    for i in range(columns):
        line_len = 0
        for j in range(i, out_len, columns):
            if data_len[j] > line_len:
                line_len = data_len[j]
        max_line_len.append(line_len + sep)
    pad_start = True
    if values is not None:
        for i in range(len(output)):
            v = values[i] if i < len(values) else None
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                pad_start = False
                break
    tmp = []
    for i in range(0, out_len, columns):
        last = min(i + columns, out_len)
        line = ''
        j = i
        while j < last - 1:
            cell = output[j] + ', '
            width = max_line_len[j - i]
            line += cell.rjust(width) if pad_start else cell.ljust(width)
            j += 1
        if pad_start:
            line += output[j].rjust(max_line_len[j - i] - sep)
        else:
            line += output[j]
        tmp.append(line)
    if len(output) > NODE_MAX_ITEMS:
        tmp.append(output[out_len])
    return tmp


def _reduce(output, base, b0, b1, level, ctx, values=None, is_array=False):
    entries = len(output)
    if is_array and entries > 6:
        output = _group_array(output, values, ctx)
    if ctx.cur - level < NODE_COMPACT and entries == len(output):
        start = len(output) + ctx.indent + len(b0) + len(base) + 10
        total = len(output) + start
        fits = total + len(output) <= NODE_BREAK
        if fits:
            for entry in output:
                total += len(entry)
                if total > NODE_BREAK:
                    fits = False
                    break
        if fits and '\n' not in base:
            joined = ', '.join(output)
            if '\n' not in joined:
                return f"{base + ' ' if base else ''}{b0} {joined} {b1}"
    ind = '\n' + ' ' * ctx.indent
    return f"{base + ' ' if base else ''}{b0}{ind}  {(',' + ind + '  ').join(output)}{ind}{b1}"


def _inspect(v, level, ctx):
    if v is None:
        return 'null'
    if v is True:
        return 'true'
    if v is False:
        return 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _num(v)
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, list):
        if level > NODE_DEPTH:
            return '[Array]'
        if not v:
            return '[]'
        ctx.cur = level
        output = []
        for item in v[:NODE_MAX_ITEMS]:
            ctx.indent += 2
            output.append(_inspect(item, level + 1, ctx))
            ctx.indent -= 2
        if len(v) > NODE_MAX_ITEMS:
            rest = len(v) - NODE_MAX_ITEMS
            output.append(f"... {rest} more item{'s' if rest > 1 else ''}")
        return _reduce(output, '', '[', ']', level, ctx, values=v, is_array=True)
    if isinstance(v, dict):
        if level > NODE_DEPTH:
            return '[Object]'
        if not v:
            return '{}'
        ctx.cur = level
        output = []
        for k, item in v.items():
            ctx.indent += 2
            output.append(f'{_key(str(k))}: {_inspect(item, level + 1, ctx)}')
            ctx.indent -= 2
        return _reduce(output, '', '{', '}', level, ctx)
    return str(v)


def node_format(value):
    if isinstance(value, str):
        return value
    return _inspect(value, 0, _Ctx())


def normalize_url(text):
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        raise PluginError('Invalid URL')
    path = quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%+:@/?!$'()*,;-._~")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, parts.fragment))


async def handler(m, *, conn, text):
    if not re.match(r'^https?://', text or ''):
        raise PluginError('Prefix *URL* with http:// or https://')
    url = normalize_url(text)
    try:
        res = await http.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.google.com/',
        }, timeout=30, max_redirects=5)
        length = res.headers.get('content-length')
        try:
            too_big = length is not None and float(length) > 100 * 1024 ** 3
        except ValueError:
            too_big = False
        if too_big:
            raise PluginError(f'Content-Length: {length}')
        ctype = res.headers.get('content-type') or ''
        if not re.search('text|json', ctype):
            return await conn.sendFile(m.chat, url, 'file', text, m)
        txt = res.content.decode('utf-8', errors='replace')
        try:
            txt = node_format(json.loads(txt))
        except Exception:
            pass
        await m.reply(txt[:65536])
    except PluginError:
        raise
    except Exception as e:
        raise PluginError(str(getattr(e, 'message', e)))


help = ['fetch <url>', 'get <url>']
tags = ['tools']
command = re.compile(r'^(fetch|get)$', re.I)
ai = {'risk': 'low', 'summarize': False, 'description': 'Retrieving data or content from a URL.'}
