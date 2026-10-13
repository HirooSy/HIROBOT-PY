import struct

from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.voip.relay.sctp.crc32c import crc32c


ChunkType = JSObject({
    'DATA': 0,
    'INIT': 1,
    'INIT_ACK': 2,
    'SACK': 3,
    'HEARTBEAT': 4,
    'HEARTBEAT_ACK': 5,
    'ABORT': 6,
    'SHUTDOWN': 7,
    'SHUTDOWN_ACK': 8,
    'ERROR': 9,
    'COOKIE_ECHO': 10,
    'COOKIE_ACK': 11,
    'SHUTDOWN_COMPLETE': 14,
})

COMMON_HEADER_LEN = 12


def pad4(length):
    return (4 - (length % 4)) % 4


def u16(value):
    return struct.pack('>H', value & 0xFFFF)


def u32(value):
    return struct.pack('>I', value & 0xFFFFFFFF)


def buildPacket(sourcePort, destinationPort, verificationTag, chunks):
    header = bytearray(COMMON_HEADER_LEN)
    struct.pack_into('>H', header, 0, sourcePort)
    struct.pack_into('>H', header, 2, destinationPort)
    struct.pack_into('>I', header, 4, verificationTag & 0xFFFFFFFF)

    body = b''.join(chunks)
    packetWithZeroChecksum = bytes(header) + body
    checksum = crc32c(packetWithZeroChecksum)

    out = bytearray(packetWithZeroChecksum)
    struct.pack_into('<I', out, 8, checksum)
    return bytes(out)


def parsePacketHeader(datagram):
    if len(datagram) < COMMON_HEADER_LEN:
        raise JSError('SCTP packet shorter than common header')
    sourcePort = struct.unpack_from('>H', datagram, 0)[0]
    destinationPort = struct.unpack_from('>H', datagram, 2)[0]
    verificationTag = struct.unpack_from('>I', datagram, 4)[0]

    receivedChecksum = struct.unpack_from('<I', datagram, 8)[0]
    forVerification = bytearray(datagram)
    struct.pack_into('>I', forVerification, 8, 0)
    computedChecksum = crc32c(bytes(forVerification))
    if computedChecksum != receivedChecksum:
        raise JSError('SCTP checksum mismatch: got 0x%x, computed 0x%x' % (receivedChecksum, computedChecksum))
    return JSObject({'sourcePort': sourcePort, 'destinationPort': destinationPort,
                     'verificationTag': verificationTag, 'chunksStart': COMMON_HEADER_LEN})


def splitChunks(datagram, chunksStart):
    chunks = []
    offset = chunksStart
    while offset + 4 <= len(datagram):
        type = datagram[offset]
        flags = datagram[offset + 1]
        length = struct.unpack_from('>H', datagram, offset + 2)[0]
        if length < 4 or offset + length > len(datagram):
            break
        chunks.append(JSObject({'type': type, 'flags': flags,
                                'value': bytes(datagram[offset + 4:offset + length])}))
        offset += length + pad4(length)
    return chunks


def encodeChunk(type, flags, value):
    length = 4 + len(value)
    header = bytes([type, flags]) + struct.pack('>H', length & 0xFFFF)
    return header + bytes(value) + bytes(pad4(length))


SUPPORTED_EXTENSIONS_PARAM_TYPE = 0x8008
CHUNK_TYPE_RECONFIG = 130
CHUNK_TYPE_FORWARD_TSN = 192


def buildSupportedExtensionsParam():
    chunkTypes = bytes([CHUNK_TYPE_RECONFIG, CHUNK_TYPE_FORWARD_TSN])
    header = u16(SUPPORTED_EXTENSIONS_PARAM_TYPE) + u16(4 + len(chunkTypes))
    return header + chunkTypes + bytes(pad4(4 + len(chunkTypes)))


def buildInit(initiateTag, advertisedReceiverWindow, outboundStreams, inboundStreams, initialTsn):
    fixed = u32(initiateTag) + u32(advertisedReceiverWindow) + u16(outboundStreams) + u16(inboundStreams) + u32(initialTsn)
    value = fixed + buildSupportedExtensionsParam()
    return encodeChunk(ChunkType['INIT'], 0, value)


def parseInitOrInitAck(chunkValue):
    initiateTag = struct.unpack_from('>I', chunkValue, 0)[0]
    advertisedReceiverWindow = struct.unpack_from('>I', chunkValue, 4)[0]
    outboundStreams = struct.unpack_from('>H', chunkValue, 8)[0]
    inboundStreams = struct.unpack_from('>H', chunkValue, 10)[0]
    initialTsn = struct.unpack_from('>I', chunkValue, 12)[0]

    offset = 16
    stateCookie = None
    while offset + 4 <= len(chunkValue):
        paramType = struct.unpack_from('>H', chunkValue, offset)[0]
        paramLength = struct.unpack_from('>H', chunkValue, offset + 2)[0]
        if paramLength < 4 or offset + paramLength > len(chunkValue):
            break
        paramValue = bytes(chunkValue[offset + 4:offset + paramLength])
        if paramType == 7:
            stateCookie = paramValue
        offset += paramLength + pad4(paramLength)
    return JSObject({'initiateTag': initiateTag, 'advertisedReceiverWindow': advertisedReceiverWindow,
                     'outboundStreams': outboundStreams, 'inboundStreams': inboundStreams,
                     'initialTsn': initialTsn, 'stateCookie': stateCookie})


def buildInitAck(initiateTag, advertisedReceiverWindow, outboundStreams, inboundStreams, initialTsn, stateCookie):
    fixed = u32(initiateTag) + u32(advertisedReceiverWindow) + u16(outboundStreams) + u16(inboundStreams) + u32(initialTsn)
    cookieParamHeader = u16(7) + u16(4 + len(stateCookie))
    cookieParam = cookieParamHeader + bytes(stateCookie) + bytes(pad4(4 + len(stateCookie)))
    return encodeChunk(ChunkType['INIT_ACK'], 0, fixed + cookieParam)


def buildCookieEcho(stateCookie):
    return encodeChunk(ChunkType['COOKIE_ECHO'], 0, stateCookie)


def buildCookieAck():
    return encodeChunk(ChunkType['COOKIE_ACK'], 0, b'')


DATA_HEADER_LEN = 12


def buildData(tsn, streamId, streamSeq, ppid, payload, unordered=False, beginning=True, ending=True):
    flags = 0
    if unordered:
        flags |= 0x04
    if beginning:
        flags |= 0x02
    if ending:
        flags |= 0x01
    value = (u32(tsn) + u16(streamId) + u16(streamSeq) + u32(ppid) + bytes(payload))
    return encodeChunk(ChunkType['DATA'], flags, value)


def parseData(chunkFlags, chunkValue):
    return JSObject({
        'unordered': (chunkFlags & 0x04) != 0,
        'beginning': (chunkFlags & 0x02) != 0,
        'ending': (chunkFlags & 0x01) != 0,
        'tsn': struct.unpack_from('>I', chunkValue, 0)[0],
        'streamId': struct.unpack_from('>H', chunkValue, 4)[0],
        'streamSeq': struct.unpack_from('>H', chunkValue, 6)[0],
        'ppid': struct.unpack_from('>I', chunkValue, 8)[0],
        'payload': bytes(chunkValue[DATA_HEADER_LEN:]),
    })


def buildSack(cumulativeTsnAck, advertisedReceiverWindow, gapAckBlocks=None):
    if gapAckBlocks is None:
        gapAckBlocks = []
    value = bytearray(12 + len(gapAckBlocks) * 4)
    struct.pack_into('>I', value, 0, cumulativeTsnAck & 0xFFFFFFFF)
    struct.pack_into('>I', value, 4, advertisedReceiverWindow & 0xFFFFFFFF)
    struct.pack_into('>H', value, 8, len(gapAckBlocks) & 0xFFFF)
    struct.pack_into('>H', value, 10, 0)
    offset = 12
    for block in gapAckBlocks:
        struct.pack_into('>H', value, offset, block.get('start') & 0xFFFF)
        struct.pack_into('>H', value, offset + 2, block.get('end') & 0xFFFF)
        offset += 4
    return encodeChunk(ChunkType['SACK'], 0, bytes(value))


def parseSack(chunkValue):
    cumulativeTsnAck = struct.unpack_from('>I', chunkValue, 0)[0]
    advertisedReceiverWindow = struct.unpack_from('>I', chunkValue, 4)[0]
    gapAckBlocks = []
    if len(chunkValue) >= 12:
        numGapBlocks = struct.unpack_from('>H', chunkValue, 8)[0]
        offset = 12
        i = 0
        while i < numGapBlocks and offset + 4 <= len(chunkValue):
            gapAckBlocks.append(JSObject({
                'start': struct.unpack_from('>H', chunkValue, offset)[0],
                'end': struct.unpack_from('>H', chunkValue, offset + 2)[0],
            }))
            offset += 4
            i += 1
    return JSObject({'cumulativeTsnAck': cumulativeTsnAck, 'advertisedReceiverWindow': advertisedReceiverWindow,
                     'gapAckBlocks': gapAckBlocks})


def buildHeartbeat(heartbeatInfo):
    paramHeader = u16(1) + u16(4 + len(heartbeatInfo))
    return encodeChunk(ChunkType['HEARTBEAT'], 0,
                       paramHeader + bytes(heartbeatInfo) + bytes(pad4(4 + len(heartbeatInfo))))


def buildHeartbeatAck(heartbeatParamValue):
    paramHeader = u16(1) + u16(4 + len(heartbeatParamValue))
    return encodeChunk(ChunkType['HEARTBEAT_ACK'], 0,
                       paramHeader + bytes(heartbeatParamValue) + bytes(pad4(4 + len(heartbeatParamValue))))


def parseHeartbeatInfo(chunkValue):
    paramLength = struct.unpack_from('>H', chunkValue, 2)[0]
    return bytes(chunkValue[4:paramLength])


def buildAbort(reasonText=''):
    return encodeChunk(ChunkType['ABORT'], 0, reasonText.encode('utf-8'))
