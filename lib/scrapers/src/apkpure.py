import asyncio
import gzip
import http.client as httplib
import os
import random
import re
import shutil
import ssl
import urllib.parse
import urllib.request
import zlib

from lib.compat import http
from lib.compat.html import load


BASE = 'https://apkpure.com'
CONFIG = {'proxy': '', 'spoofIP': True}
UA_POOL = [
    'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
]
H2_PROFILES = [
    {'ua': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
     'ch': '"Chromium";v="137", "Not/A)Brand";v="24"', 'mobile': '?1', 'platform': '"Android"'},
    {'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
     'ch': '"Chromium";v="137", "Google Chrome";v="137", "Not/A)Brand";v="24"', 'mobile': '?0', 'platform': '"Windows"'},
    {'ua': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
     'ch': '"Chromium";v="137", "Google Chrome";v="137", "Not/A)Brand";v="24"', 'mobile': '?0', 'platform': '"macOS"'},
]
H2_PROFILE = random.choice(H2_PROFILES)
CHROME_CIPHERS = ':'.join([
    'TLS_AES_128_GCM_SHA256', 'TLS_AES_256_GCM_SHA384', 'TLS_CHACHA20_POLY1305_SHA256',
    'ECDHE-ECDSA-AES128-GCM-SHA256', 'ECDHE-RSA-AES128-GCM-SHA256',
    'ECDHE-ECDSA-AES256-GCM-SHA384', 'ECDHE-RSA-AES256-GCM-SHA384',
    'ECDHE-ECDSA-CHACHA20-POLY1305', 'ECDHE-RSA-CHACHA20-POLY1305',
    'ECDHE-RSA-AES128-SHA', 'ECDHE-RSA-AES256-SHA',
    'AES128-GCM-SHA256', 'AES256-GCM-SHA384', 'AES128-SHA', 'AES256-SHA',
])
REDIRECT_CODES = (301, 302, 303, 307, 308)
ENC_SAFE = "-_.!~*'()"
_jar = {}


class CloudflareBlocked(Exception):
    cloudflare = True


class _TraceError(Exception):
    def __init__(self, message, trail):
        super().__init__(message)
        self.trail = trail


def _make_tls_ctx():
    try:
        ctx = ssl.create_default_context()
        ctx.set_ciphers(CHROME_CIPHERS)
        return ctx
    except ssl.SSLError:
        return ssl.create_default_context()


_TLS_CTX = _make_tls_ctx()


def _proxy_kw():
    return {'proxy': CONFIG['proxy'] or False}


def _to_int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        return None


def randomUA():
    return random.choice(UA_POOL)


def randomIP():
    while True:
        a, b, c, d = random.randint(1, 223), random.randint(0, 255), random.randint(0, 255), random.randint(1, 254)
        if a in (10, 127, 0):
            continue
        if a == 172 and 16 <= b <= 31:
            continue
        if a == 192 and b == 168:
            continue
        if a == 169 and b == 254:
            continue
        if a == 100 and 64 <= b <= 127:
            continue
        return f'{a}.{b}.{c}.{d}'


def buildHeaders(extra=None):
    ip = randomIP()
    headers = {
        'User-Agent': randomUA(),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': random.choice(['en-US,en;q=0.9', 'id-ID,id;q=0.9,en;q=0.8', 'en-GB,en;q=0.9']),
        'Referer': BASE + '/',
    }
    if CONFIG['spoofIP']:
        headers.update({
            'X-Forwarded-For': ip, 'X-Real-IP': ip, 'X-Originating-IP': ip, 'X-Client-IP': ip,
            'Client-IP': ip, 'True-Client-IP': ip, 'CF-Connecting-IP': ip, 'Forwarded': f'for={ip}',
        })
    for k, v in (extra or {}).items():
        if v is not None:
            headers[k] = v
    return headers


def is_challenge(status, headers, body):
    text = (body or '')[:4000]
    return headers.get('cf-mitigated') == 'challenge' or (
        status in (403, 503) and re.search(r'Just a moment|challenge-platform|cf_chl', text, re.I) is not None)


def _save_cookies(hostname, set_cookies):
    for sc in set_cookies:
        kv = sc.split(';')[0]
        i = kv.find('=')
        if i > 0:
            _jar[f'{hostname}|{kv[:i].strip()}'] = kv[i + 1:].strip()


def _cookie_header(hostname):
    return '; '.join(f'{k.split("|", 1)[1]}={v}' for k, v in _jar.items() if k.startswith(hostname + '|'))


def _decode(buf, enc):
    enc = (enc or '').lower()
    try:
        if 'gzip' in enc:
            return gzip.decompress(buf)
        if 'deflate' in enc:
            try:
                return zlib.decompress(buf)
            except zlib.error:
                return zlib.decompress(buf, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return buf
    return buf


def _h1_fetch(url, headers, head_only=False, timeout=25):
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    path = (parts.path or '/') + (('?' + parts.query) if parts.query else '')
    if parts.scheme == 'https':
        conn = httplib.HTTPSConnection(host, parts.port, timeout=timeout, context=_TLS_CTX)
    else:
        conn = httplib.HTTPConnection(host, parts.port, timeout=timeout)
    try:
        conn.request('GET', path, headers=headers)
        resp = conn.getresponse()
        hdrs = {}
        set_cookies = []
        for k, v in resp.getheaders():
            lk = k.lower()
            if lk == 'set-cookie':
                set_cookies.append(v)
            else:
                hdrs[lk] = v
        hdrs['set-cookie'] = set_cookies
        _save_cookies(host, set_cookies)
        if head_only:
            return {'status': resp.status, 'headers': hdrs, 'data': '', 'url': url}
        body = _decode(resp.read(), hdrs.get('content-encoding', ''))
        return {'status': resp.status, 'headers': hdrs, 'data': body.decode('utf-8', errors='replace'), 'url': url}
    finally:
        conn.close()


def _h2_like_headers(url, referer):
    u = urllib.parse.urlsplit(url)
    site = 'none'
    if referer:
        r = urllib.parse.urlsplit(referer)

        def root(h):
            return '.'.join((h or '').split('.')[-2:])
        if r.netloc == u.netloc:
            site = 'same-origin'
        elif root(r.hostname) == root(u.hostname):
            site = 'same-site'
        else:
            site = 'cross-site'
    headers = {
        'sec-ch-ua': H2_PROFILE['ch'],
        'sec-ch-ua-mobile': H2_PROFILE['mobile'],
        'sec-ch-ua-platform': H2_PROFILE['platform'],
        'upgrade-insecure-requests': '1',
        'user-agent': H2_PROFILE['ua'],
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'sec-fetch-site': site,
        'sec-fetch-mode': 'navigate',
        'sec-fetch-user': '?1',
        'sec-fetch-dest': 'document',
        'accept-encoding': 'gzip, deflate',
        'accept-language': 'en-US,en;q=0.9',
    }
    if referer:
        headers['referer'] = referer
    cookie = _cookie_header(u.hostname or '')
    if cookie:
        headers['cookie'] = cookie
    return headers


def _h2_like_once(url, referer=None, head_only=False):
    return _h1_fetch(url, _h2_like_headers(url, referer), head_only=head_only)


def _trace_redirects(start_url, referer, max_hops=8):
    trail = []
    url = start_url
    for _ in range(max_hops):
        r = _h2_like_once(url, referer, head_only=True)
        trail.append(f'{r["status"]} {urllib.parse.urlsplit(url).netloc}')
        location = r['headers'].get('location')
        if r['status'] in REDIRECT_CODES and location:
            url = urllib.parse.urljoin(url, location)
            continue
        return {'url': url, 'status': r['status'], 'headers': r['headers'], 'trail': trail}
    raise _TraceError('Too many redirects', trail)


def _follow_get(url, hops=5):
    res = _h2_like_once(url)
    while hops > 0 and res['status'] in REDIRECT_CODES and res['headers'].get('location'):
        hops -= 1
        url = urllib.parse.urljoin(url, res['headers']['location'])
        res = _h2_like_once(url)
    res['url'] = url
    return res


async def _quiet(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception:
        return None


async def get_html(url):
    if not CONFIG['proxy']:
        r = await _quiet(_follow_get, url)
        if r and r['status'] < 400 and not is_challenge(r['status'], r['headers'], r['data']):
            return r['data']
    res = await http.get(url, headers=buildHeaders(), timeout=30, max_redirects=5, raise_for_status=False, **_proxy_kw())
    if is_challenge(res.status, res.headers, res.text()):
        raise CloudflareBlocked(f'APKPure blocked the request with a Cloudflare challenge (HTTP {res.status})')
    if res.status >= 400:
        raise RuntimeError(f'HTTP {res.status} from {url}')
    return res.text()


def absolute(u):
    if not u:
        return ''
    if u.startswith('//'):
        return 'https:' + u
    if u.startswith('/'):
        return BASE + u
    return u


def clean(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def parse_app_path(href):
    m = re.match(r'^(?:https?://apkpure\.com)?/([^/]+)/([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)/?$', (href or '').split('?')[0])
    return {'slug': m.group(1), 'pkg': m.group(2)} if m else None


async def apkpure_search(query, limit=10):
    if not query:
        raise ValueError('Empty query')
    html = await get_html(f'{BASE}/search?q={urllib.parse.quote(str(query), safe=ENC_SAFE)}&t=')
    c = load(html)
    results, seen = [], set()
    for el in c('a[href]'):
        a = c(el)
        info = parse_app_path(a.attr('href'))
        if not info or info['pkg'] in seen:
            continue
        title = clean(a.find('.p1, .first-info .p1, .name, .title, h2, h3').first().text() or a.attr('title')
                      or a.find('img').attr('alt') or a.text())
        if not title:
            continue
        seen.add(info['pkg'])
        img = a.find('img').first()
        results.append({
            'title': title,
            'slug': info['slug'],
            'pkg': info['pkg'],
            'url': f"{BASE}/{info['slug']}/{info['pkg']}",
            'icon': absolute(img.attr('data-src') or img.attr('src') or ''),
            'developer': clean(a.find('.p2, .developer, .dev').first().text()),
            'rating': clean(a.find('.score, .rating, .star').first().text()),
        })
    return results[:limit]


async def aptoide_search(query, limit=10):
    res = await http.get('https://ws75.aptoide.com/api/7/apps/search', params={'query': query, 'limit': limit},
                         headers={'User-Agent': randomUA(), 'Accept': 'application/json'}, timeout=20, **_proxy_kw())
    data = res.json()
    lst = ((data.get('datalist') or {}).get('list')) or []
    out = []
    for a in [x for x in lst if x and x.get('package')][:limit]:
        avg = (((a.get('stats') or {}).get('rating')) or {}).get('avg')
        out.append({
            'title': a.get('name'),
            'slug': 'app',
            'pkg': a['package'],
            'url': f"{BASE}/app/{a['package']}",
            'icon': a.get('icon') or '',
            'developer': (a.get('developer') or {}).get('name') or '',
            'rating': f'{float(avg):.1f}' if avg else '',
        })
    return out


async def apkSearch(query, limit=10):
    if not query:
        raise ValueError('Empty query')
    try:
        r = await apkpure_search(query, limit)
        if r:
            return r
    except Exception as e:
        if not getattr(e, 'cloudflare', False) and not re.search(r'HTTP 4|HTTP 5', str(e)):
            raise
    return await aptoide_search(query, limit)


def resolve_target(inp):
    if not inp:
        raise ValueError('Empty input')
    inp = str(inp).strip()
    m = re.search(r'apkpure\.(?:com|net)/([^/?#]+)/([a-zA-Z0-9_.]+)', inp)
    if m:
        return {'slug': m.group(1), 'pkg': m.group(2)}
    m = re.match(r'^([^/\s]+)/([a-zA-Z0-9_.]+)$', inp)
    if m:
        return {'slug': m.group(1), 'pkg': m.group(2)}
    if re.match(r'^[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)+$', inp):
        return {'slug': 'app', 'pkg': inp}
    raise ValueError('Unrecognized format. Use an APKPure URL or a package name (e.g. com.whatsapp)')


async def apkDetail(inp):
    t = resolve_target(inp)
    slug, pkg = t['slug'], t['pkg']
    url = f'{BASE}/{slug}/{pkg}'
    c = load(await get_html(url))

    def meta(p):
        return c(f'meta[property="{p}"]').attr('content') or ''
    title = clean(c('h1').first().text() or meta('og:title'))
    info = {}
    for el in c('.additional li, .details-sdk, .info-list li, .ny-down li, dl, table tr'):
        text = clean(c(el).text())
        m = re.match(r'^(Latest Version|Version|Size|Updated on|Update|Requires Android|Android OS|Category|Installs|Content Rating|Developer|Available on)\s*[:\-]?\s*(.+)$', text, re.I)
        if m and not info.get(m.group(1).lower()):
            info[m.group(1).lower()] = m.group(2)
    version = info.get('latest version') or info.get('version') or clean(c('.details-sdk span, .version, [class*="version"]').first().text()) or ''
    return {
        'title': title,
        'pkg': pkg,
        'slug': slug,
        'url': url,
        'icon': meta('og:image'),
        'description': clean(meta('og:description') or c('meta[name="description"]').attr('content')),
        'version': version,
        'size': info.get('size', ''),
        'updated': info.get('updated on') or info.get('update') or '',
        'android': info.get('requires android') or info.get('android os') or '',
        'developer': info.get('developer') or clean(c('.developer a, [class*="developer"]').first().text()),
        'info': info,
    }


async def _probe(link, referer):
    try:
        res = await http.get(link, headers=buildHeaders({'Referer': referer, 'Range': 'bytes=0-0', 'Accept': '*/*'}), timeout=30,
                             max_redirects=10, validate_status=lambda s: s < 400, max_bytes=1, **_proxy_kw())
    except Exception:
        return None
    ct = res.headers.get('content-type', '')
    if re.search(r'text/html', ct, re.I):
        return None
    cd = res.headers.get('content-disposition', '')
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
    rng = res.headers.get('content-range', '')
    rng = rng.split('/')[1] if '/' in rng else ''
    size = _to_int(rng or res.headers.get('content-length') or 0)
    return {'url': res.url, 'name': urllib.parse.unquote(m.group(1)) if m else '', 'size': size}


def _trace_info(link, page_url, trails):
    try:
        r = _trace_redirects(link, page_url)
    except Exception as e:
        trails.append('error: ' + str(e))
        return None
    trails.append(' > '.join(r['trail']))
    if r['status'] != 200 or re.search(r'text/html', str(r['headers'].get('content-type') or ''), re.I):
        return None
    fin = urllib.parse.urlsplit(r['url'])
    query = urllib.parse.parse_qs(fin.query)
    cd = str(r['headers'].get('content-disposition') or '')
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
    name = (urllib.parse.unquote(m.group(1)) if m else '') or (query.get('filename') or [''])[0]
    size = _to_int(r['headers'].get('content-length') or (query.get('full_size') or [0])[0])
    return {'url': r['url'], 'name': name, 'size': size}


async def _scrape_download_href(page_url):
    try:
        c = load(await get_html(page_url))
    except Exception:
        return ''
    return absolute(
        c('#download_link').attr('href')
        or c('a.download-start-btn').attr('href')
        or c('a[href*="d.apkpure.com"]').first().attr('href')
        or c('a[href*="/b/APK/"], a[href*="/b/XAPK/"]').first().attr('href')
        or ''
    )


async def apkDownload(inp, type_='auto', version='latest'):
    t = resolve_target(inp)
    slug, pkg = t['slug'], t['pkg']
    page_url = f'{BASE}/{slug}/{pkg}/download?utm_content=1008'
    types = ['XAPK', 'APK'] if re.match(r'^auto$', str(type_), re.I) else [str(type_).upper()]
    trails = []
    link, chosen, info = '', types[0], None

    for ty in types:
        l = f'https://d.apkpure.com/b/{ty}/{pkg}?version={version}'
        info = None
        if not CONFIG['proxy']:
            info = await asyncio.to_thread(_trace_info, l, page_url, trails)
        info = info or await _probe(l, page_url)
        if info:
            link, chosen = l, ty
            break

    if not info:
        href = await _scrape_download_href(page_url)
        if href:
            link = href
            info = None
            if not CONFIG['proxy']:
                info = await asyncio.to_thread(_trace_info, href, page_url, trails)
            info = info or await _probe(href, page_url) or {'url': href, 'name': '', 'size': 0}
            m = re.search(r'/b/(X?APK)/', href, re.I)
            chosen = m.group(1).upper() if m else chosen

    if not info:
        raise RuntimeError('Download link not found or blocked [' + (' | '.join(trails) or 'no response')
                           + ']. Check the package name or try again later')

    m = re.search(r'\.(xapk|apk|apks)(\?|$)', info.get('name') or info['url'], re.I)
    ext = m.group(1).lower() if m else chosen.lower()
    return {
        'pkg': pkg,
        'slug': slug,
        'type': 'APK' if ext == 'apk' else ext.upper(),
        'pageUrl': page_url,
        'link': link,
        'url': info['url'],
        'fileName': info.get('name') or f'{pkg}.{ext}',
        'size': info.get('size'),
    }


def formatSize(b):
    if not b:
        return '-'
    units = ['B', 'KB', 'MB', 'GB']
    i = 0
    b = float(b)
    while b >= 1024 and i < len(units) - 1:
        b /= 1024
        i += 1
    return f'{b:.2f} {units[i]}'


def _opener():
    proxy = CONFIG['proxy']
    return urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}))


def _fetch_sync(url, dest, headers, max_bytes):
    req = urllib.request.Request(url, headers=headers)
    with _opener().open(req, timeout=60) as resp:
        if re.search(r'text/html', resp.headers.get('content-type') or '', re.I):
            raise RuntimeError('Download blocked (received an HTML page instead of a file)')
        length = _to_int(resp.headers.get('content-length') or 0)
        if max_bytes and length > max_bytes:
            raise RuntimeError('File too large (' + formatSize(length) + ')')
        with open(dest, 'wb') as fh:
            shutil.copyfileobj(resp, fh, 262144)
    return os.path.getsize(dest)


async def apkFetchFile(dl, dest, max_bytes=0):
    urls = list(dict.fromkeys(u for u in (dl.get('url'), dl.get('link')) if u))
    last = None
    for url in urls:
        try:
            return await asyncio.to_thread(_fetch_sync, url, dest, buildHeaders({'Referer': dl.get('pageUrl'), 'Accept': '*/*'}), max_bytes)
        except Exception as e:
            last = e
            _unlink(dest)
    if last is None:
        raise RuntimeError('Download failed')
    raise last
