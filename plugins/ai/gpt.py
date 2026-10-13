import json
import os
import sys
import time

from lib.compat import http


HOTBOT_URL = 'https://www.hotbot.com/api/chat'
def hotbot_sessions(conn):
    sessions = conn.get('hotbot')
    if sessions is None:
        sessions = {}
        conn['hotbot'] = sessions
    return sessions


def temp_uuid():
    return f'yp7lPkXl-{int(time.time())}-{os.urandom(8).hex()}'


def parse_sse(raw):
    out = ''
    for line in raw.split('\n'):
        t = line.strip()
        if t.startswith('data: ') and '[DONE]' not in t:
            try:
                parsed = json.loads(t[6:])
                if parsed.get('content'):
                    out += parsed['content']
            except Exception:
                pass
    return out


def error_text(e):
    resp = getattr(e, 'response', None)
    if resp is None:
        return str(getattr(e, 'message', None) or e)
    data = getattr(resp, 'data', None)
    if isinstance(data, (bytes, bytearray)):
        data = data.decode('utf-8', errors='replace')
    return json.dumps(data, ensure_ascii=False)


async def ask(sender, text, conn):
    sessions = hotbot_sessions(conn)
    if sender not in sessions:
        sessions[sender] = {'tempUuid': temp_uuid(), 'messages': []}
    s = sessions[sender]
    s['messages'].append({'role': 'user', 'content': text})
    res = await http.post(HOTBOT_URL, data=json.dumps({'messages': s['messages'], 'model': 'gpt-5', 'camp': False}), headers={
        'accept': '*/*', 'accept-language': 'id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7', 'content-type': 'application/json',
        'cookie': f"chatai_language=en; theme=light; temp_uuid={s['tempUuid']}", 'origin': 'https://www.hotbot.com', 'referer': 'https://www.hotbot.com/',
        'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36'}, response_type='text')
    reply = parse_sse(res.text()).strip() or '(empty)'
    s['messages'].append({'role': 'assistant', 'content': reply})
    if len(s['messages']) > 20:
        s['messages'] = s['messages'][-20:]
    return reply


async def handler(m, *, conn, text, usedPrefix, command):
    if not text:
        return await conn.reply(m.chat, f"*GPT-5 (Hotbot)*\n\n▸ {usedPrefix + command} <pertanyaan>\n▸ {usedPrefix + command} reset", m)
    t = text.strip()
    if t.lower() == 'reset':
        sessions = conn.get('hotbot')
        if sessions and m.sender in sessions:
            del sessions[m.sender]
        return await conn.reply(m.chat, '✅ History cleared.', m)
    try:
        return await conn.reply(m.chat, await ask(m.sender, t, conn), m)
    except Exception as e:
        print('[hotbot]', e, file=sys.stderr)
        return await conn.reply(m.chat, error_text(e), m)


help = command = ['gpt']
tags = ['ai']
limit = 0
