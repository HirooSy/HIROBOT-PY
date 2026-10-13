import json
import os
import re
import sys
import time
from http.cookiejar import CookieJar
from urllib.parse import quote

from lib.compat import ffmpeg as ff, http


FFMPEG_PATH = ff.FFMPEG
TMP_DIR = os.path.normpath(os.getcwd() + os.sep + (os.environ.get('TMP') or 'data/tmp'))
os.makedirs(TMP_DIR, exist_ok=True)
GIF_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/136.0.0.0'


async def gifToMp4(fileUrl):
    tmpDir = TMP_DIR
    try:
        for f in os.listdir(tmpDir):
            if f.startswith('tmp_'):
                try:
                    os.unlink(os.path.join(tmpDir, f))
                except OSError:
                    pass
    except OSError:
        pass

    ts = int(time.time() * 1000)
    tmpInput = os.path.join(tmpDir, f'tmp_in_{ts}.gif')
    tmpOutput = os.path.join(tmpDir, f'tmp_out_{ts}.mp4')

    response = await http.get(fileUrl, headers={'User-Agent': GIF_UA})
    with open(tmpInput, 'wb') as fh:
        fh.write(response.content)

    if not os.path.exists(tmpInput) or os.path.getsize(tmpInput) == 0:
        raise RuntimeError('Download gagal, file input kosong')

    try:
        await ff.ffmpeg(['-i', tmpInput, '-movflags', 'faststart', '-pix_fmt', 'yuv420p',
                         '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', '-c:v', 'libx264', '-preset', 'fast',
                         '-crf', '23', '-f', 'mp4', tmpOutput])
    except Exception as err:
        raise RuntimeError(f'ffmpeg error: {err}')

    if os.path.exists(tmpInput):
        os.unlink(tmpInput)

    if not os.path.exists(tmpOutput) or os.path.getsize(tmpOutput) == 0:
        raise RuntimeError('Konversi gagal, file output kosong atau tidak ditemukan')

    return tmpOutput


def _resp_status(error):
    return getattr(getattr(error, 'response', None), 'status', None)


def _obj(v):
    return v if isinstance(v, dict) else {}


class _Pinterest:
    def __init__(self):
        self.api = {
            'base': 'https://www.pinterest.com',
            'endpoints': {
                'search': '/resource/BaseSearchResource/get/',
                'pin': '/resource/PinResource/get/',
                'user': '/resource/UserResource/get/',
            },
        }
        self.headers = {
            'accept': 'application/json, text/javascript, */*, q=0.01',
            'referer': 'https://id.pinterest.com/',
            'user-agent': 'Postify/1.0.0',
            'x-app-version': 'a9522f',
            'x-pinterest-appstate': 'active',
            'x-pinterest-pws-handler': 'www/[username]/[slug].js',
            'x-pinterest-source-url': '/search/pins/?rs=typed&q=search/',
            'x-requested-with': 'XMLHttpRequest',
        }

    def isUrl(self, s):
        return re.match(r'^[A-Za-z][A-Za-z0-9+.\-]*:', s or '') is not None

    def isPin(self, url):
        if not url:
            return False
        patterns = [
            r'^https?://(?:www\.)?pinterest\.com/pin/[\w.-]+',
            r'^https?://(?:www\.)?pinterest\.[\w.]+/pin/[\w.-]+',
            r'^https?://pin\.it/[\w.-]+',
            r'^https?://(?:www\.)?pinterest\.com/amp/pin/[\w.-]+',
        ]
        clean = url.strip().lower()
        return any(re.match(p, clean) for p in patterns)

    async def getCookies(self):
        jar = CookieJar()
        try:
            await http.get('https://id.pinterest.com', cookies=jar)
            pairs = [f'{c.name}={c.value}' for c in jar]
            if pairs:
                return '; '.join(pairs)
            return None
        except Exception as error:
            print(error, file=sys.stderr)
            return None

    async def search(self, query, limit=10):
        if not query:
            return {'status': False, 'code': 400, 'result': {'message': 'Please provide a search query.'}}
        try:
            cookies = await self.getCookies()
            if not cookies:
                return {'status': False, 'code': 400, 'result': {'message': 'Failed to retrieve cookies. Please try again later.'}}

            params = {
                'source_url': f'/search/pins/?q={query}',
                'data': json.dumps({
                    'options': {
                        'isPrefetch': False,
                        'query': query,
                        'scope': 'pins',
                        'bookmarks': [''],
                        'no_fetch_context_on_resource': False,
                        'page_size': limit,
                    },
                    'context': {},
                }, separators=(',', ':')),
                '_': int(time.time() * 1000),
            }
            res = await http.get(self.api['base'] + self.api['endpoints']['search'],
                                 headers={**self.headers, 'cookie': cookies}, params=params)
            data = res.json()

            container = []
            results = [v for v in data['resource_response']['data']['results'] if _obj(v.get('images')).get('orig')]
            for result in results:
                images = result['images']
                videos = result.get('videos')
                pinner = result['pinner']
                container.append({
                    'id': result.get('id'),
                    'title': result.get('title') or '',
                    'description': result.get('description') or '',
                    'pin_url': f"https://pinterest.com/pin/{result.get('id')}",
                    'media': {
                        'images': {
                            'orig': images['orig'],
                            'small': images.get('236x'),
                            'medium': images.get('474x'),
                            'large': images.get('736x'),
                        },
                        'video': {'video_list': videos['video_list'], 'duration': videos.get('duration')} if videos else None,
                    },
                    'uploader': {
                        'username': pinner.get('username'),
                        'full_name': pinner.get('full_name'),
                        'profile_url': f"https://pinterest.com/{pinner.get('username')}",
                    },
                })

            if not container:
                return {'status': False, 'code': 404, 'result': {'message': f'No results found for "{query}". Try another search term.'}}

            return {'status': True, 'code': 200, 'result': {'query': query, 'total': len(container), 'pins': container}}
        except Exception as error:
            return {'status': False, 'code': _resp_status(error) or 500, 'result': {'message': 'Server error. Please try again later.'}}

    async def download(self, pinUrl):
        if not pinUrl:
            return {'status': False, 'code': 400, 'result': {'message': 'Please provide a Pinterest URL.'}}
        if not self.isUrl(pinUrl):
            return {'status': False, 'code': 400, 'result': {'message': 'Invalid URL format.'}}
        if not self.isPin(pinUrl):
            return {'status': False, 'code': 400, 'result': {'message': 'This is not a valid Pinterest link.'}}

        try:
            pinId = pinUrl.split('/pin/')[1].replace('/', '', 1)
            cookies = await self.getCookies()
            if not cookies:
                return {'status': False, 'code': 400, 'result': {'message': 'Failed to retrieve cookies. Please try again later.'}}

            params = {
                'source_url': f'/pin/{pinId}/',
                'data': json.dumps({'options': {'field_set_key': 'detailed', 'id': pinId}, 'context': {}}, separators=(',', ':')),
                '_': int(time.time() * 1000),
            }
            res = await http.get(self.api['base'] + self.api['endpoints']['pin'],
                                 headers={**self.headers, 'cookie': cookies}, params=params)
            data = res.json()

            if not data['resource_response'].get('data'):
                return {'status': False, 'code': 404, 'result': {'message': 'Pin not found. It may have been deleted or is no longer available.'}}

            pd = data['resource_response']['data']
            mediaUrls = []

            if pd.get('videos'):
                videoFormats = sorted(pd['videos']['video_list'].values(), key=lambda v: -(v.get('width') or 0))
                for video in videoFormats:
                    mediaUrls.append({
                        'type': 'video',
                        'quality': f"{video.get('width')}x{video.get('height')}",
                        'width': video.get('width'),
                        'height': video.get('height'),
                        'duration': pd['videos'].get('duration') or None,
                        'url': video.get('url'),
                        'file_size': video.get('file_size') or None,
                        'thumbnail': pd['images']['orig']['url'],
                    })

            if pd.get('images'):
                imge = [('original', pd['images'].get('orig')), ('large', pd['images'].get('736x')),
                        ('medium', pd['images'].get('474x')), ('small', pd['images'].get('236x'))]
                for quality, image in imge:
                    if image:
                        mediaUrls.append({
                            'type': 'image',
                            'quality': quality,
                            'width': image.get('width'),
                            'height': image.get('height'),
                            'url': image.get('url'),
                            'size': f"{image.get('width')}x{image.get('height')}",
                        })

            if not mediaUrls:
                return {'status': False, 'code': 404, 'result': {'message': 'No media found in this pin.'}}

            pinner = _obj(pd.get('pinner'))
            return {'status': True, 'code': 200, 'result': {
                'id': pd.get('id'),
                'title': pd.get('title') or pd.get('grid_title') or '',
                'description': pd.get('description') or '',
                'media_urls': mediaUrls,
                'uploader': {
                    'id': pinner.get('id') or None,
                    'username': pinner.get('username') or None,
                    'full_name': pinner.get('full_name') or None,
                },
                'statistics': {'saves': pd.get('repin_count') or 0, 'comments': pd.get('comment_count') or 0},
            }}
        except Exception as error:
            if _resp_status(error) == 404:
                return {'status': False, 'code': 404, 'result': {'message': 'Pin not found.'}}
            return {'status': False, 'code': _resp_status(error) or 500, 'result': {'message': 'Server error. Please try again later.'}}


pinterest = _Pinterest()

hlsHeaders = {
    'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
    'referer': 'https://id.pinterest.com/',
    'origin': 'https://id.pinterest.com/',
}


async def getPinterestHLS(m3u8Url):
    try:
        res = await http.get(m3u8Url, headers={'User-Agent': 'Mozilla/5.0', 'referer': 'https://id.pinterest.com/'})
        data = res.text()
        base = m3u8Url[:m3u8Url.rfind('/') + 1]
        audioMatch = re.search(r'URI="([^"]+)"', data)
        audio = None
        if audioMatch:
            audio = audioMatch.group(1) if audioMatch.group(1).startswith('http') else base + audioMatch.group(1)
        qualities = []
        for match in re.finditer(r'RESOLUTION=(\d+x\d+).*?\n(.*?\.m3u8)', data):
            qualities.append({
                'resolution': match.group(1),
                'url': match.group(2) if match.group(2).startswith('http') else base + match.group(2),
            })
        qualities.sort(key=lambda q: int(q['resolution'].split('x')[0]))
        return {'audio': audio, 'qualities': qualities}
    except Exception:
        return None


def formatNumber(n):
    if not n:
        return '0'
    if n >= 1000000:
        return f'{n / 1000000:.1f}M'
    if n >= 1000:
        return f'{n / 1000:.1f}K'
    return str(n)


async def mergeVideoAudio(videoUrl, audioUrl, output='/tmp/pin_output.mp4'):
    headerArg = f"referer: {hlsHeaders['referer']}\r\norigin: {hlsHeaders['origin']}\r\n"
    args = ['-y']
    args += ['-user_agent', hlsHeaders['User-Agent'], '-headers', headerArg, '-i', videoUrl]
    if audioUrl:
        args += ['-user_agent', hlsHeaders['User-Agent'], '-headers', headerArg, '-i', audioUrl]
    args += ['-c:v', 'copy', '-c:a', 'aac', '-shortest', output]
    await ff.run(FFMPEG_PATH, args)
    return output


def isPinterestUrl(s):
    return re.search(r'pinterest\.(com|co\.\w+)/pin/|pin\.it/', s or '', re.I) is not None


def detectMode(args):
    modeMap = {
        'vid': 'video', 'video': 'video',
        'gif': 'gif', 'gifs': 'gif',
        'img': 'image', 'image': 'image', 'images': 'image',
    }
    first = args[0].lower() if args else None
    return modeMap.get(first, 'all')


def pickPreviewUrl(pin):
    imgs = _obj(_obj(pin.get('media')).get('images'))
    for k in ('medium', 'large', 'small', 'orig'):
        u = _obj(imgs.get(k)).get('url')
        if u and not u.lower().endswith('.gif'):
            return u
    return _obj(imgs.get('orig')).get('url') or None


def _sortedVideos(videoList):
    return sorted(videoList.values(), key=lambda v: -(v.get('width') or 0))


def extractMediaFromPin(pin):
    mediaUrls = []
    embed = _obj(pin.get('embed'))
    if embed.get('type') == 'gif':
        mediaUrls.append({'type': 'gif', 'url': embed.get('src') or embed.get('url') or None,
                          'thumbnailUrl': pickPreviewUrl(pin), 'isGif': True})
        return mediaUrls

    images = _obj(_obj(pin.get('media')).get('images'))
    origUrl = _obj(images.get('orig')).get('url')
    if isinstance(origUrl, str) and origUrl.lower().endswith('.gif'):
        mediaUrls.append({'type': 'gif', 'url': origUrl, 'thumbnailUrl': origUrl, 'isGif': True})
        return mediaUrls

    for variant in ('orig', '736x', '474x', '236x'):
        vurl = _obj(images.get(variant)).get('url')
        if isinstance(vurl, str) and vurl.lower().endswith('.gif'):
            mediaUrls.append({'type': 'gif', 'url': vurl, 'thumbnailUrl': vurl, 'isGif': True})
            return mediaUrls

    video = _obj(_obj(pin.get('media')).get('video'))
    if video.get('video_list'):
        for v in _sortedVideos(video['video_list']):
            mediaUrls.append({'type': 'video', 'url': v.get('url'), 'thumbnailUrl': pickPreviewUrl(pin), 'isGif': False})
        return mediaUrls

    if images.get('orig'):
        imageUrl = _or_(images['orig'].get('url'), _obj(images.get('large')).get('url'), _obj(images.get('medium')).get('url'))
        if imageUrl and not imageUrl.lower().endswith('.gif'):
            mediaUrls.append({'type': 'image', 'url': imageUrl,
                              'thumbnailUrl': pickPreviewUrl(pin) or images['orig'].get('url'), 'isGif': False})

    return mediaUrls if mediaUrls else None


def _or_(*vals):
    for v in vals:
        if v:
            return v
    return None
