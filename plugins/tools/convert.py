import re

from lib.compat import http
from lib.utils import converter
from lib.utils.handler import G, PluginError
from lib.utils.helper import oc


async def handler(m, *, conn, command, usedPrefix):
    command, p = command, usedPrefix
    ezgif = G.scraper.ezgif
    if command == 'toimg':
        usage = f'Reply sticker with command *{p + command}*'
        if not m.quoted:
            raise PluginError(usage)
        if not re.search('sticker', m.quoted.mediaType or ''):
            raise PluginError(usage)
        media = await m.quoted.download()
        try:
            url = await ezgif.webp2png(media)
        except Exception:
            url = None
        if not url:
            raise PluginError('Gagal mengonversi stiker ke gambar')
        out = (await http.get(url, raise_for_status=False)).content
        await conn.sendFile(m.chat, out, 'out.png', None, m)
    if command == 'tovideo':
        usage = f'Balas stiker/audio yang ingin diubah menjadi video dengan perintah {p + command}'
        if not m.quoted:
            raise PluginError(usage)
        mime = m.quoted.mimetype or ''
        if not re.search('webp|audio', mime):
            raise PluginError(usage)
        media = await m.quoted.download()
        out = b''
        if 'webp' in mime:
            try:
                url = await ezgif.webp2mp4(media)
            except Exception:
                url = None
            if not url:
                raise PluginError('Gagal mengonversi stiker ke video')
            out = (await http.get(url, raise_for_status=False)).content
        else:
            out = await converter.ffmpeg_bytes(media, ['-filter_complex', 'color', '-pix_fmt', 'yuv420p', '-crf', '51', '-c:a', 'copy', '-shortest'], 'mp3', 'mp4')
        await conn.sendFile(m.chat, out, 'out.mp4', None, m)
    if re.match(r'^to(vn|ptt|voicenote)$', command, re.I):
        q = m.quoted if m.quoted else m
        mime = (m.quoted.mimetype if m.quoted else oc(m.msg, 'mimetype')) or ''
        if not re.search('video|audio', mime):
            raise PluginError(f'reply video/audio you want to convert to voice note/vn with caption *{p + command}*')
        media = await q.download()
        if not media:
            raise PluginError("Can't download media")
        audio = await converter.to_ptt(media, 'mp4')
        if not audio['data']:
            raise PluginError("Can't convert media to audio")
        await conn.sendFile(m.chat, audio['data'], 'audio.mp3', '', m, True, {'mimetype': 'audio/mp4'})
    if re.match(r'^to(mp3|a(udio)?)$', command, re.I):
        q = m.quoted if m.quoted else m
        mime = (m.quoted.mimetype if m.quoted else oc(m.msg, 'mimetype')) or ''
        if not re.search('video|audio', mime):
            raise PluginError(f'reply video/voice note you want to convert to audio/mp3 with caption *{p + command}*')
        media = await q.download()
        if not media:
            raise PluginError("Can't download media")
        audio = await converter.to_audio(media, 'mp4')
        if not audio['data']:
            raise PluginError("Can't convert media to audio")
        await conn.sendFile(m.chat, audio['data'], 'audio.mp3', '', m, None, {'mimetype': 'audio/mp4'})


tags = ['tools']
help = ['toimg', 'tovideo', 'tovn', 'tovoicenote', 'tomp3']
command = re.compile(r'^(toimg|tovideo|to(vn|ptt|voicenote)|to(mp3|a(udio)?))$', re.I)
ai = {'risk': 'low', 'description': 'convert sticker to image/video, convert video to audio/mp3, convert audio/video to voice note'}
