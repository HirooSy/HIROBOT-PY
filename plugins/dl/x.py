import asyncio
import os
import re
import sys

from lib.utils.handler import G


def js_str(v):
    if v is None:
        return 'undefined'
    return str(v)


def _timeout(conn, sender, secs):
    loop = asyncio.get_event_loop()
    return loop.call_later(secs, lambda: conn.twitter.pop(sender, None))


async def _send_video_file(conn, m, path, extra):
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
        await conn.sendMessage(m.chat, {'video': data, **extra}, {'quoted': m})
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def handler(m, *, conn, text, usedPrefix, command):
    xs = G.scraper.x
    if not text:
        return await conn.reply(m.chat, f"Inout Twitter/X URL!\nExample: {usedPrefix + command} https://x.com/username/status/123456789", m)
    txt = xs.isLink(text)
    if not txt:
        return await conn.reply(m.chat, 'Invalid URL!', m)
    inp = txt[0]
    conn.twitter = conn.twitter or {}
    try:
        data = await xs.twitter(inp)
        videos = list(data.get('videoUrls') or [])
        if data.get('type') == 'gif':
            videos.insert(0, {'type': 'GIF', 'quality': 'GIF format', 'link': [data['gif']]})
            if data.get('image'):
                videos.append({'type': 'JPG', 'quality': 'Image', 'link': [data['image']]})
        if not videos:
            return await conn.reply(m.chat, 'Content not found.', m)
        mp4 = [v for v in videos if v['type'] == 'MP4']
        jpg = [v for v in videos if v['type'] == 'JPG']
        caption = f"- *Caption :* \n{js_str(data.get('description'))}"
        if any(isinstance(v['link'], list) and len(v['link']) > 1 for v in jpg):
            for item in videos:
                for i in item['link']:
                    if item['type'] == 'MP3':
                        await conn.sendMessage(m.chat, {'audio': {'url': i}, 'mimetype': 'audio/mpeg'}, {'quoted': m})
                    elif item['type'] == 'JPG':
                        await conn.sendMessage(m.chat, {'image': {'url': i}, 'caption': caption}, {'quoted': m})
                    else:
                        await conn.sendMessage(m.chat, {'video': {'url': i}, 'caption': caption}, {'quoted': m})
            return
        if mp4 and data.get('type') != 'gif':
            for i in mp4[0]['link']:
                path = await xs.fixVideoMetadata(i)
                await _send_video_file(conn, m, path, {'caption': caption})
            return
        if data.get('type') == 'gif':
            entry = next((v for v in videos if v['type'] == 'GIF'), None)
            if entry:
                for i in entry['link']:
                    path = await xs.gifToMp4(i)
                    await _send_video_file(conn, m, path, {'gifPlayback': True, 'caption': caption})
                return
        menu = '\n'.join(f"*_{i + 1}. {item['type']} - {item['quality']}_*" for i, item in enumerate(videos))
        await conn.reply(m.chat, f'Pilih konten dengan mengetik nomor:\n{menu}', m)
        conn.twitter[m.sender] = {'url': inp, 'caption': data.get('description'), 'allLinks': [v['link'] for v in videos],
                                  'isGif': data.get('type') == 'gif', 'timeout': _timeout(conn, m.sender, 160)}
    except Exception as e:
        print(e, file=sys.stderr)
        await conn.reply(m.chat, f'Error: {e}', m)
        m.error = e


async def before(m, *, conn):
    xs = G.scraper.x
    conn.twitter = conn.twitter or {}
    if m.sender not in conn.twitter:
        return
    st = conn.twitter[m.sender]
    nums = re.findall(r'\d+', m.text or '')
    if not nums:
        return
    try:
        index = int(nums[0]) - 1
        links = st['allLinks']
        if index < 0 or index >= len(links):
            return await conn.reply(m.chat, 'Please select valid number!', m)
        selected_gif = index == 0 and st['isGif']
        for i in links[index]:
            if selected_gif:
                path = await xs.gifToMp4(i)
                await _send_video_file(conn, m, path, {'gifPlayback': True, 'caption': ''})
            else:
                if '.mp3' in i:
                    await conn.sendMessage(m.chat, {'audio': {'url': i}, 'mimetype': 'audio/mpeg'}, {'quoted': m})
                elif '.jpg' in i or '.jpeg' in i:
                    await conn.sendMessage(m.chat, {'image': {'url': i}, 'caption': ''}, {'quoted': m})
                else:
                    path = await xs.fixVideoMetadata(i)
                    await _send_video_file(conn, m, path, {'caption': ''})
    except Exception as e:
        print(e, file=sys.stderr)
        await conn.reply(m.chat, f'Error: {e}', m)
        m.error = e
    finally:
        try:
            st['timeout'].cancel()
        except Exception:
            pass
        conn.twitter.pop(m.sender, None)


help = [v + ' <url>' for v in ('twitter', 'x')]
tags = ['downloader']
command = ['twitter', 'x']
limit = 1
ai = {'risk': 'low', 'description': 'download twitter/x post'}
