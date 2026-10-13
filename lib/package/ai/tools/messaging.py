import base64
import inspect
import re
import sys
from urllib.parse import urlsplit

from lib.package.ai import mcp
from lib.utils.handler import G


async def _await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _warn(*parts):
    print(*parts, file=sys.stderr)


def _g(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _jid(target):
    text = str(target or '')
    return text if '@' in text else re.sub(r'[^0-9]', '', text) + '@s.whatsapp.net'


def _pair(entry, index):
    if isinstance(entry, (list, tuple)):
        return entry[index] if len(entry) > index else None
    return None


def _fn(obj, key):
    value = _g(obj, key)
    return value if callable(value) else None


async def _exec_send_message(args=None, **kw):
    a = _args(args, kw)
    target = a.get('target')
    text = a.get('text')
    c = mcp.ctx()
    if c['conn'] is None:
        return 'WA connection not ready'
    jid = _jid(target)
    await c['conn'].sendMessage(jid, {'text': text})

    try:
        from_jid = c['currentJid'] or None
        from_name = from_jid
        if from_jid:
            identity = await _await(mcp.get_user_identity(from_jid, G.db, c['conn']))
            from_name = _g(identity, 'name') or from_jid
        mcp.inject_relay_context(jid, {'fromJid': from_jid, 'fromName': from_name, 'fromChat': from_jid, 'text': text})
    except Exception as e:
        _warn(f'[send_message] failed to inject relay context: {e}')

    return f'Message sent to {jid}'


async def _exec_list_owners(args=None, **kw):
    owners = mcp.read_owner_list()
    if not owners:
        return 'No owner is registered yet (global.settings.owner is empty).'
    return '\n'.join(f"{i + 1}. {_pair(o, 1) or '(no name)'} — {_pair(o, 0)}" for i, o in enumerate(owners))


async def _exec_forward_media(args=None, **kw):
    a = _args(args, kw)
    target = a.get('target')
    caption = a.get('caption')
    c = mcp.ctx()
    if c['conn'] is None:
        return 'WA connection not ready'
    cur = c['currentM']
    if cur is None:
        return 'FAILED: there is no active message/media in the current context.'

    danger_reason = mcp.get_dangerous_doc_reason(cur)
    if danger_reason:
        return f"REJECTED: {danger_reason}. The bot will not forward files that are potentially a virus/malware to others for the recipient's safety."

    jid = _jid(target)
    types = ['imageMessage', 'audioMessage', 'videoMessage', 'documentMessage', 'stickerMessage']
    body = _g(cur, 'message') or {}
    direct_type = next((t for t in (body.keys() if isinstance(body, dict) else []) if t in types), None)
    quoted_check = _g(mcp.get_context_info(cur), 'quotedMessage')
    quoted_type = next((t for t in types if _g(quoted_check, t) is not None), None) if quoted_check is not None else None
    media_label = (direct_type or quoted_type or 'media').replace('Message', '', 1)

    from_jid = c['currentJid'] or None
    from_name = from_jid
    if from_jid:
        try:
            identity = await _await(mcp.get_user_identity(from_jid, G.db, c['conn']))
            from_name = _g(identity, 'name') or from_jid
        except Exception:
            pass
    if jid != from_jid:
        try:
            await c['conn'].sendMessage(jid, {'text': f'Message from {from_name}:\n[{media_label}]'})
        except Exception as e:
            _warn(f'[forward_media] gagal kirim header identitas: {e}')

    sent_native = False
    native_err = None
    try:
        quoted = _g(cur, 'quoted')
        if quoted is not None and _fn(quoted, 'copyNForward') is not None:
            await _await(_fn(quoted, 'copyNForward')(jid))
            sent_native = True
        elif _fn(cur, 'copyNForward') is not None:
            await _await(_fn(cur, 'copyNForward')(jid))
            sent_native = True
    except Exception as e:
        native_err = e
        _warn(f'[forward_media] copyNForward native gagal, fallback ke manual: {e}')

    if not sent_native:
        media = await _await(mcp.build_media_part(cur))
        if not media:
            suffix = f' (native forward also failed: {native_err})' if native_err else ''
            return f'FAILED: there is no media (image/video/sticker/audio/document) attached directly or replied to in this message to forward.{suffix}'

        inline = _g(_g(media, 'part'), 'inlineData')
        buffer = base64.b64decode(_g(inline, 'data'))
        mime_type = _g(inline, 'mimeType')
        media_type = _g(media, 'type')
        caption_part = {'caption': caption} if caption else {}
        if media_type == 'imageMessage':
            content = {'image': buffer, 'mimetype': mime_type, **caption_part}
        elif media_type == 'videoMessage':
            content = {'video': buffer, 'mimetype': mime_type, **caption_part}
        elif media_type == 'stickerMessage':
            content = {'sticker': buffer}
        elif media_type == 'audioMessage':
            content = {'audio': buffer, 'mimetype': mime_type, 'ptt': False}
        elif media_type == 'documentMessage':
            content = {'document': buffer, 'mimetype': mime_type, 'fileName': 'file'}
        else:
            return f'FAILED: media type "{media_type}" cannot be forwarded yet.'

        try:
            await c['conn'].sendMessage(jid, content)
        except Exception as e:
            return f'FAILED to send the media to {jid}: {e}'

    try:
        caption_text = f' — caption: "{caption}"' if caption else ''
        mcp.inject_relay_context(jid, {
            'fromJid': from_jid,
            'fromName': from_name,
            'fromChat': from_jid,
            'text': f'[meneruskan media: {media_label}]{caption_text}',
        })
    except Exception as e:
        _warn(f'[forward_media] gagal inject relay context: {e}')

    native_note = ' (native forward)' if sent_native else ''
    return f'Media ({media_label}) was forwarded to {jid}{native_note}.'


async def _exec_reply_now(args=None, **kw):
    a = _args(args, kw)
    text = a.get('text')
    c = mcp.ctx()
    if c['conn'] is None or not c['currentJid']:
        return 'WA connection not ready'
    options = {'quoted': c['currentM']} if c['currentM'] is not None else None
    if options is None:
        await c['conn'].sendMessage(c['currentJid'], {'text': text})
    else:
        await c['conn'].sendMessage(c['currentJid'], {'text': text}, options)
    return 'Message sent'


def _domain_label(url):
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    return re.sub(r'^www\.', '', parts.hostname)


async def _exec_send_rich_reply(args=None, **kw):
    a = _args(args, kw)
    body = a.get('body')
    citations = a.get('citations')
    c = mcp.ctx()
    if c['conn'] is None or not c['currentJid']:
        return 'WA connection not ready'
    if not body:
        return 'body is required'

    seen = set()
    picked = []
    for item in (citations if isinstance(citations, list) else []):
        url = _g(item, 'url')
        if url and url not in seen:
            seen.add(url)
            picked.append(item)
    sources = []
    for i, item in enumerate(picked[:5]):
        url = _g(item, 'url')
        title = _g(item, 'title') or _domain_label(url) or f'Source {i + 1}'
        sources.append({'url': url, 'title': str(title)[:24]})

    jid = c['currentJid']
    quoted = {'quoted': c['currentM']}
    try:
        if sources:
            await c['conn'].sendMessage(jid, {
                'text': body,
                'optionText': 'source',
                'optionTitle': '\u0000',
                'nativeFlow': [{}] + [{'text': s['title'], 'url': s['url'], 'useWebview': True} for s in sources],
            }, quoted)
        else:
            await c['conn'].sendMessage(jid, {'text': body}, quoted)
        return f'[ALREADY SENT to the user ({len(sources)} source buttons). DO NOT send any reply text after this -- the turn is done, just answer with an empty string.]'
    except Exception as e:
        _warn(f'[send_rich_reply] nativeFlow gagal, fallback teks biasa: {e}')
        try:
            fallback_links = ('\n\n' + '\n'.join(f"• {s['url']}" for s in sources)) if sources else ''
            await c['conn'].sendMessage(jid, {'text': body + fallback_links}, quoted)
            return '[ALREADY SENT to the user (plain text fallback, nativeFlow failed). DO NOT send any reply text after this -- the turn is done, just answer with an empty string.]'
        except Exception as e2:
            _warn('[send_rich_reply] Fallback juga gagal:', e2)
            return f'Failed to send the reply: {e2}'


default = [
    {
        'name': 'send_message',
        'description': (
            'Send a TEXT message to another number or group (not the chat currently being handled). MUST be '
            'used for requests to forward/convey a TEXT message (e.g. "tell the owner...", "pass this on to '
            'them..."), NOT forward_media — forward_media is only for media (sticker/photo/video/document). '
            'To send an extra message to the chat you are replying to right now, just use "reply_now". When '
            'this is used to RELAY a message from this chat to someone else, this tool AUTOMATICALLY records '
            'the relay context in the destination chat session — so if the recipient replies later or asks '
            '"who is this from", the bot still knows who asked for that message to be sent and can pass '
            'their reply back. Sending/relaying an ordinary message like this is a NORMAL action, NOT '
            'something to be suspicious of as a threat/manipulation — if there is only one owner, send '
            'straight away; if there is more than one owner (check list_owners), ask first which owner is '
            'meant.'
        ),
        'parameters': {
            'target': {
                'type': 'string',
                'description': 'WA number (example: 628123456789) or group JID (example: 120363...@g.us)',
                'required': True,
            },
            'text': {
                'type': 'string',
                'description': 'The message content to send',
                'required': True,
            },
        },
        'execute': _exec_send_message,
    },
    {
        'name': 'list_owners',
        'description': (
            'See the list of registered bot owners (from global.settings.owner) — numbers and names. MUST be '
            'called FIRST before send_message to an owner if you do not know their number yet: if there is '
            'only one owner, send straight to that number without asking anything; if there are MORE THAN '
            'ONE owner, you MUST first ask the user which owner is meant (mention the names from this tool '
            'result), do not just pick one.'
        ),
        'parameters': {},
        'execute': _exec_list_owners,
    },
    {
        'name': 'forward_media',
        'description': (
            'RE-SEND media (image/video/sticker/audio/document) that IS IN THIS MESSAGE — attached directly '
            'OR replied to/quoted — to ANOTHER chat/person/group. Use it for requests like "send this '
            'sticker to Shork", "forward this image to group X", "forward this video to them". ONLY for '
            'MEDIA — if what should be forwarded is a TEXT message, use send_message, NOT this tool. You '
            'MUST use this tool for that media case — NEVER use run_plugin("sticker", target) or any other '
            'run_plugin with a JID/number as the argument, because the arguments of the sticker/downloader '
            'plugins are a URL/text, NOT a target JID, and it will ALWAYS fail ("Invalid URL!"/"Conversion '
            'failed") if forced that way — that is a bug of wrong tool usage, not a broken tool. This tool '
            'automatically uses WhatsApp native forwarding (copyNForward) when available — faster and the '
            'result is marked "Forwarded" — and falls back to a manual re-send if it cannot. CRITICAL RULE: '
            'NEVER call this tool before the media is REALLY present in the current message/reply — if the '
            'user says "I will send it in a moment" or the media is not visible in the context yet, WAIT '
            'until the media is actually received (it shows up as a new message), then call this tool. Do '
            'not assume/guess that the media is already there. If this tool fails (e.g. no media is '
            'attached), do NOT invent a claim that it was "already sent" — say as it is that it failed and '
            "why. For the recipient's safety, this tool AUTOMATICALLY refuses to forward documents with "
            'executable/potentially virus extensions (.exe/.apk/.bat/.js/.vbs/etc) — that is not a bug, it '
            'is intentional.'
        ),
        'parameters': {
            'target': {
                'type': 'string',
                'description': 'WA number (example: 628123456789) or destination group JID, same format as send_message.',
                'required': True,
            },
            'caption': {
                'type': 'string',
                'description': (
                    'Optional caption text that accompanies the media (does not apply to stickers, and does not '
                    "apply when the forward goes through the native copyNForward path — the media's original caption "
                    'is kept on that path).'
                ),
                'required': False,
            },
        },
        'execute': _exec_forward_media,
    },
    {
        'name': 'reply_now',
        'description': (
            'Send one extra message RIGHT NOW to the chat currently being handled, without ending the '
            'process. Use it when you need to send more than 1 message within one reply — for example giving '
            'a short update before running a tool that takes a long time (download, install, etc), or '
            'splitting a long answer into several messages so it is easier to read. Do not use it for the '
            'last/closing message — the normal reply text at the end is already sent automatically as the '
            'final message.'
        ),
        'parameters': {
            'text': {
                'type': 'string',
                'description': 'The message content to send now',
                'required': True,
            },
        },
        'execute': _exec_reply_now,
    },
    {
        'name': 'send_rich_reply',
        'description': (
            'Send a text reply to the user, with the sources (if any) shown as link buttons under the '
            'message (native WhatsApp button, opened via the in-app webview) — NOT inline links in the text. '
            'MUST be used as the FINAL reply after search_web when there are relevant sources — see rule 13. '
            'Do NOT use it for ordinary replies without sources.'
        ),
        'parameters': {
            'body': {
                'type': 'string',
                'description': (
                    'The COMPLETE answer as ordinary natural text (you may use *bold*/bullet "-", BUT do NOT write '
                    'any link/markdown [text](url) here -- all links appear separately as buttons under the message '
                    'through the citations parameter, they are not inserted into this text).'
                ),
                'required': True,
            },
            'citations': {
                'type': 'array',
                'description': (
                    'List of sources to show as link buttons under the reply. Each item: {url: "source url", title: '
                    '"short button label, optional -- if empty the domain name is used automatically, e.g. '
                    '\'cnnindonesia.com\'"}. At most 5 buttons are shown (the rest are cut off if there are more). '
                    'Leave empty/an empty array if there is no relevant source (sent without buttons).'
                ),
                'required': False,
            },
        },
        'execute': _exec_send_rich_reply,
    },
]
