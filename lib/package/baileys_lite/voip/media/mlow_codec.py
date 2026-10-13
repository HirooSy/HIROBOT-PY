import asyncio
import math
from array import array

from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.voip.shim.core import createNoopLogger
from baileys_lite.voip.shim.util import toError

MLOW_SAMPLE_RATE = 16000
MLOW_CHANNELS = 1
FRAME_SIZE = 960
MAX_FRAME_SIZE = 1920
APPLICATION_VOIP = 2048
SIGNAL_VOICE = 3001
CONCEAL_QUANTUM = MLOW_SAMPLE_RATE // 400
DEFAULT_BITRATE = 15000
DEFAULT_COMPLEXITY = 5
DEFAULT_MAX_CONCEAL_FRAMES = 5
MAX_LOSS_HINT = 5
SPEECH_TOC_MASK = 0xf8
SPEECH_TOC_60MS = 0x50
TINY_FRAME_BYTES = 18
RENEW_MIN_RMS = 120
RENEW_MIN_GAP_FRAMES = 4
SEQ_SPACE = 0x10000
SEQ_HALF = 0x8000

MLOW_ENCODER_CTL = JSObject({
    'SUBFRAME_IMPORTANCE': 4060,
    'USE_SPEECH_ACTIVITY_FLATNESS': 4062,
    'VAD_NON_BINARY': 4066,
    'VAD_HIGHPASS_SHARPNESS': 4068
})

wasmReady = None


async def loadMlowModule():
    global wasmReady
    if wasmReady is None:
        from baileys_lite.voip.media import audio_codec as mod
        try:
            await mod.loadLibopus()
            wasmReady = mod
        except Exception:
            wasmReady = None
            raise
    return wasmReady


def isFiniteNumber(value):
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float)) and math.isfinite(value)


def clampPercent(value):
    if not isFiniteNumber(value):
        return 0
    rounded = math.floor(value + 0.5)
    if rounded < 0:
        return 0
    return 100 if rounded > 100 else rounded


class MLowCodec:
    def __init__(self):
        self.encoder = None
        self.decoder = None
        self.frameSize = FRAME_SIZE
        self.logger = createNoopLogger()
        self.decodeErrors = 0
        self.decodeSuccess = 0
        self.plcFrames = 0
        self.fecFrames = 0
        self.lateFrames = 0
        self.concealCapped = 0
        self.opts = JSObject({})
        self.packetLossPercent = 0
        self.maxConcealFrames = DEFAULT_MAX_CONCEAL_FRAMES
        self.lastSeq = -1
        self.lastDecodedSamples = 0
        self.pcmScratch = array('h', [0]) * FRAME_SIZE
        self.lib = None
        self.encoderOptions = None
        self.spareEncoder = None
        self.spareBuilding = False
        self.generation = 0
        self.frameIndex = 0
        self.lastRenewFrame = -1000
        self.renewCount = 0
        self.silenceScratch = array('f', [0.0]) * MAX_FRAME_SIZE

    @staticmethod
    async def create(opts=None):
        codec = MLowCodec()
        await codec.init(opts)
        return codec

    async def init(self, opts):
        opts = JSObject(opts if opts is not None else {})
        self.opts = opts
        self.logger = opts.get('logger') if opts.get('logger') is not None else createNoopLogger()
        self.packetLossPercent = min(MAX_LOSS_HINT, clampPercent(0 if opts.get('packetLossPercent') is None else opts.get('packetLossPercent')))
        requestedConcealFrames = opts.get('maxConcealFrames')
        self.maxConcealFrames = max(0, math.trunc(requestedConcealFrames if isFiniteNumber(requestedConcealFrames) else DEFAULT_MAX_CONCEAL_FRAMES))
        lib = await loadMlowModule()
        self.lib = lib
        self.decoder = await lib.createDecoder(JSObject({
            'channels': MLOW_CHANNELS,
            'sampleRate': MLOW_SAMPLE_RATE,
            'useSmpl': True,
            'maxFrameSize': MAX_FRAME_SIZE
        }))
        try:
            self.encoderOptions = JSObject({
                'channels': MLOW_CHANNELS,
                'sampleRate': MLOW_SAMPLE_RATE,
                'application': APPLICATION_VOIP,
                'frameSize': FRAME_SIZE,
                'useSmpl': True,
                'dtx': False if opts.get('dtx') is None else opts.get('dtx'),
                'fec': True if opts.get('fec') is None else opts.get('fec'),
                'packetLossPercent': self.packetLossPercent,
                'bitrate': DEFAULT_BITRATE if opts.get('bitrate') is None else opts.get('bitrate'),
                'complexity': DEFAULT_COMPLEXITY if opts.get('complexity') is None else opts.get('complexity'),
                'signal': SIGNAL_VOICE
            })
            self.encoder = await lib.createEncoder(self.encoderOptions)
        except Exception:
            if self.decoder is not None:
                self.decoder.free()
            self.decoder = None
            raise
        if opts.get('tunables') is not None:
            self.applyEncoderTunables(opts.get('tunables'))
        self.frameIndex = 0
        self.lastRenewFrame = -1000
        self.prepareSpare()
        self.logger.get('warn')('[MLOW] encoder ready', JSObject({
            'vadRenew': True,
            'bitrate': self.encoderOptions.get('bitrate'),
            'lossHintCap': MAX_LOSS_HINT
        }))

    def prepareSpare(self):
        if self.spareEncoder is not None or self.spareBuilding or self.lib is None or self.encoderOptions is None:
            return
        generation = self.generation
        self.spareBuilding = True
        options = JSObject({**self.encoderOptions, 'packetLossPercent': self.packetLossPercent})
        asyncio.ensure_future(self.buildSpare(generation, options))

    async def buildSpare(self, generation, options):
        try:
            enc = await self.lib.createEncoder(options)
        except Exception as err:
            self.spareBuilding = False
            self.logger.get('warn')('mlow spare encoder unavailable', JSObject({'message': toError(err).message}))
            return
        self.spareBuilding = False
        if generation != self.generation:
            enc.free()
            return
        tunables = self.opts.get('tunables')
        if tunables is not None:
            self.writeCtlOn(enc, MLOW_ENCODER_CTL['SUBFRAME_IMPORTANCE'], tunables.get('subframeImportance'))
            self.writeCtlOn(enc, MLOW_ENCODER_CTL['USE_SPEECH_ACTIVITY_FLATNESS'], tunables.get('useSpeechActivityFlatness'))
            self.writeCtlOn(enc, MLOW_ENCODER_CTL['VAD_NON_BINARY'], tunables.get('vadNonBinary'))
            self.writeCtlOn(enc, MLOW_ENCODER_CTL['VAD_HIGHPASS_SHARPNESS'], tunables.get('vadHighpassSharpness'))
        self.spareEncoder = enc

    def renewEncoder(self, pcm, original):
        spare = self.spareEncoder
        self.spareEncoder = None
        kept = bytes(original)
        try:
            alt = spare.encode(pcm, JSObject({'frameSize': self.frameSize}))
            if len(alt) > 0 and (alt[0] & SPEECH_TOC_MASK) == SPEECH_TOC_60MS:
                out = bytes(alt)
                old = self.encoder
                self.encoder = spare
                try:
                    spare.setPacketLossPercent(self.packetLossPercent)
                except Exception:
                    pass
                try:
                    old.free()
                except Exception:
                    pass
                self.renewCount += 1
                self.lastRenewFrame = self.frameIndex
                if self.renewCount == 1 or self.renewCount % 20 == 0:
                    self.logger.get('warn')('[MLOW] encoder renewed: frame came out with VAD off', JSObject({
                        'renewCount': self.renewCount,
                        'frame': self.frameIndex,
                        'tocBefore': kept[0],
                        'tocAfter': out[0]
                    }))
                self.prepareSpare()
                return out
            spare.free()
        except Exception as err:
            self.logger.get('warn')('mlow encoder renew failed', JSObject({'message': toError(err).message}))
            try:
                spare.free()
            except Exception:
                pass
        self.prepareSpare()
        return kept

    def encode(self, float32Audio):
        if self.encoder is None:
            raise JSError('[MLowCodec] encoder not initialized')
        if len(float32Audio) != self.frameSize:
            raise JSError(f'[MLowCodec] encode expects {self.frameSize} samples, got {len(float32Audio)}')
        pcm = self.pcmScratch
        sumSq = 0
        for i in range(len(pcm)):
            sample = max(-1, min(1, float32Audio[i]))
            v = math.floor(sample * 32767 + 0.5)
            pcm[i] = v
            sumSq += v * v
        packet = self.encoder.encode(pcm, JSObject({'frameSize': self.frameSize}))
        self.frameIndex += 1
        if (len(packet) > TINY_FRAME_BYTES
                and (packet[0] & SPEECH_TOC_MASK) != SPEECH_TOC_60MS
                and self.spareEncoder is not None
                and self.frameIndex - self.lastRenewFrame >= RENEW_MIN_GAP_FRAMES
                and math.sqrt(sumSq / len(pcm)) >= RENEW_MIN_RMS):
            return self.renewEncoder(pcm, packet)
        return packet

    def decode(self, mlowFrame):
        if self.decoder is None:
            raise JSError('[MLowCodec] decoder not initialized')
        if mlowFrame is None:
            return self.conceal(self.concealFrameSize())
        decoded = self.tryDecode(mlowFrame)
        return decoded if decoded is not None else self.silence(self.concealFrameSize())

    def decodeSequenced(self, seq, packet, onFrame):
        if self.decoder is None:
            raise JSError('[MLowCodec] decoder not initialized')
        if len(packet) == 0:
            return
        current = seq & 0xffff
        if self.lastSeq >= 0:
            delta = (current - self.lastSeq + SEQ_SPACE) % SEQ_SPACE
            if delta == 0 or delta >= SEQ_HALF:
                self.lateFrames += 1
                self.logger.get('trace')('mlow packet arrived out of order', JSObject({
                    'seq': current,
                    'lastSeq': self.lastSeq
                }))
                return
            if delta > 1:
                self.concealGap(delta - 1, packet, onFrame)
        decoded = self.tryDecode(packet)
        if decoded is None:
            self.lastSeq = (current - 1 + SEQ_SPACE) % SEQ_SPACE
            return
        self.lastSeq = current
        onFrame(decoded)

    def resetSequence(self):
        self.lastSeq = -1
        self.lastDecodedSamples = 0

    def setExpectedPacketLossPercent(self, percent):
        nextValue = min(MAX_LOSS_HINT, clampPercent(percent))
        if nextValue == self.packetLossPercent:
            return
        self.packetLossPercent = nextValue
        if self.encoder is None:
            return
        try:
            self.encoder.setPacketLossPercent(nextValue)
        except Exception as err:
            self.logger.get('warn')('mlow packet loss percent rejected', JSObject({
                'percent': nextValue,
                'message': toError(err).message
            }))

    def getExpectedPacketLossPercent(self):
        return self.packetLossPercent

    def applyEncoderTunables(self, tunables):
        if self.encoder is None:
            raise JSError('[MLowCodec] encoder not initialized')
        self.opts = JSObject({**self.opts, 'tunables': tunables})
        self.writeCtl(MLOW_ENCODER_CTL['SUBFRAME_IMPORTANCE'], tunables.get('subframeImportance'))
        self.writeCtl(MLOW_ENCODER_CTL['USE_SPEECH_ACTIVITY_FLATNESS'], tunables.get('useSpeechActivityFlatness'))
        self.writeCtl(MLOW_ENCODER_CTL['VAD_NON_BINARY'], tunables.get('vadNonBinary'))
        self.writeCtl(MLOW_ENCODER_CTL['VAD_HIGHPASS_SHARPNESS'], tunables.get('vadHighpassSharpness'))

    def getStats(self):
        return JSObject({
            'success': self.decodeSuccess,
            'errors': self.decodeErrors,
            'plc': self.plcFrames,
            'fec': self.fecFrames,
            'late': self.lateFrames,
            'concealCapped': self.concealCapped,
            'vadRenew': self.renewCount
        })

    def getFrameSize(self):
        return self.frameSize

    def getFrameDurationMs(self):
        return (self.frameSize / MLOW_SAMPLE_RATE) * 1000

    def getSampleRate(self):
        return MLOW_SAMPLE_RATE

    def getMaxConcealFrames(self):
        return self.maxConcealFrames

    async def reset(self):
        self.destroy()
        await self.init(self.opts)
        self.decodeErrors = 0
        self.decodeSuccess = 0
        self.plcFrames = 0
        self.fecFrames = 0
        self.lateFrames = 0
        self.concealCapped = 0
        self.renewCount = 0
        self.resetSequence()

    def destroy(self):
        if self.frameIndex > 0:
            self.logger.get('warn')('[MLOW] call summary', JSObject({
                'frames': self.frameIndex,
                'renewed': self.renewCount,
                'renewedPct': math.floor((1000 * self.renewCount) / self.frameIndex + 0.5) / 10
            }))
        self.generation += 1
        self.spareBuilding = False
        if self.spareEncoder is not None:
            self.spareEncoder.free()
        self.spareEncoder = None
        if self.encoder is not None:
            self.encoder.free()
        if self.decoder is not None:
            self.decoder.free()
        self.encoder = None
        self.decoder = None

    def concealGap(self, missing, nextPacket, onFrame):
        limit = self.maxConcealFrames
        frames = limit if missing > limit else missing
        if frames < missing:
            self.concealCapped += 1
            self.logger.get('trace')('mlow concealment capped', JSObject({'missing': missing, 'frames': frames}))
        if frames <= 0:
            return
        frameSize = self.concealFrameSize()
        for i in range(frames - 1):
            onFrame(self.conceal(frameSize))
        onFrame(self.recoverPrevious(nextPacket, frameSize))

    def tryDecode(self, mlowFrame):
        if self.decoder is None:
            return None
        try:
            audio = self.decoder.decodeFloat(mlowFrame)
            self.decodeSuccess += 1
            if len(audio) > 0:
                self.lastDecodedSamples = len(audio)
            return audio
        except Exception as err:
            self.decodeErrors += 1
            self.logger.get('trace')('mlow decode failed', JSObject({'message': toError(err).message}))
            return None

    def recoverPrevious(self, nextPacket, frameSize):
        if self.decoder is None:
            return self.silence(frameSize)
        try:
            recovered = self.decoder.decodeFloat(nextPacket, JSObject({
                'decodeFec': True,
                'frameSize': frameSize
            }))
            self.fecFrames += 1
            return recovered
        except Exception as err:
            self.logger.get('trace')('mlow fec decode failed, concealing instead', JSObject({
                'message': toError(err).message
            }))
            return self.conceal(frameSize)

    def conceal(self, frameSize):
        if self.decoder is None:
            return self.silence(frameSize)
        try:
            concealed = self.decoder.decodePacketLossFloat(frameSize)
            self.plcFrames += 1
            return concealed
        except Exception as err:
            self.logger.get('trace')('mlow concealment failed', JSObject({'message': toError(err).message}))
            return self.silence(frameSize)

    def concealFrameSize(self):
        last = self.lastDecodedSamples
        if last <= 0:
            return self.frameSize
        quantized = math.floor(last / CONCEAL_QUANTUM + 0.5) * CONCEAL_QUANTUM
        if quantized < CONCEAL_QUANTUM:
            return CONCEAL_QUANTUM
        return MAX_FRAME_SIZE if quantized > MAX_FRAME_SIZE else quantized

    def silence(self, frameSize):
        size = MAX_FRAME_SIZE if frameSize > MAX_FRAME_SIZE else frameSize
        return self.silenceScratch[0:size]

    def writeCtlOn(self, enc, request, value):
        if value is None or enc is None:
            return
        try:
            enc.encoderCtl(request, value)
        except Exception as err:
            self.logger.get('warn')('mlow spare ctl rejected', JSObject({'request': request, 'message': toError(err).message}))

    def writeCtl(self, request, value):
        if value is None or self.encoder is None:
            return
        try:
            self.encoder.encoderCtl(request, value)
        except Exception as err:
            self.logger.get('warn')('mlow encoder ctl rejected', JSObject({
                'request': request,
                'value': value,
                'message': toError(err).message
            }))
