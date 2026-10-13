import asyncio
import base64
import html as htmllib
import os
import random
import re
import shutil
import tempfile
import time

from lib.compat import ffmpeg as ff, http
from lib.utils.handler import G, PluginError


SEARCH_STATE_TTL = 15 * 60 * 1000
PREVIEW_UA = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
PREVIEW_CONCURRENCY = 30
_search_state = {}
_dl_state = {}


def is_gif_url(u):
    return bool(re.search(r'\.gif(\?|$)', u or '', re.I))


def base36(n):
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = ''
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def js_parse_int(value):
    mt = re.match(r'\s*[+-]?\d+', str(value or ''))
    return int(mt.group(0)) if mt else None


def cleanup_search_state():
    now = time.time() * 1000
    for k in [k for k, v in _search_state.items() if now - v['timestamp'] > SEARCH_STATE_TTL]:
        del _search_state[k]


def small_thumb(u):
    return re.sub(r'i\.pinimg\.com/(474x|736x|originals|orig)/', 'i.pinimg.com/236x/', u, flags=re.I) if re.search(r'i\.pinimg\.com/(474x|736x|originals|orig)/', u, re.I) else u


async def fetch_to_file(url, path):
    res = await http.get(url, headers={'User-Agent': PREVIEW_UA, 'accept': 'image/*,*/*', 'referer': 'https://id.pinterest.com/'}, timeout=10)
    if not res.content:
        raise RuntimeError('empty response')
    with open(path, 'wb') as fh:
        fh.write(res.content)
    return res.headers.get('content-type', '').split(';')[0].strip()


async def first_frame(inp, out):
    args = []
    if re.match(r'^https?://', inp, re.I):
        args += ['-user_agent', PREVIEW_UA, '-headers', 'referer: https://id.pinterest.com/\r\norigin: https://id.pinterest.com/\r\n']
    args += ['-i', inp, '-frames:v', '1', '-vf', 'scale=320:-2', '-q:v', '5', out]
    await ff.ffmpeg(args, timeout=30)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError('empty frame')


async def save_preview(item, d, idx):
    out = os.path.join(d, f'pin_{idx}.jpg')
    still = item['thumbUrl'] if item.get('thumbUrl') and not is_gif_url(item['thumbUrl']) else (
        item['rawUrl'] if item['type'] == 'image' and not is_gif_url(item['rawUrl']) else None)
    if still:
        small = small_thumb(still)
        try:
            await fetch_to_file(small, out)
        except Exception:
            if small == still:
                raise
            await fetch_to_file(still, out)
        return out
    if item['type'] == 'gif':
        guess = re.sub(r'\.gif(\?.*)?$', '.jpg', small_thumb(item.get('thumbUrl') or item['rawUrl']), flags=re.I)
        try:
            kind = await fetch_to_file(guess, out)
            if re.match(r'^image/jpe?g$', kind, re.I):
                return out
        except Exception:
            pass
    if item['type'] in ('gif', 'video'):
        await first_frame(item['rawUrl'], out)
        return out
    raise RuntimeError('no preview source')


async def download_previews(items, d):
    sem = asyncio.Semaphore(PREVIEW_CONCURRENCY)

    async def one(i, item):
        async with sem:
            try:
                item['localPath'] = await save_preview(item, d, i)
            except Exception:
                item['localPath'] = None
    await asyncio.gather(*[one(i, it) for i, it in enumerate(items)])


def build_preview_html(items):
    labels = {'gif': 'GIF', 'video': 'VIDEO'}
    cells = ''
    for i, item in enumerate(items):
        with open(item['localPath'], 'rb') as fh:
            src = 'data:image/jpeg;base64,' + base64.b64encode(fh.read()).decode()
        tag = f'<span class="tag">{labels[item["type"]]}</span>' if item['type'] in labels else ''
        cells += f'<div class="cell"><img src="{src}" alt="{i + 1}"><span class="num">{i + 1}</span>{tag}</div>'
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>'
            ':root{color-scheme:light dark}*{box-sizing:border-box;margin:0;padding:0}body{padding:8px;font-family:sans-serif;background:transparent}'
            '.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.cell{position:relative;aspect-ratio:1/1;border-radius:10px;overflow:hidden;background:rgba(128,128,128,.2)}'
            '.cell img{width:100%;height:100%;object-fit:cover;display:block}.num{position:absolute;top:6px;left:6px;min-width:24px;padding:2px 7px;border-radius:12px;background:rgba(0,0,0,.65);color:#fff;font-size:12px;font-weight:700;text-align:center}'
            '.tag{position:absolute;bottom:6px;right:6px;padding:2px 6px;border-radius:6px;background:rgba(230,0,35,.9);color:#fff;font-size:10px;font-weight:700;letter-spacing:.5px}'
            f'</style></head><body><div class="grid">{cells}</div></body></html>')


async def download_item(conn, chat_id, quoted, item, caption):
    pin = G.scraper.pinterest
    if item['type'] == 'image':
        return await conn.sendFile(chat_id, item['rawUrl'], 'pinterest.jpg', caption, quoted)
    if item['type'] == 'gif':
        path = await pin.gifToMp4(item['rawUrl'])
        try:
            with open(path, 'rb') as fh:
                await conn.sendFile(chat_id, fh.read(), 'pinterest.mp4', caption, quoted)
        finally:
            os.unlink(path)
        return
    if item['type'] == 'video':
        hls = await pin.getPinterestHLS(item['rawUrl'])
        best = hls['qualities'][-1] if hls and hls.get('qualities') else None
        if not best:
            raise RuntimeError('No video quality available.')
        output = os.path.join(tempfile.gettempdir(), f'pin_dl_{int(time.time() * 1000)}.mp4')
        try:
            await pin.mergeVideoAudio(best['url'], hls.get('audio'), output)
            with open(output, 'rb') as fh:
                await conn.sendFile(chat_id, fh.read(), 'pinterest.mp4', caption, quoted)
        finally:
            if os.path.exists(output):
                os.unlink(output)
        return
    raise RuntimeError('Unknown item type.')


async def handler(m, *, conn, args, command):
    sc = G.scraper.pinterest
    if not args:
        raise PluginError('Usage:\n\n*Download by URL:*\n.pin https://pinterest.com/pin/xxx\n.pin https://pin.it/xxx\n\n*Search:*\n.pin <keyword>\n.pin video <keyword>\n.pin image <keyword>\n.pin gif <keyword>')
    first = args[0]
    if first == 'get':
        token = args[1] if len(args) > 1 else None
        index = int(args[2]) if len(args) > 2 and args[2].isdigit() else -1
        state = _search_state.get(token)
        if not state:
            raise PluginError('This search result has expired, please search again.')
        if index < 0 or index >= len(state['items']):
            raise PluginError('Item not found.')
        item = state['items'][index]
        await m.react('⬇️')
        caption = f"Pinterest — {item['title']}" if item.get('title') else f"Pinterest — {state['query']}"
        try:
            await download_item(conn, m.chat, m, item, caption)
            await m.react('✅')
        except Exception as error:
            await m.react('❌')
            raise PluginError(f'Failed to download: {error}')
        return
    if sc.isPinterestUrl(first):
        await m.reply('Fetching pin info...')
        dr = await sc.pinterest.download(first)
        if not dr['status']:
            raise PluginError(dr['result']['message'])
        result = dr['result']
        media = result['media_urls'][0]
        title, desc = result.get('title') or '', result.get('description') or ''
        up = result['uploader']
        creator = up.get('full_name') or up.get('username') or ''
        saves = sc.formatNumber(result['statistics'].get('saves') or 0)
        info = f"Pinterest Pin\n{f'- Title: {title}' + chr(10) if title else ''}{f'- Description: {desc}' + chr(10) if desc else ''}- Creator: {creator}\n- Saves: {saves}"
        if media['type'] == 'gif' or '.gif' in (media.get('url') or '').lower():
            try:
                path = await sc.gifToMp4(media['url'])
                with open(path, 'rb') as fh:
                    await conn.sendFile(m.chat, fh.read(), 'converted.mp4', info, m)
                os.unlink(path)
                return
            except Exception as error:
                raise PluginError(f'Failed to convert GIF to video: {error}')
        if media['type'] == 'image':
            return await conn.sendFile(m.chat, media['url'], 'pinterest.jpg', info, m)
        if media['type'] == 'video':
            hls = await sc.getPinterestHLS(media['url'])
            if not hls or not hls['qualities']:
                raise PluginError('Failed to get video quality.')
            qlist = '\n'.join(f"{i + 1}. {q['resolution']}" for i, q in enumerate(hls['qualities']))
            caption = (f"Pinterest Video\n{f'- Title: {title}' + chr(10) if title else ''}{f'- Description: {desc}' + chr(10) if desc else ''}"
                       f"- Creator: {creator}\n- Saves: {saves}\n\nChoose Resolution:\n{qlist}")
            sent = await conn.reply(m.chat, caption, m)
            _dl_state[m.sender] = {'hls': hls, 'title': title, 'desc': desc, 'creator': creator, 'saves': saves, 'messageId': sent['key']['id'],
                                   'timestamp': time.time() * 1000}
        return
    mode_keys = ['vid', 'video', 'gif', 'gifs', 'img', 'image', 'images']
    mode = sc.detectMode(args)
    query_args = args[1:] if args[0].lower() in mode_keys else args
    query = ' '.join(query_args)
    if not query:
        raise PluginError('Please enter a search keyword!')
    labels = {'all': 'All', 'video': 'Video', 'gif': 'GIF', 'image': 'Image'}
    sr = await sc.pinterest.search(query, 50)
    if not sr['status']:
        raise PluginError(f'No results found for: *{query}*')
    pins = sr['result']['pins']

    def keep(pin):
        medias = sc.extractMediaFromPin(pin)
        if not medias:
            return False
        if mode == 'all':
            return True
        if mode == 'gif':
            return any(x['type'] == 'gif' or x.get('isGif') is True for x in medias)
        return any(x['type'] == mode for x in medias)
    filtered = [p for p in pins if keep(p)]
    if not filtered:
        raise PluginError(f'No {mode} results found for: *{query}*')
    total = len(filtered)
    random.shuffle(filtered)
    items = []
    for pin in filtered[:30]:
        for media in sc.extractMediaFromPin(pin) or []:
            base = {'rawUrl': media['url'], 'thumbUrl': media.get('thumbnailUrl'), 'pinUrl': pin['pin_url'], 'title': pin.get('title')}
            if (media['type'] == 'gif' or media.get('isGif') is True) and mode in ('all', 'gif'):
                items.append({'type': 'gif', **base})
            elif media['type'] == 'image' and mode != 'video':
                items.append({'type': 'image', **base})
            elif media['type'] == 'video' and mode != 'image':
                items.append({'type': 'video', **base})
    if not items:
        raise PluginError(f'No {mode} results found for: *{query}*')
    await m.react('⏳')
    d = tempfile.mkdtemp(prefix='pin_')
    try:
        await download_previews(items, d)
        ready = [i for i in items if i.get('localPath')]
        if not ready:
            raise PluginError(f'Failed to load previews for: *{query}*')
        cleanup_search_state()
        token = f"{(m.sender or '').split('@')[0]}_{base36(int(time.time() * 1000))}"
        _search_state[token] = {'items': ready, 'query': query, 'mode': mode, 'timestamp': time.time() * 1000}
        labels2 = {'image': 'Image', 'gif': 'GIF', 'video': 'Video'}
        rows = [{'header': f"{i + 1}. {labels2.get(it['type'], 'Media')}", 'title': (it.get('title') or query)[:60], 'description': '',
                 'id': f'.{command} get {token} {i}'} for i, it in enumerate(ready)]
        caption = f"- *Query:* {query}\n- Mode: {labels.get(mode, 'All')}\n- Result: {total}\n- Showing: {len(ready)}\n\nChoose the number you want to download."
        await conn.aiRich().setTitle(f'Pinterest — {query}').addHtml(build_preview_html(ready)).send(m.chat, {'quoted': m})
        await conn.sendButton(m.chat, {'text': caption, 'footer': 'Pinterest', 'nativeFlow': [{'text': 'Select', 'sections': [{'rows': rows}]}]}, m)
        await m.react('✅')
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def before(m, *, conn):
    sc = G.scraper.pinterest
    if not m.quoted or not m.quoted.id:
        return
    state = _dl_state.get(m.sender)
    if not state or time.time() * 1000 - state['timestamp'] > 300000:
        return
    if state['messageId'] != m.quoted.id:
        return
    choice = js_parse_int(m.text)
    if choice is None:
        return
    if choice < 1 or choice > len(state['hls']['qualities']):
        return
    try:
        selected = state['hls']['qualities'][choice - 1]
        title, desc = state['title'], state['desc']
        info = f"{f'- Title: {title}' + chr(10) if title else ''}{f'- Description: {desc}' + chr(10) if desc else ''}- Creator: {state['creator']}\n- Saves: {state['saves']}\n- Resolution: {selected['resolution']}"
        await m.react('⬇️')
        output = os.path.join(tempfile.gettempdir(), f'pin_{int(time.time() * 1000)}.mp4')
        await sc.mergeVideoAudio(selected['url'], state['hls'].get('audio'), output)
        with open(output, 'rb') as fh:
            await conn.sendFile(m.chat, fh.read(), 'pinterest.mp4', info, m)
        os.unlink(output)
        await m.reply('Video downloaded successfully!')
    except Exception as err:
        await m.reply(f'Failed: {err}')
    _dl_state.pop(m.sender, None)
    return True


help = [v + ' <url|keyword>' for v in ('pinterest',)]
tags = ['downloader']
command = re.compile(r'^(pint(erest)?)$', re.I)
limit = True
ai = {'risk': 'low', 'description': 'search/download from pinterest'}
