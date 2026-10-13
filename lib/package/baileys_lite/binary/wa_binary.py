import re
import zlib

from baileys_lite.foundation.node_runtime import JSObject, JSError, now
from baileys_lite.foundation.node_crypto import randomBytes
from baileys_lite.foundation.boom import Boom
from baileys_lite.binary import wa_binary_constants as constants


S_WHATSAPP_NET = '@s.whatsapp.net'
OFFICIAL_BIZ_JID = '16505361212@c.us'
SERVER_JID = 'server@c.us'
PSA_WID = '0@c.us'
STORIES_JID = 'status@broadcast'
META_AI_JID = '13135550002@c.us'

WAJIDDomains = JSObject({
    'WHATSAPP': 0,
    'LID': 1,
    'HOSTED': 128,
    'HOSTED_LID': 129
})

WA_BINARY_CONSTANTS = JSObject({
    'TAGS': constants.TAGS,
    'DOUBLE_BYTE_TOKENS': constants.DOUBLE_BYTE_TOKENS,
    'SINGLE_BYTE_TOKENS': constants.SINGLE_BYTE_TOKENS,
    'TOKEN_MAP': constants.TOKEN_MAP
})

BOT_JID_RE = re.compile(r'1313555[0-9]{4}|131655500[0-9]{2}')
ARRAY_INDEX_RE = re.compile(r'0|[1-9][0-9]{0,9}')
NUMBER_RE = re.compile(r'[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?')
HEX_RE = re.compile(r'0[xX][0-9a-fA-F]+')
PARSE_INT_RE = re.compile(r'\s*([+-]?[0-9]+)')

FLOWS_MAP = JSObject({
    'mpm': True,
    'cta_catalog': True,
    'send_location': True,
    'call_permission_request': True,
    'wa_payment_transaction_details': True,
    'automated_greeting_message_view_catalog': True
})
DECISION_SOURCE_CONTENT = [JSObject({'tag': 'decision_source', 'attrs': JSObject({'value': 'df'})})]
LIST_TYPE_CONTENT = JSObject({'tag': 'list', 'attrs': JSObject({'v': '2', 'type': 'product_list'})})
NATIVE_FLOW_ATTRIBUTE = JSObject({'type': 'native_flow', 'v': '1'})
MIXED_NATIVE_FLOW = JSObject({
    'tag': 'interactive',
    'attrs': NATIVE_FLOW_ATTRIBUTE,
    'content': [JSObject({'tag': 'native_flow', 'attrs': JSObject({'v': '9', 'name': 'mixed'})})]
})


def isTruthy(value):
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return value == value and value != 0
    return True


def normalizeNumber(value):
    if isinstance(value, float) and value == value and value not in (float('inf'), float('-inf')) and value.is_integer():
        return int(value)
    return value


def jsNumber(value):
    if value is None:
        return float('nan')
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return normalizeNumber(value)
    if isinstance(value, str):
        text = value.strip()
        if text == '':
            return 0
        if HEX_RE.fullmatch(text):
            return int(text, 16)
        if NUMBER_RE.fullmatch(text):
            return normalizeNumber(float(text))
        if text in ('Infinity', '+Infinity'):
            return float('inf')
        if text == '-Infinity':
            return float('-inf')
        return float('nan')
    return float('nan')


def jsString(value):
    if value is None:
        return 'undefined'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode('utf-8', 'replace')
    if isinstance(value, float):
        if value != value:
            return 'NaN'
        if value == float('inf'):
            return 'Infinity'
        if value == float('-inf'):
            return '-Infinity'
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return ','.join('' if item is None else jsString(item) for item in value)
    return '[object Object]'


def typeOf(value):
    if value is None:
        return 'undefined'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, str):
        return 'string'
    return 'object'


def parseIntJs(value):
    match = PARSE_INT_RE.match(jsString(value))
    if match is None:
        return float('nan')
    return int(match.group(1))


def isArrayIndex(key):
    return ARRAY_INDEX_RE.fullmatch(key) is not None and int(key) < 4294967295


def jsKeys(obj):
    keys = list(dict.keys(obj))
    indices = sorted((key for key in keys if isArrayIndex(key)), key=int)
    others = [key for key in keys if not isArrayIndex(key)]
    return indices + others


def prop(obj, key):
    if isinstance(obj, dict):
        return dict.get(obj, key)
    return None


def toByte(value):
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value) & 0xff
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            return 0
        return int(value) & 0xff
    return value & 0xff


def utf8Bytes(text):
    try:
        return text.encode('utf-8')
    except UnicodeEncodeError:
        return text.encode('utf-16', 'surrogatepass').decode('utf-16', 'replace').encode('utf-8')


def isValidChild(item):
    if not isTruthy(item):
        return False
    if isinstance(item, (bytes, bytearray, str)):
        return True
    if isinstance(item, dict):
        return isTruthy(dict.get(item, 'tag'))
    return False


def tabs(n):
    return '\t' * n


def encodeBinaryNode(node, opts=None, buffer=None):
    if opts is None:
        opts = WA_BINARY_CONSTANTS
    if buffer is None:
        buffer = [0]
    encoded = encodeBinaryNodeInner(node, opts, buffer)
    return bytes(encoded)


def encodeBinaryNodeInner(node, opts, buffer):
    TAGS = opts.get('TAGS')
    TOKEN_MAP = opts.get('TOKEN_MAP')
    tag = prop(node, 'tag')
    attrs = prop(node, 'attrs')
    content = prop(node, 'content')

    def pushByte(value):
        buffer.append(toByte(value))

    def pushInt(value, n, littleEndian=False):
        for i in range(n):
            curShift = i if littleEndian else n - 1 - i
            buffer.append((value >> (curShift * 8)) & 0xff)

    def pushBytes(data):
        buffer.extend(data)

    def pushInt16(value):
        pushBytes([(value >> 8) & 0xff, value & 0xff])

    def pushInt20(value):
        pushBytes([(value >> 16) & 0x0f, (value >> 8) & 0xff, value & 0xff])

    def writeByteLength(length):
        if length >= 4294967296:
            raise JSError('string too large to encode: ' + jsString(length))
        if length >= 1 << 20:
            pushByte(TAGS['BINARY_32'])
            pushInt(length, 4)
        elif length >= 256:
            pushByte(TAGS['BINARY_20'])
            pushInt20(length)
        else:
            pushByte(TAGS['BINARY_8'])
            pushByte(length)

    def writeStringRaw(text):
        data = utf8Bytes(text)
        writeByteLength(len(data))
        pushBytes(data)

    def writeJid(decoded):
        domainType = decoded.get('domainType')
        device = decoded.get('device')
        user = decoded.get('user')
        server = decoded.get('server')
        if device is not None:
            pushByte(TAGS['AD_JID'])
            pushByte(domainType if isTruthy(domainType) else 0)
            pushByte(device if isTruthy(device) else 0)
            writeString(user)
        else:
            pushByte(TAGS['JID_PAIR'])
            if len(user):
                writeString(user)
            else:
                pushByte(TAGS['LIST_EMPTY'])
            writeString(server)

    def packNibble(char):
        if char == '-':
            return 10
        if char == '.':
            return 11
        if char == '\0':
            return 15
        if '0' <= char <= '9':
            return ord(char) - ord('0')
        raise JSError('invalid byte for nibble "' + char + '"')

    def packHex(char):
        if '0' <= char <= '9':
            return ord(char) - ord('0')
        if 'A' <= char <= 'F':
            return 10 + ord(char) - ord('A')
        if 'a' <= char <= 'f':
            return 10 + ord(char) - ord('a')
        if char == '\0':
            return 15
        raise JSError('Invalid hex char "' + char + '"')

    def writePackedBytes(text, kind):
        if len(text) > TAGS['PACKED_MAX']:
            raise JSError('Too many bytes to pack')
        pushByte(TAGS['NIBBLE_8'] if kind == 'nibble' else TAGS['HEX_8'])
        roundedLength = (len(text) + 1) // 2
        if len(text) % 2 != 0:
            roundedLength |= 128
        pushByte(roundedLength)
        packFunction = packNibble if kind == 'nibble' else packHex

        def packBytePair(v1, v2):
            return (packFunction(v1) << 4) | packFunction(v2)

        strLengthHalf = len(text) // 2
        for i in range(strLengthHalf):
            pushByte(packBytePair(text[2 * i], text[2 * i + 1]))
        if len(text) % 2 != 0:
            pushByte(packBytePair(text[-1], '\x00'))

    def isNibble(text):
        if not text or len(text) > TAGS['PACKED_MAX']:
            return False
        for char in text:
            isInNibbleRange = '0' <= char <= '9'
            if not isInNibbleRange and char != '-' and char != '.':
                return False
        return True

    def isHex(text):
        if not text or len(text) > TAGS['PACKED_MAX']:
            return False
        for char in text:
            isInNibbleRange = '0' <= char <= '9'
            if not isInNibbleRange and not ('A' <= char <= 'F'):
                return False
        return True

    def writeString(text):
        if text is None:
            pushByte(TAGS['LIST_EMPTY'])
            return
        if text == '':
            writeStringRaw(text)
            return
        tokenIndex = dict.get(TOKEN_MAP, text)
        if tokenIndex is not None:
            if tokenIndex.get('dict') is not None:
                pushByte(TAGS['DICTIONARY_0'] + tokenIndex.get('dict'))
            pushByte(tokenIndex.get('index'))
        elif isNibble(text):
            writePackedBytes(text, 'nibble')
        elif isHex(text):
            writePackedBytes(text, 'hex')
        else:
            decodedJid = jidDecode(text)
            if decodedJid is not None:
                writeJid(decodedJid)
            else:
                writeStringRaw(text)

    def writeListStart(listSize):
        if listSize == 0:
            pushByte(TAGS['LIST_EMPTY'])
        elif listSize < 256:
            pushBytes([TAGS['LIST_8'], listSize])
        else:
            pushByte(TAGS['LIST_16'])
            pushInt16(listSize)

    if not tag:
        raise JSError('Invalid node: tag cannot be undefined')

    if attrs is None:
        attrs = JSObject({})
    validAttributes = [key for key in jsKeys(attrs) if attrs[key] is not None]
    writeListStart(2 * len(validAttributes) + 1 + (0 if content is None else 1))
    writeString(tag)
    for key in validAttributes:
        if isinstance(attrs[key], str):
            writeString(key)
            writeString(attrs[key])

    if isinstance(content, str):
        writeString(content)
    elif isinstance(content, (bytes, bytearray)):
        writeByteLength(len(content))
        pushBytes(content)
    elif isinstance(content, list):
        validContent = [item for item in content if isValidChild(item)]
        writeListStart(len(validContent))
        for item in validContent:
            encodeBinaryNodeInner(item, opts, buffer)
    elif content is None:
        pass
    else:
        raise JSError('invalid children for header "' + jsString(tag) + '": ' + jsString(content) + ' (' + typeOf(content) + ')')

    return buffer


async def decompressingIfRequired(buffer):
    if len(buffer) == 0:
        raise JSError('Attempt to access memory outside buffer bounds')
    if 2 & buffer[0]:
        buffer = zlib.decompress(bytes(buffer[1:]))
    else:
        buffer = bytes(buffer[1:])
    return buffer


def decodeDecompressedBinaryNode(buffer, opts, indexRef=None):
    if indexRef is None:
        indexRef = JSObject({'index': 0})
    TAGS = opts.get('TAGS')
    DOUBLE_BYTE_TOKENS = opts.get('DOUBLE_BYTE_TOKENS')
    SINGLE_BYTE_TOKENS = opts.get('SINGLE_BYTE_TOKENS')

    def checkEOS(length):
        if indexRef.get('index') + length > len(buffer):
            raise JSError('end of stream')

    def next():
        value = buffer[indexRef.get('index')]
        indexRef['index'] += 1
        return value

    def readByte():
        checkEOS(1)
        return next()

    def readBytes(n):
        checkEOS(n)
        value = bytes(buffer[indexRef.get('index'):indexRef.get('index') + n])
        indexRef['index'] += n
        return value

    def readStringFromChars(length):
        return readBytes(length).decode('utf-8', 'replace')

    def readInt(n, littleEndian=False):
        checkEOS(n)
        val = 0
        for i in range(n):
            shift = i if littleEndian else n - 1 - i
            val |= next() << (shift * 8)
        return val

    def readInt20():
        checkEOS(3)
        return ((next() & 15) << 16) + (next() << 8) + next()

    def unpackHex(value):
        if 0 <= value < 16:
            return chr(ord('0') + value) if value < 10 else chr(ord('A') + value - 10)
        raise JSError('invalid hex: ' + str(value))

    def unpackNibble(value):
        if 0 <= value <= 9:
            return chr(ord('0') + value)
        if value == 10:
            return '-'
        if value == 11:
            return '.'
        if value == 15:
            return '\0'
        raise JSError('invalid nibble: ' + str(value))

    def unpackByte(tag, value):
        if tag == TAGS['NIBBLE_8']:
            return unpackNibble(value)
        if tag == TAGS['HEX_8']:
            return unpackHex(value)
        raise JSError('unknown tag: ' + str(tag))

    def readPacked8(tag):
        startByte = readByte()
        value = ''
        for i in range(startByte & 127):
            curByte = readByte()
            value += unpackByte(tag, (curByte & 0xf0) >> 4)
            value += unpackByte(tag, curByte & 0x0f)
        if startByte >> 7 != 0:
            value = value[:-1]
        return value

    def isListTag(tag):
        return tag == TAGS['LIST_EMPTY'] or tag == TAGS['LIST_8'] or tag == TAGS['LIST_16']

    def readListSize(tag):
        if tag == TAGS['LIST_EMPTY']:
            return 0
        if tag == TAGS['LIST_8']:
            return readByte()
        if tag == TAGS['LIST_16']:
            return readInt(2)
        raise JSError('invalid tag for list size: ' + str(tag))

    def readJidPair():
        i = readString(readByte())
        j = readString(readByte())
        if j:
            return (i or '') + '@' + j
        raise JSError('invalid jid pair: ' + jsString(i) + ', ' + jsString(j))

    def readAdJid():
        domainType = readByte()
        device = readByte()
        user = readString(readByte())
        server = 's.whatsapp.net'
        if domainType == WAJIDDomains['LID']:
            server = 'lid'
        elif domainType == WAJIDDomains['HOSTED']:
            server = 'hosted'
        elif domainType == WAJIDDomains['HOSTED_LID']:
            server = 'hosted.lid'
        return jidEncode(user, server, device)

    def readFbJid():
        user = readString(readByte())
        device = readInt(2)
        server = readString(readByte())
        return user + ':' + str(device) + '@' + server

    def readInteropJid():
        user = readString(readByte())
        device = readInt(2)
        integrator = readInt(2)
        server = 'interop'
        beforeServer = indexRef.get('index')
        try:
            server = readString(readByte())
        except Exception:
            indexRef['index'] = beforeServer
        return str(integrator) + '-' + user + ':' + str(device) + '@' + server

    def readString(tag):
        if 1 <= tag < len(SINGLE_BYTE_TOKENS):
            return SINGLE_BYTE_TOKENS[tag] or ''
        if tag in (TAGS['DICTIONARY_0'], TAGS['DICTIONARY_1'], TAGS['DICTIONARY_2'], TAGS['DICTIONARY_3']):
            return getTokenDouble(tag - TAGS['DICTIONARY_0'], readByte())
        if tag == TAGS['LIST_EMPTY']:
            return ''
        if tag == TAGS['BINARY_8']:
            return readStringFromChars(readByte())
        if tag == TAGS['BINARY_20']:
            return readStringFromChars(readInt20())
        if tag == TAGS['BINARY_32']:
            return readStringFromChars(readInt(4))
        if tag == TAGS['JID_PAIR']:
            return readJidPair()
        if tag == TAGS['FB_JID']:
            return readFbJid()
        if tag == TAGS['INTEROP_JID']:
            return readInteropJid()
        if tag == TAGS['AD_JID']:
            return readAdJid()
        if tag == TAGS['HEX_8'] or tag == TAGS['NIBBLE_8']:
            return readPacked8(tag)
        raise JSError('invalid string with tag: ' + str(tag))

    def readList(tag):
        items = []
        size = readListSize(tag)
        for i in range(size):
            items.append(decodeDecompressedBinaryNode(buffer, opts, indexRef))
        return items

    def getTokenDouble(index1, index2):
        dictionary = DOUBLE_BYTE_TOKENS[index1]
        if not dictionary:
            raise JSError('Invalid double token dict (' + str(index1) + ')')
        if index2 >= len(dictionary):
            raise JSError('Invalid double token (' + str(index2) + ')')
        return dictionary[index2]

    listSize = readListSize(readByte())
    header = readString(readByte())
    if not listSize or not len(header):
        raise JSError('invalid node')

    attrs = JSObject({})
    data = None

    attributesLength = (listSize - 1) >> 1
    for i in range(attributesLength):
        key = readString(readByte())
        value = readString(readByte())
        attrs[key] = value

    if listSize % 2 == 0:
        tag = readByte()
        if isListTag(tag):
            data = readList(tag)
        elif tag == TAGS['BINARY_8']:
            data = readBytes(readByte())
        elif tag == TAGS['BINARY_20']:
            data = readBytes(readInt20())
        elif tag == TAGS['BINARY_32']:
            data = readBytes(readInt(4))
        else:
            data = readString(tag)

    return JSObject({'tag': header, 'attrs': attrs, 'content': data})


async def decodeBinaryNode(buff):
    decompBuff = await decompressingIfRequired(buff)
    return decodeDecompressedBinaryNode(decompBuff, WA_BINARY_CONSTANTS)


def jidEncode(user, server, device=None, agent=None):
    userPart = jsString(user) if isTruthy(user) else ''
    agentPart = '_' + jsString(agent) if isTruthy(agent) else ''
    devicePart = ':' + jsString(device) if isTruthy(device) else ''
    serverPart = jsString(server) if isTruthy(server) else 'lid'
    return userPart + agentPart + devicePart + '@' + serverPart


def jidDecode(jid):
    sepIdx = jid.find('@') if isinstance(jid, str) else -1
    if sepIdx < 0:
        return None

    server = jid[sepIdx + 1:]
    userCombined = jid[:sepIdx]
    parts = userCombined.split(':')
    userAgent = parts[0]
    device = parts[1] if len(parts) > 1 else None
    userAgentParts = userAgent.split('_')
    user = userAgentParts[0]
    agent = userAgentParts[1] if len(userAgentParts) > 1 else None

    domainType = WAJIDDomains['WHATSAPP']
    if server == 'lid':
        domainType = WAJIDDomains['LID']
    elif server == 'hosted':
        domainType = WAJIDDomains['HOSTED']
    elif server == 'hosted.lid':
        domainType = WAJIDDomains['HOSTED_LID']
    elif isTruthy(agent):
        domainType = parseIntJs(agent)

    return JSObject({
        'server': server,
        'user': user,
        'domainType': domainType,
        'device': jsNumber(device) if isTruthy(device) else None
    })


def areJidsSameUser(jid1, jid2):
    decoded1 = jidDecode(jid1)
    decoded2 = jidDecode(jid2)
    user1 = decoded1.get('user') if decoded1 is not None else None
    user2 = decoded2.get('user') if decoded2 is not None else None
    return user1 == user2


def endsWithSuffix(jid, suffix):
    if jid is None:
        return None
    return jid.endswith(suffix)


def isJidMetaAI(jid):
    return endsWithSuffix(jid, '@bot')


def isPnUser(jid):
    return endsWithSuffix(jid, '@s.whatsapp.net')


def isLidUser(jid):
    return endsWithSuffix(jid, '@lid')


def isJidBroadcast(jid):
    return endsWithSuffix(jid, '@broadcast')


def isJidGroup(jid):
    return endsWithSuffix(jid, '@g.us')


def isJidStatusBroadcast(jid):
    return jid == 'status@broadcast'


def isJidNewsletter(jid):
    return endsWithSuffix(jid, '@newsletter')


def isHostedPnUser(jid):
    return endsWithSuffix(jid, '@hosted')


def isHostedLidUser(jid):
    return endsWithSuffix(jid, '@hosted.lid')


def isJidBot(jid):
    if not isTruthy(jid):
        return jid
    return bool(BOT_JID_RE.fullmatch(jid.split('@')[0])) and jid.endswith('@c.us')


def jidNormalizedUser(jid):
    result = jidDecode(jid)
    if result is None:
        return ''
    user = result.get('user')
    server = result.get('server')
    return jidEncode(user, 's.whatsapp.net' if server == 'c.us' else server)


def transferDevice(fromJid, toJid):
    fromDecoded = jidDecode(fromJid)
    deviceId = fromDecoded.get('device') if fromDecoded is not None else None
    if not isTruthy(deviceId):
        deviceId = 0
    toDecoded = jidDecode(toJid)
    return jidEncode(toDecoded.get('user'), toDecoded.get('server'), deviceId)


def getServerFromDomainType(initialServer, domainType):
    if domainType == WAJIDDomains['LID']:
        return 'lid'
    if domainType == WAJIDDomains['HOSTED']:
        return 'hosted'
    if domainType == WAJIDDomains['HOSTED_LID']:
        return 'hosted.lid'
    return initialServer


def getBinaryNodeChildren(node, childTag):
    content = prop(node, 'content')
    if not isinstance(content, list):
        return []
    index = {}
    for child in content:
        if child is None:
            raise JSError("Cannot read properties of undefined (reading 'tag')")
        childNodeTag = prop(child, 'tag')
        if childNodeTag not in index:
            index[childNodeTag] = []
        index[childNodeTag].append(child)
    return index.get(childTag, [])


def getBinaryNodeChild(node, childTag):
    children = getBinaryNodeChildren(node, childTag)
    return children[0] if children else None


def getAllBinaryNodeChildren(node):
    content = prop(node, 'content')
    return content if isinstance(content, list) else []


def getBinaryNodeChildBuffer(node, childTag):
    child = prop(getBinaryNodeChild(node, childTag), 'content')
    if isinstance(child, (bytes, bytearray)):
        return child
    return None


def getBinaryNodeChildString(node, childTag):
    child = prop(getBinaryNodeChild(node, childTag), 'content')
    if isinstance(child, (bytes, bytearray)):
        return bytes(child).decode('utf-8', 'replace')
    if isinstance(child, str):
        return child
    return None


def bufferToUInt(e, t):
    a = 0
    for i in range(t):
        if i >= len(e):
            return float('nan')
        a = 256 * a + e[i]
    return a


def getBinaryNodeChildUInt(node, childTag, length):
    buff = getBinaryNodeChildBuffer(node, childTag)
    if buff is not None:
        return bufferToUInt(buff, length)
    return None


def assertNodeErrorFree(node):
    errNode = getBinaryNodeChild(node, 'error')
    if errNode is not None:
        attrs = prop(errNode, 'attrs')
        text = attrs.get('text')
        raise Boom(text if isTruthy(text) else 'Unknown error', JSObject({'data': jsNumber(attrs.get('code'))}))


def reduceBinaryNodeToDictionary(node, tag):
    nodes = getBinaryNodeChildren(node, tag)
    dictionary = JSObject({})
    for child in nodes:
        attrs = prop(child, 'attrs')
        if isinstance(attrs.get('name'), str):
            dictionary[attrs.get('name')] = attrs.get('value') or attrs.get('config_value')
        else:
            dictionary[jsString(attrs.get('config_code'))] = attrs.get('value') or attrs.get('config_value')
    return dictionary


def getBinaryNodeMessages(node, proto):
    msgs = []
    content = prop(node, 'content')
    if isinstance(content, list):
        for item in content:
            if item is None:
                raise JSError("Cannot read properties of undefined (reading 'tag')")
            if prop(item, 'tag') == 'message':
                msgs.append(proto.WebMessageInfo.decode(prop(item, 'content')).toJSON())
    return msgs


def binaryNodeToString(node, i=0):
    if not isTruthy(node):
        return node
    if isinstance(node, str):
        return tabs(i) + node
    if isinstance(node, (bytes, bytearray)):
        return tabs(i) + bytes(node).hex()
    if isinstance(node, list):
        return '\n'.join(tabs(i + 1) + jsString(binaryNodeToString(x, i + 1)) for x in node)

    children = binaryNodeToString(prop(node, 'content'), i + 1)
    attrs = prop(node, 'attrs')
    if not isTruthy(attrs):
        attrs = JSObject({})
    attrText = ' '.join(jsString(key) + "='" + jsString(attrs[key]) + "'" for key in jsKeys(attrs) if attrs[key] is not None)
    tag = '<' + jsString(prop(node, 'tag')) + ' ' + attrText
    if isTruthy(children):
        content = '>\n' + jsString(children) + '\n' + tabs(i) + '</' + jsString(prop(node, 'tag')) + '>'
    else:
        content = '/>'
    return tag + content


def getBizBinaryNode(message):
    interactiveMessage = prop(message, 'interactiveMessage')
    flowMsg = prop(interactiveMessage, 'nativeFlowMessage')
    buttons = prop(flowMsg, 'buttons')
    firstButton = buttons[0] if isinstance(buttons, list) and len(buttons) > 0 else None
    firstButtonName = prop(firstButton, 'name')
    qualityContent = JSObject({
        'tag': 'quality_control',
        'attrs': JSObject({
            'decision_id': randomBytes(20).hex(),
            'source_type': 'third_party'
        }),
        'content': DECISION_SOURCE_CONTENT
    })
    bizAttributes = JSObject({
        'actual_actors': '2',
        'host_storage': '2',
        'privacy_mode_ts': str(int(now() / 1000))
    })

    if firstButtonName == 'review_and_pay' or firstButtonName == 'payment_info':
        bizAttributes['native_flow_name'] = 'order_details' if firstButtonName == 'review_and_pay' else firstButtonName
        return JSObject({'tag': 'biz', 'attrs': bizAttributes, 'content': [qualityContent]})

    if firstButtonName and FLOWS_MAP[firstButtonName]:
        return JSObject({
            'tag': 'biz',
            'attrs': bizAttributes,
            'content': [
                JSObject({
                    'tag': 'interactive',
                    'attrs': NATIVE_FLOW_ATTRIBUTE,
                    'content': [JSObject({'tag': 'native_flow', 'attrs': JSObject({'v': '2', 'name': firstButtonName})})]
                }),
                qualityContent
            ]
        })

    if (
        isTruthy(flowMsg)
        or isTruthy(prop(interactiveMessage, 'carouselMessage'))
        or isTruthy(prop(message, 'buttonsMessage'))
        or isTruthy(prop(message, 'templateMessage'))
    ):
        return JSObject({'tag': 'biz', 'attrs': bizAttributes, 'content': [MIXED_NATIVE_FLOW, qualityContent]})

    if isTruthy(prop(message, 'listMessage')):
        return JSObject({'tag': 'biz', 'attrs': bizAttributes, 'content': [LIST_TYPE_CONTENT, qualityContent]})

    return JSObject({'tag': 'biz', 'attrs': bizAttributes, 'content': [qualityContent]})
