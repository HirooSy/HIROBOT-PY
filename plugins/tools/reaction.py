import json

from lib.compat import http


BASE_URL = 'https://reaction-whatsapp.edgeone.dev'
API_KEY = '9J88DPLJ'


def _body_json(res):
    text = res.content.decode('utf-8', errors='replace')
    if not text:
        return ''
    try:
        return json.loads(text)
    except Exception:
        return text


async def handler(m, *, args):
    link, emoji = (args[0] if len(args) > 0 else None), (args[1] if len(args) > 1 else None)
    if not link or not emoji:
        return await m.reply('Penggunaan: .react <link> <emoji>')
    try:
        await m.reply('Sedang memproses reaksi...')
        res = await http.post(f'{BASE_URL}/react', json={'link': link, 'emoji': emoji}, timeout=60,
                              headers={'Authorization': f'Bearer {API_KEY}', 'Content-Type': 'application/json'})
        if res.status == 200:
            await m.reply(f'Berhasil! Reaksi {emoji} telah dikirim ke {link}.')
        else:
            await m.reply(f'Gagal: {json.dumps(_body_json(res), separators=(",", ":"), ensure_ascii=False)}')
    except Exception as error:
        await m.reply(f'Error: {getattr(error, "message", error)}')


command = ['react']
tags = ['tools']
help = ['react <link> <emoji>']
