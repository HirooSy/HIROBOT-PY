import base64
import hashlib
import hmac as hmacModule
import os
import re
import secrets
import struct
import uuid

from baileys_lite.foundation.node_runtime import JSError, JSObject


def gmul(a, b):
    product = 0
    for _ in range(8):
        if b & 1:
            product ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= 0x1B
        b >>= 1
    return product


def rotl8(value, count):
    return ((value << count) | (value >> (8 - count))) & 0xFF


def buildSboxes():
    inverse = [0] * 256
    for x in range(1, 256):
        result = 1
        base = x
        exponent = 254
        while exponent:
            if exponent & 1:
                result = gmul(result, base)
            base = gmul(base, base)
            exponent >>= 1
        inverse[x] = result
    sBox = [0] * 256
    for x in range(256):
        b = inverse[x]
        sBox[x] = b ^ rotl8(b, 1) ^ rotl8(b, 2) ^ rotl8(b, 3) ^ rotl8(b, 4) ^ 0x63
    invSBox = [0] * 256
    for x in range(256):
        invSBox[sBox[x]] = x
    return sBox, invSBox


SBOX, INV_SBOX = buildSboxes()

RCON = []
rconValue = 1
for _ in range(15):
    RCON.append(rconValue)
    rconValue = gmul(rconValue, 2)

MUL = {n: [gmul(x, n) for x in range(256)] for n in (2, 3, 9, 11, 13, 14)}

SHIFT_ROWS = [(i % 4) + 4 * (((i // 4) + (i % 4)) % 4) for i in range(16)]
INV_SHIFT_ROWS = [(i % 4) + 4 * (((i // 4) - (i % 4)) % 4) for i in range(16)]


def expandKey(key):
    nk = len(key) // 4
    nr = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            temp = [SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    roundKeys = []
    for r in range(nr + 1):
        roundKeys.append(sum(words[4 * r:4 * r + 4], []))
    return nr, roundKeys


def mixColumns(state):
    m2, m3 = MUL[2], MUL[3]
    out = [0] * 16
    for c in range(0, 16, 4):
        a0, a1, a2, a3 = state[c:c + 4]
        out[c] = m2[a0] ^ m3[a1] ^ a2 ^ a3
        out[c + 1] = a0 ^ m2[a1] ^ m3[a2] ^ a3
        out[c + 2] = a0 ^ a1 ^ m2[a2] ^ m3[a3]
        out[c + 3] = m3[a0] ^ a1 ^ a2 ^ m2[a3]
    return out


def invMixColumns(state):
    m9, m11, m13, m14 = MUL[9], MUL[11], MUL[13], MUL[14]
    out = [0] * 16
    for c in range(0, 16, 4):
        a0, a1, a2, a3 = state[c:c + 4]
        out[c] = m14[a0] ^ m11[a1] ^ m13[a2] ^ m9[a3]
        out[c + 1] = m9[a0] ^ m14[a1] ^ m11[a2] ^ m13[a3]
        out[c + 2] = m13[a0] ^ m9[a1] ^ m14[a2] ^ m11[a3]
        out[c + 3] = m11[a0] ^ m13[a1] ^ m9[a2] ^ m14[a3]
    return out


class AesBlock:
    def __init__(self, key):
        self.nr, self.roundKeys = expandKey(bytes(key))

    def encrypt(self, block):
        roundKeys = self.roundKeys
        nr = self.nr
        state = [b ^ k for b, k in zip(block, roundKeys[0])]
        for r in range(1, nr):
            state = [SBOX[state[SHIFT_ROWS[i]]] for i in range(16)]
            state = mixColumns(state)
            rk = roundKeys[r]
            state = [state[i] ^ rk[i] for i in range(16)]
        state = [SBOX[state[SHIFT_ROWS[i]]] for i in range(16)]
        rk = roundKeys[nr]
        return bytes(state[i] ^ rk[i] for i in range(16))

    def decrypt(self, block):
        roundKeys = self.roundKeys
        nr = self.nr
        rk = roundKeys[nr]
        state = [block[i] ^ rk[i] for i in range(16)]
        for r in range(nr - 1, 0, -1):
            state = [INV_SBOX[state[INV_SHIFT_ROWS[i]]] for i in range(16)]
            rk = roundKeys[r]
            state = [state[i] ^ rk[i] for i in range(16)]
            state = invMixColumns(state)
        state = [INV_SBOX[state[INV_SHIFT_ROWS[i]]] for i in range(16)]
        rk = roundKeys[0]
        return bytes(state[i] ^ rk[i] for i in range(16))


GCM_R = 0xE1 << 120


def gfMul(x, y):
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ GCM_R
        else:
            v >>= 1
    return z


class Ghash:
    def __init__(self, hashKey):
        self.hashKey = hashKey
        self.y = 0
        self.pending = b''

    def feed(self, data):
        buf = self.pending + bytes(data)
        full = len(buf) - len(buf) % 16
        for offset in range(0, full, 16):
            self.y = gfMul(self.y ^ int.from_bytes(buf[offset:offset + 16], 'big'), self.hashKey)
        self.pending = buf[full:]

    def pad(self):
        if self.pending:
            self.feed(bytes((16 - len(self.pending)) % 16))


def xorBytes(a, b):
    count = len(a)
    if count == 0:
        return b''
    return (int.from_bytes(a, 'big') ^ int.from_bytes(bytes(b[:count]), 'big')).to_bytes(count, 'big')


def incCtr128(value):
    return (value + 1) & ((1 << 128) - 1)


def incGcm32(value):
    return (value & ~0xFFFFFFFF) | ((value + 1) & 0xFFFFFFFF)


def toBytes(data):
    if isinstance(data, str):
        return data.encode('utf-8')
    if data is None:
        raise JSError('The "data" argument must be of type string or an instance of Buffer', code='ERR_INVALID_ARG_TYPE')
    return bytes(data)


def hashName(alg):
    name = str(alg).lower()
    if name not in ('md5', 'sha1', 'sha256', 'sha384', 'sha512'):
        raise JSError(f'Digest method not supported: {alg}', code='ERR_CRYPTO_INVALID_DIGEST')
    return name


def encodeDigest(raw, enc):
    if enc is None:
        return raw
    if enc == 'hex':
        return raw.hex()
    if enc == 'base64':
        return base64.b64encode(raw).decode('ascii')
    if enc == 'base64url':
        return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
    if enc in ('utf8', 'utf-8'):
        return raw.decode('utf-8')
    if enc in ('latin1', 'binary'):
        return raw.decode('latin-1')
    raise JSError(f'Unknown encoding: {enc}', code='ERR_INVALID_ARG_VALUE')


class Hash:
    def __init__(self, alg):
        self.digestState = hashlib.new(hashName(alg))

    def update(self, data, inputEncoding=None):
        self.digestState.update(toBytes(data))
        return self

    def digest(self, enc=None):
        return encodeDigest(self.digestState.digest(), enc)

    def copy(self):
        clone = Hash.__new__(Hash)
        clone.digestState = self.digestState.copy()
        return clone


class Hmac:
    def __init__(self, alg, key):
        self.mac = hmacModule.new(toBytes(key), digestmod=hashName(alg))

    def update(self, data, inputEncoding=None):
        self.mac.update(toBytes(data))
        return self

    def digest(self, enc=None):
        return encodeDigest(self.mac.digest(), enc)


def createHash(alg):
    return Hash(alg)


def createHmac(alg, key):
    return Hmac(alg, key)


def randomBytes(size, callback=None):
    raw = os.urandom(int(size))
    if callback is not None:
        callback(None, raw)
        return None
    return raw


def randomUUID(options=None):
    return str(uuid.uuid4())


def randomInt(minOrMax, maxValue=None, callback=None):
    if maxValue is None:
        low, high = 0, minOrMax
    else:
        low, high = minOrMax, maxValue
    if high <= low:
        raise JSError('The value of "max" is out of range.', code='ERR_OUT_OF_RANGE')
    value = low + secrets.randbelow(high - low)
    if callback is not None:
        callback(None, value)
        return None
    return value


def timingSafeEqual(a, b):
    left = bytes(a)
    right = bytes(b)
    if len(left) != len(right):
        raise JSError('Input buffers must have the same byte length', code='ERR_CRYPTO_TIMING_SAFE_EQUAL_LENGTH')
    return hmacModule.compare_digest(left, right)


def getRandomValues(array):
    count = len(array)
    raw = os.urandom(count)
    if isinstance(array, bytearray):
        array[:] = raw
        return array
    if isinstance(array, list):
        array[:] = list(raw)
        return array
    return raw


def randomFillSync(buffer, offset=0, size=None):
    if not isinstance(buffer, bytearray):
        raise JSError('randomFillSync requires a mutable buffer', code='ERR_INVALID_ARG_TYPE')
    start = offset or 0
    end = len(buffer) if size is None else start + size
    buffer[start:end] = os.urandom(end - start)
    return buffer


def hkdfSync(digest, ikm, salt, info, keylen):
    name = hashName(digest)
    hashLength = hashlib.new(name).digest_size
    if keylen > 255 * hashLength:
        raise JSError('HKDF expansion failed', code='ERR_CRYPTO_INVALID_KEYLEN')
    ikmBytes = toBytes(ikm)
    saltBytes = b'' if salt is None else toBytes(salt)
    infoBytes = b'' if info is None else toBytes(info)
    if not saltBytes:
        saltBytes = bytes(hashLength)
    prk = hmacModule.new(saltBytes, ikmBytes, name).digest()
    output = b''
    block = b''
    index = 1
    while len(output) < keylen:
        block = hmacModule.new(prk, block + infoBytes + bytes([index]), name).digest()
        output += block
        index += 1
    return output[:keylen]


async def hkdf(digest, ikm, salt, info, keylen):
    return hkdfSync(digest, ikm, salt, info, keylen)


AES_NAME = re.compile(r'aes-(128|256)-(cbc|ctr|gcm)')


class Cipher:
    def __init__(self, alg, key, iv, options=None, decrypt=False):
        match = AES_NAME.fullmatch(str(alg).lower())
        if match is None:
            raise JSError(f'Unknown cipher: {alg}', code='ERR_CRYPTO_UNKNOWN_CIPHER')
        keyBytes = toBytes(key)
        ivBytes = b'' if iv is None else toBytes(iv)
        if len(keyBytes) != int(match.group(1)) // 8:
            raise JSError('Invalid key length', code='ERR_CRYPTO_INVALID_KEYLEN')
        self.mode = match.group(2)
        self.decrypting = bool(decrypt)
        self.aes = AesBlock(keyBytes)
        self.autoPadding = True
        self.buffered = bytearray()
        self.keystream = b''
        self.finalized = False
        self.tag = None
        self.expectedTag = None
        self.tagLength = 16
        if options is not None and options.get('authTagLength') is not None:
            self.tagLength = int(options.get('authTagLength'))
        if self.mode == 'cbc':
            if len(ivBytes) != 16:
                raise JSError('Invalid initialization vector', code='ERR_CRYPTO_INVALID_IV')
            self.chain = ivBytes
        elif self.mode == 'ctr':
            if len(ivBytes) != 16:
                raise JSError('Invalid initialization vector', code='ERR_CRYPTO_INVALID_IV')
            self.counter = int.from_bytes(ivBytes, 'big')
        else:
            if len(ivBytes) == 0:
                raise JSError('Invalid initialization vector', code='ERR_CRYPTO_INVALID_IV')
            self.hashKey = int.from_bytes(self.aes.encrypt(bytes(16)), 'big')
            if len(ivBytes) == 12:
                j0 = int.from_bytes(ivBytes + b'\x00\x00\x00\x01', 'big')
            else:
                helper = Ghash(self.hashKey)
                helper.feed(ivBytes)
                helper.pad()
                helper.feed(struct.pack('>QQ', 0, len(ivBytes) * 8))
                j0 = helper.y
            self.j0 = j0
            self.counter = incGcm32(j0)
            self.ghash = Ghash(self.hashKey)
            self.aadLength = 0
            self.dataLength = 0
            self.aadSealed = False

    def takeKeystream(self, count):
        pieces = [self.keystream]
        have = len(self.keystream)
        while have < count:
            pieces.append(self.aes.encrypt(self.counter.to_bytes(16, 'big')))
            self.counter = incGcm32(self.counter) if self.mode == 'gcm' else incCtr128(self.counter)
            have += 16
        stream = b''.join(pieces)
        self.keystream = stream[count:]
        return stream[:count]

    def sealAad(self):
        if not self.aadSealed:
            self.ghash.pad()
            self.aadSealed = True

    def setAAD(self, data, options=None):
        if self.mode != 'gcm' or self.aadSealed:
            raise JSError('Unsupported state', code='ERR_CRYPTO_INVALID_STATE')
        chunk = toBytes(data)
        self.ghash.feed(chunk)
        self.aadLength += len(chunk)
        return self

    def update(self, data, inputEncoding=None, outputEncoding=None):
        if self.finalized:
            raise JSError('Unsupported state', code='ERR_CRYPTO_INVALID_STATE')
        chunk = toBytes(data)
        if self.mode == 'ctr':
            return xorBytes(chunk, self.takeKeystream(len(chunk)))
        if self.mode == 'gcm':
            self.sealAad()
            self.dataLength += len(chunk)
            if self.decrypting:
                self.ghash.feed(chunk)
                return xorBytes(chunk, self.takeKeystream(len(chunk)))
            sealed = xorBytes(chunk, self.takeKeystream(len(chunk)))
            self.ghash.feed(sealed)
            return sealed
        pending = self.buffered
        pending += chunk
        blockCount = len(pending) // 16
        if self.decrypting and self.autoPadding and blockCount and len(pending) % 16 == 0:
            blockCount -= 1
        out = bytearray()
        for offset in range(0, blockCount * 16, 16):
            block = bytes(pending[offset:offset + 16])
            if self.decrypting:
                out += xorBytes(self.aes.decrypt(block), self.chain)
                self.chain = block
            else:
                cipherBlock = self.aes.encrypt(xorBytes(block, self.chain))
                self.chain = cipherBlock
                out += cipherBlock
        del pending[:blockCount * 16]
        return bytes(out)

    def final(self):
        if self.finalized:
            raise JSError('Unsupported state', code='ERR_CRYPTO_INVALID_STATE')
        self.finalized = True
        if self.mode == 'ctr':
            return b''
        if self.mode == 'gcm':
            self.sealAad()
            self.ghash.pad()
            self.ghash.feed(struct.pack('>QQ', self.aadLength * 8, self.dataLength * 8))
            maskBlock = int.from_bytes(self.aes.encrypt(self.j0.to_bytes(16, 'big')), 'big')
            tag = (self.ghash.y ^ maskBlock).to_bytes(16, 'big')[:self.tagLength]
            if not self.decrypting:
                self.tag = tag
                return b''
            if self.expectedTag is None or len(self.expectedTag) != len(tag) or not hmacModule.compare_digest(self.expectedTag, tag):
                raise JSError('Unsupported state or unable to authenticate data', code='ERR_CRYPTO_INVALID_STATE')
            self.tag = tag
            return b''
        remaining = bytes(self.buffered)
        self.buffered = bytearray()
        if not self.decrypting:
            if self.autoPadding:
                padLength = 16 - len(remaining) % 16
                remaining += bytes([padLength]) * padLength
            elif len(remaining) % 16:
                raise JSError('wrong final block length', code='ERR_OSSL_WRONG_FINAL_BLOCK_LENGTH')
            out = bytearray()
            for offset in range(0, len(remaining), 16):
                cipherBlock = self.aes.encrypt(xorBytes(remaining[offset:offset + 16], self.chain))
                self.chain = cipherBlock
                out += cipherBlock
            return bytes(out)
        if self.autoPadding:
            if len(remaining) != 16:
                raise JSError('wrong final block length', code='ERR_OSSL_WRONG_FINAL_BLOCK_LENGTH')
            plain = xorBytes(self.aes.decrypt(remaining), self.chain)
            padLength = plain[-1]
            if padLength < 1 or padLength > 16 or plain[-padLength:] != bytes([padLength]) * padLength:
                raise JSError('bad decrypt', code='ERR_OSSL_BAD_DECRYPT')
            return plain[:-padLength]
        if remaining:
            raise JSError('wrong final block length', code='ERR_OSSL_WRONG_FINAL_BLOCK_LENGTH')
        return b''

    def getAuthTag(self):
        if self.mode != 'gcm' or self.tag is None or self.decrypting:
            raise JSError('Invalid state for operation getAuthTag', code='ERR_CRYPTO_INVALID_STATE')
        return self.tag

    def setAuthTag(self, tag, encoding=None):
        self.expectedTag = toBytes(tag)
        return self

    def setAutoPadding(self, flag=True):
        self.autoPadding = True if flag is None else bool(flag)
        return self


def createCipheriv(alg, key, iv, options=None):
    return Cipher(alg, key, iv, options, False)


def createDecipheriv(alg, key, iv, options=None):
    return Cipher(alg, key, iv, options, True)


P25519 = (1 << 255) - 19
X25519_BASE = (9).to_bytes(32, 'little')


def x25519(scalar, uCoordinate):
    k = bytearray(toBytes(scalar))
    if len(k) != 32:
        raise JSError('Invalid X25519 scalar length', code='ERR_CRYPTO_INVALID_KEYLEN')
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    kNum = int.from_bytes(bytes(k), 'little')
    u = bytearray(toBytes(uCoordinate))
    u[31] &= 127
    x1 = int.from_bytes(bytes(u), 'little') % P25519
    x2, z2, x3, z3 = 1, 0, x1, 1
    swap = 0
    for t in range(254, -1, -1):
        kt = (kNum >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt
        a = (x2 + z2) % P25519
        aa = a * a % P25519
        b = (x2 - z2) % P25519
        bb = b * b % P25519
        e = (aa - bb) % P25519
        c = (x3 + z3) % P25519
        d = (x3 - z3) % P25519
        da = d * a % P25519
        cb = c * b % P25519
        x3 = pow((da + cb) % P25519, 2, P25519)
        z3 = x1 * pow((da - cb) % P25519, 2, P25519) % P25519
        x2 = aa * bb % P25519
        z2 = e * ((aa + 121665 * e) % P25519) % P25519
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    result = x2 * pow(z2, P25519 - 2, P25519) % P25519
    return result.to_bytes(32, 'little')


P256 = 2 ** 256 - 2 ** 224 + 2 ** 192 + 2 ** 96 - 1
P256_A = P256 - 3
P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
P256_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
          0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def ecAdd(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P256 == 0:
        return None
    if x1 == x2 and y1 == y2:
        lam = (3 * x1 * x1 + P256_A) * pow(2 * y1, -1, P256) % P256
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P256) % P256
    x3 = (lam * lam - x1 - x2) % P256
    y3 = (lam * (x1 - x3) - y1) % P256
    return (x3, y3)


def ecMul(scalar, point):
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = ecAdd(result, addend)
        addend = ecAdd(addend, addend)
        scalar >>= 1
    return result


def encodeEcPoint(point):
    return b'\x04' + point[0].to_bytes(32, 'big') + point[1].to_bytes(32, 'big')


def decodeEcPoint(data):
    raw = bytes(data)
    if len(raw) == 65 and raw[0] == 4:
        point = (int.from_bytes(raw[1:33], 'big'), int.from_bytes(raw[33:], 'big'))
    elif len(raw) == 33 and raw[0] in (2, 3):
        x = int.from_bytes(raw[1:], 'big')
        rhs = (pow(x, 3, P256) + P256_A * x + P256_B) % P256
        y = pow(rhs, (P256 + 1) // 4, P256)
        if y % 2 != raw[0] & 1:
            y = P256 - y
        point = (x, y)
    else:
        raise JSError('Failed to convert buffer to EC_POINT', code='ERR_CRYPTO_INVALID_POINT')
    return point


def randomScalar():
    return secrets.randbelow(P256_N - 1) + 1


def derLength(length):
    if length < 128:
        return bytes([length])
    encoded = length.to_bytes((length.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(encoded)]) + encoded


def derInteger(value):
    body = value.to_bytes((value.bit_length() + 7) // 8 or 1, 'big')
    if body[0] & 0x80:
        body = b'\x00' + body
    return b'\x02' + derLength(len(body)) + body


def ecSignDigest(d, digest):
    e = int.from_bytes(digest, 'big')
    excess = len(digest) * 8 - P256_N.bit_length()
    if excess > 0:
        e >>= excess
    while True:
        k = randomScalar()
        r = ecMul(k, P256_G)[0] % P256_N
        if r == 0:
            continue
        s = pow(k, -1, P256_N) * (e + r * d) % P256_N
        if s == 0:
            continue
        body = derInteger(r) + derInteger(s)
        return b'\x30' + derLength(len(body)) + body


def ecVerifyDigest(public, digest, signature):
    try:
        parts = _children(signature)
        if parts[0][0] != 0x30:
            return False
        kids = _children(parts[0][1])
        r = int.from_bytes(kids[0][1], 'big')
        s = int.from_bytes(kids[1][1], 'big')
    except (IndexError, JSError):
        return False
    if not (1 <= r < P256_N and 1 <= s < P256_N):
        return False
    e = int.from_bytes(digest, 'big')
    excess = len(digest) * 8 - P256_N.bit_length()
    if excess > 0:
        e >>= excess
    w = pow(s, -1, P256_N)
    point = ecAdd(ecMul(e * w % P256_N, P256_G), ecMul(r * w % P256_N, public))
    if point is None:
        return False
    return point[0] % P256_N == r


class KeyObject:
    def __init__(self, kind, asymmetricKeyType, raw, curve=None):
        self.type = kind
        self.asymmetricKeyType = asymmetricKeyType
        self.raw = bytes(raw)
        self.namedCurve = curve

    def export(self, options=None):
        if options is None or options.get('format') != 'der':
            raise JSError('Unsupported export format', code='ERR_CRYPTO_INVALID_FORMAT')
        kind = options.get('type')
        if self.type == 'public' and kind == 'spki':
            return encodeSpki(self)
        if self.type == 'private' and kind == 'pkcs8':
            return encodePkcs8(self)
        raise JSError('Unsupported export type', code='ERR_CRYPTO_INVALID_FORMAT')


OID_X25519 = bytes.fromhex('2b656e')
OID_ED25519 = bytes.fromhex('2b6570')
OID_EC = bytes.fromhex('2a8648ce3d0201')
OID_P256 = bytes.fromhex('2a8648ce3d030107')

SPKI_PREFIX = {
    'x25519': bytes.fromhex('302a300506032b656e032100'),
    'ed25519': bytes.fromhex('302a300506032b6570032100'),
    'ec': bytes.fromhex('3059301306072a8648ce3d020106082a8648ce3d030107034200'),
}
PKCS8_PREFIX = {
    'x25519': bytes.fromhex('302e020100300506032b656e04220420'),
    'ed25519': bytes.fromhex('302e020100300506032b657004220420'),
    'ec': bytes.fromhex('3041020100301306072a8648ce3d020106082a8648ce3d030107042730250201010420'),
}


def encodeSpki(key):
    return SPKI_PREFIX[key.asymmetricKeyType] + key.raw


def encodePkcs8(key):
    return PKCS8_PREFIX[key.asymmetricKeyType] + key.raw


def readTlv(data, offset):
    length = data[offset + 1]
    tagEnd = offset + 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[tagEnd:tagEnd + count], 'big')
        tagEnd += count
    end = tagEnd + length
    if end > len(data):
        raise JSError('Invalid DER encoding', code='ERR_OSSL_ASN1_TOO_LONG')
    return data[offset], data[tagEnd:end], end


def _children(data):
    items = []
    offset = 0
    data = bytes(data)
    while offset < len(data):
        start = offset
        tag, content, offset = readTlv(data, offset)
        items.append((tag, content, data[start:offset]))
    return items


def parseSpki(der):
    root = _children(der)
    if not root or root[0][0] != 0x30:
        raise JSError('Invalid SubjectPublicKeyInfo', code='ERR_OSSL_UNSUPPORTED')
    parts = _children(root[0][1])
    algorithm = _children(parts[0][1])
    oid = algorithm[0][1]
    bits = parts[1][1][1:]
    if oid == OID_X25519:
        return KeyObject('public', 'x25519', bits)
    if oid == OID_ED25519:
        return KeyObject('public', 'ed25519', bits)
    if oid == OID_EC and len(algorithm) > 1 and algorithm[1][1] == OID_P256:
        return KeyObject('public', 'ec', bits, 'prime256v1')
    raise JSError('Unsupported public key type', code='ERR_OSSL_UNSUPPORTED')


def parsePkcs8(der):
    root = _children(der)
    parts = _children(root[0][1])
    algorithm = _children(parts[1][1])
    oid = algorithm[0][1]
    if oid == OID_X25519:
        return KeyObject('private', 'x25519', _children(parts[2][1])[0][1])
    if oid == OID_ED25519:
        return KeyObject('private', 'ed25519', _children(parts[2][1])[0][1])
    if oid == OID_EC:
        ecKids = _children(_children(parts[2][1])[0][1])
        return KeyObject('private', 'ec', ecKids[1][1], 'prime256v1')
    raise JSError('Unsupported private key type', code='ERR_OSSL_UNSUPPORTED')


def derivePublic(key):
    if key.asymmetricKeyType == 'x25519':
        return KeyObject('public', 'x25519', x25519(key.raw, X25519_BASE))
    if key.asymmetricKeyType == 'ed25519':
        from baileys_lite.foundation.curve25519 import edPublicKeyFromSeed
        return KeyObject('public', 'ed25519', edPublicKeyFromSeed(key.raw))
    if key.asymmetricKeyType == 'ec':
        point = ecMul(int.from_bytes(key.raw, 'big'), P256_G)
        return KeyObject('public', 'ec', encodeEcPoint(point), 'prime256v1')
    raise JSError('Unsupported key type', code='ERR_CRYPTO_INVALID_KEYTYPE')


def keyInput(key, kind):
    if isinstance(key, KeyObject):
        return key
    if isinstance(key, (bytes, bytearray, memoryview)):
        return JSObject({'key': bytes(key), 'format': 'der', 'type': kind})
    return key


def createPublicKey(key):
    source = keyInput(key, 'spki')
    if isinstance(source, KeyObject):
        return source if source.type == 'public' else derivePublic(source)
    material = source.get('key')
    if isinstance(material, KeyObject):
        return createPublicKey(material)
    if source.get('format') not in (None, 'der'):
        raise JSError('Unsupported key format', code='ERR_CRYPTO_INVALID_FORMAT')
    if (source.get('type') or 'spki') == 'pkcs8':
        return derivePublic(parsePkcs8(material))
    return parseSpki(material)


def createPrivateKey(key):
    source = keyInput(key, 'pkcs8')
    if isinstance(source, KeyObject):
        if source.type != 'private':
            raise JSError('Expected a private key', code='ERR_CRYPTO_INVALID_KEYTYPE')
        return source
    material = source.get('key')
    if isinstance(material, KeyObject):
        return createPrivateKey(material)
    if source.get('format') not in (None, 'der'):
        raise JSError('Unsupported key format', code='ERR_CRYPTO_INVALID_FORMAT')
    return parsePkcs8(material)


def diffieHellman(options):
    privateKey = options.get('privateKey')
    publicKey = options.get('publicKey')
    if privateKey.asymmetricKeyType == 'x25519':
        return x25519(privateKey.raw, publicKey.raw)
    if privateKey.asymmetricKeyType == 'ec':
        shared = ecMul(int.from_bytes(privateKey.raw, 'big'), decodeEcPoint(publicKey.raw))
        if shared is None:
            raise JSError('Failed to compute shared secret', code='ERR_CRYPTO_OPERATION_FAILED')
        return shared[0].to_bytes(32, 'big')
    raise JSError('Unsupported key type', code='ERR_CRYPTO_INVALID_KEYTYPE')


def applyEncoding(key, options, field):
    if options is None or options[field] is None:
        return key
    return key.export(options[field])


def generateKeyPairSync(kind, options=None):
    if kind == 'x25519':
        seed = os.urandom(32)
        private = KeyObject('private', 'x25519', seed)
        public = KeyObject('public', 'x25519', x25519(seed, X25519_BASE))
    elif kind == 'ed25519':
        from baileys_lite.foundation.curve25519 import edPublicKeyFromSeed
        seed = os.urandom(32)
        private = KeyObject('private', 'ed25519', seed)
        public = KeyObject('public', 'ed25519', edPublicKeyFromSeed(seed))
    elif kind == 'ec':
        if options is None or options.get('namedCurve') not in ('prime256v1', 'P-256', 'secp256r1'):
            raise JSError('Unsupported named curve', code='ERR_CRYPTO_INVALID_CURVE')
        scalar = randomScalar()
        private = KeyObject('private', 'ec', scalar.to_bytes(32, 'big'), 'prime256v1')
        public = KeyObject('public', 'ec', encodeEcPoint(ecMul(scalar, P256_G)), 'prime256v1')
    else:
        raise JSError(f'Unsupported key type: {kind}', code='ERR_INVALID_ARG_VALUE')
    return JSObject({
        'publicKey': applyEncoding(public, options, 'publicKeyEncoding'),
        'privateKey': applyEncoding(private, options, 'privateKeyEncoding')
    })


class ECDH:
    def __init__(self, curveName):
        if curveName not in ('prime256v1', 'secp256r1', 'P-256'):
            raise JSError('Invalid EC curve name', code='ERR_CRYPTO_UNKNOWN_CURVE')
        self.scalar = None
        self.point = None

    def generateKeys(self, enc=None, fmt=None):
        self.scalar = randomScalar()
        self.point = ecMul(self.scalar, P256_G)
        return self.getPublicKey(enc, fmt)

    def getPublicKey(self, enc=None, fmt=None):
        if self.point is None:
            raise JSError('No public key - did you forget to generate one?', code='ERR_CRYPTO_INVALID_STATE')
        return encodeDigest(encodeEcPoint(self.point), enc)

    def getPrivateKey(self, enc=None):
        if self.scalar is None:
            raise JSError('No private key - did you forget to generate one?', code='ERR_CRYPTO_INVALID_STATE')
        return encodeDigest(self.scalar.to_bytes(32, 'big'), enc)

    def setPrivateKey(self, privateKey, enc=None):
        raw = toBytes(privateKey)
        self.scalar = int.from_bytes(raw, 'big')
        self.point = ecMul(self.scalar, P256_G)
        return self

    def computeSecret(self, peerPublicKey, inputEncoding=None, outputEncoding=None):
        if self.scalar is None:
            raise JSError('No private key - did you forget to generate one?', code='ERR_CRYPTO_INVALID_STATE')
        shared = ecMul(self.scalar, decodeEcPoint(toBytes(peerPublicKey)))
        if shared is None:
            raise JSError('Failed to compute shared secret', code='ERR_CRYPTO_OPERATION_FAILED')
        return encodeDigest(shared[0].to_bytes(32, 'big'), outputEncoding)


def createECDH(curveName):
    return ECDH(curveName)


class Sign:
    def __init__(self, alg):
        self.digestState = hashlib.new(hashName(alg))

    def update(self, data, encoding=None):
        self.digestState.update(toBytes(data))
        return self

    def end(self):
        return None

    def sign(self, key, outputEncoding=None):
        private = createPrivateKey(key)
        if private.asymmetricKeyType != 'ec':
            raise JSError('Unsupported signing key type', code='ERR_CRYPTO_INVALID_KEYTYPE')
        der = ecSignDigest(int.from_bytes(private.raw, 'big'), self.digestState.digest())
        return encodeDigest(der, outputEncoding)


class Verify:
    def __init__(self, alg):
        self.digestState = hashlib.new(hashName(alg))

    def update(self, data, encoding=None):
        self.digestState.update(toBytes(data))
        return self

    def end(self):
        return None

    def verify(self, key, signature, signatureEncoding=None):
        public = createPublicKey(key)
        if public.asymmetricKeyType != 'ec':
            raise JSError('Unsupported verification key type', code='ERR_CRYPTO_INVALID_KEYTYPE')
        return ecVerifyDigest(decodeEcPoint(public.raw), self.digestState.digest(), toBytes(signature))


def createSign(alg):
    return Sign(alg)


def createVerify(alg):
    return Verify(alg)


class X509Certificate:
    def __init__(self, buffer):
        self.raw = toBytes(buffer)
        certificate = _children(_children(self.raw)[0][1])
        tbsFields = _children(certificate[0][1])
        index = 6 if tbsFields[0][0] == 0xA0 else 5
        self.publicKey = parseSpki(tbsFields[index][2])
