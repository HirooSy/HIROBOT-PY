from baileys_lite.foundation.node_runtime import JSError
from baileys_lite.voip.shim.util import (
    EMPTY_BYTES,
    TEXT_DECODER,
    TEXT_ENCODER,
    base64ToBytes,
    bytesToBase64,
    bytesToHex,
    concatBytes,
    toBytesView,
)


class RangeError(JSError):
    def __init__(self, message=''):
        super().__init__(message)
        self.name = 'RangeError'


def ensureBounds(buf, offset, size):
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset + size > len(buf):
        raise RangeError(f'byte access out of range: offset {offset}, size {size}, length {len(buf)}')


def readUInt16BE(buf, offset):
    ensureBounds(buf, offset, 2)
    return (buf[offset] << 8) | buf[offset + 1]


def readUInt32BE(buf, offset):
    ensureBounds(buf, offset, 4)
    return (buf[offset] << 24) | (buf[offset + 1] << 16) | (buf[offset + 2] << 8) | buf[offset + 3]


def readUInt32LE(buf, offset):
    ensureBounds(buf, offset, 4)
    return buf[offset] | (buf[offset + 1] << 8) | (buf[offset + 2] << 16) | (buf[offset + 3] << 24)


def readBigUInt64BE(buf, offset):
    hi = readUInt32BE(buf, offset)
    lo = readUInt32BE(buf, offset + 4)
    return (hi << 32) | lo


def writeUInt16BE(buf, value, offset):
    ensureBounds(buf, offset, 2)
    buf[offset] = (value >> 8) & 0xff
    buf[offset + 1] = value & 0xff


def writeUInt32BE(buf, value, offset):
    ensureBounds(buf, offset, 4)
    buf[offset] = (value >> 24) & 0xff
    buf[offset + 1] = (value >> 16) & 0xff
    buf[offset + 2] = (value >> 8) & 0xff
    buf[offset + 3] = value & 0xff


def writeUInt32LE(buf, value, offset):
    ensureBounds(buf, offset, 4)
    buf[offset] = value & 0xff
    buf[offset + 1] = (value >> 8) & 0xff
    buf[offset + 2] = (value >> 16) & 0xff
    buf[offset + 3] = (value >> 24) & 0xff


def writeBigUInt64BE(buf, value, offset):
    writeUInt32BE(buf, (value >> 32) & 0xffffffff, offset)
    writeUInt32BE(buf, value & 0xffffffff, offset + 4)


def toArrayBuffer(bytes_):
    return bytes(bytes_)
