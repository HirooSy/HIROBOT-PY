import asyncio
import os
import random
import re
import ssl
import sys
import urllib.error
import urllib.request

from lib.compat import http


AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1.1 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
]
_SSL_CTX = ssl.create_default_context()
MIN_FREE_MARGIN = 100 * 1024 * 1024


def getRandomUserAgent():
    return random.choice(AGENTS)


def getHeaders():
    return {
        'User-Agent': getRandomUserAgent(),
        'Accept': 'video/mp4,video/webm,video/ogg,*/*;q=0.9',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': 'https://animeinweb.com/',
        'Origin': 'https://animeinweb.com',
        'Sec-Ch-Ua': '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="99"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'video',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'cross-site',
        'Connection': 'keep-alive',
        'Cache-Control': 'no-cache',
    }


class _ApiResponse:
    def __init__(self, data, status):
        self.data = data
        self.status = status


class _ApiClient:
    baseURL = 'https://animeinweb.com/api/proxy'
    timeout = 15

    def __init__(self):
        self.headers = {
            'x-proxy-secret': 'animein-secure-proxy-key-123',
            'User-Agent': getRandomUserAgent(),
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://animeinweb.com/',
            'Origin': 'https://animeinweb.com',
        }

    async def get(self, path, params=None):
        headers = dict(self.headers)
        headers['User-Agent'] = getRandomUserAgent()
        res = await http.get(self.baseURL + path, params=params, headers=headers, timeout=self.timeout)
        try:
            data = res.json()
        except Exception:
            data = res.text()
        return _ApiResponse(data, res.status)


apiClient = _ApiClient()


def _obj(value):
    return value if isinstance(value, dict) else {}


async def searchAnime(keyword, page=0):
    try:
        res = await apiClient.get('/3/2/explore/movie', {'keyword': keyword, 'page': str(page), 'sort': 'views'})
        data = _obj(res.data)
        return _obj(data.get('data')).get('movie') or data.get('movie') or []
    except Exception as error:
        print('Search error:', error, file=sys.stderr)
        return []


async def getDetail(animeId):
    try:
        res = await apiClient.get(f'/3/2/movie/detail/{animeId}')
        return _obj(res.data).get('data')
    except Exception as error:
        print('Get detail error:', error, file=sys.stderr)
        return None


def parseEpisodeNumber(ep):
    source = str(ep.get('title') or '')
    withLabel = re.search(r'(?:episode|ep)\s*(\d+(?:\.\d+)?)', source, re.I)
    if withLabel:
        return float(withLabel.group(1))
    anyNumber = re.search(r'(\d+(?:\.\d+)?)', source)
    if anyNumber:
        return float(anyNumber.group(1))
    return 0


async def _fetchEpisodePage(animeId, page):
    res = await apiClient.get(f'/3/2/movie/episode/{animeId}', {'page': page})
    return _obj(_obj(res.data).get('data')).get('episode') or []


async def getEpisodes(animeId):
    allEpisodes = []
    try:
        firstPage = await _fetchEpisodePage(animeId, 0)
        allEpisodes.extend(firstPage)

        if len(firstPage) >= 30:
            BATCH_SIZE = 5
            page = 1
            keepGoing = True
            while keepGoing:
                pagesToFetch = [page + i for i in range(BATCH_SIZE)]

                async def safeFetch(p):
                    try:
                        return await _fetchEpisodePage(animeId, p)
                    except Exception:
                        return []

                results = await asyncio.gather(*[safeFetch(p) for p in pagesToFetch])
                for episodes in results:
                    allEpisodes.extend(episodes)
                    if len(episodes) < 30:
                        keepGoing = False

                page += BATCH_SIZE
                if page > 500:
                    break
    except Exception as error:
        print('Get episodes error:', error, file=sys.stderr)

    seen = set()
    unique = []
    for ep in allEpisodes:
        key = ep.get('id')
        if key in seen:
            continue
        seen.add(key)
        unique.append(ep)
    return sorted(unique, key=parseEpisodeNumber)


async def getStream(episodeId):
    try:
        res = await apiClient.get(f'/3/2/episode/streamnew/{episodeId}')
        data = _obj(res.data)
        return data.get('data') or res.data
    except Exception as error:
        print('Get stream error:', error, file=sys.stderr)
        return None


async def getFileSize(url):
    for attempt in range(3):
        try:
            headers = getHeaders()
            headers['Range'] = 'bytes=0-1'
            response = await http.get(url, headers=headers, timeout=10, max_bytes=2,
                                      validate_status=lambda s: s in (200, 206))
            contentRange = response.headers.get('content-range')
            if contentRange:
                return _parseIntOrNone(contentRange.split('/')[1])
            contentLength = response.headers.get('content-length')
            if contentLength:
                return _parseIntOrNone(contentLength)
            return None
        except Exception as error:
            print(f'Attempt {attempt + 1} getFileSize error:', error, file=sys.stderr)
            if attempt < 2:
                await asyncio.sleep(1 * (attempt + 1))
    return None


def _parseIntOrNone(value):
    mt = re.match(r'\s*[+-]?\d+', str(value))
    return int(mt.group(0)) if mt else None


def _freeBytes(dirPath):
    try:
        st = os.statvfs(dirPath)
        return st.f_bavail * st.f_frsize
    except (OSError, AttributeError):
        return None


def _assertEnoughDiskSpace(dirPath, neededBytes):
    if not neededBytes:
        return
    freeBytes = _freeBytes(dirPath or '.')
    if freeBytes is None:
        return
    if freeBytes < neededBytes + MIN_FREE_MARGIN:
        raise RuntimeError(f'Not enough disk space: need ~{formatSize(neededBytes)}, only {formatSize(freeBytes)} free')


def formatSize(bytes_):
    if not bytes_:
        return '?'
    if bytes_ >= 1024 * 1024 * 1024:
        return f'{bytes_ / 1024 / 1024 / 1024:.1f} GB'
    return f'{bytes_ / 1024 / 1024:.1f} MB'


def _downloadSync(url, outputPath):
    headers = getHeaders()
    headers['Accept-Encoding'] = 'identity'
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        resp = urllib.request.urlopen(req, timeout=120, context=_SSL_CTX)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Request failed with status code {error.code}') from None
    with resp, open(outputPath, 'wb') as fh:
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            fh.write(chunk)
    return os.path.getsize(outputPath)


async def downloadEpisode(url, outputPath):
    expectedSize = await getFileSize(url)
    _assertEnoughDiskSpace(os.path.dirname(outputPath), expectedSize)

    lastError = None
    for attempt in range(3):
        try:
            return await asyncio.to_thread(_downloadSync, url, outputPath)
        except Exception as error:
            lastError = error
            print(f'Attempt {attempt + 1} download error:', error, file=sys.stderr)
            if os.path.exists(outputPath):
                try:
                    os.unlink(outputPath)
                except OSError:
                    pass
            if getattr(error, 'errno', None) == 28:
                break
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
    raise lastError

