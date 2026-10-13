import json
import re
import sys

from lib.compat import http


HEADERS = {
    'accept': '*/*',
    'accept-language': 'id-ID',
    'content-type': 'application/json',
    'origin': 'https://corefreetools.originality.ai',
    'referer': 'https://corefreetools.originality.ai/',
    'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36',
}
ENDPOINT = 'https://api.originality.ai/api/v2-tools/free-tools/ai-scan'


def error_text(e):
    resp = getattr(e, 'response', None)
    if resp is None:
        return str(getattr(e, 'message', None) or e)
    data = getattr(resp, 'data', None)
    if isinstance(data, (bytes, bytearray)):
        data = data.decode('utf-8', errors='replace')
    return json.dumps(data, ensure_ascii=False)


async def scrape_originality(query):
    res = await http.post(ENDPOINT, json={'content': query}, headers=HEADERS)
    return res.json()


def format_result(data):
    ai = data.get('ai') if isinstance(data, dict) else None
    pct = f'{ai * 100:.2f}' if isinstance(ai, (int, float)) and not isinstance(ai, bool) else 'NaN'
    return f'*AI:* {pct}%'


async def handler(m, *, conn, text, usedPrefix, command):
    if not text:
        return await conn.reply(m.chat, f"Masukkan teks.\nContoh: {usedPrefix + command} teks kamu", m)
    try:
        data = await scrape_originality(text.strip())
        return await m.reply(format_result(data))
    except Exception as e:
        print('[aicheck]', e, file=sys.stderr)
        return await conn.reply(m.chat, f'Error: {error_text(e)}', m)


help = command = ['aicheck']
tags = ['tools']
limit = 0
