import base64
import binascii
import json
import re
import sys
from urllib.parse import quote

from baileys_lite.foundation.node_crypto import createDecipheriv
from lib.compat import http
from lib.scrapers.src import ytsearch


KEY = bytes.fromhex('C5D58EF67A7584E4A29F6C35BBC4EB12')
HEADERS = {
    'Content-Type': 'application/json',
    'Origin': 'https://save-tube.com',
    'Referer': 'https://save-tube.com/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.5',
}
FILE_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0'
NAN = float('nan')
URI_SAFE = "-_.!~*'()"


def _obj(v):
    return v if isinstance(v, dict) else {}


def _num(v):
    if v is None or isinstance(v, bool):
        return NAN if v is None else float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip() or 0)
        except ValueError:
            return NAN
    return NAN


def _or(*vals):
    for v in vals[:-1]:
        if v not in (None, '', 0, False) and v == v:
            return v
    return vals[-1]


def _jsStr(v):
    return 'undefined' if v is None else str(v)


def _b64_decode(value):
    text = re.sub(r'[^A-Za-z0-9+/]', '', str(value or '').split('=')[0])
    text += '=' * (-len(text) % 4)
    try:
        return base64.b64decode(text)
    except (binascii.Error, ValueError):
        return b''


def decryptPayload(encryptedBase64):
    raw = _b64_decode(encryptedBase64)
    if len(raw) < 16:
        raise ValueError('Invalid cipher format')
    iv, ciphertext = raw[:16], raw[16:]
    decipher = createDecipheriv('aes-128-cbc', KEY, iv)
    decrypted = b''.join([decipher.update(ciphertext), decipher.final()])
    return json.loads(decrypted.decode('utf-8', errors='replace'))


async def getRandomCdn():
    res = await http.get('https://media.savetube.vip/api/random-cdn', raise_for_status=False)
    if not res.ok:
        raise RuntimeError(f'HTTP {res.status}: CDN network unavailable')
    cdn = _obj(res.json()).get('cdn')
    if not cdn:
        raise RuntimeError('CDN response corrupted')
    return cdn


async def getVideoInfo(cdn, youtubeUrl):
    res = await http.post(f'https://{cdn}/v2/info', json={'url': youtubeUrl}, headers=HEADERS, raise_for_status=False)
    if not res.ok:
        raise RuntimeError(f'HTTP {res.status}: Failed to fetch video info')
    data = _obj(res.json())
    if not data.get('status') or not data.get('data'):
        raise RuntimeError(data.get('message') or 'Invalid or unsupported YouTube URL')
    return decryptPayload(data['data'])


async def getDownloadUrl(cdn, key, quality, downloadType='video'):
    payload = {'downloadType': downloadType, 'quality': _jsStr(quality)}
    if key is not None:
        payload['key'] = key
    res = await http.post(f'https://{cdn}/download', json=payload, headers=HEADERS, raise_for_status=False)
    if not res.ok:
        return {'url': None, 'error': f'HTTP {res.status}'}
    data = _obj(res.json())
    if not data.get('status') or not _obj(data.get('data')).get('downloadUrl'):
        return {'url': None, 'error': data.get('message') or 'Server render failed'}
    return {'url': data['data']['downloadUrl'], 'error': None}


def extractYoutubeId(url):
    mt = re.search(r'(?:youtu\.be/|youtube\.com/(?:embed/|live/|shorts/)|[?&]v=)([a-zA-Z0-9\-_]{11})', url or '')
    return mt.group(1) if mt else None


async def ytdl(type='audio', url=None):
    kind = type
    videoId = extractYoutubeId(url)
    if not videoId:
        raise RuntimeError(f'YouTube download error: could not extract a valid video ID from URL: {url}')

    try:
        cdn = await getRandomCdn()
        videoData = await getVideoInfo(cdn, url)
        quality = ''
        if kind == 'audio':
            audioFormats = videoData.get('audio_formats') or []
            if not audioFormats:
                raise RuntimeError('No audio formats available for this video')
            selectedFormat = next((f for f in audioFormats if _num(f.get('quality')) >= 128), audioFormats[0])
            quality = selectedFormat.get('quality')
        else:
            videoFormats = videoData.get('video_formats') or []
            if not videoFormats:
                raise RuntimeError('No video formats available for this video')
            selectedFormat = (next((f for f in videoFormats if f.get('quality') == 720), None)
                              or next((f for f in videoFormats if _num(f.get('quality')) >= 720), None)
                              or videoFormats[-1])
            quality = selectedFormat.get('quality')

        downloadUrl = selectedFormat.get('url')
        key = videoData.get('key')
        if not downloadUrl:
            result = await getDownloadUrl(cdn, key, quality, kind)
            if result['error']:
                raise RuntimeError(f"Failed to get download URL: {result['error']}")
            downloadUrl = result['url']
        if not downloadUrl:
            raise RuntimeError('No download URL found')

        cleanTitle = (videoData.get('title') or 'video').replace('#', '')
        sep = '&' if '?' in downloadUrl else '?'
        finalUrl = f"{downloadUrl}{sep}title={quote(cleanTitle, safe=URI_SAFE)}"

        fileRes = await http.get(finalUrl, headers={'User-Agent': FILE_UA, 'Accept': '*/*'}, timeout=300, raise_for_status=False)
        if not fileRes.ok:
            raise RuntimeError(f'File download failed with status {fileRes.status}')
        buffer = fileRes.content

        views = duration = thumbnail = channel = None
        try:
            meta = _obj(await ytsearch.search({'videoId': videoId}))
            views = meta.get('views')
            duration = _or(_obj(meta.get('duration')).get('timestamp'), meta.get('timestamp'))
            thumbnail = meta.get('thumbnail')
            channel = _obj(meta.get('author')).get('name')
        except Exception as metaErr:
            print('[ytdl] yts metadata fallback failed:', metaErr, file=sys.stderr)
            thumbnail = videoData.get('thumbnail') or None
            duration = _or(videoData.get('durationLabel'), videoData.get('duration'), 'Unknown')

        if views is None:
            views = videoData.get('views') if videoData.get('views') is not None else 0
        return {
            'buffer': buffer,
            'mime': 'audio/mpeg' if kind == 'audio' else 'video/mp4',
            'title': _or(videoData.get('title'), videoId),
            'views': views,
            'duration': _or(duration, 'Unknown'),
            'thumbnail': _or(thumbnail, None),
            'channel': _or(channel, _obj(videoData.get('author')).get('name'), 'Unknown'),
        }
    except Exception as error:
        raise RuntimeError(f'YouTube download error: {error}')


default = ytdl
