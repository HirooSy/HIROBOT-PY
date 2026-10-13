import base64
import json
from baileys_lite.foundation import node_crypto as nodeCrypto
from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.signal.libsignal import crypto as signalCrypto, curve as signalCurve
from baileys_lite.WAProto import proto
from baileys_lite.utils.wa_protocol_core import BufferJSON

deriveSecrets = signalCrypto.get('deriveSecrets')
calculateMAC = signalCrypto.get('calculateMAC')
decrypt = signalCrypto.get('decrypt')
encrypt = signalCrypto.get('encrypt')
calculateSignature = signalCurve.get('calculateSignature')
generateKeyPair = signalCurve.get('generateKeyPair')
verifySignature = signalCurve.get('verifySignature')

_UNSET = object()


def asBuffer(value):
    if value is None:
        return b''
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode('utf-8')
    if isinstance(value, dict):
        if dict.get(value, 'type') == 'Buffer' and isinstance(dict.get(value, 'data'), list):
            return bytes(dict.get(value, 'data'))
        return b''
    return bytes(value)


def isNull(str):
    return str is None or str == ''


def intValue(num):
    MAX_VALUE = 0x7fffffff
    MIN_VALUE = -0x80000000
    if num > MAX_VALUE or num < MIN_VALUE:
        return ((num + 0x80000000) % 0x100000000) - 0x80000000
    return num


def utf16Units(text):
    data = text.encode('utf-16-le', 'surrogatepass')
    return [int.from_bytes(data[i:i + 2], 'little') for i in range(0, len(data), 2)]


def hashCode(strKey):
    hash = 0
    if not isNull(strKey):
        for unit in utf16Units(strKey):
            hash = hash * 31 + unit
            hash = intValue(hash)
    return hash


def generateSenderKey():
    return nodeCrypto.randomBytes(32)


def generateSenderKeyId():
    return nodeCrypto.randomInt(2147483647)


def generateSenderSigningKey(key=None):
    if not key:
        key = generateKeyPair()
    return JSObject({
        'public': bytes(key.get('pubKey')),
        'private': bytes(key.get('privKey'))
    })


keyhelper = JSObject({
    'generateSenderKey': generateSenderKey,
    'generateSenderKeyId': generateSenderKeyId,
    'generateSenderSigningKey': generateSenderSigningKey
})


class CiphertextMessage:
    def __init__(self):
        self.UNSUPPORTED_VERSION = 1
        self.CURRENT_VERSION = 3
        self.WHISPER_TYPE = 2
        self.PREKEY_TYPE = 3
        self.SENDERKEY_TYPE = 4
        self.SENDERKEY_DISTRIBUTION_TYPE = 5
        self.ENCRYPTED_MESSAGE_OVERHEAD = 53


class SenderKeyName:
    def __init__(self, groupId, sender):
        self.groupId = groupId
        self.sender = sender

    def getGroupId(self):
        return self.groupId

    def getSender(self):
        return self.sender

    def serialize(self):
        return f'{self.groupId}::{self.sender.id}::{self.sender.deviceId}'

    def toString(self):
        return self.serialize()

    def equals(self, other):
        if other is None:
            return False
        return self.groupId == other.groupId and self.sender.toString() == other.sender.toString()

    def hashCode(self):
        return hashCode(self.groupId) ^ hashCode(self.sender.toString())


class SenderMessageKey:
    def __init__(self, iteration, seed):
        derivative = deriveSecrets(asBuffer(seed), bytes(32), b'WhisperGroup')
        keys = bytearray(32)
        keys[0:16] = derivative[0][16:32]
        keys[16:32] = derivative[1][0:16]
        self.iv = bytes(derivative[0][0:16])
        self.cipherKey = bytes(keys)
        self.iteration = iteration
        self.seed = seed

    def getIteration(self):
        return self.iteration

    def getIv(self):
        return self.iv

    def getCipherKey(self):
        return self.cipherKey

    def getSeed(self):
        return self.seed


class SenderChainKey:
    def __init__(self, iteration, chainKey):
        self.MESSAGE_KEY_SEED = bytes([0x01])
        self.CHAIN_KEY_SEED = bytes([0x02])
        self.iteration = iteration
        self.chainKey = asBuffer(chainKey)

    def getIteration(self):
        return self.iteration

    def getSenderMessageKey(self):
        return SenderMessageKey(self.iteration, self.getDerivative(self.MESSAGE_KEY_SEED, self.chainKey))

    def getNext(self):
        return SenderChainKey(self.iteration + 1, self.getDerivative(self.CHAIN_KEY_SEED, self.chainKey))

    def getSeed(self):
        return self.chainKey

    def getDerivative(self, seed, key):
        return calculateMAC(key, seed)


class SenderKeyState:
    def __init__(self, id, iteration, chainKey, signatureKeyPair=None, signatureKeyPublic=None, signatureKeyPrivate=None, senderKeyStateStructure=None):
        self.MAX_MESSAGE_KEYS = 2000
        if senderKeyStateStructure:
            self.senderKeyStateStructure = JSObject(dict(senderKeyStateStructure))
            self.senderKeyStateStructure['senderMessageKeys'] = (
                senderKeyStateStructure.get('senderMessageKeys') if isinstance(senderKeyStateStructure.get('senderMessageKeys'), list) else []
            )
        else:
            if signatureKeyPair:
                signatureKeyPublic = signatureKeyPair.get('public')
                signatureKeyPrivate = signatureKeyPair.get('private')
            self.senderKeyStateStructure = JSObject({
                'senderKeyId': id or 0,
                'senderChainKey': JSObject({
                    'iteration': iteration or 0,
                    'seed': asBuffer(chainKey)
                }),
                'senderSigningKey': JSObject({
                    'public': asBuffer(signatureKeyPublic),
                    'private': asBuffer(signatureKeyPrivate)
                }),
                'senderMessageKeys': []
            })

    def getKeyId(self):
        return self.senderKeyStateStructure.get('senderKeyId')

    def getSenderChainKey(self):
        return SenderChainKey(self.senderKeyStateStructure.get('senderChainKey').get('iteration'), self.senderKeyStateStructure.get('senderChainKey').get('seed'))

    def setSenderChainKey(self, chainKey):
        self.senderKeyStateStructure['senderChainKey'] = JSObject({
            'iteration': chainKey.getIteration(),
            'seed': chainKey.getSeed()
        })

    def getSigningKeyPublic(self):
        publicKey = asBuffer(self.senderKeyStateStructure.get('senderSigningKey').get('public'))
        if len(publicKey) == 32:
            return bytes([0x05]) + publicKey
        return publicKey

    def getSigningKeyPrivate(self):
        return asBuffer(self.senderKeyStateStructure.get('senderSigningKey').get('private'))

    def hasSenderMessageKey(self, iteration):
        return any(key.get('iteration') == iteration for key in self.senderKeyStateStructure.get('senderMessageKeys'))

    def addSenderMessageKey(self, senderMessageKey):
        self.senderKeyStateStructure.get('senderMessageKeys').append(JSObject({
            'iteration': senderMessageKey.getIteration(),
            'seed': senderMessageKey.getSeed()
        }))
        if len(self.senderKeyStateStructure.get('senderMessageKeys')) > self.MAX_MESSAGE_KEYS:
            self.senderKeyStateStructure.get('senderMessageKeys').pop(0)

    def removeSenderMessageKey(self, iteration):
        keys = self.senderKeyStateStructure.get('senderMessageKeys')
        index = -1
        for i, key in enumerate(keys):
            if key.get('iteration') == iteration:
                index = i
                break
        if index != -1:
            messageKey = keys[index]
            del keys[index]
            return SenderMessageKey(messageKey.get('iteration'), messageKey.get('seed'))
        return None

    def getStructure(self):
        return self.senderKeyStateStructure


class SenderKeyRecord:
    def __init__(self, serialized=None):
        self.MAX_STATES = 5
        self.senderKeyStates = []
        if serialized:
            for structure in serialized:
                self.senderKeyStates.append(SenderKeyState(None, None, None, None, None, None, structure))

    def isEmpty(self):
        return len(self.senderKeyStates) == 0

    def getSenderKeyState(self, keyId=_UNSET):
        if keyId is _UNSET and len(self.senderKeyStates):
            return self.senderKeyStates[len(self.senderKeyStates) - 1]
        for state in self.senderKeyStates:
            if state.getKeyId() == keyId:
                return state
        return None

    def addSenderKeyState(self, id, iteration, chainKey, signatureKey):
        self.senderKeyStates.append(SenderKeyState(id, iteration, chainKey, None, signatureKey))
        if len(self.senderKeyStates) > self.MAX_STATES:
            self.senderKeyStates.pop(0)

    def setSenderKeyState(self, id, iteration, chainKey, keyPair):
        self.senderKeyStates.clear()
        self.senderKeyStates.append(SenderKeyState(id, iteration, chainKey, keyPair))

    def serialize(self):
        return [state.getStructure() for state in self.senderKeyStates]

    @staticmethod
    def deserialize(data):
        text = asBuffer(data).decode('utf-8')
        parsed = reviveJSON(json.loads(text), '')
        return SenderKeyRecord(parsed)


def reviveJSON(value, key):
    if isinstance(value, list):
        value = [reviveJSON(item, str(index)) for index, item in enumerate(value)]
    elif isinstance(value, dict):
        value = JSObject({k: reviveJSON(v, k) for k, v in dict.items(value)})
    return BufferJSON['reviver'](key, value)


class SenderKeyDistributionMessage(CiphertextMessage):
    def __init__(self, id, iteration, chainKey, signatureKey, serialized=None):
        super().__init__()
        if serialized is not None:
            try:
                message = serialized[1:]
                distributionMessage = JSObject(dict(proto.SenderKeyDistributionMessage.decode(message).toJSON()))
                self.serialized = serialized
                self.id = distributionMessage.get('id')
                self.iteration = distributionMessage.get('iteration')
                self.chainKey = base64.b64decode(distributionMessage.get('chainKey')) if isinstance(distributionMessage.get('chainKey'), str) else distributionMessage.get('chainKey')
                self.signatureKey = base64.b64decode(distributionMessage.get('signingKey')) if isinstance(distributionMessage.get('signingKey'), str) else distributionMessage.get('signingKey')
            except Exception as e:
                raise JSError(str(e))
        else:
            version = self.intsToByteHighAndLow(self.CURRENT_VERSION, self.CURRENT_VERSION)
            self.id = id
            self.iteration = iteration
            self.chainKey = chainKey
            self.signatureKey = signatureKey
            message = proto.SenderKeyDistributionMessage.encode(
                proto.SenderKeyDistributionMessage.create(JSObject({'id': id, 'iteration': iteration, 'chainKey': chainKey, 'signingKey': self.signatureKey}))
            ).finish()
            self.serialized = bytes([version]) + bytes(message)

    def intsToByteHighAndLow(self, highValue, lowValue):
        return (((highValue << 4) | lowValue) & 0xff) % 256

    def serialize(self):
        return self.serialized

    def getType(self):
        return self.SENDERKEY_DISTRIBUTION_TYPE

    def getIteration(self):
        return self.iteration

    def getChainKey(self):
        return self.chainKey

    def getSignatureKey(self):
        return self.signatureKey

    def getId(self):
        return self.id


class SenderKeyMessage(CiphertextMessage):
    def __init__(self, keyId, iteration, ciphertext, signatureKey, serialized=None):
        super().__init__()
        self.SIGNATURE_LENGTH = 64
        if serialized is not None:
            version = serialized[0]
            message = serialized[1:len(serialized) - self.SIGNATURE_LENGTH]
            signature = serialized[-1 * self.SIGNATURE_LENGTH:]
            senderKeyMessage = JSObject(dict(proto.SenderKeyMessage.decode(message).toJSON()))
            self.serialized = serialized
            self.messageVersion = (version & 0xff) >> 4
            self.keyId = senderKeyMessage.get('id')
            self.iteration = senderKeyMessage.get('iteration')
            self.ciphertext = base64.b64decode(senderKeyMessage.get('ciphertext')) if isinstance(senderKeyMessage.get('ciphertext'), str) else senderKeyMessage.get('ciphertext')
            self.signature = signature
        else:
            version = (((self.CURRENT_VERSION << 4) | self.CURRENT_VERSION) & 0xff) % 256
            ciphertextBuffer = bytes(ciphertext)
            message = proto.SenderKeyMessage.encode(
                proto.SenderKeyMessage.create(JSObject({'id': keyId, 'iteration': iteration, 'ciphertext': ciphertextBuffer}))
            ).finish()
            signature = self.getSignature(signatureKey, bytes([version]) + bytes(message))
            self.serialized = bytes([version]) + bytes(message) + signature
            self.messageVersion = self.CURRENT_VERSION
            self.keyId = keyId
            self.iteration = iteration
            self.ciphertext = ciphertextBuffer
            self.signature = signature

    def getKeyId(self):
        return self.keyId

    def getIteration(self):
        return self.iteration

    def getCipherText(self):
        return self.ciphertext

    def verifySignature(self, signatureKey):
        part1 = self.serialized[0:len(self.serialized) - self.SIGNATURE_LENGTH]
        part2 = self.serialized[-1 * self.SIGNATURE_LENGTH:]
        res = verifySignature(signatureKey, part1, part2)
        if not res:
            raise JSError('Invalid signature!')

    def getSignature(self, signatureKey, serialized):
        return bytes(calculateSignature(signatureKey, serialized))

    def serialize(self):
        return self.serialized

    def getType(self):
        return 4


class GroupSessionBuilder:
    def __init__(self, senderKeyStore):
        self.senderKeyStore = senderKeyStore

    async def process(self, senderKeyName, senderKeyDistributionMessage):
        senderKeyRecord = await self.senderKeyStore.get('loadSenderKey')(senderKeyName)
        senderKeyRecord.addSenderKeyState(
            senderKeyDistributionMessage.getId(),
            senderKeyDistributionMessage.getIteration(),
            senderKeyDistributionMessage.getChainKey(),
            senderKeyDistributionMessage.getSignatureKey()
        )
        await self.senderKeyStore.get('storeSenderKey')(senderKeyName, senderKeyRecord)

    async def create(self, senderKeyName):
        senderKeyRecord = await self.senderKeyStore.get('loadSenderKey')(senderKeyName)
        if senderKeyRecord.isEmpty():
            keyId = keyhelper.get('generateSenderKeyId')()
            senderKey = keyhelper.get('generateSenderKey')()
            signingKey = keyhelper.get('generateSenderSigningKey')()
            senderKeyRecord.setSenderKeyState(keyId, 0, senderKey, signingKey)
            await self.senderKeyStore.get('storeSenderKey')(senderKeyName, senderKeyRecord)
        state = senderKeyRecord.getSenderKeyState()
        if state is None:
            raise JSError('No session state available')
        return SenderKeyDistributionMessage(
            state.getKeyId(),
            state.getSenderChainKey().getIteration(),
            state.getSenderChainKey().getSeed(),
            state.getSigningKeyPublic()
        )


class GroupCipher:
    def __init__(self, senderKeyStore, senderKeyName):
        self.senderKeyStore = senderKeyStore
        self.senderKeyName = senderKeyName

    async def encrypt(self, paddedPlaintext):
        record = await self.senderKeyStore.get('loadSenderKey')(self.senderKeyName)
        if not record:
            raise JSError('No SenderKeyRecord found for encryption')
        senderKeyState = record.getSenderKeyState()
        if senderKeyState is None:
            raise JSError('No session to encrypt message')
        iteration = senderKeyState.getSenderChainKey().getIteration()
        senderKey = self.getSenderKey(senderKeyState, 0 if iteration == 0 else iteration + 1)
        ciphertext = await self.getCipherText(senderKey.getIv(), senderKey.getCipherKey(), paddedPlaintext)
        senderKeyMessage = SenderKeyMessage(senderKeyState.getKeyId(), senderKey.getIteration(), ciphertext, senderKeyState.getSigningKeyPrivate())
        await self.senderKeyStore.get('storeSenderKey')(self.senderKeyName, record)
        return senderKeyMessage.serialize()

    async def decrypt(self, senderKeyMessageBytes):
        record = await self.senderKeyStore.get('loadSenderKey')(self.senderKeyName)
        if not record:
            raise JSError('No SenderKeyRecord found for decryption')
        senderKeyMessage = SenderKeyMessage(None, None, None, None, senderKeyMessageBytes)
        senderKeyState = record.getSenderKeyState(senderKeyMessage.getKeyId())
        if senderKeyState is None:
            raise JSError('No session found to decrypt message')
        senderKeyMessage.verifySignature(senderKeyState.getSigningKeyPublic())
        senderKey = self.getSenderKey(senderKeyState, senderKeyMessage.getIteration())
        plaintext = await self.getPlainText(senderKey.getIv(), senderKey.getCipherKey(), senderKeyMessage.getCipherText())
        await self.senderKeyStore.get('storeSenderKey')(self.senderKeyName, record)
        return plaintext

    def getSenderKey(self, senderKeyState, iteration):
        senderChainKey = senderKeyState.getSenderChainKey()
        if senderChainKey.getIteration() > iteration:
            if senderKeyState.hasSenderMessageKey(iteration):
                messageKey = senderKeyState.removeSenderMessageKey(iteration)
                if messageKey is None:
                    raise JSError('No sender message key found for iteration')
                return messageKey
            raise JSError(f'Received message with old counter: {senderChainKey.getIteration()}, {iteration}')
        if iteration - senderChainKey.getIteration() > 2000:
            raise JSError('Over 2000 messages into the future!')
        while senderChainKey.getIteration() < iteration:
            senderKeyState.addSenderMessageKey(senderChainKey.getSenderMessageKey())
            senderChainKey = senderChainKey.getNext()
        senderKeyState.setSenderChainKey(senderChainKey.getNext())
        return senderChainKey.getSenderMessageKey()

    async def getPlainText(self, iv, key, ciphertext):
        try:
            return decrypt(key, ciphertext, iv)
        except Exception:
            raise JSError('InvalidMessageException')

    async def getCipherText(self, iv, key, plaintext):
        try:
            return encrypt(key, plaintext, iv)
        except Exception:
            raise JSError('InvalidMessageException')
