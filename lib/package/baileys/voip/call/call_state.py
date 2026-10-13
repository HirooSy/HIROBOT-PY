from baileys.foundation.node_runtime import JSError, JSObject, now
from baileys.voip.types import CallDirection, CallMediaType, CallState


class CallInfo:
    def __init__(self, init):
        self.callId = None
        self.peerJid = None
        self.callCreator = None
        self.direction = None
        self.mediaType = None
        self.stateData = None
        self.createdAt = None
        self.groupJid = None
        self.isOffline = None
        self.callerPn = None
        self.encryptionKey = None
        self.relayData = None
        self.electedRelayIdx = None
        self.raisedHands = set()
        self.peerScreenShare = None
        self.callId = init.get('callId')
        self.peerJid = init.get('peerJid')
        self.callCreator = init.get('callCreator')
        self.direction = init.get('direction')
        self.mediaType = init.get('mediaType')
        self.stateData = init.get('stateData')
        self.createdAt = init.get('createdAt') if init.get('createdAt') is not None else now()
        self.groupJid = init.get('groupJid')
        self.isOffline = init.get('isOffline') if init.get('isOffline') is not None else False
        self.callerPn = init.get('callerPn')
        self.encryptionKey = init.get('encryptionKey')
        self.relayData = init.get('relayData')
        self.electedRelayIdx = init.get('electedRelayIdx')

    @staticmethod
    def newOutgoing(callId, peerJid, ourJid, mediaType):
        return CallInfo(JSObject({
            'callId': callId,
            'peerJid': peerJid,
            'callCreator': ourJid,
            'direction': CallDirection.Outgoing,
            'mediaType': mediaType,
            'stateData': JSObject({
                'state': CallState.Initiating,
                'audioMuted': False,
                'videoOff': mediaType != CallMediaType.Video
            })
        }))

    @staticmethod
    def newIncoming(callId, peerJid, callCreator, callerPn, mediaType):
        return CallInfo(JSObject({
            'callId': callId,
            'peerJid': peerJid,
            'callCreator': callCreator,
            'direction': CallDirection.Incoming,
            'mediaType': mediaType,
            'callerPn': callerPn,
            'stateData': JSObject({
                'state': CallState.IncomingRinging,
                'audioMuted': False,
                'videoOff': mediaType != CallMediaType.Video
            })
        }))

    @property
    def isInitiator(self):
        return self.direction == CallDirection.Outgoing

    @property
    def isActive(self):
        return self.stateData.get('state') == CallState.Active

    @property
    def isRinging(self):
        return (self.stateData.get('state') == CallState.Ringing or
                self.stateData.get('state') == CallState.IncomingRinging)

    @property
    def isEnded(self):
        return self.stateData.get('state') == CallState.Ended

    @property
    def canAccept(self):
        return self.stateData.get('state') == CallState.IncomingRinging and not self.stateData.get('acceptBlocked')

    @property
    def isAcceptBlocked(self):
        return self.stateData.get('acceptBlocked') is True

    @property
    def canReject(self):
        return (self.stateData.get('state') == CallState.IncomingRinging or
                self.stateData.get('state') == CallState.Ringing)

    def applyTransition(self, transition):
        s = self.stateData
        ttype = transition.get('type')
        if ttype == 'offer_sent':
            if s.get('state') != CallState.Initiating:
                raise InvalidTransition(s.get('state'), ttype)
            s['state'] = CallState.Ringing
        elif ttype == 'offer_received':
            if s.get('state') != CallState.Initiating:
                raise InvalidTransition(s.get('state'), ttype)
            s['state'] = CallState.IncomingRinging
            s['silenced'] = transition.get('silenced')
        elif ttype == 'remote_accepted':
            if s.get('state') != CallState.Ringing:
                raise InvalidTransition(s.get('state'), ttype)
            s['state'] = CallState.Connecting
            s['acceptedAt'] = now()
        elif ttype == 'local_accepted':
            if s.get('state') != CallState.IncomingRinging:
                raise InvalidTransition(s.get('state'), ttype)
            s['state'] = CallState.Connecting
            s['acceptedAt'] = now()
        elif ttype == 'remote_rejected':
            if s.get('state') != CallState.Ringing:
                raise InvalidTransition(s.get('state'), ttype)
            s['state'] = CallState.Ended
            s['endedAt'] = now()
            s['endReason'] = transition.get('reason')
        elif ttype == 'local_rejected':
            if s.get('state') != CallState.IncomingRinging:
                raise InvalidTransition(s.get('state'), ttype)
            s['state'] = CallState.Ended
            s['endedAt'] = now()
            s['endReason'] = transition.get('reason')
        elif ttype == 'media_connected':
            if s.get('state') != CallState.Connecting:
                raise InvalidTransition(s.get('state'), ttype)
            s['state'] = CallState.Active
            s['connectedAt'] = now()
            s['videoOff'] = self.mediaType != CallMediaType.Video
        elif ttype == 'terminated':
            if s.get('state') == CallState.Ended:
                raise InvalidTransition(s.get('state'), ttype)
            if s.get('state') == CallState.Active and s.get('connectedAt') is not None:
                s['durationSecs'] = (now() - s.get('connectedAt')) // 1000
            elif s.get('state') == CallState.OnHold and s.get('connectedAt') is not None:
                s['durationSecs'] = (now() - s.get('connectedAt')) // 1000
            s['state'] = CallState.Ended
            s['endedAt'] = now()
            s['endReason'] = transition.get('reason')
        elif ttype == 'hold':
            if s.get('state') != CallState.Active:
                raise InvalidTransition(s.get('state'), ttype)
            s['state'] = CallState.OnHold
        elif ttype == 'resume':
            if s.get('state') != CallState.OnHold:
                raise InvalidTransition(s.get('state'), ttype)
            s['state'] = CallState.Active
        elif ttype == 'audio_mute_changed':
            if s.get('state') != CallState.Active:
                raise InvalidTransition(s.get('state'), ttype)
            s['audioMuted'] = transition.get('muted')
        elif ttype == 'hand_raise_changed':
            if s.get('state') != CallState.Active:
                raise InvalidTransition(s.get('state'), ttype)
            s['handRaised'] = transition.get('raised')
        elif ttype == 'screen_share_changed':
            if s.get('state') != CallState.Active:
                raise InvalidTransition(s.get('state'), ttype)
            s['screenSharing'] = transition.get('sharing')
        elif ttype == 'video_state_changed':
            if s.get('state') != CallState.Active:
                raise InvalidTransition(s.get('state'), ttype)
            s['videoOff'] = transition.get('off')
        else:
            raise InvalidTransition(s.get('state'), ttype)


class InvalidTransition(JSError):
    def __init__(self, currentState, attempted):
        super().__init__(f"invalid transition '{attempted}' in state '{currentState}'")
        self.name = 'InvalidTransition'
        self.currentState = currentState
        self.attempted = attempted
