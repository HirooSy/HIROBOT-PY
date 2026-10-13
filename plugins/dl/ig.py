import os
import re
import sys
import time
from urllib.parse import urlparse

from lib.compat import ffmpeg as ff
from lib.utils.handler import G


STATE_TTL = 5 * 60 * 1000
_state = {}


def set_state(sender, data):
    _state[sender] = {**data, 'timestamp': time.time() * 1000}


def get_state(sender):
    s = _state.get(sender)
    if not s:
        return None
    if time.time() * 1000 - s['timestamp'] > STATE_TTL:
        _state.pop(sender, None)
        return None
    return s


def is_ig_url(text):
    try:
        return (urlparse(text.strip()).hostname or '') in ('instagram.com', 'www.instagram.com')
    except Exception:
        return False


def clean_files(*files):
    for f in files:
        try:
            if f and os.path.exists(f):
                os.unlink(f)
        except OSError:
            pass


async def send_single_video(conn, m, video_url, audios):
    if audios:
        out = ff.tmp_path('mp4', 'ig_')
        try:
            await ff.ffmpeg(['-i', video_url, '-i', audios[0]['url'], '-c:v', 'copy', '-c:a', 'aac', '-shortest', out])
            await conn.sendMessage(m.chat, {'video': {'url': out}, 'mimetype': 'video/mp4'}, {'quoted': m})
        finally:
            clean_files(out)
    else:
        await conn.sendMessage(m.chat, {'video': {'url': video_url}, 'mimetype': 'video/mp4'}, {'quoted': m})


def build_menu(items):
    total = len(items)
    lines = ['━━━━━━━━━━━━━━━━━━━━', '*Select the item you want to download:*']
    for i, item in enumerate(items):
        icon = '▶︎' if item['type'] == 'video' else '▢'
        lines.append(f"{i + 1}. Slide {item['index']} ({icon})")
    lines += [f'{total + 1}. Download All', '━━━━━━━━━━━━━━━━━━━━',
              '> *Option valid for 5 minutes*, type the number directly or reply to this message']
    return '\n'.join(lines)


async def send_item(conn, m, item):
    if item['type'] == 'video':
        v0 = (item.get('videos') or [None])[0]
        if not v0 or not v0.get('url'):
            return False
        await conn.sendMessage(m.chat, {'video': {'url': v0['url']}, 'mimetype': 'video/mp4'}, {'quoted': m})
        return True
    i0 = (item.get('images') or [None])[0]
    if not i0 or not i0.get('url'):
        return False
    await conn.sendMessage(m.chat, {'image': {'url': i0['url']}}, {'quoted': m})
    return True


async def process_selection(conn, m, state, choice):
    if state['type'] != 'carousel':
        return False
    items = state['result']['media']['items']
    max_choice = len(items) + 1
    if choice < 1 or choice > max_choice:
        await m.reply(f'> *Invalid choice*, enter number 1—{max_choice}.')
        return False
    if choice == max_choice:
        sent = False
        try:
            for item in items:
                if await send_item(conn, m, item):
                    sent = True
            if not sent:
                await m.reply('No valid items found.')
                return False
            await m.react('✅')
            return True
        except Exception as err:
            print('[IG Album Error]', err, file=sys.stderr)
            await m.reply(f'Failed: {err}')
            return False
    item = items[choice - 1]
    if item['type'] == 'video':
        if not ((item.get('videos') or [None])[0] or {}).get('url'):
            await m.reply('Video URL not found.')
            return False
        try:
            await send_item(conn, m, item)
            await m.react('✅')
            return True
        except Exception as err:
            print('[IG Video Error]', err, file=sys.stderr)
            await m.reply(f'Failed: {err}')
            return False
    if not ((item.get('images') or [None])[0] or {}).get('url'):
        await m.reply('Image URL not found.')
        return False
    try:
        await send_item(conn, m, item)
        await m.react('✅')
        return True
    except Exception as err:
        print('[IG Image Error]', err, file=sys.stderr)
        await m.reply(f'Failed: {err}')
        return False


async def handler(m, *, conn, args, usedPrefix, command):
    if not args:
        return await m.reply(f"Where's the URL?\n{usedPrefix + command} https://instagram.com/....")
    url = args[0].strip()
    if not is_ig_url(url):
        return await m.reply('> Invalid URL, make sure the URL is from Instagram.com')
    await m.react('⬇️')
    try:
        result = await G.scraper.ig.instagram(url)
        if not result['status']:
            return await m.reply(f"Failed: {result['error']}")
        meta, media = result['result']['metadata'], result['result']['media']
        if meta['type'] == 'single_image':
            best = (media.get('images') or [None])[0]
            if not best or not best.get('url'):
                return await m.reply('No image found.')
            await conn.sendMessage(m.chat, {'image': {'url': best['url']}}, {'quoted': m})
            return await m.react('✅')
        if meta['type'] in ('video', 'reels'):
            best = (media.get('videos') or [None])[0]
            if not best or not best.get('url'):
                return await m.reply('No video found.')
            await send_single_video(conn, m, best['url'], media.get('audios') or [])
            return await m.react('✅')
        if meta['type'] == 'carousel':
            items = media.get('items')
            if not items:
                return await m.reply('No items in carousel.')
            await conn.reply(m.chat, build_menu(items), m)
            set_state(m.sender, {'type': 'carousel', 'result': result['result']})
            return
        await m.reply('Content type not supported.')
    except Exception as err:
        print('[IG Handler Error]', err, file=sys.stderr)
        await m.reply(f'Error: {err}')


async def before(m, *, conn):
    text = (m.text or '').strip()
    if not re.fullmatch(r'[0-9]+', text):
        return
    state = get_state(m.sender)
    if not state:
        return
    try:
        if await process_selection(conn, m, state, int(text)):
            _state.pop(m.sender, None)
    except Exception as err:
        print('[IG Before Error]', err, file=sys.stderr)
        await m.reply(f'Error: {err}')
        _state.pop(m.sender, None)
    return True


help = [v + ' <url>' for v in ('ig', 'instagram', 'igdl')]
tags = ['downloader']
command = re.compile(r'^(ig|instagram|igdl)$', re.I)
limit = True
ai = {'risk': 'low', 'description': 'download post/reel from Instagram'}
