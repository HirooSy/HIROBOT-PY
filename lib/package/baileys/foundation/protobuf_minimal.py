import base64 as _base64
import math
import re
import struct

from baileys.foundation.node_runtime import JSObject, JSError
from baileys.foundation.long import Long, isLong, toInt32, toUint32, jsNumber, prop, stringToNumber, MASK32


SURROGATE_RE = re.compile('[\ud800-\udfff]')
B64_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
S64 = {ord(ch): index for index, ch in enumerate(B64_CHARS)}


class Namespace:
    pass


def toNumber(value):
    if value is None:
        return 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return stringToNumber(value)
    return math.nan


def toStr(value):
    if value is None:
        return 'undefined'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ','.join('' if item is None else toStr(item) for item in value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode('utf-8', 'replace')
    if isLong(value):
        return value.toString()
    return '[object Object]'


def toBoolean(value):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0 and value == value
    if isinstance(value, str):
        return len(value) > 0
    return True


def typeOf(value):
    if value is None:
        return 'undefined'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if callable(value):
        return 'function'
    return 'object'


def jsIsFinite(value):
    number = toNumber(value)
    if isinstance(number, float):
        return not (math.isnan(number) or math.isinf(number))
    return True


def strictEquals(a, b):
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return a is b


def jsLength(value):
    if isinstance(value, (list, tuple, str, bytes, bytearray)):
        return len(value)
    return None


def isArrayIndex(key):
    return key.isascii() and key.isdigit() and (key == '0' or not key.startswith('0')) and int(key) < 4294967295


def objectKeys(obj):
    indexed = []
    others = []
    for key in list(dict.keys(obj)):
        name = key if isinstance(key, str) else toStr(key)
        if isArrayIndex(name):
            indexed.append(name)
        else:
            others.append(name)
    indexed.sort(key=int)
    return indexed + others


def indexOutOfRange(reader, writeLength=None):
    return JSError('index out of range: ' + str(reader.pos) + ' + ' + str(writeLength or 1) + ' > ' + str(reader.len), name='RangeError')


def readFixed32End(buf, end):
    return int.from_bytes(bytes(buf[end - 4:end]), 'little')


class LongBits:
    def __init__(self, lo, hi):
        self.lo = toUint32(lo)
        self.hi = toUint32(hi)

    @staticmethod
    def fromNumber(value):
        if value == 0:
            return LongBits.zero
        sign = value < 0
        if sign:
            value = -value
        lo = toUint32(value)
        hi = toUint32((value - lo) / 4294967296)
        if sign:
            hi = (~hi) & MASK32
            lo = (~lo) & MASK32
            lo += 1
            if lo > 4294967295:
                lo = 0
                hi += 1
                if hi > 4294967295:
                    hi = 0
        return LongBits(lo, hi)

    @staticmethod
    def from_(value):
        if isinstance(value, bool):
            return LongBits.zero
        if isinstance(value, (int, float)):
            return LongBits.fromNumber(value)
        if isinstance(value, str):
            return LongBits.from_(Long.fromString(toStr(value)))
        low = prop(value, 'low')
        high = prop(value, 'high')
        if low or high:
            return LongBits(toUint32(low), toUint32(high))
        return LongBits.zero

    def toNumber(self, unsigned=False):
        if not unsigned and (self.hi >> 31):
            lo = (~self.lo + 1) & MASK32
            hi = (~self.hi) & MASK32
            if not lo:
                hi = (hi + 1) & MASK32
            return jsNumber(-(lo + hi * 4294967296))
        return jsNumber(self.lo + self.hi * 4294967296)

    def toLong(self, unsigned=False):
        return Long(toInt32(self.lo), toInt32(self.hi), bool(unsigned))

    def zzEncode(self):
        mask = toInt32(self.hi) >> 31
        hi = ((toInt32(self.hi) << 1) | (self.lo >> 31)) ^ mask
        lo = (toInt32(self.lo) << 1) ^ mask
        self.hi = hi & MASK32
        self.lo = lo & MASK32
        return self

    def zzDecode(self):
        mask = -(self.lo & 1)
        lo = ((self.lo >> 1) | (self.hi << 31)) ^ mask
        hi = (self.hi >> 1) ^ mask
        self.lo = lo & MASK32
        self.hi = hi & MASK32
        return self

    def length(self):
        part0 = self.lo
        part1 = ((self.lo >> 28) | (self.hi << 4)) & MASK32
        part2 = self.hi >> 24
        if part2 == 0:
            if part1 == 0:
                if part0 < 16384:
                    return 1 if part0 < 128 else 2
                return 3 if part0 < 2097152 else 4
            if part1 < 16384:
                return 5 if part1 < 128 else 6
            return 7 if part1 < 2097152 else 8
        return 9 if part2 < 128 else 10


class LongBitsZero(LongBits):
    def toNumber(self, unsigned=False):
        return 0

    def zzEncode(self):
        return self

    def zzDecode(self):
        return self

    def length(self):
        return 1


LongBits.zero = LongBitsZero(0, 0)


def readVarint32NearEnd(reader):
    value = 0
    for i in range(4):
        if reader.pos >= reader.len:
            raise indexOutOfRange(reader)
        b = reader.buf[reader.pos]
        reader.pos += 1
        value = (value | ((b & 127) << (i * 7))) & MASK32
        if b < 128:
            return value
    raise indexOutOfRange(reader)


def readLongVarint(reader):
    bits = LongBits(0, 0)
    buf = reader.buf
    if reader.len - reader.pos > 4:
        for i in range(4):
            bits.lo = (bits.lo | ((buf[reader.pos] & 127) << (i * 7))) & MASK32
            byte = buf[reader.pos]
            reader.pos += 1
            if byte < 128:
                return bits
        bits.lo = (bits.lo | ((buf[reader.pos] & 127) << 28)) & MASK32
        bits.hi = (bits.hi | ((buf[reader.pos] & 127) >> 4)) & MASK32
        byte = buf[reader.pos]
        reader.pos += 1
        if byte < 128:
            return bits
    else:
        for i in range(3):
            if reader.pos >= reader.len:
                raise indexOutOfRange(reader)
            bits.lo = (bits.lo | ((buf[reader.pos] & 127) << (i * 7))) & MASK32
            byte = buf[reader.pos]
            reader.pos += 1
            if byte < 128:
                return bits
        last = buf[reader.pos] if reader.pos < reader.len else 0
        bits.lo = (bits.lo | ((last & 127) << 21)) & MASK32
        reader.pos += 1
        return bits
    if reader.len - reader.pos > 4:
        for i in range(5):
            bits.hi = (bits.hi | ((buf[reader.pos] & 127) << (i * 7 + 3))) & MASK32
            byte = buf[reader.pos]
            reader.pos += 1
            if byte < 128:
                return bits
    else:
        for i in range(5):
            if reader.pos >= reader.len:
                raise indexOutOfRange(reader)
            bits.hi = (bits.hi | ((buf[reader.pos] & 127) << (i * 7 + 3))) & MASK32
            byte = buf[reader.pos]
            reader.pos += 1
            if byte < 128:
                return bits
    raise JSError('invalid varint encoding')


class Reader:
    recursionLimit = 100

    def __init__(self, buffer):
        if isinstance(buffer, list):
            buffer = bytes(buffer)
        elif isinstance(buffer, memoryview):
            buffer = buffer.tobytes()
        self.buf = buffer
        self.pos = 0
        self.len = len(buffer)

    @staticmethod
    def create(buffer):
        if isinstance(buffer, (bytes, bytearray, memoryview, list)):
            return Reader(buffer)
        raise JSError('illegal buffer')

    def uint32(self):
        buf = self.buf
        if self.len - self.pos < 5:
            if self.pos >= self.len:
                raise indexOutOfRange(self)
            if buf[self.pos] >= 128:
                return readVarint32NearEnd(self)
        value = 0
        for shift, mask in ((0, 127), (7, 127), (14, 127), (21, 127), (28, 15)):
            b = buf[self.pos]
            value = (value | ((b & mask) << shift)) & MASK32
            self.pos += 1
            if b < 128:
                return value
        self.pos += 5
        if self.pos > self.len:
            self.pos = self.len
            raise indexOutOfRange(self, 10)
        return value

    def int32(self):
        return toInt32(self.uint32())

    def sint32(self):
        value = self.uint32()
        return toInt32((value >> 1) ^ -(value & 1))

    def int64(self):
        return readLongVarint(self).toLong(False)

    def uint64(self):
        return readLongVarint(self).toLong(True)

    def sint64(self):
        return readLongVarint(self).zzDecode().toLong(False)

    def bool(self):
        return self.uint32() != 0

    def fixed32(self):
        if self.pos + 4 > self.len:
            raise indexOutOfRange(self, 4)
        self.pos += 4
        return readFixed32End(self.buf, self.pos)

    def sfixed32(self):
        return toInt32(self.fixed32())

    def _fixed64(self):
        if self.pos + 8 > self.len:
            raise indexOutOfRange(self, 8)
        self.pos += 4
        lo = readFixed32End(self.buf, self.pos)
        self.pos += 4
        hi = readFixed32End(self.buf, self.pos)
        return LongBits(lo, hi)

    def fixed64(self):
        return self._fixed64().toLong(True)

    def sfixed64(self):
        return self._fixed64().toLong(False)

    def float(self):
        if self.pos + 4 > self.len:
            raise indexOutOfRange(self, 4)
        value = struct.unpack_from('<f', self.buf, self.pos)[0]
        self.pos += 4
        return value

    def double(self):
        if self.pos + 8 > self.len:
            raise indexOutOfRange(self, 4)
        value = struct.unpack_from('<d', self.buf, self.pos)[0]
        self.pos += 8
        return value

    def bytes(self):
        length = self.uint32()
        start = self.pos
        end = self.pos + length
        if end > self.len:
            raise indexOutOfRange(self, length)
        self.pos += length
        if start == end:
            return b''
        return bytes(self.buf[start:end])

    def string(self):
        length = self.uint32()
        start = self.pos
        end = min(start + length, self.len)
        self.pos = end
        return bytes(self.buf[start:end]).decode('utf-8', 'replace')

    def skip(self, length=None):
        if length is not None:
            if self.pos + length > self.len:
                raise indexOutOfRange(self, length)
            self.pos += length
        else:
            while True:
                if self.pos >= self.len:
                    raise indexOutOfRange(self)
                b = self.buf[self.pos]
                self.pos += 1
                if not (b & 128):
                    break
        return self

    def skipType(self, wireType, depth=0):
        if depth is None:
            depth = 0
        if depth > Reader.recursionLimit:
            raise JSError('maximum nesting depth exceeded')
        if wireType == 0:
            self.skip()
        elif wireType == 1:
            self.skip(8)
        elif wireType == 2:
            self.skip(self.uint32())
        elif wireType == 3:
            while True:
                wireType = self.uint32() & 7
                if wireType == 4:
                    break
                self.skipType(wireType, depth + 1)
        elif wireType == 5:
            self.skip(4)
        else:
            raise JSError('invalid wire type ' + str(wireType) + ' at offset ' + str(self.pos))
        return self


def noop(val, buf, pos):
    return None


def writeByte(val, buf, pos):
    buf[pos] = val & 255


def writeVarint32(val, buf, pos):
    while val > 127:
        buf[pos] = (val & 127) | 128
        pos += 1
        val >>= 7
    buf[pos] = val


def writeVarint64(val, buf, pos):
    lo = val.lo
    hi = val.hi
    while hi:
        buf[pos] = (lo & 127) | 128
        pos += 1
        lo = ((lo >> 7) | (hi << 25)) & MASK32
        hi >>= 7
    while lo > 127:
        buf[pos] = (lo & 127) | 128
        pos += 1
        lo >>= 7
    buf[pos] = lo


def writeFixed32(val, buf, pos):
    buf[pos] = val & 255
    buf[pos + 1] = (val >> 8) & 255
    buf[pos + 2] = (val >> 16) & 255
    buf[pos + 3] = (val >> 24) & 255


def packFloatLE(fmt, val, buf, pos):
    number = toNumber(val)
    try:
        struct.pack_into(fmt, buf, pos, number)
    except OverflowError:
        struct.pack_into(fmt, buf, pos, math.copysign(math.inf, number))


def writeFloat(val, buf, pos):
    packFloatLE('<f', val, buf, pos)


def writeDouble(val, buf, pos):
    packFloatLE('<d', val, buf, pos)


def writeBytes(val, buf, pos):
    buf[pos:pos + len(val)] = val


def utf8Bytes(value):
    text = toStr(value)
    try:
        return text.encode('utf-8')
    except UnicodeEncodeError:
        return SURROGATE_RE.sub('�', text).encode('utf-8')


class Op:
    def __init__(self, fn, length, val):
        self.fn = fn
        self.len = length
        self.next = None
        self.val = val


class State:
    def __init__(self, writer):
        self.head = writer.head
        self.tail = writer.tail
        self.len = writer.len
        self.next = writer.states


class Writer:
    def __init__(self):
        self.len = 0
        self.head = Op(noop, 0, 0)
        self.tail = self.head
        self.states = None

    @staticmethod
    def create():
        return Writer()

    @staticmethod
    def alloc(size):
        return bytearray(size)

    def _push(self, fn, length, val):
        op = Op(fn, length, val)
        self.tail.next = op
        self.tail = op
        self.len += length
        return self

    def uint32(self, value):
        value = toUint32(value)
        if value < 128:
            size = 1
        elif value < 16384:
            size = 2
        elif value < 2097152:
            size = 3
        elif value < 268435456:
            size = 4
        else:
            size = 5
        return self._push(writeVarint32, size, value)

    def int32(self, value):
        value = toInt32(value)
        if value < 0:
            return self._push(writeVarint64, 10, LongBits.fromNumber(value))
        return self.uint32(value)

    def sint32(self, value):
        value = toInt32(value)
        return self.uint32(toUint32(toInt32(value << 1) ^ (value >> 31)))

    def uint64(self, value):
        bits = LongBits.from_(value)
        return self._push(writeVarint64, bits.length(), bits)

    def int64(self, value):
        return self.uint64(value)

    def sint64(self, value):
        bits = LongBits.from_(value).zzEncode()
        return self._push(writeVarint64, bits.length(), bits)

    def bool(self, value):
        return self._push(writeByte, 1, 1 if toBoolean(value) else 0)

    def fixed32(self, value):
        return self._push(writeFixed32, 4, toUint32(value))

    def sfixed32(self, value):
        return self.fixed32(value)

    def fixed64(self, value):
        bits = LongBits.from_(value)
        return self._push(writeFixed32, 4, bits.lo)._push(writeFixed32, 4, bits.hi)

    def sfixed64(self, value):
        return self.fixed64(value)

    def float(self, value):
        return self._push(writeFloat, 4, value)

    def double(self, value):
        return self._push(writeDouble, 8, value)

    def bytes(self, value):
        if isinstance(value, str):
            value = util.base64.toBytes(value)
        length = len(value)
        self.uint32(length)
        if length:
            self._push(writeBytes, length, value)
        return self

    def string(self, value):
        data = utf8Bytes(value)
        length = len(data)
        self.uint32(length)
        if length:
            self._push(writeBytes, length, data)
        return self

    def fork(self):
        self.states = State(self)
        self.head = self.tail = Op(noop, 0, 0)
        self.len = 0
        return self

    def reset(self):
        if self.states is not None:
            self.head = self.states.head
            self.tail = self.states.tail
            self.len = self.states.len
            self.states = self.states.next
        else:
            self.head = self.tail = Op(noop, 0, 0)
            self.len = 0
        return self

    def ldelim(self):
        head = self.head
        tail = self.tail
        length = self.len
        self.reset().uint32(length)
        if length:
            self.tail.next = head.next
            self.tail = tail
            self.len += length
        return self

    def finish(self):
        head = self.head.next
        buf = Writer.alloc(self.len)
        pos = 0
        while head is not None:
            head.fn(head.val, buf, pos)
            pos += head.len
            head = head.next
        return bytes(buf)


def newBuffer(sizeOrArray):
    if isinstance(sizeOrArray, (int, float)) and not isinstance(sizeOrArray, bool):
        return bytearray(int(sizeOrArray))
    return bytearray(sizeOrArray)


def base64Length(string):
    p = len(string)
    if not p:
        return 0
    n = 0
    while True:
        p -= 1
        if p >= 0 and p % 4 > 1 and string[p] == '=':
            n += 1
        else:
            break
    return math.ceil(len(string) * 3) / 4 - n


def base64Encode(buffer, start, end):
    return _base64.b64encode(bytes(buffer[start:end])).decode('ascii')


def base64Decode(string, buffer, offset):
    start = offset
    j = 0
    t = 0
    i = 0
    while i < len(string):
        c = ord(string[i])
        i += 1
        if c == 61 and j > 1:
            break
        c = S64.get(c)
        if c is None:
            raise JSError('invalid encoding')
        if j == 0:
            t = c
            j = 1
        elif j == 1:
            if offset < len(buffer):
                buffer[offset] = ((t << 2) | ((c & 48) >> 4)) & 255
            offset += 1
            t = c
            j = 2
        elif j == 2:
            if offset < len(buffer):
                buffer[offset] = (((t & 15) << 4) | ((c & 60) >> 2)) & 255
            offset += 1
            t = c
            j = 3
        else:
            if offset < len(buffer):
                buffer[offset] = (((t & 3) << 6) | c) & 255
            offset += 1
            j = 0
    if j == 1:
        raise JSError('invalid encoding')
    return offset - start


def base64ToBytes(string):
    buffer = newBuffer(base64Length(string))
    base64Decode(string, buffer, 0)
    return bytes(buffer)


def makeProp(obj, key):
    return None


def oneOfGetter(fieldNames):
    fieldMap = set(fieldNames)

    def getter(self):
        for key in reversed(list(dict.keys(self))):
            if key in fieldMap and dict.get(self, key) is not None:
                return key
        return None

    return getter


def oneOfSetter(fieldNames):
    def setter(self, name):
        for field in fieldNames:
            if field != name:
                dict.pop(self, field, None)

    return setter


util = Namespace()
util.Long = Long
util.LongBits = LongBits
util.isUnsafeProperty = lambda key: key in ('__proto__', 'prototype', 'constructor')
util.emptyArray = ()
util.emptyObject = JSObject()
util.recursionLimit = 100
util.nestingLimit = 32
util.toJSONOptions = JSObject({'longs': str, 'enums': str, 'bytes': str, 'json': True})
util.newBuffer = newBuffer
util.base64 = Namespace()
util.base64.length = base64Length
util.base64.encode = base64Encode
util.base64.decode = base64Decode
util.base64.toBytes = base64ToBytes
util.makeProp = makeProp
util.oneOfGetter = oneOfGetter
util.oneOfSetter = oneOfSetter
util.objectKeys = objectKeys

roots = JSObject()

default = JSObject({'Reader': Reader, 'Writer': Writer, 'util': util, 'roots': roots, 'LongBits': LongBits})
