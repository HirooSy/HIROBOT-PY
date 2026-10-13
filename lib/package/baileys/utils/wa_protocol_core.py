import asyncio
import base64
import hashlib
import inspect
import math
import re
import platform as osPlatform
import sys
import traceback
import urllib.request
import urllib.error

from baileys.binary.wa_binary import (
    assertNodeErrorFree,
    decodeBinaryNode,
    getAllBinaryNodeChildren,
    getBinaryNodeChild,
    getBinaryNodeChildBuffer,
    getBinaryNodeChildUInt,
    getBinaryNodeChildren,
    getServerFromDomainType,
    jidDecode,
    S_WHATSAPP_NET,
    WAJIDDomains
)
from baileys.constants import DisconnectReason
from baileys.foundation.boom import Boom
from baileys.foundation.logger import createLogger
from baileys.foundation.long import stringToNumber
from baileys.foundation.node_crypto import createCipheriv, createDecipheriv, createHash, createHmac, randomBytes
from baileys.foundation.node_runtime import JSError, JSObject, clearTimeout, now, setTimeout
from baileys.foundation.wa_crypto import LTHashAntiTampering, hkdf, md5
from baileys.signal.libsignal import curve
from baileys.WAProto import proto

KEY_BUNDLE_TYPE = bytes([5])


def _truthy(value):
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value != ''
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == value and value != 0
    return True


def _or(*values):
    result = None
    for value in values:
        result = value
        if _truthy(value):
            return value
    return result


def _plus(value):
    if isinstance(value, str):
        return stringToNumber(value)
    if value is None:
        return math.nan
    if isinstance(value, bool):
        return int(value)
    return value


def _numStr(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _joinJS(items, sep):
    return sep.join('' if item is None else _numStr(item) if isinstance(item, (int, float)) else str(item) for item in items)


def _isValidUInt(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and float(value).is_integer()


def _errAttr(err, name):
    if err is None:
        return None
    if isinstance(err, dict):
        return err.get(name)
    return getattr(err, name, None)


def _asException(err):
    if isinstance(err, BaseException):
        return err
    return JSError(str(err))


def _resolveFuture(future, value=None):
    if not future.done():
        future.set_result(value)


def _rejectFuture(future, err=None):
    if not future.done():
        future.set_exception(_asException(err))


def _asyncListener(fn):
    def listener(*args):
        result = fn(*args)
        if inspect.isawaitable(result):
            return asyncio.ensure_future(result)
        return result
    return listener


async def _maybeAwait(value):
    if inspect.isawaitable(value):
        return await value
    return value


def generateSignalPubKey(pubKey):
    if len(pubKey) == 33:
        return pubKey
    return KEY_BUNDLE_TYPE + bytes(pubKey)


def _curveGenerateKeyPair():
    pair = curve.get('generateKeyPair')()
    return JSObject({'private': bytes(pair.get('privKey')), 'public': bytes(pair.get('pubKey')[1:])})


def _curveSharedKey(privateKey, publicKey):
    shared = curve.get('calculateAgreement')(generateSignalPubKey(publicKey), privateKey)
    return bytes(shared)


def _curveVerify(pubKey, message, signature):
    try:
        curve.get('verifySignature')(generateSignalPubKey(pubKey), message, signature)
        return True
    except Exception:
        return False


def _curveVerifyStrict(pubKey, message, signature):
    try:
        return curve.get('verifySignature')(generateSignalPubKey(pubKey), message, signature) is True
    except Exception:
        return False


Curve = JSObject({
    'generateKeyPair': _curveGenerateKeyPair,
    'sharedKey': _curveSharedKey,
    'sign': lambda privateKey, buf: curve.get('calculateSignature')(privateKey, buf),
    'verify': _curveVerify,
    'verifyStrict': _curveVerifyStrict
})


def signedKeyPair(identityKeyPair, keyId):
    preKey = Curve['generateKeyPair']()
    pubKey = generateSignalPubKey(preKey.get('public'))
    signature = Curve['sign'](identityKeyPair.get('private'), pubKey)
    return JSObject({'keyPair': preKey, 'signature': signature, 'keyId': keyId})


GCM_TAG_LENGTH = 128 >> 3


def aesEncryptGCM(plaintext, key, iv, additionalData):
    cipher = createCipheriv('aes-256-gcm', key, iv)
    cipher.setAAD(additionalData)
    return b''.join([cipher.update(plaintext), cipher.final(), cipher.getAuthTag()])


def aesDecryptGCM(ciphertext, key, iv, additionalData):
    decipher = createDecipheriv('aes-256-gcm', key, iv)
    enc = ciphertext[:len(ciphertext) - GCM_TAG_LENGTH]
    tag = ciphertext[len(ciphertext) - GCM_TAG_LENGTH:]
    decipher.setAAD(additionalData)
    decipher.setAuthTag(tag)
    return b''.join([decipher.update(enc), decipher.final()])


def aesEncryptCTR(plaintext, key, iv):
    cipher = createCipheriv('aes-256-ctr', key, iv)
    return b''.join([cipher.update(plaintext), cipher.final()])


def aesDecryptCTR(ciphertext, key, iv):
    decipher = createDecipheriv('aes-256-ctr', key, iv)
    return b''.join([decipher.update(ciphertext), decipher.final()])


def aesDecrypt(buffer, key):
    return aesDecryptWithIV(bytes(buffer[16:]), key, bytes(buffer[:16]))


def aesDecryptWithIV(buffer, key, IV):
    aes = createDecipheriv('aes-256-cbc', key, IV)
    return b''.join([aes.update(buffer), aes.final()])


def aesEncrypt(buffer, key):
    IV = randomBytes(16)
    aes = createCipheriv('aes-256-cbc', key, IV)
    return b''.join([IV, aes.update(buffer), aes.final()])


def aesEncrypWithIV(buffer, key, IV):
    aes = createCipheriv('aes-256-cbc', key, IV)
    return b''.join([aes.update(buffer), aes.final()])


def hmacSign(buffer, key, variant='sha256'):
    return createHmac(variant, key).update(buffer).digest()


def sha256(buffer):
    return createHash('sha256').update(buffer).digest()


async def derivePairingCodeKey(pairingCode, salt):
    passwordBytes = pairingCode.encode('utf-8')
    saltBytes = bytes(salt)
    return hashlib.pbkdf2_hmac('sha256', passwordBytes, saltBytes, 2 << 16, 32)


LT_HASH_ANTI_TAMPERING = LTHashAntiTampering()


def _bufferFrom(value):
    if isinstance(value, str):
        return value.encode('utf-8')
    return bytes(int(item) & 0xFF for item in value)


def _bufferReplacer(k, value):
    if isinstance(value, (bytes, bytearray)) or (isinstance(value, dict) and value.get('type') == 'Buffer'):
        data = value.get('data') if isinstance(value, dict) else None
        source = data if _truthy(data) else value
        return JSObject({'type': 'Buffer', 'data': base64.b64encode(_bufferFrom(source)).decode()})
    return value


def _bufferReviver(_, value):
    if isinstance(value, dict) and not isinstance(value, list):
        if value.get('type') == 'Buffer' and isinstance(value.get('data'), str):
            return base64.b64decode(value.get('data'))
        keys = list(dict.keys(value))
        if len(keys) > 0 and all(_isIntegerKey(k) for k in keys):
            values = list(dict.values(value))
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
                return _bufferFrom(values)
    return value


def _isIntegerKey(key):
    text = str(key).strip()
    if text[:1] in ('+', '-'):
        text = text[1:]
    return text.isdigit()


BufferJSON = JSObject({
    'replacer': _bufferReplacer,
    'reviver': _bufferReviver
})


def getKeyAuthor(key, meId='me'):
    if key is None:
        return ''
    if key.get('fromMe'):
        chosen = meId
    else:
        chosen = _or(key.get('participantAlt'), key.get('remoteJidAlt'), key.get('participant'), key.get('remoteJid'))
    return _or(chosen, '')


def isStringNullOrEmpty(value):
    return value is None or (isinstance(value, str) and value == '')


def writeRandomPadMax16(msg):
    pad = randomBytes(1)
    padLength = (pad[0] & 0x0f) + 1
    return bytes(msg) + bytes([padLength]) * padLength


def unpadRandomMax16(e):
    t = bytes(e)
    if len(t) == 0:
        raise JSError('unpadPkcs7 given empty bytes')
    r = t[len(t) - 1]
    if r > len(t):
        raise JSError(f'unpad given {len(t)} bytes, but pad is {r}')
    return t[:len(t) - r]


def generateParticipantHashV2(participants):
    participants.sort()
    sha256Hash = base64.b64encode(sha256(''.join(participants).encode('utf-8'))).decode()
    return '2:' + sha256Hash[:6]


def encodeWAMessage(message):
    return writeRandomPadMax16(proto.Message.encode(message).finish())


def generateRegistrationId():
    return randomBytes(2)[0] & 16383


def encodeBigEndian(e, t=4):
    r = int(e) & 0xFFFFFFFF
    a = bytearray(t)
    for i in range(t - 1, -1, -1):
        a[i] = r & 0xFF
        r >>= 8
    return bytes(a)


def toNumber(t):
    if t is not None and not isinstance(t, (bool, int, float, str)):
        if isinstance(t, dict):
            if 'toNumber' in t:
                return t.get('toNumber')()
            return t.get('low')
        if hasattr(t, 'toNumber'):
            return t.toNumber()
        return getattr(t, 'low', None)
    return t if _truthy(t) else 0


def unixTimestampSeconds(date=None):
    if date is None:
        ms = now()
    elif hasattr(date, 'getTime'):
        ms = date.getTime()
    else:
        ms = date
    return int(ms / 1000)


def delayCancellable(ms):
    stack = ''.join(traceback.format_stack())
    loop = asyncio.get_running_loop()
    delayFuture = loop.create_future()
    timeout = setTimeout(lambda: _resolveFuture(delayFuture, None), ms)

    def cancel():
        clearTimeout(timeout)
        _rejectFuture(delayFuture, Boom('Cancelled', JSObject({'statusCode': 500, 'data': JSObject({'stack': stack})})))

    return JSObject({'delay': delayFuture, 'cancel': cancel})


def debouncedTimeout(intervalMs=1000, task=None):
    timeout = None

    def start(newIntervalMs, newTask=None):
        nonlocal task, intervalMs, timeout
        task = newTask if _truthy(newTask) else task
        intervalMs = newIntervalMs if _truthy(newIntervalMs) else intervalMs
        if timeout is not None:
            clearTimeout(timeout)

        def fire():
            if task is not None:
                _fireAndForget(task())
        timeout = setTimeout(fire, intervalMs)

    def cancel():
        nonlocal timeout
        if timeout is not None:
            clearTimeout(timeout)
        timeout = None

    def setTask(newTask):
        nonlocal task
        task = newTask
        return newTask

    def setInterval_(newInterval):
        nonlocal intervalMs
        intervalMs = newInterval
        return newInterval

    return JSObject({'start': start, 'cancel': cancel, 'setTask': setTask, 'setInterval': setInterval_})


def delay(ms):
    return delayCancellable(ms)['delay']


def _newPromise(executor):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    try:
        executor(lambda value=None: _resolveFuture(future, value), lambda err=None: _rejectFuture(future, err))
    except Exception as err:
        _rejectFuture(future, err)
    return future


async def promiseTimeout(ms, promiseExecutor):
    if not _truthy(ms):
        return await _newPromise(promiseExecutor)
    stack = ''.join(traceback.format_stack())
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    timeout = setTimeout(
        lambda: _rejectFuture(future, Boom('Timed Out', JSObject({'statusCode': 408, 'data': JSObject({'stack': stack})}))),
        ms
    )
    try:
        executor = promiseExecutor
        executor(lambda value=None: _resolveFuture(future, value), lambda err=None: _rejectFuture(future, err))
    except Exception as err:
        _rejectFuture(future, err)
    try:
        return await future
    finally:
        clearTimeout(timeout)


def generateMessageIDV2(userId):
    data = bytearray(8 + 20 + 16)
    data[0:8] = (int(now() / 1000)).to_bytes(8, 'big')
    if _truthy(userId):
        decoded = jidDecode(userId)
        if decoded is not None and _truthy(decoded.get('user')):
            user = decoded.get('user').encode('utf-8')
            data[8:8 + len(user)] = user
            tail = b'@c.us'
            data[8 + len(user):8 + len(user) + len(tail)] = tail
    random = randomBytes(16)
    data[28:44] = random
    digest = createHash('sha256').update(bytes(data)).digest()
    return '3EB0' + digest.hex().upper()[:18]


def generateMessageID():
    return '3EB0' + randomBytes(18).hex().upper()


def bindWaitForEvent(ev, event):
    async def waitFor(check, timeoutMs):
        state = JSObject({'listener': None, 'closeListener': None})

        def executor(resolve, reject):
            def closeListener(update):
                connection = update.get('connection')
                lastDisconnect = update.get('lastDisconnect')
                if connection == 'close':
                    err = _errAttr(lastDisconnect, 'error')
                    reject(err if _truthy(err) else Boom('Connection Closed', JSObject({'statusCode': 428})))
            state['closeListener'] = _asyncListener(closeListener)
            ev.get('on')('connection.update', state.get('closeListener'))

            async def onEvent(update):
                if await _maybeAwait(check(update)):
                    resolve()
            state['listener'] = _asyncListener(onEvent)
            ev.get('on')(event, state.get('listener'))

        try:
            await promiseTimeout(timeoutMs, executor)
        finally:
            ev.get('off')(event, state.get('listener'))
            ev.get('off')('connection.update', state.get('closeListener'))

    return waitFor


def bindWaitForConnectionUpdate(ev):
    return bindWaitForEvent(ev, 'connection.update')


def _httpGet(url, headers):
    request = urllib.request.Request(url, headers=dict(headers) if headers is not None else {}, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return JSObject({'ok': 200 <= response.status < 300, 'status': response.status, 'statusText': response.reason, 'text': response.read().decode('utf-8', 'replace')})
    except urllib.error.HTTPError as err:
        return JSObject({'ok': False, 'status': err.code, 'statusText': err.reason, 'text': ''})


baileysVersion = [2, 3000, 1047970367]


async def fetchLatestBaileysVersion(options=None):
    if options is None:
        options = JSObject({})
    url = 'https://raw.githubusercontent.com/WhiskeySockets/Baileys/master/src/Defaults/index.ts'
    try:
        response = await asyncio.to_thread(_httpGet, url, options.get('headers'))
        if not response.get('ok'):
            raise Boom(f"Failed to fetch latest Baileys version: {response.get('statusText')}", JSObject({'statusCode': response.get('status')}))
        text = response.get('text')
        versionMatch = re.search(r'const version = \[(\d+),\s*(\d+),\s*(\d+)\]', text)
        if versionMatch:
            version = [int(versionMatch.group(1)), int(versionMatch.group(2)), int(versionMatch.group(3))]
            return JSObject({'version': version, 'isLatest': True})
        raise JSError('Could not parse version from Defaults/index.ts')
    except Exception as error:
        return JSObject({'version': baileysVersion, 'isLatest': False, 'error': error})


async def fetchLatestWaWebVersion(options=None):
    if options is None:
        options = JSObject({})
    try:
        defaultHeaders = JSObject({
            'sec-fetch-site': 'none',
            'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        })
        headers = JSObject({**defaultHeaders, **(options.get('headers') or {})})
        response = await asyncio.to_thread(_httpGet, 'https://web.whatsapp.com/sw.js', headers)
        if not response.get('ok'):
            raise Boom(f"Failed to fetch sw.js: {response.get('statusText')}", JSObject({'statusCode': response.get('status')}))
        data = response.get('text')
        match = re.search(r'\\?"client_revision\\?":\s*(\d+)', data)
        if match is None or not _truthy(match.group(1)):
            return JSObject({'version': baileysVersion, 'isLatest': False, 'error': JSObject({'message': 'Could not find client revision in the fetched content'})})
        return JSObject({'version': [2, 3000, int(match.group(1))], 'isLatest': True})
    except Exception as error:
        return JSObject({'version': baileysVersion, 'isLatest': False, 'error': error})


def generateMdTagPrefix():
    raw = randomBytes(4)
    return f"{int.from_bytes(raw[0:2], 'big')}.{int.from_bytes(raw[2:4], 'big')}-"


STATUS_MAP = JSObject({
    'sender': proto.WebMessageInfo.Status.SERVER_ACK,
    'played': proto.WebMessageInfo.Status.PLAYED,
    'read': proto.WebMessageInfo.Status.READ,
    'read-self': proto.WebMessageInfo.Status.READ
})


def getStatusFromReceiptType(type_):
    status = STATUS_MAP[type_]
    if type_ is None:
        return proto.WebMessageInfo.Status.DELIVERY_ACK
    return status


CODE_MAP = JSObject({'conflict': DisconnectReason['connectionReplaced']})


def getErrorCodeFromStreamError(node):
    children = getAllBinaryNodeChildren(node)
    reasonNode = children[0] if len(children) else None
    reason = _or(reasonNode.get('tag') if reasonNode is not None else None, 'unknown')
    attrs = node.get('attrs')
    statusCode = _plus(_or(attrs.get('code'), CODE_MAP[reason], DisconnectReason['badSession']))
    if reason == 'conflict' and not _truthy(attrs.get('code')):
        reasonAttrs = reasonNode.get('attrs') if reasonNode is not None else None
        if reasonAttrs is not None and reasonAttrs.get('type') == 'replaced':
            statusCode = DisconnectReason['connectionReplaced']
        else:
            statusCode = DisconnectReason['loggedOut']
            reason = 'device removed'
    if statusCode == DisconnectReason['restartRequired']:
        reason = 'restart required'
    return JSObject({'reason': reason, 'statusCode': statusCode})


def getCallStatusFromNode(node):
    tag = node.get('tag')
    attrs = node.get('attrs')
    if tag in ('offer', 'offer_notice'):
        return 'offer'
    if tag == 'terminate':
        return 'timeout' if attrs.get('reason') == 'timeout' else 'terminate'
    if tag in ('preaccept', 'transport', 'relaylatency', 'reject', 'accept'):
        return tag
    return 'ringing'


UNEXPECTED_SERVER_CODE_TEXT = 'Unexpected server response: '


def getCodeFromWSError(error):
    code = _or(_errAttr(error, 'code'), _errAttr(_errAttr(error, 'cause'), 'code'))
    message = _errAttr(error, 'message')
    if isinstance(message, str) and UNEXPECTED_SERVER_CODE_TEXT in message:
        try:
            parsed = _plus(message[len(UNEXPECTED_SERVER_CODE_TEXT):])
        except Exception:
            parsed = math.nan
        if parsed == parsed and parsed >= 400:
            return parsed
    return 408


def isWABusinessPlatform(platform):
    return platform == 'smbi' or platform == 'smba'


def trimUndefined(obj):
    for key in list(dict.keys(obj)):
        if obj[key] is None:
            del obj[key]
    return obj


CROCKFORD_CHARACTERS = '123456789ABCDEFGHJKLMNPQRSTVWXYZ'


def bytesToCrockford(buffer):
    value = 0
    bitCount = 0
    crockford = []
    for element in buffer:
        value = ((value << 8) | (element & 0xff)) & 0xFFFFFFFF
        bitCount += 8
        while bitCount >= 5:
            crockford.append(CROCKFORD_CHARACTERS[(value >> (bitCount - 5)) & 31])
            bitCount -= 5
    if bitCount > 0:
        crockford.append(CROCKFORD_CHARACTERS[(value << (5 - bitCount)) & 31])
    return ''.join(crockford)


def encodeNewsletterMessage(message):
    return proto.Message.encode(message).finish()


PLATFORM_MAP = JSObject({
    'aix': 'AIX',
    'darwin': 'Mac OS',
    'win32': 'Windows',
    'android': 'Android',
    'freebsd': 'FreeBSD',
    'openbsd': 'OpenBSD',
    'sunos': 'Solaris',
    'linux': None,
    'haiku': None,
    'cygwin': None,
    'netbsd': None
})


def _nodePlatform():
    p = sys.platform
    if p.startswith('freebsd'):
        return 'freebsd'
    if p.startswith('openbsd'):
        return 'openbsd'
    if p.startswith('netbsd'):
        return 'netbsd'
    if p.startswith('sunos') or p.startswith('solaris'):
        return 'sunos'
    return p


def _osRelease():
    return osPlatform.release()


Browsers = JSObject({
    'ubuntu': lambda browser: ['Ubuntu', browser, '22.04.4'],
    'macOS': lambda browser: ['Mac OS', browser, '14.4.1'],
    'baileys': lambda browser: ['Baileys', browser, '6.5.0'],
    'windows': lambda browser: ['Windows', browser, '10.0.22631'],
    'android': lambda browser: [browser, 'Android', ''],
    'appropriate': lambda browser: [_or(PLATFORM_MAP[_nodePlatform()], 'Ubuntu'), browser, _osRelease()]
})


def getPlatformId(browser):
    platformType = proto.DeviceProps.PlatformType[browser.upper()]
    return _numStr(platformType) if _truthy(platformType) else '1'


CompanionWebClientType = JSObject({
    'UNKNOWN': 0,
    'CHROME': 1,
    'EDGE': 2,
    'FIREFOX': 3,
    'IE': 4,
    'OPERA': 5,
    'SAFARI': 6,
    'ELECTRON': 7,
    'UWP': 8,
    'OTHER_WEB_CLIENT': 9
})

BROWSER_TO_COMPANION_WEB_CLIENT = JSObject({
    'Chrome': CompanionWebClientType['CHROME'],
    'Edge': CompanionWebClientType['EDGE'],
    'Firefox': CompanionWebClientType['FIREFOX'],
    'IE': CompanionWebClientType['IE'],
    'Opera': CompanionWebClientType['OPERA'],
    'Safari': CompanionWebClientType['SAFARI']
})


def getCompanionWebClientType(browser):
    os_ = browser[0]
    browserName = browser[1]
    if browserName == 'Desktop':
        return CompanionWebClientType['UWP'] if os_ == 'Windows' else CompanionWebClientType['ELECTRON']
    return _or(BROWSER_TO_COMPANION_WEB_CLIENT[browserName], CompanionWebClientType['OTHER_WEB_CLIENT'])


def getCompanionPlatformId(browser):
    return _numStr(getCompanionWebClientType(browser))


def buildPairingQRData(ref, noiseKeyB64, identityKeyB64, advB64, browser):
    return 'https://wa.me/settings/linked_devices#' + _joinJS([ref, noiseKeyB64, identityKeyB64, advB64, getCompanionPlatformId(browser)], ',')


def buildAckStanza(node, errorCode, meId):
    tag = node.get('tag')
    attrs = node.get('attrs')
    stanza = JSObject({'tag': 'ack', 'attrs': JSObject({'id': attrs.get('id'), 'to': attrs.get('from'), 'class': tag})})
    if _truthy(errorCode):
        stanza.get('attrs')['error'] = _numStr(errorCode)
    if _truthy(attrs.get('participant')):
        stanza.get('attrs')['participant'] = attrs.get('participant')
    if _truthy(attrs.get('recipient')):
        stanza.get('attrs')['recipient'] = attrs.get('recipient')
    if _truthy(attrs.get('type')):
        stanza.get('attrs')['type'] = attrs.get('type')
    if tag == 'message' and _truthy(meId):
        stanza.get('attrs')['from'] = meId
    return stanza


def createSignalIdentity(wid, accountSignatureKey):
    return JSObject({
        'identifier': JSObject({'name': wid, 'deviceId': 0}),
        'identifierKey': generateSignalPubKey(accountSignatureKey)
    })


async def getPreKeys(keysStore, min, limit):
    idList = [str(i) for i in range(min, limit)]
    return await _maybeAwait(keysStore.get('get')('pre-key', idList))


def generateOrGetPreKeys(creds, range_):
    avaliable = creds.get('nextPreKeyId') - creds.get('firstUnuploadedPreKeyId')
    remaining = range_ - avaliable
    lastPreKeyId = creds.get('nextPreKeyId') + remaining - 1
    newPreKeys = JSObject({})
    if remaining > 0:
        for i in range(creds.get('nextPreKeyId'), lastPreKeyId + 1):
            newPreKeys[str(i)] = Curve['generateKeyPair']()
    return JSObject({
        'newPreKeys': newPreKeys,
        'lastPreKeyId': lastPreKeyId,
        'preKeysRange': [creds.get('firstUnuploadedPreKeyId'), range_]
    })


def xmppSignedPreKey(key):
    return JSObject({
        'tag': 'skey',
        'attrs': JSObject({}),
        'content': [
            JSObject({'tag': 'id', 'attrs': JSObject({}), 'content': encodeBigEndian(key.get('keyId'), 3)}),
            JSObject({'tag': 'value', 'attrs': JSObject({}), 'content': key.get('keyPair').get('public')}),
            JSObject({'tag': 'signature', 'attrs': JSObject({}), 'content': key.get('signature')})
        ]
    })


def xmppPreKey(pair, id_):
    return JSObject({
        'tag': 'key',
        'attrs': JSObject({}),
        'content': [
            JSObject({'tag': 'id', 'attrs': JSObject({}), 'content': encodeBigEndian(id_, 3)}),
            JSObject({'tag': 'value', 'attrs': JSObject({}), 'content': pair.get('public')})
        ]
    })


def extractE2ESessionFromRetryReceipt(receipt):
    keysNode = getBinaryNodeChild(receipt, 'keys')
    if keysNode is None:
        return None
    typeBuf = getBinaryNodeChildBuffer(keysNode, 'type')
    if typeBuf is None or len(typeBuf) != 1 or typeBuf[0] != KEY_BUNDLE_TYPE[0]:
        return None
    identity = getBinaryNodeChildBuffer(keysNode, 'identity')
    skey = getBinaryNodeChild(keysNode, 'skey')
    if identity is None or len(identity) != 32 or skey is None:
        return None
    registrationId = getBinaryNodeChildUInt(receipt, 'registration', 4)
    if not _isValidUInt(registrationId):
        return None
    signedPubKey = getBinaryNodeChildBuffer(skey, 'value')
    signedSig = getBinaryNodeChildBuffer(skey, 'signature')
    signedKeyId = getBinaryNodeChildUInt(skey, 'id', 3)
    if signedPubKey is None or len(signedPubKey) != 32 or signedSig is None or not _isValidUInt(signedKeyId):
        return None
    preKeyNode = getBinaryNodeChild(keysNode, 'key')
    preKey = None
    if preKeyNode is not None:
        preKeyPub = getBinaryNodeChildBuffer(preKeyNode, 'value')
        preKeyId = getBinaryNodeChildUInt(preKeyNode, 'id', 3)
        if preKeyPub is None or len(preKeyPub) != 32 or not _isValidUInt(preKeyId):
            return None
        preKey = JSObject({'keyId': preKeyId, 'publicKey': generateSignalPubKey(preKeyPub)})
    return JSObject({
        'registrationId': registrationId,
        'identityKey': generateSignalPubKey(identity),
        'signedPreKey': JSObject({'keyId': signedKeyId, 'publicKey': generateSignalPubKey(signedPubKey), 'signature': signedSig}),
        'preKey': preKey
    })


async def parseAndInjectE2ESessions(node, repository):
    def extractKey(key):
        if not _truthy(key):
            return None
        return JSObject({
            'keyId': getBinaryNodeChildUInt(key, 'id', 3),
            'publicKey': generateSignalPubKey(getBinaryNodeChildBuffer(key, 'value')),
            'signature': getBinaryNodeChildBuffer(key, 'signature')
        })

    nodes = getBinaryNodeChildren(getBinaryNodeChild(node, 'list'), 'user')
    for n in nodes:
        assertNodeErrorFree(n)

    chunkSize = 100
    chunks = [nodes[i:i + chunkSize] for i in range(0, len(nodes), chunkSize)]
    for nodesChunk in chunks:
        for n in nodesChunk:
            signedKey = getBinaryNodeChild(n, 'skey')
            key = getBinaryNodeChild(n, 'key')
            identity = getBinaryNodeChildBuffer(n, 'identity')
            jid = n.get('attrs').get('jid')
            registrationId = getBinaryNodeChildUInt(n, 'registration', 4)
            await _maybeAwait(repository.get('injectE2ESession')(JSObject({
                'jid': jid,
                'session': JSObject({
                    'registrationId': registrationId,
                    'identityKey': generateSignalPubKey(identity),
                    'signedPreKey': extractKey(signedKey),
                    'preKey': extractKey(key)
                })
            })))


def extractDeviceJids(result, myJid, myLid, excludeZeroDevices):
    decodedMe = jidDecode(myJid)
    myUser = decodedMe.get('user')
    myDevice = decodedMe.get('device')
    extracted = []
    for userResult in result:
        devices = userResult.get('devices')
        decoded = jidDecode(userResult.get('id'))
        user = decoded.get('user')
        server = decoded.get('server')
        domainType = decoded.get('domainType')
        deviceList = devices.get('deviceList') if devices is not None else None
        if not isinstance(deviceList, list):
            continue
        for entry in deviceList:
            device = entry.get('id')
            keyIndex = entry.get('keyIndex')
            isHosted = entry.get('isHosted')
            if ((not excludeZeroDevices or device != 0)
                    and ((myUser != user and myLid != user) or myDevice != device)
                    and (device == 0 or _truthy(keyIndex))):
                if _truthy(isHosted):
                    domainType = WAJIDDomains['HOSTED_LID'] if domainType == WAJIDDomains['LID'] else WAJIDDomains['HOSTED']
                extracted.append(JSObject({
                    'user': user,
                    'device': device,
                    'domainType': domainType,
                    'server': getServerFromDomainType(server, domainType)
                }))
    return extracted


async def getNextPreKeys(state, count):
    creds = state.get('creds')
    keys = state.get('keys')
    generated = generateOrGetPreKeys(creds, count)
    newPreKeys = generated.get('newPreKeys')
    lastPreKeyId = generated.get('lastPreKeyId')
    preKeysRange = generated.get('preKeysRange')
    update = JSObject({
        'nextPreKeyId': max(lastPreKeyId + 1, creds.get('nextPreKeyId')),
        'firstUnuploadedPreKeyId': max(creds.get('firstUnuploadedPreKeyId'), lastPreKeyId + 1)
    })
    await _maybeAwait(keys.get('set')(JSObject({'pre-key': newPreKeys})))
    preKeys = await getPreKeys(keys, preKeysRange[0], preKeysRange[0] + preKeysRange[1])
    return JSObject({'update': update, 'preKeys': preKeys})


async def getNextPreKeysNode(state, count):
    creds = state.get('creds')
    result = await getNextPreKeys(state, count)
    update = result.get('update')
    preKeys = result.get('preKeys')
    node = JSObject({
        'tag': 'iq',
        'attrs': JSObject({'xmlns': 'encrypt', 'type': 'set', 'to': S_WHATSAPP_NET}),
        'content': [
            JSObject({'tag': 'registration', 'attrs': JSObject({}), 'content': encodeBigEndian(creds.get('registrationId'))}),
            JSObject({'tag': 'type', 'attrs': JSObject({}), 'content': KEY_BUNDLE_TYPE}),
            JSObject({'tag': 'identity', 'attrs': JSObject({}), 'content': creds.get('signedIdentityKey').get('public')}),
            JSObject({'tag': 'list', 'attrs': JSObject({}), 'content': [xmppPreKey(preKeys[k], int(k)) for k in list(dict.keys(preKeys))]}),
            xmppSignedPreKey(creds.get('signedPreKey'))
        ]
    })
    return JSObject({'update': update, 'node': node})


NOISE_MODE = b'Noise_XX_25519_AESGCM_SHA256\x00\x00\x00\x00'
WA_CERT_DETAILS = JSObject({
    'SERIAL': 0,
    'ISSUER': 'WhatsAppLongTerm1',
    'PUBLIC_KEY': bytes.fromhex('142375574d0a587166aae71ebe516437c4a28b73e3695c6ce1f7f9545da8ee6b')
})
IV_LENGTH = 12
EMPTY_BUFFER = b''
MAX_NOISE_IN_BYTES = 10 * 1024 * 1024


def generateIV(counter):
    return bytes(8) + (counter & 0xFFFFFFFF).to_bytes(4, 'big')


class TransportState:
    def __init__(self, encKey, decKey):
        self.encKey = encKey
        self.decKey = decKey
        self.readCounter = 0
        self.writeCounter = 0
        self.iv = bytearray(IV_LENGTH)

    def encrypt(self, plaintext):
        c = self.writeCounter
        self.writeCounter += 1
        self.iv[8:12] = (c & 0xFFFFFFFF).to_bytes(4, 'big')
        return aesEncryptGCM(plaintext, self.encKey, bytes(self.iv), EMPTY_BUFFER)

    def decrypt(self, ciphertext):
        c = self.readCounter
        self.readCounter += 1
        self.iv[8:12] = (c & 0xFFFFFFFF).to_bytes(4, 'big')
        return aesDecryptGCM(ciphertext, self.decKey, bytes(self.iv), EMPTY_BUFFER)


def _fireAndForget(result):
    if inspect.isawaitable(result):
        asyncio.ensure_future(result)


def makeNoiseHandler(params):
    keyPair = params.get('keyPair')
    privateKey = keyPair.get('private')
    publicKey = keyPair.get('public')
    NOISE_HEADER = params.get('NOISE_HEADER')
    logger = params.get('logger')
    routingInfo = params.get('routingInfo')

    logger = (logger if logger is not None else createLogger()).child(JSObject({'class': 'ns'}))
    data = NOISE_MODE
    hash_ = data if len(data) == 32 else sha256(data)
    salt = hash_
    encKey = hash_
    decKey = hash_
    counter = 0
    sentIntro = False
    inBytes = b''
    transport = None
    isWaitingForTransport = False
    pendingOnFrame = None
    destroyed = False
    decodeChain = None

    if routingInfo is not None:
        routing = bytes(routingInfo)
        introHeader = bytearray(7 + len(routing) + len(NOISE_HEADER))
        introHeader[0:2] = b'ED'
        introHeader[2] = 0
        introHeader[3] = 1
        introHeader[4] = (len(routing) >> 16) & 0xFF
        introHeader[5:7] = (len(routing) & 65535).to_bytes(2, 'big')
        introHeader[7:7 + len(routing)] = routing
        introHeader[7 + len(routing):] = bytes(NOISE_HEADER)
        introHeader = bytes(introHeader)
    else:
        introHeader = bytes(NOISE_HEADER)

    def authenticate(payload):
        nonlocal hash_
        if transport is None:
            hash_ = sha256(hash_ + bytes(payload))

    authenticate(NOISE_HEADER)
    authenticate(publicKey)

    def encrypt(plaintext):
        nonlocal counter
        if transport is not None:
            return transport.encrypt(plaintext)
        result = aesEncryptGCM(plaintext, encKey, generateIV(counter), hash_)
        counter += 1
        authenticate(result)
        return result

    def decrypt(ciphertext):
        nonlocal counter
        if transport is not None:
            return transport.decrypt(ciphertext)
        result = aesDecryptGCM(ciphertext, decKey, generateIV(counter), hash_)
        counter += 1
        authenticate(ciphertext)
        return result

    def localHKDF(payload):
        key = hkdf(bytes(payload), 64, JSObject({'salt': salt, 'info': ''}))
        return key[:32], key[32:]

    def mixIntoKey(payload):
        nonlocal salt, encKey, decKey, counter
        write, read = localHKDF(payload)
        salt = write
        encKey = read
        decKey = read
        counter = 0

    async def processData(onFrame):
        nonlocal inBytes, destroyed
        while not destroyed:
            if len(inBytes) < 3:
                return
            size = (inBytes[0] << 16) | (inBytes[1] << 8) | inBytes[2]
            if len(inBytes) < size + 3:
                return
            frame = inBytes[3:size + 3]
            inBytes = inBytes[size + 3:]
            if transport is not None:
                result = transport.decrypt(frame)
                try:
                    frame = await decodeBinaryNode(result)
                except Exception as err:
                    logger.error(JSObject({'err': err, 'size': size}), 'failed to decode frame, skipping')
                    continue
                if destroyed:
                    return
            if logger.level == 'trace':
                logger.trace(JSObject({'msg': _optionalAttr(frame, 'attrs', 'id')}), 'recv frame')
            try:
                _fireAndForget(onFrame(frame))
            except Exception as err:
                logger.error(JSObject({'err': err, 'id': _optionalAttr(frame, 'attrs', 'id'), 'tag': _optionalAttr(frame, 'tag')}), 'frame handler threw')

    async def finishInit():
        nonlocal isWaitingForTransport, transport, pendingOnFrame
        isWaitingForTransport = True
        write, read = localHKDF(b'')
        transport = TransportState(write, read)
        isWaitingForTransport = False
        logger.trace('Noise handler transitioned to Transport state')
        if pendingOnFrame is not None:
            logger.trace(JSObject({'length': len(inBytes)}), 'Flushing buffered frames after transport ready')
            await processData(pendingOnFrame)
            pendingOnFrame = None

    def processHandshake(params_, noiseKey):
        serverHello = params_.get('serverHello')
        authenticate(serverHello.get('ephemeral'))
        mixIntoKey(_curveSharedKey(privateKey, serverHello.get('ephemeral')))
        decStaticContent = decrypt(serverHello.get('static'))
        mixIntoKey(_curveSharedKey(privateKey, decStaticContent))
        certDecoded = decrypt(serverHello.get('payload'))
        certChain = proto.CertChain.decode(certDecoded)
        certIntermediate = certChain.get('intermediate')
        leaf = certChain.get('leaf')

        if leaf is None or leaf.get('details') is None or leaf.get('signature') is None:
            raise Boom('invalid noise leaf certificate', JSObject({'statusCode': 400}))
        if certIntermediate is None or certIntermediate.get('details') is None or certIntermediate.get('signature') is None:
            raise Boom('invalid noise intermediate certificate', JSObject({'statusCode': 400}))

        details = proto.CertChain.NoiseCertificate.Details.decode(certIntermediate.get('details'))
        issuerSerial = details.get('issuerSerial')
        verify = Curve['verify'](details.get('key'), leaf.get('details'), leaf.get('signature'))
        verifyIntermediate = Curve['verify'](WA_CERT_DETAILS['PUBLIC_KEY'], certIntermediate.get('details'), certIntermediate.get('signature'))

        if not verify:
            raise Boom('noise certificate signature invalid', JSObject({'statusCode': 400}))
        if not verifyIntermediate:
            raise Boom('noise intermediate certificate signature invalid', JSObject({'statusCode': 400}))
        if issuerSerial != WA_CERT_DETAILS['SERIAL']:
            raise Boom('certification match failed', JSObject({'statusCode': 400}))

        keyEnc = encrypt(noiseKey.get('public'))
        mixIntoKey(_curveSharedKey(noiseKey.get('private'), serverHello.get('ephemeral')))
        return keyEnc

    def encodeFrame(payload):
        nonlocal sentIntro
        data_ = bytes(payload)
        if transport is not None:
            data_ = transport.encrypt(data_)
        dataLen = len(data_)
        introSize = 0 if sentIntro else len(introHeader)
        frame = bytearray(introSize + 3 + dataLen)
        if not sentIntro:
            frame[0:len(introHeader)] = introHeader
            sentIntro = True
        frame[introSize] = (dataLen >> 16) & 0xFF
        frame[introSize + 1] = (dataLen >> 8) & 0xFF
        frame[introSize + 2] = dataLen & 0xFF
        frame[introSize + 3:introSize + 3 + dataLen] = data_
        return bytes(frame)

    def decodeFrame(newData, onFrame):
        nonlocal decodeChain, inBytes, pendingOnFrame

        async def run():
            nonlocal inBytes, pendingOnFrame
            if destroyed:
                return
            if isWaitingForTransport:
                inBytes = inBytes + bytes(newData)
                pendingOnFrame = onFrame
            else:
                inBytes = bytes(newData) if len(inBytes) == 0 else inBytes + bytes(newData)
            if len(inBytes) > MAX_NOISE_IN_BYTES:
                logger.error(JSObject({'bufferedBytes': len(inBytes), 'max': MAX_NOISE_IN_BYTES}), 'noise handler inBytes buffer exceeded cap, clearing')
                inBytes = b''
                pendingOnFrame = None
                return
            if not isWaitingForTransport:
                await processData(onFrame)

        previous = decodeChain

        async def chained():
            if previous is not None:
                await previous
            return await run()

        result = asyncio.ensure_future(chained())

        async def swallow():
            try:
                await result
            except BaseException:
                pass

        decodeChain = asyncio.ensure_future(swallow())
        return result

    def destroy():
        nonlocal destroyed, inBytes, pendingOnFrame, transport
        destroyed = True
        inBytes = b''
        pendingOnFrame = None
        transport = None

    return JSObject({
        'encrypt': encrypt,
        'decrypt': decrypt,
        'authenticate': authenticate,
        'mixIntoKey': mixIntoKey,
        'finishInit': finishInit,
        'processHandshake': processHandshake,
        'encodeFrame': encodeFrame,
        'decodeFrame': decodeFrame,
        'destroy': destroy
    })


def _optionalAttr(value, *names):
    current = value
    for name in names:
        if current is None:
            return None
        current = current[name] if isinstance(current, dict) else getattr(current, name, None)
    return current


WA_ADV_ACCOUNT_SIG_PREFIX = bytes([6, 0])
WA_ADV_DEVICE_SIG_PREFIX = bytes([6, 1])
WA_ADV_HOSTED_ACCOUNT_SIG_PREFIX = bytes([6, 5])


def getUserAgent(config):
    return JSObject({
        'appVersion': JSObject({'primary': config.get('version')[0], 'secondary': config.get('version')[1], 'tertiary': config.get('version')[2]}),
        'platform': proto.ClientPayload.UserAgent.Platform.ANDROID
        if 'android' in config.get('browser')[1].lower()
        else proto.ClientPayload.UserAgent.Platform.WEB,
        'releaseChannel': proto.ClientPayload.UserAgent.ReleaseChannel.RELEASE,
        'osVersion': '0.1',
        'device': 'Desktop',
        'osBuildNumber': '0.1',
        'localeLanguageIso6391': 'en',
        'mnc': '000',
        'mcc': '000',
        'localeCountryIso31661Alpha2': config.get('countryCode')
    })


WEB_PLATFORM_MAP = JSObject({
    'Mac OS': proto.ClientPayload.WebInfo.WebSubPlatform.DARWIN,
    'Windows': proto.ClientPayload.WebInfo.WebSubPlatform.WIN32
})


def getWebInfo(config):
    webSubPlatform = proto.ClientPayload.WebInfo.WebSubPlatform.WEB_BROWSER
    if _truthy(config.get('syncFullHistory')) and WEB_PLATFORM_MAP[config.get('browser')[0]] is not None and config.get('browser')[1] == 'Desktop':
        webSubPlatform = WEB_PLATFORM_MAP[config.get('browser')[0]]
    return JSObject({'webSubPlatform': webSubPlatform})


def getClientPayload(config):
    payload = JSObject({
        'connectType': proto.ClientPayload.ConnectType.WIFI_UNKNOWN,
        'connectReason': proto.ClientPayload.ConnectReason.USER_ACTIVATED,
        'userAgent': getUserAgent(config)
    })
    if 'android' not in config.get('browser')[1].lower():
        payload['webInfo'] = getWebInfo(config)
    if _truthy(config.get('pushName')):
        payload['pushName'] = config.get('pushName')
    return payload


def generateLoginNode(userJid, config):
    decoded = jidDecode(userJid)
    payload = JSObject({**getClientPayload(config), 'passive': True, 'pull': True, 'username': _plus(decoded.get('user')), 'device': decoded.get('device'), 'lidDbMigrated': False})
    return proto.ClientPayload.fromObject(payload)


def getPlatformType(platform):
    platformType = platform.upper()
    if platformType == 'ANDROID':
        return proto.DeviceProps.PlatformType.ANDROID_PHONE
    return _or(proto.DeviceProps.PlatformType[platformType], proto.DeviceProps.PlatformType.CHROME)


def generateRegistrationNode(keys, config):
    registrationId = keys.get('registrationId')
    signedPreKey = keys.get('signedPreKey')
    signedIdentityKey = keys.get('signedIdentityKey')
    appVersionBuf = createHash('md5').update('.'.join(str(v) for v in config.get('version')).encode('utf-8')).digest()
    companion = JSObject({
        'os': config.get('browser')[0],
        'platformType': getPlatformType(config.get('browser')[1]),
        'requireFullSync': config.get('syncFullHistory'),
        'historySyncConfig': JSObject({
            'storageQuotaMb': 10240,
            'inlineInitialPayloadInE2EeMsg': True,
            'recentSyncDaysLimit': None,
            'supportCallLogHistory': False,
            'supportBotUserAgentChatHistory': True,
            'supportCagReactionsAndPolls': True,
            'supportBizHostedMsg': True,
            'supportRecentSyncChunkMessageCountTuning': True,
            'supportHostedGroupMsg': True,
            'supportFbidBotChatHistory': True,
            'supportAddOnHistorySyncMigration': None,
            'supportMessageAssociation': True,
            'supportGroupHistory': False,
            'onDemandReady': None,
            'supportGuestChat': None
        }),
        'version': JSObject({'primary': 10, 'secondary': 15, 'tertiary': 7})
    })
    companionProto = proto.DeviceProps.encode(companion).finish()
    registerPayload = JSObject({
        **getClientPayload(config),
        'passive': False,
        'pull': False,
        'devicePairingData': JSObject({
            'buildHash': appVersionBuf,
            'deviceProps': companionProto,
            'eRegid': encodeBigEndian(registrationId),
            'eKeytype': KEY_BUNDLE_TYPE,
            'eIdent': signedIdentityKey.get('public'),
            'eSkeyId': encodeBigEndian(signedPreKey.get('keyId'), 3),
            'eSkeyVal': signedPreKey.get('keyPair').get('public'),
            'eSkeySig': signedPreKey.get('signature')
        })
    })
    return proto.ClientPayload.fromObject(registerPayload)


def encodeSignedDeviceIdentity(account, includeSignatureKey):
    account = JSObject({**account})
    key = account.get('accountSignatureKey')
    if not includeSignatureKey or key is None or len(key) == 0:
        account['accountSignatureKey'] = None
    return proto.ADVSignedDeviceIdentity.encode(account).finish()


def configureSuccessfulPairing(stanza, params):
    advSecretKey = params.get('advSecretKey')
    signedIdentityKey = params.get('signedIdentityKey')
    signalIdentities = params.get('signalIdentities')
    msgId = stanza.get('attrs').get('id')
    pairSuccessNode = getBinaryNodeChild(stanza, 'pair-success')
    deviceIdentityNode = getBinaryNodeChild(pairSuccessNode, 'device-identity')
    platformNode = getBinaryNodeChild(pairSuccessNode, 'platform')
    deviceNode = getBinaryNodeChild(pairSuccessNode, 'device')
    businessNode = getBinaryNodeChild(pairSuccessNode, 'biz')

    if deviceIdentityNode is None or deviceNode is None:
        raise Boom('Missing device-identity or device in pair success node', JSObject({'data': stanza}))

    bizName = businessNode.get('attrs').get('name') if businessNode is not None else None
    jid = deviceNode.get('attrs').get('jid')
    lid = deviceNode.get('attrs').get('lid')
    decodedHmac = proto.ADVSignedDeviceIdentityHMAC.decode(deviceIdentityNode.get('content'))
    details = decodedHmac.get('details')
    hmac = decodedHmac.get('hmac')
    accountType = decodedHmac.get('accountType')

    hmacPrefix = b''
    if accountType is not None and accountType == proto.ADVEncryptionType.HOSTED:
        hmacPrefix = WA_ADV_HOSTED_ACCOUNT_SIG_PREFIX

    advSign = hmacSign(hmacPrefix + bytes(details), base64.b64decode(advSecretKey))
    if bytes(hmac or b'') != advSign:
        raise Boom('Invalid account signature')

    account = proto.ADVSignedDeviceIdentity.decode(details)
    accountSignatureKey = account.get('accountSignatureKey')
    accountSignature = account.get('accountSignature')
    deviceDetails = account.get('details')
    deviceIdentity = proto.ADVDeviceIdentity.decode(deviceDetails)
    accountSignaturePrefix = (
        WA_ADV_HOSTED_ACCOUNT_SIG_PREFIX
        if deviceIdentity.get('deviceType') == proto.ADVEncryptionType.HOSTED
        else WA_ADV_ACCOUNT_SIG_PREFIX
    )
    accountMsg = accountSignaturePrefix + bytes(deviceDetails) + signedIdentityKey.get('public')
    if not Curve['verify'](accountSignatureKey, accountMsg, accountSignature):
        raise Boom('Failed to verify account signature')

    deviceMsg = WA_ADV_DEVICE_SIG_PREFIX + bytes(deviceDetails) + signedIdentityKey.get('public') + bytes(accountSignatureKey)
    account['deviceSignature'] = Curve['sign'](signedIdentityKey.get('private'), deviceMsg)
    identity = createSignalIdentity(lid, accountSignatureKey)
    accountEnc = encodeSignedDeviceIdentity(account, False)

    reply = JSObject({
        'tag': 'iq',
        'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'result', 'id': msgId}),
        'content': [
            JSObject({
                'tag': 'pair-device-sign',
                'attrs': JSObject({}),
                'content': [JSObject({
                    'tag': 'device-identity',
                    'attrs': JSObject({'key-index': _numStr(deviceIdentity.get('keyIndex'))}),
                    'content': accountEnc
                })]
            })
        ]
    })
    authUpdate = JSObject({
        'account': account,
        'me': JSObject({'id': jid, 'name': bizName, 'lid': lid}),
        'signalIdentities': [*(signalIdentities or []), identity],
        'platform': platformNode.get('attrs').get('name') if platformNode is not None else None
    })
    return JSObject({'creds': authUpdate, 'reply': reply})


REPORTING_FIELDS = [
	{'f': 1 },
	{'f': 3, 's': [{'f': 2 }, {'f': 3 }, {'f': 8 }, {'f': 11 }, {'f': 17, 's': [{'f': 21 }, {'f': 22 }] }, {'f': 25 }] },
	{'f': 4, 's': [{'f': 1 }, {'f': 16 }, {'f': 17, 's': [{'f': 21 }, {'f': 22 }] }] },
	{'f': 5, 's': [{'f': 3 }, {'f': 4 }, {'f': 5 }, {'f': 16 }, {'f': 17, 's': [{'f': 21 }, {'f': 22 }] }] },
	{'f': 6, 's': [{'f': 1 }, {'f': 17, 's': [{'f': 21 }, {'f': 22 }] }, {'f': 30 }] },
	{'f': 7, 's': [{'f': 2 }, {'f': 7 }, {'f': 10 }, {'f': 17, 's': [{'f': 21 }, {'f': 22 }] }, {'f': 20 }] },
	{'f': 8, 's': [{'f': 2 }, {'f': 7 }, {'f': 9 }, {'f': 17, 's': [{'f': 21 }, {'f': 22 }] }, {'f': 21 }] },
	{'f': 9, 's': [{'f': 2 }, {'f': 6 }, {'f': 7 }, {'f': 13 }, {'f': 17, 's': [{'f': 21 }, {'f': 22 }] }, {'f': 20 }] },
	{'f': 12, 's': [{'f': 1 }, {'f': 2 }, {'f': 14, 'm': True }, {'f': 15 }] },
	{'f': 18, 's': [{'f': 6 }, {'f': 16 }, {'f': 17, 's': [{'f': 21 }, {'f': 22 }] }] },
	{'f': 26, 's': [{'f': 4 }, {'f': 5 }, {'f': 8 }, {'f': 13 }, {'f': 17, 's': [{'f': 21 }, {'f': 22 }] }] },
	{'f': 28, 's': [{'f': 1 }, {'f': 2 }, {'f': 4 }, {'f': 5 }, {'f': 6 }, {'f': 7, 's': [{'f': 21 }, {'f': 22 }] }] },
	{'f': 37, 's': [{'f': 1, 'm': True }] },
	{'f': 49, 's': [{'f': 2 }, {'f': 3, 's': [{'f': 1 }, {'f': 2 }] }, {'f': 5, 's': [{'f': 21 }, {'f': 22 }] }, {'f': 8, 's': [{'f': 1 }, {'f': 2 }] }] },
	{'f': 53, 's': [{'f': 1, 'm': True }] },
	{'f': 55, 's': [{'f': 1, 'm': True }] },
	{'f': 58, 's': [{'f': 1, 'm': True }] },
	{'f': 59, 's': [{'f': 1, 'm': True }] },
	{'f': 60, 's': [{'f': 2 }, {'f': 3, 's': [{'f': 1 }, {'f': 2 }] }, {'f': 5, 's': [{'f': 21 }, {'f': 22 }] }, {'f': 8, 's': [{'f': 1 }, {'f': 2 }] }] },
	{'f': 64, 's': [{'f': 2 }, {'f': 3, 's': [{'f': 1 }, {'f': 2 }] }, {'f': 5, 's': [{'f': 21 }, {'f': 22 }] }, {'f': 8, 's': [{'f': 1 }, {'f': 2 }] }] },
	{'f': 66, 's': [{'f': 2 }, {'f': 6 }, {'f': 7 }, {'f': 13 }, {'f': 17, 's': [{'f': 21 }, {'f': 22 }] }, {'f': 20 }] },
	{'f': 74, 's': [{'f': 1, 'm': True }] },
	{'f': 87, 's': [{'f': 1, 'm': True }] },
	{'f': 88, 's': [{'f': 1 }, {'f': 2, 's': [{'f': 1 }] }, {'f': 3, 's': [{'f': 21 }, {'f': 22 }] }] },
	{'f': 92, 's': [{'f': 1, 'm': True }] },
	{'f': 93, 's': [{'f': 1, 'm': True }] },
	{'f': 94, 's': [{'f': 1, 'm': True }] }
]


def _compileReportingFields(fields):
    result = {}
    for f in fields:
        result[f.get('f')] = {'m': f.get('m'), 'children': _compileReportingFields(f.get('s')) if 's' in f else None}
    return result


compiledReportingFields = _compileReportingFields(REPORTING_FIELDS)
EMPTY_MAP = {}
ENC_SECRET_REPORT_TOKEN = 'Report Token'
WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_BYTES = 2
WIRE_FIXED32 = 5


def shouldIncludeReportingToken(message):
    return not (message.get('reactionMessage') or message.get('encReactionMessage') or message.get('encEventResponseMessage') or message.get('pollUpdateMessage'))


def _int32(value):
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= (1 << 31) else value


def generateMsgSecretKey(modificationType, origMsgId, origMsgSender, modificationSender, origMsgSecret):
    useCaseSecret = (
        origMsgId.encode('utf-8')
        + origMsgSender.encode('utf-8')
        + modificationSender.encode('utf-8')
        + modificationType.encode('utf-8')
    )
    return hkdf(origMsgSecret, 32, JSObject({'info': useCaseSecret.decode('latin-1')}))


def decodeVarint(buffer, offset):
    value = 0
    count = 0
    shift = 0
    while offset + count < len(buffer):
        current = buffer[offset + count]
        value = _int32(value | _int32((current & 0x7f) << (shift & 31)))
        count += 1
        if (current & 0x80) == 0:
            return JSObject({'value': value, 'bytes': count, 'ok': True})
        shift += 7
        if shift > 35:
            return JSObject({'value': 0, 'bytes': 0, 'ok': False})
    return JSObject({'value': 0, 'bytes': 0, 'ok': False})


def encodeVarint(value):
    parts = []
    remaining = value & 0xFFFFFFFF
    while remaining > 0x7f:
        parts.append((remaining & 0x7f) | 0x80)
        remaining >>= 7
    parts.append(remaining)
    return bytes(parts)


def extractReportingTokenContent(data, cfg):
    out = []
    i = 0
    while i < len(data):
        tag = decodeVarint(data, i)
        if not tag.get('ok'):
            return None
        tagValue = tag.get('value')
        fieldNum = tagValue >> 3
        wireType = tagValue & 0x7
        fieldStart = i
        i += tag.get('bytes')
        fieldCfg = cfg.get(fieldNum)

        if wireType == WIRE_VARINT:
            v = decodeVarint(data, i)
            if not v.get('ok'):
                return None
            end = i + v.get('bytes')
        elif wireType == WIRE_FIXED64:
            end = i + 8
        elif wireType == WIRE_FIXED32:
            end = i + 4
        elif wireType == WIRE_BYTES:
            ln = decodeVarint(data, i)
            if not ln.get('ok'):
                return None
            valStart = i + ln.get('bytes')
            valEnd = valStart + ln.get('value')
            if valEnd > len(data):
                return None
            if fieldCfg is None:
                i = valEnd
                continue
            if fieldCfg.get('m') or fieldCfg.get('children') is not None:
                children = fieldCfg.get('children') if fieldCfg.get('children') is not None else EMPTY_MAP
                sub = extractReportingTokenContent(data[valStart:valEnd], children)
                if sub is None:
                    return None
                if len(sub) > 0:
                    out.append((fieldNum, encodeVarint(tagValue) + encodeVarint(len(sub)) + sub))
                i = valEnd
                continue
            out.append((fieldNum, data[fieldStart:valEnd]))
            i = valEnd
            continue
        else:
            return None

        if end > len(data):
            return None
        if fieldCfg is not None:
            out.append((fieldNum, data[fieldStart:end]))
        i = end

    if len(out) == 0:
        return b''
    out.sort(key=lambda f: f[0])
    return b''.join(f[1] for f in out)


async def getMessageReportingToken(msgProtobuf, message, key):
    msgSecret = message.get('messageContextInfo').get('messageSecret') if message.get('messageContextInfo') is not None else None
    if msgSecret is None or not _truthy(key.get('id')):
        return None
    from_ = key.get('remoteJid') if key.get('fromMe') else _or(key.get('participant'), key.get('remoteJid'))
    to = _or(key.get('participant'), key.get('remoteJid')) if key.get('fromMe') else key.get('remoteJid')
    reportingSecret = generateMsgSecretKey(ENC_SECRET_REPORT_TOKEN, key.get('id'), from_, to, msgSecret)
    content = extractReportingTokenContent(bytes(msgProtobuf), compiledReportingFields)
    if content is None or len(content) == 0:
        return None
    reportingToken = createHmac('sha256', reportingSecret).update(content).digest()[:16]
    return JSObject({
        'tag': 'reporting',
        'attrs': JSObject({}),
        'content': [JSObject({'tag': 'reporting_token', 'attrs': JSObject({'v': '2'}), 'content': reportingToken})]
    })
