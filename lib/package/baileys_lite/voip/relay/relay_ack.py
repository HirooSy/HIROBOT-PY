import functools
import math
import re

from baileys_lite.foundation.node_runtime import JSObject
from baileys_lite.voip.shim.transport import getNodeChildren, getNodeChildrenByTag, getNodeTextContent
from baileys_lite.voip.shim.util import base64ToBytes, bytesToBase64


PARSE_INT_RE = re.compile(r'^\s*([+-]?\d+)')


def parseInt(value, radix=10):
    match = PARSE_INT_RE.match(str(value))
    if match is None:
        return math.nan
    return int(match.group(1), radix)


def attrOf(node, name):
    attrs = node.get('attrs')
    if attrs is None:
        return None
    return attrs[name]


def isBytesLike(value):
    return isinstance(value, (bytes, bytearray))


def nullish(value, fallback):
    return fallback if value is None else value


def cmpRelays(a, b):
    if bool(a.get('isFna')) != bool(b.get('isFna')):
        return 1 if a.get('isFna') else -1
    aRtt = nullish(a.get('c2rRtt'), math.inf)
    bRtt = nullish(b.get('c2rRtt'), math.inf)
    diff = aRtt - bRtt
    if diff != diff:
        return 0
    return diff


def parseRelayFromAck(ackNode):
    relays = []
    participantJids = []
    participantSeen = set()
    uuid = ''
    selfPid = None
    peerPid = None
    hbhKey = None

    content = ackNode.get('content')
    if not content or not isinstance(content, list):
        return JSObject({'relays': relays, 'participantJids': participantJids, 'uuid': uuid})

    for child in content:
        if not isinstance(child, dict) or 'tag' not in child:
            continue
        if child.get('tag') == 'user' and isinstance(child.get('content'), list):
            for deviceNode in child.get('content'):
                if isinstance(deviceNode, dict) and 'tag' in deviceNode and \
                        deviceNode.get('tag') == 'device' and attrOf(deviceNode, 'jid'):
                    jid = attrOf(deviceNode, 'jid')
                    if jid not in participantSeen:
                        participantSeen.add(jid)
                        participantJids.append(jid)

        if child.get('tag') != 'relay':
            continue

        relayNode = child
        uuid = attrOf(relayNode, 'uuid') or ''
        if attrOf(relayNode, 'self_pid'):
            selfPid = parseInt(attrOf(relayNode, 'self_pid'), 10)
        if attrOf(relayNode, 'peer_pid'):
            peerPid = parseInt(attrOf(relayNode, 'peer_pid'), 10)

        relayContent = getNodeChildren(relayNode)
        for rc in getNodeChildrenByTag(relayNode, 'participant'):
            jid = attrOf(rc, 'jid')
            if jid and jid not in participantSeen:
                participantSeen.add(jid)
                participantJids.append(jid)

        relayKey = ''
        tokens = {}
        authTokens = {}
        rawTokens = {}
        rawAuthTokens = {}
        for rc in relayContent:
            if not isinstance(rc, dict) or 'tag' not in rc:
                continue
            rcNode = rc
            if rcNode.get('tag') == 'key' and rcNode.get('content'):
                relayKey = nullish(getNodeTextContent(rcNode), '')
            if rcNode.get('tag') == 'hbh_key' and rcNode.get('content'):
                rawKey = None
                if isBytesLike(rcNode.get('content')):
                    rawKey = rcNode.get('content')
                elif isinstance(rcNode.get('content'), str):
                    rawKey = base64ToBytes(rcNode.get('content'))
                if rawKey is not None:
                    if len(rawKey) == 30:
                        hbhKey = rawKey
                    elif len(rawKey) > 30:
                        asB64 = bytes(rawKey).decode('utf-8', errors='replace').strip()
                        decoded = base64ToBytes(asB64)
                        if len(decoded) == 30:
                            hbhKey = decoded
            if rcNode.get('tag') == 'token' and rcNode.get('content'):
                tokenId = nullish(attrOf(rcNode, 'id'), '0') or '0'
                tokenData = bytesToBase64(rcNode.get('content')) if isBytesLike(rcNode.get('content')) \
                    else str(rcNode.get('content'))
                tokens[tokenId] = tokenData
                if isBytesLike(rcNode.get('content')):
                    rawTokens[tokenId] = rcNode.get('content')
            if rcNode.get('tag') == 'auth_token' and rcNode.get('content'):
                authTokenId = attrOf(rcNode, 'id') or '0'
                authTokenData = bytesToBase64(rcNode.get('content')) if isBytesLike(rcNode.get('content')) \
                    else str(rcNode.get('content'))
                authTokens[authTokenId] = authTokenData
                if isBytesLike(rcNode.get('content')):
                    rawAuthTokens[authTokenId] = rcNode.get('content')

        for rcNode in getNodeChildrenByTag(relayNode, 'te2'):
            tokenId = attrOf(rcNode, 'token_id') or '0'
            authTokenId = attrOf(rcNode, 'auth_token_id') or ''
            token = tokens.get(tokenId) or ''
            authToken = authTokens.get(authTokenId) if authTokenId else None
            relayName = attrOf(rcNode, 'relay_name') or ''
            protocol = parseInt(attrOf(rcNode, 'protocol'), 10) if attrOf(rcNode, 'protocol') else 0
            isFna = attrOf(rcNode, 'is_fna') == '1'
            if not isBytesLike(rcNode.get('content')) or len(rcNode.get('content')) < 6:
                continue
            addrBytes = rcNode.get('content')
            addressBytes = bytes(addrBytes)
            if len(addrBytes) == 6:
                ip = '%d.%d.%d.%d' % (addrBytes[0], addrBytes[1], addrBytes[2], addrBytes[3])
                port = (addrBytes[4] << 8) | addrBytes[5]
                relays.append(JSObject({
                    'ip': ip,
                    'port': port,
                    'token': token,
                    'authToken': authToken,
                    'rawAuthToken': rawAuthTokens.get(authTokenId) if authTokenId else None,
                    'rawToken': rawTokens.get(tokenId),
                    'key': relayKey,
                    'relayId': parseInt(attrOf(rcNode, 'relay_id') or '0', 10),
                    'protocol': protocol,
                    'c2rRtt': parseInt(attrOf(rcNode, 'c2r_rtt'), 10) if attrOf(rcNode, 'c2r_rtt') else None,
                    'relayName': relayName,
                    'addressBytes': addressBytes,
                    'authTokenId': authTokenId or tokenId,
                    'isFna': isFna,
                }))

    relays.sort(key=functools.cmp_to_key(cmpRelays))
    return JSObject({'relays': relays, 'participantJids': participantJids, 'uuid': uuid,
                     'selfPid': selfPid, 'peerPid': peerPid, 'hbhKey': hbhKey})
