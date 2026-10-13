from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.voip.bytes import EMPTY_BYTES, TEXT_DECODER, TEXT_ENCODER, concatBytes
from baileys_lite.voip.protobuf import encodeProtoLengthDelimited, encodeProtoVarintField
from baileys_lite.voip.shim.util import optGet


APP_DATA_PAYLOADS_MESSAGES = 1
APP_DATA_MESSAGE_REACTION_INFO = 1
APP_DATA_MESSAGE_AR_EFFECT_INFO = 3
REACTION_INFO_TRANSACTION_ID = 1
REACTION_INFO_REACTION = 2
AR_EFFECT_INFO_TRANSACTION_ID = 1
AR_EFFECT_INFO_AR_EFFECT_ID = 2
WIRE_TYPE_VARINT = 0
WIRE_TYPE_FIXED64 = 1
WIRE_TYPE_LENGTH_DELIMITED = 2
WIRE_TYPE_FIXED32 = 5
VARINT_CONTINUATION = 0x80
VARINT_PAYLOAD_MASK = 0x7f
VARINT_SHIFT = 7

MAX_MESSAGES_PER_PAYLOAD = 32


def readVarint(data, offset):
    value = 0
    shift = 0
    cursor = offset
    while cursor < len(data):
        byte = data[cursor]
        cursor += 1
        value |= (byte & VARINT_PAYLOAD_MASK) << shift
        if (byte & VARINT_CONTINUATION) == 0:
            return JSObject({'value': value, 'next': cursor})
        shift += VARINT_SHIFT
        if shift > 63:
            break
    raise JSError('truncated protobuf varint')


def readFields(data):
    offset = 0
    while offset < len(data):
        tag = readVarint(data, offset)
        offset = tag.get('next')
        fieldNumber = tag.get('value') >> 3
        wireType = tag.get('value') & 0x7
        if fieldNumber == 0:
            raise JSError('invalid protobuf field number 0')
        if wireType == WIRE_TYPE_VARINT:
            read = readVarint(data, offset)
            offset = read.get('next')
            yield JSObject({'fieldNumber': fieldNumber, 'wireType': wireType, 'varint': read.get('value'), 'bytes': EMPTY_BYTES})
            continue
        if wireType == WIRE_TYPE_LENGTH_DELIMITED:
            length = readVarint(data, offset)
            start = length.get('next')
            end = start + length.get('value')
            if end > len(data):
                raise JSError('truncated protobuf length-delimited field')
            offset = end
            yield JSObject({'fieldNumber': fieldNumber, 'wireType': wireType, 'varint': 0, 'bytes': data[start:end]})
            continue
        if wireType == WIRE_TYPE_FIXED64:
            offset += 8
        elif wireType == WIRE_TYPE_FIXED32:
            offset += 4
        else:
            raise JSError(f'unsupported protobuf wire type {wireType}')
        if offset > len(data):
            raise JSError('truncated protobuf fixed-width field')


def encodeReactionPayload(reaction):
    reactionInfo = concatBytes([
        encodeProtoVarintField(REACTION_INFO_TRANSACTION_ID, optGet(reaction, 'transactionId')),
        encodeProtoLengthDelimited(REACTION_INFO_REACTION, TEXT_ENCODER.encode(optGet(reaction, 'reaction'))),
    ])
    message = encodeProtoLengthDelimited(APP_DATA_MESSAGE_REACTION_INFO, reactionInfo)
    return encodeProtoLengthDelimited(APP_DATA_PAYLOADS_MESSAGES, message)


def decodeReactionInfo(data, requireTransactionId):
    transactionId = None
    reaction = None
    for field in readFields(data):
        if field.get('fieldNumber') == REACTION_INFO_TRANSACTION_ID and field.get('wireType') == 0:
            transactionId = field.get('varint')
        elif field.get('fieldNumber') == REACTION_INFO_REACTION and field.get('wireType') == 2:
            reaction = TEXT_DECODER.decode(field.get('bytes'))
    if reaction is None:
        return None
    if transactionId is None and requireTransactionId:
        return None
    return JSObject({'transactionId': 0 if transactionId is None else transactionId, 'reaction': reaction})


def decodeArEffectInfo(data, requireTransactionId):
    transactionId = None
    arEffectId = None
    for field in readFields(data):
        if field.get('fieldNumber') == AR_EFFECT_INFO_TRANSACTION_ID and field.get('wireType') == 0:
            transactionId = field.get('varint')
        elif field.get('fieldNumber') == AR_EFFECT_INFO_AR_EFFECT_ID and field.get('wireType') == 2:
            arEffectId = TEXT_DECODER.decode(field.get('bytes'))
    if arEffectId is None:
        return None
    if transactionId is None and requireTransactionId:
        return None
    return JSObject({'transactionId': 0 if transactionId is None else transactionId, 'arEffectId': arEffectId})


def decodeAppDataMessage(data, strict):
    reaction = None
    arEffect = None
    for field in readFields(data):
        if field.get('wireType') != WIRE_TYPE_LENGTH_DELIMITED:
            continue
        if field.get('fieldNumber') == APP_DATA_MESSAGE_REACTION_INFO:
            reaction = decodeReactionInfo(field.get('bytes'), strict)
        elif field.get('fieldNumber') == APP_DATA_MESSAGE_AR_EFFECT_INFO:
            arEffect = decodeArEffectInfo(field.get('bytes'), strict)
    if reaction is not None:
        if arEffect is not None:
            return JSObject({'reaction': reaction, 'arEffect': arEffect})
        return JSObject({'reaction': reaction})
    if arEffect is not None:
        return JSObject({'arEffect': arEffect})
    return None


def decodeAsPayloads(data):
    items = []
    truncated = False
    for field in readFields(data):
        if field.get('fieldNumber') != APP_DATA_PAYLOADS_MESSAGES:
            continue
        if field.get('wireType') != WIRE_TYPE_LENGTH_DELIMITED:
            continue
        if len(items) >= MAX_MESSAGES_PER_PAYLOAD:
            truncated = True
            break
        item = decodeAppDataMessage(field.get('bytes'), False)
        if item is not None:
            items.append(item)
    return JSObject({'items': items, 'truncated': truncated})


def decodeAppDataPayload(data):
    try:
        nested = decodeAsPayloads(data)
        if len(nested.get('items')) > 0:
            return JSObject({'shape': 'payloads', 'items': nested.get('items'), 'truncated': nested.get('truncated')})
    except Exception:
        pass
    try:
        flat = decodeAppDataMessage(data, True)
        if flat is not None:
            return JSObject({'shape': 'message', 'items': [flat], 'truncated': False})
    except Exception:
        return None
    return None
