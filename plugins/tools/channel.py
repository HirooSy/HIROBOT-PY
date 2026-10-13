import json
import math
import re
import sys
import time
from datetime import datetime, timezone

from baileys import getBinaryNodeChild, getBinaryNodeChildren, S_WHATSAPP_NET
from lib.utils.helper import oc
from lib.utils.handler import PluginError


CHANNEL_LINK = re.compile(r'whatsapp\.com/channel/([A-Za-z0-9_-]+)', re.I)
NEWSLETTER_JID = re.compile(r'^\d+@newsletter$')
POST_COUNT = 20
QUERY = {'FOLLOWERS': '27472091235714801', 'INSIGHTS': '9853618868050977'}


def fmt(n):
    return f'{int(float(n)):,}'


def format_date(seconds):
    try:
        t = float(seconds)
    except (TypeError, ValueError):
        return '-'
    if not t:
        return '-'
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d %H:%M') + ' UTC'


async def safe(coro):
    try:
        return await coro
    except Exception:
        return None


async def resolve_role(conn, meta):
    def read(m):
        return str(oc(m, 'viewer_metadata', 'role') or '').upper()
    role = read(meta)
    if not role:
        role = read(await safe(conn.newsletterMetadata('jid', meta['id'])))
    return role


async def fetch_metadata(conn, inp):
    if NEWSLETTER_JID.match(inp):
        return await safe(conn.newsletterMetadata('jid', inp))
    mt = CHANNEL_LINK.search(inp)
    if not mt:
        raise PluginError('Invalid channel link. Example: https://whatsapp.com/channel/xxxxxxxx')
    return await safe(conn.newsletterMetadata('invite', mt.group(1)))


def _json_default(o):
    if isinstance(o, (bytes, bytearray)):
        return {'type': 'Buffer', 'data': list(o)}
    return str(o)


def stringify(value, indent=None):
    return json.dumps(value, indent=indent, default=_json_default, ensure_ascii=False)


def _text(content):
    if isinstance(content, (bytes, bytearray)):
        return bytes(content).decode('utf-8', errors='replace')
    return str(content)


def describe_error(e):
    data = getattr(e, 'data', None)
    detail = ''
    try:
        node = getBinaryNodeChild(data, 'result')
        content = node.get('content') if node else None
        detail = _text(content) if content else (stringify(data) if data else '')
    except Exception:
        detail = stringify(data) if data else ''
    return f"{e}{chr(10) * 2 + detail[:1500] if detail else ''}"


async def send_chunked(m, text, limit=3500):
    chunk = ''
    for line in text.split('\n'):
        if chunk and len(chunk) + len(line) + 1 > limit:
            await m.reply(chunk)
            chunk = ''
        chunk += ('\n' if chunk else '') + line
    if chunk:
        await m.reply(chunk)


async def fetch_followers(conn, jid, count):
    res = await conn.executeWMexQuery({'input': {'newsletter_id': jid, 'count': count}}, QUERY['FOLLOWERS'], 'xwa2_newsletter_followers')
    out = []
    for edge in oc(res, 'followers', 'edges') or []:
        node = (edge or {}).get('node') or {}
        out.append({'id': node.get('id'), 'phone': node.get('pn'),
                    'name': node.get('display_name') or oc(edge, 'admin_profile', 'name') or '',
                    'username': oc(node, 'username_info', 'username'), 'role': str((edge or {}).get('role') or 'SUBSCRIBER').upper()})
    return out


def format_person(p):
    who = (p.get('phone') or p.get('id') or '-').split('@')[0]
    extra = ', '.join(x for x in [f"@{p['username']}" if p.get('username') else '', p['role'] if p['role'] != 'SUBSCRIBER' else ''] if x)
    return f"- {p.get('name') or '-'} ({who}){f' [{extra}]' if extra else ''}"


async def fetch_insights(conn, jid):
    return await conn.executeWMexQuery({'input': {'newsletter_id': jid, 'metrics': ['NET_FOLLOWS', 'UNFOLLOWS']}}, QUERY['INSIGHTS'], 'xwa2_newsletter_admin_insights')


def build_poll(q):
    values = [oc(o, 'optionName') for o in (oc(q, 'options') or []) if oc(o, 'optionName')]
    if not oc(q, 'name') or len(values) < 2:
        raise PluginError('Invalid poll, cannot copy it.')
    poll = {'name': oc(q, 'name'), 'values': values, 'selectableCount': min(_to_int(oc(q, 'selectableOptionsCount')), len(values))}
    if oc(q, 'hideParticipantName'):
        poll['hideVoter'] = True
    ptype = oc(q, 'pollType')
    if (ptype == 1 and not isinstance(ptype, bool)) or ptype == 'QUIZ':
        ans = oc(q, 'correctAnswer', 'optionName')
        if not ans:
            raise PluginError('This quiz has no correct answer to copy.')
        poll['pollType'] = 1
        poll['correctAnswer'] = ans
    return poll


def question_text(q):
    inner = oc(q, 'message') or q
    return oc(inner, 'extendedTextMessage', 'text') or oc(inner, 'conversation') or oc(q, 'text') or ''


def build_question(text):
    return {'questionMessage': {'message': {'extendedTextMessage': {'text': text, 'contextInfo': {'isQuestion': True}}}}}


async def fetch_post_stats(conn, jid):
    gen = getattr(conn, 'generateMessageTag', None)
    tag = (gen() if callable(gen) else None) or str(int(time.time() * 1000))
    result = await conn.query({'tag': 'iq', 'attrs': {'id': tag, 'type': 'get', 'xmlns': 'newsletter', 'to': S_WHATSAPP_NET},
                               'content': [{'tag': 'messages', 'attrs': {'count': str(POST_COUNT), 'type': 'jid', 'jid': jid}}]})
    messages_node = getBinaryNodeChild(result, 'messages')
    posts = []
    for node in getBinaryNodeChildren(messages_node, 'message'):
        views_node = getBinaryNodeChild(node, 'views_count')
        views_attr = oc(views_node, 'attrs', 'count')
        reaction_nodes = getBinaryNodeChildren(getBinaryNodeChild(node, 'reactions'), 'reaction')
        posts.append({'id': node['attrs'].get('server_id') or node['attrs'].get('id'), 'time': _to_int(node['attrs'].get('t')),
                      'views': None if views_attr is None else _to_int(views_attr),
                      'reactions': sum(_to_int(oc(r, 'attrs', 'count')) for r in reaction_nodes)})
    return posts


def _to_int(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0
    return int(n) if n == n else 0


def summarize_posts(posts, followers):
    with_views = [p for p in posts if p['views'] is not None]
    if not with_views:
        return '- `Post stats:` not available (the server returned no view counts).'
    total = sum(p['views'] for p in with_views)
    reactions = sum(p['reactions'] for p in posts)
    avg = math.floor(total / len(with_views) + 0.5)
    best = max(with_views, key=lambda p: p['views'])
    lines = [f'- `Posts analyzed:` {len(posts)}', f'- `Total views:` {fmt(total)}', f'- `Average views/post:` {fmt(avg)}']
    if followers > 0:
        lines.append(f'- `Average reach:` {avg / followers * 100:.1f}% of followers')
    lines += [f'- `Total reactions:` {fmt(reactions)}', f"- `Best post:` {fmt(best['views'])} views ({format_date(best['time'])})"]
    return '\n'.join(lines)


async def up_channel(m, *, conn, args, usedPrefix, command):
    args, p, command = args, usedPrefix, command
    inp = next((a for a in args if CHANNEL_LINK.search(a) or NEWSLETTER_JID.match(a)), None)
    if not inp:
        raise PluginError(f'Usage: {p + command} <channel link> [question]\nReply to the message you want to send.')
    if not m.quoted:
        raise PluginError('Reply to the message you want to send to the channel.')
    flags = [re.sub(r'^-+', '', a.lower()) for a in args if a != inp]
    q = m.quoted
    as_question = 'question' in flags or 'q' in flags or q.mtype == 'questionMessage'
    meta = await fetch_metadata(conn, inp)
    if not meta or not meta.get('id'):
        raise PluginError('Channel not found, or the link is invalid.')
    role = str(oc(meta, 'viewer_metadata', 'role') or '').upper()
    if role and role not in ('OWNER', 'ADMIN'):
        raise PluginError('The bot is not an owner/admin of that channel, so it cannot post there.')
    name = oc(meta, 'thread_metadata', 'name', 'text') or meta.get('name') or inp
    cid = meta['id']
    try:
        if re.match(r'^pollCreationMessage', q.mtype or ''):
            label = 'poll'
            await conn.sendMessage(cid, {'poll': build_poll(q)})
        elif as_question:
            label = 'question'
            text = question_text(q)
            if not text:
                raise PluginError('The question has no text.')
            await conn.relayMessage(cid, build_question(text), {})
        elif q.mediaMessage and q.mediaType:
            media = q.mediaMessage[q.mediaType] or {}
            buf = await q.download()
            ptt = q.mediaType == 'audioMessage' and bool(media.get('ptt'))
            label = re.sub(r'Message$', '', q.mediaType)
            await conn.sendFile(cid, buf, media.get('fileName') or '', media.get('caption') or '', None, ptt,
                                {'mimetype': media['mimetype']} if q.mediaType == 'documentMessage' and media.get('mimetype') else {})
        elif q.text:
            label = 'text'
            await conn.sendMessage(cid, {'text': q.text})
        else:
            raise PluginError('That message type is not supported.')
    except PluginError:
        raise
    except Exception as e:
        print('[channel] failed to send:', e, file=sys.stderr)
        raise PluginError(f'Failed to send to channel: {e}')
    await m.reply(f'Sent {label} to {name}.')


async def status_channel(m, *, conn, args, usedPrefix, command):
    args, p, command = args, usedPrefix, command
    inp = next((a for a in args if CHANNEL_LINK.search(a) or NEWSLETTER_JID.match(a)), None)
    if not inp:
        raise PluginError(f'Usage: {p + command} <channel link> [followers|admins|insights]')
    flags = [re.sub(r'^-+', '', a.lower()) for a in args if a != inp]
    meta = await fetch_metadata(conn, inp)
    if not meta or not meta.get('id'):
        raise PluginError('Channel not found, or the link is invalid.')
    thread = meta.get('thread_metadata') or {}
    followers = _to_int(thread.get('subscribers_count'))
    role = await resolve_role(conn, meta)
    can_manage = role not in ('SUBSCRIBER', 'GUEST')
    desc = oc(thread, 'description', 'text') or ''
    handle = f"@{re.sub(r'^@', '', str(thread['handle']))}" if thread.get('handle') else '-'
    info = [f"- `Name:` {oc(thread, 'name', 'text') or '-'}", f'- `Handle:` {handle}', f"- `ID:` {meta['id']}", f"- `State:` {oc(meta, 'state', 'type') or '-'}",
            f"- `Verified:` {'yes' if str(thread.get('verification') or '').upper() == 'VERIFIED' else 'no'}", f"- `Created:` {format_date(thread.get('creation_time'))}",
            f'- `Followers:` {fmt(followers)}', f"- `Bot role:` {role or 'unknown'}"]
    if can_manage:
        admins = await safe(conn.newsletterAdminCount(meta['id']))
        if admins is not None:
            info.append(f'- `Admins:` {admins}')
    if desc:
        info.append(f"- `Description:` {desc[:200] + '...' if len(desc) > 200 else desc}")
    try:
        stats = summarize_posts(await fetch_post_stats(conn, meta['id']), followers)
    except Exception as e:
        print('[channel] failed to fetch post stats:', e, file=sys.stderr)
        stats = '- `Post stats:` failed to fetch.'
    await m.reply('\n'.join(info) + '\n\n' + stats)
    want_f, want_a = 'followers' in flags, 'admins' in flags
    if want_f or want_a:
        try:
            people = await fetch_followers(conn, meta['id'], min(max(followers, 50), 1000))
        except Exception as e:
            raise PluginError(f'Failed to fetch followers: {describe_error(e)}')
        if want_a:
            admins = [x for x in people if x['role'] in ('OWNER', 'ADMIN')]
            await send_chunked(m, '\n'.join([f'Admins ({len(admins)}):', *map(format_person, admins)]))
        if want_f:
            await send_chunked(m, '\n'.join([f'Followers returned: {len(people)} of {fmt(followers)}', *map(format_person, people)]))
    if 'insights' in flags:
        try:
            await send_chunked(m, stringify(await fetch_insights(conn, meta['id']), 2) or 'No insights data returned.')
        except Exception as e:
            raise PluginError(f'Failed to fetch insights: {describe_error(e)}')


async def handler(m, *, conn, command, args, usedPrefix):
    if re.match(r'^up(ch|channel)$', command, re.I):
        return await up_channel(m, conn=conn, args=args, usedPrefix=usedPrefix, command=command)
    return await status_channel(m, conn=conn, args=args, usedPrefix=usedPrefix, command=command)


help = ['upch|upchannel <link> [question] (reply)', 'statsch|statschannel|statusch|statuschannel <link> [followers|admins|insights]']
tags = ['tools']
command = re.compile(r'^(upch(annel)?|statsch(annel)?|statusch(annel)?)$', re.I)
owner = True
ai = {'risk': 'low', 'description': 'send a replied message (media, audio, poll, question) to a WhatsApp channel, or check channel stats (followers, views, reactions)'}
