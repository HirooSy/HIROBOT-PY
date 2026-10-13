import re

from lib.utils.handler import G, PluginError
from lib.utils.helper import spawn


async def handler(m, *, conn, args, command):
    tk = G.scraper.tiktok
    if command in ('tiktok', 'tt'):
        if not args:
            raise PluginError('> Where The Url?')
        link = tk.isLink(args[0])
        if not link:
            raise PluginError('> Invalid Url!')
        await m.react('⬇️')
        try:
            data = await tk.tiktok(link[0])
            title = data.get('title')
            desc = '‎' if not title else f'\n- `Description`: \n> {title}'
            caption = f"- `Author:` {data['author']['nickname']} ( @ {data['author']['unique_id']} ){desc}"
            if data.get('images'):
                await m.reply(caption)
                for img in data['images']:
                    spawn(conn.sendFile(m.chat, img, 'tiktok.png', None, None))
            else:
                video_url = data.get('play') or data.get('hdplay') or data.get('wmplay')
                if not video_url:
                    raise RuntimeError('Tidak ada URL video dari tikwm untuk link ini')
                boosted = await tk.tiktokBoostVolume(video_url)
                await conn.sendFile(m.chat, boosted, 'tiktok.mp4', caption, m)
            spawn(m.react('✅'))
        except Exception as e:
            m.error = e
            raise
    if command in ('tiktokaudio', 'ttaudio'):
        if not args:
            raise PluginError('> Where The Url?')
        link = tk.isLink(args[0])
        if not link:
            raise PluginError('> Invalid Url!')
        chat = G.db.data['chats'][m.chat]
        await m.react('⬇️')
        try:
            data = await tk.tiktok(link[0])
            await conn.sendFile(m.chat, data['music'], 'tiktok.mp3', None, m, False, {'mimetype': 'audio/mpeg', 'asDocument': chat.get('useDocument')})
            spawn(m.react('✅'))
        except Exception as e:
            m.error = e
            raise


help = [v + ' <url>' for v in ('tiktok', 'tiktokaudio')]
tags = ['downloader']
command = re.compile(r'^(tt|tiktok)(audio)?$', re.I)
limit = True
ai = {'risk': 'low', 'description': 'download tiktok post'}
