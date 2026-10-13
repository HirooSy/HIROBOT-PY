from baileys_lite.foundation.node_runtime import JSObject
from baileys_lite.voip.shim.core import createNoopLogger
from baileys_lite.voip.shim.protocol import normalizeDeviceJid
from baileys_lite.voip.shim.transport import buildAckNode, getFirstNodeChild
from baileys_lite.voip.shim.util import toError


RECEIPT_CALL_TAGS = {
    'offer',
    'accept',
    'preaccept',
    'terminate',
    'transport',
    'relaylatency',
    'mute_v2'
}


async def routeCallStanza(manager, deps, node, logger=None):
    log = logger if logger is not None else createNoopLogger()
    inner = getFirstNodeChild(node)
    if not inner:
        return None
    tag = inner.get('tag')
    peerJid = node.get('attrs').get('from')
    await deps.get('lowLevelCoordinator').sendNode(buildAckNode(JSObject({
        'kind': 'custom',
        'ackClass': 'call',
        'to': peerJid,
        'id': node.get('attrs').get('id'),
        'type': tag
    })))
    try:
        normalizedPeerJid = normalizeDeviceJid(peerJid)
    except Exception as err:
        log.warn('failed to normalize call peer jid', JSObject({
            'from': peerJid,
            'message': toError(err).message
        }))
        return tag
    if tag == 'offer':
        await manager.handleCallOffer(node, normalizedPeerJid)
    elif tag == 'preaccept':
        await manager.handleCallPreaccept(node, normalizedPeerJid)
    elif tag == 'accept':
        await manager.handleCallAccept(node, normalizedPeerJid)
    elif tag == 'transport':
        await manager.handleCallTransport(node, normalizedPeerJid)
    elif tag == 'terminate':
        await manager.handleCallTerminate(node)
    elif tag == 'relaylatency':
        await manager.handleCallRelaylatency(node, normalizedPeerJid)
    elif tag == 'mute_v2':
        await manager.handleCallMuteV2(node, normalizedPeerJid)
    elif tag == 'user_action':
        manager.handleCallUserAction(node, normalizedPeerJid)
    elif tag == 'raise_hand':
        manager.handleCallRaiseHand(node, normalizedPeerJid)
    elif tag == 'screen_share' or tag == 'screen':
        manager.handleCallScreenShare(node)
    elif tag == 'video':
        await manager.handleCallVideo(node)
    elif tag == 'relay_election':
        manager.handleRelayElection(node)
    return tag


async def routeCallAck(manager, node):
    await manager.handleCallAck(node)


async def routeCallReceipt(deps, node):
    inner = getFirstNodeChild(node)
    if not inner:
        return False
    if inner.get('tag') not in RECEIPT_CALL_TAGS:
        return False
    peerJid = node.get('attrs').get('from')
    await deps.get('lowLevelCoordinator').sendNode(buildAckNode(JSObject({
        'kind': 'custom',
        'ackClass': 'receipt',
        'to': peerJid,
        'id': node.get('attrs').get('id'),
        'type': node.get('attrs').get('type') or 'retry'
    })))
    return True

