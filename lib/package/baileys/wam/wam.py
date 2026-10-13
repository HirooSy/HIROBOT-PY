import math
import re
import struct

from baileys.foundation.node_runtime import JSError, JSObject
from baileys.wam.wam_constants import FLAG_BYTE, FLAG_EVENT, FLAG_EXTENDED, FLAG_FIELD, FLAG_GLOBAL, WEB_EVENTS, WEB_GLOBALS


class BinaryInfo:
    def __init__(self, options=None):
        if options is None:
            options = JSObject({})
        self.protocolVersion = 5
        self.sequence = 0
        self.events = []
        self.buffer = []
        for key, value in list(dict.items(options)):
            setattr(self, key, value)


def getHeaderBitLength(key):
    return 2 if key < 256 else 3


def encodeWAM(binaryInfo):
    binaryInfo.buffer = []
    encodeWAMHeader(binaryInfo)
    encodeEvents(binaryInfo)
    return b''.join(binaryInfo.buffer)


def encodeWAMHeader(binaryInfo):
    headerBuffer = bytearray(8)
    headerBuffer[0:3] = 'WAM'.encode('utf-8')
    struct.pack_into('>B', headerBuffer, 3, binaryInfo.protocolVersion)
    struct.pack_into('>B', headerBuffer, 4, 1)
    struct.pack_into('>H', headerBuffer, 5, binaryInfo.sequence)
    struct.pack_into('>B', headerBuffer, 7, 0)
    binaryInfo.buffer.append(bytes(headerBuffer))


def encodeGlobalAttributes(binaryInfo, globals_):
    for key, _value in list(dict.items(globals_)):
        found = next((a for a in WEB_GLOBALS if a is not None and a.get('name') == key), None)
        id_ = found.get('id')
        value = _value
        if isinstance(value, bool):
            value = 1 if value else 0
        binaryInfo.buffer.append(serializeData(id_, value, FLAG_GLOBAL))


def encodeEvents(binaryInfo):
    for name, eventData in [list(dict.items(a))[0] for a in binaryInfo.events]:
        props = eventData.get('props')
        globals_ = eventData.get('globals')
        encodeGlobalAttributes(binaryInfo, globals_)
        event = next((a for a in WEB_EVENTS if a is not None and a.get('name') == name), None)
        props_ = list(dict.items(props))
        extended = False
        for _, value in props_:
            extended = extended or (value is not None)
        eventFlag = FLAG_EVENT if extended else FLAG_EVENT | FLAG_EXTENDED
        binaryInfo.buffer.append(serializeData(event.get('id'), -event.get('weight'), eventFlag))
        for i in range(len(props_)):
            key, _value = props_[i]
            propEntry = event.get('props')[key]
            id_ = None if propEntry is None else propEntry[0]
            extended = i < len(props_) - 1
            value = _value
            if isinstance(value, bool):
                value = 1 if value else 0
            fieldFlag = FLAG_EVENT if extended else FLAG_FIELD | FLAG_EXTENDED
            binaryInfo.buffer.append(serializeData(id_, value, fieldFlag))


def isJSNumber(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def isJSInteger(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value) and value.is_integer()
    return False


def encodeUtf8(value):
    try:
        return value.encode('utf-8')
    except UnicodeEncodeError:
        return re.sub('[\ud800-\udfff]', '�', value).encode('utf-8')


def serializeData(key, value, flag):
    bufferLength = getHeaderBitLength(key)
    if value is None:
        if flag == FLAG_GLOBAL:
            buffer = bytearray(bufferLength)
            serializeHeader(buffer, 0, key, flag)
            return bytes(buffer)
    elif isJSNumber(value) and isJSInteger(value):
        value = int(value)
        if value == 0 or value == 1:
            buffer = bytearray(bufferLength)
            serializeHeader(buffer, 0, key, flag | ((value + 1) << 4))
            return bytes(buffer)
        elif -128 <= value < 128:
            buffer = bytearray(bufferLength + 1)
            offset = serializeHeader(buffer, 0, key, flag | (3 << 4))
            struct.pack_into('<b', buffer, offset, value)
            return bytes(buffer)
        elif -32768 <= value < 32768:
            buffer = bytearray(bufferLength + 2)
            offset = serializeHeader(buffer, 0, key, flag | (4 << 4))
            struct.pack_into('<h', buffer, offset, value)
            return bytes(buffer)
        elif -2147483648 <= value < 2147483648:
            buffer = bytearray(bufferLength + 4)
            offset = serializeHeader(buffer, 0, key, flag | (5 << 4))
            struct.pack_into('<i', buffer, offset, value)
            return bytes(buffer)
        else:
            buffer = bytearray(bufferLength + 8)
            offset = serializeHeader(buffer, 0, key, flag | (7 << 4))
            struct.pack_into('<d', buffer, offset, float(value))
            return bytes(buffer)
    elif isJSNumber(value):
        buffer = bytearray(bufferLength + 8)
        offset = serializeHeader(buffer, 0, key, flag | (7 << 4))
        struct.pack_into('<d', buffer, offset, float(value))
        return bytes(buffer)
    elif isinstance(value, str):
        utf8 = encodeUtf8(value)
        utf8Bytes = len(utf8)
        if utf8Bytes < 256:
            buffer = bytearray(bufferLength + 1 + utf8Bytes)
            offset = serializeHeader(buffer, 0, key, flag | (8 << 4))
            struct.pack_into('<B', buffer, offset, utf8Bytes)
            offset += 1
        elif utf8Bytes < 65536:
            buffer = bytearray(bufferLength + 2 + utf8Bytes)
            offset = serializeHeader(buffer, 0, key, flag | (9 << 4))
            struct.pack_into('<H', buffer, offset, utf8Bytes)
            offset += 2
        else:
            buffer = bytearray(bufferLength + 4 + utf8Bytes)
            offset = serializeHeader(buffer, 0, key, flag | (10 << 4))
            struct.pack_into('<I', buffer, offset, utf8Bytes)
            offset += 4
        buffer[offset:offset + utf8Bytes] = utf8
        return bytes(buffer)
    raise JSError('missing')


def serializeHeader(buffer, offset, key, flag):
    if key < 256:
        struct.pack_into('<B', buffer, offset, flag)
        offset += 1
        struct.pack_into('<B', buffer, offset, key)
        offset += 1
    else:
        struct.pack_into('<B', buffer, offset, flag | FLAG_BYTE)
        offset += 1
        struct.pack_into('<H', buffer, offset, key)
        offset += 2
    return offset
