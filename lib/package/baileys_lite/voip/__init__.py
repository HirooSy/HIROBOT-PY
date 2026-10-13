import asyncio
import json
import math
import os
import random
import re
import string
import subprocess
import tempfile
import urllib.error
import urllib.request

from baileys_lite.foundation.node_runtime import EventEmitter, JSError, JSObject, clearTimeout, now, setTimeout
from baileys_lite.voip.shim.util import (
    isSafeInteger,
    itemAt,
    jsRound,
    messageOr,
    nullish,
    optGet,
    parseFloatJs,
    parseIntJs,
    spawnCatching,
    toNumberJs,
)
from baileys_lite.voip.voipClient import VoipClient
from baileys_lite.voip.types import CallMediaType, CallDirection


RESOLUTION_PRESETS = JSObject({
    '240p': JSObject({'width': 320, 'height': 240, 'frameRate': 20}),
    '360p': JSObject({'width': 640, 'height': 360, 'frameRate': 30}),
    '480p': JSObject({'width': 854, 'height': 480, 'frameRate': 30}),
    '720p': JSObject({'width': 1280, 'height': 720, 'frameRate': 30}),
    '1080p': JSObject({'width': 1920, 'height': 1080, 'frameRate': 30}),
})

VIDEO_EXTENSIONS = re.compile(r'\.(mp4|mov|webm|mkv|avi|m4v|3gp)(\?|#|\Z)', re.IGNORECASE)
AUDIO_EXTENSIONS = re.compile(r'\.(mp3|ogg|opus|wav|m4a|aac|flac|weba)(\?|#|\Z)', re.IGNORECASE)
URL_PREFIX = re.compile(r'https?://', re.IGNORECASE)


def resolveVideoConfig(resolution, sourceDims):
    if not resolution:
        if not optGet(sourceDims, 'width') or not optGet(sourceDims, 'height'):
            return None
        return applyOrientation(RESOLUTION_PRESETS['480p'], sourceDims)
    if isinstance(resolution, dict):
        return resolution
    key = str(resolution).strip().lower()
    preset = RESOLUTION_PRESETS[key]
    if preset is None:
        known = ', '.join(dict.keys(RESOLUTION_PRESETS))
        raise JSError(f'Unknown resolution "{resolution}". Use one of: {known}, or pass {{ width, height, frameRate }} directly.')
    return applyOrientation(preset, sourceDims)


def applyOrientation(preset, sourceDims):
    if not optGet(sourceDims, 'width') or not optGet(sourceDims, 'height'):
        return JSObject({**preset})
    sourceIsPortrait = optGet(sourceDims, 'height') > optGet(sourceDims, 'width')
    presetIsPortrait = preset.get('height') > preset.get('width')
    if sourceIsPortrait == presetIsPortrait:
        return JSObject({**preset})
    return JSObject({**preset, 'width': preset.get('height'), 'height': preset.get('width')})


def detectKind(source):
    if VIDEO_EXTENSIONS.search(str(source)):
        return 'video'
    if AUDIO_EXTENSIONS.search(str(source)):
        return 'audio'
    return None


def isUrl(source):
    return URL_PREFIX.match(str(source)) is not None


def fetchDownload(url, maxBytes):
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as err:
        raise JSError(f'Download failed: HTTP {err.code}')
    with response:
        try:
            declared = int(response.headers.get('content-length') or 0)
        except ValueError:
            declared = 0
        if declared and declared > maxBytes:
            raise JSError(f'Download too large: {declared} bytes (max {maxBytes})')
        data = response.read(maxBytes + 1)
    if len(data) > maxBytes:
        raise JSError(f'Download too large: {len(data)} bytes (max {maxBytes})')
    return data


async def downloadToTemp(url, extHint, maxBytes, tmpDirOverride):
    data = await asyncio.to_thread(fetchDownload, url, maxBytes)
    tmpDir = tmpDirOverride or tempfile.gettempdir()
    if not os.path.exists(tmpDir):
        os.makedirs(tmpDir, exist_ok=True)
    suffix = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    filePath = os.path.join(tmpDir, f'voip_{now()}_{suffix}{extHint}')
    with open(filePath, 'wb') as handle:
        handle.write(data)
    return filePath


def getEffectiveRotation(videoStream):
    tagRotate = optGet(optGet(videoStream, 'tags'), 'rotate')
    if tagRotate is not None:
        n = parseIntJs(tagRotate)
        if n is not None:
            return ((n % 360) + 360) % 360
    sideDataList = optGet(videoStream, 'side_data_list')
    matrixSideData = None
    if sideDataList is not None:
        matrixSideData = next((sd for sd in sideDataList if optGet(sd, 'rotation') is not None), None)
    if matrixSideData is not None:
        n = jsRound(optGet(matrixSideData, 'rotation'))
        if n is not None:
            return ((n % 360) + 360) % 360
    return 0


async def probeMedia(filePath, ffprobePath):
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            [ffprobePath, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', filePath],
            capture_output=True,
            check=True,
        )
        parsed = json.loads(completed.stdout)
        duration = optGet(optGet(parsed, 'format'), 'duration')
        if not duration or math.isnan(toNumberJs(duration)):
            durationMs = None
        else:
            durationMs = math.ceil(parseFloatJs(duration) * 1000)
        streams = optGet(parsed, 'streams')
        videoStream = None
        if streams is not None:
            videoStream = next((s for s in streams if optGet(s, 'codec_type') == 'video'), None)
        width = optGet(videoStream, 'width') or None
        height = optGet(videoStream, 'height') or None
        rotation = getEffectiveRotation(videoStream)
        if width and height and abs(rotation) % 180 == 90:
            width, height = height, width
        return JSObject({'durationMs': durationMs, 'width': width, 'height': height})
    except Exception:
        return JSObject({'durationMs': None, 'width': None, 'height': None})


async def normalizeItem(item, ffprobePath, tmpDir):
    kind = None
    rawSource = None
    autoAdvance = False
    if isinstance(item, str):
        rawSource = item
        kind = nullish(detectKind(item), 'audio')
    elif item is not None and isinstance(item, (dict, list)):
        autoAdvance = bool(optGet(item, 'autoAdvance'))
        if optGet(item, 'video'):
            rawSource = optGet(item, 'video')
            kind = 'video'
        elif optGet(item, 'audio'):
            rawSource = optGet(item, 'audio')
            kind = 'audio'
        else:
            raise JSError('Playlist item must be a string, or an object with a "video" or "audio" key.')
    else:
        raise JSError('Playlist item must be a string or an object.')
    source = rawSource
    isTemp = False
    if isUrl(rawSource):
        extHint = '.mp4' if kind == 'video' else '.audio'
        maxBytes = 50 * 1024 * 1024 if kind == 'video' else 20 * 1024 * 1024
        source = await downloadToTemp(rawSource, extHint, maxBytes, tmpDir)
        isTemp = True
    elif not (isinstance(rawSource, str) and os.path.exists(rawSource)):
        raise JSError(f'Media file not found: {rawSource}')
    probed = await probeMedia(source, ffprobePath)
    result = JSObject({
        'kind': kind,
        'source': source,
        'isTemp': isTemp,
        'durationMs': probed.get('durationMs'),
        'autoAdvance': autoAdvance,
    })
    if kind == 'video' and probed.get('width') and probed.get('height'):
        result['sourceWidth'] = probed.get('width')
        result['sourceHeight'] = probed.get('height')
    return result


class VoipCall(EventEmitter):
    def __init__(self, items, onRelease, opts=None):
        super().__init__()
        opts = JSObject({}) if opts is None else opts
        self._activeCall = None
        self._coordinator = None
        self._items = items
        self._index = -1
        self._ended = False
        self._onRelease = onRelease
        self._autoEndCall = True if optGet(opts, 'autoEndCall') is None else bool(optGet(opts, 'autoEndCall'))
        self._loop = bool(optGet(opts, 'loop'))
        self._onItemAdvance = optGet(opts, 'onItemAdvance')
        self._target = optGet(opts, 'target')
        self._autoDowngrade = True if optGet(opts, 'autoDowngrade') is None else bool(optGet(opts, 'autoDowngrade'))
        self._startVideoOnFirst = bool(optGet(opts, 'startVideoOnFirst'))
        self._videoDown = False
        self._silenced = False
        self._itemTimer = None

    async def _attach(self, activeCall):
        self._activeCall = activeCall
        self._coordinator = activeCall.coordinator

        def onConnected(*args):
            self.emit('connected')
            spawnCatching(self._advance(), lambda err: self.emit('error', err))

        activeCall.on('ringing', lambda *args: self.emit('ringing'))
        activeCall.on('connected', onConnected)
        activeCall.on('ended', lambda reason=None: self._finish(reason))
        activeCall.on('error', lambda err: self.emit('error', err))

        def onPeerVideo(change):
            spawnCatching(self._onPeerVideo(change), lambda err: self.emit('error', err))

        activeCall.on('peer_video', onPeerVideo)
        if activeCall.ended:
            self._finish('ended')
            return
        if activeCall.connected:
            self.emit('connected')
            spawnCatching(self._advance(), lambda err: self.emit('error', err))
        for ev in ['peer_mute', 'hand_raise', 'reaction', 'screen_share', 'peer_video', 'inbound_audio', 'inbound_video', 'audio_finished']:
            activeCall.on(ev, lambda *args, ev=ev: self.emit(ev, *args))

    @property
    def callId(self):
        return None if self._activeCall is None else self._activeCall.callId

    @property
    def target(self):
        return self._target

    def mute(self, value=True):
        value = True if value is None else value
        if self._activeCall is None:
            return None
        return self._activeCall.setMute(value)

    def raiseHand(self, value=True):
        value = True if value is None else value
        if self._activeCall is None:
            return None
        return self._activeCall.raiseHand(value)

    def shareScreen(self, value=True):
        value = True if value is None else value
        if self._activeCall is None:
            return None
        return self._activeCall.shareScreen(value)

    def react(self, emoji):
        result = None if self._activeCall is None else self._activeCall.react(emoji)
        return False if result is None else result

    async def upgradeToVideo(self):
        if self._activeCall is not None:
            await self._activeCall.upgradeToVideo()
        self._videoDown = False

    @property
    def isVideoDowngraded(self):
        return self._videoDown

    async def _onPeerVideo(self, change):
        if not self._autoDowngrade or self._ended or self._silenced or self._videoDown:
            return
        if optGet(change, 'direction') != 'inbound' or optGet(change, 'active') or not optGet(change, 'allOff'):
            return
        current = itemAt(self._items, self._index)
        if optGet(current, 'kind') != 'video':
            return
        callId = None if self._activeCall is None else self._activeCall.callId
        if not callId:
            return
        self._videoDown = True
        try:
            await self._coordinator.stopVideoMidCall(callId, JSObject({'keepSource': True}))
            self.emit('downgraded')
        except Exception as err:
            self._videoDown = False
            raise JSError(f'Failed to downgrade call to audio: {messageOr(err)}')

    async def _advance(self):
        if self._silenced:
            return
        previous = itemAt(self._items, self._index)
        self._index += 1
        next_ = itemAt(self._items, self._index)
        if not next_:
            if self._loop and len(self._items) > 0:
                self.emit('playlist_looped')
                self._index = 0
                next_ = itemAt(self._items, 0)
            else:
                self.emit('playlist_ended')
                if self._autoEndCall:
                    spawnCatching(self.end(), lambda err: self.emit('error', err))
                return
        isFirst = previous is None and self._index == 0
        nextKind = next_.get('kind')
        nextSource = next_.get('source')
        if isFirst and nextKind == 'video' and self._startVideoOnFirst:
            try:
                await self._coordinator.startVideoMidCall(self._activeCall.callId)
            except Exception as err:
                self.emit('error', JSError(f'Failed to start video: {messageOr(err)}'))
        if not isFirst:
            callId = self._activeCall.callId
            videoRunning = optGet(previous, 'kind') == 'video' and not self._videoDown
            try:
                if nextKind == 'video':
                    if optGet(previous, 'kind') == 'video':
                        await self._coordinator.swapVideoSource(callId, nextSource)
                    else:
                        await self._coordinator.loadVideo(callId, nextSource)
                    await self._coordinator.loadAudio(callId, nextSource)
                    if not videoRunning:
                        await self._coordinator.startVideoMidCall(callId)
                else:
                    await self._coordinator.loadAudio(callId, nextSource)
                    if videoRunning:
                        await self._coordinator.stopVideoMidCall(callId, JSObject({'keepSource': True}))
                self._videoDown = False
            except Exception as err:
                self.emit('error', JSError(f'Failed to advance to playlist item {self._index} ({nextKind} "{nextSource}"): {messageOr(err)}'))
            finally:
                if not self._loop:
                    self._cleanupItem(previous)
        self.emit('item', JSObject({'index': self._index, 'kind': nextKind, 'source': nextSource}))
        if self._onItemAdvance is not None:
            self._onItemAdvance(next_)
        if (nextKind != 'video' or optGet(next_, 'autoAdvance')) and optGet(next_, 'durationMs'):
            def onItemTimer():
                spawnCatching(self._advance(), lambda err: self.emit('error', err))

            self._itemTimer = setTimeout(onItemTimer, next_.get('durationMs'))

    def _cleanupItem(self, item):
        source = optGet(item, 'source')
        if optGet(item, 'isTemp') and isinstance(source, str) and os.path.exists(source):
            try:
                os.unlink(source)
            except OSError:
                pass

    def _finish(self, reason):
        if self._ended:
            return
        self._ended = True
        if self._itemTimer is not None:
            clearTimeout(self._itemTimer)
        for item in self._items:
            self._cleanupItem(item)
        self._onRelease()
        self.emit('ended', reason)

    async def hangup(self):
        if self._ended:
            return None
        if self._activeCall is not None:
            await self._activeCall.end()
        return None

    async def silent(self, value=None):
        if self._ended:
            return self._silenced
        nextState = (not self._silenced) if value is None else bool(value)
        if nextState == self._silenced:
            return self._silenced
        self._silenced = nextState
        callId = None if self._activeCall is None else self._activeCall.callId
        if not callId:
            return self._silenced
        current = itemAt(self._items, self._index)
        try:
            if nextState:
                self._coordinator.setMute(callId, True)
                if optGet(current, 'kind') == 'video' and not self._videoDown:
                    await self._coordinator.stopVideoMidCall(callId, JSObject({'keepSource': True}))
                    self._videoDown = True
                if self._itemTimer is not None:
                    clearTimeout(self._itemTimer)
                    self._itemTimer = None
            else:
                self._coordinator.setMute(callId, False)
                if optGet(current, 'kind') == 'video':
                    await self._coordinator.startVideoMidCall(callId)
                    self._videoDown = False
            self.emit('silent', self._silenced)
        except Exception as err:
            self._silenced = not nextState
            verb = 'silence' if nextState else 'resume'
            self.emit('error', JSError(f'Failed to {verb} call: {messageOr(err)}'))
        return self._silenced

    @property
    def isSilenced(self):
        return self._silenced

    async def end(self):
        return await self.hangup()


class Voip:
    def __init__(self, conn, opts=None):
        opts = JSObject({}) if opts is None else opts
        self._conn = conn
        self._client = None
        self._clientForConn = None
        self._calls = {}
        self._pending = 0
        self._ffprobePath = optGet(opts, 'ffprobePath') or 'ffprobe'
        self._voipLogLevel = nullish(optGet(opts, 'voipLogLevel'), 'warn')
        self._maxConcurrentCalls = nullish(optGet(opts, 'maxConcurrentCalls'), 1)
        if not isSafeInteger(self._maxConcurrentCalls) or self._maxConcurrentCalls < 1:
            raise JSError('maxConcurrentCalls must be a positive integer.')
        self._tmpDir = optGet(opts, 'tmpDir')

    def _getClient(self):
        if self._client is not None and self._clientForConn is self._conn:
            return self._client
        self._client = VoipClient(JSObject({
            'existingSocket': self._conn,
            'voipLogLevel': self._voipLogLevel,
            'maxConcurrentCalls': self._maxConcurrentCalls,
        }))
        self._clientForConn = self._conn
        return self._client

    @property
    def calls(self):
        return list(dict.keys(self._calls))

    @property
    def maxConcurrentCalls(self):
        return self._maxConcurrentCalls

    def _assertCapacity(self):
        if len(self._calls) + self._pending >= self._maxConcurrentCalls:
            raise JSError(f'Maximum concurrent calls reached ({self._maxConcurrentCalls}), wait for a call to end.')

    async def call(self, jid, media, resolution, options=None):
        options = JSObject({}) if options is None else options
        self._assertCapacity()
        targetJid = re.sub(r'[^0-9]', '', str(jid or ''))
        if not targetJid:
            raise JSError('Invalid phone number / jid.')

        def begin(client, first, videoConfig):
            extra = {'videoSource': first.get('source')} if first.get('kind') == 'video' else {}
            return client.call(targetJid, JSObject({
                'audioSource': first.get('source'),
                'isVideo': first.get('kind') == 'video',
                **extra,
                'durationMs': 0,
                'videoConfig': videoConfig,
            }))

        return await self._launch(targetJid, media, resolution, options, begin)

    async def accept(self, callId, media, resolution, options=None):
        options = JSObject({}) if options is None else options
        await self.listen()
        self._assertCapacity()
        info = self.coordinator.getCall(callId)
        if info is None:
            raise JSError(f'Unknown call {callId}.')
        if not info.canAccept:
            raise JSError(f'Call {callId} cannot be accepted right now.')
        callIsVideo = info.mediaType == CallMediaType.Video
        launchOptions = JSObject({**options, 'startVideoOnFirst': not callIsVideo})

        def begin(client, first, videoConfig):
            return client.answer(callId, JSObject({
                'audioSource': first.get('source'),
                'videoSource': first.get('source') if first.get('kind') == 'video' else None,
                'durationMs': 0,
            }))

        return await self._launch(info.peerJid, media, resolution, launchOptions, begin)

    async def reject(self, callId, reason=None):
        await self.listen()
        return await self.coordinator.rejectCall(callId, reason)

    @property
    def incoming(self):
        coordinator = self.coordinator
        calls = None if coordinator is None else coordinator.getCalls()
        if calls is None:
            calls = []
        return [info for info in calls if info.direction == CallDirection.Incoming and info.isRinging]

    def onIncoming(self, listener):
        coordinator = self._requireCoordinator()

        def wrapped(info):
            return listener(JSObject({
                'callId': info.callId,
                'jid': info.peerJid,
                'creator': info.callCreator,
                'isVideo': info.mediaType == CallMediaType.Video,
                'canAccept': info.canAccept,
                'info': info,
            }))

        coordinator.on('call_incoming', wrapped)

        def unregister():
            return coordinator.off('call_incoming', wrapped)

        return unregister

    async def _launch(self, targetJid, media, resolution, options, begin):
        self._pending += 1
        reserved = True

        def releaseReservation():
            nonlocal reserved
            if not reserved:
                return
            reserved = False
            self._pending -= 1

        items = []

        def cleanupItems():
            for item in items:
                source = optGet(item, 'source')
                if optGet(item, 'isTemp') and isinstance(source, str) and os.path.exists(source):
                    try:
                        os.unlink(source)
                    except OSError:
                        pass

        try:
            rawItems = media if isinstance(media, list) else [nullish(media, 'silence')]
            for raw in rawItems:
                if raw == 'silence' or raw is None:
                    items.append(JSObject({'kind': 'audio', 'source': 'silence', 'isTemp': False}))
                    continue
                items.append(await normalizeItem(raw, self._ffprobePath, self._tmpDir))
            first = itemAt(items, 0)
            videoConfig = resolveVideoConfig(resolution, JSObject({
                'width': optGet(first, 'sourceWidth'),
                'height': optGet(first, 'sourceHeight'),
            }))
            client = self._getClient()
            await client.connect()
            safetyTimer = JSObject({'handle': None})
            call = None

            def scheduleSafety(ms):
                if safetyTimer.get('handle') is not None:
                    clearTimeout(safetyTimer.get('handle'))

                def onSafety():
                    call.emit('error', JSError('Safety timeout — call never reached ended/error.'))
                    self._calls.pop(call, None)

                safetyTimer['handle'] = setTimeout(onSafety, ms)

            def rescheduleSafetyForItem(item):
                if optGet(item, 'kind') == 'video':
                    windowMs = 10 * 60000
                else:
                    durationMs = optGet(item, 'durationMs')
                    windowMs = max(120000 if durationMs is None else durationMs, 45000) + 60000
                scheduleSafety(windowMs)

            def onRelease():
                self._calls.pop(call, None)

            call = VoipCall(items, onRelease, JSObject({
                'autoEndCall': optGet(options, 'autoEndCall'),
                'loop': optGet(options, 'loop'),
                'autoDowngrade': optGet(options, 'autoDowngrade'),
                'startVideoOnFirst': optGet(options, 'startVideoOnFirst'),
                'target': targetJid,
                'onItemAdvance': rescheduleSafetyForItem,
            }))
            self._calls[call] = None
            releaseReservation()
            scheduleSafety(105000)
            try:
                activeCall = await begin(client, first, videoConfig)
            except Exception:
                clearTimeout(safetyTimer.get('handle'))
                call._finish('failed')
                raise
            activeCall.on('ended', lambda *args: clearTimeout(safetyTimer.get('handle')))
            activeCall.on('error', lambda *args: clearTimeout(safetyTimer.get('handle')))
            await call._attach(activeCall)
            return call
        except Exception:
            cleanupItems()
            raise
        finally:
            releaseReservation()

    async def callMany(self, targets, media, resolution, options=None):
        options = JSObject({}) if options is None else options
        seen = set()
        list_ = []
        for target in (targets if isinstance(targets, list) else [targets]):
            text = '' if target is None else str(target)
            key = re.sub(r'[^0-9]', '', text) or text
            if key in seen:
                continue
            seen.add(key)
            list_.append(target)
        if not list_:
            raise JSError('callMany needs at least one target.')
        await self.listen()
        results = await asyncio.gather(*[self.call(jid, media, resolution, options) for jid in list_], return_exceptions=True)
        settled = []
        for index, result in enumerate(results):
            if isinstance(result, BaseException):
                settled.append(JSObject({'jid': list_[index], 'error': result}))
            else:
                settled.append(JSObject({'jid': list_[index], 'call': result}))
        return settled

    async def listen(self):
        await self._getClient().connect()
        return self

    @property
    def coordinator(self):
        return None if self._client is None else self._client.coordinator

    def on(self, event, listener):
        if self.coordinator is None:
            raise JSError('Call listen() before subscribing to voip events.')
        self.coordinator.on(event, listener)
        return self

    def off(self, event, listener):
        coordinator = self.coordinator
        if coordinator is not None:
            coordinator.off(event, listener)
        return self

    async def acceptCall(self, callId):
        return await self._requireCoordinator().acceptCall(callId)

    async def rejectCall(self, callId, reason=None):
        return await self._requireCoordinator().rejectCall(callId, reason)

    async def hangup(self, callId):
        return await self._requireCoordinator().endCall(callId)

    def _requireCoordinator(self):
        if self.coordinator is None:
            raise JSError('Call listen() first.')
        return self.coordinator

    async def end(self, force=False):
        if force:
            self._calls.clear()
            return None
        await asyncio.gather(*[call.hangup() for call in list(dict.keys(self._calls))])
        return None


default = Voip
