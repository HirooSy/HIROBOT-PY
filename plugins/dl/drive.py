import re
from urllib.parse import urlparse

from lib.compat import http


UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'


def extract_drive_id(url):
    m = re.search(r'/d/([\w-]+)', url)
    if m:
        return m.group(1), 'drive'
    m = re.search(r'[?&]id=([\w-]+)', url)
    if m:
        return m.group(1), 'drive'
    m = re.search(r'/videos/d/([\w-]+)', url)
    if m:
        return m.group(1), 'docs_video'
    return None


def is_gdrive_url(text):
    try:
        return (urlparse(text.strip()).hostname or '') in ('drive.google.com', 'docs.google.com')
    except Exception:
        return False


async def gdrive(url):
    ex = extract_drive_id(url)
    if not ex:
        return {'status': False, 'error': 'Invalid Google Drive URL.'}
    did, kind = ex
    if kind == 'docs_video':
        return {'status': False, 'error': 'Video yang diupload ke Google Docs/Slides tidak dapat didownload — Google memerlukan login untuk mengaksesnya. Coba minta pengirim upload ulang ke Google Drive biasa.'}
    try:
        download_url = f'https://drive.usercontent.google.com/download?id={did}&export=download&confirm=t'
        res = await http.request('GET', download_url, headers={'user-agent': UA}, timeout=10, max_bytes=1)
        if res.status != 200:
            raise RuntimeError(f'Server returned {res.status}')
        disp = res.headers.get('content-disposition', '')
        nm = re.search(r'filename="?([^"]+)"?', disp)
        file_name = nm.group(1) if nm else f'file_{did}'
        mimetype = res.headers.get('content-type') or 'application/octet-stream'
        size = int(res.headers.get('content-length') or 0)
        return {'status': True, 'fileName': file_name, 'fileSize': f'{size / 1048576:.2f} MB' if size else 'Unknown', 'mimetype': mimetype,
                'extension': file_name.split('.')[-1].lower(), 'downloadUrl': download_url}
    except Exception as err:
        return {'status': False, 'error': str(err)}


async def handler(m, *, conn, args, usedPrefix, command):
    if not args:
        return await m.reply(f"Mana URL-nya?\n{usedPrefix + command} https://drive.google.com/...")
    url = args[0].strip()
    if not is_gdrive_url(url):
        return await m.reply('> URL tidak valid, pastikan dari drive.google.com atau docs.google.com')
    await m.react('⬇️')
    r = await gdrive(url)
    if not r['status']:
        return await m.reply(f"> *Gagal:* {r['error']}")
    info = f"📁 *{r['fileName']}*\n📦 Size: {r['fileSize']}\n🗂️ Type: {r['mimetype']}"
    mime, dl = r['mimetype'] or '', r['downloadUrl']
    try:
        if mime.startswith('video/'):
            await conn.sendMessage(m.chat, {'video': {'url': dl}, 'caption': info, 'mimetype': mime}, {'quoted': m})
        elif mime.startswith('image/'):
            await conn.sendMessage(m.chat, {'image': {'url': dl}, 'caption': info}, {'quoted': m})
        elif mime.startswith('audio/'):
            await conn.sendMessage(m.chat, {'audio': {'url': dl}, 'mimetype': mime, 'ptt': False}, {'quoted': m})
        else:
            await conn.sendMessage(m.chat, {'document': {'url': dl}, 'mimetype': mime or 'application/octet-stream', 'fileName': r['fileName'], 'caption': info}, {'quoted': m})
        await m.react('✅')
    except Exception as err:
        await m.reply(f'> *Gagal kirim file:* {err}')


help = [v + ' <url>' for v in ('gdrive', 'gd')]
tags = ['downloader']
command = re.compile(r'^(gdrive|gd)$', re.I)
limit = True
ai = {'risk': 'low', 'description': 'download drive file'}
