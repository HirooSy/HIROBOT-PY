from baileys.foundation.node_runtime import EventEmitter, JSError, JSObject
from baileys.voip.call.call_state import CallInfo
from baileys.voip.call.WaCallMediaSession import WaCallMediaSession
from baileys.voip.crypto.encryption import generateCallKey
from baileys.voip.media.WaAudioEngine import WaAudioEngine
from baileys.voip.relay.relay_ack import parseRelayFromAck
from baileys.voip.shim.core import createNoopLogger
from baileys.voip.shim.protocol import isLidJid
from baileys.voip.shim.transport import hasNodeChild
from baileys.voip.shim.util import resolvePositive, toError
from baileys.voip.signaling.signaling import buildOfferStanza, decryptCallKey, extractNodeInfo, generateCallId
from baileys.voip.types import CallDirection, CallMediaType, CallState, EndCallReason


DEFAULT_MAX_CONCURRENT_CALLS = 1


def optGet(o, k):
    if o is None:
        return None
    if isinstance(o, dict):
        return o.get(k)
    try:
        return o[k]
    except Exception:
        return getattr(o, k, None)


def orDefault(value, fallback):
    return fallback if value is None else value


class WaCallManager(EventEmitter):
    def __init__(self, config):
        super().__init__()
        self.deps = config.get('deps')
        self.stores = config.get('stores')
        self.logger = orDefault(config.get('logger'), createNoopLogger())
        self.calls = {}
        self.maxConcurrentCalls = resolvePositive(config.get('maxConcurrentCalls'), DEFAULT_MAX_CONCURRENT_CALLS, 'maxConcurrentCalls')

    async def startCall(self, options):
        if self.activeCallCount >= self.maxConcurrentCalls:
            raise JSError(f'max concurrent calls reached ({self.maxConcurrentCalls})')
        callId = generateCallId()
        mediaType = CallMediaType.Video if options.get('isVideo') else CallMediaType.Audio
        creds = self.deps.get('authClient').getCurrentCredentials()
        callCreator = optGet(creds, 'meLid') or optGet(creds, 'meJid') or ''
        peerJid = await self.resolvePeerLid(options.get('peerJid'))
        info = CallInfo.newOutgoing(callId, peerJid, callCreator, mediaType)
        callKey = generateCallKey()
        info.encryptionKey = callKey
        session = self.createSession(info, JSObject({'videoConfig': options.get('videoConfig')}))
        try:
            session.resetOutgoingFlags()
            selfLid = optGet(creds, 'meLid') or optGet(creds, 'meJid') or ''
            await session.initMedia(selfLid, peerJid)
            offerStanza = await buildOfferStanza(
                self.deps,
                self.stores,
                callId,
                callKey,
                peerJid,
                orDefault(options.get('isVideo'), False),
                self.logger.child(JSObject({'component': 'signaling'}))
            )
            await self.deps.get('lowLevelCoordinator').sendNode(offerStanza)
        except BaseException as err:
            session.cleanup()
            self.calls.pop(callId, None)
            raise
        info.applyTransition(JSObject({'type': 'offer_sent'}))
        self.emitState(info)
        self.logger.debug('outgoing offer sent', JSObject({'callId': callId, 'peerJid': peerJid}))
        return callId

    async def acceptCall(self, callId):
        session = self.getSessionOrThrow(callId)
        if not session.info.canAccept:
            raise JSError(f'Call {callId} cannot be accepted in state {session.info.stateData.get('state')}')
        await session.acceptCall()

    async def rejectCall(self, callId, reason=EndCallReason.Declined):
        session = self.getSessionOrThrow(callId)
        await session.rejectCall(reason)
        self.calls.pop(callId, None)
        await self.maybeUnblockWaitingCalls()

    async def endCall(self, callId, reason=EndCallReason.UserEnded):
        session = self.calls.get(callId)
        if session is None or session.info.isEnded:
            return
        await session.endCall(reason)
        self.calls.pop(callId, None)
        await self.maybeUnblockWaitingCalls()

    def setMute(self, callId, muted):
        session = self.calls.get(callId)
        if session is not None:
            session.setMute(muted)

    async def setHandRaised(self, callId, raised):
        session = self.getSessionOrThrow(callId)
        await session.setHandRaised(raised)

    async def setScreenShare(self, callId, sharing):
        session = self.getSessionOrThrow(callId)
        await session.setScreenShare(sharing)

    def sendReaction(self, callId, reaction):
        session = self.getSessionOrThrow(callId)
        return session.sendReaction(reaction)

    async def loadAudio(self, callId, audioPath):
        session = self.getSessionOrThrow(callId)
        await session.loadAudio(audioPath)

    async def loadVideo(self, callId, videoPath):
        session = self.getSessionOrThrow(callId)
        await session.loadVideo(videoPath)

    async def enableVideoMidCall(self, callId):
        session = self.getSessionOrThrow(callId)
        await session.enableVideoMidCall()

    async def disableVideoMidCall(self, callId, options):
        session = self.getSessionOrThrow(callId)
        session.disableVideoMidCall(options)

    async def startVideoMidCall(self, callId):
        session = self.getSessionOrThrow(callId)
        await session.startVideoMidCall()

    async def stopVideoMidCall(self, callId, options):
        session = self.getSessionOrThrow(callId)
        await session.stopVideoMidCall(options)

    async def swapVideoSource(self, callId, videoPath):
        session = self.getSessionOrThrow(callId)
        await session.swapVideoSource(videoPath)

    def setExternalAudioMode(self, callId, enabled):
        session = self.getSessionOrThrow(callId)
        session.setExternalAudioMode(enabled)

    def feedLiveAudio(self, callId, data):
        session = self.calls.get(callId)
        result = None if session is None else session.feedLiveAudio(data)
        return orDefault(result, 0)

    def getLiveBufferMs(self, callId):
        session = self.calls.get(callId)
        result = None if session is None else session.getLiveBufferMs()
        return orDefault(result, 0)

    def getFeedWatermarksMs(self):
        return WaAudioEngine.feedWatermarksMs()

    def getCall(self, callId):
        session = self.calls.get(callId)
        return None if session is None else session.info

    def getCalls(self):
        result = []
        for session in self.calls.values():
            result.append(session.info)
        return result

    async def handleCallOffer(self, node, peerJid):
        nodeInfo = extractNodeInfo(node)
        if not optGet(nodeInfo, 'callId'):
            return
        callId = nodeInfo.get('callId')
        existing = self.calls.get(callId)
        if existing is not None:
            if not existing.info.isEnded:
                self.logger.debug('duplicate offer for active call, ignoring', JSObject({'callId': callId}))
                return
            existing.cleanup()
            self.calls.pop(callId, None)
        callCreator = optGet(optGet(nodeInfo.get('innerNode'), 'attrs'), 'call-creator') or peerJid
        isVideo = hasNodeChild(nodeInfo.get('innerNode'), 'video')
        callKey = await decryptCallKey(self.deps, nodeInfo.get('innerNode'), peerJid, self.logger.child(JSObject({'component': 'signaling'})))
        ack = parseRelayFromAck(nodeInfo.get('innerNode'))
        relays = ack.get('relays')
        participantJids = ack.get('participantJids')
        uuid = ack.get('uuid')
        selfPid = ack.get('selfPid')
        peerPid = ack.get('peerPid')
        hbhKey = ack.get('hbhKey')
        mediaType = CallMediaType.Video if isVideo else CallMediaType.Audio
        info = CallInfo.newIncoming(callId, peerJid, callCreator, None, mediaType)
        if callKey:
            info.encryptionKey = callKey
        if len(relays) > 0:
            info.relayData = JSObject({
                'endpoints': relays,
                'participantJids': participantJids,
                'uuid': uuid,
                'selfPid': selfPid,
                'peerPid': peerPid,
                'hbhKey': hbhKey
            })
        atCapacity = self.activeCallCount >= self.maxConcurrentCalls
        session = self.createSession(info, JSObject({'acceptBlocked': atCapacity}))
        if not atCapacity:
            try:
                creds = self.deps.get('authClient').getCurrentCredentials()
                selfLid = optGet(creds, 'meLid') or optGet(creds, 'meJid') or ''
                await session.initMedia(selfLid, peerJid)
                await session.sendIncomingPreaccept(peerJid)
                await session.sendIncomingRelayLatency()
            except Exception as err:
                self.logger.error('incoming call activation failed', JSObject({
                    'callId': callId,
                    'message': toError(err).message
                }))
                try:
                    info.applyTransition(JSObject({'type': 'terminated', 'reason': EndCallReason.Failed}))
                except Exception as transitionErr:
                    self.logger.trace('failed-activation transition skipped', JSObject({
                        'message': toError(transitionErr).message
                    }))
                self.emit('call_ended', info)
                self.emitState(info)
                session.cleanup()
                self.calls.pop(callId, None)
                await self.maybeUnblockWaitingCalls()
                return
        else:
            self.logger.debug('incoming call waiting, at capacity', JSObject({
                'callId': callId,
                'peerJid': peerJid,
                'maxConcurrentCalls': self.maxConcurrentCalls
            }))
        self.emit('call_incoming', info)
        self.emitState(info)
        self.logger.debug('incoming call', JSObject({
            'callId': callId,
            'peerJid': peerJid,
            'callCreator': callCreator,
            'isVideo': isVideo,
            'relayCount': len(relays),
            'acceptBlocked': atCapacity
        }))

    async def handleCallAccept(self, node, peerJid):
        session = self.resolveSessionFromNode(node)
        if session is None:
            return
        await session.handleCallAccept(node, peerJid)

    async def handleCallPreaccept(self, node, peerJid):
        session = self.resolveSessionFromNode(node)
        if session is None:
            return
        await session.handleCallPreaccept(node, peerJid)

    async def handleCallTransport(self, node, peerJid):
        session = self.resolveSessionFromNode(node)
        if session is None:
            return
        await session.handleCallTransport(node)

    async def handleCallAck(self, node):
        session = self.resolveSessionForOfferAck(node)
        if session is None:
            return
        await session.handleCallAck(node)

    async def handleCallRelaylatency(self, node, peerJid):
        session = self.resolveSessionFromNode(node)
        if session is None:
            return
        await session.handleCallRelaylatency(node, peerJid)

    def handleRelayElection(self, node):
        session = self.resolveSessionFromNode(node)
        if session is None:
            return
        session.handleRelayElection(node)

    async def handleCallMuteV2(self, node, peerJid):
        session = self.resolveSessionFromNode(node)
        if session is None:
            return
        await session.handleCallMuteV2(node, peerJid)

    def handleCallUserAction(self, node, peerJid):
        session = self.resolveSessionFromNode(node)
        if session is None:
            return
        session.handleCallUserAction(node, peerJid)

    def handleCallRaiseHand(self, node, peerJid):
        session = self.resolveSessionFromNode(node)
        if session is None:
            return
        session.handleCallRaiseHand(node, peerJid)

    def handleCallScreenShare(self, node):
        session = self.resolveSessionFromNode(node)
        if session is None:
            return
        session.handleCallScreenShare(node)

    async def handleCallVideo(self, node):
        session = self.resolveSessionFromNode(node)
        if session is None:
            return
        await session.handleCallVideo(node)

    async def handleCallTerminate(self, node):
        session = self.resolveSessionFromNode(node)
        if session is None:
            return
        session.handleCallTerminate()
        self.calls.pop(session.callId, None)
        await self.maybeUnblockWaitingCalls()

    def destroy(self):
        for session in self.calls.values():
            session.cleanup()
        self.calls.clear()
        self.removeAllListeners()

    @property
    def activeCallCount(self):
        count = 0
        for session in self.calls.values():
            if not session.info.isEnded and not session.info.isAcceptBlocked:
                count += 1
        return count

    def createSession(self, info, options=None):
        if options is None:
            options = JSObject({})
        prior = self.calls.get(info.callId)
        if prior is not None:
            if not prior.info.isEnded:
                raise JSError(f'call {info.callId} already exists')
            prior.cleanup()
            self.calls.pop(info.callId, None)
        acceptBlocked = orDefault(options.get('acceptBlocked'), False)
        if not acceptBlocked and self.activeCallCount >= self.maxConcurrentCalls:
            raise JSError(f'max concurrent calls reached ({self.maxConcurrentCalls})')
        if acceptBlocked:
            info.stateData['acceptBlocked'] = True
        sessionLogger = self.logger.child(JSObject({'callId': info.callId}))
        delegate = JSObject({
            'emitState': lambda call: self.emitState(call),
            'emitIncoming': lambda call: self.emit('call_incoming', call),
            'emitEnded': lambda call: self.emit('call_ended', call),
            'emitInboundAudio': lambda call, pcm: self.emit('call_inbound_audio', call, pcm),
            'emitPeerMute': lambda call, muted: self.emit('call_peer_mute', call, muted),
            'emitHandRaise': lambda call, participantJid, raised: self.emit('call_hand_raise', call, participantJid, raised),
            'emitCallReaction': lambda call, reaction: self.emit('call_reaction', call, reaction),
            'emitScreenShare': lambda call, share: self.emit('call_screen_share', call, share),
            'emitOutboundAudioFinished': lambda call: self.emit('call_outbound_audio_finished', call),
            'emitVideoState': lambda call, videoState: self.emit('call_video_state', call, videoState),
            'emitInboundVideo': lambda call, au: self.emit('call_inbound_video', call, au)
        })
        session = WaCallMediaSession(JSObject({
            'deps': self.deps,
            'logger': sessionLogger,
            'info': info,
            'videoConfig': options.get('videoConfig'),
            'delegate': delegate
        }))
        self.calls[info.callId] = session
        return session

    def getSessionOrThrow(self, callId):
        session = self.calls.get(callId)
        if session is None:
            raise JSError(f'No call with id {callId}')
        return session

    def resolveSessionFromNode(self, node):
        nodeInfo = extractNodeInfo(node)
        if not optGet(nodeInfo, 'callId'):
            self.logger.debug('stanza missing call-id, ignored')
            return None
        session = self.calls.get(nodeInfo.get('callId'))
        if session is None:
            self.logger.debug('no session for call-id', JSObject({'callId': nodeInfo.get('callId')}))
            return None
        return session

    def resolveSessionForOfferAck(self, node):
        callId = optGet(node.get('attrs'), 'call-id')
        if callId:
            session = self.calls.get(callId)
            if session is not None:
                return session
        outgoing = []
        for session in self.calls.values():
            if session.info.isInitiator and not session.info.isEnded:
                state = session.info.stateData.get('state')
                if state == CallState.Initiating or state == CallState.Ringing:
                    outgoing.append(session)
        if len(outgoing) == 1:
            return outgoing[0]
        self.logger.trace('offer ack could not be routed', JSObject({
            'callId': orDefault(callId, None),
            'candidateCount': len(outgoing)
        }))
        return None

    def emitState(self, call):
        self.emit('call_state', call)

    async def resolvePeerLid(self, peerJid):
        if isLidJid(peerJid):
            return peerJid
        try:
            result = await self.deps.get('signalDeviceSync').queryLidsByPhoneJids([peerJid])
            mapped = result[0] if len(result) > 0 else None
            if optGet(mapped, 'lidJid'):
                return mapped.get('lidJid')
        except Exception as err:
            self.logger.trace('lid resolution failed', JSObject({'message': toError(err).message}))
        return peerJid

    async def maybeUnblockWaitingCalls(self):
        while self.activeCallCount < self.maxConcurrentCalls:
            waiting = next((
                session for session in list(self.calls.values())
                if session.info.direction == CallDirection.Incoming
                and session.info.isRinging
                and session.info.isAcceptBlocked
            ), None)
            if waiting is None:
                break
            await self.activateWaitingIncoming(waiting)

    async def activateWaitingIncoming(self, session):
        session.info.stateData['acceptBlocked'] = False
        creds = self.deps.get('authClient').getCurrentCredentials()
        selfLid = optGet(creds, 'meLid') or optGet(creds, 'meJid') or ''
        await session.initMedia(selfLid, session.info.peerJid)
        await session.sendIncomingPreaccept(session.info.peerJid)
        await session.sendIncomingRelayLatency()
        self.emitState(session.info)
        self.logger.debug('waiting incoming call unblocked', JSObject({'callId': session.callId}))
