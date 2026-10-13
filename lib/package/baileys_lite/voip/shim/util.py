import asyncio
import base64
import math
import re

from baileys_lite.foundation.node_crypto import timingSafeEqual
from baileys_lite.foundation.node_runtime import JSError


EMPTY_BYTES = b''

HEX_CHARS = '0123456789abcdefABCDEF'
BASE64_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
BASE64_LOOKUP = {ch: index for index, ch in enumerate(BASE64_CHARS)}
DECIMAL_NUMBER = re.compile(r'[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
PARSE_INT_PREFIX = re.compile(r'[ \t\n\r\f\v]*([+-]?[0-9]+)')
PARSE_FLOAT_PREFIX = re.compile(r'[ \t\n\r\f\v]*([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)')
MAX_SAFE_INTEGER = 2 ** 53 - 1


class TextEncoder:
    def encode(self, text):
        return text.encode('utf-8')


class TextDecoder:
    def decode(self, data):
        return bytes(data).decode('utf-8', 'replace')


TEXT_ENCODER = TextEncoder()
TEXT_DECODER = TextDecoder()


def optGet(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return dict.get(obj, key)
    if isinstance(obj, list):
        return None
    return getattr(obj, key, None)


def callOptional(obj, name, *args):
    member = optGet(obj, name)
    if member is None:
        return None
    return member(*args)


def messageOr(err):
    message = optGet(err, 'message')
    return err if not message else message


def itemAt(items, index):
    if 0 <= index < len(items):
        return items[index]
    return None


def nullish(value, fallback):
    return fallback if value is None else value


def spawnCatching(awaitable, onError):
    task = asyncio.ensure_future(awaitable)

    def settle(finished):
        if finished.cancelled():
            return
        failure = finished.exception()
        if failure is not None:
            onError(failure)

    task.add_done_callback(settle)
    return task


def resolvedFuture(value=None):
    future = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


def isSafeInteger(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return False
        value = int(value)
    if not isinstance(value, int):
        return False
    return -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER


def parseIntJs(value):
    match = PARSE_INT_PREFIX.match(str(value))
    if match is None:
        return None
    return int(match.group(1))


def parseFloatJs(value):
    match = PARSE_FLOAT_PREFIX.match(str(value))
    if match is None:
        return math.nan
    return float(match.group(1))


def toNumberJs(value):
    if value is None:
        return math.nan
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == '':
        return 0.0
    if DECIMAL_NUMBER.fullmatch(text):
        return float(text)
    return math.nan


def jsRound(value):
    number = toNumberJs(value)
    if not math.isfinite(number):
        return None
    return int(math.floor(number + 0.5))


def concatBytes(parts):
    return b''.join(parts)


def bytesToHex(value):
    return bytes(value).hex()


def hexToBytes(value):
    if len(value) & 1:
        raise JSError('hex string must have even length')
    for ch in value:
        if ch not in HEX_CHARS:
            raise JSError('invalid hex character')
    return bytes.fromhex(value)


def lookupBase64(ch):
    if ord(ch) > 127:
        raise JSError('invalid base64 character')
    if ch == '=':
        return 0
    index = BASE64_LOOKUP.get(ch)
    if index is None:
        raise JSError('invalid base64 character')
    return index


def bytesToBase64(value):
    return base64.b64encode(bytes(value)).decode('ascii')


def base64ToBytes(value):
    length = len(value)
    if length == 0:
        return EMPTY_BYTES
    if (length & 3) != 0:
        raise JSError('base64 string length must be multiple of 4')
    padding = 0
    if value[length - 1] == '=':
        padding += 1
    if value[length - 2] == '=':
        padding += 1
    outLen = ((length * 3) >> 2) - padding
    out = bytearray(outLen)
    j = 0
    mainLen = length - 4
    i = 0
    while i < mainLen:
        a = lookupBase64(value[i])
        b = lookupBase64(value[i + 1])
        c = lookupBase64(value[i + 2])
        d = lookupBase64(value[i + 3])
        out[j] = (a << 2) | (b >> 4)
        out[j + 1] = ((b & 0x0f) << 4) | (c >> 2)
        out[j + 2] = ((c & 0x03) << 6) | d
        j += 3
        i += 4
    a = lookupBase64(value[i])
    b = lookupBase64(value[i + 1])
    out[j] = (a << 2) | (b >> 4)
    j += 1
    if j < outLen:
        c = lookupBase64(value[i + 2])
        out[j] = ((b & 0x0f) << 4) | (c >> 2)
        j += 1
        if j < outLen:
            d = lookupBase64(value[i + 3])
            out[j] = ((c & 0x03) << 6) | d
            j += 1
    return bytes(out)


def toBytesView(value):
    if isinstance(value, bytes):
        return value
    return bytes(value)


def uint8TimingSafeEqual(left, right):
    if len(left) != len(right):
        return False
    return timingSafeEqual(left, right)


def toError(value):
    if isinstance(value, JSError):
        return value
    if isinstance(value, BaseException):
        return JSError(str(value), cause=value)
    if isinstance(value, str):
        return JSError(value)
    if isinstance(value, bool):
        return JSError('true' if value else 'false')
    if isinstance(value, (int, float)):
        return JSError(str(value))
    if value is not None:
        message = optGet(value, 'message')
        if isinstance(message, str) and len(message) > 0:
            return JSError(message)
        code = optGet(value, 'code')
        if isinstance(code, (str, int, float)) and not isinstance(code, bool):
            return JSError(f'unknown error ({code})')
    return JSError('unknown error')


def resolveOptionalPositive(value, name):
    if value is None:
        return None
    if isSafeInteger(value) and value > 0:
        return value
    raise JSError(f'{name} must be a positive safe integer')


def resolvePositive(value, fallback, name):
    resolved = resolveOptionalPositive(value, name)
    return fallback if resolved is None else resolved
