import asyncio
import json
import math
import re
from array import array

from baileys.foundation.node_runtime import JSError, JSObject, clearInterval, now, setInterval, setTimeout
from baileys.voip.app_data.WaAppDataStream import WaAppDataStream
from baileys.voip.bytes import EMPTY_BYTES, concatBytes, readUInt32BE, toArrayBuffer
from baileys.voip.crypto.encryption import derivePerJidSrtpKey
from baileys.voip.crypto.rtcp import SrtcpSendContext
from baileys.voip.crypto.srtp import SrtpSession
from baileys.voip.crypto.ssrc import deriveWasmRelayStreamSsrcs, generateSecureSsrc
from baileys.voip.media.h264 import H264AccessUnitAssembler, auHasIDR, buildAccessUnitPayload, packageH264NALU
from baileys.voip.media.mlow_codec import MLowCodec
from baileys.voip.media.rtcp import buildSenderReportWithSdes, generateWhatsappRtcpCname
from baileys.voip.media.rtp import RtpPacket, RtpSession, VideoMediaFrameInfo, VideoRtpStream, videoRtpDurationSamples
from baileys.voip.media.WaAudioEngine import WaAudioEngine
from baileys.voip.media.WaVideoEngine import WaVideoEngine
from baileys.voip.relay.relay_ack import parseRelayFromAck
from baileys.voip.relay.stun import isRtpPacket, isStunPacket
from baileys.voip.relay.WaManualRelay import WaManualRelay
from baileys.voip.shim.protocol import toUserJid
from baileys.voip.shim.transport import getFirstNodeChild, getNodeChildrenByTag
from baileys.voip.shim.util import toError, uint8TimingSafeEqual
from baileys.voip.signaling.screen_share import WA_SCREEN_SHARE_STATE, buildScreenShareStanza, parseScreenShareNode
from baileys.voip.signaling.signaling import (
    isFiniteNumber,
    jsNumber,
    toSafeNonNegativeInt,
    buildAcceptReceiptStanza,
    buildAcceptStanza,
    buildMuteV2Stanza,
    buildPreacceptStanza,
    buildRaiseHandStanza,
    buildRejectStanza,
    buildRelaylatencyForwardStanza,
    buildRelayLatencyStanza,
    buildTerminateStanza,
    buildTransportStanza,
    buildVideoStateStanza,
    decryptCallKey,
    extractNodeInfo,
    extractRelayEndpoints,
    needsDecryption,
    parseIntJS,
    parseMuteV2,
    parseRaiseHandState,
    VideoDecAccept,
    VideoDecRequest,
    VideoState,
)
from baileys.voip.types import (
    CallDirection,
    CallMediaType,
    CallState,
    EndCallReason,
    PayloadType,
    SRTP_AUTH_TAG_LEN,
    SRTP_RECV_AUTH_TAG_LEN,
    SRTP_SEND_AUTH_TAG_LEN,
)


def optGet(o, k):
    if o is None:
        return None
    if isinstance(o, dict):
        return o.get(k)
    try:
        return o[k]
    except Exception:
        return getattr(o, k, None)


def optAttr(o, k):
    return None if o is None else getattr(o, k, None)


def orDefault(value, fallback):
    return fallback if value is None else value


def callOpt(obj, name, *args):
    fn = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    if fn is not None:
        return fn(*args)
    return None


def jsRound(value):
    if not isFiniteNumber(value):
        return value
    return int(math.floor(value + 0.5))


class WaCallMediaSession:
    EMPTY_BYTES = EMPTY_BYTES

    def __init__(self, options):
        self.info = None
        self.deps = None
        self.logger = None
        self.delegate = None
        self.rtpSession = None
        self.srtpSession = None
        self.opusCodec = None
        self.sctpRelay = None
        self.audioEngine = None
        self.initialTransportSent = False
        self.outgoingPreacceptSent = False
        self.selfSsrc = 0
        self.selfSsrcJid = ''
        self.peerSsrcs = []
        self.firstPacketSent = False
        self.acceptedByJid = None
        self.debeEnabled = True
        self.audioSendCount = 0
        self.audioDropCount = 0
        self.realAudioSendCount = 0
        self.encodeBufferA = None
        self.encodeBufferB = None
        self.encodeBuffer = None
        self.encodeBufferPos = 0
        self.authPaddingBuffer = None
        self.audioRecvCount = 0
        self.recvRealCount = 0
        self.recvDtxCount = 0
        self.srtpErrorCount = 0
        self.relayPacketCount = 0
        self.stunResponseCount = 0
        self.selfEchoCount = 0
        self.lastRecvSeq = -1
        self.recvSeqGaps = 0
        self.lastCapturedAtMs = None
        self.qLog = JSObject({'n': 0, 'tiny': 0, 'sent': 0, 'octets': 0, 'retx': 0, 'rx': 0, 'plc': 0, 'late': 0, 'fec': 0, 'errors': 0})
        self.actualPeerSsrc = None
        self.ssrcResubscribed = False
        self.videoSsrc = 0
        self.videoRtpStream = None
        self.videoSrtpSession = None
        self.videoEngine = None
        self.videoSendCount = 0
        self.videoKeyframeRequired = False
        self.videoFlushSeen = 0
        self.videoIdrSendCount = 0
        self.videoDropCount = 0
        self.audioRtcpSession = None
        self.videoRtcpSession = None
        self.audioRtcpCname = None
        self.videoRtcpCname = None
        self.audioOctetsSent = 0
        self.videoOctetsSent = 0
        self.rtcpTimer = None
        self.mediaStartedAtMs = None
        self.mediaFlowStarted = False
        self.peerVideoUpgradePending = False
        self.peerVideoActive = False
        self.peerVideoJids = set()
        self.videoAssembler = None
        self.appDataStream = None
        self.appDataSrtpSession = None
        self.peerAppDataSsrcs = set()
        self.lossWindow = None
        self.deps = options.get('deps')
        self.logger = options.get('logger')
        self.info = options.get('info')
        self.delegate = options.get('delegate')
        self.sctpRelay = WaManualRelay(JSObject({
            'logger': self.logger.child(JSObject({'component': 'sctp'}))
        }))
        self.audioEngine = WaAudioEngine(JSObject({
            'logger': self.logger.child(JSObject({'component': 'audio-engine'}))
        }))
        self.audioEngine.setAudioSender(self)
        self.audioEngine.setOnAudioFinished(lambda: self.delegate.get('emitOutboundAudioFinished')(self.info))
        self.videoEngine = WaVideoEngine(JSObject({
            'logger': self.logger.child(JSObject({'component': 'video-engine'})),
            **(orDefault(options.get('videoConfig'), JSObject({})))
        }))
        self.videoEngine.setVideoSender(self)
        self.sctpRelay.on('relay_connected', lambda *args: self.onRelayConnected())
        self.sctpRelay.on('relay_receive', lambda relayInfo: self.onRelayData(relayInfo.get('data')))

    @property
    def callId(self):
        return self.info.callId

    def onDecodedAudio(self, pcm):
        self.audioEngine.onPlaybackData(pcm)
        callOpt(self.delegate, 'emitInboundAudio', self.info, pcm)

    async def initMedia(self, selfLid, peerJid):
        ssrc = generateSecureSsrc(self.info.callId, self.ensureDeviceJid(selfLid))
        self.rtpSession = RtpSession.whatsappOpus(ssrc)
        self.selfSsrc = ssrc
        self.selfSsrcJid = self.ensureDeviceJid(selfLid)
        peerSsrc = generateSecureSsrc(self.info.callId, self.ensureDeviceJid(peerJid))
        self.peerSsrcs = [peerSsrc]
        self.logger.media('call media initialized', JSObject({
            'callId': self.info.callId,
            'selfSsrc': f'0x{ssrc:X}',
            'peerSsrc': f'0x{peerSsrc:X}'
        }))
        self.logger.warn('[DIAG] initMedia called', JSObject({
            'callId': self.info.callId,
            'selfSsrc': f'0x{ssrc:X}',
            'peerSsrc': f'0x{peerSsrc:X}',
            'videoEngineIsFresh': self.videoEngine is not None and self.videoEngine.proc is None
        }))
        self.opusCodec = await MLowCodec.create(JSObject({'logger': self.logger.child(JSObject({'component': 'mlow'}))}))
        if self.info.mediaType == CallMediaType.Video:
            self.videoSsrc = generateSecureSsrc(self.info.callId, self.ensureDeviceJid(selfLid), 2)
            self.videoRtpStream = VideoRtpStream(self.videoSsrc, videoRtpDurationSamples(self.videoEngine.frameDurationMs))
            self.videoAssembler = H264AccessUnitAssembler()
            self.logger.media('video media initialized', JSObject({
                'callId': self.info.callId,
                'videoSsrc': f'0x{self.videoSsrc:X}'
            }))
            self.logger.warn('[DIAG] video media initialized', JSObject({
                'callId': self.info.callId,
                'videoSsrc': f'0x{self.videoSsrc:X}'
            }))

    def resetOutgoingFlags(self):
        self.initialTransportSent = False
        self.outgoingPreacceptSent = False

    async def acceptCall(self):
        if not self.info.canAccept:
            raise JSError(f"Call {self.info.callId} cannot be accepted in state {self.info.stateData.get('state')}")
        self.info.applyTransition(JSObject({'type': 'local_accepted'}))
        self.delegate.get('emitState')(self.info)
        meId = orDefault(optGet(self.deps.get('authClient').getCurrentCredentials(), 'meJid'), '')
        callId = self.info.callId
        callCreator = self.info.callCreator
        peerJid = self.info.peerJid
        isVideo = self.info.mediaType == CallMediaType.Video
        self.acceptedByJid = peerJid
        self.initSrtpKeys()
        try:
            muteNode = buildMuteV2Stanza(peerJid, callId, callCreator, 0, meId)
            await self.deps.get('lowLevelCoordinator').sendNode(muteNode)
        except Exception as err:
            self.logger.error('error sending mute_v2', JSObject({
                'message': toError(err).message
            }))
        try:
            transportNode = buildTransportStanza(peerJid, callId, callCreator, meId, '1', '1')
            await self.deps.get('lowLevelCoordinator').sendNode(transportNode)
        except Exception as err:
            self.logger.error('error sending transport', JSObject({
                'message': toError(err).message
            }))
        if self.info.encryptionKey:
            acceptStanza = await buildAcceptStanza(self.deps, self.info.callId, self.info.encryptionKey, self.info.peerJid, self.info.callCreator, isVideo)
            try:
                await self.deps.get('lowLevelCoordinator').sendNode(acceptStanza)
            except Exception as err:
                self.logger.error('accept send error', JSObject({
                    'message': toError(err).message
                }))
        if self.info.relayData:
            await self.connectRelays(self.info.relayData.get('endpoints'))
        self.logger.media('call accepted', JSObject({'callId': callId}))

    async def rejectCall(self, reason=EndCallReason.Declined):
        self.info.applyTransition(JSObject({'type': 'local_rejected', 'reason': reason}))
        self.delegate.get('emitState')(self.info)
        node = buildRejectStanza(self.info.peerJid, self.info.callId, self.info.callCreator)
        try:
            await self.deps.get('lowLevelCoordinator').sendNode(node)
        except Exception as err:
            self.logger.warn('reject send failed', JSObject({'message': toError(err).message}))
        self.cleanup()

    async def endCall(self, reason=EndCallReason.UserEnded):
        if self.info.isEnded:
            return
        connectedAt = self.info.stateData.get('connectedAt')
        audioDurationMs = (now() - connectedAt) if connectedAt else None
        self.info.applyTransition(JSObject({'type': 'terminated', 'reason': reason}))
        terminateTarget = orDefault(self.acceptedByJid, self.info.peerJid)
        node = buildTerminateStanza(terminateTarget, self.info.callId, self.info.callCreator, audioDurationMs, None)
        self.delegate.get('emitEnded')(self.info)
        self.delegate.get('emitState')(self.info)
        try:
            await self.deps.get('lowLevelCoordinator').sendNode(node)
        except Exception as err:
            self.logger.warn('terminate send failed', JSObject({'message': toError(err).message}))
        await asyncio.sleep(0.4)
        self.cleanup()

    def setMute(self, muted):
        if not self.info.isActive:
            return
        self.info.applyTransition(JSObject({'type': 'audio_mute_changed', 'muted': muted}))
        self.delegate.get('emitState')(self.info)
        if muted:
            self.audioEngine.stopCapture()
        else:
            self.audioEngine.startCapture()

    async def loadAudio(self, audioPath):
        self.audioEngine.setLoopMode(False)
        await self.audioEngine.loadAudioFile(audioPath)
        self.resetEncodeState()
        self.logger.media('audio loaded for call', JSObject({'callId': self.info.callId}))

    def setExternalAudioMode(self, enabled):
        self.audioEngine.setExternalMode(enabled)
        if enabled:
            self.resetEncodeState()
            self.logger.debug('external audio mode enabled', JSObject({'callId': self.info.callId}))

    def feedLiveAudio(self, data):
        return self.audioEngine.feedExternalAudio(data)

    def getLiveBufferMs(self):
        return self.audioEngine.getLiveBufferMs()

    async def sendIncomingPreaccept(self, peerJid):
        try:
            preacceptNode = buildPreacceptStanza(peerJid, self.info.callId, self.info.callCreator)
            await self.deps.get('lowLevelCoordinator').sendNode(preacceptNode)
        except Exception as err:
            self.logger.error('error sending preaccept', JSObject({
                'message': toError(err).message
            }))

    async def sendIncomingRelayLatency(self):
        if not self.info.relayData:
            return
        meId = orDefault(optGet(self.deps.get('authClient').getCurrentCredentials(), 'meJid'), '')
        callId = self.info.callId
        callCreator = self.info.callCreator
        destinationJids = orDefault(self.info.relayData.get('participantJids'), [])
        seenRelayNames = set()
        for ep in self.info.relayData.get('endpoints'):
            name = orDefault(ep.get('relayName'), '')
            if not name or name in seenRelayNames:
                continue
            seenRelayNames.add(name)
            try:
                relayData = [
                    JSObject({
                        'relayName': name,
                        'latency': ep.get('c2rRtt') or 0,
                        'addressBytes': ep.get('addressBytes')
                    })
                ]
                relayLatencyNode = buildRelayLatencyStanza(self.info.peerJid, callId, callCreator, relayData, destinationJids, meId)
                await self.deps.get('lowLevelCoordinator').sendNode(relayLatencyNode)
            except Exception as err:
                self.logger.error('error sending incoming relaylatency', JSObject({
                    'relayName': name,
                    'message': toError(err).message
                }))

    async def handleCallAccept(self, node, peerJid):
        nodeInfo = extractNodeInfo(node)
        if not nodeInfo:
            return
        srtpFromPeerKey = False
        if needsDecryption(nodeInfo.get('tag')):
            try:
                peerCallKey = await decryptCallKey(self.deps, nodeInfo.get('innerNode'), peerJid, self.logger.child(JSObject({'component': 'signaling'})))
                if peerCallKey:
                    ourCallKey = self.info.encryptionKey
                    keysMatch = uint8TimingSafeEqual(ourCallKey, peerCallKey) if ourCallKey else False
                    if not keysMatch and ourCallKey:
                        meLid = optGet(self.deps.get('authClient').getCurrentCredentials(), 'meLid')
                        meJid = optGet(self.deps.get('authClient').getCurrentCredentials(), 'meJid')
                        ourCredJid = meLid or meJid or ''
                        ourBase = toUserJid(ourCredJid) if ourCredJid else ''
                        participants = orDefault(optGet(self.info.relayData, 'participantJids'), [])
                        ourDeviceJid = next(
                            (jid for jid in participants if toUserJid(jid) == ourBase and re.search(r':\d+@', jid)),
                            None
                        ) or ourCredJid
                        if ourDeviceJid and peerJid:
                            try:
                                sendKeying = derivePerJidSrtpKey(ourCallKey, self.ensureDeviceJid(ourDeviceJid))
                                recvKeying = derivePerJidSrtpKey(peerCallKey, self.ensureDeviceJid(peerJid))
                                self.srtpSession = SrtpSession(sendKeying, recvKeying, SRTP_SEND_AUTH_TAG_LEN, SRTP_RECV_AUTH_TAG_LEN)
                                srtpFromPeerKey = True
                                self.logger.debug('srtp re-initialized with peer call_key', JSObject({
                                    'callId': self.info.callId
                                }))
                            except Exception as err:
                                self.logger.error('per-jid srtp re-derivation failed', JSObject({
                                    'message': toError(err).message
                                }))
            except Exception as err:
                self.logger.error('accept decrypt error', JSObject({
                    'message': toError(err).message
                }))
        try:
            self.info.applyTransition(JSObject({'type': 'remote_accepted'}))
            self.delegate.get('emitState')(self.info)
        except Exception as err:
            self.logger.trace('call transition skipped', JSObject({'message': toError(err).message}))
        meId = orDefault(optGet(self.deps.get('authClient').getCurrentCredentials(), 'meJid'), '')
        meLid = optGet(self.deps.get('authClient').getCurrentCredentials(), 'meLid')
        ourJid = meLid or meId
        ourBase = toUserJid(ourJid) if ourJid else ''
        callId = self.info.callId
        callCreator = self.info.callCreator
        acceptingDeviceJid = peerJid
        self.acceptedByJid = acceptingDeviceJid
        if self.actualPeerSsrc is not None:
            calculatedJid = self.ensureDeviceJid(acceptingDeviceJid)
            self.logger.debug('accept keeping actual peer ssrc', JSObject({
                'callId': callId,
                'actualPeerSsrc': f'0x{self.actualPeerSsrc:x}',
                'calculatedJid': calculatedJid
            }))
        else:
            peerDeviceJidForSsrc = self.ensureDeviceJid(acceptingDeviceJid)
            acceptSsrc = generateSecureSsrc(callId, peerDeviceJidForSsrc)
            self.peerSsrcs = [acceptSsrc]
            self.logger.debug('accept ssrc assigned', JSObject({
                'callId': callId,
                'jid': peerDeviceJidForSsrc,
                'ssrc': f'0x{acceptSsrc:x}'
            }))
        self.sctpRelay.setSubscriptionSsrc(orDefault(self.peerSsrcs[0] if len(self.peerSsrcs) > 0 else None, 0))
        self.sctpRelay.resendSubscriptions()
        if not srtpFromPeerKey:
            self.initSrtpKeys()
        if optGet(self.info.relayData, 'participantJids'):
            otherDevices = []
            for jid in self.info.relayData.get('participantJids'):
                if jid == acceptingDeviceJid:
                    continue
                jidBase = toUserJid(jid)
                if jidBase == ourBase:
                    continue
                otherDevices.append(jid)
            for deviceJid in otherDevices:
                try:
                    terminateNode = buildTerminateStanza(deviceJid, callId, callCreator, None, 'accepted_elsewhere')
                    await self.deps.get('lowLevelCoordinator').sendNode(terminateNode)
                except Exception as err:
                    self.logger.error('error sending terminate_elsewhere', JSObject({
                        'deviceJid': deviceJid,
                        'message': toError(err).message
                    }))
        try:
            transportNode = buildTransportStanza(acceptingDeviceJid, callId, callCreator, meId, '1', '1')
            await self.deps.get('lowLevelCoordinator').sendNode(transportNode)
        except Exception as err:
            self.logger.error('error sending transport', JSObject({
                'message': toError(err).message
            }))
        try:
            muteNode = buildMuteV2Stanza(acceptingDeviceJid, callId, callCreator, 0, meId)
            await self.deps.get('lowLevelCoordinator').sendNode(muteNode)
        except Exception as err:
            self.logger.error('error sending mute_v2', JSObject({
                'message': toError(err).message
            }))
        acceptMsgId = optGet(node.get('attrs'), 'id')
        if acceptMsgId:
            try:
                receiptNode = buildAcceptReceiptStanza(acceptingDeviceJid, acceptMsgId, callId, callCreator, ourJid)
                await self.deps.get('lowLevelCoordinator').sendNode(receiptNode)
            except Exception as err:
                self.logger.error('error sending accept receipt', JSObject({
                    'message': toError(err).message
                }))
        if self.sctpRelay.hasConnection():
            try:
                self.info.applyTransition(JSObject({'type': 'media_connected'}))
                self.delegate.get('emitState')(self.info)
                MEDIA_START_DELAY_MS = 3000

                def delayedStart():
                    self.logger.warn('[DIAG] delayed startMediaFlow firing (path B)', JSObject({
                        'callId': self.info.callId, 'state': self.info.stateData.get('state')
                    }))
                    if self.info.stateData.get('state') == CallState.Ended:
                        return
                    self.startMediaFlow()
                setTimeout(delayedStart, MEDIA_START_DELAY_MS)
            except Exception as err:
                self.logger.trace('call transition skipped', JSObject({'message': toError(err).message}))
        elif self.info.relayData:
            await self.connectRelays(self.info.relayData.get('endpoints'))

    async def handleCallPreaccept(self, node, peerJid):
        nodeInfo = extractNodeInfo(node)
        if not nodeInfo:
            return
        if self.info.direction == CallDirection.Outgoing and self.info.relayData:
            meId = orDefault(optGet(self.deps.get('authClient').getCurrentCredentials(), 'meJid'), '')
            callId = self.info.callId
            callCreator = self.info.callCreator
            destinationJids = orDefault(self.info.relayData.get('participantJids'), [])
            seenRelayNames = set()
            for ep in self.info.relayData.get('endpoints'):
                name = orDefault(ep.get('relayName'), '')
                if not name or name in seenRelayNames:
                    continue
                seenRelayNames.add(name)
                try:
                    relayData = [
                        JSObject({
                            'relayName': name,
                            'latency': ep.get('c2rRtt') or 0,
                            'addressBytes': ep.get('addressBytes')
                        })
                    ]
                    relayLatencyNode = buildRelayLatencyStanza(self.info.peerJid, callId, callCreator, relayData, destinationJids, meId)
                    await self.deps.get('lowLevelCoordinator').sendNode(relayLatencyNode)
                except Exception as err:
                    self.logger.error('error sending relaylatency', JSObject({
                        'relayName': name,
                        'message': toError(err).message
                    }))
            if not self.initialTransportSent:
                try:
                    basePeerJid = toUserJid(peerJid)
                    transportNode = buildTransportStanza(basePeerJid, callId, callCreator, meId)
                    await self.deps.get('lowLevelCoordinator').sendNode(transportNode)
                    self.initialTransportSent = True
                except Exception as err:
                    self.logger.error('error sending initial transport', JSObject({
                        'message': toError(err).message
                    }))

    async def handleCallTransport(self, _node):
        nodeInfo = extractNodeInfo(_node)
        if not nodeInfo:
            return
        relays = extractRelayEndpoints(nodeInfo.get('innerNode'))
        if len(relays) > 0 and not self.sctpRelay.hasConnection():
            self.info.relayData = JSObject({**(orDefault(self.info.relayData, JSObject({}))), 'endpoints': relays})
            await self.connectRelays(relays)

    async def handleCallAck(self, node):
        ackType = optGet(node.get('attrs'), 'type')
        if ackType != 'offer':
            return
        error = optGet(node.get('attrs'), 'error')
        if error:
            self.logger.error('ack error', JSObject({'callId': self.info.callId, 'error': error}))
            return
        ack = parseRelayFromAck(node)
        relays = ack.get('relays')
        participantJids = ack.get('participantJids')
        if len(relays) > 0:
            self.info.relayData = JSObject({
                'endpoints': relays,
                'participantJids': participantJids,
                'uuid': ack.get('uuid'),
                'selfPid': ack.get('selfPid'),
                'peerPid': ack.get('peerPid'),
                'hbhKey': ack.get('hbhKey')
            })
            self.logger.debug('offer ack relays parsed', JSObject({
                'callId': self.info.callId,
                'relayCount': len(relays),
                'participantCount': len(participantJids)
            }))
            callKey = self.info.encryptionKey
            if len(participantJids) > 0:
                meLid = optGet(self.deps.get('authClient').getCurrentCredentials(), 'meLid')
                meId = optGet(self.deps.get('authClient').getCurrentCredentials(), 'meJid')
                ourCredJid = meLid or meId or ''
                ourBase = toUserJid(ourCredJid) if ourCredJid else ''
                ourDeviceJid = self.ensureDeviceJid(next(
                    (jid for jid in participantJids if toUserJid(jid) == ourBase and re.search(r':\d+@', jid)),
                    None
                ) or ourCredJid)
                peerJids = [jid for jid in participantJids if toUserJid(jid) != ourBase]
                peerCandidate = next(
                    (jid for jid in peerJids if re.search(r':\d+@', jid) and not re.search(r':0@', jid)),
                    None
                ) or (peerJids[0] if len(peerJids) > 0 else None)
                peerDeviceJid = self.ensureDeviceJid(peerCandidate) if peerCandidate else None
                newSelfSsrc = generateSecureSsrc(self.info.callId, ourDeviceJid)
                if newSelfSsrc != self.selfSsrc:
                    self.selfSsrc = newSelfSsrc
                    self.selfSsrcJid = ourDeviceJid
                    self.rtpSession = RtpSession.whatsappOpus(newSelfSsrc)
                if peerDeviceJid:
                    peerDeviceSsrc = generateSecureSsrc(self.info.callId, peerDeviceJid)
                    self.peerSsrcs = [peerDeviceSsrc]
                if callKey:
                    self.initSrtpKeys()
                else:
                    self.logger.media('no call_key, srtp not initialized', JSObject({
                        'callId': self.info.callId
                    }))
            if self.info.isInitiator and not self.outgoingPreacceptSent:
                try:
                    preacceptNode = buildPreacceptStanza(self.info.peerJid, self.info.callId, self.info.callCreator)
                    await self.deps.get('lowLevelCoordinator').sendNode(preacceptNode)
                    self.outgoingPreacceptSent = True
                except Exception as err:
                    self.logger.error('error sending preaccept (caller)', JSObject({
                        'message': toError(err).message
                    }))
            await self.connectRelays(relays)
            if (self.srtpSession is not None and self.rtpSession is not None and self.opusCodec is not None
                    and self.sctpRelay.hasConnection()):
                self.audioEngine.startSilenceCapture()

    async def handleCallRelaylatency(self, node, peerJid):
        nodeInfo = extractNodeInfo(node)
        if not nodeInfo:
            return
        inner = nodeInfo.get('innerNode')
        callId = optGet(inner.get('attrs'), 'call-id') or self.info.callId
        callCreator = optGet(inner.get('attrs'), 'call-creator') or self.info.callCreator
        teNodes = getNodeChildrenByTag(inner, 'te')
        if len(teNodes) == 0:
            return
        destinationJids = orDefault(optGet(self.info.relayData, 'participantJids'), [])
        if len(destinationJids) > 0:
            forwardNode = buildRelaylatencyForwardStanza(peerJid, callId, callCreator, teNodes, destinationJids)
            try:
                await self.deps.get('lowLevelCoordinator').sendNode(forwardNode)
            except Exception as err:
                self.logger.error('error forwarding relaylatency', JSObject({
                    'message': toError(err).message
                }))

    def handleRelayElection(self, node):
        inner = getFirstNodeChild(node)
        if not inner:
            return
        electedRelayIdx = None
        if optGet(inner.get('attrs'), 'elected_relay_idx') is not None:
            parsed = toSafeNonNegativeInt(inner.get('attrs').get('elected_relay_idx'))
            if parsed is not None:
                electedRelayIdx = parsed
        elif optGet(inner.get('attrs'), 'relay_id') is not None:
            parsed = toSafeNonNegativeInt(inner.get('attrs').get('relay_id'))
            if parsed is not None:
                electedRelayIdx = parsed
        elif isinstance(inner.get('content'), (bytes, bytearray)):
            data = inner.get('content')
            if len(data) >= 4:
                electedRelayIdx = readUInt32BE(data, 0)
            elif len(data) > 0:
                electedRelayIdx = data[0]
        if electedRelayIdx is not None:
            self.info.electedRelayIdx = electedRelayIdx
            self.logger.debug('elected relay index', JSObject({
                'callId': self.info.callId,
                'electedRelayIdx': electedRelayIdx
            }))

    async def handleCallMuteV2(self, node, peerJid):
        nodeInfo = extractNodeInfo(node)
        if not nodeInfo:
            return
        meId = orDefault(optGet(self.deps.get('authClient').getCurrentCredentials(), 'meJid'), '')
        callId = self.info.callId
        callCreator = self.info.callCreator
        if not self.isOwnAccountJid(peerJid):
            payload = parseMuteV2(nodeInfo.get('innerNode'))
            if not payload.get('isRequest') and payload.get('muted') is not None and self.info.stateData.get('peerAudioMuted') != payload.get('muted'):
                self.info.stateData['peerAudioMuted'] = payload.get('muted')
                callOpt(self.delegate, 'emitPeerMute', self.info, payload.get('muted'))
        try:
            muteNode = buildMuteV2Stanza(peerJid, callId, callCreator, 0, meId)
            await self.deps.get('lowLevelCoordinator').sendNode(muteNode)
        except Exception as err:
            self.logger.error('error sending mute_v2 response', JSObject({
                'message': toError(err).message
            }))

    def isOwnAccountJid(self, jid):
        creds = self.deps.get('authClient').getCurrentCredentials()
        user = toUserJid(jid)
        return ((bool(optGet(creds, 'meLid')) and user == toUserJid(creds.get('meLid'))) or
                (bool(optGet(creds, 'meJid')) and user == toUserJid(creds.get('meJid'))))

    @property
    def videoSendActive(self):
        return bool(self.videoRtpStream is not None and self.videoSrtpSession is not None)

    async def setHandRaised(self, raised):
        if not self.info.isActive:
            return
        if self.info.stateData.get('handRaised') == raised:
            return
        node = buildRaiseHandStanza(orDefault(self.acceptedByJid, self.info.peerJid), self.info.callId, self.info.callCreator, raised)
        try:
            await self.deps.get('lowLevelCoordinator').sendNode(node)
        except Exception as err:
            self.logger.warn('raise hand send failed', JSObject({
                'raised': raised,
                'message': toError(err).message
            }))
            raise
        self.info.applyTransition(JSObject({'type': 'hand_raise_changed', 'raised': raised}))
        self.delegate.get('emitState')(self.info)

    async def setScreenShare(self, sharing):
        if not self.info.isActive:
            return
        if self.info.stateData.get('screenSharing') == sharing:
            return
        if sharing and not self.videoSendActive:
            raise JSError(f"Call {self.info.callId} carries no video to share the screen on")
        peerDeviceJid = orDefault(self.acceptedByJid, self.info.peerJid)
        state = WA_SCREEN_SHARE_STATE['Started'] if sharing else WA_SCREEN_SHARE_STATE['Stopped']
        try:
            await self.deps.get('lowLevelCoordinator').sendNode(buildScreenShareStanza(peerDeviceJid, self.info.callId, self.info.callCreator, state))
        except Exception as err:
            self.logger.warn('screen share request failed', JSObject({
                'sharing': sharing,
                'message': toError(err).message
            }))
            raise
        self.info.applyTransition(JSObject({'type': 'screen_share_changed', 'sharing': sharing}))
        self.delegate.get('emitState')(self.info)

    def handleCallUserAction(self, node, peerJid):
        self.applyPeerRaiseHand(node, peerJid)

    def handleCallRaiseHand(self, node, peerJid):
        self.applyPeerRaiseHand(node, peerJid)

    def applyPeerRaiseHand(self, node, peerJid):
        nodeInfo = extractNodeInfo(node)
        if not nodeInfo:
            return
        if self.isOwnAccountJid(peerJid):
            return
        raised = parseRaiseHandState(nodeInfo.get('innerNode'))
        if raised is None:
            return
        raisedHands = self.info.raisedHands
        if (peerJid in raisedHands) == raised:
            return
        if raised:
            if len(raisedHands) >= 64:
                return
            raisedHands.add(peerJid)
        else:
            raisedHands.discard(peerJid)
        callOpt(self.delegate, 'emitHandRaise', self.info, peerJid, raised)

    def handleCallScreenShare(self, node):
        nodeInfo = extractNodeInfo(node)
        if not nodeInfo:
            return
        share = parseScreenShareNode(nodeInfo.get('innerNode'))
        if not share:
            return
        self.info.peerScreenShare = share
        callOpt(self.delegate, 'emitScreenShare', self.info, share)
        self.delegate.get('emitState')(self.info)

    def sendReaction(self, reaction):
        if not self.appDataStream or not self.appDataSrtpSession:
            return False
        if not self.info.isActive:
            return False
        return self.appDataStream.sendReaction(reaction)

    def openAppDataStream(self, selfDeviceJid):
        ssrc = generateSecureSsrc(self.info.callId, selfDeviceJid, 6)
        if self.appDataStream is not None:
            self.appDataStream.close()

        def sendPacket(packet):
            if not self.appDataSrtpSession:
                return False
            return self.sctpRelay.broadcast(toArrayBuffer(self.appDataSrtpSession.protect(packet)))
        self.appDataStream = WaAppDataStream(JSObject({
            'logger': self.logger.child(JSObject({'component': 'app-data'})),
            'ssrc': ssrc,
            'sendPacket': sendPacket
        }))

    def trackPeerAppDataSsrcs(self, deviceJids):
        for jid in deviceJids:
            if not jid:
                continue
            if len(self.peerAppDataSsrcs) >= 16:
                break
            self.peerAppDataSsrcs.add(generateSecureSsrc(self.info.callId, self.ensureDeviceJid(jid), 6))

    def onAppDataPacket(self, data, payloadType, ssrc):
        stream = self.appDataStream
        if not stream or not self.appDataSrtpSession:
            return
        try:
            packet = self.appDataSrtpSession.unprotect(data)
            stream.observeInboundPayloadType(payloadType)
            for reaction in stream.receive(packet.payload, ssrc):
                callOpt(self.delegate, 'emitCallReaction', self.info, reaction)
        except Exception as err:
            self.logger.debug('app data packet dropped', JSObject({
                'callId': self.info.callId,
                'payloadType': payloadType,
                'message': toError(err).message
            }))

    async def handleCallVideo(self, node):
        nodeInfo = extractNodeInfo(node)
        if not nodeInfo:
            return
        inner = nodeInfo.get('innerNode')
        rawState = optGet(inner.get('attrs'), 'state')
        state = jsNumber(rawState) if rawState is not None else float('nan')
        if not isFiniteNumber(state):
            return
        peerJid = self.acceptedByJid or self.info.peerJid
        callId = self.info.callId
        callCreator = self.info.callCreator
        sourceJid = nodeInfo.get('peerJid') or peerJid

        def setPeerVideo(on):
            before = len(self.peerVideoJids)
            if on:
                self.peerVideoJids.add(sourceJid)
            else:
                self.peerVideoJids.discard(sourceJid)
            self.peerVideoActive = len(self.peerVideoJids) > 0
            return (before, len(self.peerVideoJids))

        self.logger.media('inbound video state', JSObject({'callId': callId, 'state': state}))
        if state == VideoState['UpgradeRequest'] or state == VideoState['UpgradeRequestV2']:
            setPeerVideo(True)
            self.peerVideoUpgradePending = True
            await self.enableVideoMidCall()
            try:
                accept = buildVideoStateStanza(peerJid, callId, callCreator, VideoState['UpgradeAccept'], JSObject({'dec': VideoDecAccept}))
                await self.deps.get('lowLevelCoordinator').sendNode(accept)
            except Exception as err:
                self.logger.error('error sending video upgrade accept', JSObject({'callId': callId, 'message': toError(err).message}))
            self.peerVideoUpgradePending = False
            self.info.mediaType = CallMediaType.Video
            self.info.stateData['videoOff'] = False
            self.delegate.get('emitState')(self.info)
            callOpt(self.delegate, 'emitVideoState', self.info, JSObject({'active': True, 'upgrade': True, 'raw': state, 'jid': sourceJid}))
        elif state == VideoState['Disabled'] or state == VideoState['Stopped']:
            before, after = setPeerVideo(False)
            if not (state == VideoState['Disabled'] and before == after):
                callOpt(self.delegate, 'emitVideoState', self.info, JSObject({
                    'active': False,
                    'upgrade': False,
                    'raw': state,
                    'direction': 'inbound',
                    'jid': sourceJid,
                    'allOff': before > 0 and after == 0
                }))
        elif state == VideoState['UpgradeReject'] or state == VideoState['UpgradeCancel']:
            self.disableVideoMidCall(JSObject({'keepSource': True}))
            callOpt(self.delegate, 'emitVideoState', self.info, JSObject({'active': False, 'upgrade': False, 'raw': state}))
        elif state == VideoState['Enabled']:
            setPeerVideo(True)
            callOpt(self.delegate, 'emitVideoState', self.info, JSObject({'active': True, 'upgrade': False, 'raw': state, 'direction': 'inbound', 'jid': sourceJid}))

    async def startVideoMidCall(self):
        peerJid = self.acceptedByJid or self.info.peerJid
        request = buildVideoStateStanza(peerJid, self.info.callId, self.info.callCreator, VideoState['UpgradeRequestV2'], JSObject({'dec': VideoDecRequest}))
        await self.deps.get('lowLevelCoordinator').sendNode(request)
        await self.enableVideoMidCall()
        self.info.mediaType = CallMediaType.Video
        self.info.stateData['videoOff'] = False
        self.delegate.get('emitState')(self.info)
        callOpt(self.delegate, 'emitVideoState', self.info, JSObject({'active': True, 'upgrade': True, 'raw': VideoState['UpgradeRequestV2']}))

    async def enableVideoMidCall(self):
        if self.videoRtpStream is not None and self.videoSrtpSession is not None:
            return
        creds = self.deps.get('authClient').getCurrentCredentials()
        selfLid = optGet(creds, 'meLid') or optGet(creds, 'meJid') or ''
        if not self.videoSsrc:
            self.videoSsrc = generateSecureSsrc(self.info.callId, self.ensureDeviceJid(selfLid), 2)
        self.videoRtpStream = VideoRtpStream(self.videoSsrc, videoRtpDurationSamples(self.videoEngine.frameDurationMs))
        if self.srtpSession is not None:
            meLid = optGet(creds, 'meLid')
            meId = optGet(creds, 'meJid')
            ourBase = toUserJid(meLid or meId or '')
            participants = orDefault(optGet(self.info.relayData, 'participantJids'), [])
            ourDeviceJid = self.ensureDeviceJid(next(
                (jid for jid in participants if toUserJid(jid) == ourBase and re.search(r':\d+@', jid)),
                None
            ) or meLid or meId or '')
            peerDeviceJid = self.ensureDeviceJid(self.acceptedByJid or self.info.peerJid)
            try:
                sendKeying = derivePerJidSrtpKey(self.info.encryptionKey, ourDeviceJid)
                recvKeying = derivePerJidSrtpKey(self.info.encryptionKey, peerDeviceJid)
                self.videoSrtpSession = SrtpSession(sendKeying, recvKeying, SRTP_SEND_AUTH_TAG_LEN, SRTP_RECV_AUTH_TAG_LEN)
                self.videoRtcpSession = SrtcpSendContext(sendKeying)
            except Exception as err:
                self.logger.error('video srtp key derivation failed', JSObject({'callId': self.info.callId, 'message': toError(err).message}))
        self.videoAssembler = H264AccessUnitAssembler()
        if not self.videoEngine.hasSource():
            try:
                await self.videoEngine.loadBlankSource()
            except Exception as err:
                self.logger.error('blank video fallback unavailable', JSObject({'callId': self.info.callId, 'message': toError(err).message}))
        if self.sctpRelay.hasConnection():
            self.videoEngine.start()
        self.logger.media('video enabled mid-call', JSObject({
            'callId': self.info.callId,
            'videoSsrc': f'0x{self.videoSsrc:X}',
            'source': self.videoEngine.sourceKind
        }))

    async def swapVideoSource(self, videoPath):
        if self.info.mediaType != CallMediaType.Video:
            raise JSError(f"Call {self.info.callId} has no active video stream to swap")
        self.videoEngine.stop(JSObject({'keepSource': False}))
        await self.videoEngine.loadVideoFile(videoPath)
        self.audioEngine.setLoopMode(True)
        self.videoEngine.start()
        self.logger.media('video source swapped', JSObject({'callId': self.info.callId, 'videoPath': videoPath}))

    async def stopVideoMidCall(self, options=None):
        keepSource = False if options is None else orDefault(options.get('keepSource'), False)
        peerJid = self.acceptedByJid or self.info.peerJid
        try:
            stop = buildVideoStateStanza(peerJid, self.info.callId, self.info.callCreator, VideoState['Stopped'])
            await self.deps.get('lowLevelCoordinator').sendNode(stop)
        except Exception as err:
            self.logger.error('error sending video stop', JSObject({'callId': self.info.callId, 'message': toError(err).message}))
        self.disableVideoMidCall(JSObject({'keepSource': keepSource}))
        self.info.stateData['videoOff'] = True
        self.delegate.get('emitState')(self.info)
        callOpt(self.delegate, 'emitVideoState', self.info, JSObject({'active': False, 'upgrade': False, 'raw': VideoState['Stopped']}))

    def disableVideoMidCall(self, options=None):
        keepSource = False if options is None else orDefault(options.get('keepSource'), False)
        self.videoEngine.stop(JSObject({'keepSource': keepSource}))
        self.videoRtpStream = None
        self.videoSrtpSession = None
        self.videoRtcpSession = None
        self.videoAssembler = None
        self.info.mediaType = CallMediaType.Audio
        self.logger.media('video disabled mid-call', JSObject({'callId': self.info.callId, 'keptSource': keepSource}))

    def handleCallTerminate(self):
        try:
            self.info.applyTransition(JSObject({
                'type': 'terminated',
                'reason': EndCallReason.UserEnded
            }))
        except Exception as err:
            self.logger.trace('call transition skipped', JSObject({'message': toError(err).message}))
        self.delegate.get('emitEnded')(self.info)
        self.delegate.get('emitState')(self.info)
        self.cleanup()

    def sendCapturedAudio(self, data):
        nowMs = now()
        if self.lastCapturedAtMs is not None:
            gapMs = nowMs - self.lastCapturedAtMs
            if gapMs > 300:
                self.logger.warn('captured audio frame gap larger than expected', JSObject({
                    'callId': self.info.callId,
                    'gapMs': gapMs,
                    'audioSendCount': self.audioSendCount,
                    'sctpBacklog': self.sctpRelay.getSendBacklog()
                }))
        self.lastCapturedAtMs = nowMs
        hasRelay = self.sctpRelay.hasConnection()
        if self.rtpSession is None or self.srtpSession is None or self.opusCodec is None or not hasRelay:
            self.audioDropCount += 1
            if self.audioDropCount == 1 or self.audioDropCount % 500 == 0:
                missing = ', '.join(name for name, flag in [
                    ('rtpSession', self.rtpSession is None),
                    ('srtpSession', self.srtpSession is None),
                    ('opusCodec', self.opusCodec is None),
                    ('relayConnection', not hasRelay)
                ] if flag)
                self.logger.media('audio dropped', JSObject({
                    'callId': self.info.callId,
                    'dropCount': self.audioDropCount,
                    'missing': missing
                }))
            return
        for i in range(len(data)):
            if not isFiniteNumber(data[i]):
                data[i] = 0
        frameSamples = self.encodeFrameSamples
        if self.encodeBuffer is None:
            if self.encodeBufferA is None:
                self.encodeBufferA = array('f', [0.0]) * frameSamples
                self.encodeBufferB = array('f', [0.0]) * frameSamples
            self.encodeBuffer = self.encodeBufferA
            self.encodeBufferPos = 0
        offset = 0
        while offset < len(data):
            toCopy = min(len(data) - offset, frameSamples - self.encodeBufferPos)
            for k in range(toCopy):
                self.encodeBuffer[self.encodeBufferPos + k] = data[offset + k]
            self.encodeBufferPos += toCopy
            offset += toCopy
            if self.encodeBufferPos < frameSamples:
                break
            frameData = self.encodeBuffer
            self.encodeBuffer = self.encodeBufferB if frameData is self.encodeBufferA else self.encodeBufferA
            self.encodeBufferPos = 0
            try:
                opusFrame = self.opusCodec.encode(frameData)
                if len(opusFrame) <= 18:
                    self.qLog['tiny'] += 1
                self.sendOpusFrame(opusFrame, False)
                self.realAudioSendCount += 1
            except Exception as err:
                self.logger.error('encode error', JSObject({
                    'callId': self.info.callId,
                    'message': toError(err).message
                }))

    async def loadVideo(self, videoPath):
        if self.info.mediaType != CallMediaType.Video:
            raise JSError(f"Call {self.info.callId} was not started with isVideo — no video SSRC/RTP stream to send on")
        await self.videoEngine.loadVideoFile(videoPath)
        self.audioEngine.setLoopMode(True)
        if self.mediaFlowStarted and self.sctpRelay.hasConnection():
            self.videoEngine.start()

    def sendCapturedVideoAU(self, au, durationMs):
        hasRelay = self.sctpRelay.hasConnection()
        if self.videoRtpStream is None or self.videoSrtpSession is None or not hasRelay:
            self.videoDropCount += 1
            if self.videoDropCount == 1 or self.videoDropCount % 150 == 0:
                missing = ', '.join(name for name, flag in [
                    ('videoRtpStream', self.videoRtpStream is None),
                    ('videoSrtpSession', self.videoSrtpSession is None),
                    ('relayConnection', not hasRelay)
                ] if flag)
                self.logger.media('video access unit dropped', JSObject({
                    'callId': self.info.callId, 'dropCount': self.videoDropCount, 'missing': missing
                }))
            return
        try:
            payload = buildAccessUnitPayload(au)
            if not payload:
                return
            idr = auHasIDR(au)
            flushesBefore = self.sctpRelay.getVideoFlushCount()
            if flushesBefore != self.videoFlushSeen:
                self.videoFlushSeen = flushesBefore
                self.videoKeyframeRequired = True
            if self.videoKeyframeRequired and not idr:
                return
            mediaFrameInfo = VideoMediaFrameInfo.IDR if idr else VideoMediaFrameInfo.Delta
            self.videoRtpStream.setTimestampStride(videoRtpDurationSamples(durationMs))
            chunks = packageH264NALU(payload)
            for i in range(len(chunks)):
                header = self.videoRtpStream.nextPacket(i == len(chunks) - 1, mediaFrameInfo)
                srtpData = self.videoSrtpSession.protect(RtpPacket(header, chunks[i]))
                self.sctpRelay.broadcast(toArrayBuffer(srtpData))
                self.videoOctetsSent = (self.videoOctetsSent + len(chunks[i])) & 0xFFFFFFFF
            flushesAfter = self.sctpRelay.getVideoFlushCount()
            if flushesAfter != flushesBefore:
                self.videoFlushSeen = flushesAfter
                self.videoKeyframeRequired = True
            elif idr:
                self.videoKeyframeRequired = False
            self.videoSendCount += 1
            if idr:
                self.videoIdrSendCount += 1
            if idr or self.videoSendCount == 1 or self.videoSendCount % 150 == 0:
                self.logger.media('video sent', JSObject({
                    'callId': self.info.callId,
                    'sendCount': self.videoSendCount,
                    'idrSentTotal': self.videoIdrSendCount,
                    'auBytes': len(au),
                    'packets': len(chunks),
                    'idr': idr
                }))
        except Exception as err:
            self.logger.error('error sending video', JSObject({
                'callId': self.info.callId, 'message': toError(err).message
            }))

    def cleanup(self):
        self.logger.warn('[DIAG] cleanup entered', JSObject({'callId': self.info.callId, 'state': self.info.stateData.get('state')}))
        opusStats = None if self.opusCodec is None else self.opusCodec.getStats()
        self.logger.media('call stats', JSObject({
            'callId': self.info.callId,
            'relayPackets': self.relayPacketCount,
            'recvOk': self.audioRecvCount,
            'srtpErrors': self.srtpErrorCount,
            'sent': self.audioSendCount,
            'dropped': self.audioDropCount,
            'opusOk': orDefault(optGet(opusStats, 'success'), 0),
            'opusErr': orDefault(optGet(opusStats, 'errors'), 0),
            'videoSent': self.videoSendCount,
            'videoIdrSent': self.videoIdrSendCount,
            'videoDropped': self.videoDropCount
        }))
        self.audioEngine.setOnAudioFinished(None)
        self.audioEngine.stop()
        self.videoEngine.stop()
        self.sctpRelay.cleanup()
        if self.rtcpTimer:
            clearInterval(self.rtcpTimer)
            self.rtcpTimer = None
        if self.opusCodec is not None:
            self.opusCodec.destroy()
            self.opusCodec = None
        self.rtpSession = None
        self.srtpSession = None
        if self.appDataStream is not None:
            self.appDataStream.close()
        self.appDataStream = None
        self.appDataSrtpSession = None
        self.peerAppDataSsrcs.clear()
        self.audioRtcpSession = None
        self.audioRtcpCname = None
        self.audioOctetsSent = 0
        self.videoRtpStream = None
        self.videoSrtpSession = None
        self.videoRtcpSession = None
        self.videoRtcpCname = None
        self.videoOctetsSent = 0
        self.videoSsrc = 0
        self.videoAssembler = None
        self.peerVideoUpgradePending = False
        self.peerVideoActive = False
        self.peerVideoJids.clear()
        self.mediaStartedAtMs = None
        self.videoSendCount = 0
        self.videoIdrSendCount = 0
        self.videoDropCount = 0
        self.audioSendCount = 0
        self.audioDropCount = 0
        self.audioRecvCount = 0
        self.srtpErrorCount = 0
        self.relayPacketCount = 0
        self.stunResponseCount = 0
        self.selfEchoCount = 0
        self.lastRecvSeq = -1
        self.recvSeqGaps = 0
        self.actualPeerSsrc = None
        self.ssrcResubscribed = False
        self.recvRealCount = 0
        self.recvDtxCount = 0
        self.initialTransportSent = False
        self.outgoingPreacceptSent = False
        self.firstPacketSent = False
        self.realAudioSendCount = 0
        self.encodeBuffer = None
        self.encodeBufferPos = 0
        self.acceptedByJid = None

    @property
    def encodeFrameSamples(self):
        return orDefault(None if self.opusCodec is None else self.opusCodec.getFrameSize(), 960)

    @property
    def rtpTsDelta(self):
        return self.encodeFrameSamples

    def sendOpusFrame(self, opusFrame, isSilence):
        if self.rtpSession is None or self.srtpSession is None:
            return
        try:
            rtpPayload = opusFrame
            authPadding = SRTP_AUTH_TAG_LEN - SRTP_SEND_AUTH_TAG_LEN
            if authPadding > 0:
                if self.authPaddingBuffer is None or len(self.authPaddingBuffer) != authPadding:
                    self.authPaddingBuffer = bytes(authPadding)
                rtpPayload = concatBytes([rtpPayload, self.authPaddingBuffer])
            marker = not self.firstPacketSent
            tsDelta = self.rtpTsDelta
            rtpPacket = self.rtpSession.createPacketWithDuration(rtpPayload, tsDelta, marker)
            if self.debeEnabled:
                rtpPacket.header.extension = True
                rtpPacket.header.extensionProfile = 0xdebe
                rtpPacket.header.extensionData = WaCallMediaSession.EMPTY_BYTES
            if not self.firstPacketSent:
                self.firstPacketSent = True
            srtpData = self.srtpSession.protect(rtpPacket)
            self.sctpRelay.broadcast(toArrayBuffer(srtpData))
            self.audioSendCount += 1
            self.audioOctetsSent = (self.audioOctetsSent + len(rtpPayload)) & 0xFFFFFFFF
            if self.audioSendCount == 1 or self.audioSendCount % 500 == 0:
                self.logger.media('audio sent', JSObject({
                    'callId': self.info.callId,
                    'sendCount': self.audioSendCount,
                    'opusBytes': len(opusFrame),
                    'srtpBytes': len(srtpData),
                    'silence': isSilence,
                    'sctpBacklog': self.sctpRelay.getSendBacklog()
                }))
        except Exception as err:
            self.logger.error('error sending audio', JSObject({
                'callId': self.info.callId,
                'message': toError(err).message
            }))

    def ensureDeviceJid(self, jid):
        if re.search(r':\d+@', jid):
            return jid
        return jid.replace('@', ':0@', 1)

    def initSrtpKeys(self):
        callKey = self.info.encryptionKey
        if not callKey:
            self.logger.media('no call_key, srtp not initialized', JSObject({'callId': self.info.callId}))
            return
        meLid = optGet(self.deps.get('authClient').getCurrentCredentials(), 'meLid')
        meId = optGet(self.deps.get('authClient').getCurrentCredentials(), 'meJid')
        ourCredJid = meLid or meId or ''
        ourBase = toUserJid(ourCredJid)
        participants = orDefault(optGet(self.info.relayData, 'participantJids'), [])
        ourDeviceJid = self.ensureDeviceJid(next(
            (jid for jid in participants if toUserJid(jid) == ourBase and re.search(r':\d+@', jid)),
            None
        ) or ourCredJid)
        rawPeerJid = self.acceptedByJid or self.info.peerJid
        if not self.acceptedByJid:
            peerFromParticipants = next((jid for jid in participants if toUserJid(jid) != ourBase), None)
            if peerFromParticipants:
                rawPeerJid = peerFromParticipants
        peerDeviceJid = self.ensureDeviceJid(rawPeerJid)
        try:
            sendKeying = derivePerJidSrtpKey(callKey, ourDeviceJid)
            recvKeying = derivePerJidSrtpKey(callKey, peerDeviceJid)
            self.srtpSession = SrtpSession(sendKeying, recvKeying, SRTP_SEND_AUTH_TAG_LEN, SRTP_RECV_AUTH_TAG_LEN)
            self.appDataSrtpSession = SrtpSession(sendKeying, recvKeying, SRTP_SEND_AUTH_TAG_LEN, SRTP_RECV_AUTH_TAG_LEN)
            self.openAppDataStream(ourDeviceJid)
            self.peerAppDataSsrcs.clear()
            self.trackPeerAppDataSsrcs([peerDeviceJid] + [jid for jid in participants if toUserJid(jid) != ourBase])
            self.audioRtcpSession = SrtcpSendContext(sendKeying)
            if self.info.mediaType == CallMediaType.Video:
                self.videoSrtpSession = SrtpSession(sendKeying, recvKeying, SRTP_SEND_AUTH_TAG_LEN, SRTP_RECV_AUTH_TAG_LEN)
                self.videoRtcpSession = SrtcpSendContext(sendKeying)
            self.startRtcpSenderReports()
            self.logger.debug('srtp per-jid keys initialized', JSObject({
                'callId': self.info.callId,
                'sendJid': ourDeviceJid,
                'recvJid': peerDeviceJid
            }))
        except Exception as err:
            self.logger.debug('srtp key derivation failed', JSObject({
                'callId': self.info.callId,
                'message': toError(err).message
            }))

    def startRtcpSenderReports(self):
        if self.rtcpTimer:
            clearInterval(self.rtcpTimer)
        if not self.audioRtcpCname:
            self.audioRtcpCname = generateWhatsappRtcpCname()
        if not self.videoRtcpCname:
            self.videoRtcpCname = generateWhatsappRtcpCname()

        def tick():
            if not self.sctpRelay.hasConnection():
                return
            self.qLog['n'] += 1
            if self.qLog.get('n') % 3 == 0:
                self.logVoipQuality()
            nowMs = now()
            try:
                if self.audioRtcpSession is not None and self.selfSsrc:
                    stats = JSObject({
                        'packetsSent': self.audioSendCount,
                        'octetsSent': self.audioOctetsSent,
                        'rtpTimestamp': orDefault(optAttr(self.rtpSession, 'timestamp'), 0)
                    })
                    plain = buildSenderReportWithSdes(self.selfSsrc, stats, nowMs, self.audioRtcpCname)
                    protectedPacket = self.audioRtcpSession.protect(self.selfSsrc, plain)
                    self.sctpRelay.broadcast(toArrayBuffer(protectedPacket))
                if self.videoRtcpSession is not None and self.videoSsrc and self.videoSendCount > 0:
                    stats = JSObject({
                        'packetsSent': self.videoSendCount,
                        'octetsSent': self.videoOctetsSent,
                        'rtpTimestamp': orDefault(optAttr(self.videoRtpStream, 'timestamp'), 0)
                    })
                    plain = buildSenderReportWithSdes(self.videoSsrc, stats, nowMs, self.videoRtcpCname)
                    protectedPacket = self.videoRtcpSession.protect(self.videoSsrc, plain)
                    self.sctpRelay.broadcast(toArrayBuffer(protectedPacket))
                if self.mediaStartedAtMs:
                    wallElapsedMs = nowMs - self.mediaStartedAtMs
                    audioElapsedMs = self.audioSendCount * 60
                    videoElapsedMs = self.videoSendCount * (1000 / self.videoEngine.frameRate)
                    self.logger.media('av sync check', JSObject({
                        'callId': self.info.callId,
                        'wallElapsedMs': wallElapsedMs,
                        'audioElapsedMs': audioElapsedMs,
                        'videoElapsedMs': videoElapsedMs,
                        'audioMinusWallMs': audioElapsedMs - wallElapsedMs,
                        'videoMinusWallMs': jsRound(videoElapsedMs - wallElapsedMs)
                    }))
            except Exception as err:
                self.logger.debug('rtcp sender report failed', JSObject({
                    'callId': self.info.callId,
                    'message': toError(err).message
                }))
        self.rtcpTimer = setInterval(tick, 1500)

    def resetEncodeState(self):
        self.encodeBuffer = None
        self.encodeBufferPos = 0
        self.realAudioSendCount = 0

    def onRelayConnected(self):
        if self.info.stateData.get('state') == CallState.Connecting:
            try:
                self.info.applyTransition(JSObject({'type': 'media_connected'}))
                self.delegate.get('emitState')(self.info)
                self.logger.media('relay connected', JSObject({
                    'callId': self.info.callId, 'acceptedByJid': orDefault(self.acceptedByJid, None)
                }))
                if not self.acceptedByJid:
                    self.logger.media('relay connected before accept, deferring media start', JSObject({
                        'callId': self.info.callId
                    }))
                    return
                MEDIA_START_DELAY_MS = 3000

                def delayedStart():
                    self.logger.warn('[DIAG] delayed startMediaFlow firing (path A)', JSObject({
                        'callId': self.info.callId, 'state': self.info.stateData.get('state')
                    }))
                    if self.info.stateData.get('state') == CallState.Ended:
                        return
                    self.startMediaFlow()
                setTimeout(delayedStart, MEDIA_START_DELAY_MS)
            except Exception as err:
                self.logger.trace('call transition skipped', JSObject({'message': toError(err).message}))

    def onRelayData(self, data):
        self.relayPacketCount += 1
        if isStunPacket(data):
            self.stunResponseCount += 1
            return
        if not isRtpPacket(data):
            return
        pt = data[1] & 0x7f
        if len(self.peerAppDataSsrcs) > 0 and len(data) >= 12:
            appDataSsrc = readUInt32BE(data, 8)
            if appDataSsrc in self.peerAppDataSsrcs:
                self.onAppDataPacket(data, pt, appDataSsrc)
                return
        if pt == PayloadType.H264:
            self.onRelayVideoData(data)
            return
        if self.srtpSession is None or self.opusCodec is None:
            return
        if pt != 120:
            return
        if len(data) >= 12:
            ssrc = ((data[8] << 24) | (data[9] << 16) | (data[10] << 8) | data[11]) & 0xFFFFFFFF
            if ssrc == self.selfSsrc:
                self.selfEchoCount += 1
                return
            if not self.ssrcResubscribed and self.actualPeerSsrc is None:
                self.actualPeerSsrc = ssrc
                knownSsrc = ssrc in self.peerSsrcs
                if not knownSsrc:
                    self.peerSsrcs = [ssrc]
                    self.ssrcResubscribed = True
                    self.opusCodec.resetSequence()
                    self.lastRecvSeq = -1
                    self.sctpRelay.setSubscriptionSsrc(orDefault(self.peerSsrcs[0] if len(self.peerSsrcs) > 0 else None, 0))
                    self.sctpRelay.resendSubscriptions()
        try:
            rtpPacket = self.srtpSession.unprotect(data)
            opusPayload = rtpPacket.payload
            self.audioRecvCount += 1
            if len(opusPayload) == 0:
                return
            seq = rtpPacket.header.sequenceNumber
            if self.lastRecvSeq >= 0:
                gap = ((seq - self.lastRecvSeq + 65536) % 65536) - 1
                if gap > 0 and gap < 0x8000:
                    self.recvSeqGaps += gap
            self.lastRecvSeq = seq
            isDtx = len(opusPayload) <= 2
            if isDtx:
                self.recvDtxCount += 1
            else:
                self.recvRealCount += 1
            self.opusCodec.decodeSequenced(seq, opusPayload, self.onDecodedAudio)
            if self.audioRecvCount % 100 == 0:
                if self.lossWindow is None:
                    self.lossWindow = JSObject({'gaps': 0, 'pkts': 0})
                w = self.lossWindow
                gaps = self.recvSeqGaps - w.get('gaps')
                pkts = self.audioRecvCount - w.get('pkts')
                w['gaps'] = self.recvSeqGaps
                w['pkts'] = self.audioRecvCount
                lossPct = min(100, jsRound((100 * gaps) / (pkts + gaps))) if pkts + gaps > 0 else 0
                self.opusCodec.setExpectedPacketLossPercent(lossPct)
            if self.audioRecvCount % 1500 == 0:
                stats = self.opusCodec.getStats()
                self.logger.trace('audio recv stats', JSObject({
                    'callId': self.info.callId,
                    'recvCount': self.audioRecvCount,
                    'real': self.recvRealCount,
                    'dtx': self.recvDtxCount,
                    'decodeOk': stats.get('success'),
                    'decodeErr': stats.get('errors'),
                    'plc': stats.get('plc'),
                    'fec': stats.get('fec'),
                    'late': stats.get('late')
                }))
        except Exception as err:
            self.srtpErrorCount += 1
            if self.srtpErrorCount <= 5:
                ssrc = readUInt32BE(data, 8) if len(data) >= 12 else 0
                self.logger.debug('srtp recv error', JSObject({
                    'callId': self.info.callId,
                    'errorCount': self.srtpErrorCount,
                    'message': toError(err).message,
                    'ssrc': f'0x{ssrc:x}'
                }))

    def logVoipQuality(self):
        try:
            q = self.qLog
            pace = self.audioEngine.takePacingStats()
            bl = orDefault(self.sctpRelay.getSendBacklog(), JSObject({}))
            sent = self.audioSendCount - q.get('sent')
            octets = (self.audioOctetsSent - q.get('octets')) & 0xFFFFFFFF
            rs = orDefault(None if self.opusCodec is None else self.opusCodec.getStats(), JSObject({'late': 0, 'plc': 0, 'fec': 0, 'errors': 0}))
            line = JSObject({
                'callId': self.info.callId,
                'tSec': jsRound((now() - self.mediaStartedAtMs) / 1000) if self.mediaStartedAtMs else 0,
                'txFrames': sent,
                'txAvgB': jsRound(octets / sent) if sent > 0 else 0,
                'txTinyPct': jsRound((100 * q.get('tiny')) / sent) if sent > 0 else 0,
                'tickLate90': pace.get('late90'),
                'tickLate200': pace.get('late200'),
                'tickBurst': pace.get('burst20'),
                'tickMaxGapMs': pace.get('maxGap'),
                'loopP50ms': pace.get('loopP50'),
                'loopP99ms': pace.get('loopP99'),
                'loopMaxMs': pace.get('loopMax'),
                'sctpInFlight': orDefault(bl.get('inFlight'), 0),
                'sctpQueuedAudio': orDefault(bl.get('queuedAudio'), 0),
                'sctpRetx': orDefault(bl.get('retransmits'), 0) - q.get('retx'),
                'aLagMs': self.audioSendCount * 60 - (now() - self.mediaStartedAtMs) if self.mediaStartedAtMs else 0,
                'vLagMs': (jsRound(self.videoSendCount * (1000 / self.videoEngine.frameRate) - (now() - self.mediaStartedAtMs))
                           if (self.mediaStartedAtMs and self.videoSendCount > 0 and self.videoEngine.frameRate) else 0),
                'vRestarts': orDefault(optAttr(self.videoEngine, '_restartCount'), 0),
                'relayRx': self.relayPacketCount,
                'srtpErr': self.srtpErrorCount,
                'stun': orDefault(self.sctpRelay.getRxStunStats(), JSObject({})),
                'rxPkts': self.audioRecvCount - q.get('rx'),
                'rxLate': rs.get('late') - q.get('late'),
                'rxPlc': rs.get('plc') - q.get('plc'),
                'rxFec': rs.get('fec') - q.get('fec'),
                'rxDecErr': rs.get('errors') - q.get('errors')
            })
            self.logger.warn('[VOIPQ] ' + json.dumps(line, separators=(',', ':')))
            q['sent'] = self.audioSendCount
            q['octets'] = self.audioOctetsSent
            q['tiny'] = 0
            q['retx'] = orDefault(bl.get('retransmits'), 0)
            q['rx'] = self.audioRecvCount
            q['late'] = rs.get('late')
            q['plc'] = rs.get('plc')
            q['fec'] = rs.get('fec')
            q['errors'] = rs.get('errors')
        except Exception as err:
            self.logger.trace('voip quality log failed', JSObject({'message': toError(err).message}))

    def onRelayVideoData(self, data):
        if self.videoSrtpSession is None or self.videoAssembler is None:
            return
        try:
            rtpPacket = self.videoSrtpSession.unprotect(data)
            accessUnit, complete = self.videoAssembler.push(rtpPacket.header.sequenceNumber, rtpPacket.header.marker, rtpPacket.payload)
            if complete and accessUnit:
                callOpt(self.delegate, 'emitInboundVideo', self.info, accessUnit)
        except Exception as err:
            self.srtpErrorCount += 1
            if self.srtpErrorCount <= 5:
                self.logger.debug('video srtp recv error', JSObject({
                    'callId': self.info.callId,
                    'message': toError(err).message
                }))

    async def connectRelays(self, endpoints):
        self.logger.debug('connecting relays', JSObject({
            'callId': self.info.callId,
            'endpointCount': len(endpoints)
        }))
        seen = set()
        uniqueEndpoints = []
        for ep in endpoints:
            if orDefault(ep.get('protocol'), 0) != 0:
                continue
            key = f"{ep.get('ip')}:{ep.get('port')}"
            if key not in seen:
                seen.add(key)
                uniqueEndpoints.append(ep)
        usable = [ep for ep in uniqueEndpoints if ep.get('key') and ep.get('rawToken')]
        inbound = self.info.direction == CallDirection.Incoming
        chosen = None
        if inbound:
            chosen = next((ep for ep in usable if ep.get('isFna')), None)
        if not chosen:
            chosen = (
                next((ep for ep in usable if not ep.get('isFna') and parseIntJS(ep.get('authTokenId')) != 0), None)
                or next((ep for ep in usable if not ep.get('isFna')), None)
                or (usable[0] if len(usable) > 0 else None)
            )
        WA_RELAY_PORT = 3478
        relays = [
            JSObject({
                'ip': chosen.get('ip'),
                'port': WA_RELAY_PORT,
                'token': chosen.get('token'),
                'authToken': chosen.get('authToken'),
                'rawAuthToken': chosen.get('rawAuthToken'),
                'rawToken': chosen.get('rawToken'),
                'key': chosen.get('key'),
                'relayId': chosen.get('relayId'),
                'name': chosen.get('relayName') or f"{chosen.get('ip')}:{WA_RELAY_PORT}",
                'authTokenId': chosen.get('authTokenId'),
                'isFna': chosen.get('isFna')
            })
        ] if chosen else []
        if len(relays) == 0:
            self.logger.error('no relay configs', JSObject({'callId': self.info.callId}))
            return
        self.logger.media('relay selected', JSObject({
            'callId': self.info.callId,
            'inbound': inbound,
            'candidateCount': len(usable),
            'chosen': relays[0].get('name'),
            'isFna': relays[0].get('isFna')
        }))
        if self.selfSsrcJid:
            self.sctpRelay.setStreamSsrcs(deriveWasmRelayStreamSsrcs(self.info.callId, self.selfSsrcJid))
        self.sctpRelay.setSsrc(self.selfSsrc)
        if self.info.mediaType == CallMediaType.Video:
            self.sctpRelay.setVideoSsrc(self.videoSsrc)
        self.sctpRelay.setSubscriptionSsrc(orDefault(self.peerSsrcs[0] if len(self.peerSsrcs) > 0 else None, 0))
        try:
            await self.sctpRelay.configureRelays(relays)
            self.logger.debug('sctp relays configured', JSObject({
                'callId': self.info.callId,
                'connected': self.sctpRelay.getConnectedCount()
            }))
        except Exception as err:
            self.logger.error('sctp relay error', JSObject({
                'callId': self.info.callId,
                'message': toError(err).message
            }))

    def startMediaFlow(self):
        self.mediaStartedAtMs = now()
        self.mediaFlowStarted = True
        self.logger.warn('[DIAG] startMediaFlow entered', JSObject({'callId': self.info.callId}))
        self.resetEncodeState()
        self.audioEngine.startPlayback()
        self.audioEngine.startCapture()
        self.videoEngine.start()
