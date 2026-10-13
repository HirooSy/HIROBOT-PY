import asyncio
import math
import random
import re

from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.voip.crypto.primitives import randomBytes
from baileys_lite.voip.shim.core import createNoopLogger, unpadPkcs7, writeRandomPadMax16
from baileys_lite.voip.shim.proto import getProto
from baileys_lite.voip.shim.protocol import parseSignalAddressFromJid, toUserJid
from baileys_lite.voip.shim.transport import (
    buildReceiptNode,
    findNodeChild,
    getFirstNodeChild,
    getNodeChildren,
    getNodeChildrenByTag,
)
from baileys_lite.voip.shim.util import bytesToHex, toError


DECIMAL_LITERAL = re.compile(r'^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$')


def isFiniteNumber(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def jsNumber(value):
    if value is None:
        return float('nan')
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if text == '':
        return 0
    if text in ('Infinity', '+Infinity'):
        return math.inf
    if text == '-Infinity':
        return -math.inf
    if re.match(r'^0[xX][0-9a-fA-F]+$', text):
        return int(text[2:], 16)
    if re.match(r'^0[oO][0-7]+$', text):
        return int(text[2:], 8)
    if re.match(r'^0[bB][01]+$', text):
        return int(text[2:], 2)
    if not DECIMAL_LITERAL.match(text):
        return float('nan')
    number = float(text)
    if math.isfinite(number) and number.is_integer():
        return int(number)
    return number


def toSafeNonNegativeInt(value):
    number = jsNumber(value)
    if not isFiniteNumber(number) or not float(number).is_integer():
        return None
    if abs(number) > 2 ** 53 - 1 or number < 0:
        return None
    return int(number)


def optGet(o, k):
    if o is None:
        return None
    if isinstance(o, dict):
        return o.get(k)
    try:
        return o[k]
    except Exception:
        return getattr(o, k, None)


def parseIntJS(value, radix=10):
    text = str(value).lstrip(' \t\n\r\f\v')
    sign = 1
    if text[:1] in ('+', '-'):
        sign = -1 if text[0] == '-' else 1
        text = text[1:]
    digits = ''
    for ch in text:
        if ch.isdigit() and ch.isascii():
            digits += ch
        else:
            break
    if digits == '':
        return None
    return sign * int(digits, radix)


async def encodeWAMessage(message):
    proto = await getProto()
    return writeRandomPadMax16(proto.Message.encode(message).finish())


def generateCallId():
    data = bytes(int(random.random() * 256) for _ in range(16))
    return bytesToHex(data).upper()


def generateCallStanzaId():
    return bytesToHex(randomBytes(16)).upper()


def extractNodeInfo(node):
    innerNode = getFirstNodeChild(node)
    if not innerNode:
        return None
    return JSObject({
        'tag': innerNode.get('tag'),
        'peerJid': node.get('attrs').get('from'),
        'callId': optGet(innerNode.get('attrs'), 'call-id') or '',
        'peerPlatform': optGet(node.get('attrs'), 'platform') or '',
        'peerAppVersion': optGet(node.get('attrs'), 'version') or '',
        'epochId': optGet(innerNode.get('attrs'), 'e'),
        'timestamp': optGet(innerNode.get('attrs'), 't'),
        'innerNode': innerNode
    })


def toRelayEndpoint(node):
    attrs = node.get('attrs')
    c2rRtt = None
    if optGet(attrs, 'c2r-rtt'):
        c2rRtt = parseIntJS(attrs.get('c2r-rtt'))
    relay = JSObject({
        'ip': optGet(attrs, 'ip') or '',
        'port': parseIntJS(optGet(attrs, 'port') or '3480'),
        'token': optGet(attrs, 'token') or '',
        'key': optGet(attrs, 'relay-key') or optGet(attrs, 'key') or '',
        'relayId': parseIntJS(optGet(attrs, 'relay-id') or '0'),
        'c2rRtt': c2rRtt
    })
    return relay if relay.get('ip') and relay.get('token') else None


def extractRelayEndpoints(node):
    relayNodes = list(getNodeChildrenByTag(node, 'relay'))
    for wrapper in getNodeChildrenByTag(node, 'relays'):
        relayNodes.extend(getNodeChildrenByTag(wrapper, 'relay'))
    relays = []
    for relayNode in relayNodes:
        relay = toRelayEndpoint(relayNode)
        if relay:
            relays.append(relay)
    relays.sort(key=lambda r: math.inf if r.get('c2rRtt') is None else r.get('c2rRtt'))
    return relays


async def decryptCallKey(deps, node, peerJid, logger=None):
    log = logger if logger is not None else createNoopLogger()
    proto = await getProto()

    def isEnc(child):
        return child.get('tag') == 'enc' and bool(optGet(child.get('attrs'), 'type'))

    encNodes = [child for child in getNodeChildren(node) if isEnc(child)]
    destinationNode = findNodeChild(node, 'destination')
    if destinationNode:
        for toNode in getNodeChildren(destinationNode):
            if toNode.get('tag') == 'to':
                encNodes.extend([child for child in getNodeChildren(toNode) if isEnc(child)])
    address = parseSignalAddressFromJid(peerJid)
    for encNode in encNodes:
        if not isinstance(encNode.get('content'), (bytes, bytearray)):
            continue
        try:
            decrypted = await deps.get('signalProtocol').decryptMessage(address, JSObject({
                'type': encNode.get('attrs').get('type'),
                'ciphertext': encNode.get('content')
            }))
            message = proto.Message.decode(unpadPkcs7(decrypted))
            callMsg = optGet(message, 'call')
            callKey = optGet(callMsg, 'callKey')
            if callKey and len(callKey) == 32:
                return callKey
        except Exception as err:
            log.trace('call key decrypt candidate failed', JSObject({'message': toError(err).message}))
    return None


CAPABILITY_OFFER = bytes([0x01, 0x05, 0xf7, 0x09, 0xe4, 0xbb, 0x07])
CAPABILITY_PREACCEPT = bytes([0x01, 0x05, 0xff, 0x09, 0xe4, 0xbb, 0x07])


async def buildCallParticipantNodes(deps, devices, callKey):
    await deps.get('sessionResolver').ensureSessionsBatch(devices)
    plaintext = await encodeWAMessage(JSObject({'call': JSObject({'callKey': callKey})}))
    encrypted = await asyncio.gather(*[
        deps.get('signalProtocol').encryptMessage(parseSignalAddressFromJid(jid), plaintext)
        for jid in devices
    ])
    nodes = [
        JSObject({
            'tag': 'to',
            'attrs': JSObject({'jid': jid}),
            'content': [
                JSObject({
                    'tag': 'enc',
                    'attrs': JSObject({'v': '2', 'type': encrypted[index].get('type'), 'count': '0'}),
                    'content': encrypted[index].get('ciphertext')
                })
            ]
        })
        for index, jid in enumerate(devices)
    ]
    return JSObject({
        'nodes': nodes,
        'shouldIncludeDeviceIdentity': any(entry.get('type') == 'pkmsg' for entry in encrypted)
    })


async def buildOfferStanza(deps, stores, callId, callKey, peerJid, isVideo, logger=None):
    log = logger if logger is not None else createNoopLogger()
    creds = deps.get('authClient').getCurrentCredentials()
    callCreator = optGet(creds, 'meLid') or optGet(creds, 'meJid') or ''
    synced = await deps.get('signalDeviceSync').syncDeviceList([peerJid])
    devices = [jid for entry in synced for jid in entry.get('deviceJids')]
    if len(devices) == 0:
        raise JSError(f'no device sessions to encrypt the call offer for {peerJid}')
    built = await buildCallParticipantNodes(deps, devices, callKey)
    destinations = built.get('nodes')
    shouldIncludeDeviceIdentity = built.get('shouldIncludeDeviceIdentity')
    offerContent = []
    try:
        peerJidNormalized = toUserJid(peerJid)
        tcTokenRecord = await stores.get('privacyToken').getByJid(peerJidNormalized)
        tctoken = optGet(tcTokenRecord, 'tcToken')
        if tctoken:
            offerContent.append(JSObject({
                'tag': 'privacy',
                'attrs': JSObject({}),
                'content': tctoken if isinstance(tctoken, (bytes, bytearray)) else bytes(tctoken)
            }))
    except Exception as err:
        log.trace('tctoken lookup failed', JSObject({'message': toError(err).message}))
    offerContent.append(JSObject({'tag': 'audio', 'attrs': JSObject({'enc': 'opus', 'rate': '8000'}), 'content': None}))
    offerContent.append(JSObject({'tag': 'audio', 'attrs': JSObject({'enc': 'opus', 'rate': '16000'}), 'content': None}))
    if isVideo:
        offerContent.append(JSObject({
            'tag': 'video',
            'attrs': JSObject({
                'enc': 'h.264',
                'dec': 'H264',
                'screen_width': '1920',
                'screen_height': '1080',
                'device_orientation': '0'
            }),
            'content': None
        }))
    offerContent.append(JSObject({'tag': 'net', 'attrs': JSObject({'medium': '3'}), 'content': None}))
    offerContent.append(JSObject({
        'tag': 'capability',
        'attrs': JSObject({'ver': '1'}),
        'content': CAPABILITY_OFFER
    }))
    offerContent.append(JSObject({'tag': 'destination', 'attrs': JSObject({}), 'content': destinations}))
    offerContent.append(JSObject({
        'tag': 'encopt',
        'attrs': JSObject({'keygen': '2'}),
        'content': None
    }))
    if shouldIncludeDeviceIdentity and optGet(creds, 'signedIdentity'):
        offerContent.append(JSObject({
            'tag': 'device-identity',
            'attrs': JSObject({}),
            'content': creds.get('signedIdentity')
        }))
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': peerJid, 'id': generateCallStanzaId()}),
        'content': [
            JSObject({
                'tag': 'offer',
                'attrs': JSObject({'call-id': callId, 'call-creator': callCreator}),
                'content': offerContent
            })
        ]
    })


async def buildAcceptStanza(deps, callId, callKey, peerJid, callCreator, isVideo):
    await deps.get('messageDispatch').syncSignalSession(callCreator)
    data = await encodeWAMessage(JSObject({'call': JSObject({'callKey': callKey})}))
    encNode = None
    shouldIncludeDeviceIdentity = False
    try:
        result = await deps.get('signalProtocol').encryptMessage(parseSignalAddressFromJid(callCreator), data)
        typ = result.get('type')
        ciphertext = result.get('ciphertext')
        if typ == 'pkmsg':
            shouldIncludeDeviceIdentity = True
        encNode = JSObject({
            'tag': 'enc',
            'attrs': JSObject({'v': '2', 'type': typ, 'count': '0'}),
            'content': ciphertext
        })
    except Exception as err:
        raise JSError(f"Failed to encrypt accept for {callCreator}: {toError(err).message}")
    acceptContent = [
        JSObject({'tag': 'audio', 'attrs': JSObject({'enc': 'opus', 'rate': '16000'})}),
        JSObject({'tag': 'net', 'attrs': JSObject({'medium': '3'})}),
        encNode,
        JSObject({'tag': 'encopt', 'attrs': JSObject({'keygen': '2'})})
    ]
    acceptSignedIdentity = optGet(deps.get('authClient').getCurrentCredentials(), 'signedIdentity')
    if shouldIncludeDeviceIdentity and acceptSignedIdentity:
        acceptContent.append(JSObject({
            'tag': 'device-identity',
            'attrs': JSObject({}),
            'content': acceptSignedIdentity
        }))
    if isVideo:
        acceptContent.append(JSObject({'tag': 'video', 'attrs': JSObject({'dec': 'H264', 'device_orientation': '0'})}))
    toJidClean = toUserJid(peerJid)
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': toJidClean, 'id': generateCallStanzaId()}),
        'content': [
            JSObject({
                'tag': 'accept',
                'attrs': JSObject({'call-id': callId, 'call-creator': callCreator}),
                'content': acceptContent
            })
        ]
    })


def buildTerminateStanza(peerJid, callId, callCreator, audioDurationMs, reason):
    attrs = JSObject({
        'call-id': callId,
        'call-creator': callCreator
    })
    if audioDurationMs is not None and audioDurationMs >= 0:
        ms = str(math.floor(audioDurationMs))
        attrs['duration'] = ms
        attrs['audio_duration'] = ms
    if reason is not None:
        attrs['reason'] = reason
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': peerJid, 'id': generateCallStanzaId()}),
        'content': [
            JSObject({
                'tag': 'terminate',
                'attrs': attrs,
                'content': None
            })
        ]
    })


def buildRelaylatencyForwardStanza(peerJid, callId, callCreator, teNodes, destinationJids):
    destinationContent = [
        JSObject({'tag': 'to', 'attrs': JSObject({'jid': jid}), 'content': None})
        for jid in destinationJids
    ]
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': toUserJid(peerJid), 'id': generateCallStanzaId()}),
        'content': [
            JSObject({
                'tag': 'relaylatency',
                'attrs': JSObject({'call-id': callId, 'call-creator': callCreator}),
                'content': [
                    *teNodes,
                    JSObject({'tag': 'destination', 'attrs': JSObject({}), 'content': destinationContent})
                ]
            })
        ]
    })


def buildRejectStanza(peerJid, callId, callCreator):
    toJidClean = toUserJid(peerJid)
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': toJidClean, 'id': generateCallStanzaId()}),
        'content': [
            JSObject({
                'tag': 'reject',
                'attrs': JSObject({'call-id': callId, 'call-creator': callCreator})
            })
        ]
    })


def buildPreacceptStanza(peerJid, callId, callCreator):
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': peerJid, 'id': generateCallStanzaId()}),
        'content': [
            JSObject({
                'tag': 'preaccept',
                'attrs': JSObject({'call-id': callId, 'call-creator': callCreator}),
                'content': [
                    JSObject({'tag': 'audio', 'attrs': JSObject({'enc': 'opus', 'rate': '16000'})}),
                    JSObject({'tag': 'encopt', 'attrs': JSObject({'keygen': '2'})}),
                    JSObject({'tag': 'capability', 'attrs': JSObject({'ver': '1'}), 'content': CAPABILITY_PREACCEPT})
                ]
            })
        ]
    })


def buildRelayLatencyStanza(peerJid, callId, callCreator, relays, destinationJids, meId):
    seenRelays = set()
    teNodes = []
    for relay in relays:
        if not relay.get('relayName') or relay.get('relayName') in seenRelays:
            continue
        seenRelays.add(relay.get('relayName'))
        encodedLatency = 0x2000000 + (relay.get('latency') or 0)
        teNodes.append(JSObject({
            'tag': 'te',
            'attrs': JSObject({
                'latency': str(encodedLatency),
                'relay_name': relay.get('relayName')
            }),
            'content': relay.get('addressBytes') or None
        }))
    destinationContent = [
        JSObject({'tag': 'to', 'attrs': JSObject({'jid': jid}), 'content': None})
        for jid in destinationJids
    ]
    relayLatencyContent = [*teNodes]
    if len(destinationContent) > 0:
        relayLatencyContent.append(JSObject({
            'tag': 'destination',
            'attrs': JSObject({}),
            'content': destinationContent
        }))
    toJidClean = toUserJid(peerJid)
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': toJidClean, 'id': generateCallStanzaId()}),
        'content': [
            JSObject({
                'tag': 'relaylatency',
                'attrs': JSObject({'call-id': callId, 'call-creator': callCreator}),
                'content': relayLatencyContent
            })
        ]
    })


def buildTransportStanza(peerJid, callId, callCreator, meId, messageType='0', p2pCandRound='0'):
    if messageType is None:
        messageType = '0'
    if p2pCandRound is None:
        p2pCandRound = '0'
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': peerJid, 'id': generateCallStanzaId()}),
        'content': [
            JSObject({
                'tag': 'transport',
                'attrs': JSObject({
                    'call-id': callId,
                    'call-creator': callCreator,
                    'transport-message-type': messageType,
                    'p2p-cand-round': p2pCandRound
                }),
                'content': [
                    JSObject({
                        'tag': 'net',
                        'attrs': JSObject({'medium': '2', 'protocol': '0'}),
                        'content': None
                    })
                ]
            })
        ]
    })


def buildMuteV2Stanza(peerDeviceJid, callId, callCreator, muteState, meId):
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': peerDeviceJid, 'id': generateCallStanzaId()}),
        'content': [
            JSObject({
                'tag': 'mute_v2',
                'attrs': JSObject({
                    'call-id': callId,
                    'call-creator': callCreator,
                    'mute-state': str(muteState)
                })
            })
        ]
    })


def parseMuteV2(inner):
    rawMuteState = optGet(inner.get('attrs'), 'mute-state')
    muted = None
    if rawMuteState == '1':
        muted = True
    elif rawMuteState == '0':
        muted = False
    return JSObject({'muted': muted, 'isRequest': optGet(inner.get('attrs'), 'request-state') is not None})


RAISE_HAND_ACTION = 'raise_hand'
RAISE_HAND_STATE_ATTR = 'raise-hand-state'


def buildRaiseHandStanza(peerDeviceJid, callId, callCreator, raised, broadcast=False):
    attrs = JSObject({
        'call-id': callId,
        'call-creator': callCreator,
        'action': RAISE_HAND_ACTION
    })
    if broadcast:
        attrs['broadcast'] = '1'
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': peerDeviceJid, 'id': generateCallStanzaId()}),
        'content': [
            JSObject({
                'tag': 'user_action',
                'attrs': attrs,
                'content': [
                    JSObject({
                        'tag': RAISE_HAND_ACTION,
                        'attrs': JSObject({RAISE_HAND_STATE_ATTR: '1' if raised else '0'})
                    })
                ]
            })
        ]
    })


def parseRaiseHandState(node):
    stateNode = node if node.get('tag') == RAISE_HAND_ACTION else findNodeChild(node, RAISE_HAND_ACTION)
    raw = optGet(optGet(stateNode, 'attrs'), RAISE_HAND_STATE_ATTR)
    if raw == '1':
        return True
    if raw == '0':
        return False
    return None


def buildAcceptReceiptStanza(peerDeviceJid, acceptMsgId, callId, callCreator, ourJid):
    return buildReceiptNode(JSObject({
        'kind': 'custom',
        'attrs': JSObject({'to': peerDeviceJid, 'id': acceptMsgId, 'from': ourJid}),
        'content': [JSObject({'tag': 'accept', 'attrs': JSObject({'call-id': callId, 'call-creator': callCreator})})]
    }))


VideoState = JSObject({
    'Disabled': 0,
    'Enabled': 1,
    'UpgradeRequest': 3,
    'UpgradeAccept': 4,
    'UpgradeReject': 5,
    'Stopped': 6,
    'UpgradeCancel': 8,
    'UpgradeRequestV2': 11
})
VideoDecRequest = 'H264'
VideoDecAccept = 'H264,AV1'


def buildVideoStateStanza(peerJid, callId, callCreator, state, options=None):
    if options is None:
        options = JSObject({})
    dec = options.get('dec')
    deviceOrientation = options.get('deviceOrientation')
    attrs = JSObject({
        'call-id': callId,
        'call-creator': callCreator,
        'state': str(state)
    })
    if dec:
        attrs['dec'] = dec
    if state == VideoState['UpgradeRequestV2']:
        attrs['voip_settings'] = 'video'
    if deviceOrientation is not None:
        attrs['device_orientation'] = str(deviceOrientation)
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': peerJid, 'id': generateCallStanzaId()}),
        'content': [JSObject({'tag': 'video', 'attrs': attrs})]
    })


ENCRYPTED_TAGS = ['preaccept', 'accept']


def needsDecryption(tag):
    return tag in ENCRYPTED_TAGS
