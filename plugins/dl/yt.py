import re
import sys

from lib.compat import http
from lib.utils.handler import G, PluginError
from lib.utils.helper import spawn, to_simple_number


def is_link(text):
    return re.findall(r'https?://\S+', text or '', re.I) or None


def parse_int(value):
    mt = re.match(r'\s*[+-]?\d+', str(value))
    return int(mt.group(0)) if mt else float('nan')


def fake_contact(name, sender, remote='16504228206@s.whatsapp.net'):
    return {'key': {'fromMe': False, 'participant': sender, 'remoteJid': remote}, 'message': {'contactMessage': {
        'displayName': name, 'vcard': f'BEGIN:VCARD\nVERSION:3.0\nN:;a,;;;\nFN:{name}\nitem1.TEL;waid=6283143393763:6283143393763\nitem1.X-ABLabel:Ponsel\nEND:VCARD'}}}


async def handler(m, *, conn, usedPrefix, text, command):
    p, text, command = usedPrefix, text, command
    yts = G.scraper.ytsearch.search
    ytdl = G.scraper.ytdl.ytdl
    chat = G.db.data['chats'].get(m.chat) or {}
    if command == 'play':
        links = is_link(text)
        if not text and links is None:
            raise PluginError(f'- [PlayAudio] {p + command} music\n- [PlayVideo] {p + command} video_name --video')
        if links is None:
            found = await yts(text)
            inp = found['videos'][0]['url']
        else:
            inp = links[0]
        is_video = '--video' in text
        spawn(m.react('🎶'))
        data = await ytdl('video' if is_video else 'audio', inp)
        desc = f"`{data['title']}`\n\n- *Duration:* {data['duration']}\n- *Views:* {to_simple_number(data['views'])}\n- *Url:* {inp.replace('https://', '')}"
        filename = 'YouTube.mp4' if is_video else 'YouTube.mp3'
        thumb = None
        if data.get('thumbnail'):
            try:
                thumb = await conn.resize((await http.get(data['thumbnail'])).content, 150, 150)
            except Exception:
                thumb = None
        order = {'orderId': '780642630945098', 'thumbnail': thumb, 'itemCount': 666, 'status': 1, 'surface': 1, 'message': 'YOUTUBE DOWNLOADER', 'orderTitle': 'Channel.',
                 'sellerJid': '6283143393763@s.whatsapp.net', 'token': 'AR6pyJ/fz5vRFxggGxURL7EA/vCtjKrhcJSNhHqX1iJh8A==', 'totalAmount1000': '0', 'totalCurrencyCode': 'IDR'}
        spawn(conn.reply(m.chat, desc, {'key': {'remoteJid': '0@s.whatsapp.net'}, 'message': {'orderMessage': order}}))
        await conn.sendMessage(m.chat, {('video' if is_video else 'audio'): data['buffer'], 'mimetype': data['mime'],
                                        'asDocument': chat.get('useDocument'), 'fileName': filename}, {})
    if re.match(r'^yt(a|v|audio|video|mp4|mp3)$', command, re.I):
        links = is_link(text)
        if not text or not links:
            return await m.reply(f'*How To Use:* `{p + command} <your_link>`\n\nContoh: `{p + command} https://youtube.com/watch?v=xxxx`')
        is_audio = bool(re.match(r'yt(a|audio|mp3)', command))
        try:
            data = await ytdl('audio' if is_audio else 'video', links[0])
            await conn.sendMessage(m.chat, {('audio' if is_audio else 'video'): data['buffer'], 'mimetype': data['mime'], 'asDocument': chat.get('useDocument'),
                                            'fileName': 'YouTube.mp3' if is_audio else 'YouTube.mp4'}, {'quoted': m})
        except Exception as e:
            print(e, file=sys.stderr)
            return await m.reply('Failed to download, Scrape trouble')
    if re.match(r'^yt(s|search)$', command, re.I):
        if not text:
            raise PluginError('- *Example:* .yts <query>')
        s = await yts(text)
        data = [v for v in s['all'] if v['type'] == 'video'][:10]
        res = '\n\n'.join(f"> [ {i + 1} ] {v['title']}\n- *Author:* {v['author']['name']}\n- *Publish:* {v['ago']}\n- *Duration:* {v['timestamp']}\n"
                          f"- *Views:* {to_simple_number(parse_int(v['views']))}\n- {v['url'].replace('https://', '')}" for i, v in enumerate(data))
        await conn.reply(m.chat, res, fake_contact('YOUTUBE SEARCH', m.sender))


help = ['ytv <link>', 'yta <link>', 'yts <query>', 'play <query>']
tags = ['downloader']
command = re.compile(r'^(play|yt(v|video|a|audio|s|search|mp3|mp4))$', re.I)
limit = True
ai = {'risk': 'low', 'description': 'play/download music or video from youtube'}
