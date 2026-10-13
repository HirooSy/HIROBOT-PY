import asyncio
import base64
import json
import os
import re
import sys
import time

from baileys import (
    areJidsSameUser, DisconnectReason, fetchLatestBaileysVersion, generateQR,
    makeCacheableSignalKeyStore, makeWASocket,
)
from baileys.foundation.node_runtime import JSObject
from lib.utils import connection as Connection, database as dbmod, helper
from lib.utils.handler import G, PluginError
from lib.utils.helper import maybe_await, oc, spawn
from lib.utils.simple import _get, helper_connection


WEB_PAIR_TTL = 150
WEB_START_TTL = 90
_auto = {'done': False}


def msg_text(err):
    return str(getattr(err, 'message', None) or err or '')


def now_ms():
    return int(time.time() * 1000)


def handles():
    if G.subbotHandles is None:
        G.subbotHandles = {}
    return G.subbotHandles


def web_entries():
    if G.webSubbots is None:
        G.webSubbots = {}
    return G.webSubbots


def get_config():
    cfg = oc(G.settings, 'connection', 'subbot') or {}
    return {
        'type': cfg.get('type') or 'sql',
        'max': cfg.get('maxConnect') if cfg.get('maxConnect') is not None else 3,
        'autoConnect': cfg.get('autoConnect') if cfg.get('autoConnect') is not None else True,
    }


get_subbot_config = get_config


def sanitize_paircode(raw, silent=False):
    if not raw:
        return None
    s = str(raw).strip().upper()
    if len(s) != 8:
        if not silent:
            print(f'[CUSTOM_PAIRING] Ignored: must be exactly 8 characters (got {len(s)}).', file=sys.stderr)
        return None
    return s


async def fetch_version_with_timeout(timeout=8):
    try:
        return await asyncio.wait_for(fetchLatestBaileysVersion(), timeout)
    except Exception as e:
        print('[Subbot] Baileys version fetch failed, using bundled default:', msg_text(e), file=sys.stderr)
        return {'version': None, 'isLatest': False}


fetch_version = fetch_version_with_timeout


def get_user_auto(jid):
    return dbmod.get_user_auto_reconnect(jid, get_config()['autoConnect'])


def rel_path(jid):
    return 'subbot/' + re.sub(r'@(s\.whatsapp\.net|lid)$', '', str(jid), flags=re.I)


def session_path(jid):
    return Connection.session_storage_path(get_config()['type'], rel_path(jid))


def has_saved_session(jid):
    return Connection.has_saved_auth_state(get_config()['type'], rel_path(jid))


def list_saved_session_jids():
    t = get_config()['type']
    base = os.path.join(os.getcwd(), 'data', 'sessions', 'subbot')
    if not os.path.isdir(base):
        return []
    if t == 'multi':
        names = [e for e in os.listdir(base) if os.path.isdir(os.path.join(base, e))]
    else:
        ext = '.json' if t == 'single' else '.session'
        names = [n[:-len(ext)] for n in os.listdir(base) if n.endswith(ext)]
    return [n + '@s.whatsapp.net' for n in names if has_saved_session(n + '@s.whatsapp.net')]


def remove_saved_session(jid):
    Connection.remove_saved_auth_state(get_config()['type'], rel_path(jid))


def same_jid(a, b):
    return bool(a) and bool(b) and areJidsSameUser(a, b)


def wrap(fn):
    def cb(*args):
        return spawn(maybe_await(fn(*args)))
    return cb


async def call_opt(fn, *args):
    if fn:
        return await maybe_await(fn(*args))
    return None


def ev_on(sock, name, fn):
    _get(sock, 'ev')['on'](name, fn)


def ev_off(sock, name, fn):
    _get(sock, 'ev')['off'](name, fn)


def remove_listeners(sock):
    try:
        _get(sock, 'ev')['removeAllListeners']()
    except Exception:
        pass


def close_ws(sock):
    try:
        r = _get(sock, 'ws').close()
        if asyncio.iscoroutine(r):
            spawn(r)
    except Exception:
        pass


def apply_props(sock, props):
    for k, v in props.items():
        sock[k] = v


def status_code(last):
    out = _get(_get(last, 'error'), 'output')
    return _get(out, 'statusCode') or _get(_get(out, 'payload'), 'statusCode')


def keep_message(msg, start_epoch):
    ts = _get(msg, 'messageTimestamp')
    if isinstance(ts, dict) or (ts is not None and hasattr(ts, 'low')):
        ts = _get(ts, 'low')
    if not ts:
        return True
    try:
        return float(ts) >= start_epoch
    except (TypeError, ValueError):
        return True


class Handle:
    def __init__(self, jid, opts):
        self.jid = jid
        self.opts = opts
        self.sub_conn = None
        self.state = None
        self.stopped = False
        self.generation = 0
        self.is_init = True
        self.reconnecting = False
        self.listeners = {}

    @property
    def subConn(self):
        return self.sub_conn

    async def request_pairing_code(self, phone, custom_code=None):
        code = sanitize_paircode(custom_code if custom_code is not None else oc(G.settings, 'connection', 'subbot', 'paircode'))
        return await maybe_await(self.sub_conn['requestPairingCode'](phone, code))

    def stop(self):
        self.stopped = True
        self.generation += 1
        remove_listeners(self.sub_conn)
        close_ws(self.sub_conn)
        Connection.conns.pop(self.jid, None)
        if handles().get(self.jid) is self:
            handles().pop(self.jid, None)


async def start_sub_bot(jid, opts=None):
    opts = opts or {}
    cfg = get_config()
    auth = await Connection.resolve_auth_state(cfg['type'], rel_path(jid))
    state, save_creds = auth['state'], auth['saveCreds']
    version = (await fetch_version_with_timeout()).get('version')
    logger = helper.P({'level': 'silent'})
    handle = Handle(jid, opts)
    handle.state = state

    def build_options():
        sub = oc(G.settings, 'connection', 'subbot') or {}
        custom = sanitize_paircode(sub.get('paircode'))
        if custom:
            state['creds']['pairingCode'] = custom
        return JSObject({
            'printQRInTerminal': False,
            'mobile': False,
            'version': version,
            'browser': opts.get('browser') or sub.get('browser'),
            'generateHighQualityLinkPreview': True,
            'connectTimeoutMs': 60000,
            'keepAliveIntervalMs': 15000,
            'retryRequestDelayMs': 500,
            'syncFullHistory': bool(sub.get('loadhistory')),
            'markOnlineOnConnect': bool(oc(G.settings, 'system', 'online')),
            'logger': logger,
            'auth': JSObject({
                'creds': state['creds'],
                'keys': makeCacheableSignalKeyStore(state['keys'], logger, None),
            }),
        })

    def new_socket():
        sock = makeWASocket(build_options())
        helper_connection(sock, Connection.store, logger)
        sock['authState'] = state
        return sock

    handle.sub_conn = new_socket()
    handle.sub_conn['isInit'] = False

    async def connection_update(update):
        if handle.stopped:
            return
        connection = _get(update, 'connection')
        last = _get(update, 'lastDisconnect')
        qr = _get(update, 'qr')
        sub_cfg = oc(G.settings, 'connection', 'subbot') or {}
        if qr and sub_cfg.get('qr'):
            generateQR(qr, {'small': True}, lambda out: print(f'\n[Subbot: {jid}]\n{out}'))
        if qr:
            await call_opt(opts.get('onQR'), qr)
        if not connection:
            return
        if connection == 'open':
            handle.is_init = True
            handle.sub_conn['isInit'] = True
            Connection.conns[jid] = handle.sub_conn
            await call_opt(opts.get('onOpen'), handle.sub_conn)
            return
        if connection == 'close':
            Connection.conns.pop(jid, None)
            code = status_code(last)
            logged_out = code == DisconnectReason['loggedOut']
            if logged_out:
                if handles().get(jid) is handle:
                    handles().pop(jid, None)
                Connection.remove_saved_auth_state(cfg['type'], rel_path(jid))
                await call_opt(opts.get('onClose'), handle.sub_conn, code, True)
                return
            if code:
                if handle.reconnecting:
                    return
                handle.reconnecting = True
                try:
                    await call_opt(opts.get('onReconnecting'), handle.sub_conn)
                    await asyncio.sleep(1 if code == DisconnectReason['connectionLost'] else 3)
                    if handle.stopped:
                        return
                    try:
                        await reload_handler(True)
                    except Exception as err:
                        print('[Subbot] Reload error:', msg_text(err), file=sys.stderr)
                finally:
                    handle.reconnecting = False
                return
            if handles().get(jid) is handle:
                handles().pop(jid, None)
            await call_opt(opts.get('onClose'), handle.sub_conn, code, False)

    conn_cb = wrap(connection_update)

    def creds_cb(*_a):
        return spawn(maybe_await(save_creds()))

    async def reload_handler(restart=False):
        if handle.stopped:
            return False
        handle.generation += 1
        my_generation = handle.generation
        hmod = Connection._load_handler()
        if handle.stopped:
            return False
        if restart:
            close_ws(handle.sub_conn)
            remove_listeners(handle.sub_conn)
            handle.sub_conn = new_socket()
            handle.is_init = True
        if my_generation != handle.generation:
            return False
        sock = handle.sub_conn
        if not handle.is_init:
            for name, fn in list(handle.listeners.items()):
                ev_off(sock, name, fn)
        apply_props(sock, Connection.get_message_config())
        listeners = {}
        if hmod is not None:
            start_epoch = int(time.time())

            async def handler_wrapper(chat_update):
                if isinstance(chat_update, dict) and chat_update.get('messages'):
                    kept = [msg for msg in chat_update['messages'] if keep_message(msg, start_epoch)]
                    chat_update['messages'] = kept
                    if not kept:
                        return None
                return await hmod.handler(sock, chat_update)

            on_upsert = wrap(handler_wrapper)
            participants = wrap(lambda u: hmod.participants_update(sock, u))
            groups = wrap(lambda u: hmod.groups_update(sock, u))
            on_delete = wrap(lambda u: hmod.delete_update(sock, u))
            sock['handler'] = on_upsert
            sock['participantsUpdate'] = participants
            sock['groupsUpdate'] = groups
            sock['onDelete'] = on_delete
            listeners['messages.upsert'] = on_upsert
            listeners['group-participants.update'] = participants
            listeners['groups.update'] = groups
            listeners['messages.delete'] = on_delete
        sock['connectionUpdate'] = conn_cb
        sock['credsUpdate'] = creds_cb
        listeners['connection.update'] = conn_cb
        listeners['creds.update'] = creds_cb
        for name, fn in listeners.items():
            ev_on(sock, name, fn)
        handle.listeners = listeners
        handle.is_init = False
        return True

    await reload_handler(False)
    handles()[jid] = handle
    return handle


async def auto_connect_sub_bots():
    if _auto['done']:
        return
    _auto['done'] = True
    if dbmod.database.data is None:
        try:
            await dbmod.load_database()
        except Exception as e:
            print('[Subbot] loadDatabase error:', msg_text(e), file=sys.stderr)
    cfg = get_config()
    if not cfg['autoConnect']:
        return
    jids = [j for j in list_saved_session_jids() if get_user_auto(j)][:cfg['max']]
    for jid in jids:
        if jid in Connection.conns:
            continue
        spawn(_auto_start(jid))


async def _auto_start(jid):
    try:
        await start_sub_bot(jid, {})
    except Exception as err:
        print(f"[Subbot] Failed to auto-reconnect {jid.split('@')[0]}:", msg_text(err), file=sys.stderr)


def clear_web_entry(jid):
    entry = web_entries().get(jid)
    timer = entry.get('timer') if entry else None
    if timer:
        timer.cancel()
    web_entries().pop(jid, None)
    return entry


def discard_unregistered(jid):
    if not has_saved_session(jid):
        remove_saved_session(jid)


async def assert_can_run(jid):
    parent = await Connection.get_main_conn()
    cfg = get_config()
    parent_id = (_get(parent, 'user') or {}).get('id')
    if parent_id and areJidsSameUser(parent_id, jid):
        raise RuntimeError('Cannot create a session on the main bot number.')
    if jid in Connection.conns:
        raise RuntimeError('Your session is already running.')
    if jid in web_entries():
        raise RuntimeError('A connection attempt is already in progress.')
    if len(Connection.conns) >= cfg['max']:
        raise RuntimeError(f"Slots are full (max {cfg['max']}).")


async def fetch_pairing_code(handle, jid, entry):
    for i in range(4):
        await asyncio.sleep(3 if i == 0 else 2.5)
        if web_entries().get(jid) is not entry:
            return
        try:
            entry['code'] = await handle.request_pairing_code(jid.split('@')[0])
            return
        except Exception as err:
            entry['lastError'] = msg_text(err) or 'Failed to get pairing code.'
    if web_entries().get(jid) is entry:
        entry['failed'] = True


def web_subbot_info(jid):
    cfg = get_config()
    entry = web_entries().get(jid)
    running = jid in Connection.conns
    saved = has_saved_session(jid)
    state = 'none'
    if running:
        state = 'online'
    elif entry:
        state = 'connecting' if entry['kind'] == 'start' else 'pairing'
    elif saved:
        state = 'stopped'
    info = {
        'state': state,
        'saved': saved,
        'autoConnect': bool(get_user_auto(jid)),
        'slots': {'used': len(Connection.conns), 'max': cfg['max']},
        'active': list(Connection.conns.keys()),
    }
    if state == 'pairing':
        info['pairing'] = {
            'method': entry['method'],
            'code': entry['code'],
            'qr': Connection.generateQRMatrix(entry['qr']) if entry['qr'] else None,
            'failed': bool(entry['failed']),
            'error': (entry.get('lastError') or 'Failed to get pairing code.') if entry['failed'] else None,
            'expiresIn': max(0, entry['expiresAt'] - now_ms()),
        }
    return info


def expire_web(jid, entry):
    if web_entries().get(jid) is entry:
        web_subbot_abort(jid)


async def web_subbot_connect(jid, method, allowed):
    if not allowed:
        raise RuntimeError('Subbot requires premium.')
    if method not in ('pairing', 'qr'):
        raise RuntimeError('Invalid method.')
    await assert_can_run(jid)
    if has_saved_session(jid):
        raise RuntimeError('You already have a session. Start it or delete it first.')
    entry = {'kind': 'pair', 'method': method, 'code': None, 'qr': None, 'failed': False, 'lastError': None,
             'timer': None, 'expiresAt': now_ms() + WEB_PAIR_TTL * 1000}
    web_entries()[jid] = entry
    entry['timer'] = asyncio.get_running_loop().call_later(WEB_PAIR_TTL, expire_web, jid, entry)

    def on_qr(qr):
        if web_entries().get(jid) is entry and method == 'qr':
            entry['qr'] = qr

    async def on_open(*_a):
        if web_entries().get(jid) is entry:
            clear_web_entry(jid)
        await dbmod.set_user_auto_reconnect(jid, True)

    def on_close(*_a):
        if web_entries().get(jid) is entry:
            clear_web_entry(jid)
            discard_unregistered(jid)

    try:
        handle = await start_sub_bot(jid, {
            'onQR': on_qr,
            'onOpen': on_open,
            'onReconnecting': lambda *_a: None,
            'onClose': on_close,
        })
        if method == 'pairing' and not (handle.state['creds'].get('registered')):
            spawn(fetch_pairing_code(handle, jid, entry))
    except Exception:
        clear_web_entry(jid)
        discard_unregistered(jid)
        raise


async def web_subbot_start(jid, allowed):
    if not allowed:
        raise RuntimeError('Subbot requires premium.')
    await assert_can_run(jid)
    if not has_saved_session(jid):
        raise RuntimeError('No saved session found. Connect first.')
    entry = {'kind': 'start', 'timer': None, 'expiresAt': now_ms() + WEB_START_TTL * 1000}
    web_entries()[jid] = entry
    entry['timer'] = asyncio.get_running_loop().call_later(WEB_START_TTL, expire_web, jid, entry)

    def on_done(*_a):
        if web_entries().get(jid) is entry:
            clear_web_entry(jid)

    try:
        await start_sub_bot(jid, {
            'onOpen': on_done,
            'onReconnecting': lambda *_a: None,
            'onClose': on_done,
        })
    except Exception:
        clear_web_entry(jid)
        raise


def web_subbot_abort(jid):
    entry = clear_web_entry(jid)
    handle = handles().get(jid)
    if handle:
        handle.stop()
    if entry and entry.get('kind') == 'pair':
        discard_unregistered(jid)
    return bool(entry)


def web_subbot_stop(jid):
    if jid in web_entries():
        return web_subbot_abort(jid)
    handle = handles().get(jid)
    live = Connection.conns.get(jid)
    if not handle and not live:
        return False
    if handle:
        handle.stop()
    else:
        remove_listeners(live)
        close_ws(live)
        Connection.conns.pop(jid, None)
    return True


def web_subbot_delete(jid):
    web_subbot_stop(jid)
    remove_saved_session(jid)


async def web_subbot_set_auto(jid, value):
    await dbmod.set_user_auto_reconnect(jid, bool(value))


def web_subbot_session_file(jid):
    if not has_saved_session(jid):
        return None
    target = session_path(jid)
    if not os.path.exists(target) or os.path.isdir(target):
        return None
    return {'path': target, 'name': os.path.basename(target)}


def premium_only():
    return oc(G.db.data, 'siteSettings', 'subbot', 'mode') == 'premium'


async def do_connect(m, *, conn, usedPrefix, isPrems):
    p, is_prems = usedPrefix, isPrems
    if premium_only() and not is_prems:
        raise PluginError('❌ This command is for premium users only.')
    parent = await Connection.get_main_conn()
    cfg = get_config()
    parent_id = (_get(parent, 'user') or {}).get('id')
    if m.sender == parent_id:
        return await m.reply(f"❌ Cannot create *Session* on {parent_id.split('@')[0]}")
    if m.sender in Connection.conns:
        return await m.reply(f'⚠️ You already have an active *Session*.\nUse *{p}disconnect* to stop it first.')
    if len(Connection.conns) >= cfg['max']:
        return await m.reply(f"❌ Slots are full (max {cfg['max']}).\nPlease wait for a slot to be available.")
    if has_saved_session(m.sender):
        return await m.reply(f'⚠️ You already have a previous session.\nUse *{p}reconnect* to continue, or delete the old session first.')

    async def on_open(_sub):
        await dbmod.set_user_auto_reconnect(m.sender, True)
        await conn.reply(m.chat, f'✅ *Session connected!*\n\nUse *{p}disconnect* to stop.', m, {'mentions': [m.sender]})

    async def on_reconnecting(*_a):
        await conn.reply(m.chat, '⚠️ *session* disconnected, trying to reconnect...', m)

    async def on_close(_sub, _code, logged_out):
        await conn.reply(m.chat, f'❌ *Session* logged out. Session deleted.\nPlease *{p}pairing* again.' if logged_out else '❌ *Session* disconnected.', m)

    handle = await start_sub_bot(m.sender, {'onOpen': on_open, 'onReconnecting': on_reconnecting, 'onClose': on_close})
    if not handle.state['creds'].get('registered'):
        async def ask_code():
            await asyncio.sleep(3)
            try:
                code = await handle.request_pairing_code(m.sender.split('@')[0])
                await conn.reply(m.chat, f'-> Code: `{code}`', m)
            except Exception as error:
                print('[MultiSession] Error requesting pairing code:', msg_text(error), file=sys.stderr)
                await conn.reply(m.chat, f"❌ Failed to get pairing code. Try again later.\n\n_Error: {msg_text(error)}_", m)
        spawn(ask_code())


async def do_reconnect(m, *, conn, args, usedPrefix, isPrems):
    args, p, is_prems = args, usedPrefix, isPrems
    if premium_only() and not is_prems:
        raise PluginError('❌ This command is for premium users only.')
    parent = await Connection.get_main_conn()
    cfg = get_config()
    parent_id = (_get(parent, 'user') or {}).get('id')
    if (_get(conn, 'user') or {}).get('id') != parent_id:
        return await conn.reply(m.chat, f"❌ This command can only be used from the main bot!\nwa.me/{parent_id.split('@')[0]}", m)
    if m.sender in Connection.conns:
        return await conn.reply(m.chat, f'⚠️ You already have an active *Jadibot*.\nUse *{p}disconnect* to stop it first.', m)
    if len(Connection.conns) >= cfg['max']:
        return await conn.reply(m.chat, f"❌ Slots are full (max {cfg['max']}).\nPlease wait for a slot to be available.", m)
    if args:
        try:
            creds_json = json.loads(base64.b64decode(args[0]).decode('utf-8'))
            auth = await Connection.resolve_auth_state(cfg['type'], rel_path(m.sender))
            auth['state']['creds'].update(creds_json)
            await maybe_await(auth['saveCreds']())
        except Exception:
            return await conn.reply(m.chat, f'❌ *Invalid Session ID.*\nMake sure you send the correct Session ID from *{p}pairing*.', m)
    elif not has_saved_session(m.sender):
        return await conn.reply(m.chat, f'❌ No session found for your number.\n\nUse *{p}pairing* first to create a new session.', m)
    await conn.react(m.chat, '⏳', m.key)

    async def cleanup_loop():
        while True:
            await asyncio.sleep(60)
            if not _get(Connection.conns.get(m.sender), 'user'):
                return
    cleanup = spawn(cleanup_loop())

    async def on_open(_sub):
        await dbmod.set_user_auto_reconnect(m.sender, True)
        cleanup.cancel()
        await m.react('✅')

    async def on_reconnecting(*_a):
        await conn.sendMessage(m.chat, {'text': '⚠️ Disconnected, trying to reconnect...'}, {'quoted': m})

    async def on_close(_sub, _code, logged_out):
        cleanup.cancel()
        await conn.sendMessage(m.chat, {'text': f'❌ *Connection* logged out. Session deleted.\nUse *{p}pairing* to create a new session.' if logged_out
                                        else f'❌ Disconnected. Use *{p}reconnect* to reconnect.'}, {'quoted': m})

    await start_sub_bot(m.sender, {'onOpen': on_open, 'onReconnecting': on_reconnecting, 'onClose': on_close})


async def do_disconnect(m, *, conn, isOwner):
    is_owner = isOwner
    if not is_owner:
        raise PluginError('❌ Only the bot owner can use this command.')
    found = None
    for key, c in list(Connection.conns.items()):
        if same_jid((_get(c, 'user') or {}).get('id'), (_get(conn, 'user') or {}).get('id')):
            found = key
            break
    if not found:
        parent = await Connection.get_main_conn()
        if same_jid((_get(parent, 'user') or {}).get('id'), (_get(conn, 'user') or {}).get('id')):
            raise PluginError('❌ You cannot stop the bot main session directly via this command.')
        raise PluginError('⚠️ This session was not found in the list of active connections.')
    await conn.reply(m.chat, 'Session is being stopped...', m)
    try:
        remove_listeners(conn)
        close_ws(conn)
    except Exception as e:
        print('[Subbot] Error while disconnecting:', msg_text(e), file=sys.stderr)
    Connection.conns.pop(found, None)
    await dbmod.set_user_auto_reconnect(found, False)


async def handler(m, *, conn, command, usedPrefix, isPrems, args, isOwner):
    if re.match(r'^(pairing|connect)$', command, re.I):
        return await do_connect(m, conn=conn, usedPrefix=usedPrefix, isPrems=isPrems)
    if re.match(r'^reconnect$', command, re.I):
        return await do_reconnect(m, conn=conn, args=args, usedPrefix=usedPrefix, isPrems=isPrems)
    if re.match(r'^disconnect$', command, re.I):
        return await do_disconnect(m, conn=conn, isOwner=isOwner)


help = ['pairing', 'reconnect', 'disconnect']
tags = ['subbot']
command = re.compile(r'^(pairing|connect|reconnect|disconnect)$', re.I)
ai = {'risk': 'blocked', 'description': 'connect, reconnect, or disconnect a WhatsApp sub-bot session'}
