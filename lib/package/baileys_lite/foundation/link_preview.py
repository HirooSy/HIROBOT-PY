import asyncio
import codecs
import re
import urllib.error
import urllib.parse
import urllib.request

from baileys_lite.foundation.node_runtime import JSError, JSObject


JS_WS_CHARS = '\t\n\x0b\x0c\r \xa0                　﻿'
JS_WS_CLASS = '\\t\\n\\x0b\\x0c\\r \\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff'

REGEX_VALID_URL = re.compile(r'^(?:https?://)[^' + JS_WS_CLASS + r']+\Z', re.IGNORECASE | re.ASCII)
DEFAULT_UA = 'Mozilla/5.0 (compatible; baileys-lite link preview)'
MAX_BYTES = 1024 * 1024

NAMED = JSObject({
    'amp': '&', 'lt': '<', 'gt': '>', 'quot': '"', 'apos': "'", 'nbsp': ' ', 'copy': '©', 'reg': '®', 'trade': '™', 'hellip': '…', 'mdash': '—', 'ndash': '–',
    'lsquo': '‘', 'rsquo': '’', 'ldquo': '“', 'rdquo': '”', 'laquo': '«', 'raquo': '»', 'bull': '•', 'middot': '·', 'euro': '€', 'pound': '£', 'yen': '¥', 'cent': '¢',
    'sect': '§', 'deg': '°', 'plusmn': '±', 'times': '×', 'divide': '÷', 'para': '¶', 'iexcl': '¡', 'iquest': '¿', 'shy': '­', 'larr': '←', 'rarr': '→', 'uarr': '↑',
    'darr': '↓', 'hearts': '♥', 'check': '✓', 'star': '☆', 'infin': '∞', 'ne': '≠', 'le': '≤', 'ge': '≥', 'frac12': '½', 'frac14': '¼', 'frac34': '¾', 'sup2': '²', 'sup3': '³',
    'micro': 'µ', 'agrave': 'à', 'aacute': 'á', 'acirc': 'â', 'atilde': 'ã', 'auml': 'ä', 'aring': 'å', 'aelig': 'æ', 'ccedil': 'ç', 'egrave': 'è', 'eacute': 'é', 'ecirc': 'ê',
    'euml': 'ë', 'igrave': 'ì', 'iacute': 'í', 'icirc': 'î', 'iuml': 'ï', 'ntilde': 'ñ', 'ograve': 'ò', 'oacute': 'ó', 'ocirc': 'ô', 'otilde': 'õ', 'ouml': 'ö', 'oslash': 'ø',
    'ugrave': 'ù', 'uacute': 'ú', 'ucirc': 'û', 'uuml': 'ü', 'yacute': 'ý', 'yuml': 'ÿ', 'szlig': 'ß', 'Agrave': 'À', 'Aacute': 'Á', 'Acirc': 'Â', 'Atilde': 'Ã', 'Auml': 'Ä',
    'Aring': 'Å', 'AElig': 'Æ', 'Ccedil': 'Ç', 'Egrave': 'È', 'Eacute': 'É', 'Ecirc': 'Ê', 'Euml': 'Ë', 'Igrave': 'Ì', 'Iacute': 'Í', 'Icirc': 'Î', 'Iuml': 'Ï', 'Ntilde': 'Ñ',
    'Ograve': 'Ò', 'Oacute': 'Ó', 'Ocirc': 'Ô', 'Otilde': 'Õ', 'Ouml': 'Ö', 'Oslash': 'Ø', 'Ugrave': 'Ù', 'Uacute': 'Ú', 'Ucirc': 'Û', 'Uuml': 'Ü', 'Yacute': 'Ý',
})

LEGACY = re.compile(r'amp|lt|gt|quot|nbsp|copy|reg|deg|plusmn|times|divide|para|sect|shy|micro|middot|laquo|raquo|iexcl|iquest|cent|pound|yen')

ENTITY_RE = re.compile(r'&(?:#([0-9]+)|#[xX]([0-9a-fA-F]+)|([A-Za-z][A-Za-z0-9]*))(;?)')

TEXT_ELEMENTS = {'script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript'}
WS = {' ', '\t', '\n', '\f', '\r'}


def _jsTrim(text):
    return text.strip(JS_WS_CHARS)


def _isAlpha(c):
    return c is not None and (('a' <= c <= 'z') or ('A' <= c <= 'Z'))


def _ch(html, index):
    if 0 <= index < len(html):
        return html[index]
    return ''


def decodeEntities(s, inAttr=False):
    if '&' not in s:
        return s
    out = []
    last = 0
    for match in ENTITY_RE.finditer(s):
        m = match.group(0)
        offset = match.start()
        dec, hexValue, name, semi = match.group(1), match.group(2), match.group(3), match.group(4)
        replacement = m
        if name:
            replacement = m
            if semi and name in NAMED:
                replacement = NAMED[name]
            else:
                for length in range(len(name), 1, -1):
                    prefix = name[:length]
                    if not LEGACY.fullmatch(prefix):
                        continue
                    rest = name[length:] + semi
                    if len(rest) > 0:
                        nxt = rest[0]
                    elif offset + len(m) < len(s):
                        nxt = s[offset + len(m)]
                    else:
                        nxt = None
                    if inAttr and nxt is not None and re.search(r'[=A-Za-z0-9]', nxt):
                        replacement = m
                    else:
                        replacement = NAMED[prefix] + rest
                    break
        else:
            cp = int(dec) if dec is not None else int(hexValue, 16)
            if not cp or cp > 0x10ffff or (0xd800 <= cp <= 0xdfff):
                replacement = '�'
            else:
                replacement = chr(cp)
        out.append(s[last:offset])
        out.append(replacement)
        last = offset + len(m)
    out.append(s[last:])
    return ''.join(out)


def scanHtml(html):
    out = JSObject({'metas': [], 'links': [], 'imgs': [], 'title': None, 'base': None})
    n = len(html)
    i = 0
    while i < n:
        lt = html.find('<', i)
        if lt == -1:
            break
        i = lt + 1
        c = _ch(html, i)
        if c == '!':
            if html.startswith('--', i + 1):
                if _ch(html, i + 3) == '>':
                    i += 4
                elif html.startswith('->', i + 3):
                    i += 5
                else:
                    end = html.find('-->', i + 3)
                    alt = html.find('--!>', i + 3)
                    if end == -1:
                        e = alt
                    elif alt == -1:
                        e = end
                    else:
                        e = min(end, alt)
                    if e == -1:
                        break
                    i = e + (4 if html.startswith('--!>', e) else 3)
            else:
                gt = html.find('>', i)
                if gt == -1:
                    break
                i = gt + 1
            continue
        if c == '?':
            gt = html.find('>', i)
            if gt == -1:
                break
            i = gt + 1
            continue
        if c == '/':
            d = _ch(html, i + 1)
            if d == '>':
                i += 2
                continue
            gt = html.find('>', i)
            if gt == -1:
                break
            i = gt + 1
            continue
        if not _isAlpha(c):
            continue

        j = i
        while j < n and html[j] not in WS and html[j] != '/' and html[j] != '>':
            j += 1
        name = html[i:j].lower()
        if name == 'image':
            name = 'img'
        attrs = JSObject()
        closed = False

        while True:
            while j < n and (html[j] in WS or html[j] == '/'):
                j += 1
            if j >= n:
                break
            if html[j] == '>':
                j += 1
                closed = True
                break

            k = j + 1
            while k < n and html[k] not in WS and html[k] != '/' and html[k] != '>' and html[k] != '=':
                k += 1
            aname = html[j:k].lower().replace('\x00', '�')
            j = k
            while j < n and html[j] in WS:
                j += 1
            value = ''
            if _ch(html, j) == '=':
                j += 1
                while j < n and html[j] in WS:
                    j += 1
                q = _ch(html, j)
                if q == '"' or q == "'":
                    e = html.find(q, j + 1)
                    if e == -1:
                        j = n
                        break
                    value = html[j + 1:e]
                    j = e + 1
                else:
                    e = j
                    while e < n and html[e] not in WS and html[e] != '>':
                        e += 1
                    value = html[j:e]
                    j = e
                value = decodeEntities(value, True)
            if aname not in attrs:
                attrs[aname] = value
        if not closed:
            break
        i = j
        if name == 'meta':
            out.get('metas').append(attrs)
        elif name == 'link':
            out.get('links').append(attrs)
        elif name == 'img':
            out.get('imgs').append(attrs)
        elif name == 'base':
            if out.get('base') is None and attrs.get('href'):
                out['base'] = attrs.get('href')
        if name in TEXT_ELEMENTS:
            pattern = re.compile('</' + name + r'(?=[' + JS_WS_CLASS + r'/>])', re.IGNORECASE | re.ASCII)
            m = pattern.search(html, i)
            end = m.start() if m else n
            if name == 'title' and out.get('title') is None:
                out['title'] = decodeEntities(html[i:end])
            i = end
    return out


def normalizeUrl(url):
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if not scheme:
        raise ValueError('Invalid URL')
    userinfo, sep, hostport = parts.netloc.rpartition('@')
    if hostport.startswith('['):
        end = hostport.find(']')
        host = hostport[:end + 1]
        rest = hostport[end + 1:]
    else:
        host, colon, portText = hostport.partition(':')
        rest = colon + portText
    host = host.lower()
    if rest.startswith(':') and ((scheme == 'http' and rest == ':80') or (scheme == 'https' and rest == ':443')):
        rest = ''
    netloc = userinfo + sep + host + rest
    path = parts.path
    if not path and scheme in ('http', 'https'):
        path = '/'
    safe = "/%:@!$&'()*+,;=-._~[]|^"
    path = urllib.parse.quote(path, safe=safe)
    query = urllib.parse.quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
    fragment = urllib.parse.quote(parts.fragment, safe="=&%:@!$'()*+,;/?-._~")
    return urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))


def resolveUrl(href, base):
    try:
        joined = urllib.parse.urljoin(base, _jsTrim(href)) if base is not None else _jsTrim(href)
        return normalizeUrl(joined)
    except Exception:
        return None


CHARSET_VALUE = r'([A-Za-z0-9_.:-]+)'
CHARSET_IN_CT = re.compile(r'charset[' + JS_WS_CLASS + r']*=[' + JS_WS_CLASS + r']*["\']?' + CHARSET_VALUE, re.IGNORECASE | re.ASCII)
META_CHARSET = re.compile(r'<meta[^>]+charset[' + JS_WS_CLASS + r']*=[' + JS_WS_CLASS + r']*["\']?' + CHARSET_VALUE, re.IGNORECASE | re.ASCII)
META_CONTENT_CHARSET = re.compile(r'<meta[^>]+content[' + JS_WS_CLASS + r']*=[' + JS_WS_CLASS + r']*["\'][^"\']*charset=' + CHARSET_VALUE, re.IGNORECASE | re.ASCII)
HEAD_END = re.compile(r'</head[' + JS_WS_CLASS + r']*>', re.IGNORECASE | re.ASCII)
FAVICON_REL = re.compile(r'(^|[' + JS_WS_CLASS + r'])(shortcut[' + JS_WS_CLASS + r']+)?icon([' + JS_WS_CLASS + r']|\Z)|apple-touch-icon', re.IGNORECASE | re.ASCII)
DATA_URI = re.compile(r'^data:', re.IGNORECASE | re.ASCII)


def charsetFrom(contentType, head):
    h = CHARSET_IN_CT.search(contentType or '')
    if h:
        return h.group(1)
    m = META_CHARSET.search(head) or META_CONTENT_CHARSET.search(head)
    return m.group(1) if m else 'utf-8'


def _textDecoderCodec(charset):
    lowered = charset.lower()
    if lowered in ('utf-8', 'utf8', 'unicode-1-1-utf-8'):
        return 'utf-8'
    if lowered in ('iso-8859-1', 'iso8859-1', 'latin1', 'l1', 'ascii', 'us-ascii', 'windows-1252', 'x-cp1252', 'cp1252'):
        return 'cp1252'
    try:
        return codecs.lookup(lowered).name
    except LookupError:
        raise JSError('The "' + charset + '" encoding is not supported', name='RangeError')


def decodeBody(data, contentType):
    head = data[0:2048].decode('latin-1')
    charset = charsetFrom(contentType, head)
    try:
        codec = _textDecoderCodec(charset)
        if data.startswith(b'\xef\xbb\xbf'):
            return data[3:].decode('utf-8', 'replace')
        if data.startswith(b'\xff\xfe'):
            return data[2:].decode('utf-16-le', 'replace')
        if data.startswith(b'\xfe\xff'):
            return data[2:].decode('utf-16-be', 'replace')
        return data.decode(codec, 'replace')
    except JSError:
        return data.decode('utf-8', 'replace')


def _status(res):
    status = getattr(res, 'status', None)
    if status is None:
        status = res.code
    return status


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetchSync(url, headers, redirect, timeout):
    handlers = []
    if redirect != 'follow':
        handlers.append(_NoRedirectHandler())
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers=headers)
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as err:
        return err


def _readBodySync(res, maxBytes):
    chunks = []
    total = 0
    tail = ''
    while total < maxBytes:
        value = res.read(8192)
        if not value:
            break
        chunks.append(value)
        total += len(value)
        tail = (tail + value.decode('latin-1'))[-16:]
        if HEAD_END.search(tail):
            break
    try:
        res.close()
    except Exception:
        pass
    return b''.join(chunks)[:maxBytes]


def _metaKey(attrs):
    value = attrs.get('property')
    if value is None:
        value = attrs.get('name')
    if value is None:
        value = ''
    return value.lower()


async def getLinkPreview(text, options=None):
    if not isinstance(text, str):
        raise JSError('link-preview did not receive a valid a url or text')
    detected = None
    for token in text.replace('\n', ' ').split(' '):
        if REGEX_VALID_URL.match(token):
            detected = token
            break
    if detected is None:
        raise JSError('link-preview did not receive a valid a url or text')
    options = JSObject(options or {})
    timeout = options.get('timeout') if options.get('timeout') is not None else 3000
    followRedirects = options.get('followRedirects') if options.get('followRedirects') is not None else 'follow'
    handleRedirects = options.get('handleRedirects')
    maxBytes = options.get('maxBytes') if options.get('maxBytes') is not None else MAX_BYTES
    headers = {
        'user-agent': DEFAULT_UA,
        'accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
        'accept-language': 'en-US,en;q=0.9',
    }
    headers.update(options.get('headers') or {})

    url = detected
    hops = 0
    while True:
        res = await asyncio.to_thread(_fetchSync, url, headers, 'manual' if followRedirects == 'manual' else 'follow', timeout / 1000.0)
        status = _status(res)
        location = res.headers.get('location')
        if followRedirects == 'error' and 300 <= status < 400:
            raise JSError('fetch failed')
        if followRedirects == 'manual' and 300 <= status < 400 and location:
            forwarded = urllib.parse.urljoin(url, location)
            res.close()
            if hops >= 10 or not handleRedirects or not handleRedirects(url, forwarded):
                raise JSError('link-preview could not handle redirect')
            url = forwarded
            hops += 1
            continue
        break

    finalUrl = res.geturl() or url
    contentType = (res.headers.get('content-type') or '').lower()
    type = _jsTrim(contentType.split(';')[0])
    try:
        parsed = urllib.parse.urlsplit(finalUrl)
        origin = (parsed.scheme.lower() + '://' + parsed.netloc.lower()) if parsed.scheme in ('http', 'https') and parsed.netloc else 'null'
    except Exception:
        origin = ''
    defaultFavicons = [origin + '/favicon.ico'] if origin else []

    if type.startswith('image/'):
        res.close()
        return JSObject({'url': finalUrl, 'mediaType': 'image', 'contentType': type, 'favicons': defaultFavicons})
    if type.startswith('audio/') or type.startswith('video/') or (
        type.startswith('application/') and 'html' not in type and 'xml' not in type
    ):
        res.close()
        return JSObject({'url': finalUrl, 'mediaType': type.split('/')[0], 'contentType': type, 'favicons': defaultFavicons})
    if type and 'html' not in type and 'xml' not in type and not type.startswith('text/'):
        raise JSError('link-preview: unsupported content type ' + type)

    html = decodeBody(await asyncio.to_thread(_readBodySync, res, maxBytes), contentType)
    doc = scanHtml(html)
    base = (resolveUrl(doc.get('base'), finalUrl) or finalUrl) if doc.get('base') else finalUrl

    def meta(*keys):
        for k in keys:
            for a in doc.get('metas'):
                if _metaKey(a) == k and a.get('content') is not None and _jsTrim(a.get('content')) != '':
                    return _jsTrim(a.get('content'))
        return None

    def metaAll(*keys):
        result = []
        for a in doc.get('metas'):
            if _metaKey(a) in keys and a.get('content') is not None and _jsTrim(a.get('content')):
                result.append(_jsTrim(a.get('content')))
        return result

    title = meta('og:title', 'twitter:title')
    if title is None and doc.get('title') is not None:
        title = _jsTrim(doc.get('title'))
    description = meta('og:description', 'twitter:description', 'description')
    siteName = meta('og:site_name')
    mediaType = meta('og:type')
    if mediaType is None:
        mediaType = 'website'

    images = []

    def pushUrl(lst, href):
        if not href or DATA_URI.match(_jsTrim(href)):
            return
        u = resolveUrl(href, base)
        if u and u not in lst:
            lst.append(u)

    for v in metaAll('og:image', 'og:image:url', 'og:image:secure_url', 'twitter:image', 'twitter:image:src'):
        pushUrl(images, v)
    if not images:
        for img in doc.get('imgs')[:20]:
            pushUrl(images, img.get('src'))
    videos = []
    for v in metaAll('og:video', 'og:video:url', 'og:video:secure_url'):
        pushUrl(videos, v)
    favicons = []
    for link in doc.get('links'):
        rel = link.get('rel') if link.get('rel') is not None else ''
        if FAVICON_REL.search(rel) and link.get('href'):
            pushUrl(favicons, link.get('href'))
    if not favicons:
        favicons.extend(defaultFavicons)

    return JSObject({
        'url': finalUrl,
        'title': title,
        'siteName': siteName,
        'description': description,
        'mediaType': mediaType,
        'contentType': type or 'text/html',
        'images': images,
        'videos': videos,
        'favicons': favicons,
    })


default = getLinkPreview
