import json
import os
import re
import sys
from urllib.parse import urlencode, urlparse

from lib.compat import ffmpeg as ff, http


_UNDEFINED = object()


def _js_text(value):
    if value is _UNDEFINED:
        return 'undefined'
    if value is None:
        return 'null'
    return str(value)


async def extractVideoThumbnail(videoUrl, timeoutMs=15000):
    outPath = ff.tmp_path('jpg', 'fb_thumb_')
    try:
        await ff.ffmpeg(['-user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                         '-headers', 'Referer: https://savereels.io/\r\n', '-ss', '0.5', '-i', videoUrl,
                         '-frames:v', '1', '-q:v', '4', outPath], timeout=timeoutMs / 1000)
        if os.path.exists(outPath) and os.path.getsize(outPath) > 0:
            return outPath
    except Exception:
        pass
    try:
        if os.path.exists(outPath):
            os.unlink(outPath)
    except OSError:
        pass
    return None


def isFbUrl(url):
    try:
        host = urlparse(url).hostname or ''
    except Exception:
        return False
    return bool(re.search(r'(^|\.)facebook\.com$|(^|\.)fb\.watch$|(^|\.)fb\.com$', host, re.I))


def _qualityNum(quality):
    mt = re.match(r'\d+', str(quality))
    return int(mt.group(0)) if mt else 0


async def facebook(url):
    if not isFbUrl(url):
        return {'status': False, 'error': 'Invalid Facebook URL'}

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

        tm = re.search(r'<h3[^>]*>([^<]+)</h3>', html, re.I) or re.search(r'class="[^"]*title[^"]*"[^>]*>([^<]+)<', html, re.I)
        title = tm.group(1).strip() if tm else None

        rows = []
        for row in re.finditer(r'<tr[^>]*>([\s\S]*?)</tr>', html, re.I):
            rowHtml = row.group(1)
            qualityMatch = re.search(r'(\d{3,4}p)\s*(\(HD\)|\(SD\))?', rowHtml, re.I)
            hrefMatch = re.search(r'href="([^"]+)"', rowHtml, re.I)
            needsRender = bool(re.search('render', rowHtml, re.I)) and not re.search(r'no\s*</td>', rowHtml, re.I)
            if qualityMatch and hrefMatch:
                rows.append({
                    'quality': qualityMatch.group(1),
                    'label': qualityMatch.group(0).strip(),
                    'url': hrefMatch.group(1).replace('&amp;', '&'),
                    'needsRender': needsRender,
                })

        if not rows:
            directLinks = []
            patterns = [
                r'https://[a-zA-Z0-9.-]+\.snapcdn\.app/get\?token=[^\s"\']+',
                r'https://[a-zA-Z0-9.-]+\.snapcdn\.app/video[^\s"\']*',
                r'https://[^\s"\']+\.mp4[^\s"\']*',
            ]
            for pattern in patterns:
                for matched in re.findall(pattern, html, re.I):
                    clean = matched.replace('"', '').replace("'", '').strip()
                    if clean and clean.startswith('http') and clean not in directLinks:
                        directLinks.append(clean)

            if not directLinks:
                return {'status': False, 'error': 'No downloadable video found'}

            videoUrl = directLinks[0]
            thumbnail = await extractVideoThumbnail(videoUrl)
            return {'status': True, 'result': {
                'metadata': {'type': 'video', 'title': title},
                'media': {'videos': [{'url': videoUrl, 'quality': None}], 'thumbnail': thumbnail},
            }}

        instant = sorted([r for r in rows if not r['needsRender']], key=lambda r: -_qualityNum(r['quality']))
        rendered = sorted([r for r in rows if r['needsRender']], key=lambda r: -_qualityNum(r['quality']))
        orderedVideos = [{'url': r['url'], 'quality': r['quality'], 'needsRender': r['needsRender']} for r in instant + rendered]

        best = orderedVideos[0] if orderedVideos else None
        thumbnail = await extractVideoThumbnail(best['url']) if best else None

        return {'status': True, 'result': {
            'metadata': {'type': 'video', 'title': title},
            'media': {'videos': orderedVideos, 'thumbnail': thumbnail},
        }}
    except Exception as err:
        print('[FB Savereels Error]', err, file=sys.stderr)
        return {'status': False, 'error': str(err)}
