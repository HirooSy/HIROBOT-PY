import asyncio
import base64
import json
import re
import time

from lib.compat import http
from lib.utils.handler import G


BASE = 'https://spotsaver.net'
YTM_API = 'https://music.youtube.com/youtubei/v1/search'
YTM_KEY = 'AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30'
YTM_VERSION = '1.20260915.14.00'
UA = 'Mozilla/5.0 (Linux; Android 13; SM-A536E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36'
CLIENT_HEADERS = {'User-Agent': UA, 'Accept': 'application/json, text/plain, */*', 'Accept-Language': 'id-ID,id;q=0.9,en;q=0.8',
                  'Referer': BASE + '/id/', 'Origin': BASE}
URL_REGEX = re.compile(r'https://(?![^:@/\s]+:[^:@/\s]+@)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(:\d+)?(/[^\s]*)?')
_cache = {}
_pending = {}
SEARCH_CACHE_TTL = 30


def pick_track(t):
    if not t:
        return None
    return {'id': t.get('id'), 'title': t.get('title'), 'artist': t.get('artist'), 'album': t.get('album'), 'duration': t.get('duration'),
            'thumbnail': t.get('thumbnail'), 'previewUrl': t.get('previewUrl') or t.get('preview_url'),
            'spotifyUrl': f"https://open.spotify.com/track/{t['id']}" if t.get('id') else None}


async def spotsaver_get(params):
    r = await http.get(BASE + '/api/spotify', params=params, headers=CLIENT_HEADERS, timeout=90, raise_for_status=False)
    try:
        d = r.json()
    except Exception:
        d = None
    return r.status, d


async def spotsaver_search(q):
    if not q:
        raise ValueError('Empty query')
    status, d = await spotsaver_get({'q': q})
    if status >= 400 or not d or not d.get('items'):
        raise RuntimeError(f'Search failed: HTTP {status}')
    return {'query': q, 'type': d.get('type') or 'search', 'count': len(d['items']), 'items': [pick_track(i) for i in d['items']]}


async def spotsaver_info(url):
    if not url:
        raise ValueError('Empty URL')
    status, d = await spotsaver_get({'url': url})
    if status >= 400 or not d or not d.get('items'):
        raise RuntimeError(f'Info request failed: HTTP {status}')
    return {'type': d.get('type'), 'url': url, 'count': len(d['items']), 'items': [pick_track(i) for i in d['items']]}


async def ytm_search(query):
    body = {'context': {'client': {'clientName': 'WEB_REMIX', 'clientVersion': YTM_VERSION, 'hl': 'id', 'gl': 'ID'}}, 'query': query,
            'params': 'EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D'}
    r = await http.post(f'{YTM_API}?key={YTM_KEY}&prettyPrint=false', json=body, timeout=20, raise_for_status=False,
                        headers={'User-Agent': UA, 'X-Goog-Api-Key': YTM_KEY, 'X-YouTube-Client-Name': '67', 'X-YouTube-Client-Version': YTM_VERSION,
                                 'Origin': 'https://music.youtube.com', 'Referer': 'https://music.youtube.com/'})
    try:
        d = r.json()
    except Exception:
        d = {}
    out = []
    for tab in (((d.get('contents') or {}).get('tabbedSearchResultsRenderer') or {}).get('tabs')) or []:
        for sec in ((((tab.get('tabRenderer') or {}).get('content') or {}).get('sectionListRenderer') or {}).get('contents')) or []:
            shelf = sec.get('musicShelfRenderer')
            if not shelf:
                continue
            for it in shelf.get('contents') or []:
                item = it.get('musicResponsiveListItemRenderer')
                if not item:
                    continue
                vid = (item.get('playlistItemData') or {}).get('videoId')
                texts = []
                for col in item.get('flexColumns') or []:
                    runs = (((col.get('musicResponsiveListItemFlexColumnRenderer') or {}).get('text') or {}).get('runs')) or []
                    t = ''.join(x.get('text', '') for x in runs).strip()
                    if t:
                        texts.append(t)
                thumbs = (((item.get('thumbnail') or {}).get('musicThumbnailRenderer') or {}).get('thumbnail') or {}).get('thumbnails')
                image = thumbs[-1].get('url') if isinstance(thumbs, list) and thumbs else None
                if vid and texts:
                    out.append({'videoId': vid, 'title': texts[0], 'subtitle': texts[1] if len(texts) > 1 else None, 'thumbnail': image})
    return out


async def search_spotify(query):
    q = str(query or '').strip()
    if not q:
        return {'success': False, 'message': 'Search query must not be empty'}
    key = q.lower()
    cached = _cache.get(key)
    if cached and time.time() < cached['expires']:
        return cached['result']
    if key in _pending:
        return await _pending[key]

    async def run():
        try:
            data = await spotsaver_search(q)
            results = [{'id': v.get('id'), 'title': v.get('title') or 'Unknown Title', 'artists': [v['artist']] if v.get('artist') else [],
                        'durationMs': None, 'duration': v.get('duration') or '0:00', 'album': {'name': v.get('album'), 'cover': v.get('thumbnail')}}
                       for v in data['items']]
            results = [v for v in results if v['title'] and v['title'] != 'Unknown Title']
            result = {'success': bool(results), 'total': len(results), 'results': results, 'message': None if results else 'No results found'}
            _cache[key] = {'result': result, 'expires': time.time() + SEARCH_CACHE_TTL}
            return result
        except Exception as error:
            return {'success': False, 'message': str(error) or 'Failed to search for songs'}
        finally:
            _pending.pop(key, None)
    task = asyncio.ensure_future(run())
    _pending[key] = task
    return await task


async def download_by_track(title, artist, album, thumbnail):
    yt = await ytm_search(title + (' ' + artist if artist else ''))
    if not yt:
        raise RuntimeError('No results on YouTube Music')
    audio = await G.scraper.ytdl.ytdl('audio', f"https://www.youtube.com/watch?v={yt[0]['videoId']}")
    return {'metadata': {'name': title, 'artist': artist or None, 'album': album or None, 'cover': thumbnail or None},
            'links': {'mp3': audio['buffer'], 'cover': thumbnail or None}}


async def download_by_url(spotify_url):
    info = await spotsaver_info(spotify_url.split('?')[0])
    if not info['items']:
        return None
    t = info['items'][0]
    return await download_by_track(t['title'], t['artist'], t['album'], t['thumbnail'])


async def send_track_result(m, conn, chat, result):
    meta, links = result['metadata'], result['links']
    name = meta.get('name') or 'Unknown Title'
    cover = links.get('cover') or meta.get('cover')
    audio = links.get('mp3')
    if not audio:
        return await m.reply('- Failed to get song data.\n- Debug: mp3 link is empty, check the console log.')
    thumb = None
    if cover:
        try:
            thumb = await conn.resize((await http.get(cover)).content, 150, 150)
        except Exception:
            thumb = None
    order = {'orderId': '780642630945098', 'thumbnail': thumb, 'itemCount': 666, 'status': 1, 'surface': 1, 'message': name, 'orderTitle': name, 'sellerJid': '0@s.whatsapp.net',
             'token': 'AR6pyJ/fz5vRFxggGxURL7EA/vCtjKrhcJSNhHqX1iJh8A==', 'totalAmount1000': '0', 'totalCurrencyCode': 'IDR'}
    return await conn.sendFile(m.chat, audio, f'{name}.mp3', '', m, False, {
        'mimetype': 'audio/mpeg', 'asDocument': chat.get('useDocument'), 'quoted': {'key': {'remoteJid': '0@s.whatsapp.net'}, 'message': {'orderMessage': order}}})


async def handler(m, *, conn, usedPrefix, command, text):
    p, command, text = usedPrefix, command, text
    chat = G.db.data['chats'].get(m.chat) or {}
    if not text:
        return await m.reply(f'> *SEARCH -* [ {p + command} <Music_name> ]\n> *DOWNLOAD -* [ {p + command} <Spotify_link> ]')
    pick = re.match(r'^--pick\s+(\S+)$', text, re.I)
    if pick:
        try:
            track = json.loads(base64.b64decode(pick.group(1)).decode('utf-8'))
            if not isinstance(track, dict):
                raise ValueError('invalid payload')
        except Exception:
            return await m.reply('- Failed to get song data.\n- Debug: invalid payload')
        try:
            result = await download_by_track(track.get('t'), track.get('a'), track.get('al'), track.get('c'))
        except Exception as err:
            return await m.reply(f'- Failed to get song data.\n- Debug: {err}')
        return await send_track_result(m, conn, chat, result)
    if not URL_REGEX.search(text):
        res = await search_spotify(text)
        if not res.get('success') or not res.get('results'):
            return await m.reply('- *Error:* ' + str(res.get('message') or 'undefined'))
        rows = []
        for v in res['results']:
            payload = base64.b64encode(json.dumps({'t': v['title'], 'a': ', '.join(v['artists']), 'al': v['album'].get('name'), 'c': v['album'].get('cover')}).encode()).decode()
            rows.append({'header': v['title'], 'title': f"Artist: {', '.join(v['artists'])}  •  Duration: {v['duration']}",
                         'description': f"📁 {v['album'].get('name') or 'Unknown Album'}", 'id': f'{p}spotify --pick {payload}'})
        cover = res['results'][0]['album'].get('cover') or 'https://i.scdn.co/image/ab67616d0000b273'
        thumb = None
        try:
            thumb = await conn.resize((await http.get(cover)).content, 100, 100)
        except Exception:
            thumb = None
        payload = {'document': {'url': cover}, 'mimetype': 'image/webp', 'caption': ' ', 'fileName': 'SPOTIFY SEARCH', 'fileLength': '665666646645000',
                   'nativeFlow': [{'text': 'Select', 'sections': [{'title': 'Result', 'rows': rows}]}]}
        if thumb:
            payload['jpegThumbnail'] = thumb
        return await conn.sendButton(m.chat, payload, m)
    if not re.search(r'open\.spotify\.com', text, re.I):
        return await m.reply('- Only support Spotify link.')
    try:
        result = await download_by_url(text)
    except Exception as err:
        return await m.reply(f'- Failed to get song data.\n- Debug: {err}')
    if not result:
        return await m.reply('- Failed to get song data.\n- Debug: track not found on spotsaver.net')
    return await send_track_result(m, conn, chat, result)


tags = ['downloader']
help = ['spotify <name/link>']
command = ['spotify']
ai = {'risk': 'low', 'description': 'search/download spotify music'}
