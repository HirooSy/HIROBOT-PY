import struct

from baileys.foundation.node_crypto import createHmac, randomBytes
from baileys.foundation.node_runtime import JSObject
from baileys.voip.bytes import readUInt16BE, readUInt32BE, readBigUInt64BE, writeUInt16BE, writeUInt32BE


STUN_MAGIC_COOKIE = 0x2112A442
STUN_FINGERPRINT_XOR = 0x5354554E
STUN_BINDING_REQUEST = 0x0001
STUN_ALLOCATE_REQUEST = 0x0003
WHATSAPP_PING = 0x0801
WHATSAPP_PONG = 0x0802
ATTR_USERNAME = 0x0006
ATTR_MESSAGE_INTEGRITY = 0x0008
ATTR_LIFETIME = 0x000D
ATTR_XOR_RELAYED_ADDRESS = 0x0016
ATTR_REQUESTED_TRANSPORT = 0x0019
ATTR_PRIORITY = 0x0024

ATTR_SENDER_SUBSCRIPTIONS = 0x4000
ATTR_SSRC_LIST = 0x4024
ATTR_ICE_CONTROLLED = 0x8029
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_ICE_CONTROLLING = 0x802A
ATTR_FINGERPRINT = 0x8028
DEFAULT_ICE_PRIORITY = 16777215


def generateTransactionId():
    return randomBytes(12)


def encodeAttribute(attrType, data):
    header = bytearray(4)
    writeUInt16BE(header, attrType, 0)
    writeUInt16BE(header, len(data), 2)
    padding = (4 - (len(data) % 4)) % 4
    return bytes(header) + bytes(data) + bytes(padding)


def crc32(data):
    crc = 0xFFFFFFFF
    for i in range(len(data)):
        crc ^= data[i]
        for j in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc >>= 1
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


def buildStunMessage(msgType, attrs, transactionId, integrityKey, includeFingerprint=True):
    attrsData = bytes(attrs)
    if integrityKey is not None:
        msgLenForHmac = len(attrsData) + 24
        hmacHeader = bytearray(20)
        writeUInt16BE(hmacHeader, msgType, 0)
        writeUInt16BE(hmacHeader, msgLenForHmac, 2)
        writeUInt32BE(hmacHeader, STUN_MAGIC_COOKIE, 4)
        hmacHeader[8:8 + len(transactionId)] = transactionId
        hmacInput = bytes(hmacHeader) + attrsData
        hmac = createHmac('sha1', integrityKey).update(hmacInput).digest()
        miAttr = encodeAttribute(ATTR_MESSAGE_INTEGRITY, hmac)
        attrsData = attrsData + miAttr
    if includeFingerprint:
        msgLenForCrc = len(attrsData) + 8
        crcHeader = bytearray(20)
        writeUInt16BE(crcHeader, msgType, 0)
        writeUInt16BE(crcHeader, msgLenForCrc, 2)
        writeUInt32BE(crcHeader, STUN_MAGIC_COOKIE, 4)
        crcHeader[8:8 + len(transactionId)] = transactionId
        crcInput = bytes(crcHeader) + attrsData
        fingerprint = (crc32(crcInput) ^ STUN_FINGERPRINT_XOR) & 0xFFFFFFFF
        fpBuf = bytearray(4)
        writeUInt32BE(fpBuf, fingerprint, 0)
        fpAttr = encodeAttribute(ATTR_FINGERPRINT, bytes(fpBuf))
        attrsData = attrsData + fpAttr
    header = bytearray(20)
    writeUInt16BE(header, msgType, 0)
    writeUInt16BE(header, len(attrsData), 2)
    writeUInt32BE(header, STUN_MAGIC_COOKIE, 4)
    header[8:8 + len(transactionId)] = transactionId
    return bytes(header) + attrsData


def encodeVarint(value):
    out = []
    v = value & 0xFFFFFFFF
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def encodeProtobufVarintField(fieldNumber, value):
    return encodeVarint((fieldNumber << 3) | 0) + encodeVarint(value)


def encodeProtobufLengthDelimited(fieldNumber, data):
    return encodeVarint((fieldNumber << 3) | 2) + encodeVarint(len(data)) + bytes(data)


def buildSenderSubscriptions(ssrc):
    inner = (encodeProtobufVarintField(3, ssrc)
             + encodeProtobufVarintField(5, 0)
             + encodeProtobufVarintField(6, 0))
    return encodeProtobufLengthDelimited(1, inner)


def buildSSRCSubscriptionList(selfSsrcs, peerSsrcs, selfPid, peerPid):
    entries = []
    for ssrc in selfSsrcs:
        if ssrc == 0:
            continue
        inner = (encodeProtobufVarintField(1, selfPid)
                 + encodeProtobufVarintField(2, 1)
                 + encodeProtobufVarintField(3, ssrc))
        entries.append(encodeProtobufLengthDelimited(1, inner))
    for peerSsrc in peerSsrcs:
        if peerSsrc == 0:
            continue
        inner = (encodeProtobufVarintField(1, peerPid)
                 + encodeProtobufVarintField(2, 1)
                 + encodeProtobufVarintField(3, peerSsrc))
        entries.append(encodeProtobufLengthDelimited(1, inner))
    return b''.join(entries)


WASM_STREAM_DESCRIPTOR_PLAN = [
    JSObject({'participant': 0, 'layer': 0}),
    JSObject({'participant': 0, 'layer': 1}),
    JSObject({'participant': 0, 'layer': 2}),
    JSObject({'participant': 1, 'layer': 0}),
    JSObject({'participant': 1, 'layer': 1}),
    JSObject({'participant': 1, 'layer': 2}),
    JSObject({'participant': 2, 'layer': 0}),
    JSObject({'participant': 2, 'layer': 1}),
    JSObject({'participant': 2, 'layer': 2}),
]


def createWasmStreamDescriptors(streamSsrcs):
    entries = []
    for i in range(len(streamSsrcs)):
        ssrc = streamSsrcs[i]
        if ssrc == 0:
            continue
        plan = WASM_STREAM_DESCRIPTOR_PLAN[i]
        parts = []
        if plan.get('participant') != 0:
            parts.append(encodeProtobufVarintField(1, plan.get('participant')))
        if plan.get('layer') != 0:
            parts.append(encodeProtobufVarintField(2, plan.get('layer')))
        parts.append(encodeProtobufVarintField(3, ssrc))
        entries.append(encodeProtobufLengthDelimited(1, b''.join(parts)))
    return b''.join(entries)


def encodeXorRelayedAddress(ip, port):
    data = bytearray(8)
    data[0] = 0x00
    data[1] = 0x01
    writeUInt16BE(data, port ^ (STUN_MAGIC_COOKIE >> 16), 2)
    parts = [int(p) for p in ip.split('.')]
    ipNum = ((parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]) & 0xFFFFFFFF
    writeUInt32BE(data, (ipNum ^ STUN_MAGIC_COOKIE) & 0xFFFFFFFF, 4)
    return bytes(data)


def buildAllocateForRelay(relayToken, streamDescriptors, hmacKey, relayIp, relayPort):
    transactionId = generateTransactionId()
    parts = []
    parts.append(encodeAttribute(ATTR_SENDER_SUBSCRIPTIONS, relayToken))
    parts.append(encodeAttribute(ATTR_SSRC_LIST, streamDescriptors))
    parts.append(encodeAttribute(ATTR_XOR_RELAYED_ADDRESS, encodeXorRelayedAddress(relayIp, relayPort)))
    attrs = b''.join(parts)
    return buildStunMessage(STUN_ALLOCATE_REQUEST, attrs, transactionId, hmacKey, False)


def buildBindingRequest(username, hmacKey, senderSubscriptions, includeIceControllingOrOptions=True):
    if isinstance(includeIceControllingOrOptions, bool):
        options = JSObject({'iceRole': 'controlling' if includeIceControllingOrOptions else 'none'})
    else:
        options = JSObject() if includeIceControllingOrOptions is None else includeIceControllingOrOptions
    iceRole = options.get('iceRole') if options.get('iceRole') is not None else 'controlling'
    includePriority = options.get('includePriority') if options.get('includePriority') is not None else True
    includeUsername = options.get('includeUsername') if options.get('includeUsername') is not None else True

    transactionId = generateTransactionId()
    usernameAttr = encodeAttribute(ATTR_USERNAME, username) if includeUsername else None
    priorityAttr = None
    if includePriority:
        priorityBuf = bytearray(4)
        writeUInt32BE(priorityBuf, DEFAULT_ICE_PRIORITY, 0)
        priorityAttr = encodeAttribute(ATTR_PRIORITY, bytes(priorityBuf))

    parts = []
    if usernameAttr is not None:
        parts.append(usernameAttr)
    if priorityAttr is not None:
        parts.append(priorityAttr)
    if iceRole == 'controlling' or iceRole == 'controlled':
        tieBreaker = randomBytes(8)
        attrType = ATTR_ICE_CONTROLLED if iceRole == 'controlled' else ATTR_ICE_CONTROLLING
        parts.append(encodeAttribute(attrType, tieBreaker))
    if senderSubscriptions is not None and len(senderSubscriptions) > 0:
        parts.append(encodeAttribute(ATTR_SENDER_SUBSCRIPTIONS, senderSubscriptions))
    attrs = b''.join(parts)
    return buildStunMessage(STUN_BINDING_REQUEST, attrs, transactionId, hmacKey, True)


def encodeXorMappedAddressV4(ip, port):
    octets = [int(s) & 0xFF for s in ip.split('.')]
    buf = bytearray(8)
    buf[0] = 0x00
    buf[1] = 0x01
    xPort = port ^ (STUN_MAGIC_COOKIE >> 16)
    writeUInt16BE(buf, xPort, 2)
    cookieBytes = bytearray(4)
    writeUInt32BE(cookieBytes, STUN_MAGIC_COOKIE, 0)
    for i in range(4):
        buf[4 + i] = octets[i] ^ cookieBytes[i]
    return bytes(buf)


def buildBindingSuccessResponse(transactionId, mappedIp, mappedPort, hmacKey):
    xorAddr = encodeXorMappedAddressV4(mappedIp, mappedPort)
    attrs = encodeAttribute(ATTR_XOR_MAPPED_ADDRESS, xorAddr)
    STUN_BINDING_SUCCESS_RESPONSE = 0x0101
    return buildStunMessage(STUN_BINDING_SUCCESS_RESPONSE, attrs, transactionId, hmacKey, True)


def buildBindingRequestWithSubs(username, hmacKey, senderSubscriptions, includeIceControlling, includeFingerprint):
    transactionId = generateTransactionId()
    parts = []
    if username is not None and len(username) > 0:
        parts.append(encodeAttribute(ATTR_USERNAME, username))
    priorityBuf = bytearray(4)
    writeUInt32BE(priorityBuf, DEFAULT_ICE_PRIORITY, 0)
    parts.append(encodeAttribute(ATTR_PRIORITY, bytes(priorityBuf)))
    if includeIceControlling:
        tieBreaker = randomBytes(8)
        parts.append(encodeAttribute(ATTR_ICE_CONTROLLING, tieBreaker))
    if senderSubscriptions is not None and len(senderSubscriptions) > 0:
        parts.append(encodeAttribute(ATTR_SENDER_SUBSCRIPTIONS, senderSubscriptions))
    attrs = b''.join(parts)
    return buildStunMessage(STUN_BINDING_REQUEST, attrs, transactionId, hmacKey, includeFingerprint)


def buildMinimalBindingWithSubs(senderSubscriptions, includeFingerprint=False):
    transactionId = generateTransactionId()
    attrs = encodeAttribute(ATTR_SENDER_SUBSCRIPTIONS, senderSubscriptions)
    return buildStunMessage(STUN_BINDING_REQUEST, attrs, transactionId, None, includeFingerprint)


def buildMinimalAllocateWithSubs(senderSubscriptions, includeFingerprint=False):
    transactionId = generateTransactionId()
    attrs = encodeAttribute(ATTR_SENDER_SUBSCRIPTIONS, senderSubscriptions)
    return buildStunMessage(STUN_ALLOCATE_REQUEST, attrs, transactionId, None, includeFingerprint)


def buildAllocateRequest(username, hmacKey, lifetime=3600):
    transactionId = generateTransactionId()
    parts = []
    parts.append(encodeAttribute(ATTR_REQUESTED_TRANSPORT, bytes([17, 0, 0, 0])))
    parts.append(encodeAttribute(ATTR_USERNAME, username))
    lifetimeBuf = bytearray(4)
    writeUInt32BE(lifetimeBuf, lifetime, 0)
    parts.append(encodeAttribute(ATTR_LIFETIME, bytes(lifetimeBuf)))
    attrs = b''.join(parts)
    return buildStunMessage(STUN_ALLOCATE_REQUEST, attrs, transactionId, hmacKey, True)


def buildRelayBindingSuccess(transactionId, hmacKey):
    return buildStunMessage(0x0101, b'', transactionId, hmacKey, True)


def buildWhatsAppPing():
    transactionId = generateTransactionId()
    header = bytearray(20)
    writeUInt16BE(header, WHATSAPP_PING, 0)
    writeUInt16BE(header, 0, 2)
    writeUInt32BE(header, STUN_MAGIC_COOKIE, 4)
    header[8:8 + len(transactionId)] = transactionId
    return bytes(header)


def isStunPacket(data):
    if len(data) < 2:
        return False
    if (data[0] & 0xC0) != 0:
        return False
    type = readUInt16BE(data, 0)
    if type == WHATSAPP_PING or type == WHATSAPP_PONG:
        return True
    return len(data) >= 8 and readUInt32BE(data, 4) == STUN_MAGIC_COOKIE


def isRtpPacket(data):
    if len(data) < 2:
        return False
    return (data[0] & 0xC0) == 0x80


STUN_ATTR_NAMES = JSObject({
    0x0001: 'MAPPED-ADDRESS',
    0x0006: 'USERNAME',
    0x0008: 'MESSAGE-INTEGRITY',
    0x0009: 'ERROR-CODE',
    0x000A: 'UNKNOWN-ATTRIBUTES',
    0x0014: 'REALM',
    0x0015: 'NONCE',
    0x0019: 'REQUESTED-TRANSPORT',
    0x0020: 'XOR-MAPPED-ADDRESS',
    0x0024: 'PRIORITY',
    0x0025: 'USE-CANDIDATE',
    0x4000: 'SENDER-SUBSCRIPTIONS',
    0x4001: 'RECEIVER-SUBSCRIPTION',
    0x4002: 'SUBSCRIPTION-ACK',
    0x8022: 'SOFTWARE',
    0x8028: 'FINGERPRINT',
    0x8029: 'ICE-CONTROLLED',
    0x802A: 'ICE-CONTROLLING',
    0x4033: 'STABLE-ROUTING-CONN-ID',
})

METHOD_NAMES = {
    0x001: 'binding',
    0x003: 'allocate',
    0x004: 'refresh',
    0x006: 'send',
    0x007: 'data',
    0x008: 'create-permission',
    0x009: 'channel-bind',
}


def parseStunResponse(data):
    if len(data) < 20:
        return None
    cookie = readUInt32BE(data, 4)
    if cookie != STUN_MAGIC_COOKIE:
        msgType = readUInt16BE(data, 0)
        if msgType == 0x0801 or msgType == 0x0802:
            return JSObject({
                'rawType': msgType,
                'method': 'wa-ping' if msgType == 0x0801 else 'wa-pong',
                'stunClass': 'indication',
                'isSuccess': False,
                'isError': False,
                'transactionId': bytes(data[8:20]).hex(),
                'length': len(data),
                'attributes': [],
            })
        return None

    rawType = readUInt16BE(data, 0)
    msgLength = readUInt16BE(data, 2)
    transactionId = bytes(data[8:20]).hex()
    c0 = (rawType >> 4) & 0x1
    c1 = (rawType >> 8) & 0x1
    stunClassNum = (c1 << 1) | c0
    stunClass = ['request', 'indication', 'success', 'error'][stunClassNum] or 'unknown'
    method_bits = ((rawType & 0x3E00) >> 2) | ((rawType & 0x00E0) >> 1) | (rawType & 0x000F)
    method = METHOD_NAMES.get(method_bits, 'unknown')
    if rawType == 0x0801:
        method = 'wa-ping'
    if rawType == 0x0802:
        method = 'wa-pong'

    attributes = []
    errorCode = None
    errorReason = None
    stableRoutingConnId = None
    offset = 20
    while offset + 4 <= 20 + msgLength and offset + 4 <= len(data):
        attrType = readUInt16BE(data, offset)
        attrLength = readUInt16BE(data, offset + 2)
        attrEnd = offset + 4 + attrLength
        if attrEnd > len(data):
            break
        attrData = bytes(data[offset + 4:attrEnd])
        attributes.append(JSObject({
            'type': attrType,
            'typeName': STUN_ATTR_NAMES[attrType] or ('0x%04x' % attrType),
            'length': attrLength,
            'data': attrData,
        }))
        if attrType == 0x0009 and attrLength >= 4:
            errorClass = attrData[2] & 0x07
            errorNumber = attrData[3]
            errorCode = errorClass * 100 + errorNumber
            if attrLength > 4:
                errorReason = attrData[4:].decode('utf-8', errors='replace')
        if attrType == 0x4033 and stunClass == 'success' and attrLength == 8:
            stableRoutingConnId = readBigUInt64BE(attrData, 0)
        offset = attrEnd + ((4 - (attrLength % 4)) % 4)

    return JSObject({
        'rawType': rawType,
        'method': method,
        'stunClass': stunClass,
        'isSuccess': stunClass == 'success',
        'isError': stunClass == 'error',
        'errorCode': errorCode,
        'errorReason': errorReason,
        'stableRoutingConnId': stableRoutingConnId,
        'transactionId': transactionId,
        'length': len(data),
        'attributes': attributes,
    })


def formatStunResponse(info):
    result = 'STUN %s %s (0x%04x, %dB)' % (info.get('method'), info.get('stunClass'), info.get('rawType'), info.get('length'))
    if info.get('isError') and info.get('errorCode'):
        result += ' ERROR %d' % info.get('errorCode')
        if info.get('errorReason'):
            result += ': ' + info.get('errorReason')
    if len(info.get('attributes')) > 0:
        attrNames = ', '.join(a.get('typeName') for a in info.get('attributes'))
        result += ' [%s]' % attrNames
    return result


def classifyPacket(data):
    if len(data) < 2:
        return 'tiny(%dB)' % len(data)
    firstByte = data[0]
    twoBits = (firstByte & 0xC0) >> 6
    if twoBits == 0:
        info = parseStunResponse(data)
        if info is not None:
            return formatStunResponse(info)
        msgType = (data[0] << 8) | data[1]
        return 'STUN? 0x%x (%dB)' % (msgType, len(data))
    if twoBits == 2:
        pt = data[1] & 0x7F
        marker = (data[1] >> 7) & 1
        seq = ((data[2] << 8) | data[3]) if len(data) >= 4 else 0
        return 'RTP/SRTP PT=%d M=%d seq=%d (%dB)' % (pt, marker, seq, len(data))
    if twoBits == 1:
        return 'DTLS? 0x%x (%dB)' % (firstByte, len(data))
    return 'unknown 0x%x (%dB)' % (firstByte, len(data))
