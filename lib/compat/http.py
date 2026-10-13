import asyncio
import base64
import gzip
import http.client
import io
import json as _json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.cookiejar import CookieJar


_DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
_SSL_CTX = ssl.create_default_context()

_CAMEL_ALIASES = {
    'responseType': 'response_type',
    'maxRedirects': 'max_redirects',
    'validateStatus': 'validate_status',
    'maxContentLength': 'max_content_length',
    'baseURL': 'base_url',
}


class HTTPError(Exception):
    def __init__(self, message, response=None, status=None, code=None):
        super().__init__(message)
        self.message = message
        self.response = response
        self.status = status if status is not None else (response.status if response is not None else None)
        self.code = code


class Response:
    def __init__(self, status, headers, content, url, reason='', response_type='arraybuffer'):
        self.status = status
        self.statusText = reason
        self.headers = {k.lower(): v for k, v in headers}
        self.content = content
        self.url = url
        self._json = None
        self.responseType = response_type
        self.data = _shape_data(self, content, response_type)

    @property
    def ok(self):
        return 200 <= self.status < 300

    def text(self, encoding=None):
        enc = encoding
        if not enc:
            ctype = self.headers.get('content-type', '')
            if 'charset=' in ctype:
                enc = ctype.split('charset=')[1].split(';')[0].strip()
        return self.content.decode(enc or 'utf-8', errors='replace')

    def json(self):
        if self._json is None:
            self._json = _json.loads(self.text())
        return self._json


def _shape_data(resp, content, response_type):
    rt = (response_type or 'arraybuffer').lower()
    if rt == 'stream':
        return io.BytesIO(content)
    if rt == 'text':
        return resp.text()
    if rt == 'json':
        try:
            return resp.json()
        except ValueError:
            return resp.text()
    return content


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _LimitedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, limit):
        self.max_redirections = limit


def _decode_body(raw, encoding):
    enc = (encoding or '').lower()
    try:
        if enc == 'gzip':
            return gzip.decompress(raw)
        if enc == 'deflate':
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        return raw
    return raw


def _build_url(url, params):
    if not params:
        return url
    parts = urllib.parse.urlsplit(url)
    existing = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    items = params.items() if isinstance(params, dict) else params
    for k, v in items:
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            for x in v:
                existing.append((f'{k}[]', str(x)))
        else:
            existing.append((k, str(v)))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(existing), parts.fragment))


def _combine_base_url(base_url, url):
    if not base_url or urllib.parse.urlsplit(url).scheme:
        return url
    return base_url.rstrip('/') + '/' + url.lstrip('/')


def _encode_multipart(fields):
    boundary = '----hirobot' + str(int(time.time() * 1000))
    buf = io.BytesIO()
    for name, value in fields.items():
        buf.write(f'--{boundary}\r\n'.encode())
        if isinstance(value, dict) and 'content' in value:
            fname = value.get('filename', 'file')
            ctype = value.get('contentType', 'application/octet-stream')
            buf.write(f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'.encode())
            buf.write(f'Content-Type: {ctype}\r\n\r\n'.encode())
            content = value['content']
            buf.write(content if isinstance(content, bytes) else str(content).encode())
        else:
            buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            buf.write(value if isinstance(value, bytes) else str(value).encode())
        buf.write(b'\r\n')
    buf.write(f'--{boundary}--\r\n'.encode())
    return buf.getvalue(), f'multipart/form-data; boundary={boundary}'


def _proxy_handlers(proxy):
    if proxy is False:
        return [urllib.request.ProxyHandler({})]
    if not proxy:
        return []
    if isinstance(proxy, dict):
        host = proxy.get('host')
        port = proxy.get('port')
        protocol = proxy.get('protocol') or 'http'
        auth = proxy.get('auth') or {}
        userinfo = ''
        if auth.get('username') is not None:
            userinfo = urllib.parse.quote(str(auth.get('username')), safe='') + ':' + urllib.parse.quote(str(auth.get('password', '')), safe='') + '@'
        url = f'{protocol}://{userinfo}{host}' + (f':{port}' if port else '')
        return [urllib.request.ProxyHandler({'http': url, 'https': url})]
    return [urllib.request.ProxyHandler({'http': proxy, 'https': proxy})]


def _basic_auth_header(auth):
    if isinstance(auth, dict):
        user, pwd = auth.get('username', ''), auth.get('password', '')
    else:
        user, pwd = auth[0], auth[1]
    token = base64.b64encode(f'{user}:{pwd}'.encode('utf-8')).decode()
    return f'Basic {token}'


def request_sync(method='GET', url='', headers=None, params=None, data=None, json=None, form=None,
                 multipart=None, timeout=30, max_redirects=5, validate_status=None,
                 proxy=None, cookies=None, raise_for_status=True, max_bytes=None,
                 auth=None, response_type='arraybuffer', max_content_length=None, base_url=None):
    url = _combine_base_url(base_url, url)
    url = _build_url(url, params)
    hdrs = {'User-Agent': _DEFAULT_UA, 'Accept-Encoding': 'gzip, deflate', 'Accept': '*/*'}
    for k, v in (headers or {}).items():
        if v is not None:
            hdrs[k] = v
    if auth is not None and not any(k.lower() == 'authorization' for k in hdrs):
        hdrs['Authorization'] = _basic_auth_header(auth)
    body = None
    if json is not None:
        body = _json.dumps(json).encode()
        if not any(k.lower() == 'content-type' for k in hdrs):
            hdrs['Content-Type'] = 'application/json'
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        if not any(k.lower() == 'content-type' for k in hdrs):
            hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
    elif multipart is not None:
        body, ctype = _encode_multipart(multipart)
        hdrs['Content-Type'] = ctype
    elif data is not None:
        if isinstance(data, (dict, list)):
            body = _json.dumps(data).encode()
            if not any(k.lower() == 'content-type' for k in hdrs):
                hdrs['Content-Type'] = 'application/json'
        elif isinstance(data, str):
            body = data.encode()
        else:
            body = bytes(data)

    jar = cookies if isinstance(cookies, CookieJar) else CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPSHandler(context=_SSL_CTX)]
    handlers.extend(_proxy_handlers(proxy))
    if max_redirects is not None and max_redirects <= 0:
        handlers.append(_NoRedirect())
    elif max_redirects is not None:
        handlers.append(_LimitedRedirect(max_redirects))
    opener = urllib.request.build_opener(*handlers)

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
    try:
        resp = opener.open(req, timeout=timeout)
        status, reason = resp.status, resp.reason
        if max_content_length is not None:
            raw = resp.read(max_content_length + 1)
            if len(raw) > max_content_length:
                raise HTTPError(f'maxContentLength size of {max_content_length} bytes exceeded')
        else:
            raw = resp.read() if max_bytes is None else resp.read(max_bytes)
        resp_headers = resp.getheaders()
        final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        status, reason = e.code, e.reason
        raw = e.read()
        resp_headers = list(e.headers.items())
        final_url = e.geturl()
    except HTTPError:
        raise
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        raise HTTPError(str(getattr(e, 'reason', e)), code=getattr(e, 'errno', None))

    enc = dict((k.lower(), v) for k, v in resp_headers).get('content-encoding')
    content = _decode_body(raw, enc)
    r = Response(status, resp_headers, content, final_url, reason, response_type)
    ok = validate_status(status) if validate_status else (200 <= status < 300)
    if raise_for_status and not ok:
        raise HTTPError(f'Request failed with status code {status}', response=r)
    return r


def _normalize_kw(kw):
    out = {}
    for k, v in kw.items():
        out[_CAMEL_ALIASES.get(k, k)] = v
    return out


async def request(method='GET', url='', **kw):
    kw = _normalize_kw(kw)
    return await asyncio.to_thread(lambda: request_sync(method, url, **kw))


async def get(url, **kw):
    return await request('GET', url, **kw)


async def post(url, **kw):
    return await request('POST', url, **kw)


async def head(url, **kw):
    return await request('HEAD', url, **kw)


async def get_json(url, **kw):
    r = await request('GET', url, **kw)
    return r.json()


async def get_text(url, **kw):
    r = await request('GET', url, **kw)
    return r.text()


async def get_bytes(url, **kw):
    r = await request('GET', url, **kw)
    return r.content


class HttpClient:
    def __init__(self, defaults=None):
        self.defaults = _normalize_kw(defaults or {})

    def _merge(self, kw):
        merged = dict(self.defaults)
        kw = _normalize_kw(kw)
        for k, v in kw.items():
            if k == 'headers' and isinstance(merged.get('headers'), dict) and isinstance(v, dict):
                merged['headers'] = {**merged['headers'], **v}
            else:
                merged[k] = v
        return merged

    async def request(self, method='GET', url='', **kw):
        return await request(method, url, **self._merge(kw))

    async def get(self, url, **kw):
        return await self.request('GET', url, **kw)

    async def post(self, url, **kw):
        return await self.request('POST', url, **kw)

    async def head(self, url, **kw):
        return await self.request('HEAD', url, **kw)

    async def get_json(self, url, **kw):
        r = await self.request('GET', url, **kw)
        return r.json()


def create(defaults=None, **kw):
    merged = dict(defaults or {})
    merged.update(kw)
    return HttpClient(merged)


def parse_set_cookie(headers_value):
    out = {}
    for part in str(headers_value or '').split(','):
        kv = part.split(';')[0].strip()
        if '=' in kv:
            k, v = kv.split('=', 1)
            out[k.strip()] = v.strip()
    return out
