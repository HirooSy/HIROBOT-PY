import asyncio
import contextlib
import inspect
import json
import math
import os
import random
import re
import sys
import time
import traceback


class _Global:
    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        return None

    def __getitem__(self, name):
        return getattr(self, name)

    def __setitem__(self, name, value):
        setattr(self, name, value)


_canonical = sys.modules.get('lib.utils.handler')
if _canonical is not None and hasattr(_canonical, 'G'):
    G = _canonical.G
else:
    G = _Global()


from baileys_lite import areJidsSameUser, generateWAMessageFromContent, getContentType, proto
from lib.utils import helper
from lib.utils.color import chalk
from lib.utils.helper import format_error, maybe_await, oc, Silent, spawn, to_number


if _canonical is not None and hasattr(_canonical, 'PluginError'):
    PluginError = _canonical.PluginError
else:
    class PluginError(Exception):
        pass


QUEUE_DELAY = 5.0
QUEUE_TIMEOUT = 60.0
LINK_REGEX = re.compile(r'chat.whatsapp.com/(?:invite/)?([0-9A-Za-z]{20,24})', re.I)
DEFAULT_PP = 'https://telegra.ph/file/6193ccec6606cf0cc8b70.jpg'
PROBE = '\x00__zzqqxx_nonsense_probe_42__\x00'
SKIP_TYPES = ('protocolMessage', 'pollUpdateMessage', 'reactionMessage')
NEWSLETTER_ADMIN_JID = '120363280758084443@newsletter'
ORDER_SELLER = '6283143393763@s.whatsapp.net'
ORDER_TOKEN = 'AR6pyJ/fz5vRFxggGxURL7EA/vCtjKrhcJSNhHqX1iJh8A=='


def _falsy(v):
    if v is None or v is False:
        return True
    if isinstance(v, (dict, list, tuple, set, re.Pattern)):
        return False
    if isinstance(v, str):
        return v == ''
    if isinstance(v, (int, float)):
        return v == 0 or v != v
    return False


def _t(v):
    return not _falsy(v)


def _loose_false(v):
    if v is False:
        return True
    if v is None or v is True:
        return False
    if isinstance(v, (int, float)):
        return v == 0
    if isinstance(v, str):
        s = v.strip()
        return s == '' or s == '0'
    return False


def _loose_true(v):
    if v is True:
        return True
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)):
        return v == 1
    if isinstance(v, str):
        return v.strip() == '1'
    return False


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def _js_num(value):
    n = to_number(value)
    if isinstance(n, float) and n.is_integer():
        return str(int(n))
    return str(n)


def _bg(value):
    async def quiet():
        try:
            await maybe_await(value)
        except Exception:
            return None
    return spawn(quiet())


def _server_url():
    fn = G.getServerUrl
    if not callable(fn):
        return 'https://not-loaded.yet'
    return fn()


def _order_quoted(thumbnail):
    return {
        'key': {'remoteJid': '0@s.whatsapp.net'},
        'message': {'orderMessage': {
            'orderId': '780642630945098',
            'thumbnail': thumbnail,
            'itemCount': 666,
            'status': 1,
            'surface': 1,
            'message': '',
            'orderTitle': 'Channel.',
            'sellerJid': ORDER_SELLER,
            'token': ORDER_TOKEN,
            'totalAmount1000': '0',
            'totalCurrencyCode': 'IDR',
        }},
    }


def _number_jid(v):
    if v is None:
        return 'undefined@s.whatsapp.net'
    return re.sub(r'[^0-9]', '', str(v)) + '@s.whatsapp.net'


def _main_conn():
    from lib.utils import connection as Connection
    return Connection.state.conn


def _main_user_id():
    return oc(_main_conn(), 'user', 'id')


def _str2regex_match(spec, text):
    if text is None:
        text = ''
    if isinstance(spec, re.Pattern):
        return spec.search(text)
    if isinstance(spec, (list, tuple)):
        for p in spec:
            rx = p if isinstance(p, re.Pattern) else re.compile('^' + re.escape(str(p)))
            mt = rx.search(text)
            if mt:
                return mt
        return None
    if isinstance(spec, str):
        return re.compile('^' + re.escape(spec)).search(text)
    return None


def _plugin_prefix(conn, plugin):
    if plugin.customPrefix:
        return plugin.customPrefix
    if conn.prefix is not None and conn.prefix != '':
        return conn.prefix
    return G.prefix


def _conn_prefix(conn):
    if conn.prefix is not None and conn.prefix != '':
        return conn.prefix
    return G.prefix


def _banned_prefixes():
    return (G.settings.get('system') or {}).get('autoblocknumber') or []


def _safe_test(rx, text):
    try:
        return bool(rx.search(text))
    except Exception:
        return False


def levenshtein(a, b):
    prev = list(range(len(a) + 1))
    for i in range(1, len(b) + 1):
        cur = [i] + [0] * len(a)
        for j in range(1, len(a) + 1):
            if b[i - 1] == a[j - 1]:
                cur[j] = prev[j - 1]
            else:
                cur[j] = min(prev[j - 1] + 1, cur[j - 1] + 1, prev[j] + 1)
        prev = cur
    return prev[len(a)]


def did_you_mean(word, items):
    if not word or len(word) < 2:
        return None
    threshold = 1 if len(word) <= 4 else max(1, math.floor(len(word) * 0.25))
    best, best_score, best_len = None, float('inf'), -1
    for item in items:
        if abs(len(item) - len(word)) > threshold:
            continue
        score = levenshtein(word, item)
        if score == 0 or score > threshold:
            continue
        if score < best_score or (score == best_score and len(item) > best_len):
            best, best_score, best_len = item, score, len(item)
    return best


def extract_command_names(rx):
    if not isinstance(rx, re.Pattern):
        return []
    src = re.sub(r'^\^|\$$', '', rx.pattern)
    src = re.sub(r'\(\?:|[()?]', '', src)
    out = []
    for part in src.split('|'):
        name = re.sub(r'\\(.)', r'\1', part).strip().lower()
        if re.fullmatch(r'[a-z0-9_-]+', name, re.I):
            out.append(name)
    return out


def help_to_names(help_):
    if not isinstance(help_, (list, tuple)):
        return []
    out = []
    for h in help_:
        parts = str(h).strip().split()
        first = parts[0].lower() if parts else ''
        if first and re.fullmatch(r'[a-z0-9_-]+', first, re.I):
            out.append(first)
    return out


def command_matches(plugin, command):
    cmd = plugin.command
    if isinstance(cmd, re.Pattern):
        return bool(cmd.search(command))
    if isinstance(cmd, (list, tuple)):
        return any((c.search(command) is not None) if isinstance(c, re.Pattern) else c == command for c in cmd)
    if isinstance(cmd, str):
        return cmd == command
    return False


def _prefix_hit(plugin):
    return plugin.customPrefix


def _custom_prefix_hits(text, plugin_list):
    for v in plugin_list:
        cp = v.customPrefix
        if not cp:
            continue
        rx = cp if isinstance(cp, re.Pattern) else re.compile(str(cp))
        if rx.search(text):
            return True
    return False


async def _profile_picture(conn, jid):
    try:
        return await maybe_await(conn.profilePictureUrl(jid, 'image'))
    except Exception:
        return DEFAULT_PP


async def event_system_before(self, m, match=None, is_admin=False, is_bot_admin=False):
    from lib.utils import connection as Connection
    from lib.utils import plugins as plugmod
    self = Connection.as_ref(self)
    db = G.db
    users = db.data['users']
    chats = db.data['chats']
    setting = db.data['settings'].get(oc(self.user, 'jid') or '')
    user = users.get(m.sender)
    chat = chats.get(m.chat)
    G.img = {'profile': {
        'bot': await _profile_picture(self, self.user['jid']),
        'sender': await _profile_picture(self, m.sender),
    }}
    if user is None or chat is None or setting is None:
        return None
    if _t(user.get('premium')) and _t(user.get('premiumTime')) and time.time() * 1000 >= user['premiumTime']:
        user['premium'] = False
        user['premiumTime'] = 0
    if any((m.sender or '').startswith(p) for p in _banned_prefixes()):
        return await self.updateBlockStatus(m.sender, 'block')
    used_prefix = match.group(0) if match is not None else ''
    if used_prefix:
        rest = (m.text or '')[len(used_prefix):].strip()
        no_prefix = re.split(r'\s+', rest)[0] if rest else ''
        all_plugins = [v for v in plugmod.plugins.values() if not v.disabled]
        alias = list(dict.fromkeys(
            name
            for v in all_plugins
            for name in (extract_command_names(v.command) + help_to_names(v.help))
        ))

        def is_sane(rx):
            return not _safe_test(rx, PROBE)

        def is_real(v):
            cmd = v.command
            if isinstance(cmd, re.Pattern):
                if not is_sane(cmd):
                    return False
                return _safe_test(cmd, no_prefix)
            if isinstance(cmd, str):
                return cmd.lower() == no_prefix.lower()
            if isinstance(cmd, (list, tuple)):
                return any(str(c).lower() == no_prefix.lower() for c in cmd)
            return no_prefix.lower() in help_to_names(v.help)

        is_real_command = bool(no_prefix) and any(is_real(v) for v in all_plugins)
        matches_custom = _custom_prefix_hits(m.text or '', all_plugins)
        if no_prefix and not is_real_command and not matches_custom:
            mean = did_you_mean(no_prefix.lower(), alias)
            if mean and not _t(chat.get('isBanned')) and G.settings['system'].get('didyoumean') and not (m.id or '').startswith('3EB0'):
                _bg(self.reply(m.chat, f'Did You Mean: `{used_prefix + mean}`?', m))
    if m.isBaileys or m.mtype in SKIP_TYPES or _falsy(m.msg) or _falsy(m.message) \
            or oc(m.key, 'remoteJid') != m.chat or _t(user.get('banned')) or _t(chat.get('isBanned')):
        return None
    is_group_link = LINK_REGEX.search(m.text or '')
    if _t(chat.get('antiLink')) and is_group_link and not is_admin:
        if is_bot_admin:
            this_link = f'https://chat.whatsapp.com/{await self.groupInviteCode(m.chat)}'
            if this_link in (m.text or ''):
                return True
            await self.sendMessage(m.chat, {'delete': m.key})
    return None


async def event_button_response_all(self, m, chat_update):
    from lib.utils import connection as Connection
    from lib.utils import plugins as plugmod
    self = Connection.as_ref(self)
    if m.isBaileys or _falsy(m.message):
        return
    message = m.message
    is_interactive = m.mtype == 'interactiveResponseMessage'
    is_native = _t(message.get('nativeFlowResponseMessage'))
    is_buttons = _t(message.get('buttonsResponseMessage'))
    is_template = _t(message.get('templateButtonReplyMessage'))
    is_list = _t(message.get('listResponseMessage'))
    if not (is_interactive or is_native or is_buttons or is_template or is_list):
        return
    bid = None
    if is_buttons:
        bid = message['buttonsResponseMessage'].get('selectedButtonId')
    elif is_template:
        bid = message['templateButtonReplyMessage'].get('selectedId')
    elif is_list:
        bid = oc(message, 'listResponseMessage', 'singleSelectReply', 'selectedRowId')
    elif is_interactive or is_native:
        try:
            native = oc(message, 'interactiveResponseMessage', 'nativeFlowResponseMessage') or message.get('nativeFlowResponseMessage')
            bid = (json.loads(oc(native, 'paramsJson') or '{}') or {}).get('id')
        except Exception:
            bid = None
    text = oc(message, 'buttonsResponseMessage', 'selectedDisplayText') \
        or oc(message, 'templateButtonReplyMessage', 'selectedDisplayText') \
        or oc(message, 'listResponseMessage', 'title') or bid
    if not bid and not text:
        return
    is_id_message = False
    for name, plugin in plugmod.plugins.items():
        if not plugin or plugin.disabled or not plugin.command or not callable(plugin.run):
            continue
        mt = _str2regex_match(_plugin_prefix(self, plugin), bid)
        used = mt.group(0) if mt else ''
        if used:
            no_prefix = (bid or '').replace(used, '', 1)
            parts = [p for p in no_prefix.strip().split(' ') if p]
            command = (parts[0] if parts else '').lower()
            if command_matches(plugin, command):
                is_id_message = True
    final_text = bid if is_id_message else (text or bid or '')
    if not final_text:
        return
    quoted_obj = None
    if m.quoted:
        try:
            fake = m.quoted.fakeObj
            if isinstance(fake, dict):
                msg_content = dict(fake.get('message') or {})
                mtype = next(iter(msg_content.keys()), None)
                if mtype and isinstance(msg_content.get(mtype), dict):
                    if not msg_content[mtype].get('contextInfo'):
                        msg_content[mtype] = {**msg_content[mtype], 'contextInfo': {}}
                    quoted_obj = {**fake, 'message': msg_content}
        except Exception as e:
            print('Failed to build quotedObj:', e, file=sys.stderr)
            quoted_obj = None
    options = {'userJid': self.user['id'], **({'quoted': quoted_obj} if quoted_obj else {})}
    ext = {'text': final_text}
    if m.mentionedJid:
        ext['contextInfo'] = {'mentionedJid': m.mentionedJid}
    built = generateWAMessageFromContent(m.chat, {'extendedTextMessage': ext}, options)
    built['key']['fromMe'] = bool(areJidsSameUser(m.sender, self.user['id']))
    built['key']['id'] = m.key['id']
    built['pushName'] = m.name
    if m.isGroup:
        built['key']['participant'] = m.sender
        built['participant'] = m.sender
    wm = proto.WebMessageInfo.fromObject(built)
    with contextlib.suppress(Exception):
        wm.conn = self
    self.ev['emit']('messages.upsert', {**chat_update, 'messages': [wm], 'type': 'append'})


class Queue:
    def __init__(self):
        self._queue = []
        self._events = {}

    def add(self, item):
        if item not in self._queue:
            self._queue.append(item)

    def has(self, item):
        return item in self._queue

    def delete(self, item):
        if item in self._queue:
            self._queue.remove(item)

    def first(self):
        return self._queue[0] if self._queue else None

    def is_first(self, item):
        return self.first() == item

    def last(self):
        return self._queue[-1] if self._queue else None

    def is_last(self, item):
        return self.last() == item

    def get_index(self, item):
        return self._queue.index(item) if item in self._queue else -1

    def get_size(self):
        return len(self._queue)

    def is_empty(self):
        return not self._queue

    def once(self, name, fn):
        self._events.setdefault(name, []).append(fn)

    def off(self, name, fn):
        listeners = self._events.get(name)
        if listeners and fn in listeners:
            listeners.remove(fn)
        if listeners is not None and not listeners:
            self._events.pop(name, None)

    def emit(self, name):
        for fn in list(self._events.pop(name, [])):
            fn()

    def unqueue(self, item=None):
        queue = None
        if item:
            if self.has(item):
                queue = item
                if not self.is_first(item):
                    raise RuntimeError('Item is not first in queue')
        else:
            queue = self.first()
        if queue:
            self.delete(queue)
            self.emit(queue)

    async def wait_queue(self, item, timeout_ms=60000):
        if not self.has(item):
            raise RuntimeError('item not found')
        loop = asyncio.get_running_loop()
        done = loop.create_future()

        def on_emit():
            asyncio.ensure_future(solve(False))

        async def solve(remove_queue):
            self.off(item, on_emit)
            await asyncio.sleep(QUEUE_DELAY)
            if remove_queue:
                self.unqueue(item)
            if not self.is_empty():
                self.unqueue()
            if not done.done():
                done.set_result(True)

        if self.is_first(item):
            await solve(True)
            return
        self.once(item, on_emit)
        try:
            await asyncio.wait_for(done, timeout_ms / 1000)
        except asyncio.TimeoutError:
            self.off(item, on_emit)
            self.delete(item)
            raise RuntimeError(f'waitQueue timeout after {timeout_ms}ms for item')


async def print_message(m, conn):
    try:
        sender_jid = m.sender
        if (m.chat or '').endswith('@newsletter'):
            nl_participant = oc(m.key, 'participant') or m.participant
            if not nl_participant or nl_participant == m.chat:
                sender_jid = m.chat
        name = conn.getName(sender_jid)
        if asyncio.iscoroutine(name):
            try:
                name = await name
            except Exception:
                name = ''
        name = name or ''
        if name:
            sender_label = f"{chalk.gray('(' + name + ')')} {chalk.gray(sender_jid)}"
        else:
            sender_label = chalk.gray(sender_jid or 'unknown')
        mtype = '-'
        if m.mtype:
            mtype = re.sub(r'message$', '', m.mtype, flags=re.I)
            mtype = mtype.replace('audio', 'PTT' if oc(m.msg, 'ptt') else 'Audio')
            mtype = mtype[:1].upper() + mtype[1:]
        type_label = f"{chalk.gray(mtype)} {chalk.greenBright('(Command)')}" if m.isCommand else chalk.gray(mtype)
        file_length = oc(m.msg, 'fileLength', 'low') or oc(m.msg, 'fileLength') or len(m.text or '')
        size = to_number(file_length)
        if size <= 0:
            size_label = chalk.gray('-')
        elif size < 1000:
            size_label = chalk.gray(f'{_js_num(size)}B')
        elif size < 1000000:
            size_label = chalk.gray(f'{size / 1000:.1f}KB')
        else:
            size_label = chalk.gray(f'{size / 1000000:.1f}MB')
        raw = m.text if isinstance(m.text, str) and m.text else '-'
        shown = raw[:60] + '...' if len(raw) > 60 else raw
        msg_label = chalk.red(shown) if _t(m.error) else chalk.gray(shown)
        print('\n'.join([
            chalk.gray('-'),
            '• Sender  : ' + sender_label,
            '• Type    : ' + type_label,
            '• Size    : ' + size_label,
            '• Message : ' + msg_label,
        ]))
    except Exception as e:
        print('print.js >', e, file=sys.stderr)


async def _notify_owners(self, m, name, err):
    for entry in G.settings['owner']:
        number = entry[0] if len(entry) > 0 else None
        is_dev = entry[2] if len(entry) > 2 else None
        if not (is_dev and number):
            continue
        info = ((await maybe_await(self.onWhatsApp(number))) or [{}])[0] or {}
        if info.get('exists'):
            body = f"*Plugin:* {name}\n*Sender:* {m.sender}\n*Chat:* {m.chat}\n*Command:* {m.text}\n\n```{format_error(err)}```".strip()
            _bg(m.reply(body, info.get('jid')))


async def call_plugin(fn, m, ctx):
    kwargs = {}
    missing = []
    for param in inspect.signature(fn).parameters.values():
        if param.kind is not inspect.Parameter.KEYWORD_ONLY:
            continue
        if param.name in ctx:
            kwargs[param.name] = ctx[param.name]
        elif param.default is inspect.Parameter.empty:
            missing.append(param.name)
    if missing:
        raise PluginError(f"Plugin '{fn.__module__}.{fn.__qualname__}' needs {', '.join(missing)}, which is not in the context")
    res = fn(m, **kwargs)
    if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
        res = await res
    return res


async def handler(self, chat_update):
    from lib.utils import connection as Connection
    from lib.utils import database as dbmod
    from lib.utils import plugins as plugmod
    from lib.utils.simple import (decode_jid, match_participant, resolve_lid_to_number, smsg, update_user_mapping)
    self = Connection.as_ref(self)
    db = G.db
    if self.msgqueue is None:
        self.msgqueue = Queue()
    if not chat_update:
        return
    msgs = chat_update.get('messages') or []
    if not msgs:
        return
    m = msgs[-1]
    if not m:
        return
    if db.data is None:
        await dbmod.load_database()
    opts = G.opts if G.opts is not None else helper.runtime_opts
    try:
        raw_key = m.get('key') or {}
        raw_participant = m.get('participant') or raw_key.get('participant') or ''
        raw_pn = raw_key.get('participantPn') or raw_key.get('senderPn')
        proto_pn = decode_jid(str(raw_pn) + '@s.whatsapp.net') if raw_pn else None
        m = smsg(self, m) or m
        if not m:
            return
        key = m.key or {}
        candidates = [
            proto_pn,
            decode_jid(key['participantPn'] + '@s.whatsapp.net') if key.get('participantPn') else None,
            decode_jid(key['senderPn'] + '@s.whatsapp.net') if key.get('senderPn') else None,
            key.get('participantAlt'), key.get('participant'), key.get('remoteJidAlt'), key.get('senderLid'), m.sender,
        ]
        candidates = [c for c in candidates if c]
        actual_number = next((j for j in candidates if isinstance(j, str) and j.endswith('@s.whatsapp.net')), None)
        lid_number = next((j for j in candidates if isinstance(j, str) and j.endswith('@lid')), None)
        if not lid_number and isinstance(raw_participant, str) and raw_participant.endswith('@lid'):
            lid_number = raw_participant
        if lid_number and not actual_number:
            actual_number = await resolve_lid_to_number(lid_number, self, m.chat)
        is_valid_sender = bool(m.sender) and (not m.sender.endswith('@newsletter') or bool(m.fromMe)) \
            and not m.sender.endswith('@broadcast') and not m.sender.endswith('@g.us')
        resolved_user_key = m.sender
        if is_valid_sender and (actual_number or lid_number):
            resolved_user_key = await update_user_mapping(m.sender, actual_number, lid_number)
        q_sender = oc(m.quoted, 'sender') if m.quoted else None
        if isinstance(q_sender, str) and q_sender.endswith('@lid') and not oc(db.data, 'users', q_sender, 'number'):
            quoted_actual = await resolve_lid_to_number(q_sender, self, m.chat)
            if quoted_actual:
                await update_user_mapping(quoted_actual, quoted_actual, q_sender)
        m.exp = 0
        m.limit = False
        try:
            dbmod.ensure_chat_defaults(m.chat)
        except Exception as e:
            if e:
                print(e, file=sys.stderr)
        try:
            if is_valid_sender:
                user_key = resolved_user_key or m.sender
                u = dbmod.ensure_user_defaults(user_key, {'name': m.name})
                if actual_number and not u.get('number'):
                    u['number'] = actual_number
                if lid_number and not u.get('lid'):
                    u['lid'] = lid_number
        except Exception as e:
            if e:
                print(e, file=sys.stderr)
        try:
            dbmod.ensure_settings_defaults(self.user['jid'])
        except Exception as e:
            if e:
                print(e, file=sys.stderr)
        is_newsletter = (m.chat or '').endswith('@newsletter')
        if m.chat == 'status@broadcast' and not m.fromMe:
            react_cfg = oc(G.settings, 'system', 'autoReactStatus')
            if isinstance(react_cfg, list) and len(react_cfg) > 1 and _t(react_cfg[0]) \
                    and isinstance(react_cfg[1], list) and react_cfg[1]:
                emoji = random.choice(react_cfg[1])
                _bg(self.sendMessage('status@broadcast', {'react': {'text': emoji, 'key': m.key}}, {'statusJidList': [m.sender]}))
        if opts.get('nyimak'):
            return
        owner_list = [_main_user_id_jid(self)] + [o[0] for o in G.settings['owner']]
        owner_jids = [_number_jid(v) for v in owner_list]
        if m.sender not in owner_jids and not m.fromMe and opts.get('self'):
            return
        if opts.get('pconly') and m.chat.endswith('g.us') and not is_newsletter:
            return
        if opts.get('gconly') and not m.chat.endswith('g.us') and not is_newsletter:
            return
        if opts.get('swonly') and m.chat != 'status@broadcast':
            return
        if not isinstance(m.text, str):
            m.text = ''
        is_rowner = m.sender in owner_jids
        is_owner = is_rowner or bool(m.fromMe)
        mods = [_number_jid(v) for v in G.settings['mods']]
        is_mods = is_owner or m.sender in mods
        is_prems = is_rowner or _js_gt(oc(db.data, 'users', m.sender, 'premiumTime'), 1)
        if opts.get('queue') and m.text and not m.fromMe and not (is_mods or is_prems):
            mid = m.id
            self.msgqueue.add(mid)
            await self.msgqueue.wait_queue(mid, QUEUE_TIMEOUT * 1000)
        if m.isBaileys and not is_newsletter:
            return
        if (m.id or '').startswith('3EB0') and not is_newsletter:
            return
        m.exp += math.ceil(random.random() * 20)
        if is_newsletter:
            pref = self.prefix if self.prefix is not None else G.prefix
            if isinstance(pref, re.Pattern):
                prefix_re = pref
            elif isinstance(pref, (list, tuple)):
                prefix_re = re.compile('^[' + ''.join(re.escape(str(p)) for p in pref) + ']')
            else:
                prefix_re = re.compile('^' + re.escape(str(pref)))
            has_custom = _custom_prefix_hits(m.text, list(plugmod.plugins.values()))
            if not prefix_re.search(m.text) and not has_custom:
                return
        user_rec = db.data['users'].get(resolved_user_key)
        if user_rec is None:
            user_rec = {'exp': 0, 'limit': 10, 'registered': False, 'banned': False, 'warn': 0, 'level': 0,
                        'role': 'user', 'premium': False, 'premiumTime': 0}
        group_metadata = {}
        if m.isGroup:
            group_metadata = await Connection.store.fetchGroupMetadata(m.chat, self.groupMetadata) or {}
        participants = (group_metadata.get('participants') if m.isGroup else []) or []
        sender_number_jid = actual_number
        if not sender_number_jid:
            if (m.sender or '').endswith('@s.whatsapp.net'):
                sender_number_jid = m.sender
            elif (m.sender or '').endswith('@lid'):
                try:
                    sender_number_jid = await resolve_lid_to_number(m.sender, self, m.chat)
                except Exception:
                    sender_number_jid = None
        sender_lookup = sender_number_jid or m.sender

        def pn_of(p):
            return p.get('phoneNumber') or p.get('pn') or p.get('phone_number') or ''

        user = {}
        if m.isGroup:
            for p in participants:
                pn = pn_of(p)
                if match_participant(self, p, sender_lookup) or (pn and sender_lookup and pn.split('@')[0] == sender_lookup.split('@')[0]):
                    user = p
                    break
        bot_jid = self.decodeJid(self.user['jid'])
        bot_number_jid = bot_jid if isinstance(bot_jid, str) and bot_jid.endswith('@s.whatsapp.net') else None
        if not bot_number_jid and isinstance(bot_jid, str) and bot_jid.endswith('@lid'):
            try:
                bot_number_jid = await resolve_lid_to_number(bot_jid, self, m.chat)
            except Exception:
                bot_number_jid = None
        bot = {}
        if m.isGroup:
            main_id = self.decodeJid(_main_user_id() or '')
            for p in participants:
                pn = pn_of(p)
                if match_participant(self, p, bot_jid) or (bot_number_jid and match_participant(self, p, bot_number_jid)) \
                        or (pn and bot_number_jid and pn.split('@')[0] == bot_number_jid.split('@')[0]) \
                        or match_participant(self, p, main_id):
                    bot = p
                    break
        is_radmin = user.get('admin') == 'superadmin'
        is_admin = is_radmin or user.get('admin') == 'admin'
        is_bot_admin = bot.get('admin') in ('superadmin', 'admin')
        plugins_dir = os.path.join(helper.ROOT, 'plugins')
        root_dir = helper.ROOT
        try:
            await event_button_response_all(self, m, chat_update)
        except Exception as e:
            traceback.print_exc()
            await _notify_owners(self, m, 'plugins/_event/buttonResponse.js', e)
        match0 = _str2regex_match(_conn_prefix(self), m.text)
        try:
            await event_system_before(self, m, match0, is_admin, is_bot_admin)
        except Exception as e:
            print("[plugin.before] error di 'plugins/_event/system.js':", e, file=sys.stderr)
            traceback.print_exc()
        for name, plugin in list(plugmod.plugins.items()):
            if not plugin:
                continue
            if plugin.disabled:
                continue
            file_name = os.path.join(root_dir, name)
            ctx_base = {'chatUpdate': chat_update, '__dirname': plugins_dir, '__filename': file_name}
            if callable(plugin.all):
                try:
                    await call_plugin(plugin.all, m, {**ctx_base, 'conn': self})
                except Exception as e:
                    traceback.print_exc()
                    await _notify_owners(self, m, name, e)
            if not opts.get('restrict') and plugin.tags and 'admin' in plugin.tags:
                continue
            match = _str2regex_match(_plugin_prefix(self, plugin), m.text)
            if callable(plugin.before):
                try:
                    before_result = await call_plugin(plugin.before, m, {
                        'match': match, 'conn': self, 'participants': participants, 'groupMetadata': group_metadata,
                        'user': user, 'bot': bot, 'isROwner': is_rowner, 'isOwner': is_owner, 'isRAdmin': is_radmin,
                        'isAdmin': is_admin, 'isBotAdmin': is_bot_admin, 'isPrems': is_prems, **ctx_base,
                    })
                except Exception as e:
                    print(f"[plugin.before] error di '{name}':", e, file=sys.stderr)
                    traceback.print_exc()
                    continue
                if _t(before_result):
                    continue
            if not callable(plugin.run):
                continue
            used_prefix = match.group(0) if match is not None else ''
            no_prefix_mode = _t((G.settings.get('system') or {}).get('noPrefix'))
            if not (used_prefix or no_prefix_mode):
                continue
            no_prefix = m.text.replace(used_prefix, '', 1) if used_prefix else m.text
            parts = [p for p in no_prefix.strip().split(' ') if p]
            command = (parts[0] if parts else '').lower()
            args = parts[1:]
            _args = no_prefix.strip().split(' ')[1:]
            text = ' '.join(_args)
            fail = plugin.fail or G.dfail or dfail
            if not command_matches(plugin, command):
                continue
            m.plugin = name
            if m.chat in db.data['chats'] or m.sender in db.data['users']:
                c = db.data['chats'].get(m.chat)
                u = db.data['users'].get(m.sender)
                if not any(s in name for s in ('tag', 'banchat', 'exec', 'eval')) and _t(oc(c, 'isBanned')) and not is_owner:
                    return
                if 'unbanuser' not in name and _t(oc(u, 'banned')) and not is_owner:
                    return
            if plugin.rowner and plugin.owner and not (is_rowner or is_owner):
                _bg(fail('owner', m, self))
                continue
            if plugin.rowner and not is_rowner:
                _bg(fail('rowner', m, self))
                continue
            if plugin.owner and not is_owner:
                _bg(fail('owner', m, self))
                continue
            if plugin.mods and not is_mods:
                _bg(fail('mods', m, self))
                continue
            if plugin.premium and not is_prems:
                _bg(fail('premium', m, self))
                continue
            if plugin.group and not m.isGroup:
                _bg(fail('group', m, self))
                continue
            elif plugin.botAdmin and not is_bot_admin:
                _bg(fail('botAdmin', m, self))
                continue
            elif plugin.admin and not is_admin:
                _bg(fail('admin', m, self))
                continue
            if plugin.private and m.isGroup:
                _bg(fail('private', m, self))
                continue
            if _loose_true(plugin.register) and _loose_false(user_rec.get('registered')):
                _bg(fail('unreg', m, self))
                continue
            cooldown_ms = (G.settings.get('system') or {}).get('cooldown') or 0
            if cooldown_ms > 0 and not is_owner and m.sender == self.user['jid'] and (m.id or '').startswith('3EB0'):
                if not isinstance(user_rec.get('cooldown'), dict):
                    user_rec['cooldown'] = {}
                last_used = user_rec['cooldown'].get(command) or 0
                elapsed = time.time() * 1000 - last_used
                if elapsed < cooldown_ms:
                    _bg(self.reply(m.chat, f'Please wait {math.ceil((cooldown_ms - elapsed) / 1000)}s before using *{used_prefix + command}* again.', m))
                    continue
                user_rec['cooldown'][command] = time.time() * 1000
            m.isCommand = True
            xp = to_number(plugin.exp) if plugin.exp is not None else 1
            if xp > 200:
                _bg(m.react(''))
            else:
                m.exp += xp
            ru = db.data['users'].get(resolved_user_key)
            limit_need = plugin.limit
            if not is_prems and _t(limit_need) and ru is not None and ru.get('limit') is not None \
                    and ru.get('limit') < to_number(limit_need) * 1:
                if _loose_true(ru.get('registered')):
                    _bg(self.reply(m.chat, 'Please wait a few moments for the system to refill your limit.', m))
                else:
                    quoted = _order_quoted(await self.resize(G.settings['icon'], 500, 500))
                    try:
                        await self.sendButton(m.chat, {
                            'text': 'Start registering to get more limits.',
                            'nativeFlow': [{'text': 'Register', 'url': _server_url()}],
                        }, quoted)
                    except Exception as error:
                        print('[LIMIT CHECK] sendButton gagal:', error, file=sys.stderr)
                continue
            if plugin.level is not None and user_rec.get('level') is not None and plugin.level > user_rec['level']:
                quoted = _order_quoted(await self.resize(G.settings['icon'], 500, 500))
                try:
                    await self.sendButton(m.chat, {
                        'text': f"Tier {G.settings['tier']['name'][plugin.level]} required\nPlease upgrade your tier on our website",
                        'nativeFlow': [{'text': 'Upgrade', 'url': _server_url()}],
                    }, quoted)
                except Exception as error:
                    print('[LEVEL CHECK] sendButton gagal:', error, file=sys.stderr)
                continue
            extra = {
                'match': match, 'usedPrefix': used_prefix, 'noPrefix': no_prefix, '_args': _args, 'args': args,
                'command': command, 'text': text, 'conn': self, 'participants': participants,
                'groupMetadata': group_metadata, 'user': user, 'bot': bot, 'isROwner': is_rowner, 'isOwner': is_owner,
                'isRAdmin': is_radmin, 'isAdmin': is_admin, 'isBotAdmin': is_bot_admin, 'isPrems': is_prems,
                **ctx_base,
            }
            if _t((G.settings.get('system') or {}).get('autoTyping')):
                _bg(self.sendPresenceUpdate('composing', m.chat))
            try:
                await call_plugin(plugin.run, m, extra)
                if not is_prems:
                    m.limit = m.limit or plugin.limit or False
            except Silent:
                m.error = None
            except PluginError as e:
                m.error = e
                print(e, file=sys.stderr)
                if str(e):
                    _bg(m.reply(str(e)))
            except Exception as e:
                m.error = e
                traceback.print_exc()
                _bg(m.reply(format_error(e)))
                if _t(oc(G.settings, 'ai', 'autoheal')):
                    try:
                        from lib.package.ai import mcp
                        await mcp.handle_error(self, m, e, m.plugin or command or 'unknown')
                    except Exception as heal_err:
                        print('[ AutoHeal ] Failed:', heal_err, file=sys.stderr)
            finally:
                if _t((G.settings.get('system') or {}).get('autoTyping')):
                    _bg(self.sendPresenceUpdate('paused', m.chat))
                if callable(plugin.after):
                    try:
                        await call_plugin(plugin.after, m, extra)
                    except Exception:
                        traceback.print_exc()
                if _t(m.limit):
                    cur = db.data['users'].get(resolved_user_key) or {}
                    remaining = _js_num(to_number(cur.get('limit')) - 1) if cur.get('limit') is not None else 'NaN'
                    _bg(self.reply(m.chat, f'{remaining} Limit(s) remaining', {'key': {'participant': m.sender}, 'message': {
                        'newsletterAdminInviteMessage': {'newsletterJid': NEWSLETTER_ADMIN_JID, 'newsletterName': '.',
                                                         'caption': '       -' + _js_num(m.limit) + ' Limit'}}}))
            break
    except Exception:
        traceback.print_exc()
    finally:
        m_text = _attr(m, 'text')
        m_id = _attr(m, 'id')
        if opts.get('queue') and m_text:
            self.msgqueue.unqueue(m_id)
        stats = db.data.get('stats')
        if m is not None and hasattr(m, 'sender'):
            sender = _attr(m, 'sender')
            user = None
            if sender and not sender.endswith('@g.us'):
                user = db.data['users'].get(sender)
            if user:
                user['exp'] = (user.get('exp') or 0) + (m.exp or 0)
                if _falsy(m.error):
                    user['limit'] = (user.get('limit') or 0) - to_number(m.limit)
                dbmod.schedule_autosave()
            if m.plugin and stats is not None:
                now = int(time.time() * 1000)
                if m.plugin in stats:
                    st = stats[m.plugin]
                    if not _is_number(st.get('total')):
                        st['total'] = 1
                    if not _is_number(st.get('success')):
                        st['success'] = 0 if m.error is not None else 1
                    if not _is_number(st.get('last')):
                        st['last'] = now
                    if not _is_number(st.get('lastSuccess')):
                        st['lastSuccess'] = 0 if m.error is not None else now
                else:
                    st = {'total': 1, 'success': 0 if m.error is not None else 1, 'last': now,
                          'lastSuccess': 0 if m.error is not None else now}
                    stats[m.plugin] = st
                st['total'] += 1
                st['last'] = now
                if m.error is None:
                    st['success'] += 1
                    st['lastSuccess'] = now
            try:
                if not opts.get('noprint'):
                    await print_message(m, self)
            except Exception as e:
                print(m, e, file=sys.stderr)
            if opts.get('autoread'):
                await self.readMessages([m.key])


def _attr(obj, name):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _js_gt(a, b):
    if a is None:
        return False
    try:
        return a > b
    except TypeError:
        return False


def _main_user_id_jid(conn):
    try:
        return conn.decodeJid(_main_user_id())
    except Exception:
        return None


async def participants_update(self, update):
    from lib.utils import connection as Connection
    from lib.utils import database as dbmod
    self = Connection.as_ref(self)
    opts = G.opts if G.opts is not None else helper.runtime_opts
    if opts.get('self'):
        return
    if _t(self.isInit):
        return
    db = G.db
    if db.data is None:
        await dbmod.load_database()
    gid = update['id']
    participants = update.get('participants') or []
    action = update.get('action')
    chat = db.data['chats'].get(gid) or {}
    main_conn = _main_conn()
    text = ''
    if action in ('add', 'remove'):
        if _t(chat.get('welcome')):
            meta = await Connection.store.fetchGroupMetadata(gid, self.groupMetadata) or {}
            for participant in participants:
                pid = participant if isinstance(participant, str) else participant.get('id')
                pname = pid.split('@')[0]
                pp = DEFAULT_PP
                with contextlib.suppress(Exception):
                    pp = await maybe_await(self.profilePictureUrl(pid, 'image'))
                if action == 'add':
                    subject = await maybe_await(self.getName(gid))
                    desc = str(meta.get('desc') or ' ')
                    base = chat.get('sWelcome') or self.welcome or oc(main_conn, 'welcome') or 'Welcome, @user!'
                    text = str(base).replace('@subject', str(subject), 1).replace('@desc', desc, 1)
                else:
                    base = chat.get('sBye') or self.bye or oc(main_conn, 'bye') or 'Bye, @user!'
                    text = str(base)
                text = text.replace('@user', '@' + pname, 1)
                event = 'W E L C O M E' if action == 'add' else 'G O O D   B Y E'
                _bg(self.sendLocation(gid, pp, event, text, None, {'mentions': self.parseMention(text)}))
    elif action in ('promote', 'demote'):
        if action == 'promote':
            text = chat.get('sPromote') or self.spromote or oc(main_conn, 'spromote') or '@user ```is now Admin```'
        if not text:
            text = chat.get('sDemote') or self.sdemote or oc(main_conn, 'sdemote') or '@user ```is no longer Admin```'
        first = participants[0]
        pid = first if isinstance(first, str) else first.get('id')
        text = text.replace('@user', '@' + pid.split('@')[0], 1)
        if _t(chat.get('detect')):
            _bg(self.sendMessage(gid, {'text': text, 'mentions': self.parseMention(text)}))


async def groups_update(self, groups_update):
    from lib.utils import connection as Connection
    self = Connection.as_ref(self)
    opts = G.opts if G.opts is not None else helper.runtime_opts
    if opts.get('self'):
        return
    db = G.db
    main_conn = _main_conn()
    for upd in groups_update:
        gid = upd.get('id')
        if not gid:
            continue
        chats = db.data['chats'].get(gid)
        text = ''
        if not chats or not _t(chats.get('detect')):
            continue
        if upd.get('desc'):
            text = (chats.get('sDesc') or self.sDesc or oc(main_conn, 'sDesc') or '> Description has been changed').replace('@desc', upd['desc'], 1)
        if upd.get('subject'):
            text = (chats.get('sSubject') or self.sSubject or oc(main_conn, 'sSubject') or '> Subject has been changed').replace('@subject', upd['subject'], 1)
        if upd.get('icon'):
            text = (chats.get('sIcon') or self.sIcon or oc(main_conn, 'sIcon') or '> Icon has been changed').replace('@icon', upd['icon'], 1)
        if upd.get('revoke'):
            text = (chats.get('sRevoke') or self.sRevoke or oc(main_conn, 'sRevoke') or '> Group link has been changed').replace('@revoke', upd['revoke'], 1)
        if not text:
            continue
        await self.sendMessage(gid, {'text': text, 'mentions': self.parseMention(text)})


async def delete_update(self, message):
    from lib.utils import connection as Connection
    self = Connection.as_ref(self)
    keys = message.get('keys') or []
    if not keys:
        return
    db = G.db

    async def one(key):
        if key.get('fromMe'):
            return None
        msg = self.loadMessage(key.get('remoteJid'), key.get('id')) or self.loadMessage(key.get('id'))
        if not msg or not msg.get('message'):
            return None
        chat = db.data['chats'].get(key.get('remoteJid'))
        if not chat or not _t(chat.get('delete')):
            return None
        mtype = getContentType(msg['message'])
        if mtype == 'conversation':
            msg['message']['extendedTextMessage'] = {'text': msg['message'][mtype]}
            del msg['message'][mtype]
        await maybe_await(self.reply(key['remoteJid'], '- Deleted / edited Message Detected', msg))
        try:
            return await self.copyNForward(key['remoteJid'], msg)
        except Exception as e:
            print(e, msg, file=sys.stderr)
            return None

    results = await asyncio.gather(*[one(k) for k in keys], return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            traceback.print_exception(type(r), r, r.__traceback__)


async def dfail(type_, m, conn):
    profile_sender = oc(G.img, 'profile', 'sender')
    thumbnail = await conn.resize(profile_sender, 150, 150)
    quoted = _order_quoted(thumbnail)
    if type_ == 'unreg':
        return await conn.sendButton(m.chat, {
            'text': 'Sorry User, You can only use this command after registering to the bot database.',
            'nativeFlow': [{'text': 'Register', 'url': _server_url()}],
        }, quoted)
    msg = G.settings['msg'].get(type_)
    if msg:
        return await maybe_await(conn.reply(m.chat, msg, quoted))
    return None


G.dfail = dfail
