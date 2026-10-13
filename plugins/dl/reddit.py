import base64
import os
import re
import sys
from http.cookiejar import CookieJar
from urllib.parse import quote, unquote

from lib.compat import http
from lib.compat.html import load
from lib.utils.handler import G


UA = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
BASE = 'https://rapidsave.com'
ENC_SAFE = "-_.!~*'()"


def absolute(href):
    if not href:
        return None
    if href.startswith('http'):
        return href
    return BASE + ('' if href.startswith('/') else '/') + href


def decode_dlink(href):
    try:
        mt = re.search(r'/d/([^?#]+)', href)
        if not mt:
            return None
        raw = unquote(mt.group(1))
        raw += '=' * (-len(raw) % 4)
        url = base64.b64decode(raw).decode('utf-8')
        return url if re.match(r'^https?://', url) else None
    except Exception:
        return None


def real_url(href):
    if not href:
        return None
    if re.match(r'^https?://', href, re.I):
        return href
    return decode_dlink(href) or absolute(href)


async def resolve_url(url):
    if re.search(r'i\.redd\.it', url, re.I):
        return url
    if not re.search(r'redd\.it|/s/[A-Za-z0-9]+', url, re.I):
        return url
    try:
        res = await http.get(url, headers={'User-Agent': UA}, timeout=15, validate_status=lambda s: 200 <= s < 400)
        return res.url if 'comments/' in res.url else url
    except Exception:
        return url


async def prime_cache(post_id, cookie):
    try:
        info = (await http.get(f'https://api.reddit.com/api/info.json?id=t3_{post_id}', headers={'User-Agent': UA}, timeout=15)).json()
        await http.post(f'{BASE}/save-cache.php', json={'postId': post_id, 'jsonData': info},
                        headers={'User-Agent': UA, 'Referer': BASE + '/', 'Origin': BASE, **({'Cookie': cookie} if cookie else {})}, timeout=15)
        return True
    except Exception:
        return False


async def fetch_page(url):
    jar = CookieJar()
    res = await http.get(url, headers={'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 'Referer': BASE + '/'}, timeout=20, cookies=jar)
    cookie = '; '.join(f'{c.name}={c.value}' for c in jar)
    return res.text(), cookie


def parse(html):
    c = load(html)
    title = c('h2.text-center').first().text().strip()
    no_audio = bool(re.search(r'no audio/sound', c('center').text(), re.I))
    video = audio = gif = image = None
    for el in c('a.downloadbutton'):
        a = c(el)
        href = a.attr('href') or ''
        label = a.text().strip().lower()
        real = real_url(href)
        if not real:
            continue
        if 'video_url' in href or re.search(r'\.mp4(\?|$)', real, re.I) or 'video' in label or 'hd' in label:
            video = video or absolute(href)
        elif 'audio' in label or re.search('audio', href, re.I):
            audio = audio or absolute(href)
        elif re.search(r'\.gif(\?|$)', real, re.I) or 'gif' in label:
            gif = gif or real
        elif re.search(r'\.(jpe?g|png|webp)(\?|$)', real, re.I) or 'image' in label:
            image = image or real
        elif not image:
            image = real
    return {'title': title, 'noAudio': no_audio, 'videoHD': video, 'audioOnly': audio, 'gif': gif, 'image': image}


async def scrape(reddit_url):
    reddit_url = await resolve_url(reddit_url)
    mt = re.search(r'comments/([a-z0-9]+)', reddit_url, re.I)
    post_id = mt.group(1) if mt else None
    page = BASE + '/info?url=' + quote(reddit_url, safe=ENC_SAFE)
    html, cookie = await fetch_page(page)
    r = parse(html)
    if not r['videoHD'] and not r['gif'] and not r['image'] and post_id:
        await prime_cache(post_id, cookie)
        html, _ = await fetch_page(page)
        r = parse(html)
    if r['videoHD']:
        return {'type': 'video', 'title': r['title'], 'download': {'video': r['videoHD'], 'audio': None if r['noAudio'] else r['audioOnly']}}
    if r['gif']:
        return {'type': 'gif', 'title': r['title'], 'download': {'gif': r['gif']}}
    if r['image']:
        return {'type': 'image', 'title': r['title'], 'download': {'image': r['image']}}
    raise RuntimeError('No download link found. Post might be removed or private.')


async def handler(m, *, conn, text):
    xs = G.scraper.x
    if not text:
        return await m.reply("Where's the URL?")
    if not re.search(r'reddit\.com|redd\.it', text, re.I):
        return await m.reply('Invalid Reddit Url!')
    await m.react('⬇️')
    try:
        data = await scrape(text.strip())
        if not data or not data.get('download'):
            return await m.reply('Failed to fetch data. Link may be private or incorrect.')

        async def buf(u):
            return (await http.get(u, headers={'User-Agent': UA, 'Referer': BASE + '/'}, timeout=60)).content
        if data['type'] == 'image':
            return await conn.sendMessage(m.chat, {'image': await buf(data['download']['image']), 'caption': data.get('title') or ''}, {'quoted': m})
        if data['type'] == 'gif':
            path = await xs.gifToMp4(data['download']['gif'])
            try:
                with open(path, 'rb') as fh:
                    return await conn.sendMessage(m.chat, {'video': fh.read(), 'gifPlayback': True, 'caption': data.get('title') or ''}, {'quoted': m})
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        if data['type'] == 'video':
            path = await xs.fixVideoMetadata(data['download']['video'])
            try:
                with open(path, 'rb') as fh:
                    await conn.sendMessage(m.chat, {'video': fh.read(), 'caption': data.get('title') or ''}, {'quoted': m})
            finally:
                if os.path.exists(path):
                    os.unlink(path)
            if data['download'].get('audio'):
                await conn.sendMessage(m.chat, {'audio': await buf(data['download']['audio']), 'mimetype': 'audio/mp4', 'fileName': 'audio.mp4'}, {'quoted': m})
    except Exception as e:
        print(e, file=sys.stderr)
        return await m.reply('Error: ' + (str(e) or 'Unknown error'))


help = ['reddit', 'redditdl']
command = re.compile(r'^(reddit|redditdl)$', re.I)
tags = ['downloader']
limit = True
ai = {'risk': 'low', 'description': 'download reddit post'}
