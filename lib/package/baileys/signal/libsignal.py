import asyncio
import base64
import re
import sys
from baileys.foundation import node_crypto as nodeCrypto
from baileys.foundation.curve25519 import publicFromPrivate, randomX25519, sharedKey, sign as xeddsaSign, verify as xeddsaVerify
from baileys.foundation.node_runtime import JSError, JSObject, now


class SignalError(JSError):
    def __init__(self, message='', **props):
        super().__init__(message, **props)
        self.name = 'Error'


class UntrustedIdentityKeyError(SignalError):
    def __init__(self, addr, identityKey):
        super().__init__('')
        self.name = 'UntrustedIdentityKeyError'
        self.addr = addr
        self.identityKey = identityKey


class SessionError(SignalError):
    def __init__(self, message=''):
        super().__init__(message)
        self.name = 'SessionError'


class MessageCounterError(SessionError):
    def __init__(self, message=''):
        super().__init__(message)
        self.name = 'MessageCounterError'


class PreKeyError(SessionError):
    def __init__(self, message=''):
        super().__init__(message)
        self.name = 'PreKeyError'


def isBufferValue(value):
    return isinstance(value, (bytes, bytearray))


def assertBuffer(value):
    if not isBufferValue(value):
        raise TypeError(f'Expected Buffer instead of: {type(value).__name__}')
    return value


def encrypt(key, data, iv):
    assertBuffer(key)
    assertBuffer(data)
    assertBuffer(iv)
    cipher = nodeCrypto.createCipheriv('aes-256-cbc', bytes(key), bytes(iv))
    return b''.join([cipher.update(bytes(data)), cipher.final()])


def decrypt(key, data, iv):
    assertBuffer(key)
    assertBuffer(data)
    assertBuffer(iv)
    decipher = nodeCrypto.createDecipheriv('aes-256-cbc', bytes(key), bytes(iv))
    return b''.join([decipher.update(bytes(data)), decipher.final()])


def calculateMAC(key, data):
    assertBuffer(key)
    assertBuffer(data)
    return bytes(nodeCrypto.createHmac('sha256', bytes(key)).update(bytes(data)).digest())


def hash(data):
    assertBuffer(data)
    return nodeCrypto.createHash('sha512').update(bytes(data)).digest()


def deriveSecrets(input, salt, info, chunks=None):
    assertBuffer(input)
    assertBuffer(salt)
    assertBuffer(info)
    if len(salt) != 32:
        raise JSError('Got salt of incorrect length')
    if not chunks:
        chunks = 3
    if not (chunks >= 1 and chunks <= 3):
        raise JSError('chunks must be between 1 and 3')
    PRK = calculateMAC(salt, input)
    infoArray = bytearray(len(info) + 1 + 32)
    infoArray[32:32 + len(info)] = bytes(info)
    infoArray[len(infoArray) - 1] = 1
    signed = [calculateMAC(PRK, bytes(infoArray[32:]))]
    if chunks > 1:
        infoArray[0:32] = signed[len(signed) - 1]
        infoArray[len(infoArray) - 1] = 2
        signed.append(calculateMAC(PRK, bytes(infoArray)))
    if chunks > 2:
        infoArray[0:32] = signed[len(signed) - 1]
        infoArray[len(infoArray) - 1] = 3
        signed.append(calculateMAC(PRK, bytes(infoArray)))
    return signed


def verifyMAC(data, key, mac, length):
    calculatedMac = calculateMAC(key, data)[:length]
    if len(mac) != length or len(calculatedMac) != length:
        raise JSError('Bad MAC length')
    if bytes(mac) != calculatedMac:
        raise JSError('Bad MAC')


crypto = JSObject({
    'encrypt': encrypt,
    'decrypt': decrypt,
    'calculateMAC': calculateMAC,
    'hash': hash,
    'deriveSecrets': deriveSecrets,
    'verifyMAC': verifyMAC
})

KEY_BUNDLE_TYPE = bytes([5])


def prefixKeyInPublicKey(pubKey):
    return KEY_BUNDLE_TYPE + bytes(pubKey)


def validatePrivKey(privKey):
    if privKey is None:
        raise JSError('Undefined private key')
    if not isBufferValue(privKey):
        raise JSError(f'Invalid private key type: {type(privKey).__name__}')
    if len(privKey) != 32:
        raise JSError(f'Incorrect private key length: {len(privKey)}')


def scrubPubKeyFormat(pubKey):
    if not isBufferValue(pubKey):
        raise JSError(f'Invalid public key type: {type(pubKey).__name__}')
    if pubKey is None or ((len(pubKey) != 33 or pubKey[0] != 5) and len(pubKey) != 32):
        raise JSError('Invalid public key')
    if len(pubKey) == 33:
        return bytes(pubKey[1:])
    sys.stderr.write('WARNING: Expected pubkey of length 33, please report the ST and client that generated the pubkey\n')
    return bytes(pubKey)


def unclampEd25519PrivateKey(clampedSk):
    unclampedSk = bytearray(bytes(clampedSk))
    unclampedSk[0] |= 6
    unclampedSk[31] |= 128
    unclampedSk[31] &= 0xBF
    return unclampedSk


def getPublicFromPrivateKey(privKey):
    return prefixKeyInPublicKey(publicFromPrivate(bytes(unclampEd25519PrivateKey(privKey))))


def generateKeyPair():
    pair = randomX25519()
    return JSObject({'pubKey': prefixKeyInPublicKey(pair.get('pub')), 'privKey': pair.get('priv')})


def calculateAgreement(pubKey, privKey):
    pubKey = scrubPubKeyFormat(pubKey)
    validatePrivKey(privKey)
    if pubKey is None or len(pubKey) != 32:
        raise JSError('Invalid public key')
    return sharedKey(bytes(privKey), bytes(pubKey))


def calculateSignature(privKey, message):
    validatePrivKey(privKey)
    if not message:
        raise JSError('Invalid message')
    return bytes(xeddsaSign(bytes(privKey), bytes(message)))


def verifySignature(pubKey, msg, sig, isInit=False):
    pubKey = scrubPubKeyFormat(pubKey)
    if pubKey is None or len(pubKey) != 32:
        raise JSError('Invalid public key')
    if not msg:
        raise JSError('Invalid message')
    if sig is None or len(sig) != 64:
        raise JSError('Invalid signature')
    return True if isInit else xeddsaVerify(pubKey, bytes(msg), bytes(sig))


curve = JSObject({
    'getPublicFromPrivateKey': getPublicFromPrivateKey,
    'generateKeyPair': generateKeyPair,
    'calculateAgreement': calculateAgreement,
    'calculateSignature': calculateSignature,
    'verifySignature': verifySignature
})


def isNonNegativeInteger(n):
    return isinstance(n, int) and not isinstance(n, bool) and n >= 0


def generateRegistrationId():
    registrationId = nodeCrypto.randomBytes(2)[0]
    return registrationId & 0x3fff


def generateSignedPreKey(identityKeyPair, signedKeyId):
    if (
        not isBufferValue(identityKeyPair.get('privKey'))
        or len(identityKeyPair.get('privKey')) != 32
        or not isBufferValue(identityKeyPair.get('pubKey'))
        or len(identityKeyPair.get('pubKey')) != 33
    ):
        raise TypeError('Invalid argument for identityKeyPair')
    if not isNonNegativeInteger(signedKeyId):
        raise TypeError('Invalid argument for signedKeyId: ' + str(signedKeyId))
    keyPair = generateKeyPair()
    sig = calculateSignature(identityKeyPair.get('privKey'), keyPair.get('pubKey'))
    return JSObject({'keyId': signedKeyId, 'keyPair': keyPair, 'signature': sig})


def generatePreKey(keyId):
    if not isNonNegativeInteger(keyId):
        raise TypeError('Invalid argument for keyId: ' + str(keyId))
    return JSObject({'keyId': keyId, 'keyPair': generateKeyPair()})


keyhelper = JSObject({
    'generateIdentityKeyPair': generateKeyPair,
    'generateRegistrationId': generateRegistrationId,
    'generateSignedPreKey': generateSignedPreKey,
    'generatePreKey': generatePreKey
})


def writeVarint(out, n):
    n = n & 0xFFFFFFFF
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)


class WriterResult:
    def __init__(self, data):
        self.data = data

    def finish(self):
        return self.data


class PbReader:
    def __init__(self, buf):
        self.buf = buf
        self.pos = 0
        self.len = len(buf)

    def varint(self):
        value = 0
        for i in range(5):
            if self.pos >= self.len:
                raise JSError(f'index out of range: {self.pos} + 1 > {self.len}', name='RangeError')
            b = self.buf[self.pos]
            self.pos += 1
            value = (value | ((b & 127) << (7 * i))) & 0xFFFFFFFF
            if b < 128:
                return value
        for i in range(5, 10):
            if self.pos >= self.len:
                raise JSError(f'index out of range: {self.pos} + 1 > {self.len}', name='RangeError')
            b = self.buf[self.pos]
            self.pos += 1
            if b < 128:
                return value
        raise JSError('invalid varint encoding')

    def bytes(self):
        length = self.varint()
        start = self.pos
        end = start + length
        if end > self.len:
            raise JSError(f'index out of range: {self.pos} + {length} > {self.len}', name='RangeError')
        self.pos = end
        return bytes(self.buf[start:end])

    def skipType(self, wireType):
        if wireType == 0:
            self.varint()
        elif wireType == 1:
            self.pos += 8
        elif wireType == 2:
            skip = self.varint()
            self.pos += skip
        elif wireType == 5:
            self.pos += 4
        else:
            raise JSError('invalid wire type ' + str(wireType) + ' at offset ' + str(self.pos))
        if self.pos > self.len:
            raise JSError('index out of range', name='RangeError')


class WireMessage(JSObject):
    __slots__ = ()
    FIELDS = ()
    DEFAULTS = {}

    def __init__(self, props=None):
        super().__init__()
        if props:
            for k in list(dict.keys(props)):
                if props[k] is not None:
                    self[k] = props[k]

    def __missing__(self, key):
        return type(self).DEFAULTS.get(key)

    @classmethod
    def create(cls, props=None):
        return cls(props)

    @classmethod
    def encode(cls, message):
        out = []
        for f in cls.FIELDS:
            name = f.get('name')
            if name in message and message[name] is not None:
                if f.get('type') == 'uint32':
                    writeVarint(out, (f.get('no') << 3) | 0)
                    writeVarint(out, message[name])
                else:
                    writeVarint(out, (f.get('no') << 3) | 2)
                    b = message[name]
                    writeVarint(out, len(b))
                    out.extend(bytes(b))
        return WriterResult(bytes(out))

    @classmethod
    def decode(cls, data):
        reader = PbReader(data)
        message = cls()
        byNo = {f.get('no'): f for f in cls.FIELDS}
        while reader.pos < reader.len:
            tag = reader.varint()
            f = byNo.get(tag >> 3)
            if f is not None:
                message[f.get('name')] = reader.varint() if f.get('type') == 'uint32' else reader.bytes()
            else:
                reader.skipType(tag & 7)
        return message


def defineMessage(fields, defaults):
    return type('WireMessage', (WireMessage,), {'FIELDS': fields, 'DEFAULTS': defaults})


WhisperMessage = defineMessage(
    [
        {'name': 'ephemeralKey', 'no': 1, 'type': 'bytes'},
        {'name': 'counter', 'no': 2, 'type': 'uint32'},
        {'name': 'previousCounter', 'no': 3, 'type': 'uint32'},
        {'name': 'ciphertext', 'no': 4, 'type': 'bytes'}
    ],
    {'ephemeralKey': b'', 'counter': 0, 'previousCounter': 0, 'ciphertext': b''}
)

PreKeyWhisperMessage = defineMessage(
    [
        {'name': 'preKeyId', 'no': 1, 'type': 'uint32'},
        {'name': 'baseKey', 'no': 2, 'type': 'bytes'},
        {'name': 'identityKey', 'no': 3, 'type': 'bytes'},
        {'name': 'message', 'no': 4, 'type': 'bytes'},
        {'name': 'registrationId', 'no': 5, 'type': 'uint32'},
        {'name': 'signedPreKeyId', 'no': 6, 'type': 'uint32'}
    ],
    {
        'preKeyId': 0,
        'baseKey': b'',
        'identityKey': b'',
        'message': b'',
        'registrationId': 0,
        'signedPreKeyId': 0
    }
)


def parseIntPrefix(text):
    match = re.match(r'\s*([+-]?[0-9]+)', text)
    if match is None:
        return float('nan')
    return int(match.group(1))


class ProtocolAddress:
    @classmethod
    def from_(cls, encodedAddress):
        if not isinstance(encodedAddress, str) or not re.match(r'.*\.[0-9]+', encodedAddress):
            raise JSError('Invalid address encoding')
        parts = encodedAddress.split('.')
        return cls(parts[0], parseIntPrefix(parts[1]))

    def __init__(self, id, deviceId):
        if not isinstance(id, str):
            raise TypeError('id required for addr')
        if id.find('.') != -1:
            raise TypeError('encoded addr detected')
        self.id = id
        if not isinstance(deviceId, (int, float)) or isinstance(deviceId, bool):
            raise TypeError('number required for deviceId')
        self.deviceId = deviceId

    def toString(self):
        return f'{self.id}.{self.deviceId}'

    def is_(self, other):
        if not isinstance(other, ProtocolAddress):
            return False
        return other.id == self.id and other.deviceId == self.deviceId


_queueAsyncBuckets = {}
_gcLimit = 10000


async def _asyncQueueExecutor(queue, cleanup):
    offt = 0
    while True:
        limit = min(len(queue), _gcLimit)
        for i in range(offt, limit):
            job = queue[i]
            try:
                result = await job.get('awaitable')()
            except Exception as e:
                if not job.get('future').done():
                    job.get('future').set_exception(e)
            else:
                if not job.get('future').done():
                    job.get('future').set_result(result)
        if limit < len(queue):
            if limit >= _gcLimit:
                del queue[0:limit]
                offt = 0
            else:
                offt = limit
        else:
            break
    cleanup()


async def queueJob(bucket, awaitable):
    loop = asyncio.get_running_loop()
    inactive = False
    if bucket not in _queueAsyncBuckets:
        _queueAsyncBuckets[bucket] = []
        inactive = True
    queue = _queueAsyncBuckets[bucket]
    future = loop.create_future()
    queue.append(JSObject({'awaitable': awaitable, 'future': future}))
    if inactive:
        asyncio.ensure_future(_asyncQueueExecutor(queue, lambda: _queueAsyncBuckets.pop(bucket, None)))
    return await future


BaseKeyType = JSObject({'OURS': 1, 'THEIRS': 2})
ChainType = JSObject({'SENDING': 1, 'RECEIVING': 2})
CLOSED_SESSIONS_MAX = 40
SESSION_RECORD_VERSION = 'v1'


def assertBufferType(value):
    if not isBufferValue(value):
        raise TypeError('Buffer required')


def toBase64(buf):
    return base64.b64encode(bytes(buf)).decode('ascii')


def fromBase64(text):
    return base64.b64decode(text)


def asJSObject(value):
    if isinstance(value, dict):
        if not isinstance(value, JSObject):
            return JSObject({k: asJSObject(v) for k, v in dict.items(value)})
        return value
    if isinstance(value, list):
        return [asJSObject(v) for v in value]
    return value


class SessionEntry:
    def __init__(self):
        self._chains = {}
        self.registrationId = None
        self.currentRatchet = None
        self.indexInfo = None
        self.pendingPreKey = None

    def toString(self):
        baseKey = None
        if self.indexInfo is not None and self.indexInfo.get('baseKey') is not None:
            baseKey = toBase64(self.indexInfo.get('baseKey'))
        return f'<SessionEntry [baseKey={"undefined" if baseKey is None else baseKey}]>'

    def inspect(self):
        return self.toString()

    def addChain(self, key, value):
        assertBufferType(key)
        id = toBase64(key)
        if id in self._chains:
            raise JSError('Overwrite attempt')
        self._chains[id] = value

    def getChain(self, key):
        assertBufferType(key)
        return self._chains.get(toBase64(key))

    def deleteChain(self, key):
        assertBufferType(key)
        id = toBase64(key)
        if id not in self._chains:
            raise JSError('Not Found', name='ReferenceError')
        del self._chains[id]

    def chains(self):
        for k, v in list(self._chains.items()):
            yield [fromBase64(k), v]

    def serialize(self):
        data = JSObject({
            'registrationId': self.registrationId,
            'currentRatchet': JSObject({
                'ephemeralKeyPair': JSObject({
                    'pubKey': toBase64(self.currentRatchet.get('ephemeralKeyPair').get('pubKey')),
                    'privKey': toBase64(self.currentRatchet.get('ephemeralKeyPair').get('privKey'))
                }),
                'lastRemoteEphemeralKey': toBase64(self.currentRatchet.get('lastRemoteEphemeralKey')),
                'previousCounter': self.currentRatchet.get('previousCounter'),
                'rootKey': toBase64(self.currentRatchet.get('rootKey'))
            }),
            'indexInfo': JSObject({
                'baseKey': toBase64(self.indexInfo.get('baseKey')),
                'baseKeyType': self.indexInfo.get('baseKeyType'),
                'closed': self.indexInfo.get('closed'),
                'used': self.indexInfo.get('used'),
                'created': self.indexInfo.get('created'),
                'remoteIdentityKey': toBase64(self.indexInfo.get('remoteIdentityKey'))
            }),
            '_chains': self._serialize_chains(self._chains)
        })
        if self.pendingPreKey:
            data['pendingPreKey'] = JSObject(dict(self.pendingPreKey))
            data.get('pendingPreKey')['baseKey'] = toBase64(self.pendingPreKey.get('baseKey'))
        return data

    @classmethod
    def deserialize(cls, data):
        data = asJSObject(data)
        obj = cls()
        obj.registrationId = data.get('registrationId')
        obj.currentRatchet = JSObject({
            'ephemeralKeyPair': JSObject({
                'pubKey': fromBase64(data.get('currentRatchet').get('ephemeralKeyPair').get('pubKey')),
                'privKey': fromBase64(data.get('currentRatchet').get('ephemeralKeyPair').get('privKey'))
            }),
            'lastRemoteEphemeralKey': fromBase64(data.get('currentRatchet').get('lastRemoteEphemeralKey')),
            'previousCounter': data.get('currentRatchet').get('previousCounter'),
            'rootKey': fromBase64(data.get('currentRatchet').get('rootKey'))
        })
        obj.indexInfo = JSObject({
            'baseKey': fromBase64(data.get('indexInfo').get('baseKey')),
            'baseKeyType': data.get('indexInfo').get('baseKeyType'),
            'closed': data.get('indexInfo').get('closed'),
            'used': data.get('indexInfo').get('used'),
            'created': data.get('indexInfo').get('created'),
            'remoteIdentityKey': fromBase64(data.get('indexInfo').get('remoteIdentityKey'))
        })
        obj._chains = cls._deserialize_chains(data.get('_chains'))
        if data.get('pendingPreKey'):
            obj.pendingPreKey = JSObject(dict(data.get('pendingPreKey')))
            obj.pendingPreKey['baseKey'] = fromBase64(data.get('pendingPreKey').get('baseKey'))
        return obj

    def _serialize_chains(self, chains):
        r = {}
        for key in list(chains.keys()):
            c = chains[key]
            messageKeys = {}
            for idx, mk in list(c.get('messageKeys').items()):
                messageKeys[str(idx)] = toBase64(mk)
            chainKey = {'counter': c.get('chainKey').get('counter')}
            if c.get('chainKey').get('key') is not None:
                chainKey['key'] = toBase64(c.get('chainKey').get('key'))
            r[key] = {
                'chainKey': chainKey,
                'chainType': c.get('chainType'),
                'messageKeys': messageKeys
            }
        return r

    @classmethod
    def _deserialize_chains(cls, chains_data):
        r = {}
        for key in list(chains_data.keys()):
            c = chains_data[key]
            messageKeys = {}
            for idx, mk in list(c.get('messageKeys').items()):
                messageKeys[str(idx)] = fromBase64(mk)
            chainKey = JSObject({'counter': c.get('chainKey').get('counter')})
            if c.get('chainKey').get('key') is not None:
                chainKey['key'] = fromBase64(c.get('chainKey').get('key'))
            r[key] = JSObject({
                'chainKey': chainKey,
                'chainType': c.get('chainType'),
                'messageKeys': messageKeys
            })
        return r


def migrateV1(data):
    sessions = data.get('_sessions')
    if data.get('registrationId'):
        for key in list(sessions.keys()):
            if not sessions[key].get('registrationId'):
                sessions[key]['registrationId'] = data.get('registrationId')
    else:
        for key in list(sessions.keys()):
            if sessions[key].get('indexInfo').get('closed') == -1:
                sys.stderr.write(f"V1 session storage migration error: registrationId {data.get('registrationId')} for open session version {data.get('version')}\n")


migrations = [
    JSObject({'version': 'v1', 'migrate': migrateV1})
]


class SessionRecord:
    @staticmethod
    def createEntry():
        return SessionEntry()

    @staticmethod
    def migrate(data):
        run = data.get('version') is None
        for i in range(len(migrations)):
            if run:
                print('Migrating session to:', migrations[i].get('version'))
                migrations[i].get('migrate')(data)
            elif migrations[i].get('version') == data.get('version'):
                run = True
        if not run:
            raise JSError('Error migrating SessionRecord')

    @classmethod
    def deserialize(cls, data):
        data = asJSObject(data)
        if data.get('version') != SESSION_RECORD_VERSION:
            cls.migrate(data)
        obj = cls()
        if data.get('_sessions'):
            for key, entry in list(data.get('_sessions').items()):
                obj.sessions[key] = SessionEntry.deserialize(entry)
        return obj

    def __init__(self):
        self.sessions = {}
        self.version = SESSION_RECORD_VERSION

    def serialize(self):
        _sessions = {}
        for key, entry in list(self.sessions.items()):
            _sessions[key] = entry.serialize()
        return JSObject({'_sessions': _sessions, 'version': self.version})

    def haveOpenSession(self):
        openSession = self.getOpenSession()
        return openSession is not None and isinstance(openSession.registrationId, (int, float)) and not isinstance(openSession.registrationId, bool)

    def getSession(self, key):
        assertBufferType(key)
        session = self.sessions.get(toBase64(key))
        if session is not None and session.indexInfo.get('baseKeyType') == BaseKeyType['OURS']:
            raise JSError('Tried to lookup a session using our basekey')
        return session

    def getOpenSession(self):
        for session in list(self.sessions.values()):
            if not self.isClosed(session):
                return session
        return None

    def setSession(self, session):
        self.sessions[toBase64(session.indexInfo.get('baseKey'))] = session

    def getSessions(self):
        return sorted(list(self.sessions.values()), key=lambda s: s.indexInfo.get('used') or 0, reverse=True)

    def closeSession(self, session):
        if self.isClosed(session):
            sys.stderr.write('Session already closed\n')
            return
        print('Closing session:', session.toString())
        session.indexInfo['closed'] = now()

    def openSession(self, session):
        if not self.isClosed(session):
            sys.stderr.write('Session already open\n')
        print('Opening session:', session.toString())
        session.indexInfo['closed'] = -1

    def isClosed(self, session):
        return session.indexInfo.get('closed') != -1

    def removeOldSessions(self):
        while len(self.sessions) > CLOSED_SESSIONS_MAX:
            oldestKey = None
            oldestSession = None
            for key, session in list(self.sessions.items()):
                if session.indexInfo.get('closed') != -1 and (oldestSession is None or session.indexInfo.get('closed') < oldestSession.indexInfo.get('closed')):
                    oldestKey = key
                    oldestSession = session
            if oldestKey is not None:
                print('Removing old closed session:', oldestSession.toString())
                del self.sessions[oldestKey]
            else:
                raise JSError('Corrupt sessions object')

    def deleteAllSessions(self):
        for key in list(self.sessions.keys()):
            del self.sessions[key]


class SessionBuilder:
    def __init__(self, storage, protocolAddress):
        self.addr = protocolAddress
        self.storage = storage

    async def initOutgoing(self, device):
        fqAddr = self.addr.toString()

        async def job():
            if not await self.storage.get('isTrustedIdentity')(self.addr.id, device.get('identityKey')):
                raise UntrustedIdentityKeyError(self.addr.id, device.get('identityKey'))
            verifySignature(device.get('identityKey'), device.get('signedPreKey').get('publicKey'), device.get('signedPreKey').get('signature'), True)
            baseKey = generateKeyPair()
            devicePreKey = None if device.get('preKey') is None else device.get('preKey').get('publicKey')
            session = await self.initSession(
                True,
                baseKey,
                None,
                device.get('identityKey'),
                devicePreKey,
                device.get('signedPreKey').get('publicKey'),
                device.get('registrationId')
            )
            session.pendingPreKey = JSObject({'signedKeyId': device.get('signedPreKey').get('keyId'), 'baseKey': baseKey.get('pubKey')})
            if device.get('preKey') is not None:
                session.pendingPreKey['preKeyId'] = device.get('preKey').get('keyId')
            record = await self.storage.get('loadSession')(fqAddr)
            if not record:
                record = SessionRecord()
            else:
                openSession = record.getOpenSession()
                if openSession is not None:
                    record.closeSession(openSession)
            record.setSession(session)
            await self.storage.get('storeSession')(fqAddr, record)

        return await queueJob(fqAddr, job)

    async def initIncoming(self, record, message):
        fqAddr = self.addr.toString()
        if not await self.storage.get('isTrustedIdentity')(fqAddr, message.get('identityKey')):
            raise UntrustedIdentityKeyError(self.addr.id, message.get('identityKey'))
        if record.getSession(message.get('baseKey')) is not None:
            return None
        preKeyPair = await self.storage.get('loadPreKey')(message.get('preKeyId'))
        if message.get('preKeyId') and not preKeyPair:
            raise PreKeyError('Invalid PreKey ID')
        signedPreKeyPair = await self.storage.get('loadSignedPreKey')(message.get('signedPreKeyId'))
        if not signedPreKeyPair:
            raise PreKeyError('Missing SignedPreKey')
        existingOpenSession = record.getOpenSession()
        if existingOpenSession is not None:
            sys.stderr.write('Closing open session in favor of incoming prekey bundle\n')
            record.closeSession(existingOpenSession)
        record.setSession(
            await self.initSession(False, preKeyPair, signedPreKeyPair, message.get('identityKey'), message.get('baseKey'), None, message.get('registrationId'))
        )
        return message.get('preKeyId')

    async def initSession(self, isInitiator, ourEphemeralKey, ourSignedKey, theirIdentityPubKey, theirEphemeralPubKey, theirSignedPubKey, registrationId):
        if isInitiator:
            if ourSignedKey is not None:
                raise JSError('Invalid call to initSession')
            ourSignedKey = ourEphemeralKey
        else:
            if theirSignedPubKey is not None:
                raise JSError('Invalid call to initSession')
            theirSignedPubKey = theirEphemeralPubKey
        if ourEphemeralKey is None or theirEphemeralPubKey is None:
            sharedSecret = bytearray(32 * 4)
        else:
            sharedSecret = bytearray(32 * 5)
        for i in range(32):
            sharedSecret[i] = 0xff
        ourIdentityKey = await self.storage.get('getOurIdentity')()
        a1 = calculateAgreement(theirSignedPubKey, ourIdentityKey.get('privKey'))
        a2 = calculateAgreement(theirIdentityPubKey, ourSignedKey.get('privKey'))
        a3 = calculateAgreement(theirSignedPubKey, ourSignedKey.get('privKey'))
        if isInitiator:
            sharedSecret[32:64] = a1
            sharedSecret[64:96] = a2
        else:
            sharedSecret[64:96] = a1
            sharedSecret[32:64] = a2
        sharedSecret[96:128] = a3
        if ourEphemeralKey is not None and theirEphemeralPubKey is not None:
            a4 = calculateAgreement(theirEphemeralPubKey, ourEphemeralKey.get('privKey'))
            sharedSecret[128:160] = a4
        masterKey = deriveSecrets(bytes(sharedSecret), bytes(32), b'WhisperText')
        session = SessionRecord.createEntry()
        session.registrationId = registrationId
        session.currentRatchet = JSObject({
            'rootKey': masterKey[0],
            'ephemeralKeyPair': generateKeyPair() if isInitiator else ourSignedKey,
            'lastRemoteEphemeralKey': theirSignedPubKey,
            'previousCounter': 0
        })
        session.indexInfo = JSObject({
            'created': now(),
            'used': now(),
            'remoteIdentityKey': theirIdentityPubKey,
            'baseKey': ourEphemeralKey.get('pubKey') if isInitiator else theirEphemeralPubKey,
            'baseKeyType': BaseKeyType['OURS'] if isInitiator else BaseKeyType['THEIRS'],
            'closed': -1
        })
        if isInitiator:
            self.calculateSendingRatchet(session, theirSignedPubKey)
        return session

    def calculateSendingRatchet(self, session, remoteKey):
        ratchet = session.currentRatchet
        sharedSecret = calculateAgreement(remoteKey, ratchet.get('ephemeralKeyPair').get('privKey'))
        masterKey = deriveSecrets(sharedSecret, ratchet.get('rootKey'), b'WhisperRatchet')
        session.addChain(ratchet.get('ephemeralKeyPair').get('pubKey'), JSObject({
            'messageKeys': {},
            'chainKey': JSObject({'counter': -1, 'key': masterKey[1]}),
            'chainType': ChainType['SENDING']
        }))
        ratchet['rootKey'] = masterKey[0]


VERSION = 3


class SessionCipher:
    def __init__(self, storage, protocolAddress):
        if not isinstance(protocolAddress, ProtocolAddress):
            raise TypeError('protocolAddress must be a ProtocolAddress')
        self.addr = protocolAddress
        self.storage = storage

    def _encodeTupleByte(self, number1, number2):
        if number1 > 15 or number2 > 15:
            raise TypeError('Numbers must be 4 bits or less')
        return (number1 << 4) | number2

    def _decodeTupleByte(self, byte):
        return [byte >> 4, byte & 0xf]

    def toString(self):
        return f'<SessionCipher({self.addr.toString()})>'

    async def getRecord(self):
        record = await self.storage.get('loadSession')(self.addr.toString())
        if record and not isinstance(record, SessionRecord):
            raise TypeError('SessionRecord type expected from loadSession')
        return record

    async def storeRecord(self, record):
        record.removeOldSessions()
        await self.storage.get('storeSession')(self.addr.toString(), record)

    async def queueJob(self, awaitable):
        return await queueJob(self.addr.toString(), awaitable)

    async def encrypt(self, data):
        assertBuffer(data)
        ourIdentityKey = await self.storage.get('getOurIdentity')()

        async def job():
            record = await self.getRecord()
            if not record:
                raise SessionError('No sessions')
            session = record.getOpenSession()
            if session is None:
                raise SessionError('No open session')
            remoteIdentityKey = session.indexInfo.get('remoteIdentityKey')
            if not await self.storage.get('isTrustedIdentity')(self.addr.id, remoteIdentityKey):
                raise UntrustedIdentityKeyError(self.addr.id, remoteIdentityKey)
            chain = session.getChain(session.currentRatchet.get('ephemeralKeyPair').get('pubKey'))
            if chain.get('chainType') == ChainType['RECEIVING']:
                raise JSError('Tried to encrypt on a receiving chain')
            self.fillMessageKeys(chain, chain.get('chainKey').get('counter') + 1)
            keys = deriveSecrets(chain.get('messageKeys')[str(chain.get('chainKey').get('counter'))], bytes(32), b'WhisperMessageKeys')
            del chain.get('messageKeys')[str(chain.get('chainKey').get('counter'))]
            msg = WhisperMessage.create()
            msg['ephemeralKey'] = session.currentRatchet.get('ephemeralKeyPair').get('pubKey')
            msg['counter'] = chain.get('chainKey').get('counter')
            msg['previousCounter'] = session.currentRatchet.get('previousCounter')
            msg['ciphertext'] = encrypt(keys[0], data, keys[2][:16])
            msgBuf = WhisperMessage.encode(msg).finish()
            macInput = bytearray(len(msgBuf) + 33 * 2 + 1)
            macInput[0:33] = ourIdentityKey.get('pubKey')
            macInput[33:66] = session.indexInfo.get('remoteIdentityKey')
            macInput[66] = self._encodeTupleByte(VERSION, VERSION)
            macInput[67:67 + len(msgBuf)] = msgBuf
            mac = calculateMAC(keys[1], bytes(macInput))
            result = bytearray(len(msgBuf) + 9)
            result[0] = self._encodeTupleByte(VERSION, VERSION)
            result[1:1 + len(msgBuf)] = msgBuf
            result[len(msgBuf) + 1:len(msgBuf) + 9] = mac[:8]
            await self.storeRecord(record)
            if session.pendingPreKey:
                msgType = 3
                preKeyMsg = PreKeyWhisperMessage.create(JSObject({
                    'identityKey': ourIdentityKey.get('pubKey'),
                    'registrationId': await self.storage.get('getOurRegistrationId')(),
                    'baseKey': session.pendingPreKey.get('baseKey'),
                    'signedPreKeyId': session.pendingPreKey.get('signedKeyId'),
                    'message': bytes(result)
                }))
                if session.pendingPreKey.get('preKeyId'):
                    preKeyMsg['preKeyId'] = session.pendingPreKey.get('preKeyId')
                body = bytes([self._encodeTupleByte(VERSION, VERSION)]) + PreKeyWhisperMessage.encode(preKeyMsg).finish()
            else:
                msgType = 1
                body = bytes(result)
            return JSObject({'type': msgType, 'body': body, 'registrationId': session.registrationId})

        return await self.queueJob(job)

    async def decryptWithSessions(self, data, sessions):
        if not sessions:
            raise SessionError('No sessions available')
        errs = []
        for session in sessions:
            try:
                plaintext = await self.doDecryptWhisperMessage(data, session)
                session.indexInfo['used'] = now()
                return JSObject({'session': session, 'plaintext': plaintext})
            except Exception as e:
                errs.append(e)
        sys.stderr.write('Failed to decrypt message with any known session...\n')
        for e in errs:
            sys.stderr.write('Session error:' + str(e) + '\n')
        raise SessionError('No matching sessions found for message')

    async def decryptWhisperMessage(self, data):
        assertBuffer(data)

        async def job():
            record = await self.getRecord()
            if not record:
                raise SessionError('No session record')
            result = await self.decryptWithSessions(data, record.getSessions())
            remoteIdentityKey = result.get('session').indexInfo.get('remoteIdentityKey')
            if not await self.storage.get('isTrustedIdentity')(self.addr.id, remoteIdentityKey):
                raise UntrustedIdentityKeyError(self.addr.id, remoteIdentityKey)
            if record.isClosed(result.get('session')):
                sys.stderr.write('Decrypted message with closed session.\n')
            await self.storeRecord(record)
            return result.get('plaintext')

        return await self.queueJob(job)

    async def decryptPreKeyWhisperMessage(self, data):
        assertBuffer(data)
        versions = self._decodeTupleByte(data[0])
        if versions[1] > 3 or versions[0] < 3:
            raise JSError('Incompatible version number on PreKeyWhisperMessage')

        async def job():
            record = await self.getRecord()
            preKeyProto = PreKeyWhisperMessage.decode(data[1:])
            if not record:
                if preKeyProto.get('registrationId') is None:
                    raise JSError('No registrationId')
                record = SessionRecord()
            builder = SessionBuilder(self.storage, self.addr)
            preKeyId = await builder.initIncoming(record, preKeyProto)
            session = record.getSession(preKeyProto.get('baseKey'))
            plaintext = await self.doDecryptWhisperMessage(preKeyProto.get('message'), session)
            await self.storeRecord(record)
            if preKeyId:
                await self.storage.get('removePreKey')(preKeyId)
            return plaintext

        return await self.queueJob(job)

    async def doDecryptWhisperMessage(self, messageBuffer, session):
        assertBuffer(messageBuffer)
        if session is None:
            raise TypeError('session required')
        versions = self._decodeTupleByte(messageBuffer[0])
        if versions[1] > 3 or versions[0] < 3:
            raise JSError('Incompatible version number on WhisperMessage')
        messageProto = messageBuffer[1:-8]
        message = WhisperMessage.decode(messageProto)
        self.maybeStepRatchet(session, message.get('ephemeralKey'), message.get('previousCounter'))
        chain = session.getChain(message.get('ephemeralKey'))
        if chain.get('chainType') == ChainType['SENDING']:
            raise JSError('Tried to decrypt on a sending chain')
        self.fillMessageKeys(chain, message.get('counter'))
        if str(message.get('counter')) not in chain.get('messageKeys'):
            raise MessageCounterError('Key used already or never filled')
        messageKey = chain.get('messageKeys')[str(message.get('counter'))]
        del chain.get('messageKeys')[str(message.get('counter'))]
        keys = deriveSecrets(messageKey, bytes(32), b'WhisperMessageKeys')
        ourIdentityKey = await self.storage.get('getOurIdentity')()
        macInput = bytearray(len(messageProto) + 33 * 2 + 1)
        macInput[0:33] = session.indexInfo.get('remoteIdentityKey')
        macInput[33:66] = ourIdentityKey.get('pubKey')
        macInput[66] = self._encodeTupleByte(VERSION, VERSION)
        macInput[67:67 + len(messageProto)] = messageProto
        verifyMAC(bytes(macInput), keys[1], messageBuffer[-8:], 8)
        plaintext = decrypt(keys[0], message.get('ciphertext'), keys[2][:16])
        session.pendingPreKey = None
        return plaintext

    def fillMessageKeys(self, chain, counter):
        while chain.get('chainKey').get('counter') < counter:
            if counter - chain.get('chainKey').get('counter') > 2000:
                raise SessionError('Over 2000 messages into the future!')
            if chain.get('chainKey').get('key') is None:
                raise SessionError('Chain closed')
            key = chain.get('chainKey').get('key')
            chain.get('messageKeys')[str(chain.get('chainKey').get('counter') + 1)] = calculateMAC(key, bytes([1]))
            chain.get('chainKey')['key'] = calculateMAC(key, bytes([2]))
            chain.get('chainKey')['counter'] += 1

    def maybeStepRatchet(self, session, remoteKey, previousCounter):
        if session.getChain(remoteKey) is not None:
            return
        ratchet = session.currentRatchet
        previousRatchet = session.getChain(ratchet.get('lastRemoteEphemeralKey'))
        if previousRatchet is not None:
            self.fillMessageKeys(previousRatchet, previousCounter)
            previousRatchet.get('chainKey').pop('key', None)
        self.calculateRatchet(session, remoteKey, False)
        prevCounter = session.getChain(ratchet.get('ephemeralKeyPair').get('pubKey'))
        if prevCounter is not None:
            ratchet['previousCounter'] = prevCounter.get('chainKey').get('counter')
            session.deleteChain(ratchet.get('ephemeralKeyPair').get('pubKey'))
        ratchet['ephemeralKeyPair'] = generateKeyPair()
        self.calculateRatchet(session, remoteKey, True)
        ratchet['lastRemoteEphemeralKey'] = remoteKey

    def calculateRatchet(self, session, remoteKey, sending):
        ratchet = session.currentRatchet
        sharedSecret = calculateAgreement(remoteKey, ratchet.get('ephemeralKeyPair').get('privKey'))
        masterKey = deriveSecrets(sharedSecret, ratchet.get('rootKey'), b'WhisperRatchet', 2)
        chainKey = ratchet.get('ephemeralKeyPair').get('pubKey') if sending else remoteKey
        session.addChain(chainKey, JSObject({
            'messageKeys': {},
            'chainKey': JSObject({'counter': -1, 'key': masterKey[1]}),
            'chainType': ChainType['SENDING'] if sending else ChainType['RECEIVING']
        }))
        ratchet['rootKey'] = masterKey[0]

    async def hasOpenSession(self):
        async def job():
            record = await self.getRecord()
            if not record:
                return False
            return record.haveOpenSession()

        return await self.queueJob(job)

    async def closeOpenSession(self):
        async def job():
            record = await self.getRecord()
            if record:
                openSession = record.getOpenSession()
                if openSession is not None:
                    record.closeSession(openSession)
                    await self.storeRecord(record)

        return await self.queueJob(job)
