from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.voip.shim.util import optGet


WA_DEFAULTS = JSObject({
    'HOST_DOMAIN': 's.whatsapp.net',
    'GROUP_SERVER': 'g.us',
    'BROADCAST_SERVER': 'broadcast',
    'LID_SERVER': 'lid',
    'HOSTED_SERVER': 'hosted',
    'HOSTED_LID_SERVER': 'hosted.lid',
    'HOSTED_DEVICE_ID': 99,
    'MSGR_SERVER': 'msgr',
    'INTEROP_SERVER': 'interop',
    'NEWSLETTER_SERVER': 'newsletter',
    'BOT_SERVER': 'bot',
    'STATUS_BROADCAST_JID': 'status@broadcast',
})

WA_MESSAGE_TAGS = JSObject({
    'MESSAGE': 'message',
    'ENC': 'enc',
    'RECEIPT': 'receipt',
    'ACK': 'ack',
    'ERROR': 'error',
})

KNOWN_SERVERS = [
    WA_DEFAULTS['LID_SERVER'],
    WA_DEFAULTS['HOST_DOMAIN'],
    WA_DEFAULTS['GROUP_SERVER'],
    WA_DEFAULTS['BROADCAST_SERVER'],
    WA_DEFAULTS['NEWSLETTER_SERVER'],
    WA_DEFAULTS['HOSTED_SERVER'],
    WA_DEFAULTS['HOSTED_LID_SERVER'],
    WA_DEFAULTS['BOT_SERVER'],
    WA_DEFAULTS['MSGR_SERVER'],
    WA_DEFAULTS['INTEROP_SERVER'],
]


def internServerAt(jid, from_):
    length = len(jid) - from_
    for server in KNOWN_SERVERS:
        if len(server) == length and jid.startswith(server, from_):
            return server
    return jid[from_:]


def findAtIndex(jid):
    atIndex = jid.find('@')
    if atIndex < 1 or atIndex >= len(jid) - 1:
        raise JSError(f'invalid jid: {jid}')
    return atIndex


def isJidType(jid, type_):
    atIndex = len(jid) - len(type_) - 1
    if atIndex < 1 or jid[atIndex] != '@' or not jid.endswith(type_):
        return False
    for i in range(atIndex):
        if jid[i] == '@':
            return False
    return True


def isLidJid(jid):
    return isJidType(jid, WA_DEFAULTS['LID_SERVER'])


def isUserJid(jid):
    return isJidType(jid, WA_DEFAULTS['HOST_DOMAIN'])


def isGroupJid(jid):
    return isJidType(jid, WA_DEFAULTS['GROUP_SERVER'])


def parseSignalAddressFromJid(jid):
    atIndex = findAtIndex(jid)
    colonIndex = jid.find(':', 0)
    server = internServerAt(jid, atIndex + 1)
    if colonIndex == -1 or colonIndex > atIndex:
        return JSObject({'user': jid[:atIndex], 'server': server, 'device': 0})
    if colonIndex >= atIndex - 1:
        raise JSError(f'invalid jid device: {jid}')
    device = 0
    for i in range(colonIndex + 1, atIndex):
        digit = ord(jid[i]) - 48
        if digit < 0 or digit > 9:
            raise JSError(f'invalid jid device: {jid}')
        device = device * 10 + digit
        if device > 2 ** 53 - 1:
            raise JSError(f'invalid jid device: {jid}')
    return JSObject({'user': jid[:colonIndex], 'server': server, 'device': device})


def isHostedServerAt(jid, from_):
    length = len(jid) - from_
    if length == len(WA_DEFAULTS['HOSTED_SERVER']):
        return jid.startswith(WA_DEFAULTS['HOSTED_SERVER'], from_)
    if length == len(WA_DEFAULTS['HOSTED_LID_SERVER']):
        return jid.startswith(WA_DEFAULTS['HOSTED_LID_SERVER'], from_)
    return False


def canonicalizeSignalServer(server, hostDomain=None):
    if hostDomain is None:
        hostDomain = WA_DEFAULTS['HOST_DOMAIN']
    if server == WA_DEFAULTS['HOSTED_SERVER']:
        return hostDomain
    if server == WA_DEFAULTS['HOSTED_LID_SERVER']:
        return WA_DEFAULTS['LID_SERVER']
    return server


def toUserJid(jid, options=None):
    options = JSObject({}) if options is None else options
    canonicalize = optGet(options, 'canonicalizeSignalServer') is True
    atIndex = jid.find('@')
    if atIndex >= 1 and atIndex < len(jid) - 1:
        colonIndex = jid.find(':', 0)
        if (colonIndex == -1 or colonIndex > atIndex) and (not canonicalize or not isHostedServerAt(jid, atIndex + 1)):
            return jid
    address = parseSignalAddressFromJid(jid)
    baseServer = address.get('server') if address.get('server') is not None else WA_DEFAULTS['HOST_DOMAIN']
    if canonicalize:
        hostDomain = optGet(options, 'hostDomain')
        if hostDomain is None:
            hostDomain = WA_DEFAULTS['HOST_DOMAIN']
        server = canonicalizeSignalServer(baseServer, hostDomain)
    else:
        server = baseServer
    if server == baseServer and len(jid) == len(address.get('user')) + 1 + len(server):
        return jid
    return f"{address.get('user')}@{server}"


def normalizeDeviceJid(jid):
    address = parseSignalAddressFromJid(jid)
    if address.get('device') == 0:
        return f"{address.get('user')}@{address.get('server')}"
    return f"{address.get('user')}:{address.get('device')}@{address.get('server')}"
