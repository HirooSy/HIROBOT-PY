from baileys_lite.foundation.node_crypto import createHash, createPrivateKey, createPublicKey, diffieHellman, generateKeyPairSync, randomBytes
from baileys_lite.foundation.node_runtime import JSError, JSObject

P = (1 << 255) - 19
L = (1 << 252) + 27742317777372353535851937790883648493

D = 37095705934669439343138083508754565189542113879843219016388785533085940283555
SQRT_M1 = 19681161376707505956807079304988542015446066515923890162744021073123829784752
BASE_Y = 46316835694926478169428394003475163141307993866256225615783033603165251855960
BASE_X = 15112221349535400772501151409588531511454012693041857206046113283949847762202


def mod(a, m=P):
    return a % m


def pow(b, e, m=P):
    r = 1
    b = mod(b, m)
    while e > 0:
        if e & 1:
            r = (r * b) % m
        b = (b * b) % m
        e >>= 1
    return r


def inv(a):
    return pow(a, P - 2)


def bytesToNumLE(b):
    return int.from_bytes(bytes(b), 'little')


def numToBytesLE(n, length):
    return bytearray((n & ((1 << (8 * length)) - 1)).to_bytes(length, 'little'))


ZERO = [0, 1, 1, 0]
BASE = [BASE_X, BASE_Y, 1, mod(BASE_X * BASE_Y)]


def pointAdd(p, q):
    X1, Y1, Z1, T1 = p
    X2, Y2, Z2, T2 = q
    A = mod((Y1 - X1) * (Y2 - X2))
    B = mod((Y1 + X1) * (Y2 + X2))
    C = mod(2 * D * T1 * T2)
    Dd = mod(2 * Z1 * Z2)
    E = B - A
    F = Dd - C
    G = Dd + C
    H = B + A
    return [mod(E * F), mod(G * H), mod(F * G), mod(E * H)]


def scalarMult(point, scalar):
    r = ZERO
    q = point
    while scalar > 0:
        if scalar & 1:
            r = pointAdd(r, q)
        q = pointAdd(q, q)
        scalar >>= 1
    return r


def encodePoint(p):
    zi = inv(p[2])
    x = mod(p[0] * zi)
    y = mod(p[1] * zi)
    out = numToBytesLE(y, 32)
    if x & 1:
        out[31] |= 0x80
    return bytes(out)


def decodePoint(bytesIn):
    b = bytearray(bytesIn)
    sign = (b[31] & 0x80) >> 7
    b[31] &= 0x7f
    y = bytesToNumLE(b)
    if y >= P:
        return None
    y2 = mod(y * y)
    u = mod(y2 - 1)
    v = mod(D * y2 + 1)

    v3 = mod(v * v * v)
    v7 = mod(v3 * v3 * v)
    x = mod(u * v3 * pow(mod(u * v7), (P - 5) // 8))
    vx2 = mod(v * x * x)
    if vx2 != mod(u):
        if vx2 != mod(-u):
            return None
        x = mod(x * SQRT_M1)
    if (x & 1) != sign:
        x = mod(-x)
    return [x, y, 1, mod(x * y)]


def sha512(*parts):
    h = createHash('sha512')
    for p in parts:
        h.update(p)
    return h.digest()


def clamp(key):
    k = bytearray(bytes(key))
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return k


PUB_DER = bytes.fromhex('302a300506032b656e032100')
PRIV_DER = bytes.fromhex('302e020100300506032b656e04220420')


def sharedKey(priv, pub):
    return diffieHellman(JSObject({
        'privateKey': createPrivateKey(JSObject({'key': PRIV_DER + bytes(priv), 'format': 'der', 'type': 'pkcs8'})),
        'publicKey': createPublicKey(JSObject({'key': PUB_DER + bytes(pub), 'format': 'der', 'type': 'spki'}))
    }))


def randomX25519():
    pair = generateKeyPairSync('x25519', JSObject({
        'publicKeyEncoding': JSObject({'format': 'der', 'type': 'spki'}),
        'privateKeyEncoding': JSObject({'format': 'der', 'type': 'pkcs8'})
    }))
    publicKey = pair.get('publicKey')
    privateKey = pair.get('privateKey')
    return JSObject({
        'pub': publicKey[len(PUB_DER):len(PUB_DER) + 32],
        'priv': privateKey[len(PRIV_DER):len(PRIV_DER) + 32]
    })


def publicFromPrivate(seed):
    if len(seed) != 32:
        raise JSError('wrong seed length')
    priv = bytes(seed)

    base = bytearray(32)
    base[0] = 9
    pub = bytearray(sharedKey(priv, bytes(base)))
    pub[31] &= 0x7f
    return bytes(pub)


def montToEdY(pk):
    b = bytearray(bytes(pk))
    b[31] &= 0x7f
    u = mod(bytesToNumLE(b))
    y = mod((u - 1) * inv(mod(u + 1)))
    return numToBytesLE(y, 32)


def edPublicKeyFromSeed(seed):
    h = sha512(bytes(seed))
    a = clamp(h[:32])
    return encodePoint(scalarMult(BASE, bytesToNumLE(a)))


def sign(secretKey, msg):
    if len(secretKey) != 32:
        raise JSError('wrong secret key length')
    a = clamp(secretKey)
    aNum = bytesToNumLE(a)
    A = encodePoint(scalarMult(BASE, aNum))
    signBit = A[31] & 0x80

    r = mod(bytesToNumLE(sha512(bytes(a), msg)), L)
    R = encodePoint(scalarMult(BASE, r))
    Acompressed = bytes(A)

    h = mod(bytesToNumLE(sha512(R, Acompressed, msg)), L)
    S = mod(r + h * aNum, L)
    sig = bytearray(R + bytes(numToBytesLE(S, 32)))
    sig[63] |= signBit
    return bytes(sig)


def verify(publicKey, msg, signature):
    if len(signature) != 64:
        raise JSError('wrong signature length')
    if len(publicKey) != 32:
        raise JSError('wrong public key length')
    sig = bytearray(bytes(signature))
    edpk = bytearray(montToEdY(publicKey))
    edpk[31] |= sig[63] & 0x80
    sig[63] &= 0x7f
    A = decodePoint(bytes(edpk))
    if not A:
        return False
    R = bytes(sig[0:32])
    S = bytesToNumLE(sig[32:])
    h = mod(bytesToNumLE(sha512(R, bytes(edpk), msg)), L)

    negA = [mod(-A[0]), A[1], A[2], mod(-A[3])]
    check = pointAdd(scalarMult(negA, h), scalarMult(BASE, S))
    return encodePoint(check) == R
