import json
import os
import random
import re
import sys
import time
from urllib.parse import parse_qsl, unquote, urlsplit

from lib.compat import http
from lib.utils.handler import G
from lib.utils.helper import oc


KEY = 'AIzaSyDtG1AU22ErnQD60AzBAcaknySiz9_CEq0'
IDT = 'https://www.googleapis.com/identitytoolkit/v3/relyingparty'
STK = 'https://securetoken.googleapis.com/v1/token'
VFY = 'https://us-central1-alight-creative.cloudfunctions.net/verifyPurchase'
TTL = 10 * 60 * 1000
FIREBASE_IID = 'cSDnCyp3T-uwp07z3tL86T:APA91bFkmvvsHw5nnqa1SBFci-99DRsKClLiETdRrVcJjS5yBx1v_FbCb1d8WhBuea_zmwnYBktyTIzcRhN4b6uNOUur9wPc0gKXmJDoZic0LhNq5V2s0xI'
PURCHASE_TOKEN = 'mmgaobamlahbbeccfplmbkbb.AO-J1OzqG0or_GJJIx-ms8GrTm-jaglCRfhQSRPUZKpl2YspYS-oN7_94uv8RC5vQbvd_Ios2pPDStZ2n7F0hLE3FiOU7HS3R6Fquulv5xLXFECSv4ctElw'

H1 = {
    'content-type': 'application/json',
    'x-android-package': 'com.alightcreative.motion',
    'x-android-cert': 'ECA6BF91B8715A6F810ED0BBFC65B6CD578F52A8',
    'user-agent': 'dalvik/2.1.0 (linux; u; android 15; 23127pn0cc build/bp1a.250505.005)',
}

H2 = {
    'content-type': 'application/json; charset=utf-8',
    'user-agent': 'okhttp/3.12.1',
    'accept-encoding': 'gzip',
}

IP_POOL = [
    '36.66.', '36.72.', '36.85.', '103.28.', '103.47.',
    '114.79.', '114.122.', '180.243.', '180.244.', '182.253.',
]

_RNG = random.SystemRandom()


def _rand(lo, hi):
    return _RNG.randrange(lo, hi)


def dip():
    return f'{_rand(1, 255)}.{_rand(0, 255)}.{_rand(0, 255)}.{_rand(1, 255)}'


def dip_id():
    base = IP_POOL[_rand(0, len(IP_POOL) - 1)]
    return f'{base}{_rand(1, 255)}.{_rand(1, 255)}'


def spoof(h):
    ip = dip_id()
    ip2 = dip_id()
    ip3 = dip()
    return {
        **h,
        'x-forwarded-for': f'{ip}, {ip2}',
        'x-real-ip': ip,
        'client-ip': ip,
        'x-client-ip': ip,
        'x-originating-ip': ip2,
        'x-cluster-client-ip': ip2,
        'x-forwarded-host': 'identitytoolkit.googleapis.com',
        'x-forwarded-proto': 'https',
        'forwarded': f'for={ip};proto=https;by={ip3}',
        'via': f'1.1 {ip3} (squid/3.5.27)',
        'true-client-ip': ip,
        'cf-connecting-ip': ip,
        'x-proxyuser-ip': ip2,
    }


def _js_truthy(v):
    if isinstance(v, (dict, list)):
        return True
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0 and v == v
    return bool(v)


def _js_str(v):
    if v is None:
        return 'undefined'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return str(v)


def _axios_data(res):
    text = res.content.decode('utf-8', errors='replace')
    if not text:
        return ''
    try:
        return json.loads(text)
    except Exception:
        return text


def _bad(e):
    res = getattr(e, 'response', None)
    if res is not None:
        d = _axios_data(res)
        if _js_truthy(d):
            if isinstance(d, (dict, list)):
                return json.dumps(d, separators=(',', ':'), ensure_ascii=False)
            return _js_str(d)
    return str(getattr(e, 'message', e))


def _decode_uri_component(s):
    if re.search(r'%(?![0-9A-Fa-f]{2})', s):
        raise ValueError('URI malformed')
    return unquote(s, errors='strict')


def _parse_url(s):
    if not re.match(r'^[A-Za-z][A-Za-z0-9+.\-]*:', s):
        raise ValueError('Invalid URL')
    return urlsplit(s)


def _sp_get(query, key):
    for k, v in parse_qsl(query, keep_blank_values=True):
        if k == key:
            return v
    return None


def extract_code(raw):
    if not raw:
        return None
    s = str(raw).replace('&amp;', '&')
    try:
        s = _decode_uri_component(s)
    except Exception:
        pass
    try:
        u = _parse_url(s)
        c = _sp_get(u.query, 'oobCode')
        if not c:
            n = _sp_get(u.query, 'link') or _sp_get(u.query, 'q') or _sp_get(u.query, 'url')
            if n:
                try:
                    c = _sp_get(_parse_url(n).query, 'oobCode')
                except Exception:
                    pass
        if c:
            return re.sub(r'[^a-zA-Z0-9_\-]', '', c)
    except Exception:
        pass
    mt = re.search(r'oobCode=([a-zA-Z0-9_\-]+)', s, re.I)
    if mt:
        return mt.group(1)
    t = str(raw).strip()
    if re.fullmatch(r'[a-zA-Z0-9_\-]{10,}', t) and '://' not in t:
        return t
    return None


async def send_magic_link(email):
    try:
        await http.post(f'{IDT}/getOobConfirmationCode?key={KEY}', json={
            'requestType': 6,
            'email': email,
            'androidInstallApp': True,
            'canHandleCodeInApp': True,
            'continueUrl': 'https://alightcreative.com?ui_sid=0366624874&ui_sd=0',
            'iosBundleId': 'com.alightcreative.motion',
            'androidPackageName': 'com.alightcreative.motion',
            'androidMinimumVersion': '585',
            'clientType': 'CLIENT_TYPE_ANDROID',
        }, headers=spoof(H1), timeout=30)
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'why': _bad(e)}


async def verify_link(email, raw_link):
    c = extract_code(raw_link)
    if not c:
        return {'ok': False, 'why': 'oobCode tidak ditemukan di link'}
    try:
        a = await http.post(f'{IDT}/emailLinkSignin?key={KEY}', json={'email': email, 'oobCode': c, 'clientType': 'CLIENT_TYPE_ANDROID'},
                            headers=spoof(H1), timeout=30)
        d = _axios_data(a)
        if not isinstance(d, dict):
            d = {}
        return {'ok': True, 'id': d.get('idToken'), 'ref': d.get('refreshToken'), 'uid': d.get('localId'), 'new': bool(d.get('isNewUser'))}
    except Exception as e:
        return {'ok': False, 'why': _bad(e)}


async def activate_premium(id_token):
    order_id = os.urandom(6).hex()
    try:
        headers = spoof({
            **H2,
            'authorization': 'Bearer ' + _js_str(id_token),
            'firebase-instance-id-token': FIREBASE_IID,
        })
        r = await http.post(VFY, json={'data': {
            'productId': 'am.full.sub.annual.19q4',
            'token': PURCHASE_TOKEN,
            'skuType': 'subs',
            'orderId': order_id,
        }}, headers=headers, timeout=30)
        return {'ok': True, 'orderId': order_id, 'data': _axios_data(r)}
    except Exception as e:
        return {'ok': False, 'why': _bad(e)}


async def refresh_and_activate(refresh_token):
    try:
        r = await http.post(f'{STK}?key={KEY}', json={'grant_type': 'refresh_token', 'refresh_token': refresh_token},
                            headers=spoof(H2), timeout=30)
        d = _axios_data(r)
        if not isinstance(d, dict):
            d = {}
        result = await activate_premium(d.get('id_token'))
        return {**result, 'newRef': d.get('refresh_token')}
    except Exception as e:
        return {'ok': False, 'why': _bad(e)}


def _settings():
    data = G.db.data
    if data.get('settings') is None:
        data['settings'] = {}
    return data['settings']


def _user_settings(user_jid):
    settings = _settings()
    if settings.get(user_jid) is None:
        settings[user_jid] = {}
    return settings[user_jid]


def get_session(user_jid, email):
    user = _user_settings(user_jid)
    if user.get('amSession') is None:
        user['amSession'] = {}
    return user['amSession'].get(email)


async def save_session(user_jid, email, data):
    user = _user_settings(user_jid)
    if user.get('amSession') is None:
        user['amSession'] = {}
    user['amSession'][email] = {**data, 'savedAt': int(time.time() * 1000)}
    try:
        await G.db.write()
    except Exception as e:
        print('[alightmotion] db write failed:', e, file=sys.stderr)


def _ampro(conn):
    store = getattr(conn, 'ampro', None)
    if store is None:
        store = {}
        setattr(conn, 'ampro', store)
    return store


async def handler(m, *, conn, text, command):
    if not text:
        return await m.reply('Usage:\n- `.ampro <email>` - send magic link to email\n- `.amrefresh <email>` - re-activate from saved session\n\nExample: `.ampro user@gmail.com`')

    user_jid = conn.user['jid']

    if re.match(r'^amrefresh$', command, re.I):
        email = text.strip().lower()
        sess = get_session(user_jid, email)
        if sess is None:
            return await m.reply(f'No saved session for *{email}*.\n\nSend a link first: `.ampro {email}`')
        await m.react('🕜')
        r = await refresh_and_activate(sess.get('ref'))
        if not r['ok']:
            return await m.reply(f'Failed:\n`{r["why"]}`\n\nTry again: `.ampro {email}`')
        await save_session(user_jid, email, {**sess, 'ref': r.get('newRef') or sess.get('ref')})
        return await m.reply(f'Premium updated!\n{email}\nOrder: `{r["orderId"]}`')

    email = text.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        return await m.reply('Invalid email format.')

    await m.react('📩')
    r = await send_magic_link(email)
    if not r['ok']:
        return await m.reply(f'Failed to send link:\n`{r["why"]}`')

    prompt = await conn.sendMessage(m.chat, {
        'text': f'Magic link sent to *{email}*!\n\nOpen your email and copy the link from AlightMotion.\n*Reply to this message* with the link.',
    }, {'quoted': m})
    _ampro(conn)[m.sender] = {
        'email': email,
        'messageId': oc(prompt, 'key', 'id'),
        'at': int(time.time() * 1000),
    }


async def before(m, *, conn):
    qid = oc(m.quoted, 'id')
    if not qid:
        return None
    store = getattr(conn, 'ampro', None)
    state = store.get(m.sender) if store else None
    if not state:
        return None
    if int(time.time() * 1000) - state['at'] > TTL:
        store.pop(m.sender, None)
        return None
    if state['messageId'] != qid:
        return None
    raw_link = (m.text or '').strip()
    if not raw_link:
        return None
    code = extract_code(raw_link)
    if not code:
        return None

    store.pop(m.sender, None)
    user_jid = conn.user['jid']
    await m.react('🕜')

    v = await verify_link(state['email'], raw_link)
    if not v['ok']:
        await m.reply(f'Verification failed:\n`{v["why"]}`\n\nSend again: `.ampro {state["email"]}`')
        return True

    await m.react('✅')

    p = await activate_premium(v['id'])
    session = {'id': v['id'], 'ref': v['ref'], 'uid': v['uid']}
    if not p['ok']:
        await save_session(user_jid, state['email'], session)
        await m.reply(f'Login OK but activation failed:\n`{p["why"]}`\n\nTry: `.amrefresh {state["email"]}`')
        return True

    await save_session(user_jid, state['email'], session)
    created = 'New account created' if v['new'] else 'Existing account updated'
    await m.reply(
        '*AlightMotion Premium Activated!*\n\n'
        f'- Email: *{state["email"]}*\n'
        f'- Order: `{p["orderId"]}`\n'
        f'{created}\n\n'
        f'_Use `.amrefresh {state["email"]}` to re-activate anytime._'
    )
    return True


help = ['ampro <email>', 'amrefresh <email>']
command = re.compile(r'^(ampro|amrefresh)$', re.I)
tags = ['tools']
