import math
import re

from baileys_lite.foundation.node_runtime import JSError


MASK32 = 0xFFFFFFFF
TWO_32 = 1 << 32
TWO_64 = 1 << 64
MASK64 = TWO_64 - 1
DIGITS = '0123456789abcdefghijklmnopqrstuvwxyz'


NUMBER_TEXT_RE = re.compile(r'^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$')
HEX_TEXT_RE = re.compile(r'^[+-]?0[xX][0-9a-fA-F]+$')


def stringToNumber(text):
    text = text.strip()
    if text == '':
        return 0
    if NUMBER_TEXT_RE.match(text):
        return jsNumber(float(text))
    if HEX_TEXT_RE.match(text):
        return int(text, 16)
    if text in ('Infinity', '+Infinity'):
        return math.inf
    if text == '-Infinity':
        return -math.inf
    return math.nan


def integerPart(value):
    if isinstance(value, str):
        value = stringToNumber(value)
    if value is None or isinstance(value, bool):
        return int(bool(value))
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return 0
        return int(value)
    return int(value)


def toInt32(value):
    value = integerPart(value) & MASK32
    return value - TWO_32 if value >= (1 << 31) else value


def toUint32(value):
    return integerPart(value) & MASK32


def jsNumber(value):
    value = float(value)
    if value.is_integer() and abs(value) < 9007199254740992.0:
        return int(value)
    return value


def jsParseInt(text, radix):
    s = text.lstrip()
    sign = 1
    if s[:1] in ('+', '-'):
        if s[0] == '-':
            sign = -1
        s = s[1:]
    if radix == 16 and s[:2] in ('0x', '0X'):
        s = s[2:]
    value = 0
    count = 0
    for ch in s:
        index = DIGITS.find(ch.lower())
        if index < 0 or index >= radix:
            break
        value = value * radix + index
        count += 1
    if count == 0:
        return None
    return sign * value


def formatInt(value, radix):
    if radix == 10:
        return str(value)
    if value == 0:
        return '0'
    negative = value < 0
    n = -value if negative else value
    out = []
    while n:
        out.append(DIGITS[n % radix])
        n //= radix
    text = ''.join(reversed(out))
    return '-' + text if negative else text


def asIntN64(value):
    value &= MASK64
    return value - TWO_64 if value >= (1 << 63) else value


def prop(obj, name):
    if isinstance(obj, dict):
        return dict.get(obj, name)
    return getattr(obj, name, None)


def isLong(obj):
    return getattr(obj, '__isLong__', None) is True


class Long:
    __isLong__ = True

    def __init__(self, low, high, unsigned=None):
        self.low = toInt32(low)
        self.high = toInt32(high)
        self.unsigned = bool(unsigned)

    def _bits(self):
        return ((self.high & MASK32) << 32) | (self.low & MASK32)

    def toBigInt(self):
        b = self._bits()
        return b if self.unsigned else asIntN64(b)

    def toNumber(self):
        if self.unsigned:
            return jsNumber((self.high & MASK32) * 4294967296 + (self.low & MASK32))
        return jsNumber(self.high * 4294967296 + (self.low & MASK32))

    def toInt(self):
        return (self.low & MASK32) if self.unsigned else self.low

    def toString(self, radix=None):
        return formatInt(self.toBigInt(), radix or 10)

    def isZero(self):
        return self.high == 0 and self.low == 0

    def isNegative(self):
        return (not self.unsigned) and self.high < 0

    def equals(self, other):
        if not isLong(other):
            other = Long.fromValue(other)
        if self.unsigned != other.unsigned and ((self.high & MASK32) >> 31) == 1 and ((other.high & MASK32) >> 31) == 1:
            return False
        return self.high == other.high and self.low == other.low

    def eq(self, other):
        return self.equals(other)

    def getHighBits(self):
        return self.high

    def getHighBitsUnsigned(self):
        return self.high & MASK32

    def getLowBits(self):
        return self.low

    def getLowBitsUnsigned(self):
        return self.low & MASK32

    def toSigned(self):
        return Long(self.low, self.high, False) if self.unsigned else self

    def toUnsigned(self):
        return self if self.unsigned else Long(self.low, self.high, True)

    @staticmethod
    def fromBits(low, high, unsigned=None):
        return Long(low, high, unsigned)

    @staticmethod
    def fromBigInt(value, unsigned=None):
        b = int(value) & MASK64
        return Long(toInt32(b & MASK32), toInt32((b >> 32) & MASK32), unsigned)

    @staticmethod
    def fromNumber(value, unsigned=None):
        if value != value:
            return Long(0, 0, unsigned)
        if unsigned:
            if value < 0:
                return Long(0, 0, True)
            if value >= 18446744073709551616:
                return Long(-1, -1, True)
        else:
            if value <= -9223372036854775808:
                return Long(0, -2147483648, False)
            if value + 1 >= 9223372036854775808:
                return Long(-1, 2147483647, False)
        return Long.fromBigInt(int(value), unsigned)

    @staticmethod
    def fromString(string, unsigned=None, radix=None):
        if len(string) == 0:
            raise JSError('empty string')
        if isinstance(unsigned, (int, float)) and not isinstance(unsigned, bool):
            radix = unsigned
            unsigned = False
        else:
            unsigned = bool(unsigned)
        if string in ('NaN', 'Infinity', '+Infinity', '-Infinity'):
            return Long(0, 0, unsigned)
        radix = radix or 10
        if radix < 2 or radix > 36:
            raise JSError('radix', name='RangeError')
        p = string.find('-')
        if p > 0:
            raise JSError('interior hyphen')
        neg = p == 0
        digits = string[1:] if neg else string
        result = 0
        for i in range(0, len(digits), 8):
            size = min(8, len(digits) - i)
            value = jsParseInt(digits[i:i + size], radix)
            result = result * radix ** size + (0 if value is None else value)
        if neg:
            result = -result
        return Long.fromBigInt(result, unsigned)

    @staticmethod
    def fromValue(val, unsigned=None):
        if val is None:
            raise TypeError('Cannot read properties of null')
        if isinstance(val, bool):
            return Long(0, 0, unsigned if isinstance(unsigned, bool) else False)
        if isinstance(val, (int, float)):
            return Long.fromNumber(val, unsigned)
        if isinstance(val, str):
            return Long.fromString(val, unsigned)
        flag = unsigned if isinstance(unsigned, bool) else prop(val, 'unsigned')
        return Long(prop(val, 'low'), prop(val, 'high'), flag)

    @staticmethod
    def isLong(obj):
        return isLong(obj)


ZERO = Long(0, 0, False)
UZERO = Long(0, 0, True)

default = Long
