import struct

from baileys.foundation.node_runtime import JSObject


CIPHER_SUITE_ECDHE_ECDSA_AES_128_GCM_SHA256 = 0xC02B
COMPRESSION_METHOD_NULL = 0
SIGNATURE_SCHEME_ECDSA_SECP256R1_SHA256 = 0x0403
EXTENSION_SUPPORTED_GROUPS = 10
EXTENSION_SIGNATURE_ALGORITHMS = 13
NAMED_CURVE_SECP256R1 = 23


def u16(n):
    return struct.pack('>H', n & 0xFFFF)


def u8LenPrefixed(buf):
    return bytes([len(buf)]) + bytes(buf)


def u16LenPrefixed(buf):
    return u16(len(buf)) + bytes(buf)


def u24(value):
    return bytes([(value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF])


def buildClientHello(clientRandom, sessionId=b'', cookie=b''):
    cipherSuites = u16LenPrefixed(u16(CIPHER_SUITE_ECDHE_ECDSA_AES_128_GCM_SHA256))
    compressionMethods = u8LenPrefixed(bytes([COMPRESSION_METHOD_NULL]))
    supportedGroupsExt = (u16(EXTENSION_SUPPORTED_GROUPS)
                          + u16LenPrefixed(u16LenPrefixed(u16(NAMED_CURVE_SECP256R1))))
    sigAlgsExt = (u16(EXTENSION_SIGNATURE_ALGORITHMS)
                  + u16LenPrefixed(u16LenPrefixed(u16(SIGNATURE_SCHEME_ECDSA_SECP256R1_SHA256))))
    extensions = u16LenPrefixed(supportedGroupsExt + sigAlgsExt)
    return (bytes([0xFE, 0xFD]) + clientRandom + u8LenPrefixed(sessionId) + u8LenPrefixed(cookie)
            + cipherSuites + compressionMethods + extensions)


def parseHelloVerifyRequest(body):
    cookieLen = body[2]
    cookie = bytes(body[3:3 + cookieLen])
    return JSObject({'cookie': cookie})


def parseServerHello(body):
    offset = 2
    serverRandom = bytes(body[offset:offset + 32])
    offset += 32
    sessionIdLen = body[offset]
    offset += 1 + sessionIdLen
    cipherSuite = struct.unpack_from('>H', body, offset)[0]
    offset += 2
    compressionMethod = body[offset]
    offset += 1
    return JSObject({'serverRandom': serverRandom, 'cipherSuite': cipherSuite,
                     'compressionMethod': compressionMethod})


def parseCertificate(body):
    listLen = (body[0] << 16) | (body[1] << 8) | body[2]
    certificates = []
    offset = 3
    end = 3 + listLen
    while offset < end:
        certLen = (body[offset] << 16) | (body[offset + 1] << 8) | body[offset + 2]
        offset += 3
        certificates.append(bytes(body[offset:offset + certLen]))
        offset += certLen
    return JSObject({'certificates': certificates})


def buildCertificate(certDer):
    oneCert = u24(len(certDer)) + bytes(certDer)
    listLen = len(oneCert)
    return u24(listLen) + oneCert


def parseClientHello(body):
    offset = 2
    clientRandom = bytes(body[offset:offset + 32])
    offset += 32
    sessionIdLen = body[offset]
    offset += 1
    sessionId = bytes(body[offset:offset + sessionIdLen])
    offset += sessionIdLen
    cookieLen = body[offset]
    offset += 1
    cookie = bytes(body[offset:offset + cookieLen])
    offset += cookieLen
    cipherSuitesLen = struct.unpack_from('>H', body, offset)[0]
    offset += 2
    cipherSuites = []
    for i in range(0, cipherSuitesLen, 2):
        cipherSuites.append(struct.unpack_from('>H', body, offset + i)[0])
    offset += cipherSuitesLen
    compressionMethodsLen = body[offset]
    offset += 1
    compressionMethods = list(bytes(body[offset:offset + compressionMethodsLen]))
    return JSObject({'clientRandom': clientRandom, 'sessionId': sessionId, 'cookie': cookie,
                     'cipherSuites': cipherSuites, 'compressionMethods': compressionMethods})


def buildHelloVerifyRequest(cookie):
    return bytes([0xFE, 0xFD]) + u8LenPrefixed(cookie)


def buildServerHello(serverRandom, sessionId=b'', cipherSuite=None, compressionMethod=COMPRESSION_METHOD_NULL):
    return (bytes([0xFE, 0xFD]) + bytes(serverRandom) + u8LenPrefixed(sessionId)
            + u16(cipherSuite) + bytes([compressionMethod]))


def buildServerHelloDone():
    return b''


def parseServerKeyExchange(body):
    offset = 0
    curveType = body[offset]
    offset += 1
    namedCurve = struct.unpack_from('>H', body, offset)[0]
    offset += 2
    pointLen = body[offset]
    offset += 1
    publicKeyPoint = bytes(body[offset:offset + pointLen])
    offset += pointLen
    signatureScheme = struct.unpack_from('>H', body, offset)[0]
    offset += 2
    sigLen = struct.unpack_from('>H', body, offset)[0]
    offset += 2
    signature = bytes(body[offset:offset + sigLen])
    offset += sigLen
    return JSObject({'curveType': curveType, 'namedCurve': namedCurve, 'publicKeyPoint': publicKeyPoint,
                     'signatureScheme': signatureScheme, 'signature': signature})


def buildServerKeyExchange(publicKeyPoint, signature):
    return (bytes([3]) + u16(NAMED_CURVE_SECP256R1) + u8LenPrefixed(publicKeyPoint)
            + u16(SIGNATURE_SCHEME_ECDSA_SECP256R1_SHA256) + u16LenPrefixed(signature))


def buildClientKeyExchange(publicKeyPoint):
    return u8LenPrefixed(publicKeyPoint)


def parseClientKeyExchange(body):
    length = body[0]
    return JSObject({'publicKeyPoint': bytes(body[1:1 + length])})


def buildFinished(verifyData):
    return verifyData


def parseFinished(body):
    return JSObject({'verifyData': body})


def buildCertificateVerify(signatureScheme, signature):
    return u16(signatureScheme) + u16LenPrefixed(signature)
