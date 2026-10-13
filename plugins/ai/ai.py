import json
import math
import re
import sys

from lib.package.ai import mcp
from lib.utils import connection as Connection
from lib.utils.handler import G
from lib.utils.helper import maybe_await
from lib.utils.simple import _get


MEDIA_KEYS = ('imageMessage', 'audioMessage', 'videoMessage', 'documentMessage', 'stickerMessage')
CONTEXT_KEYS = ('extendedTextMessage', 'imageMessage', 'videoMessage', 'documentMessage', 'stickerMessage', 'audioMessage')
STICKER_TXT = 'React and respond simply and naturally to this sticker as if it were a real reply in our conversation'
IMAGE_TXT = 'Analyze or describe this image if it needed or if there problem, if no react and respond simply and naturally to this image as if it were a real reply in our conversation'
AUDIO_TXT = 'Transcribe and understand this voice note/audio'
VIDEO_TXT = 'Describe this video'
DOC_TXT = 'Read and summarize this document'
MEDIA_DEFAULTS = (('stickerMessage', STICKER_TXT), ('imageMessage', IMAGE_TXT), ('audioMessage', AUDIO_TXT),
                  ('videoMessage', VIDEO_TXT), ('documentMessage', DOC_TXT))
CONFIRM_WORDS = ('ya', 'yes', 'ok', 'oke', 'lanjut', 'confirm')
CANCEL_WORDS = ('batal', 'no', 'tidak', 'cancel', 'stop')
MODELS_TEXT = '\n'.join([
    '*Available Models*',
    '',
    '- `.ai` / `.ai:flash-lite` → Gemini 3.1 Flash Lite *(default)* — fast, can search & run all tools',
    '- `.ai:flash` → Gemini 3.5 Flash — more accurate, can search & run all tools',
    '- `.ai:pro` → Gemini 2.5 Pro — most powerful for strong reasoning, can search & run all tools',
    '- `.ai:gemma` → Gemma 4 31B-it — open-source, powerful for reasoning/coding/analysis & REPAIR of code that you paste directly in chat, *Cannot search & Cannot run tools* (pure text chat)',
    '- `.ai:gemma-moe` → Gemma 4 26B-A4B (MoE) — same as gemma but more resource efficient, ⚠️ *Can NOT search & CANNOT run tools*',
    '',
    'Example: `.ai:pro tell me about blackhole`',
    'Example: `.ai:gemma create a sorting function in python`',
    'Example: `.ai:gemma [paste your error code] Fix it`',
])
NOPE = object()


def _js(v):
    return 'undefined' if v is None else str(v)


def _botname():
    return _js(G.settings.get('botname') if isinstance(G.settings, dict) else None)


def _cget(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _num(jid):
    return str(jid or '').split(':')[0].split('@')[0]


def _strip_digits(v):
    return re.sub(r'[^0-9]', '', str(v or ''))


def _user(conn):
    return _get(conn, 'user') or {}


def _ctx_info(m):
    return mcp.get_context_info(m)


def _is_bot_mentioned(m, conn):
    bn = _num(_user(conn).get('id'))
    bl = _num(_user(conn).get('lid'))
    mentions = _cget(_ctx_info(m), 'mentionedJid') or []
    return any(_num(j) in (bn, bl) for j in mentions)


def _is_reply_to_bot(m, conn):
    ci = _ctx_info(m)
    if not ci:
        return False
    q = _num(_cget(ci, 'participant'))
    return q in (_num(_user(conn).get('id')), _num(_user(conn).get('lid')))


def _clean_text(text):
    return re.sub(r'@\d+', '', text or '').strip()


def _msg_dict(m):
    msg = _cget(m, 'message')
    return msg if isinstance(msg, dict) else {}


def _key_remote(m):
    return _cget(_cget(m, 'key'), 'remoteJid')


def _extract_text(m):
    msg = _msg_dict(m)
    sub = [
        msg.get('conversation'),
        _cget(msg.get('extendedTextMessage'), 'text'),
        _cget(msg.get('imageMessage'), 'caption'),
        _cget(msg.get('videoMessage'), 'caption'),
        _cget(msg.get('documentMessage'), 'caption'),
    ]
    for v in sub:
        if v:
            return v
    return ''


def _has_media(m):
    msg = _msg_dict(m)
    return any(msg.get(k) for k in MEDIA_KEYS)


def _has_quoted_media(m):
    q = _cget(_ctx_info(m), 'quotedMessage')
    if not q:
        return False
    return any(_cget(q, k) for k in MEDIA_KEYS)


def _media_default_text(m):
    qmsg = _cget(_ctx_info(m), 'quotedMessage') or {}
    for src in (_msg_dict(m), qmsg):
        if not isinstance(src, dict):
            continue
        for key, txt in MEDIA_DEFAULTS:
            if src.get(key):
                return txt
    return ''


def _explicit_context_info(m):
    msg = _cget(m, 'msg')
    ci = _cget(msg, 'contextInfo') if msg else None
    if ci:
        return ci
    md = _msg_dict(m)
    for k in CONTEXT_KEYS:
        v = md.get(k)
        if isinstance(v, dict) and v.get('contextInfo'):
            return v['contextInfo']
    return None


def _chat_db(chat):
    chats = (G.db.data or {}).get('chats') or {}
    return chats.get(chat) or {}


def _prefix_hit(pref, text):
    if isinstance(pref, re.Pattern):
        return bool(pref.search(text))
    if isinstance(pref, (list, tuple)):
        chars = ''.join(re.escape(str(p)) for p in pref)
        return bool(re.search('^[' + chars + ']', text))
    return bool(re.search('^' + re.escape(str(pref)), text))


async def _presence(conn, chat):
    fn = getattr(conn, 'sendPresenceUpdate', None)
    if not callable(fn):
        return
    try:
        await maybe_await(fn('composing', chat))
    except Exception:
        pass


def _buttons_payload(d):
    out = []
    for btn in (d.get('buttons') or []):
        typ = str(btn.get('type') or 'reply').lower()
        if typ == 'url':
            out.append({'text': btn.get('label') or 'Link', 'url': btn.get('value') or '', 'useWebview': True})
        elif typ == 'copy':
            out.append({'text': btn.get('label') or 'Copy', 'copy': btn.get('value') or ''})
        else:
            out.append({'text': btn.get('label') or 'Button', 'id': btn.get('value') or ''})
    return out


def _buttons_fallback(d):
    lines = [_js(d.get('body') or '')]
    if d.get('footer'):
        lines.append('_' + _js(d['footer']) + '_')
    for b in (d.get('buttons') or []):
        lines.append('• ' + _js(b.get('label')) + ': ' + _js(b.get('value')))
    return '\n'.join(lines)


def _codeblock_fallback(d):
    msg = ''
    if d.get('title'):
        msg += '*' + _js(d['title']) + '*\n\n'
    if d.get('description'):
        msg += _js(d['description']) + '\n\n'
    msg += '```' + _js(d.get('language') or 'text') + '\n' + _js(d.get('code') or '') + '\n```'
    return msg


async def _send_codeblock(conn, chat, m, d):
    rich = conn.aiRich()
    if d.get('title'):
        rich.setTitle(d['title'])
    if d.get('description'):
        rich.addText(_js(d['description']) + '\n', {'hyperlink': True})
    rich.addCode(d.get('language') or 'text', d.get('code') or '')
    return await rich.send(chat, {'quoted': m})


async def _send_buttons(conn, chat, m, d):
    msg = {'text': d.get('body') or '', 'nativeFlow': _buttons_payload(d)}
    if d.get('footer'):
        msg['footer'] = d['footer']
    return await conn.sendButton(chat, msg, m)


async def _typed_from_text(conn, chat, m, text, clear):
    t = text.strip()
    a, b = t.find('{'), t.rfind('}')
    if a == -1 or b <= a:
        return NOPE
    try:
        obj = json.loads(t[a:b + 1])
        if not isinstance(obj, dict):
            return NOPE
        kind = obj.get('__type')
        if kind == 'codeblock':
            await clear()
            try:
                return await _send_codeblock(conn, chat, m, obj)
            except Exception:
                return await conn.sendMessage(chat, {'text': _codeblock_fallback(obj)}, {'quoted': m})
        if kind == 'buttons':
            await clear()
            try:
                return await _send_buttons(conn, chat, m, obj)
            except Exception:
                return await conn.sendMessage(chat, {'text': _buttons_fallback(obj)}, {'quoted': m})
    except Exception:
        return NOPE
    return NOPE


async def handle_ai(conn, m, raw_text, model_key='default', is_owner=False):
    sender = _cget(m, 'sender') or _key_remote(m)
    chat = _key_remote(m) or _cget(m, 'chat')
    explicit = _explicit_context_info(m)

    if mcp.has_pending(sender):
        answer = raw_text.strip().lower()
        if answer in CONFIRM_WORDS:
            await _presence(conn, chat)
            result = await mcp.run_agent_confirmed(conn, m, {'senderJid': sender, 'isOwner': is_owner})
            return await conn.sendMessage(chat, {'text': result['text']}, {'quoted': m})
        if answer in CANCEL_WORDS:
            mcp.cancel_pending(sender)
            return await conn.sendMessage(chat, {'text': '❌ Cancelled.'}, {'quoted': m})
        mcp.cancel_pending(sender)

    sub = raw_text.strip().lower()

    if sub == 'reset':
        chat_jid = _key_remote(m) or _cget(m, 'chat') or sender
        mcp.reset_session(chat_jid)
        mcp.reset_rate_limit(sender)
        return await m.react('🔄')

    if sub == 'info':
        identity = await mcp.get_user_identity(sender, G.db, conn)
        keys = mcp.get_api_keys()
        if identity.get('isOwner'):
            user_info = 'Owner: ' + _js(identity.get('name'))
        elif identity.get('registered'):
            user_info = 'User: ' + _js(identity.get('name'))
        else:
            user_info = 'Unregistered user (' + _js(identity.get('number')) + ')'
        key_lines = ''
        if identity.get('isOwner') and keys:
            rows = []
            for k in mcp.get_key_status():
                cooling = k['cooling']
                if cooling:
                    state = '⏳ ' + ', '.join(f"{c['scope']} {math.ceil(c['remainingMs'] / 1000)}s" for c in cooling)
                else:
                    state = '✅ ready'
                rows.append(f"  {k['index']}. {k['masked']} {state}")
            key_lines = '\n' + '\n'.join(rows)
        key_text = f'{len(keys)} key active' if keys else 'No Apikey'
        return await conn.sendMessage(chat, {'text': f"*{_botname()} — Info*\n- {user_info}\n- API Keys: {key_text}{key_lines}\n- AI Tools: {mcp.count_tools()} tools\n- Default model: {mcp.MODELS['default']}"}, {'quoted': m})

    if sub == 'tools':
        tools = mcp.list_tools()
        if not tools:
            return await conn.sendMessage(chat, {'text': 'There is no tool yet.'}, {'quoted': m})
        body = '\n'.join(f'{i + 1}. `{t}`' for i, t in enumerate(tools))
        return await conn.sendMessage(chat, {'text': f'*AI Tools ({len(tools)})*\n\n' + body}, {'quoted': m})

    if sub == 'models':
        return await conn.sendMessage(chat, {'text': MODELS_TEXT}, {'quoted': m})

    has_content = bool(raw_text.strip()) or _has_media(m) or _has_quoted_media(m)
    if not has_content:
        usage = '\n'.join([
            f'*{_botname()}*',
            '',
            '*Usage:*',
            '• `.ai <question>` — ask anything',
            '• `.ai:flash-lite <text>` — use lite model',
            '• `.ai:pro <text>` — use powerful model',
            '• Send image/audio then `.ai` — analyze media',
            '',
            '*Subcommand:*',
            '• `.ai reset` — clear conversation session',
            '• `.ai info` — AI status & API keys',
            '• `.ai tools` — active MCP tools list',
            '• `.ai models` — list available models',
            '',
            '*In group:* mention bot or reply to bot messages to chat without prefix',
        ])
        return await conn.sendMessage(chat, {'text': usage}, {'quoted': m})

    user_text = raw_text.strip()
    if not user_text and (_has_media(m) or _has_quoted_media(m)):
        user_text = _media_default_text(m)

    await _presence(conn, chat)

    status = {'key': None, 'alive': False}

    async def on_step(label):
        if not status['alive']:
            return
        try:
            await conn.sendMessage(chat, {'edit': status['key'], 'text': f'> {label}'})
        except Exception:
            status['alive'] = False

    async def clear_status():
        if not status['key'] or not status['alive']:
            return
        status['alive'] = False
        try:
            await conn.sendMessage(chat, {'delete': status['key']})
        except Exception:
            pass

    async def finish_status(final_text):
        if not status['key'] or not status['alive']:
            return False
        status['alive'] = False
        try:
            await conn.sendMessage(chat, {'edit': status['key'], 'text': final_text})
            return True
        except Exception:
            return False

    step_fn = None
    if _cget(G.settings, 'ai') and _cget(_cget(G.settings, 'ai'), 'thinking'):
        try:
            sent = await conn.sendMessage(chat, {'text': '> ...'}, {'quoted': m})
            status['key'] = _cget(sent, 'key') or None
            status['alive'] = bool(status['key'])
        except Exception:
            status['key'] = None
            status['alive'] = False
        if status['key']:
            step_fn = on_step

    try:
        result = await mcp.run_agent(conn, m, user_text, {
            'modelKey': model_key,
            'isOwner': is_owner,
            'senderJid': sender,
            'onStep': step_fn,
            'contextInfo': explicit,
        })
    except Exception as err:
        print('[handleAI] unexpected runAgent() throw:', err, file=sys.stderr)
        await clear_status()
        return await conn.sendMessage(chat, {'text': f"❌ Unexpected error: {getattr(err, 'message', None) or err}"}, {'quoted': m})

    rtype = result.get('type')
    if rtype in ('confirm', 'error'):
        if await finish_status(result['text']):
            return None
        await clear_status()
        return await conn.sendMessage(chat, {'text': result['text']}, {'quoted': m})

    if rtype == 'message':
        await clear_status()
        mtype = result.get('messageType')
        d = result.get('messageData') or {}
        if mtype == 'codeblock':
            try:
                return await _send_codeblock(conn, chat, m, d)
            except Exception:
                return await conn.sendMessage(chat, {'text': _codeblock_fallback(d)}, {'quoted': m})
        if mtype == 'buttons':
            try:
                return await _send_buttons(conn, chat, m, d)
            except Exception:
                return await conn.sendMessage(chat, {'text': _buttons_fallback(d)}, {'quoted': m})
        return None

    text = result.get('text')
    if text:
        if '__type' in text:
            handled = await _typed_from_text(conn, chat, m, text, clear_status)
            if handled is not NOPE:
                return handled
        if await finish_status(text):
            return None
        await clear_status()
        await conn.sendMessage(chat, {'text': text}, {'quoted': m})
        return None

    await clear_status()
    return None


async def handler(m, *, conn, command, text, args, isOwner):
    model_key = command.split(':')[1] if ':' in command else 'default'
    await handle_ai(conn, m, text or ' '.join(args), model_key, isOwner)


async def all(m, *, conn):
    try:
        if _cget(m, 'fromMe'):
            return
        if _cget(m, 'isBaileys'):
            return
        conn = conn or _cget(m, 'conn') or Connection.state.conn
        sender = _cget(m, 'sender')
        if sender == _user(conn).get('jid'):
            return
        if str(_cget(m, 'id') or '').startswith('3EB0'):
            return
        raw_chat = str(_cget(m, 'chat') or '')
        if raw_chat.endswith('@broadcast') or raw_chat.endswith('@newsletter'):
            return

        bot_number = _strip_digits(_user(conn).get('jid') or _user(conn).get('id'))
        sender_number = _strip_digits(sender)
        if bot_number and sender_number and bot_number == sender_number:
            return

        pref = getattr(conn, 'prefix', None)
        if pref is None:
            pref = G.prefix
        text = str(_extract_text(m) or '')
        if _prefix_hit(pref, text.strip()):
            return

        chat = _key_remote(m) or _cget(m, 'chat')
        is_group = str(chat or '').endswith('@g.us')
        identity = await mcp.get_user_identity(sender, G.db, conn)
        is_owner = bool(identity.get('isOwner')) or bool(_cget(m, 'fromMe'))

        chat_db = _chat_db(chat)
        if not is_group:
            if not (chat_db.get('aiChat') or chat_db.get('gptChat')):
                return
            if not (text.strip() or _has_media(m) or _has_quoted_media(m)):
                return
            await handle_ai(conn, m, text, 'default', is_owner)
            return

        if not _is_bot_mentioned(m, conn) and not _is_reply_to_bot(m, conn):
            return
        if not (chat_db.get('aiChat') or chat_db.get('gptChat')):
            return
        await handle_ai(conn, m, _clean_text(text), 'default', is_owner)
    except Exception as err:
        print('[ai.js handler.all] Error caught, will not propagate:', err, file=sys.stderr)


help = ['ai <text>']
tags = ['ai']
command = re.compile(r'^ai(:[a-z-]+)?$', re.I)
