from baileys.voip.bytes import concatBytes


WIRE_TYPE_VARINT = 0
WIRE_TYPE_LENGTH_DELIMITED = 2
VARINT_CONTINUATION = 0x80
VARINT_PAYLOAD_MASK = 0x7f
VARINT_SHIFT = 7


def encodeVarint(value):
    out = []
    remaining = value
    while remaining > VARINT_PAYLOAD_MASK:
        out.append((remaining & VARINT_PAYLOAD_MASK) | VARINT_CONTINUATION)
        remaining >>= VARINT_SHIFT
    out.append(remaining & VARINT_PAYLOAD_MASK)
    return bytes(out)


def encodeTag(fieldNumber, wireType):
    return encodeVarint((fieldNumber << 3) | wireType)


def encodeProtoVarintField(fieldNumber, value):
    return concatBytes([encodeTag(fieldNumber, WIRE_TYPE_VARINT), encodeVarint(value)])


def encodeProtoLengthDelimited(fieldNumber, data):
    return concatBytes([
        encodeTag(fieldNumber, WIRE_TYPE_LENGTH_DELIMITED),
        encodeVarint(len(data)),
        data,
    ])
