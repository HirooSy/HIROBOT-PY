import asyncio
import re
from http.cookiejar import CookieJar
from urllib.parse import quote, urlencode

from lib.compat import http


BASE = 'https://www.bing.com'
UA = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
POLL_INTERVAL = 3
POLL_LIMIT = 40
DEFAULT_RATIO = '1:1'
RATIOS = {'1:1': 1, '3:2': 2, '2:3': 3}
ASPECT_RATIOS = list(RATIOS.keys())


def _enc(text):
    return quote(text, safe="-_.!~*'()")


class _Session:
    def __init__(self):
        self.jar = CookieJar()

    def headers(self, extra=None):
        base = {
            'User-Agent': UA,
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-Ch-Ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'Sec-Ch-Ua-Mobile': '?1',
            'Sec-Ch-Ua-Platform': '"Android"',
        }
        base.update(extra or {})
        return base

    async def request(self, method, url, headers=None, data=None):
        return await http.request(method.upper(), url, headers=headers, data=data, timeout=60,
                                  max_redirects=0, raise_for_status=False, cookies=self.jar)


async def generateImage(prompt, options=None, **kw):
    if isinstance(options, dict):
        aspectRatio = options.get('aspectRatio')
    elif isinstance(options, str):
        aspectRatio = options
    else:
        aspectRatio = kw.get('aspectRatio')
    if aspectRatio is None:
        aspectRatio = DEFAULT_RATIO
    if not prompt or not str(prompt).strip():
        raise ValueError('Prompt must not be empty.')

    ratio = aspectRatio if aspectRatio in RATIOS else DEFAULT_RATIO
    ar = RATIOS[ratio]

    text = str(prompt).strip()
    q = _enc(text)
    s = _Session()

    warmUrl = f'{BASE}/images/create/ai-image-generator?FORM=IRPGEN'
    await s.request('get', warmUrl, s.headers({'Accept': 'text/html,*/*'}))

    createUrl = f'{BASE}/images/create/ai-image-generator?q={q}&rt=4&mdl=12&ar={ar}&FORM=GENCRE&sm=1'
    body = urlencode({'q': text, 'qs': 'ds', 'model': 'maiimage26e', 'aspectRatio': ratio})
    created = await s.request('post', createUrl, s.headers({
        'Content-Type': 'application/x-www-form-urlencoded',
        'Origin': BASE,
        'Referer': warmUrl,
        'Accept': 'text/html,*/*',
    }), body)

    location = created.headers.get('location') or ''
    mt = re.search(r'[?&]id=(1-[a-f0-9]+)', location) or re.search(r'[?&;]id=(1-[a-f0-9]+)', created.text())
    if not mt:
        raise RuntimeError(f'Bing did not return a creation id (status {created.status}).')
    cid = mt.group(1)

    pageUrl = f'{createUrl}&id={cid}'
    page = await s.request('get', pageUrl, s.headers({'Accept': 'text/html,*/*', 'Referer': createUrl}))
    html = page.text()
    ig = re.search(r'IG:"([A-F0-9]{32})"', html) or re.search(r'[?&]IG=([A-F0-9]{32})', html)
    if not ig:
        raise RuntimeError(f'Bing session token not found (status {page.status}).')
    IG = ig.group(1)

    for _ in range(POLL_LIMIT):
        await asyncio.sleep(POLL_INTERVAL)
        res = await s.request('get',
                              f'{BASE}/images/create/ai-image-generator/async/results/{cid}?q={q}&IG={IG}&IID=images.as&mmasync=1&sm=1&mdl=12&ar={ar}&girftp=1',
                              s.headers({'Accept': '*/*', 'Referer': pageUrl}))
        data = res.text()
        ids = list(dict.fromkeys(re.findall(r'OIG\d*\.[A-Za-z0-9_.-]+', data)))
        if ids:
            return [f'https://tse2.mm.bing.net/th/id/{x}?pid=ImgGn' for x in ids]
        if len(data) > 100:
            message = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', data)).strip()[:150]
            raise RuntimeError(f'Bing rejected the request: {message}')

    raise RuntimeError('Bing image generation timed out.')
