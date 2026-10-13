from baileys_lite.foundation.node_runtime import JSObject


def encodeLength(length):
    if length < 0x80:
        return bytes([length])
    out = []
    n = length
    while n > 0:
        out.insert(0, n & 0xFF)
        n >>= 8
    return bytes([0x80 | len(out)]) + bytes(out)


def tlv(tag, content):
    return bytes([tag]) + encodeLength(len(content)) + content


def _sequence(*parts):
    return tlv(0x30, b''.join(parts))


def _set(*parts):
    return tlv(0x31, b''.join(parts))


def _contextConstructed(tagNumber, *parts):
    return tlv(0xA0 | tagNumber, b''.join(parts))


def _integer(value):
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    else:
        n = int(value)
        if n == 0:
            data = bytes([0])
        else:
            out = []
            while n > 0:
                out.insert(0, n & 0xFF)
                n >>= 8
            data = bytes(out)
    if data[0] & 0x80:
        data = bytes([0]) + data
    return tlv(0x02, data)


def _bitString(content, unusedBits=0):
    return tlv(0x03, bytes([unusedBits]) + content)


def _octetString(content):
    return tlv(0x04, content)


def _nullValue():
    return bytes([0x05, 0x00])


def _objectIdentifier(dotted):
    parts = [int(p) for p in dotted.split('.')]
    first = parts[0] * 40 + parts[1]
    out = [first]
    for part in parts[2:]:
        if part < 0x80:
            out.append(part)
            continue
        chunk = [part & 0x7F]
        n = part >> 7
        while n > 0:
            chunk.insert(0, (n & 0x7F) | 0x80)
            n >>= 7
        out.extend(chunk)
    return tlv(0x06, bytes(out))


def _utf8String(s):
    return tlv(0x0C, s.encode('utf-8'))


def _printableString(s):
    return tlv(0x13, s.encode('ascii'))


def _utcTime(date):
    text = '%02d%02d%02d%02d%02d%02dZ' % (
        date.year % 100, date.month, date.day, date.hour, date.minute, date.second)
    return tlv(0x17, text.encode('ascii'))


der = JSObject({
    'sequence': _sequence,
    'set': _set,
    'contextConstructed': _contextConstructed,
    'integer': _integer,
    'bitString': _bitString,
    'octetString': _octetString,
    'nullValue': _nullValue,
    'objectIdentifier': _objectIdentifier,
    'utf8String': _utf8String,
    'printableString': _printableString,
    'utcTime': _utcTime,
})


def readTlv(buf, offset=0):
    tag = buf[offset]
    lenByte = buf[offset + 1]
    lenStart = offset + 2
    if lenByte & 0x80:
        numBytes = lenByte & 0x7F
        length = 0
        for i in range(numBytes):
            length = (length << 8) | buf[lenStart + i]
        lenStart += numBytes
    else:
        length = lenByte
    return JSObject({'tag': tag, 'content': buf[lenStart:lenStart + length], 'next': lenStart + length})
