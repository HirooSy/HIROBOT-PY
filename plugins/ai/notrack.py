import json
import sys
import uuid

from lib.compat import http


UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
BASE_URL = 'https://notrack.ai'
def notrack_sessions(conn):
    sessions = conn.get('notrack')
    if sessions is None:
        sessions = {}
        conn['notrack'] = sessions
    return sessions


def error_text(e):
    resp = getattr(e, 'response', None)
    if resp is None:
        return str(getattr(e, 'message', None) or e)
    data = getattr(resp, 'data', None)
    if isinstance(data, (bytes, bytearray)):
        data = data.decode('utf-8', errors='replace')
    return json.dumps(data, ensure_ascii=False)


async def notrack_chat(prompt, session_id=None, chat_id=None, persona='normal', mode='usual'):
    if not prompt or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('Parameter prompt wajib diisi')
    sid = session_id or str(uuid.uuid4())
    payload = {'user_input': prompt.strip(), 'mode': mode or 'usual', 'model': 'C', 'persona': persona or 'normal', 'max_turns': 10, 'chat_id': chat_id or None,
               'attachments': [], 'regenerate': False, 'edit': False, 'edit_mid': None, 'via': 'text'}
    res = await http.post(f'{BASE_URL}/api/dispatch', json=payload, timeout=35, headers={
        'User-Agent': UA, 'Content-Type': 'application/json', 'Origin': BASE_URL, 'Referer': f'{BASE_URL}/id/chat', 'Cookie': f'uid={sid}', 'Accept': 'text/event-stream'})
    full, returned = '', chat_id or None
    for line in res.text().split('\n'):
        if not line.startswith('data: '):
            continue
        try:
            evt = json.loads(line[6:])
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        t = evt.get('type')
        if t == 'chat_meta' and evt.get('chat_id'):
            returned = evt['chat_id']
        elif t == 'delta' and evt.get('chunk'):
            full += evt['chunk']
        elif t in ('message', 'consensus') and evt.get('content'):
            full = evt['content']
        elif t == 'error':
            raise RuntimeError(evt.get('content') or 'Terjadi kesalahan dari NoTrack AI')
    if not full.strip():
        raise RuntimeError('NoTrack AI tidak memberikan respon jawaban')
    return {'prompt': prompt.strip(), 'result': full.strip(), 'session_id': sid, 'chat_id': returned}


async def ask(sender, text, conn):
    sessions = notrack_sessions(conn)
    s = sessions.get(sender) or {}
    r = await notrack_chat(text, s.get('session_id'), s.get('chat_id'))
    sessions[sender] = {'session_id': r['session_id'], 'chat_id': r['chat_id']}
    return r['result']


async def handler(m, *, conn, text, usedPrefix, command):
    if not text:
        return await conn.reply(m.chat, f"*NoTrack AI*\n\n▸ {usedPrefix + command} <pertanyaan>\n▸ {usedPrefix + command} reset", m)
    t = text.strip()
    if t.lower() == 'reset':
        sessions = conn.get('notrack')
        if sessions and m.sender in sessions:
            del sessions[m.sender]
        return await conn.reply(m.chat, '✅ History cleared.', m)
    try:
        return await conn.reply(m.chat, await ask(m.sender, t, conn), m)
    except Exception as e:
        print('[notrack]', e, file=sys.stderr)
        return await conn.reply(m.chat, error_text(e), m)


help = command = ['notrack', 'notrackai']
tags = ['ai']
limit = 0
