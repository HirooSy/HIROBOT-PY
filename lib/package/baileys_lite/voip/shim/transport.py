from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.voip.shim.protocol import WA_MESSAGE_TAGS
from baileys_lite.voip.shim.util import TEXT_DECODER, optGet


EMPTY_NODE_CHILDREN = []


def getNodeChildren(node):
    content = optGet(node, 'content')
    return content if isinstance(content, list) else EMPTY_NODE_CHILDREN


def getNodeTextContent(node):
    if node is None:
        return None
    content = optGet(node, 'content')
    if isinstance(content, (bytes, bytearray)):
        return TEXT_DECODER.decode(content)
    if isinstance(content, str):
        return content
    return None


def findNodeChild(node, tag):
    content = optGet(node, 'content')
    if not isinstance(content, list):
        return None
    for child in content:
        if optGet(child, 'tag') == tag:
            return child
    return None


def getFirstNodeChild(node):
    children = getNodeChildren(node)
    return children[0] if children else None


def getNodeChildrenByTag(node, tag):
    content = optGet(node, 'content')
    if not isinstance(content, list):
        return EMPTY_NODE_CHILDREN
    tagged = [child for child in content if optGet(child, 'tag') == tag]
    return tagged if tagged else EMPTY_NODE_CHILDREN


def hasNodeChild(node, tag):
    return findNodeChild(node, tag) is not None


def buildAckNode(input_):
    kind = optGet(input_, 'kind')
    if kind != 'custom':
        raise JSError(f'buildAckNode: unsupported kind "{kind}"')
    attrs = JSObject({
        'class': optGet(input_, 'ackClass'),
        'to': optGet(input_, 'to'),
    })
    for key in ('id', 'type', 'participant', 'recipient', 'from'):
        value = optGet(input_, key)
        if value:
            attrs[key] = value
    error = optGet(input_, 'error')
    if error is not None:
        attrs['error'] = str(error)
    return JSObject({
        'tag': WA_MESSAGE_TAGS['ACK'],
        'attrs': attrs,
        'content': optGet(input_, 'content'),
    })


def buildReceiptNode(input_):
    kind = optGet(input_, 'kind')
    if kind != 'custom':
        raise JSError(f'buildReceiptNode: unsupported kind "{kind}"')
    return JSObject({
        'tag': WA_MESSAGE_TAGS['RECEIPT'],
        'attrs': JSObject({**(optGet(input_, 'attrs') or {})}),
        'content': optGet(input_, 'content'),
    })
