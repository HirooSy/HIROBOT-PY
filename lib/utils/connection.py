import asyncio
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import time
import traceback
import types
from types import SimpleNamespace

from baileys.foundation.node_runtime import JSObject
from baileys.foundation.qrcode_terminal import _QRCode, _QRErrorCorrectLevel
from baileys.utils.auth_state_storage import bufferJsonParse, bufferJsonStringify
from lib.utils import database as dbmod, helper
from lib.utils.color import chalk
from lib.utils.handler import G
from lib.utils.helper import KeyedLock, maybe_await, now_ms, oc, spawn, truthy
from lib.utils.simple import _Lazy, helper_connection
from baileys import (
    DisconnectReason, createLogger, fetchLatestBaileysVersion, generateQR, initAuthCreds,
    makeCacheableSignalKeyStore, makeWASocket, useMultiFileAuthState,
)
from baileys import (
    WAMessageStubType, decryptPollVote, getKeyAuthor, isJidBroadcast, isJidGroup, jidDecode,
    jidNormalizedUser, proto, updateMessageWithReaction, updateMessageWithReceipt,
)


TIME_TO_DATA_STALE = 5 * 60 * 1000


MAX_MESSAGES_PER_CHAT = 100
MAX_STORE_CHATS = 300


def decode_jid(jid):
    if not isinstance(jid, str):
        return None
    if re.search(r':\d+@', jid, re.IGNORECASE):
        decoded = jidDecode(jid) or {}
        user = oc(decoded, 'user')
        server = oc(decoded, 'server')
        out = f'{user}@{server}' if user and server else jid
        return out.strip()
    return jid.strip()


def _bytes_from(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode('utf-8')
    if isinstance(value, (list, tuple)):
        return bytes(value)
    return bytes(value or b'')


def _buffer_bytes(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, dict) and value.get('type') == 'Buffer':
        return _bytes_from(value.get('data'))
    return _bytes_from(value)


def _hex_of_option(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    inner = value.get('data') if isinstance(value, dict) and value.get('data') else value
    return _bytes_from(inner).hex()


def _tsnum(m):
    t = oc(m, 'messageTimestamp')
    if not t:
        return 0
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return t
    if hasattr(t, 'toNumber'):
        try:
            return t.toNumber()
        except Exception:
            pass
    try:
        return float(str(t))
    except (TypeError, ValueError):
        return 0


def _fix_json(value):
    if isinstance(value, (bytes, bytearray)):
        return {'type': 'Buffer', 'data': list(value)}
    if hasattr(value, 'toNumber'):
        return value.toNumber()
    return str(value)


def _strip_is_chats(value):
    if isinstance(value, dict):
        return {k: _strip_is_chats(v) for k, v in value.items() if k != 'isChats'}
    if isinstance(value, list):
        return [_strip_is_chats(v) for v in value]
    return value


def _buffer_hook(obj):
    if obj.get('type') == 'Buffer' and isinstance(obj.get('data'), list):
        return bytes(obj['data'])
    return obj


def _participant_lids_numbers(p, pn_from_string=True):
    p_lid = None
    p_number = None
    raw_id = oc(p, 'id')
    if isinstance(raw_id, dict):
        inner_id = str(oc(raw_id, 'id') or oc(raw_id, 'jid') or '')
        if inner_id.endswith('@lid'):
            p_lid = inner_id
        inner_pn = oc(raw_id, 'phoneNumber') or oc(raw_id, 'pn') or ''
        if str(inner_pn).endswith('@s.whatsapp.net'):
            p_number = str(inner_pn)
    elif isinstance(raw_id, str):
        if raw_id.endswith('@lid'):
            p_lid = raw_id
        elif pn_from_string and raw_id.endswith('@s.whatsapp.net'):
            p_number = raw_id
    top_pn = oc(p, 'phoneNumber') or oc(p, 'pn') or oc(p, 'phone_number')
    if top_pn:
        if str(top_pn).endswith('@s.whatsapp.net'):
            p_number = str(top_pn)
        else:
            digits = re.sub(r'[^0-9]', '', str(top_pn))
            if len(digits) >= 7:
                p_number = digits + '@s.whatsapp.net'
    lid_field = oc(p, 'lid')
    if lid_field and str(lid_field).endswith('@lid'):
        p_lid = str(lid_field)
    return p_lid, p_number


def make_in_memory_store():
    chats = {}
    messages = {}
    state = {'connection': 'close'}

    def load_message(jid, id=None):
        if jid and not id:
            id = jid
            for msgs in messages.values():
                for m in msgs:
                    if oc(m, 'key', 'id') == id:
                        return m
            return None
        jid = decode_jid(jid)
        if jid not in messages:
            return None
        for m in messages[jid]:
            if oc(m, 'key', 'id') == id:
                return m
        return None

    def load_album(jid, parent_id):
        jid = decode_jid(jid)
        if jid:
            lst = messages.get(jid) or []
        else:
            lst = [m for v in messages.values() for m in v]
        linked = [m for m in lst if oc(m, 'message', 'messageContextInfo', 'messageAssociation', 'parentMessageKey', 'id') == parent_id]
        if linked:
            return linked
        parent = load_message(jid, parent_id)
        if parent is None:
            return []
        album = oc(parent, 'message', 'albumMessage') or {}
        expected = (oc(album, 'expectedImageCount') or 0) + (oc(album, 'expectedVideoCount') or 0)
        base = _tsnum(parent)
        parent_jid = decode_jid(oc(parent, 'key', 'remoteJid'))
        pool = messages.get(parent_jid or jid) or []
        near = []
        for m in pool:
            if m is parent or not (oc(m, 'message', 'imageMessage') or oc(m, 'message', 'videoMessage')):
                continue
            if bool(oc(m, 'key', 'fromMe')) != bool(oc(parent, 'key', 'fromMe')):
                continue
            diff = _tsnum(m) - base
            if -5 <= diff <= 120:
                near.append(m)
        who = oc(parent, 'key', 'participant')
        same = [m for m in near if oc(m, 'key', 'participant') == who] if who else near
        picked = sorted(same if same else near, key=_tsnum)
        return picked[:expected] if expected else picked

    async def fetch_group_metadata(jid, group_metadata=None):
        jid = decode_jid(jid)
        if not jid or not isJidGroup(jid):
            return None
        if jid not in chats:
            chats[jid] = {'id': jid}
            return chats[jid]
        stale = not truthy(chats[jid].get('metadata')) or now_ms() - (chats[jid].get('lastfetch') or 0) > TIME_TO_DATA_STALE
        if stale and group_metadata is not None:
            metadata = await maybe_await(group_metadata(jid))
            if truthy(metadata):
                chats[jid].update({'subject': oc(metadata, 'subject'), 'lastfetch': now_ms(), 'metadata': metadata})
        return chats[jid].get('metadata')

    def get_contact(jid):
        jid = decode_jid(jid)
        if jid not in chats:
            return None
        return chats[jid]

    def get_number_from_lid(lid_number):
        if not lid_number or not str(lid_number).endswith('@lid'):
            return None
        lid_num = lid_number.split('@')[0]
        direct = chats.get(lid_number)
        direct_number = oc(direct, 'number')
        if direct_number and str(direct_number).endswith('@s.whatsapp.net'):
            print(f'[Store] LID reverse map hit: {lid_number} → {direct_number}')
            return direct_number
        for jid, contact in list(chats.items()):
            if not jid.endswith('@s.whatsapp.net'):
                continue
            if oc(contact, 'lid') == lid_number or oc(contact, 'linkedIdentity') == lid_number:
                print(f'[Store] Found via contact.lid: {lid_number} → {jid}')
                return jid
        for group_jid, chat in list(chats.items()):
            if not group_jid.endswith('@g.us'):
                continue
            participants = oc(chat, 'metadata', 'participants')
            if not isinstance(participants, list):
                continue
            for p in participants:
                p_lid, p_number = _participant_lids_numbers(p)
                is_match = p_lid == lid_number or (p_lid is not None and p_lid.split('@')[0] == lid_num)
                if not is_match:
                    continue
                if p_number:
                    print(f'[Store] Found via group {group_jid}: {lid_number} → {p_number}')
                    return p_number
        return None

    def get_lid_from_number(number):
        if not number or not str(number).endswith('@s.whatsapp.net'):
            return None
        contact = chats.get(number)
        if contact and (oc(contact, 'lid') or oc(contact, 'linkedIdentity')):
            return oc(contact, 'lid') or oc(contact, 'linkedIdentity')
        return None

    def cache_pn_from_participants(participants):
        if not isinstance(participants, list):
            return
        for p in participants:
            p_lid, p_number = _participant_lids_numbers(p, False)
            if p_lid and p_number:
                if p_lid not in chats:
                    chats[p_lid] = {}
                if not chats[p_lid].get('number'):
                    chats[p_lid]['number'] = p_number
                    chats[p_lid]['lid'] = p_lid
                    print(f'[Store] cachePn: {p_lid} → {p_number}')
                if p_number not in chats:
                    chats[p_number] = {}
                if not chats[p_number].get('lid'):
                    chats[p_number]['lid'] = p_lid

    def upsert_message(jid, message, type_='append'):
        jid = decode_jid(jid)
        messages[jid] = messages.pop(jid, None) or []
        while len(messages) > MAX_STORE_CHATS:
            messages.pop(next(iter(messages)))
        inner = message.get('message') if isinstance(message, dict) else None
        association = None
        if isinstance(inner, dict):
            info = inner.get('messageContextInfo')
            association = info.get('messageAssociation') if isinstance(info, dict) else None
            inner.pop('messageContextInfo', None)
            inner.pop('senderKeyDistributionMessage', None)
            if association:
                inner['messageContextInfo'] = {'messageAssociation': association}
        if jid not in chats:
            chats[jid] = {}
        chats[jid]['lastfetch'] = now_ms()
        msg = load_message(jid, oc(message, 'key', 'id'))
        if msg is not None:
            msg.update(message)
        elif type_ == 'append':
            messages[jid].append(message)
            if len(messages[jid]) > MAX_MESSAGES_PER_CHAT:
                messages[jid] = messages[jid][-MAX_MESSAGES_PER_CHAT:]
        else:
            messages[jid].insert(0, message)
            if len(messages[jid]) > MAX_MESSAGES_PER_CHAT:
                messages[jid] = messages[jid][:MAX_MESSAGES_PER_CHAT]

    def bind(ev, opts=None):
        opts = opts or {}
        group_metadata = opts.get('groupMetadata')
        conn = opts.get('conn')
        on = ev['on']

        def on_connection_update(update):
            state.update(update or {})
        on('connection.update', on_connection_update)

        def chats_set(payload):
            for chat in payload.get('chats') or []:
                cid = decode_jid(oc(chat, 'id'))
                if not cid:
                    continue
                if cid not in chats:
                    chats[cid] = {**chat, 'isChats': True, **({'name': chat['name']} if chat.get('name') else {})}
                if chat.get('name'):
                    chats[cid]['name'] = chat['name']
        on('chats.set', chats_set)

        def contacts_set(payload):
            for contact in payload.get('contacts') or []:
                cid = decode_jid(oc(contact, 'id'))
                if not cid:
                    continue
                target = chats[cid] if cid in chats else {}
                target.update({**contact, 'isContact': True})
                chats[cid] = target
                lid = oc(contact, 'lid') or oc(contact, 'linkedIdentity')
                if lid and str(lid).endswith('@lid') and cid.endswith('@s.whatsapp.net'):
                    if lid not in chats:
                        chats[lid] = {}
                    chats[lid]['number'] = cid
                    chats[lid]['lid'] = lid
                    print(f'[Store] contacts.set LID mapping: {lid} → {cid}')
        on('contacts.set', contacts_set)

        def messages_set(payload):
            for message in payload.get('messages') or []:
                jid = decode_jid(oc(message, 'key', 'remoteJid'))
                if not jid or isJidBroadcast(jid):
                    continue
                if jid not in messages:
                    messages[jid] = []
                upsert_message(jid, proto.WebMessageInfo.fromObject(message), 'prepend')
        on('messages.set', messages_set)

        def contacts_update(updates):
            for contact in updates or []:
                cid = decode_jid(oc(contact, 'id'))
                if not cid:
                    continue
                target = chats[cid] if cid in chats else {}
                target.update({'id': cid, **contact, 'isContact': True})
                chats[cid] = target
                lid = oc(contact, 'lid') or oc(contact, 'linkedIdentity')
                if lid and str(lid).endswith('@lid') and cid.endswith('@s.whatsapp.net'):
                    if lid not in chats:
                        chats[lid] = {}
                    chats[lid]['number'] = cid
                    chats[lid]['lid'] = lid
                    print(f'[Store] contacts.update LID mapping: {lid} → {cid}')
        on('contacts.update', contacts_update)

        async def chats_upsert(upserts):
            async def one(chat):
                cid = decode_jid(oc(chat, 'id'))
                if not cid or isJidBroadcast(cid):
                    return
                if cid not in chats:
                    chats[cid] = {'id': cid, **chat, 'isChats': True}
                is_group = isJidGroup(cid)
                chats[cid].update({**chat, 'isChats': True})
                if is_group and not truthy(chats[cid].get('metadata')):
                    meta = await fetch_group_metadata(cid, group_metadata)
                    chats[cid].update({'metadata': meta})
                    if truthy(oc(meta, 'participants')):
                        cache_pn_from_participants(meta['participants'])
                elif is_group and truthy(oc(chats[cid], 'metadata', 'participants')):
                    cache_pn_from_participants(chats[cid]['metadata']['participants'])
            await asyncio.gather(*(one(c) for c in (upserts or [])))
        on('chats.upsert', lambda u: spawn(chats_upsert(u)))

        def chats_update(updates):
            for chat in updates or []:
                cid = decode_jid(oc(chat, 'id'))
                if not cid:
                    continue
                if cid not in chats:
                    chats[cid] = {'id': cid, **chat, 'isChats': True}
                if chat.get('unreadCount'):
                    chat['unreadCount'] = chat['unreadCount'] + (chats[cid].get('unreadCount') or 0)
                chats[cid].update({'id': cid, **chat, 'isChats': True})
        on('chats.update', chats_update)

        def presence_update(presence):
            cid = decode_jid(oc(presence, 'id'))
            if not cid:
                return
            if cid not in chats:
                chats[cid] = {'id': cid, 'isContact': True}
            chats[cid].update(presence)
        on('presence.update', presence_update)

        def process_poll(msg, jid):
            try:
                content = None
                if truthy(oc(msg, 'message', 'pollUpdateMessage')):
                    content = msg.get('message')
                elif truthy(oc(msg, 'message', 'ephemeralMessage', 'message', 'pollUpdateMessage')):
                    content = msg['message']['ephemeralMessage']['message']
                poll_update = oc(content, 'pollUpdateMessage')
                if not truthy(poll_update) or not truthy(oc(conn, 'user', 'id')):
                    return
                creation_key = oc(poll_update, 'pollCreationMessageKey')
                creation_jid = decode_jid(oc(creation_key, 'remoteJid')) or jid
                poll_msg = load_message(creation_jid, oc(creation_key, 'id'))
                enc_key = oc(poll_msg, 'message', 'messageContextInfo', 'messageSecret') or oc(poll_msg, 'messageContextInfo', 'messageSecret')
                if poll_msg is None or not truthy(enc_key):
                    return
                me_id = jidNormalizedUser(oc(conn, 'user', 'id'))
                creator = getKeyAuthor(creation_key, me_id)
                voter = getKeyAuthor(oc(msg, 'key'), me_id)
                vote_msg = decryptPollVote(oc(poll_update, 'vote'), JSObject({
                    'pollEncKey': _buffer_bytes(enc_key), 'pollCreatorJid': creator,
                    'pollMsgId': oc(creation_key, 'id'), 'voterJid': voter,
                }))
                selected = [_hex_of_option(o) for o in (oc(vote_msg, 'selectedOptions') or [])]
                pm = poll_msg.get('message') or {}
                poll_options = (oc(pm, 'pollCreationMessage', 'options') or oc(pm, 'pollCreationMessageV2', 'options')
                                or oc(pm, 'pollCreationMessageV3', 'options') or [])
                option_names = []
                for h in selected:
                    match = None
                    for o in poll_options:
                        digest = hashlib.sha256(str(oc(o, 'optionName') or '').encode('utf-8')).hexdigest()
                        if digest == h:
                            match = o
                            break
                    name = oc(match, 'optionName')
                    if name:
                        option_names.append(name)
                existing = poll_msg.get('pollUpdates') or []
                ts = oc(poll_update, 'senderTimestampMs')
                if hasattr(ts, 'toNumber'):
                    ts = ts.toNumber()
                existing.append({
                    'pollUpdateMessageKey': msg['key'], 'vote': vote_msg, 'voter': voter,
                    'selectedOptions': option_names, 'senderTimestampMs': ts,
                })
                poll_msg['pollUpdates'] = existing
                poll_jid_messages = messages.get(creation_jid)
                if poll_jid_messages:
                    cid = oc(creation_key, 'id')
                    idx = next((i for i, m in enumerate(poll_jid_messages) if oc(m, 'key', 'id') == cid), -1)
                    if idx != -1:
                        poll_jid_messages[idx]['pollUpdates'] = existing
            except Exception:
                pass

        def messages_upsert(payload):
            new_messages = payload.get('messages') or []
            type_ = payload.get('type')
            if type_ not in ('append', 'notify'):
                return
            for msg in new_messages:
                jid = decode_jid(oc(msg, 'key', 'remoteJid'))
                if not jid or isJidBroadcast(jid):
                    continue
                if oc(msg, 'messageStubType') == WAMessageStubType['CIPHERTEXT']:
                    continue
                if jid not in messages:
                    messages[jid] = []
                upsert_message(jid, proto.WebMessageInfo.fromObject(msg))
                if type_ == 'notify' and jid not in chats:
                    ev['emit']('chats.upsert', [JSObject({
                        'id': jid, 'conversationTimestamp': oc(msg, 'messageTimestamp'),
                        'unreadCount': 1, 'name': oc(msg, 'pushName') or oc(msg, 'verifiedBizName'),
                    })])
                process_poll(msg, jid)
        on('messages.upsert', messages_upsert)

        def messages_update(updates):
            for message in updates or []:
                jid = decode_jid(oc(message, 'key', 'remoteJid'))
                if not jid or isJidBroadcast(jid):
                    continue
                mid = oc(message, 'key', 'id')
                if jid not in messages:
                    messages[jid] = []
                msg = load_message(jid, mid)
                if msg is None:
                    return
                if oc(message, 'update', 'messageStubType') == WAMessageStubType['REVOKE']:
                    continue
                msg.update(oc(message, 'update') or {})
        on('messages.update', messages_update)

        async def groups_update(updates):
            async def one(group):
                gid = decode_jid(oc(group, 'id'))
                if not gid or not isJidGroup(gid):
                    return
                if gid not in chats:
                    chats[gid] = {'id': gid, **group, 'isChats': True}
                if not truthy(chats[gid].get('metadata')):
                    meta = await fetch_group_metadata(gid, group_metadata)
                    chats[gid].update({'metadata': meta})
                    if truthy(oc(meta, 'participants')):
                        cache_pn_from_participants(meta['participants'])
                chats[gid]['metadata'].update(group)
                if truthy(oc(chats[gid]['metadata'], 'participants')):
                    cache_pn_from_participants(chats[gid]['metadata']['participants'])
            await asyncio.gather(*(one(g) for g in (updates or [])))
        on('groups.update', lambda u: spawn(groups_update(u)))

        async def participants_update(update):
            gid = decode_jid(oc(update, 'id'))
            if not gid or not isJidGroup(gid):
                return
            if gid not in chats:
                chats[gid] = {'id': gid}
            if not truthy(chats[gid].get('metadata')):
                chats[gid].update({'metadata': await fetch_group_metadata(gid, group_metadata)})
            metadata = chats[gid].get('metadata')
            if not truthy(metadata):
                print(f"Try to update group {gid} but metadata not found in 'group-participants.update'")
                return
            action = oc(update, 'action')
            parts = oc(update, 'participants') or []
            if action == 'add':
                metadata['participants'].extend({'id': i, 'admin': None} for i in parts)
                cache_pn_from_participants([p if isinstance(p, dict) else {'id': p} for p in parts])
            elif action in ('demote', 'promote'):
                for participant in metadata['participants']:
                    if oc(participant, 'id') in parts:
                        participant['admin'] = 'admin' if action == 'promote' else None
            elif action == 'remove':
                metadata['participants'] = [p for p in metadata['participants'] if oc(p, 'id') not in parts]
            chats[gid].update({'metadata': metadata})
        on('group-participants.update', lambda u: spawn(participants_update(u)))

        def receipt_update(updates):
            for item in updates or []:
                key = oc(item, 'key')
                receipt = oc(item, 'receipt')
                jid = decode_jid(oc(key, 'remoteJid'))
                if not jid:
                    continue
                if jid not in messages:
                    messages[jid] = []
                msg = load_message(jid, oc(key, 'id'))
                if msg is None:
                    return
                updateMessageWithReceipt(msg, receipt)
        on('message-receipt.update', receipt_update)

        def reactions(items):
            for item in items or []:
                key = oc(item, 'key')
                reaction = oc(item, 'reaction')
                jid = decode_jid(oc(key, 'remoteJid'))
                if not jid:
                    continue
                msg = load_message(jid, oc(key, 'id'))
                if msg is None:
                    return
                updateMessageWithReaction(msg, reaction)
        on('messages.reaction', reactions)

    def to_json():
        return {'chats': chats, 'messages': messages}

    def from_json(data):
        chats.update(data.get('chats') or {})
        for jid, lst in (data.get('messages') or {}).items():
            out = []
            for m in lst or []:
                if not m:
                    continue
                wm = proto.WebMessageInfo.fromObject(m)
                if oc(wm, 'messageStubType') != WAMessageStubType['CIPHERTEXT']:
                    out.append(wm)
            messages[jid] = out

    holder = {'db': None}

    def get_store_db(db_path):
        if holder['db'] is not None:
            return holder['db']
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        db.execute('CREATE TABLE IF NOT EXISTS store (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)')
        holder['db'] = db
        return db

    def write_to_file(db_path):
        conn_db = get_store_db(db_path)
        payload = json.dumps(_strip_is_chats(to_json()), default=_fix_json, ensure_ascii=False)
        conn_db.execute(
            'INSERT INTO store (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data',
            (payload,),
        )

    def read_from_file(db_path):
        conn_db = get_store_db(db_path)
        row = conn_db.execute('SELECT data FROM store WHERE id = 1').fetchone()
        if row:
            from_json(json.loads(row[0], object_hook=_buffer_hook))

    return SimpleNamespace(
        chats=chats,
        messages=messages,
        state=state,
        loadMessage=load_message,
        loadAlbum=load_album,
        fetchGroupMetadata=fetch_group_metadata,
        getContact=get_contact,
        getNumberFromLid=get_number_from_lid,
        getLidFromNumber=get_lid_from_number,
        bind=bind,
        writeToFile=write_to_file,
        readFromFile=read_from_file,
    )

KEY_MAP = {
    'pre-key': 'preKeys', 'session': 'sessions', 'sender-key': 'senderKeys',
    'app-state-sync-key': 'appStateSyncKeys', 'app-state-sync-version': 'appStateVersions',
    'sender-key-memory': 'senderKeyMemory', 'lid-mapping': 'lidMappings',
    'device-list': 'deviceLists', 'tctoken': 'tcTokens',
}
REVERSE_KEY_MAP = {v: k for k, v in KEY_MAP.items()}
signal_key_lock = KeyedLock()

state = types.SimpleNamespace(conn=None)
conns = {}
store = make_in_memory_store()
store_file = 'data/sessions/store.db'
logger = None
auth_state = None
connection_options = {}
extra_connection_listeners = []
_flags = {'pairingCodeRequested': False, 'isReconnecting': False, 'isPairingInProgress': False, 'connGeneration': 0}
_old_handler = {'mod': None}
_handler_cache = {}


def _msg(err):
    return getattr(err, 'message', None) or str(err)


def safe_err(err):
    if isinstance(err, BaseException):
        return ''.join(traceback.format_exception(type(err), err, err.__traceback__)).rstrip()
    if isinstance(err, str):
        return err
    return _msg(err)


def _js(value):
    return 'undefined' if value is None else str(value)


def _or(a, b):
    return a if truthy(a) else b


def _error_message(err):
    if err is None:
        return None
    if isinstance(err, dict):
        msg = err.get('message')
    else:
        msg = getattr(err, 'message', None)
    if msg is None and isinstance(err, BaseException):
        return str(err)
    return msg


def _spawn_result(value):
    if asyncio.iscoroutine(value):
        spawn(value)
    return None


def _touch_connect_timestamp():
    if isinstance(G.timestamp, dict):
        G.timestamp['connect'] = time.time()


def get_message_config():
    return {
        'welcome': 'Hi @user!\nWelcome to @subject', 'bye': 'Good bye @user!',
        'spromote': '@user is now admin', 'sdemote': '@user is no longer admin',
        'sDesc': 'Description changed\n\n@desc', 'sSubject': 'Group subject changed\n\n@subject',
        'sIcon': 'Group icon changed', 'sRevoke': 'Group link has been changed',
    }


def on_connection_update(fn):
    extra_connection_listeners.append(fn)

    def remove():
        if fn in extra_connection_listeners:
            extra_connection_listeners.remove(fn)
    return remove


def generateQRMatrix(input_text):
    qr = _QRCode(-1, _QRErrorCorrectLevel['L'])
    qr.addData(input_text)
    qr.make()
    return [[1 if cell else 0 for cell in row] for row in qr.modules]


generate_qr_matrix = generateQRMatrix


def apply_custom_pairing_code_patch(creds):
    raw = oc(G.settings, 'connection', 'main', 'paircode')
    if not raw:
        return None
    sanitized = str(raw).strip().upper()
    if len(sanitized) != 8:
        print(chalk.yellow('[CUSTOM_PAIRING]'), f'Ignored: must be exactly 8 characters (got {len(sanitized)}). Falling back to random pairing code.', file=sys.stderr)
        return None
    if isinstance(creds, dict):
        creds['pairingCode'] = sanitized
    return sanitized


def use_sqlite_auth_state(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    db.executescript(
        'CREATE TABLE IF NOT EXISTS creds (\n'
        '      id INTEGER PRIMARY KEY CHECK (id = 1),\n'
        '      data TEXT NOT NULL\n'
        '    );\n'
        'CREATE TABLE IF NOT EXISTS keys (\n'
        '      type TEXT NOT NULL,\n'
        '      id TEXT NOT NULL,\n'
        '      data TEXT NOT NULL,\n'
        '      PRIMARY KEY (type, id)\n'
        '    );'
    )

    def clear_auth_keys():
        return db.execute('DELETE FROM keys').rowcount

    def read_creds():
        row = db.execute('SELECT data FROM creds WHERE id = 1').fetchone()
        return bufferJsonParse(row[0]) if row else initAuthCreds()

    creds = read_creds()

    def write_creds():
        db.execute(
            'INSERT INTO creds (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data',
            (bufferJsonStringify(creds),),
        )

    async def keys_get(type_, ids):
        db_type = REVERSE_KEY_MAP.get(type_, type_)

        async def task():
            result = JSObject({})
            for i in ids:
                row = db.execute('SELECT data FROM keys WHERE type = ? AND id = ?', (db_type, i)).fetchone()
                if row:
                    result[i] = bufferJsonParse(row[0])
            return result
        return await signal_key_lock.run_many([f'{db_type}:{i}' for i in ids], task)

    async def keys_set(data):
        lock_keys = []
        for category in data:
            t = REVERSE_KEY_MAP.get(category, category)
            for i in (data[category] or {}):
                lock_keys.append(f'{t}:{i}')

        async def task():
            db.execute('BEGIN')
            try:
                for category in data:
                    t = REVERSE_KEY_MAP.get(category, category)
                    for i, value in (data[category] or {}).items():
                        if truthy(value):
                            db.execute(
                                'INSERT INTO keys (type, id, data) VALUES (?, ?, ?) ON CONFLICT(type, id) DO UPDATE SET data = excluded.data',
                                (t, i, bufferJsonStringify(value)),
                            )
                        else:
                            db.execute('DELETE FROM keys WHERE type = ? AND id = ?', (t, i))
                db.execute('COMMIT')
            except Exception:
                try:
                    db.execute('ROLLBACK')
                except Exception:
                    pass
                raise
        return await signal_key_lock.run_many(lock_keys, task)

    async def save_creds():
        write_creds()

    return {
        'state': JSObject({'creds': creds, 'keys': JSObject({'get': keys_get, 'set': keys_set})}),
        'saveCreds': save_creds,
        'clearAuthKeys': clear_auth_keys,
    }


def use_single_file_auth_state(file_path):
    os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)

    def read_all():
        if not os.path.exists(file_path):
            return initAuthCreds(), {}
        try:
            with open(file_path, 'r', encoding='utf-8') as fh:
                parsed = bufferJsonParse(fh.read())
            return oc(parsed, 'creds') or initAuthCreds(), oc(parsed, 'keys') or {}
        except Exception as e:
            print('Single-file auth state failed to parse, starting fresh:', _msg(e), file=sys.stderr)
            return initAuthCreds(), {}

    creds, keys = read_all()
    holder = {'keys': keys}

    def persist():
        with open(file_path, 'w', encoding='utf-8') as fh:
            fh.write(bufferJsonStringify(JSObject({'creds': creds, 'keys': holder['keys']})))

    def clear_auth_keys():
        count = sum(len(by_type) for by_type in holder['keys'].values())
        holder['keys'] = {}
        persist()
        return count

    async def keys_get(type_, ids):
        db_type = REVERSE_KEY_MAP.get(type_, type_)

        async def task():
            result = JSObject({})
            for i in ids:
                raw = (holder['keys'].get(db_type) or {}).get(i)
                if raw is not None:
                    result[i] = raw
            return result
        return await signal_key_lock.run_many([f'{db_type}:{i}' for i in ids], task)

    async def keys_set(data):
        lock_keys = []
        for category in data:
            t = REVERSE_KEY_MAP.get(category, category)
            for i in (data[category] or {}):
                lock_keys.append(f'{t}:{i}')

        async def task():
            for category in data:
                t = REVERSE_KEY_MAP.get(category, category)
                holder['keys'].setdefault(t, {})
                for i, value in (data[category] or {}).items():
                    if truthy(value):
                        holder['keys'][t][i] = value
                    else:
                        holder['keys'][t].pop(i, None)
            persist()
        return await signal_key_lock.run_many(lock_keys, task)

    async def save_creds():
        persist()

    return {
        'state': JSObject({'creds': creds, 'keys': JSObject({'get': keys_get, 'set': keys_set})}),
        'saveCreds': save_creds,
        'clearAuthKeys': clear_auth_keys,
    }


async def resolve_auth_state(type_, relative_path):
    base_dir = os.path.join(os.getcwd(), 'data', 'sessions')
    if type_ == 'multi':
        folder = os.path.join(base_dir, relative_path)
        os.makedirs(folder, exist_ok=True)
        auth = await useMultiFileAuthState(folder)

        def clear_auth_keys():
            entries = [f for f in os.listdir(folder) if f != 'creds.json']
            for f in entries:
                os.unlink(os.path.join(folder, f))
            return len(entries)
        return {'state': auth['state'], 'saveCreds': auth['saveCreds'], 'clearAuthKeys': clear_auth_keys}
    if type_ == 'single':
        return use_single_file_auth_state(os.path.join(base_dir, f'{relative_path}.json'))
    return use_sqlite_auth_state(os.path.join(base_dir, f'{relative_path}.session'))


def session_storage_path(type_, relative_path):
    base_dir = os.path.join(os.getcwd(), 'data', 'sessions')
    if type_ == 'multi':
        return os.path.join(base_dir, relative_path)
    if type_ == 'single':
        return os.path.join(base_dir, f'{relative_path}.json')
    return os.path.join(base_dir, f'{relative_path}.session')


def has_saved_auth_state(type_, relative_path):
    target = session_storage_path(type_, relative_path)
    if not os.path.exists(target):
        return False
    try:
        if type_ == 'multi':
            creds_file = os.path.join(target, 'creds.json')
            if not os.path.exists(creds_file):
                return False
            with open(creds_file, 'r', encoding='utf-8') as fh:
                return bool(oc(bufferJsonParse(fh.read()), 'registered'))
        if type_ == 'single':
            with open(target, 'r', encoding='utf-8') as fh:
                return bool(oc(bufferJsonParse(fh.read()), 'creds', 'registered'))
        uri = pathlib.Path(target).resolve().as_uri() + '?mode=ro'
        db = sqlite3.connect(uri, uri=True)
        try:
            row = db.execute('SELECT data FROM creds WHERE id = 1').fetchone()
        finally:
            db.close()
        if not row:
            return False
        return bool(oc(bufferJsonParse(row[0]), 'registered'))
    except Exception:
        return False


def remove_saved_auth_state(type_, relative_path):
    target = session_storage_path(type_, relative_path)
    if not os.path.exists(target):
        return
    if type_ == 'multi':
        shutil.rmtree(target, ignore_errors=True)
    else:
        try:
            os.unlink(target)
        except OSError:
            pass


async def fetch_version(timeout_s=8):
    try:
        return await asyncio.wait_for(fetchLatestBaileysVersion(), timeout_s)
    except Exception as e:
        print(chalk.yellow('Baileys version fetch failed, using bundled default:'), safe_err(e), file=sys.stderr)
        return JSObject({'version': None, 'isLatest': False})


def _load_handler():
    from lib.utils import handler as hmod
    return hmod


def _import_handler_module():
    path = os.path.join(helper.ROOT, 'lib', 'utils', 'handler.py')
    try:
        key = helper.hash_file_content(path)
        if key not in _handler_cache:
            _handler_cache[key] = helper.import_file(path)
        return _handler_cache[key]
    except Exception as err:
        print(safe_err(err), file=sys.stderr)
        return None


def _message_ts(msg):
    raw = oc(msg, 'messageTimestamp') if isinstance(msg, dict) else None
    if isinstance(raw, dict) or (raw is not None and hasattr(raw, 'low')):
        return oc(raw, 'low')
    return raw


def _keep_message(msg, start_epoch):
    ts = _message_ts(msg)
    if not truthy(ts):
        return True
    try:
        return float(ts) >= start_epoch
    except (TypeError, ValueError):
        return False


class ConnRef:
    def __init__(self, sock):
        object.__setattr__(self, '_sock', sock)

    def __getattr__(self, name):
        if name.startswith('__') or name == '_sock':
            raise AttributeError(name)
        return self[name]

    def __setattr__(self, name, value):
        self._sock[name] = value

    def __getitem__(self, name):
        value = self._sock.get(name)
        return value.get() if isinstance(value, _Lazy) else value

    def __setitem__(self, name, value):
        self._sock[name] = value

    def __contains__(self, name):
        return dict.__contains__(self._sock, name)

    def get(self, name, default=None):
        value = self[name]
        return default if value is None else value

    def rebind(self, sock):
        object.__setattr__(self, '_sock', sock)


def as_ref(obj):
    if isinstance(obj, ConnRef):
        return obj
    return ConnRef(obj)


async def start(old=None, opts=None):
    if old is not None:
        old = as_ref(old)
    if opts is None:
        opts = {'store': store, 'logger': logger, 'authState': auth_state}
    _flags['connGeneration'] += 1
    my_generation = _flags['connGeneration']

    apply_custom_pairing_code_patch(opts['authState']['state']['creds'])
    cfg = {**connection_options, **(opts.get('connectionOptions') or {})}
    cfg = {k: v for k, v in cfg.items() if v is not None}

    async def get_message(key):
        found = opts['store'].loadMessage(oc(key, 'remoteJid'), oc(key, 'id')) or opts['store'].loadMessage(oc(key, 'id'))
        return oc(found, 'message')

    def patch_message_before_sending(message, jids=None):
        requires_patch = bool(oc(message, 'buttonsMessage') or oc(message, 'templateMessage') or oc(message, 'listMessage'))
        if requires_patch:
            return JSObject({'viewOnceMessage': JSObject({'message': JSObject({
                'messageContextInfo': JSObject({'deviceListMetadataVersion': 2, 'deviceListMetadata': JSObject({})}),
                **message,
            })})})
        return message

    sock = makeWASocket(JSObject({
        **cfg,
        'logger': opts.get('logger'),
        'auth': JSObject({
            'creds': opts['authState']['state']['creds'],
            'keys': makeCacheableSignalKeyStore(opts['authState']['state']['keys'], opts.get('logger'), None),
        }),
        'generateHighQualityLinkPreview': True,
        'defaultQueryTimeoutMs': None,
        'getMessage': get_message,
        'patchMessageBeforeSending': patch_message_before_sending,
    }))

    async def profile_picture_url(jid, type_='preview', timeout_ms=None):
        decode = sock['decodeJid']
        target_jid = decode(jid) if callable(decode) else jid
        result = await sock['query'](JSObject({
            'tag': 'iq',
            'attrs': JSObject({'target': target_jid, 'to': '@s.whatsapp.net', 'type': 'get', 'xmlns': 'w:profile:picture'}),
            'content': [JSObject({'tag': 'picture', 'attrs': JSObject({'type': type_, 'query': 'url'})})],
        }), timeout_ms)
        content = oc(result, 'content')
        child = next((n for n in content if oc(n, 'tag') == 'picture'), None) if isinstance(content, list) else None
        return oc(child, 'attrs', 'url')

    sock['profilePictureUrl'] = profile_picture_url

    fresh = helper_connection(sock, opts['store'], opts.get('logger'))
    if old is not None:
        is_init = old.get('isInit')
        is_reload_init = old.get('isReloadInit')
        old.rebind(fresh)
        conn = old
        conn.isInit = is_init
        conn.isReloadInit = is_reload_init
    else:
        conn = ConnRef(fresh)
    conn.generation = my_generation
    if conn.isInit is None:
        conn.isInit = False
        conn.isReloadInit = True

    opts['store'].bind(conn.ev, {'groupMetadata': conn.groupMetadata, 'conn': conn})
    if state.conn is None:
        state.conn = conn

    await reload(conn, False, opts)
    return conn


async def reload(conn, restart_connection, opts=None):
    conn = as_ref(conn)
    if opts is None:
        opts = {'store': store, 'authState': auth_state}
    if not opts.get('handler'):
        opts['handler'] = _import_handler_module()
    if not opts.get('handler') and _old_handler['mod'] is not None:
        opts['handler'] = _old_handler['mod']
    _old_handler['mod'] = opts.get('handler')
    hmod = opts.get('handler')

    if restart_connection:
        try:
            await asyncio.wait_for(maybe_await(conn.ws.close()), 2)
        except Exception:
            pass
        conn.ev['removeAllListeners']()
        await asyncio.sleep(3)
        await start(conn, opts)
        return True

    for key, value in get_message_config().items():
        setattr(conn, key, value)

    ev = conn.ev
    if conn.handler:
        ev['off']('messages.upsert', conn.handler)
    if conn.participantsUpdate:
        ev['off']('group-participants.update', conn.participantsUpdate)
    if conn.groupsUpdate:
        ev['off']('groups.update', conn.groupsUpdate)
    if conn.onDelete:
        ev['off']('messages.delete', conn.onDelete)
    if conn.connectionUpdate:
        ev['off']('connection.update', conn.connectionUpdate)
    if conn.credsUpdate:
        ev['off']('creds.update', conn.credsUpdate)

    if hmod is not None:
        start_epoch = int(time.time())

        async def on_upsert(chat_update):
            if chat_update and chat_update.get('messages') is not None:
                chat_update['messages'] = [m for m in chat_update['messages'] if _keep_message(m, start_epoch)]
                if not chat_update['messages']:
                    return None
            return await hmod.handler(conn, chat_update)

        conn.handler = lambda chat_update: _spawn_result(on_upsert(chat_update))
        conn.participantsUpdate = lambda update: _spawn_result(hmod.participants_update(conn, update))
        conn.groupsUpdate = lambda update: _spawn_result(hmod.groups_update(conn, update))
        conn.onDelete = lambda update: _spawn_result(hmod.delete_update(conn, update))

    if not opts.get('isChild'):
        conn.connectionUpdate = lambda update: _spawn_result(connection_update(conn, opts, update))
    conn.credsUpdate = lambda *args: _spawn_result(opts['authState']['saveCreds']())

    if conn.handler:
        ev['on']('messages.upsert', conn.handler)
    if conn.participantsUpdate:
        ev['on']('group-participants.update', conn.participantsUpdate)
    if conn.groupsUpdate:
        ev['on']('groups.update', conn.groupsUpdate)
    if conn.onDelete:
        ev['on']('messages.delete', conn.onDelete)
    if not opts.get('isChild') and conn.connectionUpdate:
        ev['on']('connection.update', conn.connectionUpdate)
    if conn.credsUpdate:
        ev['on']('creds.update', conn.credsUpdate)

    conn.isReloadInit = False
    return True


async def _ask(prompt):
    return await asyncio.get_running_loop().run_in_executor(None, lambda: input(prompt))


async def _pair_with_code(conn, main_cfg, my_generation):
    conn = as_ref(conn)
    try:
        print(chalk.yellow('\nSocket ready, requesting pairing code from WhatsApp...'))
        pair_flag = helper.opts.get('pair')
        if pair_flag:
            phone = str(pair_flag).strip()
        else:
            phone = (await _ask('Enter your WhatsApp number (example: 1305xxxx):\n')).strip()

        if my_generation != _flags['connGeneration']:
            print(chalk.yellow('Pairing cancelled, connection was replaced'), file=sys.stderr)
            return

        raw_code = main_cfg.get('paircode')
        custom_pairing = ('' if raw_code is None else str(raw_code)).strip().upper()
        pairing_code = await conn.requestPairingCode(phone, custom_pairing)

        if my_generation != _flags['connGeneration']:
            print(chalk.yellow('Pairing cancelled, connection was replaced'), file=sys.stderr)
            return

        if pairing_code:
            formatted = f'{pairing_code[:4]}-{pairing_code[4:]}' if len(pairing_code) == 8 else pairing_code
            print(chalk.bold(f'~> {formatted}'))
        else:
            print(chalk.yellow('Did not receive pairing code, please try again'), file=sys.stderr)
            _flags['pairingCodeRequested'] = False
    except Exception as error:
        print(chalk.red('Error getting pairing code:'), safe_err(error), file=sys.stderr)
        print('• Make sure the WhatsApp number is correct and try again.')
        _flags['pairingCodeRequested'] = False
    finally:
        _flags['isPairingInProgress'] = False


async def _run_on_bot_open(fn):
    try:
        await maybe_await(fn())
    except Exception as e:
        print('Tunnel setup error:', safe_err(e), file=sys.stderr)


async def _restart_tunnel():
    fn = G.restartTunnel
    if not callable(fn):
        return
    try:
        await maybe_await(fn())
    except Exception as e:
        print('Tunnel restartTunnel error:', safe_err(e), file=sys.stderr)


def _set_socket_ready(conn):
    as_ref(conn).isSocketReady = True


async def connection_update(conn, opts, update):
    conn = as_ref(conn)
    connection = oc(update, 'connection')
    last_disconnect = oc(update, 'lastDisconnect')
    qr = oc(update, 'qr')

    if connection:
        print('Connection' + chalk.gray(f' {connection}'))

    for listener in list(extra_connection_listeners):
        try:
            listener(update)
        except Exception as err:
            print(chalk.red('connection.update listener error:'), safe_err(err), file=sys.stderr)

    loop = asyncio.get_running_loop()
    if connection == 'open':
        conn.isSocketReady = False
        loop.call_later(3, _set_socket_ready, conn)
    if connection == 'close':
        conn.isSocketReady = False

    main_cfg = oc(G.settings, 'connection', 'main') or {}
    qr_only_mode = bool(main_cfg.get('qr'))

    if qr and qr_only_mode:
        generateQR(qr, JSObject({'small': True}), lambda output: print('\n' + output))

    if qr and not qr_only_mode and not _flags['pairingCodeRequested'] and not _flags['isPairingInProgress']:
        _flags['pairingCodeRequested'] = True
        _flags['isPairingInProgress'] = True
        spawn(_pair_with_code(conn, main_cfg, conn.generation))

    error_obj = oc(last_disconnect, 'error')
    code = _or(oc(error_obj, 'output', 'statusCode'), oc(error_obj, 'output', 'payload', 'statusCode'))

    if connection == 'close':
        reason_msg = _or(oc(error_obj, 'output', 'payload', 'message'), _or(_error_message(error_obj), '(no message)'))
        print(chalk.red('[ DisconnectReal ]') + chalk.gray(f' code={_js(code)} message={reason_msg}'))

    non_recoverable = {
        DisconnectReason['loggedOut'], DisconnectReason['badSession'], DisconnectReason['connectionReplaced'],
        DisconnectReason['forbidden'], DisconnectReason['multideviceMismatch'],
    }
    err_msg = str(_error_message(error_obj) or '')
    is_transport_error = code == DisconnectReason['badSession'] and bool(re.match(r'^WebSocket Error', err_msg, re.IGNORECASE))
    should_reconnect = connection == 'close' and (is_transport_error or code not in non_recoverable)

    if should_reconnect:
        if _flags['isReconnecting']:
            print(chalk.yellow('Reconnect') + chalk.gray(' is running, skip...'))
            return
        if _flags['isPairingInProgress'] and conn.generation == _flags['connGeneration']:
            print(chalk.yellow('Reconnect') + chalk.gray(' delayed, pairing in progress...'))
            return
        _flags['isReconnecting'] = True
        try:
            retry_delay = 1 if code == DisconnectReason['connectionLost'] else 3
            await asyncio.sleep(retry_delay)
            try:
                await reload(conn, True, opts)
            except Exception as err:
                print(chalk.red('Reload error:'), safe_err(err), file=sys.stderr)
            _touch_connect_timestamp()
            _flags['pairingCodeRequested'] = False
        finally:
            _flags['isReconnecting'] = False

    if connection == 'open':
        _flags['isReconnecting'] = False
        on_bot_open = G.onBotOpen
        if callable(on_bot_open):
            G.onBotOpen = None
            spawn(_run_on_bot_open(on_bot_open))
        loop.call_later(5, lambda: spawn(_restart_tunnel()))

    if dbmod.database.data is None:
        try:
            await dbmod.load_database()
        except Exception as e:
            print('DB loadDatabase error:', safe_err(e), file=sys.stderr)


async def force_reconnect(conn, opts, reason='forced'):
    conn = as_ref(conn)
    if _flags['isReconnecting']:
        print(chalk.yellow('Reconnect') + chalk.gray(' is running, skip... (forced trigger ignored)'))
        return False
    if _flags['isPairingInProgress']:
        print(chalk.yellow('Reconnect') + chalk.gray(' delayed, pairing in progress... (forced trigger ignored)'))
        return False
    _flags['isReconnecting'] = True
    print(chalk.red('Reconnect') + chalk.gray(f' forcing reload (reason: {reason})'))
    max_attempts = 3
    try:
        for attempt in range(1, max_attempts + 1):
            await asyncio.sleep(1)
            try:
                await reload(conn, True, opts)
                _touch_connect_timestamp()
                _flags['pairingCodeRequested'] = False
                return True
            except Exception as err:
                print(chalk.red('Reload error:') + chalk.gray(f' (attempt {attempt}/{max_attempts})'), safe_err(err), file=sys.stderr)
                if attempt == max_attempts:
                    print(chalk.red('Reconnect') + chalk.gray(f' gave up after {max_attempts} failed attempts — falling back to a full process restart.'), file=sys.stderr)
                    asyncio.get_running_loop().call_later(0.2, lambda: setattr(G, 'force_exit', True))
                    return False
                await asyncio.sleep(2 * attempt)
    finally:
        _flags['isReconnecting'] = False
    return False


forceReconnect = force_reconnect


async def get_main_conn():
    while state.conn is None:
        await asyncio.sleep(0.2)
    return state.conn


async def conn():
    return await get_main_conn()


async def _after_start():
    if dbmod.database.data is None:
        try:
            await dbmod.load_database()
        except Exception as e:
            print('DB loadDatabase error:', safe_err(e), file=sys.stderr)
    try:
        from lib.utils import plugins as plugmod
        entry = plugmod.plugins.get('plugins/subbot/connect.py')
        module = getattr(entry, 'module', None) if entry is not None else None
        if module is None:
            module = helper.import_file(os.path.join(helper.ROOT, 'plugins', 'subbot', 'connect.py'))
        await maybe_await(module.auto_connect_sub_bots())
    except Exception as err:
        print('[Subbot] Autoconnect error:', safe_err(err), file=sys.stderr)


async def init():
    global logger, auth_state, connection_options
    auth_state = await resolve_auth_state(oc(G.settings, 'connection', 'main', 'type') or 'sql', 'main')

    try:
        store.readFromFile(store_file)
    except Exception as e:
        print('Store failed to read store file, starting with empty store:', _msg(e), file=sys.stderr)

    logger = createLogger(JSObject({'class': 'baileys'}), 'silent')
    ver = await fetch_version()
    main_cfg = oc(G.settings, 'connection', 'main') or {}
    connection_options = {
        'printQRInTerminal': False,
        'auth': auth_state['state'],
        'logger': logger,
        'version': ver.get('version'),
        'browser': main_cfg.get('browser'),
        'connectTimeoutMs': 60000,
        'keepAliveIntervalMs': 15000,
        'retryRequestDelayMs': 500,
        'syncFullHistory': bool(main_cfg.get('loadhistory')),
        'markOnlineOnConnect': bool(oc(G.settings, 'system', 'online')),
    }

    try:
        started = await start(None, {'store': store, 'logger': logger, 'authState': auth_state})
    except Exception as err:
        print(chalk.red('Start error:'), safe_err(err), file=sys.stderr)
        started = None

    spawn(_after_start())
    return started
