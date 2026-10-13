import sys

from lib.compat import http


URL = 'https://text.pollinations.ai/openai'
HEADERS = {'Content-Type': 'application/json', 'Referer': 'https://pollinations.ai/'}
def conn_store(conn):
    store = conn.get('ai')
    if store is None:
        store = {}
        conn['ai'] = store
    return store


def reply_content(data):
    try:
        content = data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        return '(empty)'
    if content is None:
        return '(empty)'
    return str(content).strip()


async def ask(sender, text, conn):
    sessions = conn_store(conn)
    sessions.setdefault(sender, []).append({'role': 'user', 'content': text})
    res = await http.post(URL, json={'model': 'openai', 'messages': sessions[sender], 'stream': False}, headers=HEADERS)
    reply = reply_content(res.json())
    sessions[sender].append({'role': 'assistant', 'content': reply})
    if len(sessions[sender]) > 20:
        sessions[sender] = sessions[sender][-20:]
    return reply


async def handler(m, *, conn, text, usedPrefix, command):
    if not text:
        return await conn.reply(m.chat, f"*Free AI Chat*\n\n▸ {usedPrefix + command} <question>\n▸ {usedPrefix + command} reset", m)
    t = text.strip()
    if t.lower() == 'reset':
        sessions = conn.get('ai')
        if sessions and m.sender in sessions:
            del sessions[m.sender]
        return await conn.reply(m.chat, '✅ History cleared.', m)
    try:
        return await conn.reply(m.chat, await ask(m.sender, t, conn), m)
    except Exception as e:
        print('[ai]', e, file=sys.stderr)
        return await conn.reply(m.chat, str(getattr(e, 'message', None) or e), m)


help = command = ['pollination']
tags = ['ai']
limit = 0
