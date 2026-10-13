import re
import sys
from urllib.parse import urlparse

from lib.utils.handler import G


def is_fb_url(text):
    try:
        host = urlparse(text.strip()).hostname or ''
        return bool(re.search(r'(^|\.)facebook\.com$|(^|\.)fb\.watch$|(^|\.)fb\.com$', host, re.I))
    except Exception:
        return False


async def handler(m, *, conn, args, usedPrefix, command):
    if not args:
        return await m.reply(f"Where's the URL?\n{usedPrefix + command} https://facebook.com/....")
    url = args[0].strip()
    if not is_fb_url(url):
        return await m.reply('> Invalid URL, make sure the URL is from Facebook.com')
    await m.react('⬇️')
    try:
        result = await G.scraper.facebook.facebook(url)
        if not result['status']:
            return await m.reply(f"Failed: {result['error']}")
        videos = result['result']['media'].get('videos') or []
        if not videos:
            return await m.reply('No video found.')
        best = next((v for v in videos if not v.get('needsRender')), videos[0])
        if not best.get('url'):
            return await m.reply('Video URL not found.')
        await conn.sendMessage(m.chat, {'video': {'url': best['url']}, 'mimetype': 'video/mp4', 'caption': ''}, {'quoted': m})
        await m.react('✅')
    except Exception as err:
        print('[FB Handler Error]', err, file=sys.stderr)
        await m.reply(f'Error: {err}')


help = [v + ' <url>' for v in ('fb', 'facebook', 'fbdl')]
tags = ['downloader']
command = re.compile(r'^(fb|facebook|fbdl)$', re.I)
limit = True
ai = {'risk': 'low', 'description': 'download video from Facebook'}
