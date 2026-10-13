import asyncio
import base64
import concurrent.futures
import copy
import datetime
import email.utils
import hashlib
import hmac
import http.client
import importlib
import inspect
import ipaddress
import json
import math
import os
import posixpath
import random
import re
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lib.utils.color import chalk
from lib.utils.database import db, load_database
from lib.utils.handler import G


UNDEF = object()
NAN = float('nan')
PROCESS_START = time.monotonic()

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(SERVER_DIR, '..', '..', '..'))
VIEWS_DIR = os.path.join(SERVER_DIR, 'views')

MIME_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
}

UPLOAD_DIR = os.path.join(os.getcwd(), 'data', 'upload')
UPLOAD_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000
UPLOAD_MAX_BODY_BYTES = 100 * 1024 * 1024

UPLOAD_MIME_TYPES = {
    **MIME_TYPES,
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mkv': 'video/x-matroska',
    '.mov': 'video/quicktime',
    '.3gp': 'video/3gpp',
    '.mp3': 'audio/mpeg',
    '.m4a': 'audio/mp4',
    '.ogg': 'audio/ogg',
    '.opus': 'audio/ogg',
    '.wav': 'audio/wav',
    '.pdf': 'application/pdf',
    '.zip': 'application/zip',
    '.txt': 'text/plain; charset=utf-8',
}

WS_MAGIC = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
WS_MAX_PAYLOAD = 1024 * 1024
WS_HEARTBEAT_S = 30
WS_CLOSE_TIMEOUT_S = 3
WS_CONNECTING = 0
WS_OPEN = 1
WS_CLOSING = 2
WS_CLOSED = 3

REQUEST_TIMEOUT_S = 25
CORS_ENABLED_PATHS = [
    '/api/aiRich/action',
    '/api/login',
    '/api/register',
    '/api/verify-otp',
    '/api/resend-otp',
]

GACHA_TITLES = {
    '✦': 'ULTRA JACKPOT! ✦✦✦',
    '7': 'JACKPOT! 7 7 7',
    '♦': 'TRIPLE DIAMOND!',
    '♣': 'TRIPLE CLUB!',
    '♥': 'TRIPLE HEART!',
    '♠': 'TRIPLE SPADE!',
    '♪': 'TRIPLE NOTE!',
    '✿': 'TRIPLE FLOWER!',
    '×': 'TRIPLE CROSS!',
}

DEFAULT_SITE_SETTINGS = {
    'daily': {'exp': 500, 'gems': 5},
    'shop': {'limitPrice': 1, 'premiumPrice': 100},
    'gacha': {
        'symbols': [
            {'key': '✦', 'chance': 0.001, 'reward': 200000},
            {'key': '7', 'chance': 0.1, 'reward': 77777},
            {'key': '♦', 'chance': 0.5, 'reward': 10000},
            {'key': '♣', 'chance': 1, 'reward': 5000},
            {'key': '♥', 'chance': 1.5, 'reward': 3000},
            {'key': '♠', 'chance': 2, 'reward': 2000},
            {'key': '♪', 'chance': 3, 'reward': 1000},
            {'key': '✿', 'chance': 4, 'reward': 500},
            {'key': '×', 'chance': 30, 'reward': -300},
        ]
    },
    'subbot': {'mode': 'free'},
}

BRUTE_WINDOW_MS = 10 * 60 * 1000
BRUTE_MAX = 5
BRUTE_BLOCK_MS = 15 * 60 * 1000

OTP_TTL_MS = 5 * 60 * 1000
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_MS = 60 * 1000
HTML_ACTION_TTL_MS = 15 * 60 * 1000
PROXY_CACHE_MAX_ITEMS = 120
PROXY_CACHE_MAX_BYTES = 6 * 1024 * 1024
PROXY_CACHE_TTL_MS = 15 * 60 * 1000
SAFE_HOST_TTL_MS = 5 * 60 * 1000
PREMIUM_WINDOW_MS = 24 * 60 * 60 * 1000
PREMIUM_GRACE_MS = 24 * 60 * 60 * 1000
MONTHS_EN = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]
MAX_TIER_TITLE_NAMES = 10

STATE = {
    'conn': None,
    'loop': None,
    'server': None,
    'wss': None,
    'unpipe': None,
    'routes': None,
    'started': False,
    'tasks': set(),
}

_attempts = {}
_attempts_lock = threading.Lock()
_pending_otp = {}
_pending_html_actions = {}
_ws_channels = {}
_channels_lock = threading.Lock()
_proxy_cache = OrderedDict()
_proxy_cache_lock = threading.Lock()
_safe_host_cache = {}
_safe_host_lock = threading.Lock()


def now_ms():
    return int(time.time() * 1000)


def uptime():
    return time.monotonic() - PROCESS_START


def is_nan(value):
    return isinstance(value, float) and value != value


def is_finite_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and not is_nan(value) and value not in (math.inf, -math.inf)


def num_str(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if value != value:
        return 'NaN'
    if value == math.inf:
        return 'Infinity'
    if value == -math.inf:
        return '-Infinity'
    if float(value).is_integer() and abs(value) < 1e21:
        return str(int(value))
    text = repr(float(value))
    if 'e' in text:
        mant, exp = text.split('e')
        exp_val = int(exp)
        text = f"{mant}e{'+' if exp_val >= 0 else '-'}{abs(exp_val)}"
    return text


def js_str(value):
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, (int, float)):
        return num_str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ','.join('' if item is None else js_str(item) for item in value)
    if isinstance(value, dict):
        return '[object Object]'
    return str(value)


def js_strlen(value):
    if isinstance(value, str):
        return len(value.encode('utf-16-le')) // 2
    if isinstance(value, (list, tuple)):
        return len(value)
    return None


def js_number(value):
    if value is None or value is False:
        return 0
    if value is True:
        return 1
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text == '':
            return 0
        if re.fullmatch(r'[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?', text):
            number = float(text)
        elif re.fullmatch(r'[+-]?Infinity', text):
            number = math.inf if text[0] != '-' else -math.inf
        elif re.fullmatch(r'0[xX][0-9a-fA-F]+', text):
            return int(text, 16)
        else:
            return NAN
        if number.is_integer() and abs(number) < 2 ** 53:
            return int(number)
        return number
    if isinstance(value, (list, tuple)):
        return js_number(js_str(value))
    return NAN


def js_parse_int(value):
    text = js_str(value).lstrip()
    hex_match = re.match(r'[+-]?0[xX]([0-9a-fA-F]+)', text)
    if hex_match:
        sign = -1 if text.startswith('-') else 1
        return sign * int(hex_match.group(1), 16)
    match = re.match(r'[+-]?[0-9]+', text)
    if not match:
        return NAN
    return int(match.group(0))


def js_parse_float(value):
    text = js_str(value).lstrip()
    match = re.match(r'[+-]?(Infinity|(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?)', text)
    if not match:
        return NAN
    token = match.group(0)
    if token.lstrip('+-') == 'Infinity':
        return -math.inf if token.startswith('-') else math.inf
    number = float(token)
    if number.is_integer() and abs(number) < 2 ** 53:
        return int(number)
    return number


def js_round(value):
    return math.floor(value + 0.5)


def truthy(value):
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0 and not is_nan(value)
    if isinstance(value, str):
        return len(value) > 0
    return True


def or_default(value, fallback):
    return value if truthy(value) else fallback


def nullish(value, fallback):
    return fallback if value is None else value


def jget(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    if isinstance(obj, (list, tuple)):
        if isinstance(key, int) and 0 <= key < len(obj):
            return obj[key]
        return None
    if isinstance(obj, str):
        if isinstance(key, int) and 0 <= key < len(obj):
            return obj[key]
        return None
    return getattr(obj, key, None)


def dget(body, key):
    if body is None:
        raise TypeError(f"Cannot destructure property '{key}' of null")
    return jget(body, key)


def idx(seq, index):
    if seq is None or not isinstance(index, int) or isinstance(index, bool):
        return None
    if 0 <= index < len(seq):
        return seq[index]
    return None


def require_str(value):
    if not isinstance(value, str):
        raise TypeError('value.trim is not a function')
    return value


def to_upper(value):
    if not isinstance(value, str):
        raise TypeError('value.toUpperCase is not a function')
    return value.upper()


def to_lower(value):
    if not isinstance(value, str):
        raise TypeError('value.toLowerCase is not a function')
    return value.lower()


def js_basename(path_text):
    stripped = path_text.rstrip('/')
    if not stripped:
        return ''
    return stripped.split('/')[-1]


def js_extname(path_text):
    return posixpath.splitext(js_basename(path_text))[1]


def decode_uri_component(text):
    if re.search(r'%(?![0-9A-Fa-f]{2})', text):
        raise ValueError('URI malformed')
    return urllib.parse.unquote_to_bytes(text).decode('utf-8')


def normalize_pathname(path_text):
    if not path_text.startswith('/'):
        path_text = '/' + path_text
    parts = path_text.split('/')
    out = []
    last_index = len(parts) - 1
    for index in range(1, len(parts)):
        segment = parts[index]
        is_last = index == last_index
        if segment in ('.', '%2e', '%2E'):
            if is_last:
                out.append('')
            continue
        if segment.lower() in ('..', '.%2e', '%2e.', '%2e%2e'):
            if out:
                out.pop()
            if is_last:
                out.append('')
            continue
        out.append(segment)
    return '/' + '/'.join(out)


def dumps(obj):
    return json.dumps(clean_json(obj), separators=(',', ':'), ensure_ascii=False)


def clean_json(value):
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value.is_integer() and abs(value) < 1e21:
            return int(value)
        return value
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items() if item is not UNDEF}
    if isinstance(value, (list, tuple)):
        return [clean_json(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return {'type': 'Buffer', 'data': list(value)}
    if callable(value):
        return None
    return str(value)


def log_err(*args):
    print(*args, file=sys.stderr)


def log_warn(*args):
    print(*args, file=sys.stderr)


def spawn(awaitable):
    loop = STATE['loop']
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is None or running is not loop:
        if loop is None or not inspect.iscoroutine(awaitable):
            raise RuntimeError('website loop is not available')
        return asyncio.run_coroutine_threadsafe(awaitable, loop)
    task = asyncio.ensure_future(awaitable)
    STATE['tasks'].add(task)
    task.add_done_callback(STATE['tasks'].discard)
    return task


async def resolve(value):
    if inspect.isawaitable(value):
        return await value
    return value


def conn_fn(conn, name):
    fn = jget(conn, name)
    if not callable(fn):
        raise TypeError(f'{name} is not a function')
    return fn


def exc_message(err):
    message = jget(err, 'message')
    if isinstance(message, str) and message:
        return message
    return str(err)


def get_path(obj, *keys):
    cur = obj
    for key in keys:
        cur = jget(cur, key)
        if cur is None:
            return None
    return cur


def users_of():
    return (db.data or {}).get('users') or {}


def sessions_of():
    return (db.data or {}).get('sessions') or {}


def ensure_users_dict():
    if db.data.get('users') is None:
        db.data['users'] = {}
    return db.data['users']


def ensure_sessions_dict():
    if db.data.get('sessions') is None:
        db.data['sessions'] = {}
    return db.data['sessions']


def settings_of():
    return G.settings


def tier_of():
    return G.settings['tier']


def tier_names():
    return tier_of()['name']


def tier_exp():
    return tier_of()['exp_required']


def tier_limits():
    return tier_of()['limit_capacity']


def date_en_gb(ms):
    if ms is None or is_nan(ms):
        return 'Invalid Date'
    try:
        when = datetime.datetime.fromtimestamp(ms / 1000)
    except (OverflowError, OSError, ValueError):
        return 'Invalid Date'
    return f'{when.day} {MONTHS_EN[when.month - 1]} {when.year}'


def fmt_exp_amount(value):
    absolute = abs(value)
    if absolute >= 1000:
        body = f'{num_str(math.floor(absolute / 100) / 10)}k'
    else:
        body = num_str(absolute)
    sign = '−' if value < 0 else '+'
    return f'{sign}{body}'


def is_int_value(value):
    return is_finite_num(value) and float(value).is_integer()


def build_site_settings(input_value):
    out = copy.deepcopy(DEFAULT_SITE_SETTINGS)
    if not isinstance(input_value, dict):
        return {'value': out}

    def int_field(value, low, high, label):
        number = js_number(value)
        if value == '' or value is None or not is_int_value(number) or number < low or number > high:
            raise ValueError(f'{label} must be a whole number between {low} and {high}.')
        return int(number)

    try:
        daily = input_value.get('daily')
        if isinstance(daily, dict):
            if 'exp' in daily:
                out['daily']['exp'] = int_field(daily['exp'], 0, 1000000000, 'Daily EXP')
            if 'gems' in daily:
                out['daily']['gems'] = int_field(daily['gems'], 0, 1000000, 'Daily Gems')
        shop = input_value.get('shop')
        if isinstance(shop, dict):
            if 'limitPrice' in shop:
                out['shop']['limitPrice'] = int_field(shop['limitPrice'], 1, 1000000, 'Limit price')
            if 'premiumPrice' in shop:
                out['shop']['premiumPrice'] = int_field(shop['premiumPrice'], 1, 10000000, 'Premium price')
        subbot = input_value.get('subbot')
        if isinstance(subbot, dict) and 'mode' in subbot:
            if subbot['mode'] != 'free' and subbot['mode'] != 'premium':
                raise ValueError('Subbot mode must be free or premium.')
            out['subbot']['mode'] = subbot['mode']
        gacha = input_value.get('gacha')
        if isinstance(gacha, dict) and isinstance(gacha.get('symbols'), list):
            given_list = gacha['symbols']
            for sym in out['gacha']['symbols']:
                given = None
                for candidate in given_list:
                    if truthy(candidate) and isinstance(candidate, dict) and candidate.get('key') == sym['key']:
                        given = candidate
                        break
                if given is None:
                    continue
                if 'chance' in given:
                    raw_chance = given['chance']
                    number = js_number(raw_chance)
                    if raw_chance == '' or raw_chance is None or not is_finite_num(number) or number < 0 or number > 100:
                        raise ValueError(f"Chance for {sym['key']} must be between 0 and 100.")
                    sym['chance'] = js_round(number * 1000) / 1000
                if 'reward' in given:
                    sym['reward'] = int_field(given['reward'], -1000000000, 1000000000, f"Reward for {sym['key']}")
            total = 0
            for sym in out['gacha']['symbols']:
                total = total + sym['chance']
            if total > 100.0000001:
                raise ValueError(f"Total chance is {num_str(js_round(total * 1000) / 1000)}%. It must not exceed 100%.")
    except ValueError as err:
        return {'error': str(err)}
    return {'value': out}


def get_site_settings():
    result = build_site_settings((db.data or {}).get('siteSettings'))
    if 'error' in result:
        return copy.deepcopy(DEFAULT_SITE_SETTINGS)
    return result['value']


def generate_token(phone):
    return hashlib.sha256((js_str(phone) + str(now_ms()) + secrets.token_hex(8)).encode('utf-8')).hexdigest()


def password_bytes(password):
    if not isinstance(password, str):
        raise TypeError('The "password" argument must be of type string')
    return password.encode('utf-8', 'replace')


def scrypt_derive(password, salt):
    return hashlib.scrypt(password_bytes(password), salt=salt.encode('utf-8'), n=16384, r=8, p=1, dklen=64, maxmem=64 * 1024 * 1024)


async def hash_password(password):
    salt = secrets.token_hex(16)
    derived = await asyncio.to_thread(scrypt_derive, password, salt)
    return f'{salt}:{derived.hex()}'


async def verify_password(password, stored):
    if not truthy(stored):
        return False
    if not isinstance(stored, str):
        raise TypeError('stored.split is not a function')
    parts = stored.split(':')
    if len(parts) != 2 or len(parts[1]) != 128:
        left = password_bytes(password)
        right = stored.encode('utf-8', 'replace')
        if len(left) != len(right):
            return False
        return hmac.compare_digest(left, right)
    salt, hash_hex = parts
    derived = await asyncio.to_thread(scrypt_derive, password, salt)
    try:
        hash_buf = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    if len(derived) != len(hash_buf):
        return False
    return hmac.compare_digest(derived, hash_buf)


def check_memory_rate_limit(key, window_ms, max_count, block_ms):
    now = now_ms()
    with _attempts_lock:
        rec = _attempts.get(key)
        if rec and rec['blockedUntil'] and now < rec['blockedUntil']:
            return f"Too many attempts. Try again in {math.ceil((rec['blockedUntil'] - now) / 1000)}s."
        if not rec or now - rec['firstAt'] > window_ms:
            rec = {'count': 0, 'firstAt': now, 'blockedUntil': 0}
        rec['count'] += 1
        if rec['count'] > max_count:
            rec['blockedUntil'] = now + block_ms
            _attempts[key] = rec
            return f'Too many attempts. Try again in {math.ceil(block_ms / 1000)}s.'
        _attempts[key] = rec
        return None


def cleanup_memory_rate_limit():
    now = now_ms()
    with _attempts_lock:
        for key, rec in list(_attempts.items()):
            if now - rec['firstAt'] > 60 * 60 * 1000 and (not rec['blockedUntil'] or now > rec['blockedUntil']):
                del _attempts[key]


def ensure_bruteforce_store():
    if db.data.get('bruteforce') is None:
        db.data['bruteforce'] = {}


async def check_db_rate_limit(key, window_ms=BRUTE_WINDOW_MS, max_count=BRUTE_MAX, block_ms=BRUTE_BLOCK_MS):
    await ensure_db()
    ensure_bruteforce_store()
    store = db.data['bruteforce']
    now = now_ms()
    rec = store.get(key)
    if rec and rec.get('blockedUntil') and now < rec['blockedUntil']:
        return f"Too many attempts. Try again in {math.ceil((rec['blockedUntil'] - now) / 1000)}s."
    if not rec or now - rec['firstAt'] > window_ms:
        rec = {'count': 0, 'firstAt': now, 'blockedUntil': 0}
    rec['count'] += 1
    if rec['count'] > max_count:
        rec['blockedUntil'] = now + block_ms
        store[key] = rec
        await db.write()
        return f'Too many attempts. Try again in {math.ceil(block_ms / 1000)}s.'
    store[key] = rec
    await db.write()
    return None


async def clear_db_rate_limit(key):
    await ensure_db()
    ensure_bruteforce_store()
    store = db.data['bruteforce']
    if store.get(key) is not None:
        del store[key]
        await db.write()


async def cleanup_bruteforce():
    try:
        await ensure_db()
        ensure_bruteforce_store()
        now = now_ms()
        changed = False
        store = db.data['bruteforce']
        for key, rec in list(store.items()):
            window_expired = now - rec['firstAt'] > BRUTE_WINDOW_MS
            block_expired = not rec.get('blockedUntil') or now > rec['blockedUntil']
            if window_expired and block_expired:
                del store[key]
                changed = True
        if changed:
            await db.write()
    except Exception as err:
        log_err('[bruteforce cleanup]', err)


def register_html_action(opts, ttl_ms=HTML_ACTION_TTL_MS):
    run = jget(opts, 'run')
    if not callable(run):
        raise TypeError('registerHtmlAction requires a `run` function')
    token = secrets.token_hex(16)
    single_use = opts['singleUse'] if 'singleUse' in opts else True
    _pending_html_actions[token] = {
        'chatId': jget(opts, 'chatId'),
        'run': run,
        'singleUse': single_use,
        'expiresAt': now_ms() + ttl_ms,
    }
    return token


async def perform_html_action(conn, token, args=None):
    if args is None:
        args = {}
    rec = _pending_html_actions.get(token) if isinstance(token, str) else None
    if rec is None:
        return {'success': False, 'message': 'This action has expired or was already used.'}
    if now_ms() > rec['expiresAt']:
        _pending_html_actions.pop(token, None)
        return {'success': False, 'message': 'This action has expired.'}
    if truthy(rec['singleUse']):
        _pending_html_actions.pop(token, None)
    try:
        result = await resolve(rec['run'](conn, rec['chatId'], args))
        if isinstance(result, list):
            result = {str(index): item for index, item in enumerate(result)}
        if isinstance(result, dict):
            return {'success': True, **result}
        return {'success': True, 'message': 'Done.'}
    except Exception as err:
        log_err('[htmlAction]', exc_message(err))
        return {'success': False, 'message': exc_message(err) or 'Action failed.'}


def subscribe_ws(ws, channel):
    with _channels_lock:
        _ws_channels.setdefault(channel, set()).add(ws)


def unsubscribe_ws_from_all(ws):
    with _channels_lock:
        for members in _ws_channels.values():
            members.discard(ws)


def broadcast_to_channel(channel, payload, except_ws=None):
    with _channels_lock:
        members = list(_ws_channels.get(channel, ()))
    if not members:
        return
    data = dumps(payload)
    for ws in members:
        if ws is except_ws:
            continue
        if ws.readyState == WS_OPEN:
            ws.send(data)


def generate_otp_code():
    return str(int.from_bytes(secrets.token_bytes(4), 'big') % 1000000).zfill(6)


def err_status_code(err):
    status = get_path(err, 'output', 'statusCode')
    if status is None:
        status = get_path(err, 'data', 'output', 'statusCode')
    return status


def is_transient_428(err):
    return err_status_code(err) == 428 or jget(err, 'message') == 'Connection Closed'


async def send_otp(conn, target_key, purpose):
    existing = _pending_otp.get(target_key)
    if existing and now_ms() - (existing.get('sentAt') or 0) < OTP_RESEND_COOLDOWN_MS:
        return {'ok': False, 'message': 'Please wait before requesting another code.'}

    code = generate_otp_code()
    pending_token = generate_token(target_key)
    _pending_otp[target_key] = {
        'code': code,
        'purpose': purpose,
        'expiresAt': now_ms() + OTP_TTL_MS,
        'sentAt': now_ms(),
        'attempts': 0,
        'pendingToken': pending_token,
    }

    user_rec = users_of().get(target_key)
    display_name = or_default(jget(user_rec, 'name'), normalize_phone(target_key))
    purpose_label = 'registration' if purpose == 'register' else 'login'
    otp_message = {
        'text': f"Hi {js_str(display_name)}. Here's your {purpose_label} code, don't give this code to anyone.\n\nCode expires in 5 minutes.",
        'nativeFlow': [{'text': 'COPY CODE', 'copy': code}],
    }

    retry_delays = [1000, 2000]
    if jget(conn, 'isSocketReady') is False:
        wait_start = time.monotonic()
        while jget(conn, 'isSocketReady') is False and (time.monotonic() - wait_start) * 1000 < 8000:
            await asyncio.sleep(0.25)

    last_err = None
    for attempt in range(3):
        try:
            await resolve(conn_fn(conn, 'sendButton')(target_key, otp_message))
            last_err = None
            break
        except Exception as err:
            last_err = err
            is_last_attempt = attempt == 2
            if not is_last_attempt and is_transient_428(err):
                delay = idx(retry_delays, attempt)
                if delay is None:
                    delay = 2000
                log_warn(f'[OTP SEND] transient error (attempt {attempt + 1}/3), retrying in {delay}ms...')
                await asyncio.sleep(delay / 1000)
                continue
            break

    if last_err is not None:
        log_err('[OTP SEND]', last_err)
        _pending_otp.pop(target_key, None)
        return {'ok': False, 'message': 'Failed to send code. Make sure the number is registered on WhatsApp.'}

    return {'ok': True, 'pendingToken': pending_token}


def verify_otp(target_key, pending_token, code):
    rec = _pending_otp.get(target_key)
    if rec is None:
        return {'ok': False, 'message': 'No pending code for this number. Please request a new one.'}
    if rec['pendingToken'] != pending_token:
        return {'ok': False, 'message': 'Invalid session. Please request a new code.'}
    if now_ms() > rec['expiresAt']:
        _pending_otp.pop(target_key, None)
        return {'ok': False, 'message': 'Code expired. Please request a new one.'}
    if rec['attempts'] >= OTP_MAX_ATTEMPTS:
        _pending_otp.pop(target_key, None)
        return {'ok': False, 'message': 'Too many incorrect attempts. Please request a new code.'}

    rec['attempts'] += 1
    if rec['code'] != js_str(code).strip():
        return {'ok': False, 'message': f"Incorrect code. {OTP_MAX_ATTEMPTS - rec['attempts']} attempt(s) left."}

    _pending_otp.pop(target_key, None)
    return {'ok': True, 'purpose': rec['purpose']}


def cleanup_otp():
    now = now_ms()
    for key, rec in list(_pending_otp.items()):
        if now > rec['expiresAt']:
            _pending_otp.pop(key, None)


def cleanup_html_actions():
    now = now_ms()
    for token, rec in list(_pending_html_actions.items()):
        if now > rec['expiresAt']:
            _pending_html_actions.pop(token, None)


async def ensure_db():
    if db.data is None:
        await load_database()


def push_notification(user, message):
    if not isinstance(user.get('notification'), list):
        user['notification'] = []
    user['notification'].append({'message': message, 'time': now_ms()})
    if len(user['notification']) > 10:
        user['notification'] = user['notification'][-10:]


async def send_wa_notif(conn, target_key, message):
    if conn is None or not truthy(target_key):
        return
    try:
        if callable(jget(conn, 'sendMessage')):
            await resolve(conn.sendMessage(target_key, {'text': message}))
        elif callable(jget(conn, 'sendButton')):
            await resolve(conn.sendButton(target_key, {'text': message}))
    except Exception as err:
        log_err('[WA NOTIF]', err)


async def with_timeout(awaitable, ms=4000):
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=ms / 1000)
    if task in done:
        return task.result()
    return None


def write_bg():
    async def run():
        try:
            await db.write()
        except Exception as err:
            log_err('[DB WRITE]', err)
    spawn(run())


def get_token(req):
    auth = req.header('authorization')
    if not truthy(auth):
        return None
    return auth.replace('Bearer ', '', 1)


def get_user_key_from_token(token):
    if not truthy(token):
        return None
    return sessions_of().get(token)


def get_client_ip(req):
    xff = req.header('x-forwarded-for')
    if truthy(xff):
        return xff.split(',')[0].strip()
    return req.client_ip


def ip_session_key(req):
    return f'ip:{get_client_ip(req)}'


def get_user_key_from_req(req):
    by_token = get_user_key_from_token(get_token(req))
    if truthy(by_token):
        return by_token
    return sessions_of().get(ip_session_key(req)) or None


def normalize_phone(raw):
    if not truthy(raw):
        return ''
    if not isinstance(raw, str):
        raise TypeError('raw.replace is not a function')
    return re.sub(r'@[^\n\r  ]*\Z', '', raw, count=1).strip()


def check_owner(token):
    user_key = get_user_key_from_token(token)
    if not truthy(user_key):
        return {'userKey': None, 'isOwner': False}
    phone_number = js_str(user_key).replace('@s.whatsapp.net', '')
    owners = settings_of().get('owner') or []
    is_owner = any(jget(owner, 0) == phone_number for owner in owners)
    return {'userKey': user_key, 'isOwner': is_owner}


def conn_bot_jid(conn):
    user = jget(conn, 'user')
    return or_default(jget(user, 'jid'), or_default(jget(user, 'id'), ''))


def pipe_emit(target, forward, prefix=''):
    old = jget(target, 'emit')

    def emit(ev, *args):
        try:
            if callable(old):
                old(ev, *args)
            forward(prefix + ev, *args)
        except Exception as err:
            log_err('[pipeEmit]', exc_message(err))

    set_attr(target, 'emit', emit)

    def unpipe():
        if callable(old):
            set_attr(target, 'emit', old)
        else:
            del_attr(target, 'emit')
    return unpipe


def set_attr(obj, name, value):
    if isinstance(obj, dict):
        obj[name] = value
    else:
        setattr(obj, name, value)


def del_attr(obj, name):
    if isinstance(obj, dict):
        obj.pop(name, None)
    elif hasattr(obj, name):
        delattr(obj, name)


def clean_nonstr_key(value):
    return js_str(value)


def ws_accept_key(client_key):
    return base64.b64encode(hashlib.sha1((client_key + WS_MAGIC).encode('utf-8')).digest()).decode('ascii')


def ws_encode_frame(opcode, payload):
    length = len(payload)
    if length < 126:
        header = struct.pack('!BB', 0x80 | opcode, length)
    elif length < 65536:
        header = struct.pack('!BBH', 0x80 | opcode, 126, length)
    else:
        header = struct.pack('!BBQ', 0x80 | opcode, 127, length)
    return header + payload


def ws_close_payload(code, reason=''):
    return struct.pack('!H', code) + reason.encode('utf-8')[:123]


def ws_valid_close_code(code):
    return (1000 <= code <= 1003) or (1007 <= code <= 1014) or (3000 <= code <= 4999)


def ws_valid_utf8(data):
    try:
        data.decode('utf-8')
        return True
    except UnicodeDecodeError:
        return False


def ws_unmask(payload, mask):
    length = len(payload)
    if length == 0:
        return b''
    key = (mask * (length // 4 + 1))[:length]
    return (int.from_bytes(payload, 'big') ^ int.from_bytes(key, 'big')).to_bytes(length, 'big')


class WebSocketConnection:

    def __init__(self, sock, rfile, max_payload=WS_MAX_PAYLOAD):
        self.socket = sock
        self.rfile = rfile
        self.maxPayload = max_payload
        self.readyState = WS_OPEN
        self.isAlive = True
        self.OPEN = WS_OPEN
        self.CLOSED = WS_CLOSED
        self._fragments = []
        self._fragment_bytes = 0
        self._fragment_opcode = 0
        self._close_timer = None
        self._write_lock = threading.Lock()
        self._listeners = {}

    def on(self, event, listener):
        self._listeners.setdefault(event, []).append((listener, False))
        return self

    def once(self, event, listener):
        self._listeners.setdefault(event, []).append((listener, True))
        return self

    def listener_count(self, event):
        return len(self._listeners.get(event, []))

    def emit(self, event, *args):
        entries = list(self._listeners.get(event, []))
        if any(once for _, once in entries):
            self._listeners[event] = [entry for entry in self._listeners.get(event, []) if not entry[1]]
        for listener, _ in entries:
            try:
                listener(*args)
            except Exception as err:
                log_err('[WS listener]', exc_message(err))

    def _write_raw(self, data):
        if self.readyState == WS_CLOSED:
            return
        try:
            with self._write_lock:
                self.socket.sendall(data)
        except OSError:
            pass

    def send(self, data):
        if self.readyState != WS_OPEN:
            return
        if isinstance(data, (bytes, bytearray)):
            self._write_raw(ws_encode_frame(0x2, bytes(data)))
        else:
            self._write_raw(ws_encode_frame(0x1, js_str(data).encode('utf-8')))

    def ping(self):
        if self.readyState != WS_OPEN:
            return
        self._write_raw(ws_encode_frame(0x9, b''))

    def _begin_close(self, code, reason):
        if self.readyState == WS_CLOSED:
            return
        self.readyState = WS_CLOSING
        self._write_raw(ws_encode_frame(0x8, ws_close_payload(code, reason)))
        try:
            self.socket.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        self._close_timer = threading.Timer(WS_CLOSE_TIMEOUT_S, self.terminate)
        self._close_timer.daemon = True
        self._close_timer.start()

    def _fail(self, code, reason):
        self._fragments = []
        self._fragment_bytes = 0
        self._fragment_opcode = 0
        self._begin_close(code, reason)

    def close(self, code=1000, reason=''):
        self._begin_close(code, reason)

    def terminate(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass

    def _on_socket_close(self):
        if self._close_timer is not None:
            self._close_timer.cancel()
            self._close_timer = None
        self._fragments = []
        self._fragment_bytes = 0
        if self.readyState == WS_CLOSED:
            return
        self.readyState = WS_CLOSED
        self.emit('close')

    def _read_exact(self, count):
        data = self.rfile.read(count) if count else b''
        if data is None or len(data) < count:
            return None
        return data

    def _read_frame(self):
        head = self._read_exact(2)
        if head is None:
            return None
        b0, b1 = head[0], head[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        payload_len = b1 & 0x7F
        if b0 & 0x70:
            return ('error', 1002, 'RSV bits set')
        if not masked:
            return ('error', 1002, 'unmasked client frame')
        if (opcode > 0x2 and opcode < 0x8) or opcode > 0xA:
            return ('error', 1002, 'unknown opcode')
        is_control = opcode >= 0x8
        if is_control and (not fin or payload_len > 125):
            return ('error', 1002, 'invalid control frame')
        if payload_len == 126:
            ext = self._read_exact(2)
            if ext is None:
                return None
            payload_len = struct.unpack('!H', ext)[0]
        elif payload_len == 127:
            ext = self._read_exact(8)
            if ext is None:
                return None
            big = struct.unpack('!Q', ext)[0]
            if big > self.maxPayload:
                return ('error', 1009, 'message too big')
            payload_len = big
        if payload_len > self.maxPayload:
            return ('error', 1009, 'message too big')
        mask = self._read_exact(4)
        if mask is None:
            return None
        raw = self._read_exact(payload_len)
        if raw is None:
            return None
        return ('frame', fin, opcode, ws_unmask(raw, mask))

    def _handle_frame(self, fin, opcode, payload):
        if opcode in (0x0, 0x1, 0x2):
            is_cont = opcode == 0x0
            if is_cont and self._fragment_opcode == 0:
                return self._fail(1002, 'unexpected continuation')
            if not is_cont and self._fragment_opcode != 0:
                return self._fail(1002, 'expected continuation')
            if not is_cont:
                self._fragment_opcode = opcode
            self._fragment_bytes += len(payload)
            if self._fragment_bytes > self.maxPayload:
                return self._fail(1009, 'message too big')
            self._fragments.append(payload)
            if fin:
                full = b''.join(self._fragments)
                message_opcode = self._fragment_opcode
                self._fragments = []
                self._fragment_bytes = 0
                self._fragment_opcode = 0
                if message_opcode == 0x1 and not ws_valid_utf8(full):
                    return self._fail(1007, 'invalid utf-8')
                self.emit('message', full, message_opcode == 0x2)
            return None
        if opcode == 0x8:
            code = 1005
            if len(payload) == 1:
                return self._fail(1002, 'bad close payload')
            if len(payload) >= 2:
                code = struct.unpack('!H', payload[:2])[0]
                if not ws_valid_close_code(code):
                    return self._fail(1002, 'bad close code')
                if not ws_valid_utf8(payload[2:]):
                    return self._fail(1007, 'invalid utf-8 in close reason')
            return self._begin_close(1000 if code == 1005 else code, '')
        if opcode == 0x9:
            self._write_raw(ws_encode_frame(0xA, payload))
            return None
        if opcode == 0xA:
            self.isAlive = True
        return None

    def run(self):
        try:
            while self.readyState != WS_CLOSED:
                frame = self._read_frame()
                if frame is None:
                    break
                if frame[0] == 'error':
                    self._fail(frame[1], frame[2])
                    break
                _, fin, opcode, payload = frame
                self._handle_frame(fin, opcode, payload)
        except OSError as err:
            if self.listener_count('error') > 0:
                self.emit('error', err)
        finally:
            self._on_socket_close()
            self.terminate()


class WebSocketServer:

    def __init__(self, max_payload=WS_MAX_PAYLOAD):
        self.clients = set()
        self.maxPayload = max_payload
        self._lock = threading.Lock()
        self._listeners = {}

    def on(self, event, listener):
        self._listeners.setdefault(event, []).append(listener)
        return self

    def emit(self, event, *args):
        for listener in list(self._listeners.get(event, [])):
            listener(*args)

    def snapshot(self):
        with self._lock:
            return list(self.clients)

    def add(self, ws):
        with self._lock:
            self.clients.add(ws)

    def remove(self, ws):
        with self._lock:
            self.clients.discard(ws)

    def close(self):
        for ws in self.snapshot():
            ws.close(1001, 'server shutting down')


def ws_reject(handler, status, text):
    try:
        handler.wfile.write(f'HTTP/1.1 {status} {text}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'.encode('latin-1'))
        handler.wfile.flush()
    except OSError:
        pass
    handler.close_connection = True


def ws_upgrade(handler, req, wss):
    upgrade = (handler.headers.get('Upgrade') or '').lower()
    if upgrade != 'websocket':
        return ws_reject(handler, 400, 'Bad Request')
    if handler.command != 'GET':
        return ws_reject(handler, 405, 'Method Not Allowed')
    if handler.headers.get('Sec-WebSocket-Version') != '13':
        try:
            handler.wfile.write(b'HTTP/1.1 426 Upgrade Required\r\nSec-WebSocket-Version: 13\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            handler.wfile.flush()
        except OSError:
            pass
        handler.close_connection = True
        return None
    client_key = handler.headers.get('Sec-WebSocket-Key')
    if not client_key:
        return ws_reject(handler, 400, 'Bad Request')
    try:
        key_bytes = base64.b64decode(client_key, validate=False)
    except (ValueError, base64.binascii.Error):
        return ws_reject(handler, 400, 'Bad Request')
    if len(key_bytes) != 16:
        return ws_reject(handler, 400, 'Bad Request')

    accept_key = ws_accept_key(client_key)
    response = '\r\n'.join(['HTTP/1.1 101 Switching Protocols', 'Upgrade: websocket', 'Connection: Upgrade', f'Sec-WebSocket-Accept: {accept_key}', '', ''])
    try:
        handler.wfile.write(response.encode('latin-1'))
        handler.wfile.flush()
    except OSError:
        handler.close_connection = True
        return None

    sock = handler.connection
    sock.settimeout(None)
    ws = WebSocketConnection(sock, handler.rfile, wss.maxPayload)
    wss.add(ws)
    ws.once('close', lambda: wss.remove(ws))
    wss.emit('connection', ws, req)
    ws.run()
    handler.close_connection = True
    return None


def forward_all_clients(event_name, *args):
    payload = dumps({'event': event_name, 'args': list(args)})
    wss = STATE['wss']
    if wss is None:
        return
    for ws in wss.snapshot():
        if ws.readyState == WS_OPEN:
            ws.send(payload)


def forward_one_client(ws):
    def forward(event_name, *args):
        if ws.readyState == WS_OPEN:
            ws.send(dumps({'event': event_name, 'args': list(args)}))
    return forward


async def ws_message(conn, ws, raw):
    try:
        msg = json.loads(raw.decode('utf-8', 'replace'))
    except ValueError:
        return
    if not isinstance(msg, dict):
        return
    msg_type = msg.get('type')
    if msg_type == 'ping':
        ws.send(dumps({'type': 'pong'}))
        return
    if msg_type == 'subscribe' and isinstance(msg.get('channel'), str):
        subscribe_ws(ws, msg['channel'])
        return
    if msg_type == 'aiRichAction':
        try:
            token = msg.get('token')
            args = {key: value for key, value in msg.items() if key not in ('type', 'token', 'requestId')}
            result = await perform_html_action(conn, token, args)
            payload = {'type': 'aiRichActionResult'}
            if 'requestId' in msg:
                payload['requestId'] = msg['requestId']
            payload.update(result)
            ws.send(dumps(payload))
        except Exception as err:
            payload = {'type': 'aiRichActionResult'}
            if 'requestId' in msg:
                payload['requestId'] = msg['requestId']
            payload.update({'success': False, 'message': exc_message(err)})
            ws.send(dumps(payload))


def on_ws_connection(conn, ws, req):
    ws.on('error', lambda err: log_err('• [ WS ] Error:', exc_message(err)))

    def on_message(raw, is_binary):
        spawn(ws_message(conn, ws, raw))
    ws.on('message', on_message)
    ws.once('close', lambda: unsubscribe_ws_from_all(ws))
    unpipe = pipe_emit(conn, forward_one_client(ws), 'conn-')
    ws.once('close', unpipe)


class Res:

    def __init__(self, handler):
        self.handler = handler
        self.status = 200
        self.headers = []
        self.body = b''
        self.headers_sent = False
        self.ended = False
        self.streamed = False

    def set_header(self, name, value):
        lname = name.lower()
        self.headers = [(key, val) for key, val in self.headers if key.lower() != lname]
        self.headers.append((name, value))

    def append_header(self, name, value):
        self.headers.append((name, value))

    def get_header(self, name):
        lname = name.lower()
        values = [val for key, val in self.headers if key.lower() == lname]
        if not values:
            return None
        if lname == 'set-cookie':
            return values
        return values[-1]

    def write_head(self, status, headers=None):
        if self.ended or self.streamed:
            return
        self.status = status
        for key, value in (headers or {}).items():
            self.set_header(key, value)
        self.headers_sent = True

    def end(self, body=b''):
        if self.ended or self.streamed:
            return
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.body = bytes(body)
        self.headers_sent = True
        self.ended = True

    def force(self, status, body_text):
        self.status = status
        self.headers = [('Content-Type', 'application/json; charset=utf-8')]
        self.body = body_text.encode('utf-8')
        self.headers_sent = True
        self.ended = True

    def begin_stream(self, status, headers):
        self.write_head(status, headers)
        names = {key.lower() for key, _ in self.headers}
        if 'content-length' not in names:
            self.handler.close_connection = True
            self.set_header('Connection', 'close')
        self.streamed = True
        self.ended = False
        head = render_head(self.status, self.headers)
        self.handler.wfile.write(head)
        self.handler.wfile.flush()

    def stream_write(self, chunk):
        self.handler.wfile.write(chunk)
        self.handler.wfile.flush()


def render_head(status, headers):
    lines = [f'HTTP/1.1 {status} {http.client.responses.get(status, "Unknown")}']
    for key, value in headers:
        lines.append(f'{key}: {value}')
    return ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1', 'replace')


def send_json(res, status, obj):
    if res.headers_sent:
        return
    res.write_head(status, {'Content-Type': 'application/json; charset=utf-8'})
    res.end(dumps(obj))


def set_cookie(res, name, value, max_age_ms=None, same_site=None, http_only=False):
    parts = [f'{name}={value}']
    if max_age_ms:
        parts.append(f'Max-Age={int(max_age_ms // 1000)}')
    if same_site:
        parts.append(f'SameSite={same_site}')
    if http_only:
        parts.append('HttpOnly')
    parts.append('Path=/')
    res.append_header('Set-Cookie', '; '.join(parts))


class Req:

    def __init__(self, handler):
        self.handler = handler
        self.method = handler.command
        self.url = handler.path
        self.headers = handler.headers
        self.client_ip = handler.client_address[0]
        self.body_read = False
        parts = urllib.parse.urlsplit(handler.path)
        self.pathname = normalize_pathname(parts.path or '/')
        self.query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)

    def header(self, name):
        values = self.headers.get_all(name)
        if not values:
            return None
        return ', '.join(values)

    def first_param(self, name):
        for key, value in self.query:
            if key == name:
                return value
        return None

    def has_body(self):
        length = self.headers.get('Content-Length')
        if length and length.isdigit() and int(length) > 0:
            return True
        return 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower()


class PayloadTooLarge(Exception):
    pass


def read_request_body(req, limit):
    rfile = req.handler.rfile
    req.body_read = True
    if 'chunked' in (req.headers.get('Transfer-Encoding') or '').lower():
        chunks = []
        total = 0
        while True:
            line = rfile.readline(65537)
            if not line:
                break
            size = int(line.split(b';')[0].strip() or b'0', 16)
            if size == 0:
                while True:
                    trailer = rfile.readline(65537)
                    if trailer in (b'\r\n', b'\n', b''):
                        break
                break
            total += size
            if total > limit:
                raise PayloadTooLarge('Payload too large')
            data = rfile.read(size)
            if data is None or len(data) < size:
                break
            chunks.append(data)
            rfile.read(2)
        return b''.join(chunks)
    try:
        length = int(req.headers.get('Content-Length') or 0)
    except ValueError:
        length = 0
    if length > limit:
        raise PayloadTooLarge('Payload too large')
    if length <= 0:
        return b''
    data = rfile.read(length)
    if data is None:
        return b''
    return data


def reject_json_constant(name):
    raise ValueError(f'Unexpected constant {name}')


def read_json_body(req, limit=20 * 1024 * 1024):
    raw = read_request_body(req, limit)
    if not raw:
        return {}
    return json.loads(raw.decode('utf-8', 'replace'), parse_constant=reject_json_constant)


def parse_multipart(buffer, boundary):
    parts = []
    boundary_bytes = ('--' + boundary).encode('utf-8')
    start = buffer.find(boundary_bytes)
    while start != -1:
        nxt = buffer.find(boundary_bytes, start + len(boundary_bytes))
        if nxt == -1:
            break
        part = buffer[start + len(boundary_bytes):nxt]
        if part[:2] == b'\r\n':
            part = part[2:]
        if part[-2:] == b'\r\n':
            part = part[:-2]
        header_end = part.find(b'\r\n\r\n')
        if header_end != -1:
            header_str = part[:header_end].decode('utf-8', 'replace')
            body = part[header_end + 4:]
            name_match = re.search(r'name="([^"]*)"', header_str, re.I)
            filename_match = re.search(r'filename="([^"]*)"', header_str, re.I)
            type_match = re.search(r'Content-Type:\s*([^\r\n]+)', header_str, re.I)
            parts.append({
                'name': name_match.group(1) if name_match else None,
                'filename': filename_match.group(1) if filename_match else None,
                'contentType': type_match.group(1).strip() if type_match else None,
                'data': body,
            })
        start = nxt
    return parts


def ext_from_part(part):
    if truthy(part.get('filename')):
        ext = js_extname(part['filename']).replace('.', '').lower()
        if ext and re.fullmatch(r'[a-z0-9]{1,8}', ext):
            return ext
    ct_ext = {
        'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif',
        'video/mp4': 'mp4', 'video/webm': 'webm', 'video/quicktime': 'mov',
        'audio/mpeg': 'mp3', 'audio/mp4': 'm4a', 'audio/ogg': 'ogg', 'audio/wav': 'wav',
        'application/pdf': 'pdf', 'application/zip': 'zip',
    }
    if part.get('contentType'):
        key = part['contentType'].split(';')[0].strip()
        if key in ct_ext:
            return ct_ext[key]
    return 'bin'


def random_upload_name(ext):
    chars = 'abcdefghijklmnopqrstuvwxyz0123456789'
    name = ''.join(secrets.choice(chars) for _ in range(7))
    return f'{name}.{ext}' if ext else name


def ensure_upload_dir():
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def cleanup_old_uploads_sync():
    try:
        ensure_upload_dir()
        now = time.time() * 1000
        for name in os.listdir(UPLOAD_DIR):
            file_path = os.path.join(UPLOAD_DIR, name)
            try:
                st = os.stat(file_path)
                if now - st.st_mtime * 1000 > UPLOAD_MAX_AGE_MS:
                    os.unlink(file_path)
                    print(chalk.gray(f'[UPLOAD] Expired file removed: {name}'))
            except OSError:
                pass
    except Exception as err:
        log_err('[UPLOAD CLEANUP]', exc_message(err))


def build_upload_url(req, filename):
    configured = (os.environ.get('HOSTNAME_PUBLIC') or os.environ.get('CLOUDFLARE_TUNNEL_HOSTNAME') or '')
    configured = re.sub(r'^https?://', '', configured)
    configured = re.sub(r'/$', '', configured)
    if configured:
        return f'https://{configured}/files/{filename}'
    host = req.header('x-forwarded-host') or req.header('host')
    proto = req.header('x-forwarded-proto') or ('https' if truthy(host) and 'trycloudflare.com' in host else 'http')
    host_text = host if host is not None else 'undefined'
    return f'{proto}://{host_text}/files/{filename}'


def serve_uploaded_file(res, raw_name):
    try:
        name = js_basename(decode_uri_component(raw_name))
    except ValueError:
        name = js_basename(raw_name)
    if not name or '..' in name:
        return False
    file_path = os.path.join(UPLOAD_DIR, name)
    if not file_path.startswith(UPLOAD_DIR) or not os.path.exists(file_path):
        return False
    try:
        with open(file_path, 'rb') as handle:
            content = handle.read()
    except OSError:
        return False
    ext = os.path.splitext(file_path)[1].lower()
    res.write_head(200, {
        'Content-Type': UPLOAD_MIME_TYPES.get(ext, 'application/octet-stream'),
        'Content-Length': str(len(content)),
        'Cache-Control': 'public, max-age=604800',
    })
    res.end(content)
    return True


def mime_for(file_path):
    return MIME_TYPES.get(os.path.splitext(file_path)[1].lower(), 'application/octet-stream')


def serve_static(res, url_path):
    req_path = '/index.html' if url_path == '/' else url_path
    candidates = []
    if js_extname(req_path):
        candidates.append(os.path.normpath(os.path.join(VIEWS_DIR, req_path.lstrip('/'))))
    else:
        candidates.append(os.path.normpath(os.path.join(VIEWS_DIR, (req_path + '.html').lstrip('/'))))
        candidates.append(os.path.normpath(os.path.join(VIEWS_DIR, req_path.lstrip('/'), 'index.html')))

    for file_path in candidates:
        if not file_path.startswith(VIEWS_DIR):
            continue
        if os.path.exists(file_path):
            try:
                with open(file_path, 'rb') as handle:
                    content = handle.read()
                if file_path.endswith('.html'):
                    css_path = os.path.join(VIEWS_DIR, 'style.css')
                    try:
                        css_mtime = os.stat(css_path).st_mtime
                    except OSError:
                        css_mtime = None
                    if css_mtime is not None:
                        version = int(css_mtime * 1000)
                        text = content.decode('utf-8', 'replace')
                        text = re.sub(r'href="/style\.css(?:\?[^"]*)?"', lambda _m: f'href="/style.css?v={version}"', text)
                        content = text.encode('utf-8')
                res.write_head(200, {'Content-Type': mime_for(file_path)})
                res.end(content)
                return True
            except Exception:
                continue
    return False


def is_ip_literal(text):
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def ip_version(text):
    try:
        return ipaddress.ip_address(text).version
    except ValueError:
        return 0


def is_private_ip(ip):
    version = ip_version(ip)
    if version == 4:
        first, second = (int(part) for part in ip.split('.')[:2])
        if first in (10, 127, 0):
            return True
        if first == 169 and second == 254:
            return True
        if first == 172 and 16 <= second <= 31:
            return True
        if first == 192 and second == 168:
            return True
        if first == 100 and 64 <= second <= 127:
            return True
        return False
    if version == 6:
        lower = ip.lower()
        if lower == '::1':
            return True
        if lower.startswith(('fe8', 'fe9', 'fea', 'feb')):
            return True
        if lower.startswith(('fc', 'fd')):
            return True
        if lower.startswith('::ffff:'):
            mapped = lower.split(':')[-1]
            if ip_version(mapped) == 4:
                return is_private_ip(mapped)
        return False
    return True


def is_safe_proxy_target(hostname):
    if is_ip_literal(hostname):
        return not is_private_ip(hostname)
    now = now_ms()
    with _safe_host_lock:
        cached = _safe_host_cache.get(hostname)
    if cached and now < cached[1]:
        return cached[0]
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    addresses = [info[4][0] for info in infos]
    safe = len(addresses) > 0 and all(not is_private_ip(address) for address in addresses)
    with _safe_host_lock:
        _safe_host_cache[hostname] = (safe, now + SAFE_HOST_TTL_MS)
    return safe


def parse_proxy_target(text):
    match = re.match(r'^([A-Za-z][A-Za-z0-9+.\-]*):', text)
    if not match:
        return None
    scheme = match.group(1).lower() + ':'
    try:
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if scheme in ('http:', 'https:') and not host:
        return None
    host_text = host or ''
    if ':' in host_text:
        host_text = f'[{host_text}]'
    return {'scheme': scheme, 'hostname': host_text, 'port': port, 'url': text}


def referer_for(target):
    host = target['hostname']
    if host.endswith('dzcdn.net'):
        return 'https://www.deezer.com/'
    port = target['port']
    default_port = (target['scheme'] == 'http:' and port == 80) or (target['scheme'] == 'https:' and port == 443)
    host_port = f'{host}:{port}' if port and not default_port else host
    return f"{target['scheme']}//{host_port}/"


def guess_mime(url, upstream_type):
    text = (upstream_type or '').split(';')[0].strip().lower()
    if text and text not in ('application/octet-stream', 'binary/octet-stream', 'text/plain'):
        return text
    path_part = url.lower().split('?')[0]
    if re.search(r'\.mp3$', path_part):
        return 'audio/mpeg'
    if re.search(r'\.(m4a|mp4a)$', path_part):
        return 'audio/mp4'
    if re.search(r'\.ogg$', path_part):
        return 'audio/ogg'
    if re.search(r'\.webm$', path_part):
        return 'video/webm'
    if re.search(r'\.mp4$', path_part):
        return 'video/mp4'
    if re.search(r'\.png$', path_part):
        return 'image/png'
    if re.search(r'\.gif$', path_part):
        return 'image/gif'
    if re.search(r'\.webp$', path_part):
        return 'image/webp'
    if re.search(r'\.jpe?g$', path_part):
        return 'image/jpeg'
    if re.search(r'[?&]f=mp3\b', url.lower()):
        return 'audio/mpeg'
    return text or 'application/octet-stream'


def proxy_cache_get(key):
    with _proxy_cache_lock:
        hit = _proxy_cache.get(key)
        if hit is None:
            return None
        if now_ms() > hit['expiresAt']:
            del _proxy_cache[key]
            return None
        _proxy_cache.move_to_end(key)
        return hit


def proxy_cache_set(key, body, content_type):
    with _proxy_cache_lock:
        _proxy_cache[key] = {'body': body, 'contentType': content_type, 'expiresAt': now_ms() + PROXY_CACHE_TTL_MS}
        while len(_proxy_cache) > PROXY_CACHE_MAX_ITEMS:
            _proxy_cache.popitem(last=False)


class ProxyRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 5


_proxy_opener = urllib.request.build_opener(ProxyRedirectHandler)


def proxy_fetch(url, headers):
    request = urllib.request.Request(url, headers=headers, method='GET')
    try:
        return _proxy_opener.open(request, timeout=REQUEST_TIMEOUT_S)
    except urllib.error.HTTPError as err:
        return err


def close_quietly(response):
    try:
        response.close()
    except Exception:
        pass


def h_proxy(req, res):
    cors = {'Access-Control-Allow-Origin': '*', 'Cross-Origin-Resource-Policy': 'cross-origin'}

    def fail(code, message):
        if res.headers_sent:
            return
        res.write_head(code, {'Content-Type': 'application/json; charset=utf-8', **cors})
        res.end(dumps({'success': False, 'message': message}))

    try:
        target_text = req.first_param('url')
        if not truthy(target_text):
            return fail(400, 'Missing url')
        target = parse_proxy_target(target_text)
        if target is None:
            return fail(400, 'Invalid url')
        if target['scheme'] not in ('http:', 'https:'):
            return fail(400, 'Invalid protocol')

        if not is_safe_proxy_target(target['hostname']):
            return fail(403, 'Host not allowed')

        range_header = req.header('range')
        cache_key = target['url']

        if not range_header:
            cached = proxy_cache_get(cache_key)
            if cached is not None:
                res.write_head(200, {
                    'Content-Type': cached['contentType'],
                    'Content-Length': str(len(cached['body'])),
                    'Cache-Control': 'public, max-age=3600',
                    **cors,
                })
                return res.end(cached['body'])

        ref_override = req.first_param('ref')
        forward_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Referer': ref_override if truthy(ref_override) else referer_for(target),
        }
        if range_header:
            forward_headers['Range'] = range_header

        try:
            upstream = proxy_fetch(target['url'], forward_headers)
            if upstream.getcode() in (401, 403, 400):
                close_quietly(upstream)
                no_ref = dict(forward_headers)
                no_ref.pop('Referer', None)
                upstream = proxy_fetch(target['url'], no_ref)
                if upstream.getcode() in (401, 403, 400):
                    close_quietly(upstream)
                    bare = {}
                    if range_header:
                        bare['Range'] = range_header
                    upstream = proxy_fetch(target['url'], bare)
        except Exception as err:
            log_err('[proxy]', target['hostname'], exc_message(err))
            return fail(502, 'Upstream fetch failed')

        status = upstream.getcode()
        if status >= 400:
            log_err('[proxy]', target['hostname'], 'upstream status', status)
            close_quietly(upstream)
            return fail(502, f'Upstream {status}')

        content_type = guess_mime(cache_key, upstream.headers.get('Content-Type'))
        is_media = re.match(r'^(audio|video)/', content_type) is not None
        try:
            upstream_len = int(js_number(upstream.headers.get('Content-Length') or 0))
        except (TypeError, ValueError, OverflowError):
            upstream_len = 0

        if re.match(r'^(text/html|application/json)', content_type) and re.search(r'[?&]f=mp3\b|\.mp3|\.mp4|\.m4a', cache_key, re.I):
            close_quietly(upstream)
            log_err('[proxy]', target['hostname'], 'upstream returned', content_type, 'instead of media')
            return fail(502, 'Upstream returned non-media response')

        cacheable = (not range_header) and (not is_media) and status == 200 and 0 < upstream_len <= PROXY_CACHE_MAX_BYTES
        if cacheable:
            chunks = []
            try:
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except Exception:
                close_quietly(upstream)
                return fail(502, 'Upstream stream failed')
            close_quietly(upstream)
            body = b''.join(chunks)
            proxy_cache_set(cache_key, body, content_type)
            res.write_head(200, {
                'Content-Type': content_type,
                'Content-Length': str(len(body)),
                'Cache-Control': 'public, max-age=3600',
                **cors,
            })
            return res.end(body)

        headers = {
            'Content-Type': content_type,
            'Cache-Control': 'public, max-age=3600',
            **cors,
            'Access-Control-Expose-Headers': 'Content-Length, Content-Range, Accept-Ranges',
        }
        upstream_ranges = re.search(r'bytes', upstream.headers.get('Accept-Ranges') or '', re.I) is not None or status == 206
        if upstream_ranges:
            headers['Accept-Ranges'] = 'bytes'
        if upstream.headers.get('Content-Length'):
            headers['Content-Length'] = upstream.headers.get('Content-Length')
        if upstream.headers.get('Content-Range'):
            headers['Content-Range'] = upstream.headers.get('Content-Range')

        res.begin_stream(206 if status == 206 else 200, headers)
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                res.stream_write(chunk)
        except Exception:
            pass
        finally:
            close_quietly(upstream)
    except Exception as err:
        log_err('[proxy]', exc_message(err))
        fail(500, 'Proxy error')


TEST_WAV_RATE = 8000


def build_test_wav():
    rate = TEST_WAV_RATE
    count = rate
    buf = bytearray(44 + count * 2)
    buf[0:4] = b'RIFF'
    struct.pack_into('<I', buf, 4, 36 + count * 2)
    buf[8:12] = b'WAVE'
    buf[12:16] = b'fmt '
    struct.pack_into('<IHHIIHH', buf, 16, 16, 1, 1, rate, rate * 2, 2, 16)
    buf[36:40] = b'data'
    struct.pack_into('<I', buf, 40, count * 2)
    for index in range(count):
        sample = js_round(math.sin(2 * math.pi * 440 * index / rate) * 9000)
        struct.pack_into('<h', buf, 44 + index * 2, sample)
    return bytes(buf)


TEST_WAV = build_test_wav()


def h_audiotest(req, res):
    base = {
        'Content-Type': 'audio/wav',
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'no-store',
        'Access-Control-Allow-Origin': '*',
        'Cross-Origin-Resource-Policy': 'cross-origin',
    }
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', req.header('range') or '')
    total = len(TEST_WAV)
    if match:
        start = 0 if match.group(1) == '' else int(match.group(1))
        end = total - 1 if match.group(2) == '' else min(int(match.group(2)), total - 1)
        if start > end or start >= total:
            res.write_head(416, {**base, 'Content-Range': f'bytes */{total}', 'Content-Length': '0'})
            return res.end(b'')
        res.write_head(206, {**base, 'Content-Range': f'bytes {start}-{end}/{total}', 'Content-Length': str(end - start + 1)})
        return res.end(TEST_WAV[start:end + 1])
    res.write_head(200, {**base, 'Content-Length': str(total)})
    res.end(TEST_WAV)


def h_health(req, res):
    send_json(res, 200, {'ok': True, 'uptime': uptime()})


def h_upload(req, res):
    try:
        ensure_upload_dir()
        content_type = req.header('content-type') or ''
        boundary_match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type, re.I)
        if 'multipart/form-data' not in content_type.lower() or not boundary_match:
            return send_json(res, 400, {'success': False, 'message': 'Send a multipart/form-data request with a file field.'})
        boundary = boundary_match.group(1) or boundary_match.group(2)

        try:
            raw = read_request_body(req, UPLOAD_MAX_BODY_BYTES)
        except Exception as err:
            return send_json(res, 413, {'success': False, 'message': exc_message(err)})

        parts = parse_multipart(raw, boundary)
        file_part = None
        for part in parts:
            if truthy(part.get('filename')) and part.get('data') and len(part['data']) > 0:
                file_part = part
                break
        if file_part is None:
            return send_json(res, 400, {'success': False, 'message': 'No file found in the request body.'})

        ext = ext_from_part(file_part)
        name = random_upload_name(ext)
        with open(os.path.join(UPLOAD_DIR, name), 'wb') as handle:
            handle.write(file_part['data'])

        url = build_upload_url(req, name)
        send_json(res, 200, {'success': True, 'url': url, 'filename': name, 'size': len(file_part['data']), 'expiresIn': '7d'})
    except Exception as err:
        log_err('[UPLOAD]', exc_message(err))
        if not res.headers_sent:
            send_json(res, 500, {'success': False, 'message': 'Upload failed: ' + exc_message(err)})


def h_tier_asset(req, res):
    tier = tier_of()
    send_json(res, 200, {
        'name': tier['name'],
        'exp': tier['exp_required'],
        'limit': tier['limit_capacity'],
    })


async def h_bot_info(req, res, *, conn):
    bot_jid = conn_bot_jid(conn)
    bot_number = bot_jid.split('@')[0].split(':')[0] if bot_jid else ''
    send_json(res, 200, {
        'name': settings_of()['botname'],
        'icon': settings_of()['icon'],
        'number': bot_number,
        'uptime': uptime(),
        'totalUsers': len(users_of()),
    })


def h_command_list(req, res):
    try:
        from lib.utils import plugins as plugin_registry
        registry = plugin_registry.plugins
        values = list(registry.values()) if isinstance(registry, dict) else list(registry)
        helps = []
        for plugin in values:
            if jget(plugin, 'disabled') or not jget(plugin, 'help'):
                continue
            help_value = jget(plugin, 'help')
            if isinstance(help_value, list):
                helps.extend(help_value)
            else:
                helps.append(help_value)
        commands = []
        for entry in helps:
            if not isinstance(entry, str):
                raise TypeError('h.split is not a function')
            commands.append(entry.split(' ')[0])
        unique = []
        for command in commands:
            if command not in unique:
                unique.append(command)
        unique.sort()
        send_json(res, 200, {'total': len(unique), 'commands': unique})
    except Exception:
        send_json(res, 200, {'total': 0, 'commands': []})


def h_env_example(req, res):
    try:
        with open(os.path.join(ROOT_DIR, '.env.example'), 'r', encoding='utf-8') as handle:
            content = handle.read()
        send_json(res, 200, {'content': content})
    except Exception:
        send_json(res, 200, {'content': ''})


async def h_airich_action(req, res, *, conn, body=None):
    try:
        source = body if truthy(body) else {}
        token = source.get('token') if isinstance(source, dict) else None
        args = {key: value for key, value in source.items() if key != 'token'} if isinstance(source, dict) else {}
        if not truthy(token) or not isinstance(token, str):
            return send_json(res, 400, {'success': False, 'message': 'Missing token'})
        result = await perform_html_action(conn, token, args)
        send_json(res, 200 if result['success'] else 400, result)
    except Exception as err:
        log_err('[aiRich/action]', exc_message(err))
        if not res.headers_sent:
            send_json(res, 500, {'success': False, 'message': 'Internal error'})


async def h_register(req, res, *, conn, body):
    try:
        await ensure_db()
        phone = dget(body, 'phone')
        password = dget(body, 'password')
        if not truthy(phone) or not truthy(password):
            return send_json(res, 200, {'success': False, 'message': 'Incomplete data.'})

        normalized = normalize_phone(phone)
        auth_block = await check_db_rate_limit(f'auth:{normalized}')
        if auth_block:
            return send_json(res, 429, {'success': False, 'message': auth_block})

        length = js_strlen(password)
        if length is not None and length < 6:
            return send_json(res, 200, {'success': False, 'message': 'Password min 6 characters.'})

        users = ensure_users_dict()
        target_key = normalized + '@s.whatsapp.net'
        if truthy(get_path(users, target_key, 'password')):
            return send_json(res, 200, {'success': False, 'message': 'Number already registered.'})

        hashed_password = await hash_password(password)
        otp_result = await send_otp(conn, target_key, 'register')
        if not otp_result['ok']:
            return send_json(res, 200, {'success': False, 'message': otp_result['message']})

        _pending_otp[target_key]['registerData'] = {'hashedPassword': hashed_password, 'name': normalized}
        send_json(res, 200, {'success': True, 'message': 'Code sent to your WhatsApp.', 'pendingToken': otp_result['pendingToken'], 'phone': normalized})
    except Exception as err:
        log_err('[REGISTER]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_login(req, res, *, conn, body):
    try:
        await ensure_db()
        phone = dget(body, 'phone')
        password = dget(body, 'password')
        if not truthy(phone) or not truthy(password):
            return send_json(res, 200, {'success': False, 'message': 'Incomplete data.'})

        normalized = normalize_phone(phone)
        auth_block = await check_db_rate_limit(f'auth:{normalized}')
        if auth_block:
            return send_json(res, 429, {'success': False, 'message': auth_block})

        target_key = normalized + '@s.whatsapp.net'
        user = users_of().get(target_key)

        if user is None or not truthy(user.get('password')):
            return send_json(res, 200, {'success': False, 'message': 'Number not registered.'})
        password_ok = await verify_password(password, user['password'])
        if not password_ok:
            return send_json(res, 200, {'success': False, 'message': 'Incorrect password.'})

        stored = user['password']
        parts = stored.split(':') if isinstance(stored, str) else []
        if ':' not in stored or len(parts) < 2 or len(parts[1]) != 128:
            user['password'] = await hash_password(password)
            write_bg()

        otp_result = await send_otp(conn, target_key, 'login')
        if not otp_result['ok']:
            return send_json(res, 200, {'success': False, 'message': otp_result['message']})

        send_json(res, 200, {'success': True, 'message': 'Code sent to your WhatsApp.', 'pendingToken': otp_result['pendingToken'], 'phone': normalized})
    except Exception as err:
        log_err('[LOGIN]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_verify_otp(req, res, *, body):
    try:
        await ensure_db()
        phone = dget(body, 'phone')
        pending_token = dget(body, 'pendingToken')
        code = dget(body, 'code')
        if not truthy(phone) or not truthy(pending_token) or not truthy(code):
            return send_json(res, 200, {'success': False, 'message': 'Incomplete data.'})

        otp_block = await check_db_rate_limit(f'auth:{normalize_phone(phone)}', max_count=8)
        if otp_block:
            return send_json(res, 429, {'success': False, 'message': otp_block})

        target_key = normalize_phone(phone) + '@s.whatsapp.net'
        pending_rec = _pending_otp.get(target_key)
        register_data = pending_rec.get('registerData') if pending_rec is not None else None

        result = verify_otp(target_key, pending_token, code)
        if not result['ok']:
            return send_json(res, 200, {'success': False, 'message': result['message']})

        users = ensure_users_dict()
        if result['purpose'] == 'register':
            if register_data is None:
                return send_json(res, 200, {'success': False, 'message': 'Registration data expired. Please register again.'})
            if truthy(get_path(users, target_key, 'password')):
                return send_json(res, 200, {'success': False, 'message': 'Number already registered.'})
            users[target_key] = {
                'number': target_key, 'exp': 0, 'gems': 0,
                'limit': tier_limits()[0],
                'registered': True, 'name': register_data['name'], 'regTime': now_ms(),
                'password': register_data['hashedPassword'],
                'premium': False, 'premiumTime': 0, 'daily': 0, 'level': 0,
                'banned': False, 'warn': 0, 'role': 'user', 'autolevelup': False,
                'afk': -1, 'afkReason': '', 'sname': '', 'sauth': '', 'email': '', 'age': -1,
                'badge': {'premium': {}, 'developer': False},
            }
        else:
            if users.get(target_key) is None:
                return send_json(res, 200, {'success': False, 'message': 'User not found.'})

        sessions = ensure_sessions_dict()
        for stored_token, jid in list(sessions.items()):
            if jid == target_key:
                del sessions[stored_token]
        token = generate_token(target_key)
        sessions[token] = target_key
        sessions[ip_session_key(req)] = target_key
        await db.write()

        await clear_db_rate_limit(f'auth:{normalize_phone(phone)}')

        send_json(res, 200, {
            'success': True,
            'message': 'Registration successful.' if result['purpose'] == 'register' else 'Login successful.',
            'token': token,
        })
    except Exception as err:
        log_err('[VERIFY-OTP]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_resend_otp(req, res, *, conn, body):
    try:
        await ensure_db()
        phone = dget(body, 'phone')
        if not truthy(phone):
            return send_json(res, 200, {'success': False, 'message': 'Incomplete data.'})

        resend_block = await check_db_rate_limit(f'auth:{normalize_phone(phone)}', max_count=8)
        if resend_block:
            return send_json(res, 429, {'success': False, 'message': resend_block})

        target_key = normalize_phone(phone) + '@s.whatsapp.net'
        existing = _pending_otp.get(target_key)
        if existing is None:
            return send_json(res, 200, {'success': False, 'message': 'No pending verification for this number. Please start over.'})

        register_data = existing.get('registerData')
        purpose = existing['purpose']

        otp_result = await send_otp(conn, target_key, purpose)
        if not otp_result['ok']:
            return send_json(res, 200, {'success': False, 'message': otp_result['message']})
        if register_data is not None:
            _pending_otp[target_key]['registerData'] = register_data

        send_json(res, 200, {'success': True, 'message': 'Code resent.', 'pendingToken': otp_result['pendingToken']})
    except Exception as err:
        log_err('[RESEND-OTP]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def safe_profile_picture(conn, user_key):
    try:
        return await resolve(conn_fn(conn, 'profilePictureUrl')(user_key, 'image'))
    except Exception:
        return None


async def h_profile(req, res, *, conn):
    try:
        await ensure_db()
        user_key = get_user_key_from_req(req)
        if not truthy(user_key):
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized.'})

        user = users_of().get(user_key)
        if user is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found.'})

        level = nullish(user.get('level'), 0)
        max_tier_level = len(tier_names()) - 1

        profile_pic_ttl = 10 * 60 * 1000
        profile_pic = or_default(user.get('profilePicCache'), None)
        cache_age = now_ms() - (user.get('profilePicCacheTime') or 0)

        if profile_pic is None or cache_age > profile_pic_ttl:
            try:
                fresh = await with_timeout(safe_profile_picture(conn, user_key), 4000)
                if truthy(fresh):
                    profile_pic = fresh
                    user['profilePicCache'] = fresh
                    user['profilePicCacheTime'] = now_ms()
            except Exception:
                pass

        notification = user.get('notification')
        send_json(res, 200, {
            'success': True,
            'user': {
                'phone': or_default(user.get('number'), user_key),
                'name': or_default(user.get('name'), '-'),
                'profilePic': profile_pic,
                'joined': or_default(user.get('regTime'), None),
                'limit': nullish(user.get('limit'), 0),
                'exp': nullish(user.get('exp'), 0),
                'gems': nullish(user.get('gems'), 0),
                'level': level,
                'tier': or_default(idx(tier_names(), level), 'None'),
                'nextTier': idx(tier_names(), level + 1) if level < max_tier_level else None,
                'nextExp': idx(tier_exp(), level) if level < max_tier_level else None,
                'premium': or_default(user.get('premium'), False),
                'premiumTime': or_default(user.get('premiumTime'), 0),
                'premiumSince': or_default(user.get('premiumSince'), 0),
                'daily': or_default(user.get('daily'), 0),
                'number': or_default(user.get('number'), ''),
                'lid': or_default(user.get('lid'), ''),
                'spinCount': nullish(user.get('spinCount'), 0),
                'badge': or_default(user.get('badge'), {'premium': {}, 'developer': False}),
                'userKey': user_key,
                'notification': notification[-10:][::-1] if isinstance(notification, list) else [],
            },
        })
    except Exception as err:
        log_err('[PROFILE]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_daily(req, res):
    try:
        await ensure_db()
        user_key = get_user_key_from_token(get_token(req))
        if not truthy(user_key):
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized.'})

        user = users_of().get(user_key)
        if user is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found.'})

        now = now_ms()
        cooldown = 24 * 60 * 60 * 1000

        if truthy(user.get('daily')) and now - user['daily'] < cooldown:
            return send_json(res, 200, {'success': False, 'message': 'Not yet available.', 'remaining': user['daily'] + cooldown - now})

        daily = get_site_settings()['daily']
        exp_reward = daily['exp']
        gem_reward = daily['gems']

        user['exp'] = nullish(user.get('exp'), 0) + exp_reward
        user['gems'] = nullish(user.get('gems'), 0) + gem_reward
        user['daily'] = now
        write_bg()

        send_json(res, 200, {
            'success': True, 'exp': user['exp'], 'gems': user['gems'], 'daily': user['daily'],
            'expReward': exp_reward, 'gemReward': gem_reward,
            'message': f'Daily claimed! +{exp_reward} EXP, +{gem_reward} Gems',
        })
    except Exception as err:
        log_err('[DAILY]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_tierup(req, res):
    try:
        await ensure_db()
        user_key = get_user_key_from_token(get_token(req))
        if not truthy(user_key):
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized.'})

        user = users_of().get(user_key)
        if user is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found.'})

        level = nullish(user.get('level'), 0)
        max_level = len(tier_names()) - 1
        if level >= max_level:
            return send_json(res, 200, {'success': False, 'message': 'Already MAX TIER!'})

        needed = tier_exp()[level]
        if nullish(user.get('exp'), 0) < needed:
            return send_json(res, 200, {'success': False, 'message': 'Not enough EXP.', 'have': user.get('exp'), 'need': needed})

        old_tier = tier_names()[level]
        new_tier = tier_names()[level + 1]
        user['exp'] = nullish(user.get('exp'), 0) - needed
        user['level'] = level + 1
        user['limit'] = tier_limits()[level + 1]
        write_bg()

        send_json(res, 200, {'success': True, 'oldTier': old_tier, 'newTier': new_tier, 'exp': user['exp'], 'level': user['level'], 'limit': user['limit']})
    except Exception as err:
        log_err('[TIER UP]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_change_password(req, res, *, body):
    try:
        await ensure_db()
        token = get_token(req)

        change_block = check_memory_rate_limit(f"{get_client_ip(req)}:{token or 'anon'}", 10 * 60 * 1000, 5, 15 * 60 * 1000)
        if change_block:
            return send_json(res, 429, {'success': False, 'message': change_block})

        user_key = get_user_key_from_token(token)
        if not truthy(user_key):
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized.'})

        old_password = dget(body, 'oldPassword')
        new_password = dget(body, 'newPassword')
        if not truthy(old_password) or not truthy(new_password):
            return send_json(res, 200, {'success': False, 'message': 'Incomplete data.'})
        length = js_strlen(new_password)
        if length is not None and length < 6:
            return send_json(res, 200, {'success': False, 'message': 'Password min 6 characters.'})
        if old_password == new_password:
            return send_json(res, 200, {'success': False, 'message': 'New password must be different.'})

        user = users_of().get(user_key)
        if user is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found.'})
        old_ok = await verify_password(old_password, user.get('password'))
        if not old_ok:
            return send_json(res, 200, {'success': False, 'message': 'Current password is incorrect.'})

        user['password'] = await hash_password(new_password)
        write_bg()

        send_json(res, 200, {'success': True, 'message': 'Password updated successfully.'})
    except Exception as err:
        log_err('[change-password]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_rename(req, res, *, body):
    try:
        await ensure_db()
        user_key = get_user_key_from_token(get_token(req))
        if not truthy(user_key):
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized.'})

        new_name = dget(body, 'newName')
        if not truthy(new_name) or not require_str(new_name).strip():
            return send_json(res, 200, {'success': False, 'message': 'Name cannot be empty.'})
        trimmed = new_name.strip()
        if js_strlen(trimmed) > 32:
            return send_json(res, 200, {'success': False, 'message': 'Name too long (max 32 chars).'})

        user = users_of().get(user_key)
        if user is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found.'})

        user['name'] = trimmed
        write_bg()

        send_json(res, 200, {'success': True, 'message': 'Name updated!', 'name': user['name']})
    except Exception as err:
        log_err('[RENAME]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_redeem(req, res, *, conn, body):
    try:
        await ensure_db()
        user_key = get_user_key_from_token(get_token(req))
        if not truthy(user_key):
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized.'})

        user = users_of().get(user_key)
        if user is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found.'})

        code = dget(body, 'code')
        if not truthy(code) or not require_str(code).strip():
            return send_json(res, 200, {'success': False, 'message': 'Please enter a code.'})

        input_code = code.strip().upper()
        bot_jid = conn_bot_jid(conn)
        codes = get_path(db.data, 'settings', bot_jid, 'code')
        if not isinstance(codes, list) or len(codes) == 0:
            return send_json(res, 200, {'success': False, 'message': 'No gift codes available.'})

        entry = None
        for candidate in codes:
            if to_upper(candidate[0]) == input_code:
                entry = candidate
                break
        if entry is None:
            return send_json(res, 200, {'success': False, 'message': 'Invalid or expired code.'})

        if user.get('redeemedCodes') is None:
            user['redeemedCodes'] = []
        if input_code in user['redeemedCodes']:
            return send_json(res, 200, {'success': False, 'message': 'You have already redeemed this code.'})

        reward_str = entry[1]
        pieces = reward_str.split(':')
        reward_type = pieces[0] if len(pieces) > 0 else None
        reward_val = pieces[1] if len(pieces) > 1 else None
        reward_num = js_parse_float(reward_val)

        reward_message = ''
        if reward_type == 'exp':
            user['exp'] = nullish(user.get('exp'), 0) + reward_num
            reward_message = f'+{num_str(reward_num)} EXP'
        elif reward_type == 'gems':
            user['gems'] = nullish(user.get('gems'), 0) + reward_num
            reward_message = f'+{num_str(reward_num)} Gems'
        elif reward_type == 'limit':
            user['limit'] = nullish(user.get('limit'), 0) + reward_num
            reward_message = f'+{num_str(reward_num)} Limit'
        elif reward_type == 'premium':
            days = reward_num
            now = now_ms()
            ms_per_day = 86400000
            grace_ms = 86400000
            streak_broken = not truthy(user.get('premiumSince')) or (or_default(user.get('premiumTime'), 0) + grace_ms) < now
            if streak_broken:
                user['premiumSince'] = now
            if truthy(user.get('premium')) and user.get('premiumTime') is not None and user['premiumTime'] > now:
                user['premiumTime'] = user['premiumTime'] + days * ms_per_day
            else:
                user['premium'] = True
                user['premiumTime'] = now + days * ms_per_day
            if user.get('badge') is None:
                user['badge'] = {}
            user['badge']['premium'] = {'active': True, 'since': user['premiumSince']}
            user['premiumExpiryNotified'] = False
            user['premiumExpiredNotified'] = False
            user['premiumStreakLostNotified'] = False
            plural = '' if days == 1 else 's'
            reward_message = f'Premium {num_str(days)} day{plural}'
        elif reward_type == 'level':
            max_level = len(tier_names()) - 1
            new_level = min(max_level, nullish(user.get('level'), 0) + reward_num)
            user['level'] = new_level
            user['limit'] = idx(tier_limits(), new_level)
            level_name = idx(tier_names(), new_level)
            reward_message = f"Level → {'undefined' if level_name is None else level_name}"
        else:
            return send_json(res, 200, {'success': False, 'message': 'Unknown reward type.'})

        user['redeemedCodes'].append(input_code)
        write_bg()

        send_json(res, 200, {
            'success': True, 'message': f'Code redeemed! Reward: {reward_message}',
            'rewardType': reward_type, 'rewardNum': reward_num, 'rewardMessage': reward_message,
            'exp': user.get('exp'), 'limit': user.get('limit'), 'level': user.get('level'), 'gems': user.get('gems'),
        })
    except Exception as err:
        log_err('[REDEEM]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_buy_limit(req, res, *, body):
    try:
        await ensure_db()
        user_key = get_user_key_from_token(get_token(req))
        if not truthy(user_key):
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized.'})

        user = users_of().get(user_key)
        if user is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found.'})

        amount = js_parse_int(dget(body, 'amount'))
        if is_nan(amount) or amount <= 0:
            return send_json(res, 200, {'success': False, 'message': 'Invalid amount.'})

        current_level = nullish(user.get('level'), 0)
        max_limit = nullish(idx(tier_limits(), current_level), 10)
        remaining = max_limit - nullish(user.get('limit'), 0)

        if amount > remaining:
            return send_json(res, 200, {'success': False, 'message': f'Cannot buy more than {num_str(remaining)} limit(s). Max limit for your tier: {num_str(max_limit)}'})

        total_cost = amount * get_site_settings()['shop']['limitPrice']
        if nullish(user.get('gems'), 0) < total_cost:
            return send_json(res, 200, {'success': False, 'message': f'Not enough Gems! Need {num_str(total_cost)} Gems.'})

        user['gems'] = user['gems'] - total_cost
        user['limit'] = nullish(user.get('limit'), 0) + amount
        write_bg()

        send_json(res, 200, {'success': True, 'message': f'Bought {num_str(amount)} limit(s)!', 'gems': user['gems'], 'limit': user['limit']})
    except Exception as err:
        log_err('[BUY LIMIT]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_buy_premium(req, res, *, body):
    try:
        await ensure_db()
        user_key = get_user_key_from_token(get_token(req))
        if not truthy(user_key):
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized.'})

        user = users_of().get(user_key)
        if user is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found.'})

        weeks = js_parse_int(dget(body, 'weeks'))
        if is_nan(weeks) or weeks <= 0:
            return send_json(res, 200, {'success': False, 'message': 'Invalid amount.'})

        total_cost = weeks * get_site_settings()['shop']['premiumPrice']
        if nullish(user.get('gems'), 0) < total_cost:
            return send_json(res, 200, {'success': False, 'message': f'Not enough Gems! Need {num_str(total_cost)} Gems.'})

        user['gems'] = user['gems'] - total_cost

        now = now_ms()
        ms_per_week = 7 * 86400000
        grace_ms = 86400000

        streak_broken = not truthy(user.get('premiumSince')) or (or_default(user.get('premiumTime'), 0) + grace_ms) < now
        if streak_broken:
            user['premiumSince'] = now

        if truthy(user.get('premium')) and user.get('premiumTime') is not None and user['premiumTime'] > now:
            user['premiumTime'] = user['premiumTime'] + weeks * ms_per_week
        else:
            user['premium'] = True
            user['premiumTime'] = now + weeks * ms_per_week

        expiry_str = date_en_gb(user['premiumTime'])
        buy_message = f'Premium extended by {num_str(weeks)} week(s)! Now active until {expiry_str}.'
        push_notification(user, buy_message)

        if user.get('badge') is None:
            user['badge'] = {}
        user['badge']['premium'] = {'active': True, 'since': user['premiumSince']}

        user['premiumExpiryNotified'] = False
        user['premiumExpiredNotified'] = False
        user['premiumStreakLostNotified'] = False

        write_bg()

        send_json(res, 200, {
            'success': True, 'message': f'Premium extended by {num_str(weeks)} week(s)!',
            'gems': user['gems'], 'premium': user['premium'], 'premiumTime': user['premiumTime'], 'premiumSince': user['premiumSince'],
        })
    except Exception as err:
        log_err('[BUY PREMIUM]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


SLOT_ICONS = ['♣', '♦', '♠', '♥', '♪', '7', '×', '✦', '✿']


async def h_slot(req, res):
    try:
        await ensure_db()
        user_key = get_user_key_from_token(get_token(req))
        if not truthy(user_key):
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized.'})

        user = users_of().get(user_key)
        if user is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found.'})

        current_exp = nullish(user.get('exp'), 0)
        spin_cost = 200
        pity_limit = 100
        spin_cooldown = 500

        if current_exp < spin_cost:
            return send_json(res, 200, {'success': False, 'message': f'Not enough EXP! Need {spin_cost} EXP to spin.'})

        now = now_ms()
        if truthy(user.get('lastSpin')) and now - user['lastSpin'] < spin_cooldown:
            wait = math.ceil((spin_cooldown - (now - user['lastSpin'])) / 1000)
            return send_json(res, 200, {'success': False, 'message': f'Cooldown! Wait {wait}s before spinning again.'})
        user['lastSpin'] = now

        if not isinstance(user.get('spinCount'), (int, float)) or isinstance(user.get('spinCount'), bool):
            user['spinCount'] = 0

        gacha_symbols = get_site_settings()['gacha']['symbols']

        def pick_result():
            roll = random.random() * 100
            acc = 0
            for sym in gacha_symbols:
                acc += sym['chance']
                if roll < acc:
                    return sym['key']
            return None

        def visual_icon():
            return SLOT_ICONS[int(random.random() * len(SLOT_ICONS))]

        is_pity = user['spinCount'] >= pity_limit
        if is_pity:
            pity_pool = ['✦', '7', '♦']
            weights = [10, 40, 50]
            total = sum(weights)
            roll = random.random() * total
            chosen = '♦'
            for index in range(len(pity_pool)):
                roll -= weights[index]
                if roll <= 0:
                    chosen = pity_pool[index]
                    break
            slot1 = slot2 = slot3 = chosen
            is_triple = True
            triple_symbol = chosen
        else:
            triple_symbol = pick_result()
            if triple_symbol:
                slot1 = slot2 = slot3 = triple_symbol
                is_triple = True
            else:
                slot1 = visual_icon()
                slot2 = visual_icon()
                while slot2 == slot1:
                    slot2 = visual_icon()
                slot3 = visual_icon()
                while slot3 == slot1 or slot3 == slot2:
                    slot3 = visual_icon()
                is_triple = False

        slots = [slot1, slot2, slot3]

        reward = 0
        if is_triple:
            sym = next((item for item in gacha_symbols if item['key'] == triple_symbol), None)
            reward = sym['reward'] if sym is not None else 0
            win_message = f"{GACHA_TITLES.get(triple_symbol, 'TRIPLE!')} {fmt_exp_amount(reward)} EXP"
        else:
            win_message = f'No match! −{spin_cost} EXP'

        is_win = reward > 0
        exp_change = reward - spin_cost if is_triple else -spin_cost

        if is_win:
            user['spinCount'] = 0
        else:
            user['spinCount'] += 1

        user['exp'] = max(0, current_exp + exp_change)

        write_bg()

        send_json(res, 200, {
            'success': True, 'slots': slots, 'reward': reward, 'cost': spin_cost, 'expChange': exp_change,
            'newExp': user['exp'], 'winMessage': win_message, 'isWin': is_win,
            'isPity': is_pity, 'spinCount': user['spinCount'], 'pityLimit': pity_limit,
            'spinsUntilPity': max(0, pity_limit - user['spinCount']),
        })
    except Exception as err:
        log_err('[SLOT]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_gems(req, res):
    try:
        await ensure_db()
        user_key = get_user_key_from_token(get_token(req))
        if not truthy(user_key):
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized'})

        user = users_of().get(user_key)
        if user is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found'})

        send_json(res, 200, {'success': True, 'gems': or_default(user.get('gems'), 0)})
    except Exception as err:
        log_err('[GEMS]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


def subbot_module():
    return importlib.import_module('plugins.subbot.connect')


async def subbot_auth(req, res):
    await ensure_db()
    token = get_token(req)
    user_key = get_user_key_from_token(token)
    if not truthy(user_key):
        send_json(res, 200, {'success': False, 'message': 'Unauthorized.'})
        return None
    user = users_of().get(user_key)
    if user is None:
        send_json(res, 200, {'success': False, 'message': 'User not found.'})
        return None
    number = user.get('number')
    if user_key.endswith('@s.whatsapp.net'):
        jid = user_key
    elif isinstance(number, str) and number.endswith('@s.whatsapp.net'):
        jid = number
    else:
        jid = None
    if not jid:
        send_json(res, 200, {'success': False, 'message': 'Subbot is only available for WhatsApp number accounts.'})
        return None
    is_owner = check_owner(token)['isOwner']
    premium = bool(user.get('premium') and user.get('premiumTime') is not None and user['premiumTime'] > now_ms())
    can_use = get_site_settings()['subbot']['mode'] == 'free' or premium or is_owner
    return {'jid': jid, 'canUse': can_use}


def subbot_active_list(jids):
    names = tier_names()
    out = []
    for jid in jids:
        user = users_of().get(jid) or {}
        level = nullish(user.get('level'), 0)
        digits = jid.split('@')[0]
        out.append({
            'name': user.get('name') or None,
            'masked': f"+{digits[:2]}••••{digits[-4:]}",
            'tier': or_default(idx(names, level), 'None'),
            'badge': or_default(user.get('badge'), {'premium': {}, 'developer': False}),
            'premium': or_default(user.get('premium'), False),
            'premiumTime': or_default(user.get('premiumTime'), 0),
            'premiumSince': or_default(user.get('premiumSince'), 0),
            'profilePicCache': or_default(user.get('profilePicCache'), None),
        })
    return out


def subbot_payload(sb, auth):
    info = sb.web_subbot_info(auth['jid'])
    base = {'success': True, 'phone': auth['jid'].split('@')[0], 'canUse': auth['canUse']}
    merged = {**base, **info}
    merged['active'] = subbot_active_list(info.get('active') or [])
    return merged


async def h_subbot_status(req, res):
    try:
        auth = await subbot_auth(req, res)
        if auth is None:
            return
        sb = subbot_module()
        send_json(res, 200, subbot_payload(sb, auth))
    except Exception as err:
        log_err('[SUBBOT]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


def subbot_action(run):
    async def handler(req, res, *, body):
        try:
            auth = await subbot_auth(req, res)
            if auth is None:
                return
            sb = subbot_module()
            body = body if truthy(body) else {}
            await resolve(run(sb, auth, body))
            send_json(res, 200, subbot_payload(sb, auth))
        except Exception as err:
            message = exc_message(err)
            log_err('[SUBBOT]', message)
            send_json(res, 200, {'success': False, 'message': message or 'Internal error'})
    return handler


async def h_subbot_session(req, res):
    try:
        auth = await subbot_auth(req, res)
        if auth is None:
            return
        sb = subbot_module()
        file_info = sb.web_subbot_session_file(auth['jid'])
        if file_info is None:
            return send_json(res, 200, {'success': False, 'message': 'No session file available.'})
        file_path = jget(file_info, 'path')
        file_name = jget(file_info, 'name')
        with open(file_path, 'rb') as handle:
            data = handle.read()
        res.write_head(200, {
            'Content-Type': 'application/octet-stream',
            'Content-Length': str(len(data)),
            'Content-Disposition': f'attachment; filename="{file_name}"',
            'Cache-Control': 'no-store',
        })
        res.end(data)
    except Exception as err:
        log_err('[SUBBOT]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_logout(req, res):
    try:
        await ensure_db()
        token = get_token(req)
        sessions = sessions_of()
        if truthy(token) and sessions.get(token):
            del sessions[token]
        ip_key = ip_session_key(req)
        if sessions.get(ip_key):
            del sessions[ip_key]
        write_bg()
        send_json(res, 200, {'success': True})
    except Exception:
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_check_owner(req, res):
    try:
        await ensure_db()
        owner = check_owner(get_token(req))
        if owner['userKey'] is None:
            return send_json(res, 200, {'success': False, 'isOwner': False})
        send_json(res, 200, {'success': True, 'isOwner': owner['isOwner']})
    except Exception:
        send_json(res, 200, {'success': False, 'isOwner': False})


async def h_admin_giftcodes_get(req, res, *, conn):
    try:
        await ensure_db()
        owner = check_owner(get_token(req))
        if owner['userKey'] is None or not owner['isOwner']:
            return send_json(res, 200, {'success': False, 'message': 'Forbidden'})
        bot_jid = conn_bot_jid(conn)
        codes = get_path(db.data, 'settings', bot_jid, 'code') or []
        send_json(res, 200, {'success': True, 'codes': codes})
    except Exception as err:
        log_err('[ADMIN/GIFTCODES GET]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_admin_giftcodes_add(req, res, *, conn, body):
    try:
        await ensure_db()
        owner = check_owner(get_token(req))
        if owner['userKey'] is None or not owner['isOwner']:
            return send_json(res, 200, {'success': False, 'message': 'Forbidden'})

        code = dget(body, 'code')
        reward = dget(body, 'reward')
        if not truthy(code) or not truthy(reward):
            return send_json(res, 200, {'success': False, 'message': 'Invalid data'})

        bot_jid = conn_bot_jid(conn)
        if db.data.get('settings') is None:
            db.data['settings'] = {}
        if db.data['settings'].get(bot_jid) is None:
            db.data['settings'][bot_jid] = {}
        if not isinstance(db.data['settings'][bot_jid].get('code'), list):
            db.data['settings'][bot_jid]['code'] = []

        codes = db.data['settings'][bot_jid]['code']
        upper_code = require_str(code).strip().upper()
        for entry in codes:
            if to_upper(entry[0]) == upper_code:
                return send_json(res, 200, {'success': False, 'message': 'Code already exists.'})

        codes.append([upper_code, require_str(reward).strip()])
        write_bg()
        send_json(res, 200, {'success': True, 'codes': codes})
    except Exception as err:
        log_err('[ADMIN/GIFTCODES ADD]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_admin_giftcodes_delete(req, res, *, conn, body):
    try:
        await ensure_db()
        owner = check_owner(get_token(req))
        if owner['userKey'] is None or not owner['isOwner']:
            return send_json(res, 200, {'success': False, 'message': 'Forbidden'})

        code = dget(body, 'code')
        if not truthy(code):
            return send_json(res, 200, {'success': False, 'message': 'Invalid data'})

        bot_jid = conn_bot_jid(conn)
        codes = get_path(db.data, 'settings', bot_jid, 'code')
        if not isinstance(codes, list):
            return send_json(res, 200, {'success': False, 'message': 'No codes found.'})

        upper_code = require_str(code).strip().upper()
        found = None
        for index, entry in enumerate(codes):
            if to_upper(entry[0]) == upper_code:
                found = index
                break
        if found is None:
            return send_json(res, 200, {'success': False, 'message': 'Code not found.'})

        del codes[found]
        write_bg()
        send_json(res, 200, {'success': True, 'codes': codes})
    except Exception as err:
        log_err('[ADMIN/GIFTCODES DELETE]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_site_settings(req, res):
    try:
        await ensure_db()
        send_json(res, 200, {'success': True, 'settings': get_site_settings()})
    except Exception as err:
        log_err('[SITE SETTINGS]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_admin_settings_get(req, res):
    try:
        await ensure_db()
        owner = check_owner(get_token(req))
        if owner['userKey'] is None or not owner['isOwner']:
            return send_json(res, 200, {'success': False, 'message': 'Forbidden'})
        send_json(res, 200, {'success': True, 'settings': get_site_settings(), 'defaults': DEFAULT_SITE_SETTINGS})
    except Exception as err:
        log_err('[ADMIN/SETTINGS GET]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_admin_settings_post(req, res, *, body):
    try:
        await ensure_db()
        owner = check_owner(get_token(req))
        if owner['userKey'] is None or not owner['isOwner']:
            return send_json(res, 200, {'success': False, 'message': 'Forbidden'})

        result = build_site_settings(body)
        if 'error' in result:
            return send_json(res, 200, {'success': False, 'message': result['error']})

        db.data['siteSettings'] = result['value']
        write_bg()
        send_json(res, 200, {'success': True, 'settings': result['value']})
    except Exception as err:
        log_err('[ADMIN/SETTINGS POST]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_premium_notice(req, res):
    try:
        await ensure_db()
        user_key = get_user_key_from_req(req)
        if not truthy(user_key):
            return send_json(res, 200, {'success': True, 'notice': None})

        user = users_of().get(user_key)
        if user is None or not truthy(user.get('premiumTime')):
            return send_json(res, 200, {'success': True, 'notice': None})

        now = now_ms()
        day_ms = 86400000
        grace_ms = 86400000
        expires_at = user['premiumTime']

        notice = None
        if expires_at > now:
            ms_left = expires_at - now
            if ms_left <= day_ms:
                notice = {'type': 'expiring_tomorrow', 'expiresAt': expires_at, 'message': 'Your premium will expire tomorrow. Wanna keep your streak?'}
            elif ms_left <= 3 * day_ms:
                date_text = date_en_gb(expires_at)
                notice = {'type': 'expiring_soon', 'expiresAt': expires_at, 'message': f'Your premium will expire at {date_text}.'}
        elif expires_at + grace_ms > now:
            hours_left = math.ceil((expires_at + grace_ms - now) / 3600000)
            notice = {'type': 'grace_period', 'expiresAt': expires_at, 'message': f'Your premium expired! Renew within {hours_left}h to keep your streak.'}
        elif truthy(user.get('premiumSince')):
            notice = {'type': 'streak_lost', 'expiresAt': expires_at, 'message': 'Your premium streak has been reset.'}

        send_json(res, 200, {'success': True, 'notice': notice})
    except Exception as err:
        log_err('[PREMIUM-NOTICE]', err)
        send_json(res, 200, {'success': True, 'notice': None})


async def h_user_search(req, res):
    try:
        await ensure_db()
        q = (req.first_param('q') or '').strip().lower()
        if not q or js_strlen(q) < 2:
            return send_json(res, 200, {'success': True, 'results': []})

        digits_only = re.sub(r'[^0-9]', '', q)
        names = tier_names()
        exp_required = tier_exp()
        max_tier_level = len(names) - 1

        matches = []
        for key, user in list(users_of().items()):
            if key.endswith('@g.us'):
                continue
            phone_digits = re.sub(r'[^0-9]', '', key)
            name_match = to_lower(or_default(user.get('name'), '')).find(q) != -1
            number_match = to_lower(or_default(user.get('number'), '')).find(q) != -1
            phone_match = len(digits_only) >= 2 and digits_only in phone_digits
            if name_match or number_match or phone_match:
                matches.append((key, user))
        results = []
        for key, user in matches[:10]:
            level = nullish(user.get('level'), 0)
            results.append({
                'userKey': key,
                'phone': or_default(user.get('number'), key.replace('@s.whatsapp.net', '')),
                'name': or_default(user.get('name'), '-'),
                'level': level,
                'tier': or_default(idx(names, level), 'None'),
                'badge': or_default(user.get('badge'), {'premium': {}, 'developer': False}),
                'premium': or_default(user.get('premium'), False),
                'premiumTime': or_default(user.get('premiumTime'), 0),
                'premiumSince': or_default(user.get('premiumSince'), 0),
                'nextExp': idx(exp_required, level) if level < max_tier_level else None,
                'profilePicCache': or_default(user.get('profilePicCache'), None),
            })

        send_json(res, 200, {'success': True, 'results': results})
    except Exception as err:
        log_err('[USER-SEARCH]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_admin_users(req, res):
    try:
        await ensure_db()
        owner = check_owner(get_token(req))
        if owner['userKey'] is None:
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized'})
        if not owner['isOwner']:
            return send_json(res, 200, {'success': False, 'message': 'Forbidden'})

        users = [{'userKey': key, **val} for key, val in users_of().items() if not key.endswith('@g.us')]
        send_json(res, 200, {'success': True, 'users': users})
    except Exception as err:
        log_err('[ADMIN/USERS]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_admin_update_user(req, res, *, body):
    try:
        await ensure_db()
        owner = check_owner(get_token(req))
        if owner['userKey'] is None:
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized'})
        if not owner['isOwner']:
            return send_json(res, 200, {'success': False, 'message': 'Forbidden'})

        target = dget(body, 'userKey')
        field = dget(body, 'field')
        value = dget(body, 'value')
        if not truthy(target) or not truthy(field):
            return send_json(res, 200, {'success': False, 'message': 'Invalid data'})
        target_key = js_str(target)
        users = users_of()
        if users.get(target_key) is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found'})

        user = users[target_key]
        field_key = js_str(field)

        if field in ['exp', 'limit', 'level', 'warn', 'age', 'gems']:
            parsed = js_parse_int(value)
            user[field_key] = 0 if is_nan(parsed) or parsed == 0 else parsed
        elif field in ['premium', 'banned', 'registered', 'autolevelup']:
            user[field_key] = value is True or value == 'true'
        elif field in ['premiumTime', 'regTime', 'daily', 'afk']:
            number = js_number(value)
            user[field_key] = 0 if is_nan(number) or number == 0 else number
        else:
            user[field_key] = value

        write_bg()
        send_json(res, 200, {'success': True, 'message': 'Updated', 'userKey': target_key, 'field': field, 'value': user.get(field_key)})
    except Exception as err:
        log_err('[ADMIN/UPDATE-USER]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_admin_delete_user(req, res, *, body):
    try:
        await ensure_db()
        owner = check_owner(get_token(req))
        if owner['userKey'] is None:
            return send_json(res, 200, {'success': False, 'message': 'Unauthorized'})
        if not owner['isOwner']:
            return send_json(res, 200, {'success': False, 'message': 'Forbidden'})

        target = dget(body, 'userKey')
        if not truthy(target):
            return send_json(res, 200, {'success': False, 'message': 'Invalid data'})
        target_key = js_str(target)
        users = users_of()
        if users.get(target_key) is None:
            return send_json(res, 200, {'success': False, 'message': 'User not found'})

        del users[target_key]
        write_bg()
        send_json(res, 200, {'success': True, 'message': 'User deleted'})
    except Exception as err:
        log_err('[admin/delete-user]', err)
        send_json(res, 200, {'success': False, 'message': 'Internal error'})


async def h_check_number(req, res, *, body):
    try:
        await ensure_db()
        phone = dget(body, 'phone')
        if not truthy(phone):
            return send_json(res, 200, {'exists': False})
        target_key = normalize_phone(phone) + '@s.whatsapp.net'
        send_json(res, 200, {'exists': users_of().get(target_key) is not None})
    except Exception as err:
        log_err('[check-number]', err)
        send_json(res, 200, {'exists': False})


PREMIUM_CHECK_INTERVAL_S = 15 * 60


async def premium_reminder_tick():
    try:
        await ensure_db()
        now = now_ms()
        changed = False
        for user_key, user in list(users_of().items()):
            if not isinstance(user, dict) or not truthy(user.get('premiumTime')):
                continue
            ms_left = user['premiumTime'] - now

            if truthy(user.get('premium')) and 0 < ms_left <= PREMIUM_WINDOW_MS and not truthy(user.get('premiumExpiryNotified')):
                expiry_str = date_en_gb(user['premiumTime'])
                msg = f'Your premium is about to expire ({expiry_str}). Renew now to keep your streak!'
                push_notification(user, msg)
                spawn(send_wa_notif(STATE['conn'], user_key, msg))
                user['premiumExpiryNotified'] = True
                changed = True
            elif ms_left > PREMIUM_WINDOW_MS and truthy(user.get('premiumExpiryNotified')):
                user['premiumExpiryNotified'] = False
                changed = True

            if truthy(user.get('premium')) and ms_left <= 0 and not truthy(user.get('premiumExpiredNotified')):
                push_notification(user, 'Your premium has expired. Renew within 24h to keep your streak!')
                user['premiumExpiredNotified'] = True
                user['premium'] = False
                changed = True
            elif ms_left > 0 and truthy(user.get('premiumExpiredNotified')):
                user['premiumExpiredNotified'] = False
                changed = True

            if ms_left <= -PREMIUM_GRACE_MS and truthy(user.get('premiumSince')) and not truthy(user.get('premiumStreakLostNotified')):
                push_notification(user, 'Your premium streak has been reset.')
                if user.get('badge') is None:
                    user['badge'] = {}
                user['badge']['premium'] = {}
                user['premiumSince'] = 0
                user['premiumStreakLostNotified'] = True
                changed = True
            elif ms_left > -PREMIUM_GRACE_MS and truthy(user.get('premiumStreakLostNotified')):
                user['premiumStreakLostNotified'] = False
                changed = True
        if changed:
            await db.write()
    except Exception as err:
        log_err('[PREMIUM REMINDER]', err)


def build_routes():
    subbot_connect = subbot_action(lambda sb, auth, body: sb.web_subbot_connect(auth['jid'], jget(body, 'method'), auth['canUse']))
    subbot_cancel = subbot_action(lambda sb, auth, body: sb.web_subbot_abort(auth['jid']))
    subbot_start = subbot_action(lambda sb, auth, body: sb.web_subbot_start(auth['jid'], auth['canUse']))
    subbot_stop = subbot_action(lambda sb, auth, body: sb.web_subbot_stop(auth['jid']))
    subbot_delete = subbot_action(lambda sb, auth, body: sb.web_subbot_delete(auth['jid']))
    subbot_auto = subbot_action(lambda sb, auth, body: sb.web_subbot_set_auto(auth['jid'], jget(body, 'value')))
    return [
        ('GET', '/api/health', h_health),
        ('GET', '/api/audiotest.wav', h_audiotest),
        ('GET', '/api/proxy', h_proxy),
        ('POST', '/api/aiRich/action', h_airich_action),
        ('POST', '/api/upload', h_upload),
        ('GET', '/api/tierAsset', h_tier_asset),
        ('GET', '/api/botInfo', h_bot_info),
        ('GET', '/api/commandList', h_command_list),
        ('GET', '/api/envExample', h_env_example),
        ('POST', '/api/register', h_register),
        ('POST', '/api/login', h_login),
        ('POST', '/api/verify-otp', h_verify_otp),
        ('POST', '/api/resend-otp', h_resend_otp),
        ('GET', '/api/profile', h_profile),
        ('POST', '/api/daily', h_daily),
        ('POST', '/api/tierup', h_tierup),
        ('POST', '/api/change-password', h_change_password),
        ('POST', '/api/rename', h_rename),
        ('POST', '/api/redeem', h_redeem),
        ('POST', '/api/buy-limit', h_buy_limit),
        ('POST', '/api/buy-premium', h_buy_premium),
        ('POST', '/api/slot', h_slot),
        ('GET', '/api/gems', h_gems),
        ('GET', '/api/subbot', h_subbot_status),
        ('POST', '/api/subbot/connect', subbot_connect),
        ('POST', '/api/subbot/cancel', subbot_cancel),
        ('POST', '/api/subbot/start', subbot_start),
        ('POST', '/api/subbot/stop', subbot_stop),
        ('POST', '/api/subbot/delete', subbot_delete),
        ('POST', '/api/subbot/auto', subbot_auto),
        ('GET', '/api/subbot/session', h_subbot_session),
        ('POST', '/api/logout', h_logout),
        ('GET', '/api/check-owner', h_check_owner),
        ('GET', '/api/admin/giftcodes', h_admin_giftcodes_get),
        ('POST', '/api/admin/giftcodes/add', h_admin_giftcodes_add),
        ('POST', '/api/admin/giftcodes/delete', h_admin_giftcodes_delete),
        ('GET', '/api/site-settings', h_site_settings),
        ('GET', '/api/admin/settings', h_admin_settings_get),
        ('POST', '/api/admin/settings', h_admin_settings_post),
        ('GET', '/api/premium-notice', h_premium_notice),
        ('GET', '/api/user-search', h_user_search),
        ('GET', '/api/admin/users', h_admin_users),
        ('POST', '/api/admin/update-user', h_admin_update_user),
        ('POST', '/api/admin/delete-user', h_admin_delete_user),
        ('POST', '/api/check-number', h_check_number),
    ]


def route_kwargs(handler, ctx):
    return {
        p.name: ctx[p.name]
        for p in inspect.signature(handler).parameters.values()
        if p.kind is inspect.Parameter.KEYWORD_ONLY and p.name in ctx
    }


def run_route(req, res, ctx, handler):
    kwargs = route_kwargs(handler, ctx)
    if inspect.iscoroutinefunction(handler):
        future = asyncio.run_coroutine_threadsafe(handler(req, res, **kwargs), STATE['loop'])
        try:
            future.result(timeout=REQUEST_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            if not res.headers_sent:
                res.force(504, dumps({'success': False, 'message': 'Gateway Timeout'}))
        return
    handler(req, res, **kwargs)


class WebsiteRequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 60
    server_version = 'hiro-website'

    def log_message(self, format, *args):
        return None

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_PATCH(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def do_OPTIONS(self):
        self.dispatch()

    def dispatch(self):
        req = Req(self)
        res = Res(self)
        streamed_or_upgraded = False
        try:
            if self.headers.get('Upgrade') is not None:
                streamed_or_upgraded = True
                ws_upgrade(self, req, STATE['wss'])
                return

            pathname = req.pathname

            if pathname in CORS_ENABLED_PATHS:
                res.set_header('Access-Control-Allow-Origin', '*')
                res.set_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
                res.set_header('Access-Control-Allow-Headers', 'Content-Type')
                if req.method == 'OPTIONS':
                    res.write_head(204)
                    res.end(b'')
                    return

            if pathname.startswith('/api/'):
                block = check_memory_rate_limit(get_client_ip(req), 60 * 1000, 120, 60 * 1000)
                if block:
                    return send_json(res, 429, {'success': False, 'message': block})

            if 'text/html' in (req.header('accept') or ''):
                set_cookie(res, 'bypass-tunnel-reminder', '1', max_age_ms=7 * 24 * 60 * 60 * 1000, same_site='Lax')
            res.set_header('bypass-tunnel-reminder', '1')

            if req.method == 'GET' and pathname.startswith('/files/'):
                if serve_uploaded_file(res, pathname[len('/files/'):]):
                    return None
                return send_json(res, 404, {'success': False, 'message': 'File not found or expired.'})

            handler = None
            for method, route_path, route_handler in STATE['routes']:
                if method == req.method and route_path == pathname:
                    handler = route_handler
                    break

            if handler is not None:
                is_multipart = 'multipart/form-data' in (req.header('content-type') or '').lower()
                body = {}
                if not is_multipart and req.method in ('POST', 'PUT', 'PATCH'):
                    try:
                        body = read_json_body(req)
                    except Exception:
                        return send_json(res, 400, {'success': False, 'message': 'Invalid JSON body'})
                ctx = {'body': body, 'conn': STATE['conn']}
                return run_route(req, res, ctx, handler)

            if req.method == 'GET' and serve_static(res, pathname):
                return None

            return send_json(res, 404, {'success': False, 'message': 'Not found'})
        except Exception as err:
            log_err('[website]', exc_message(err))
            if not res.headers_sent:
                send_json(res, 500, {'success': False, 'message': 'Internal error'})
        finally:
            if not streamed_or_upgraded:
                flush_response(self, req, res)


def flush_response(handler, req, res):
    if res.streamed:
        return
    body = res.body or b''
    headers = list(res.headers)
    names = {key.lower() for key, _ in headers}
    if res.status not in (204, 304) and 'content-length' not in names:
        headers.append(('Content-Length', str(len(body))))
    if 'date' not in names:
        headers.append(('Date', email.utils.formatdate(usegmt=True)))
    body_unread = req.has_body() and not req.body_read
    if body_unread:
        handler.close_connection = True
    if handler.close_connection:
        headers.append(('Connection', 'close'))
    try:
        handler.wfile.write(render_head(res.status, headers))
        if req.method != 'HEAD' and body:
            handler.wfile.write(body)
        handler.wfile.flush()
    except OSError:
        handler.close_connection = True


def start_background(loop):
    async def every(seconds, fn, immediate=False):
        if immediate:
            await resolve(fn())
        while True:
            await asyncio.sleep(seconds)
            try:
                await resolve(fn())
            except Exception as err:
                log_err('[background]', exc_message(err))

    async def upload_cleanup():
        await asyncio.to_thread(cleanup_old_uploads_sync)

    async def heartbeat():
        wss = STATE['wss']
        if wss is None:
            return
        for ws in wss.snapshot():
            if not ws.isAlive:
                ws.terminate()
                continue
            ws.isAlive = False
            ws.ping()

    tasks = [
        every(60 * 60, upload_cleanup, immediate=True),
        every(10 * 60, cleanup_memory_rate_limit),
        every(15 * 60, cleanup_bruteforce),
        every(60, cleanup_otp),
        every(5 * 60, cleanup_html_actions),
        every(PREMIUM_CHECK_INTERVAL_S, premium_reminder_tick),
        every(WS_HEARTBEAT_S, heartbeat),
    ]
    for coro in tasks:
        spawn(coro)


def register_globals():
    G.registerHtmlAction = register_html_action
    G.broadcastToChannel = broadcast_to_channel
    G.authHelpers = {
        'sendOtp': send_otp,
        'verifyOtp': verify_otp,
        'hashPassword': hash_password,
        'verifyPassword': verify_password,
        'normalizePhone': normalize_phone,
        'generateToken': generate_token,
        'checkDbRateLimit': check_db_rate_limit,
        'clearDbRateLimit': clear_db_rate_limit,
        'ensureDB': ensure_db,
    }


def connect(conn, port):
    if STATE['server'] is None:
        loop = asyncio.get_running_loop()
        STATE['conn'] = conn
        STATE['loop'] = loop
        STATE['routes'] = [(method, route_path, route_handler) for method, route_path, route_handler in build_routes()]
        ensure_upload_dir()
        server = ThreadingHTTPServer(('0.0.0.0', int(port)), WebsiteRequestHandler)
        server.daemon_threads = True
        server.allow_reuse_address = True
        STATE['server'] = server
        G.server = server
        wss = WebSocketServer()
        wss.on('connection', lambda ws, req: on_ws_connection(STATE['conn'], ws, req))
        STATE['wss'] = wss
        start_background(loop)
        threading.Thread(target=server.serve_forever, name='website-http', daemon=True).start()

    if STATE['unpipe'] is not None:
        STATE['unpipe']()
    if STATE['wss'] is not None:
        STATE['unpipe'] = pipe_emit(conn, forward_all_clients, 'conn-')
    return STATE['server']


register_globals()
