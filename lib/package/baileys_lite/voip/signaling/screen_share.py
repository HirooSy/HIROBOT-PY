from baileys_lite.foundation.node_runtime import JSObject
from baileys_lite.voip.signaling.signaling import generateCallStanzaId, isFiniteNumber, jsNumber


def tryAsNumber(value):
    if value is None or value == '':
        return None
    number = jsNumber(value)
    if not isFiniteNumber(number):
        return None
    return number


WA_SCREEN_SHARE_STATE = JSObject({
    'NotSupported': 0,
    'Started': 1,
    'Stopped': 2,
    'Failed': 3
})

WA_SCREEN_SHARE_VERSION = JSObject({
    'Invalid': -1,
    'Legacy': 0,
    'V1': 1,
    'V2': 2,
    'V3': 3,
    'V4': 4
})

WA_SCREEN_SHARE_SEND_VERSION = WA_SCREEN_SHARE_VERSION['V2']


def parseScreenShareNode(node):
    attrs = node.get('attrs') if node.get('attrs') is not None else JSObject({})
    state = tryAsNumber(attrs.get('screenshare_state'))
    requestState = tryAsNumber(attrs.get('request-state') if attrs.get('request-state') is not None else attrs.get('request_state'))
    if state is None and requestState is None:
        return None
    return JSObject({
        'state': state,
        'requestState': requestState,
        'version': tryAsNumber(attrs.get('version')),
        'screenWidth': tryAsNumber(attrs.get('screen_width')),
        'screenHeight': tryAsNumber(attrs.get('screen_height')),
        'deviceOrientation': tryAsNumber(attrs.get('device_orientation'))
    })


def buildScreenShareStanza(peerDeviceJid, callId, callCreator, screenShareState):
    return JSObject({
        'tag': 'call',
        'attrs': JSObject({'to': peerDeviceJid, 'id': generateCallStanzaId()}),
        'content': [
            JSObject({
                'tag': 'screen_share',
                'attrs': JSObject({
                    'call-id': callId,
                    'call-creator': callCreator,
                    'screenshare_state': str(screenShareState),
                    'version': str(WA_SCREEN_SHARE_SEND_VERSION)
                })
            })
        ]
    })
