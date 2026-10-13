import struct

from baileys_lite.foundation.node_runtime import JSObject


ContentType = JSObject({
    'CHANGE_CIPHER_SPEC': 20,
    'ALERT': 21,
    'HANDSHAKE': 22,
    'APPLICATION_DATA': 23,
})

DTLS_1_2_VERSION = bytes([0xFE, 0xFD])

RECORD_HEADER_LEN = 13


def encodeEpochSeq(epoch, sequenceNumber):
    return struct.pack('>H', epoch & 0xFFFF) + int(sequenceNumber & 0xFFFFFFFFFFFF).to_bytes(6, 'big')


def decodeEpochSeq(buf):
    epoch = struct.unpack_from('>H', buf, 0)[0]
    sequenceNumber = int.from_bytes(bytes(buf[2:8]), 'big')
    return JSObject({'epoch': epoch, 'sequenceNumber': sequenceNumber})


def encodeRecord(type, epoch, sequenceNumber, fragment):
    header = bytearray(RECORD_HEADER_LEN)
    header[0] = type
    header[1:3] = DTLS_1_2_VERSION
    header[3:11] = encodeEpochSeq(epoch, sequenceNumber)
    struct.pack_into('>H', header, 11, len(fragment))
    return bytes(header) + bytes(fragment)


def decodeRecords(datagram):
    records = []
    offset = 0
    while offset + RECORD_HEADER_LEN <= len(datagram):
        type = datagram[offset]
        decoded = decodeEpochSeq(datagram[offset + 3:offset + 11])
        length = struct.unpack_from('>H', datagram, offset + 11)[0]
        fragmentStart = offset + RECORD_HEADER_LEN
        fragmentEnd = fragmentStart + length
        if fragmentEnd > len(datagram):
            break
        records.append(JSObject({
            'type': type,
            'epoch': decoded.get('epoch'),
            'sequenceNumber': decoded.get('sequenceNumber'),
            'fragment': bytes(datagram[fragmentStart:fragmentEnd]),
        }))
        offset = fragmentEnd
    return records
