import base64
import json
import re
import sys
from urllib.parse import urlencode, urlparse

from lib.compat import http
from lib.scrapers.src.facebook import extractVideoThumbnail


_UNDEFINED = object()


def _js_text(value):
    if value is _UNDEFINED:
        return 'undefined'
    if value is None:
        return 'null'
    return str(value)


def detectContentType(url):
    try:
        p = urlparse(url).path
    except Exception:
        return 'unknown'
    if '/reel/' in p:
        return 'reel'
    if '/p/' in p:
        return 'post'
    if '/tv/' in p:
        return 'tv'
    return 'unknown'


async def formatInstagramResponse(videos, images, contentType='unknown'):
    hasVideos = len(videos) > 0
    hasImages = len(images) > 0

    if not hasVideos and not hasImages:
        return {'status': False, 'error': 'No media found'}

    filteredVideos = videos
    filteredImages = images

    if contentType == 'post' and hasVideos and len(videos) > len(images):
        realVideos = [v for v in videos if re.search(r'\.mp4', v, re.I) or '/video/' in v]
        filteredVideos = [] if not realVideos else realVideos

    if len(filteredImages) == 1 and len(filteredVideos) == 0:
        return {'status': True, 'result': {
            'metadata': {'type': 'single_image'},
            'media': {
                'images': [{'url': u} for u in filteredImages],
                'videos': [],
                'audios': [],
                'thumbnail': filteredImages[0],
            },
        }}

    if len(filteredVideos) == 1 and len(filteredImages) == 0:
        thumbnail = await extractVideoThumbnail(filteredVideos[0])
        return {'status': True, 'result': {
            'metadata': {'type': 'reels'},
            'media': {
                'videos': [{'url': u} for u in filteredVideos],
                'images': [],
                'audios': [],
                'thumbnail': thumbnail,
            },
        }}

    items = []
    for url in filteredVideos:
        items.append({'index': len(items) + 1, 'type': 'video', 'videos': [{'url': url}], 'images': []})
    for url in filteredImages:
        items.append({'index': len(items) + 1, 'type': 'image', 'images': [{'url': url}], 'videos': []})

    kind = 'single'
    if len(items) > 1:
        kind = 'carousel'
    elif len(items) == 1 and items[0]['type'] == 'video':
        kind = 'reels'
    elif len(items) == 1 and items[0]['type'] == 'image':
        kind = 'single_image'

    thumbnail = None
    first = items[0] if items else None
    if first:
        if first['type'] == 'video':
            thumbnail = await extractVideoThumbnail(first['videos'][0]['url'])
        else:
            thumbnail = first['images'][0]['url']

    return {'status': True, 'result': {
        'metadata': {
            'type': kind,
            'totalItems': len(items),
            'totalImages': len(filteredImages),
            'totalVideos': len(filteredVideos),
        },
        'media': {'items': items, 'thumbnail': thumbnail},
    }}


def getTokenData(link):
    try:
        mt = re.search(r'token=([^&]+)', link)
        token = mt.group(1) if mt else None
        if not token:
            return None
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode('utf-8', errors='replace'))
    except Exception:
        return None


async def instagram(url):
    contentType = detectContentType(url)

    try:
        res = await http.post('https://savereels.io/api/ajaxSearch',
                              data=urlencode({'q': url, 'v': 'v2'}),
                              headers={
                                  'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                  'X-Requested-With': 'XMLHttpRequest',
                                  'Accept': 'application/json, text/plain, */*',
                                  'Referer': 'https://savereels.io/',
                                  'Origin': 'https://savereels.io',
                              },
                              timeout=30, max_redirects=5)
        try:
            body = res.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            body = {}

        status = body['status'] if 'status' in body else _UNDEFINED
        if status != 'ok':
            return {'status': False, 'error': 'Savereels error: ' + _js_text(status)}

        html = body.get('data') or body
        if isinstance(html, (dict, list)):
            found = False
            if isinstance(html, dict):
                for key in ('html', 'content', 'body', 'data'):
                    if isinstance(html.get(key), str) and html.get(key):
                        html = html[key]
                        found = True
                        break
            if not found:
                html = json.dumps(html, separators=(',', ':'), ensure_ascii=False)
        if not isinstance(html, str):
            html = _js_text(html)

        if not html or len(html) < 10:
            return {'status': False, 'error': 'Empty response from Savereels'}

        allLinks = []
        patterns = [
            r'https://[a-zA-Z0-9.-]+\.snapcdn\.app/get\?token=[^\s"\']+',
            r'https://[a-zA-Z0-9.-]+\.snapcdn\.app/video[^\s"\']*',
            r'https://[^\s"\']+\.(?:mp4|jpg|jpeg|png|gif|webp)[^\s"\']*',
            r'https://i\.snapcdn\.app/photo\?token=[^\s"\']+',
        ]
        for pattern in patterns:
            for matched in re.findall(pattern, html, re.I):
                clean = matched.replace('"', '').replace("'", '').strip()
                if clean and clean.startswith('http') and clean not in allLinks:
                    allLinks.append(clean)

        videos = []
        imageMap = {}
        for link in allLinks:
            data = getTokenData(link)
            if data and data.get('filename'):
                filename = data['filename']
                ext = filename.split('.')[-1].lower()

                if ext in ('mp4', 'mov', 'avi', 'mkv', 'webm'):
                    if link not in videos:
                        videos.append(link)
                    continue

                if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
                    originalUrl = data['url'].split('?')[0] if data.get('url') else link

                    w = 0
                    h = 0
                    match1 = re.search(r'_(\d+)x(\d+)\.', filename)
                    match2 = re.search(r'_(\d+)\.(jpg|jpeg|png|gif|webp)$', filename, re.I)
                    if match1:
                        w, h = int(match1.group(1)), int(match1.group(2))
                    elif match2:
                        w = h = int(match2.group(1))

                    if w == 0:
                        w = 1
                    if h == 0:
                        h = 1

                    cur = imageMap.get(originalUrl)
                    if cur is None or (w * h) > (cur['w'] * cur['h']):
                        imageMap[originalUrl] = {'url': link, 'w': w, 'h': h}

        sortedItems = sorted(imageMap.values(), key=lambda item: -(item['w'] * item['h']))
        imageLinks = [item['url'] for item in sortedItems]

        filteredVideos = videos
        filteredImages = imageLinks

        if contentType == 'reel':
            filteredImages = []
            if len(filteredVideos) == 0:
                return {'status': False, 'error': 'No video found for reel'}

        if len(filteredVideos) == 0 and len(filteredImages) == 0:
            return {'status': False, 'error': 'No media found'}

        return await formatInstagramResponse(filteredVideos, filteredImages, contentType)

    except Exception as err:
        print('[IG Savereels Error]', err, file=sys.stderr)
        return {'status': False, 'error': str(err)}
