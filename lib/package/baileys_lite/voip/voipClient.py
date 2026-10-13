import asyncio
import re
import sys

from baileys_lite.foundation.node_runtime import EventEmitter, JSError, JSObject, clearTimeout, setTimeout
from baileys_lite.voip.WaVoipCoordinator import WaVoipCoordinator
from baileys_lite.voip.shim.core import createConsoleLogger
from baileys_lite.voip.shim.util import callOptional, messageOr, nullish, optGet, resolvedFuture, spawnCatching
from baileys_lite.voip.voip_deps import createVoipDeps


async def resolvePeerLid(sock, target):
    raw = str(target or '').strip()
    if not raw:
        raise JSError('resolvePeerLid: target is required')
    if raw.endswith('@lid'):
        return raw
    pnJid = raw if '@' in raw else re.sub(r'[^0-9]', '', raw) + '@s.whatsapp.net'
    mapping = optGet(optGet(sock, 'signalRepository'), 'lidMapping')
    lid = None if mapping is None else await mapping.getLIDForPN(pnJid)
    return lid or pnJid


def createVoipCtx(sock, deps, stores, logger, emitter):
    def registerIncomingHandler(options):
        tag = optGet(options, 'tag')
        prepend = optGet(options, 'prepend')
        handler = optGet(options, 'handler')

        async def runHandler(node):
            try:
                await handler(node)
            except Exception as err:
                logger.get('error')('voip incoming handler failed', JSObject({'tag': tag, 'message': optGet(err, 'message')}))

        def listener(node):
            return asyncio.ensure_future(runHandler(node))

        key = f'CB:{tag}'
        ws = optGet(sock, 'ws')
        if prepend and callable(getattr(ws, 'prependListener', None)):
            ws.prependListener(key, listener)
        else:
            ws.on(key, listener)

        def unregister():
            try:
                ws.off(key, listener)
            except Exception:
                pass

        return unregister

    def emit(event, payload):
        emitter.emit(event, payload)

    return JSObject({
        'deps': deps,
        'stores': stores,
        'logger': logger,
        'registerIncomingHandler': registerIncomingHandler,
        'emit': emit,
    })


class ActiveCall(EventEmitter):
    def __init__(self, coordinator, callId, durationMs):
        super().__init__()
        self.callId = callId
        self._coordinator = coordinator
        self._endPromise = asyncio.get_running_loop().create_future()
        self._endTimer = None
        self._ended = False
        self._connectedEmitted = False
        if durationMs > 0:
            self._endTimer = setTimeout(lambda: self.end(), durationMs)

    def _onState(self, call):
        if call.callId != self.callId:
            return
        if call.isRinging:
            self.emit('ringing')
        if call.isActive and not self._connectedEmitted:
            self._connectedEmitted = True
            self.emit('connected')

    def _onEnded(self, call):
        if call.callId != self.callId:
            return
        endReason = optGet(call.stateData, 'endReason')
        self._forceEnd('ended' if endReason is None else endReason)

    def _onError(self, err):
        self.emit('error', err)

    def setMute(self, muted):
        return self._coordinator.setMute(self.callId, bool(muted))

    def raiseHand(self, raised=True):
        raised = True if raised is None else raised
        return self._coordinator.setHandRaised(self.callId, bool(raised))

    def shareScreen(self, sharing=True):
        sharing = True if sharing is None else sharing
        return self._coordinator.setScreenShare(self.callId, bool(sharing))

    def react(self, emoji):
        return self._coordinator.sendReaction(self.callId, emoji)

    def upgradeToVideo(self):
        return self._coordinator.startVideoMidCall(self.callId)

    def end(self):
        if self._ended:
            return resolvedFuture()
        if self._endTimer is not None:
            clearTimeout(self._endTimer)
            self._endTimer = None
        return asyncio.ensure_future(self._awaitEnd(self._coordinator.endCall(self.callId)))

    async def _awaitEnd(self, pending):
        try:
            await pending
        except Exception:
            self._forceEnd('ended')
            raise

    def waitForEnd(self):
        return self._endPromise

    @property
    def connected(self):
        return self._connectedEmitted

    @property
    def ended(self):
        return self._ended

    def _forceEnd(self, reason):
        if self._ended:
            return
        self._ended = True
        if self._endTimer is not None:
            clearTimeout(self._endTimer)
            self._endTimer = None
        self.emit('ended', reason)
        if not self._endPromise.done():
            self._endPromise.set_result(reason)


class VoipClient:
    def __init__(self, config):
        self._config = config
        self._sock = None
        self._coordinator = None
        self._activeCalls = {}
        self._offSocketClose = None
        self._connecting = None
        if not optGet(config, 'existingSocket'):
            raise JSError('VoipClient requires { existingSocket }: VOIP always runs on the main bot session now, there is no standalone-device mode.')

    @property
    def coordinator(self):
        return self._coordinator

    @property
    def activeCalls(self):
        return list(self._activeCalls.values())

    def connect(self):
        if self._coordinator is not None and self._sock is optGet(self._config, 'existingSocket'):
            return resolvedFuture()
        if self._connecting is None:
            task = asyncio.ensure_future(self._setup())
            self._connecting = task

            def clearConnecting(_finished):
                self._connecting = None

            task.add_done_callback(clearConnecting)
        return self._connecting

    async def _setup(self):
        self._sock = optGet(self._config, 'existingSocket')
        result = await createVoipDeps(self._sock)
        deps = optGet(result, 'deps')
        stores = optGet(result, 'stores')
        logger = createConsoleLogger(nullish(optGet(self._config, 'voipLogLevel'), 'warn'))
        emitter = EventEmitter()
        ctx = createVoipCtx(self._sock, deps, stores, logger, emitter)
        self._coordinator = WaVoipCoordinator(ctx, JSObject({
            'maxConcurrentCalls': nullish(optGet(self._config, 'maxConcurrentCalls'), 1),
            'logLevel': nullish(optGet(self._config, 'voipLogLevel'), 'warn'),
        }))
        sock = self._sock

        def onUpdate(update):
            if optGet(update, 'connection') != 'close':
                return
            if self._sock is not sock:
                return
            logger.get('warn')('voip: socket closed, tearing down active calls')
            self._teardown('connection_closed')

        callOptional(optGet(sock, 'ev'), 'on', 'connection.update', onUpdate)

        def offSocketClose():
            try:
                callOptional(optGet(sock, 'ev'), 'off', 'connection.update', onUpdate)
            except Exception:
                pass

        self._offSocketClose = offSocketClose

    def _teardown(self, reason):
        calls = list(self._activeCalls.values())
        self._activeCalls.clear()
        if self._offSocketClose is not None:
            self._offSocketClose()
        self._offSocketClose = None
        for call in calls:
            try:
                call._forceEnd(reason)
            except Exception:
                pass
        try:
            if self._coordinator is not None:
                self._coordinator.dispose()
        except Exception:
            pass
        self._sock = None
        self._coordinator = None

    async def call(self, phoneNumber, opts=None):
        opts = JSObject({}) if opts is None else opts
        coordinator = self._coordinator
        if self._sock is None or coordinator is None:
            raise JSError('Not connected. Call connect() first.')
        durationMs = nullish(optGet(opts, 'durationMs'), 120000)
        peerJid = await resolvePeerLid(self._sock, phoneNumber)
        audioSource = optGet(opts, 'audioSource')
        audioFile = audioSource if (audioSource and audioSource != 'silence') else None
        callId = await coordinator.startCall(JSObject({
            'peerJid': peerJid,
            'isVideo': bool(optGet(opts, 'isVideo')),
            'audioFile': audioFile,
            'videoConfig': optGet(opts, 'videoConfig'),
        }))
        if self._coordinator is not coordinator:
            raise JSError('Connection closed while placing the call.')
        if audioFile:
            try:
                await coordinator.loadAudio(callId, audioFile)
            except Exception as e:
                print(f'[ VOIP ] Failed to load audio "{audioFile}" for call {callId}:', messageOr(e), file=sys.stderr)
        if optGet(opts, 'isVideo') and optGet(opts, 'videoSource'):
            videoSource = optGet(opts, 'videoSource')
            try:
                await coordinator.loadVideo(callId, videoSource)
            except Exception as e:
                print(f'[ VOIP ] Failed to load video "{videoSource}" for call {callId}:', messageOr(e), file=sys.stderr)
        if self._coordinator is not coordinator:
            raise JSError('Connection closed while placing the call.')
        return self._track(coordinator, callId, durationMs)

    def _track(self, coordinator, callId, durationMs):
        call = ActiveCall(coordinator, callId, durationMs)
        self._activeCalls[callId] = call
        call.coordinator = coordinator

        def onState(info):
            call._onState(info)

        def onEnded(info):
            call._onEnded(info)

        def onError(err):
            errCallId = optGet(err, 'callId')
            if errCallId and errCallId != callId:
                return
            call._onError(err)

        coordinator.on('call_state', onState)
        coordinator.on('call_ended', onEnded)
        coordinator.on('call_error', onError)

        specs = [
            ('call_peer_mute', 'peer_mute', lambda info, muted: [muted]),
            ('call_hand_raise', 'hand_raise', lambda info, jid, raised: [JSObject({'jid': jid, 'raised': raised})]),
            ('call_reaction', 'reaction', lambda info, reaction: [reaction]),
            ('call_screen_share', 'screen_share', lambda info, share: [share]),
            ('call_video_state', 'peer_video', lambda info, change: [change]),
            ('call_inbound_audio', 'inbound_audio', lambda info, pcm: [pcm]),
            ('call_inbound_video', 'inbound_video', lambda info, frame: [frame]),
            ('call_outbound_audio_finished', 'audio_finished', lambda info: []),
        ]
        forwarded = []
        for source, target, shape in specs:
            def listener(info, *rest, target=target, shape=shape):
                if optGet(info, 'callId') == callId:
                    call.emit(target, *shape(info, *rest))

            coordinator.on(source, listener)
            forwarded.append((source, listener))

        def cleanup(*_args):
            for source, listener in forwarded:
                coordinator.off(source, listener)
            coordinator.off('call_state', onState)
            coordinator.off('call_ended', onEnded)
            coordinator.off('call_error', onError)
            if self._activeCalls.get(callId) is call:
                del self._activeCalls[callId]

        call.once('ended', cleanup)
        return call

    async def answer(self, callId, opts=None):
        opts = JSObject({}) if opts is None else opts
        coordinator = self._coordinator
        if self._sock is None or coordinator is None:
            raise JSError('Not connected. Call connect() first.')
        info = coordinator.getCall(callId)
        if info is None:
            raise JSError(f'Unknown call {callId}.')
        if not info.canAccept:
            raise JSError(f'Call {callId} cannot be accepted right now.')
        call = self._track(coordinator, callId, nullish(optGet(opts, 'durationMs'), 0))
        try:
            await coordinator.acceptCall(callId)
        except Exception:
            call._forceEnd('failed')
            raise
        audioSource = optGet(opts, 'audioSource')
        audioFile = audioSource if (audioSource and audioSource != 'silence') else None
        if audioFile:
            try:
                await coordinator.loadAudio(callId, audioFile)
            except Exception as e:
                print(f'[ VOIP ] Failed to load audio "{audioFile}" for call {callId}:', messageOr(e), file=sys.stderr)
        if optGet(opts, 'videoSource'):
            videoSource = optGet(opts, 'videoSource')
            try:
                await coordinator.loadVideo(callId, videoSource)
            except Exception as e:
                print(f'[ VOIP ] Failed to load video "{videoSource}" for call {callId}:', messageOr(e), file=sys.stderr)
        return call

    def disconnect(self):
        for call in list(self._activeCalls.values()):
            try:
                spawnCatching(call.end(), lambda err: None)
            except Exception:
                pass
        self._teardown('disconnected')
