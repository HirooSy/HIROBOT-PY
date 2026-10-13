import math
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from lib.utils import helper
from lib.utils.handler import G
from lib.utils.helper import oc


DEFAULT_LIMIT = 30
DEFAULT_MAX_CHARS = 5000
LINE_MAX = 300
QUOTE_MAX = 40

WRAPPERS = [
    'ephemeralMessage', 'viewOnceMessage', 'viewOnceMessageV2', 'viewOnceMessageV2Extension',
    'documentWithCaptionMessage', 'editedMessage', 'groupMentionedMessage', 'botInvokeMessage',
]


def _g(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def _num(jid):
    return str(jid or '').split(':')[0].split('@')[0]


def _to_float(value):
    if value is None:
        return float('nan')
    if isinstance(value, dict) and 'low' in value:
        high = int(value.get('high') or 0)
        low = int(value.get('low') or 0) & 0xFFFFFFFF
        return float((high << 32) | low)
    try:
        return float(str(value))
    except ValueError:
        return float('nan')


def _pick(opts, key, default):
    value = opts.get(key)
    return default if value is None else value


def _jsnum(value):
    if value is None:
        return float('nan')
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == '':
        return 0.0
    try:
        return float(text)
    except ValueError:
        return float('nan')


def groupContextLimit():
    match = re.match(r'\s*[+-]?\d+', os.environ.get('AI_GROUP_CONTEXT', '') or '')
    if not match:
        return DEFAULT_LIMIT
    return max(0, min(100, int(match.group(0))))


def storeEnabled():
    return oc(G.settings, 'ai', 'loadStore') is not False


def _get_store():
    if not storeEnabled():
        return None
    from lib.utils import connection
    return getattr(connection, 'store', None)


def _unwrap(message):
    cur = message or {}
    for _ in range(5):
        key = next((w for w in WRAPPERS if _g(_g(cur, w), 'message')), None)
        if not key:
            break
        cur = _g(_g(cur, key), 'message')
    return cur or {}


def _describe(message):
    c = _unwrap(message)
    text = _g(c, 'conversation') or _g(_g(c, 'extendedTextMessage'), 'text')
    if text:
        return str(text)

    def tag(label, caption):
        return f'[{label}] {caption}' if caption else f'[{label}]'

    if _g(c, 'imageMessage') is not None:
        return tag('photo', _g(c['imageMessage'], 'caption'))
    if _g(c, 'videoMessage') is not None:
        return tag('gif' if _g(c['videoMessage'], 'gifPlayback') else 'video', _g(c['videoMessage'], 'caption'))
    if _g(c, 'audioMessage') is not None:
        return '[voice note]' if _g(c['audioMessage'], 'ptt') else '[audio]'
    if _g(c, 'stickerMessage') is not None:
        return '[sticker]'
    if _g(c, 'documentMessage') is not None:
        doc = c['documentMessage']
        name = f" {_g(doc, 'fileName')}" if _g(doc, 'fileName') else ''
        return tag(f'document{name}', _g(doc, 'caption'))
    if _g(c, 'contactMessage') is not None or _g(c, 'contactsArrayMessage') is not None:
        return '[contact]'
    if _g(c, 'locationMessage') is not None or _g(c, 'liveLocationMessage') is not None:
        return '[location]'
    poll = _g(c, 'pollCreationMessage') or _g(c, 'pollCreationMessageV2') or _g(c, 'pollCreationMessageV3')
    if poll:
        return f"[poll] {_g(poll, 'name', '')}".strip()
    return ''


def _context_info(message):
    c = _unwrap(message)
    if not isinstance(c, dict):
        return None
    for v in c.values():
        if v and isinstance(v, dict) and _g(v, 'contextInfo'):
            return v['contextInfo']
    return None


def _ts_of(msg):
    t = _to_float(_g(msg, 'messageTimestamp'))
    return t if math.isfinite(t) else 0


def _zone(tz):
    try:
        return ZoneInfo(tz)
    except Exception:
        return ZoneInfo('Asia/Jakarta')


def _local(sec, tz):
    return datetime.fromtimestamp(sec, _zone(tz))


def _day_key(d):
    return f'{d.day:02d}/{d.month:02d}/{d.year}'


def _today_key(tz):
    return _day_key(_local(time.time(), tz))


def _stamp(sec, tz, today_key):
    d = _local(sec, tz)
    clock = f'{d.hour:02d}:{d.minute:02d}'
    if _day_key(d) == today_key:
        return clock
    return f'{d.day:02d}/{d.month:02d} {clock}'


def _is_command(text):
    pref = getattr(helper, 'prefix_list', None)
    if isinstance(pref, (list, tuple)):
        prefixes = list(pref)
    elif isinstance(pref, str):
        prefixes = [pref]
    else:
        prefixes = []
    t = str(text).lstrip()
    return any(
        isinstance(p, str) and p and re.match('^' + re.escape(p) + '[a-z]', t, re.I) is not None
        for p in prefixes
    )


def _clean_text(text, max_len, bot_ids):
    t = str(text)
    t = re.sub(r'</?(?:group_history|user_message|riwayat_grup|pesan_user)[^>]*>', '[tag]', t, flags=re.I)
    t = re.sub(r'\s*\n+\s*', ' ⏎ ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    if bot_ids:
        t = re.sub('@(?:' + '|'.join(re.escape(b) for b in bot_ids) + ')', '@bot', t)
    return t[:max_len - 1] + '…' if len(t) > max_len else t


def _make_who(conn):
    cache = {}

    def users():
        data = getattr(G.db, 'data', None) or {}
        return data.get('users') or {}

    def who(msg):
        key = _g(msg, 'key') or {}
        if _g(key, 'fromMe'):
            return {'self': True, 'name': 'Bot', 'num': _num(_g(_g(conn, 'user'), 'id'))}
        raw = str(_g(key, 'participant') or _g(msg, 'participant') or _g(key, 'remoteJid') or '')
        if raw in cache:
            return cache[raw]

        pn = _g(key, 'participantPn') or _g(key, 'senderPn') or None
        alt = str(_g(key, 'participantAlt') or '')
        if not pn and alt.endswith('@s.whatsapp.net'):
            pn = alt
        if not pn and raw.endswith('@lid'):
            pn = _g(users().get(raw) or {}, 'number') or None
        if not pn and raw.endswith('@s.whatsapp.net'):
            pn = raw

        number = _num(pn or raw)
        db_name = _g(users().get(pn or raw) or {}, 'name')
        name = _g(msg, 'pushName') or (db_name if db_name and db_name != number else None)
        out = {'self': False, 'name': name or None, 'num': number}
        cache[raw] = out
        return out

    return who


def _collect(store, conn, chat, skip_commands):
    messages = _g(store, 'messages') or {}
    arr = messages.get(chat) if isinstance(messages, dict) else None
    if not isinstance(arr, list) or not arr:
        return []

    who = _make_who(conn)
    by_id = {}
    for m in arr:
        mid = _g(_g(m, 'key'), 'id')
        if mid:
            by_id[mid] = m
    bot_ids = [x for x in (_num(_g(_g(conn, 'user'), 'id')), _num(_g(_g(conn, 'user'), 'lid'))) if x]

    rows = []
    for msg in arr:
        if not _g(msg, 'message') or _g(msg, 'messageStubType'):
            continue
        raw = _describe(_g(msg, 'message'))
        if not raw:
            continue
        w = who(msg)
        if skip_commands and not w['self'] and _is_command(raw):
            continue

        reply = None
        ci = _context_info(_g(msg, 'message'))
        if _g(ci, 'quotedMessage'):
            snip = _clean_text(_describe(_g(ci, 'quotedMessage')), QUOTE_MAX, bot_ids)
            stanza = _g(ci, 'stanzaId')
            target = by_id.get(stanza) if stanza else None
            participant = str(_g(ci, 'participant') or '')
            qw = who(target) if target else {'self': _num(participant) in bot_ids, 'name': None, 'num': _num(participant)}
            if snip:
                reply = {'who': qw, 'snip': snip}
        rows.append({'id': _g(_g(msg, 'key'), 'id'), 'ts': _ts_of(msg), 'who': w, 'text': raw, 'reply': reply, 'botIds': bot_ids})
    return sorted(rows, key=lambda r: r['ts'])


def _label(w, with_num):
    if w['self']:
        return 'Bot'
    if not w['name']:
        return w['num'] or '?'
    return f"{w['name']} ({w['num']})" if with_num and w['num'] else w['name']


def _line(r, tz, today_key, with_num=False, line_max=LINE_MAX):
    head = f"[{_stamp(r['ts'], tz, today_key)}] {_label(r['who'], with_num)}"
    rep = ''
    if r['reply']:
        rep = f' (reply to {_label(r["reply"]["who"], False)}: "{r["reply"]["snip"]}")'
    return f"{head}{rep}: {_clean_text(r['text'], line_max, r['botIds'])}"


async def buildGroupContext(conn, m, options=None, **kw):
    opts = {**(options or {}), **kw}
    limit = _pick(opts, 'limit', groupContextLimit())
    max_chars = _pick(opts, 'maxChars', DEFAULT_MAX_CHARS)
    tz = _pick(opts, 'tz', 'Asia/Jakarta')
    injected = opts.get('store')

    chat = _g(m, 'chat') or _g(_g(m, 'key'), 'remoteJid')
    if not limit or not chat or not str(chat).endswith('@g.us'):
        return ''

    store = injected if injected is not None else _get_store()
    if store is None:
        return ''
    msg_id = _g(_g(m, 'key'), 'id')
    rows = [r for r in _collect(store, conn, chat, True) if r['id'] != msg_id and r['id'] != _g(m, 'id')]
    if not rows:
        return ''

    today = _today_key(tz)
    picked = rows[-int(limit):]
    lines = []
    used = 0
    for i in range(len(picked) - 1, -1, -1):
        line = _line(picked[i], tz, today)
        if used + len(line) > max_chars and lines:
            break
        used += len(line) + 1
        lines.insert(0, line)

    older = len(rows) - len(lines)
    chats = _g(store, 'chats') or {}
    subject = _g(chats.get(chat) or {}, 'subject') if isinstance(chats, dict) else None
    attrs = (f' group="{str(subject).replace(chr(34), chr(39))}"' if subject else '') + f' messages="{len(lines)}" timezone="{tz}"'
    note = f'\n(+{older} older messages not shown — use the read_chat_history tool if needed)' if older > 0 else ''
    return f'<group_history{attrs}>\n' + '\n'.join(lines) + note + '\n</group_history>'


async def readChatHistory(conn, chat, options=None, **kw):
    opts = {**(options or {}), **kw}
    limit = _pick(opts, 'limit', 50)
    minutes = opts.get('minutes')
    keyword = opts.get('keyword')
    sender = opts.get('sender')
    tz = _pick(opts, 'tz', 'Asia/Jakarta')
    injected = opts.get('store')
    store = injected if injected is not None else _get_store()

    rows = _collect(store, conn, chat, False)
    total = len(rows)
    if not total:
        return {'text': '', 'total': 0, 'shown': 0}

    minutes_n = _jsnum(minutes)
    if minutes_n > 0:
        cutoff = time.time() - minutes_n * 60
        rows = [r for r in rows if r['ts'] >= cutoff]
    if keyword:
        k = str(keyword).lower()
        rows = [r for r in rows if k in r['text'].lower()]
    if sender:
        s = str(sender).lower()
        rows = [r for r in rows if s in (r['who']['name'] or '').lower() or s in (r['who']['num'] or '')]

    cap_n = _jsnum(limit)
    if math.isnan(cap_n) or cap_n == 0:
        cap_n = 50
    cap = int(max(1, min(100, cap_n)))
    picked = rows[-cap:]
    today = _today_key(tz)
    chats = _g(store, 'chats') or {}
    subject = _g(chats.get(chat) or {}, 'subject') if isinstance(chats, dict) else None
    return {
        'text': '\n'.join(_line(r, tz, today, with_num=True, line_max=600) for r in picked),
        'total': total,
        'shown': len(picked),
        'matched': len(rows),
        'subject': subject or None,
    }
