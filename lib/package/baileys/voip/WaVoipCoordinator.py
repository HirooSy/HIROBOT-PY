from baileys.foundation.node_runtime import JSObject
from baileys.voip.call.WaCallManager import WaCallManager
from baileys.voip.shim.protocol import WA_MESSAGE_TAGS
from baileys.voip.shim.util import optGet
from baileys.voip.signaling.bridge import routeCallAck, routeCallReceipt, routeCallStanza


class WaVoipCoordinator:
    def __init__(self, ctx, options=None):
        options = JSObject({}) if options is None else options
        self.unregisterHandlers = []
        self.deps = ctx.get('deps')
        self.logger = ctx.get('logger').get('child')(JSObject({'scope': 'voip'}), JSObject({'level': optGet(options, 'logLevel')}))
        self.manager = WaCallManager(JSObject({
            'deps': ctx.get('deps'),
            'stores': ctx.get('stores'),
            'logger': self.logger,
            'maxConcurrentCalls': optGet(options, 'maxConcurrentCalls'),
        }))
        self.registerIncomingHandlers(ctx)
        self.wireClientEvents(ctx)

    async def startCall(self, options):
        return await self.manager.startCall(options)

    async def acceptCall(self, callId):
        return await self.manager.acceptCall(callId)

    async def rejectCall(self, callId, reason=None):
        return await self.manager.rejectCall(callId, reason)

    async def endCall(self, callId, reason=None):
        return await self.manager.endCall(callId, reason)

    async def loadAudio(self, callId, audioPath):
        return await self.manager.loadAudio(callId, audioPath)

    async def loadVideo(self, callId, videoPath):
        return await self.manager.loadVideo(callId, videoPath)

    async def enableVideoMidCall(self, callId):
        return await self.manager.enableVideoMidCall(callId)

    async def disableVideoMidCall(self, callId, options=None):
        return await self.manager.disableVideoMidCall(callId, options)

    async def startVideoMidCall(self, callId):
        return await self.manager.startVideoMidCall(callId)

    async def stopVideoMidCall(self, callId, options=None):
        return await self.manager.stopVideoMidCall(callId, options)

    async def swapVideoSource(self, callId, videoPath):
        return await self.manager.swapVideoSource(callId, videoPath)

    def setMute(self, callId, muted):
        self.manager.setMute(callId, muted)

    async def setHandRaised(self, callId, raised):
        return await self.manager.setHandRaised(callId, raised)

    async def setScreenShare(self, callId, sharing):
        return await self.manager.setScreenShare(callId, sharing)

    def sendReaction(self, callId, reaction):
        return self.manager.sendReaction(callId, reaction)

    def setExternalAudioMode(self, callId, enabled):
        self.manager.setExternalAudioMode(callId, enabled)

    def feedLiveAudio(self, callId, data):
        return self.manager.feedLiveAudio(callId, data)

    def getLiveBufferMs(self, callId):
        return self.manager.getLiveBufferMs(callId)

    def getFeedWatermarksMs(self):
        return self.manager.getFeedWatermarksMs()

    def getCall(self, callId):
        return self.manager.getCall(callId)

    def getCalls(self):
        return self.manager.getCalls()

    def on(self, event, listener):
        self.manager.on(event, listener)
        return self

    def off(self, event, listener):
        self.manager.off(event, listener)
        return self

    def once(self, event, listener):
        self.manager.once(event, listener)
        return self

    def dispose(self):
        pending = self.unregisterHandlers[:]
        self.unregisterHandlers[:] = []
        for unregister in pending:
            unregister()
        self.manager.destroy()

    def registerIncomingHandlers(self, ctx):
        async def callHandler(node):
            tag = await routeCallStanza(self.manager, self.deps, node, self.logger)
            return tag is not None

        async def ackHandler(node):
            if optGet(optGet(node, 'attrs'), 'class') != 'call':
                return False
            await routeCallAck(self.manager, node)
            return True

        async def receiptHandler(node):
            return await routeCallReceipt(self.deps, node)

        self.unregisterHandlers.extend([
            ctx.get('registerIncomingHandler')(JSObject({'tag': 'call', 'prepend': True, 'handler': callHandler})),
            ctx.get('registerIncomingHandler')(JSObject({'tag': WA_MESSAGE_TAGS['ACK'], 'prepend': True, 'handler': ackHandler})),
            ctx.get('registerIncomingHandler')(JSObject({'tag': WA_MESSAGE_TAGS['RECEIPT'], 'prepend': True, 'handler': receiptHandler})),
        ])

    def wireClientEvents(self, ctx):
        self.manager.on('call_state', lambda call: ctx.get('emit')('voip_call_state', call))
        self.manager.on('call_incoming', lambda call: ctx.get('emit')('voip_call_incoming', call))
        self.manager.on('call_ended', lambda call: ctx.get('emit')('voip_call_ended', call))
        self.manager.on('call_inbound_audio', lambda call, pcm: ctx.get('emit')('voip_call_inbound_audio', JSObject({'call': call, 'pcm': pcm})))
        self.manager.on('call_outbound_audio_finished', lambda call: ctx.get('emit')('voip_call_outbound_audio_finished', call))
        self.manager.on('call_peer_mute', lambda call, muted: ctx.get('emit')('voip_call_peer_mute', JSObject({'call': call, 'muted': muted})))
        self.manager.on('call_hand_raise', lambda call, participantJid, raised: ctx.get('emit')('voip_call_hand_raise', JSObject({'call': call, 'participantJid': participantJid, 'raised': raised})))
        self.manager.on('call_reaction', lambda call, reaction: ctx.get('emit')('voip_call_reaction', JSObject({'call': call, 'reaction': reaction})))
        self.manager.on('call_screen_share', lambda call, share: ctx.get('emit')('voip_call_screen_share', JSObject({'call': call, 'share': share})))
        self.manager.on('call_error', lambda error: ctx.get('emit')('voip_call_error', error))
