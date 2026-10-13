from baileys.foundation.node_crypto import createHmac
from baileys.foundation.node_runtime import JSObject


def pHashSha256(secret, seed, length):
    out = []
    outLen = 0
    a = seed
    while outLen < length:
        a = createHmac('sha256', secret).update(a).digest()
        chunk = createHmac('sha256', secret).update(a + seed).digest()
        out.append(chunk)
        outLen += len(chunk)
    return b''.join(out)[:length]


def prf(secret, label, seed, length):
    return pHashSha256(secret, label.encode('ascii') + seed, length)


def deriveMasterSecret(preMasterSecret, clientRandom, serverRandom):
    return prf(preMasterSecret, 'master secret', clientRandom + serverRandom, 48)


def deriveKeyBlock(masterSecret, clientRandom, serverRandom, params):
    keyLen = params.get('keyLen')
    fixedIvLen = params.get('fixedIvLen')
    needed = 2 * keyLen + 2 * fixedIvLen
    block = prf(masterSecret, 'key expansion', serverRandom + clientRandom, needed)
    offset = 0

    def take(n):
        nonlocal offset
        b = block[offset:offset + n]
        offset += n
        return b

    return JSObject({
        'clientWriteKey': take(keyLen),
        'serverWriteKey': take(keyLen),
        'clientWriteIv': take(fixedIvLen),
        'serverWriteIv': take(fixedIvLen),
    })


def deriveVerifyData(masterSecret, label, handshakeHash):
    return prf(masterSecret, label, handshakeHash, 12)
