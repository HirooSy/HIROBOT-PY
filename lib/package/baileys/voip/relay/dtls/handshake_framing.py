import struct

from baileys.foundation.node_runtime import JSObject


HandshakeType = JSObject({
    'HELLO_REQUEST': 0,
    'CLIENT_HELLO': 1,
    'SERVER_HELLO': 2,
    'HELLO_VERIFY_REQUEST': 3,
    'CERTIFICATE': 11,
    'SERVER_KEY_EXCHANGE': 12,
    'CERTIFICATE_REQUEST': 13,
    'SERVER_HELLO_DONE': 14,
    'CERTIFICATE_VERIFY': 15,
    'CLIENT_KEY_EXCHANGE': 16,
    'FINISHED': 20,
})

HANDSHAKE_HEADER_LEN = 12


def uint24(value):
    return bytes([(value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF])


def readUint24BE(buf, offset):
    return (buf[offset] << 16) | (buf[offset + 1] << 8) | buf[offset + 2]


def makeHeader(msgType, totalLength, messageSeq, fragmentOffset, fragmentLength):
    return (bytes([msgType]) + uint24(totalLength) + struct.pack('>H', messageSeq & 0xFFFF)
            + uint24(fragmentOffset) + uint24(fragmentLength))


def encodeHandshakeMessage(msgType, messageSeq, body, maxFragmentSize=1200):
    totalLength = len(body)
    if totalLength <= maxFragmentSize:
        return [makeHeader(msgType, totalLength, messageSeq, 0, totalLength) + bytes(body)]
    fragments = []
    offset = 0
    while offset < totalLength:
        fragLen = min(maxFragmentSize, totalLength - offset)
        fragments.append(makeHeader(msgType, totalLength, messageSeq, offset, fragLen)
                         + bytes(body[offset:offset + fragLen]))
        offset += fragLen
    return fragments


def decodeHandshakeFragment(buf):
    msgType = buf[0]
    length = readUint24BE(buf, 1)
    messageSeq = struct.unpack_from('>H', buf, 4)[0]
    fragmentOffset = readUint24BE(buf, 6)
    fragmentLength = readUint24BE(buf, 9)
    body = bytes(buf[HANDSHAKE_HEADER_LEN:HANDSHAKE_HEADER_LEN + fragmentLength])
    return JSObject({
        'msgType': msgType,
        'length': length,
        'messageSeq': messageSeq,
        'fragmentOffset': fragmentOffset,
        'fragmentLength': fragmentLength,
        'body': body,
        'consumed': HANDSHAKE_HEADER_LEN + fragmentLength,
    })


class HandshakeReassembler:
    def __init__(self):
        self.pending = {}

    def addFragment(self, fragment):
        msgType = fragment.get('msgType')
        length = fragment.get('length')
        messageSeq = fragment.get('messageSeq')
        fragmentOffset = fragment.get('fragmentOffset')
        fragmentLength = fragment.get('fragmentLength')
        body = fragment.get('body')
        if fragmentOffset == 0 and fragmentLength == length:
            return JSObject({'msgType': msgType, 'messageSeq': messageSeq, 'body': bytes(body)})
        entry = self.pending.get(messageSeq)
        if entry is None:
            entry = JSObject({'length': length, 'msgType': msgType, 'chunks': {}})
            self.pending[messageSeq] = entry
        entry.get('chunks')[fragmentOffset] = bytes(body)

        covered = 0
        for chunk in entry.get('chunks').values():
            covered += len(chunk)
        if covered < entry.get('length'):
            return None
        sortedOffsets = sorted(entry.get('chunks').keys())
        assembled = b''.join(entry.get('chunks')[off] for off in sortedOffsets)
        del self.pending[messageSeq]
        return JSObject({'msgType': entry.get('msgType'), 'messageSeq': messageSeq,
                         'body': assembled[:entry.get('length')]})


def logicalHandshakeBytes(msgType, messageSeq, body):
    return makeHeader(msgType, len(body), messageSeq, 0, len(body)) + bytes(body)
