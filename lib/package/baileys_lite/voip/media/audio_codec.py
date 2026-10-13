import asyncio
import math
from array import array

from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.voip.shim.core import createNoopLogger

Application = JSObject({
    'Voip': 2048,
    'Audio': 2049,
    'RestrictedLowDelay': 2051,
})
Signal = JSObject({
    'Auto': -1000,
    'Voice': 3001,
    'Music': 3002,
})
Bitrate = JSObject({
    'Auto': -1000,
    'Max': -1,
})
Bandwidth = JSObject({
    'Narrowband': 1101,
    'Mediumband': 1102,
    'Wideband': 1103,
    'Superwideband': 1104,
    'Fullband': 1105,
})
EncoderCtl = JSObject({
    'SetApplication': 4000,
    'SetBitrate': 4002,
    'SetMaxBandwidth': 4004,
    'SetVbr': 4006,
    'SetBandwidth': 4008,
    'SetComplexity': 4010,
    'SetInBandFec': 4012,
    'SetPacketLossPercent': 4014,
    'SetDtx': 4016,
    'SetVbrConstraint': 4020,
    'SetForceChannels': 4022,
    'SetSignal': 4024,
    'SetLsbDepth': 4036,
    'SetExpertFrameDuration': 4040,
    'SetPredictionDisabled': 4042,
    'SetPhaseInversionDisabled': 4046,
    'SetUseSmpl': 4050,
    'SetEncHpCutoff': 4052,
    'SetSecondaryComplexity': 4054,
    'SetSecondaryBitrate': 4056,
    'SetSubframeImportance': 4060,
    'SetSpeechActivityFlatten': 4062,
    'SetVadNoiseLevelUpdateSpeed': 4064,
    'SetVadNonBinary': 4066,
    'SetVadHighpassSharpness': 4068,
    'SetFecRateCompensation': 4070,
})
DecoderCtl = JSObject({
    'SetGain': 4034,
    'SetPhaseInversionDisabled': 4046,
    'SetUseLpcPostfilter': 4058,
    'SetUseSmpl': 4050,
})
DEFAULT_CHANNELS = 2
DEFAULT_FRAME_DURATION_MS = 20
MAX_PACKET_DURATION_MS = 120
DEFAULT_MAX_PACKET_BYTES = 4000
DEFAULT_SAMPLE_RATE = 48000
DECODER_INTEGER_CTL_REQUESTS = frozenset(dict.values(DecoderCtl))
ENCODER_INTEGER_CTL_REQUESTS = frozenset(dict.values(EncoderCtl))
ENCODE_FRAME_DURATIONS_MS = [2.5, 5, 10, 20, 40, 60]
VALID_SAMPLE_RATES = [8000, 12000, 16000, 24000, 48000]
modulePromise = None
smplGlobalsReady = False


class RangeError(JSError):
    def __init__(self, message=''):
        super().__init__(message)
        self.name = 'RangeError'


class OpusError(JSError):
    def __init__(self, code, message, operation):
        super().__init__(message)
        self.name = 'OpusError'
        self.code = code
        self.codeName = resolveOpusErrorCodeName(code)
        self.operation = operation


OpusErrorCode = JSObject({
    'BadArg': -1,
    'BufferTooSmall': -2,
    'InternalError': -3,
    'InvalidPacket': -4,
    'Unimplemented': -5,
    'InvalidState': -6,
    'AllocFail': -7,
})


def isJsInteger(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def nullish(value, fallback):
    return fallback if value is None else value


def isOpusError(error):
    if isinstance(error, OpusError):
        return True
    return (
        error is not None
        and getattr(error, 'name', None) == 'OpusError'
        and isinstance(getattr(error, 'message', None), str)
        and isJsInteger(getattr(error, 'code', None))
        and (isinstance(getattr(error, 'codeName', None), str) or getattr(error, 'codeName', None) is None)
        and (isinstance(getattr(error, 'operation', None), str) or getattr(error, 'operation', None) is None)
    )


async def createAudioModule():
    raise JSError('libopus WASM core is not available in the pure-Python port')


async def getModule():
    global modulePromise
    if modulePromise is None:
        modulePromise = await createAudioModule()
    return modulePromise


async def opusGlobalCreate():
    module = await getModule()
    module._oc_global_create()
    global smplGlobalsReady
    smplGlobalsReady = True


async def opusGlobalFree():
    module = await getModule()
    module._oc_global_free()
    global smplGlobalsReady
    smplGlobalsReady = False


async def ensureSmplGlobals():
    if smplGlobalsReady:
        return
    module = await getModule()
    ensureSmplGlobalsSync(module)


def ensureSmplGlobalsSync(module):
    global smplGlobalsReady
    if smplGlobalsReady:
        return
    module._oc_global_create()
    smplGlobalsReady = True


async def loadLibopus():
    module = await getModule()
    return JSObject({'version': module.UTF8ToString(module._oc_get_version_string())})


async def createEncoder(options=None):
    normalized = normalizeEncoderOptions(JSObject(nullish(options, {})))
    if normalized.get('useSmpl'):
        await ensureSmplGlobals()
    module = await getModule()
    return WasmOpusEncoder(module, normalized)


async def createDecoder(options=None):
    normalized = normalizeDecoderOptions(JSObject(nullish(options, {})))
    if normalized.get('useSmpl'):
        await ensureSmplGlobals()
    module = await getModule()
    return WasmOpusDecoder(module, normalized)


def resolveOpusErrorCodeName(code):
    for name, value in dict.items(OpusErrorCode):
        if value == code:
            return name
    return None


def createOpusError(module, code, operation):
    message = module.UTF8ToString(module._oc_strerror(code))
    return OpusError(code, f'opus codec {operation} failed ({code}): {message}', operation)


def toUint8Array(input):
    return bytes(memoryview(input).cast('B'))


def checkedMalloc(module, bytesCount):
    ptr = module._malloc(bytesCount)
    if ptr == 0:
        raise JSError(f'WASM malloc failed for {bytesCount} bytes')
    return ptr


def samplesForDuration(sampleRate, durationMs):
    samples = (sampleRate / 1000) * durationMs
    if samples == int(samples):
        return int(samples)
    return samples


def normalizeBitrate(bitrate):
    if bitrate == 'auto':
        return Bitrate['Auto']
    if bitrate == 'max':
        return Bitrate['Max']
    if bitrate == Bitrate['Auto'] or bitrate == Bitrate['Max']:
        return bitrate
    validatePositiveInteger(bitrate, 'bitrate')
    return bitrate


def validateCodecOptions(options):
    if options.get('sampleRate') not in VALID_SAMPLE_RATES:
        raise RangeError('sampleRate must be 8000, 12000, 16000, 24000, or 48000')
    if options.get('channels') != 1 and options.get('channels') != 2:
        raise RangeError('channels must be 1 or 2')


def validateBandwidth(bandwidth, name):
    if bandwidth not in list(dict.values(Bandwidth)):
        raise RangeError(f'{name} must be Bandwidth.Narrowband, Bandwidth.Mediumband, Bandwidth.Wideband, Bandwidth.Superwideband, or Bandwidth.Fullband')


def validateFrameSizeForDurations(frameSize, sampleRate, name, durationsMs):
    validFrameSizes = [samplesForDuration(sampleRate, durationMs) for durationMs in durationsMs]
    if not isJsInteger(frameSize) or frameSize not in validFrameSizes:
        raise RangeError(f"{name} must be one of {', '.join(str(v) for v in validFrameSizes)} samples at {sampleRate} Hz")


def validateEncodeFrameSize(frameSize, sampleRate, name):
    validateFrameSizeForDurations(frameSize, sampleRate, name, ENCODE_FRAME_DURATIONS_MS)


def validateDecodeCapacity(frameSize, sampleRate, name):
    maxFrameSize = samplesForDuration(sampleRate, MAX_PACKET_DURATION_MS)
    if not isJsInteger(frameSize) or frameSize <= 0 or frameSize > maxFrameSize:
        raise RangeError(f'{name} must be an integer from 1 to {maxFrameSize} samples at {sampleRate} Hz')


def validatePlcFrameSize(frameSize, sampleRate, name):
    minFrameSize = samplesForDuration(sampleRate, 2.5)
    maxFrameSize = samplesForDuration(sampleRate, MAX_PACKET_DURATION_MS)
    if not isJsInteger(frameSize) or frameSize < minFrameSize or frameSize > maxFrameSize or frameSize % minFrameSize != 0:
        raise RangeError(f'{name} must be a multiple of {minFrameSize} samples from {minFrameSize} to {maxFrameSize} at {sampleRate} Hz')


def validateInteger(value, name):
    if not isJsInteger(value):
        raise RangeError(f'{name} must be an integer')


def validatePositiveInteger(value, name):
    if not isJsInteger(value) or value <= 0:
        raise RangeError(f'{name} must be a positive integer')


def validateIntegerRange(value, minValue, maxValue, name):
    if not isJsInteger(value) or value < minValue or value > maxValue:
        raise RangeError(f'{name} must be an integer from {minValue} to {maxValue}')


def normalizeEncoderOptions(options):
    sampleRate = nullish(options.get('sampleRate'), DEFAULT_SAMPLE_RATE)
    channels = nullish(options.get('channels'), DEFAULT_CHANNELS)
    validateCodecOptions(JSObject({'channels': channels, 'sampleRate': sampleRate}))
    frameSize = nullish(options.get('frameSize'), samplesForDuration(sampleRate, DEFAULT_FRAME_DURATION_MS))
    validateEncodeFrameSize(frameSize, sampleRate, 'frameSize')
    if options.get('maxBandwidth') is not None:
        validateBandwidth(options.get('maxBandwidth'), 'maxBandwidth')
    return JSObject({
        'application': nullish(options.get('application'), Application['Audio']),
        'bitrate': normalizeBitrate(nullish(options.get('bitrate'), 64000)),
        'channels': channels,
        'complexity': nullish(options.get('complexity'), 10),
        'dtx': nullish(options.get('dtx'), False),
        'fec': nullish(options.get('fec'), False),
        'frameSize': frameSize,
        'maxBandwidth': options.get('maxBandwidth'),
        'packetLossPercent': nullish(options.get('packetLossPercent'), 0),
        'sampleRate': sampleRate,
        'signal': nullish(options.get('signal'), Signal['Auto']),
        'useSmpl': options.get('useSmpl') is True,
        'vbr': options.get('vbr'),
        'vbrConstraint': options.get('vbrConstraint'),
    })


def normalizeDecoderOptions(options):
    sampleRate = nullish(options.get('sampleRate'), DEFAULT_SAMPLE_RATE)
    channels = nullish(options.get('channels'), DEFAULT_CHANNELS)
    validateCodecOptions(JSObject({'channels': channels, 'sampleRate': sampleRate}))
    maxFrameSize = nullish(options.get('maxFrameSize'), samplesForDuration(sampleRate, MAX_PACKET_DURATION_MS))
    validateDecodeCapacity(maxFrameSize, sampleRate, 'maxFrameSize')
    return JSObject({
        'channels': channels,
        'maxFrameSize': maxFrameSize,
        'sampleRate': sampleRate,
        'useLpcPostfilter': options.get('useLpcPostfilter'),
        'useSmpl': options.get('useSmpl') is True,
    })


class WasmOpusEncoder:
    def __init__(self, module, options):
        self.module = module
        self.application = options.get('application')
        self.channels = options.get('channels')
        self.frameSize = options.get('frameSize')
        self.sampleRate = options.get('sampleRate')
        self.freed = False
        self.packetBytes = 0
        self.packetPtr = 0
        self.pcmBytes = 0
        self.pcmPtr = 0
        self.ptr = None
        errorPtr = module._malloc(4)
        try:
            ptr = module._oc_create_encoder(options.get('sampleRate'), options.get('channels'), options.get('application'), errorPtr)
            error = nullish(module.HEAP32[errorPtr >> 2], 0)
            if not ptr or error != 0:
                raise createOpusError(module, error, 'createEncoder')
            self.ptr = ptr
        finally:
            module._free(errorPtr)
        self.setBitrate(options.get('bitrate'))
        self.setComplexity(options.get('complexity'))
        self.setDtx(options.get('dtx'))
        self.setFec(options.get('fec'))
        if options.get('maxBandwidth') is not None:
            self.setMaxBandwidth(options.get('maxBandwidth'))
        self.setPacketLossPercent(options.get('packetLossPercent'))
        self.setSignal(options.get('signal'))
        if options.get('vbr') is not None:
            self.setVbr(options.get('vbr'))
        if options.get('vbrConstraint') is not None:
            self.setVbrConstraint(options.get('vbrConstraint'))
        if options.get('useSmpl'):
            self.encoderCtl(EncoderCtl['SetLsbDepth'], 16)
            self.encoderCtl(EncoderCtl['SetUseSmpl'], 1)

    def encode(self, pcm, options=None):
        opts = JSObject(nullish(options, {}))
        self.assertLive()
        frameSize = nullish(opts.get('frameSize'), self.frameSize)
        validateEncodeFrameSize(frameSize, self.sampleRate, 'frameSize')
        pcmBytes = toUint8Array(pcm)
        expectedBytes = frameSize * self.channels * 2
        if len(pcmBytes) != expectedBytes:
            raise RangeError(f'PCM frame has {len(pcmBytes)} bytes; expected {expectedBytes} for {frameSize} samples and {self.channels} channel(s)')
        maxPacketBytes = nullish(opts.get('maxPacketBytes'), DEFAULT_MAX_PACKET_BYTES)
        validatePositiveInteger(maxPacketBytes, 'maxPacketBytes')
        pcmPtr = self.ensurePcmBytes(len(pcmBytes))
        packetPtr = self.ensurePacketBytes(maxPacketBytes)
        self.module.HEAPU8[pcmPtr:pcmPtr + len(pcmBytes)] = pcmBytes
        encodedBytes = self.module._oc_encode(self.ptr, pcmPtr, frameSize, packetPtr, maxPacketBytes)
        if encodedBytes < 0:
            raise createOpusError(self.module, encodedBytes, 'encode')
        return bytes(self.module.HEAPU8[packetPtr:packetPtr + encodedBytes])

    def encodeFloat(self, pcm, options=None):
        opts = JSObject(nullish(options, {}))
        self.assertLive()
        frameSize = nullish(opts.get('frameSize'), self.frameSize)
        validateEncodeFrameSize(frameSize, self.sampleRate, 'frameSize')
        expectedSamples = frameSize * self.channels
        if len(pcm) != expectedSamples:
            raise RangeError(f'Float32 PCM frame has {len(pcm)} samples; expected {expectedSamples} for {frameSize} samples and {self.channels} channel(s)')
        maxPacketBytes = nullish(opts.get('maxPacketBytes'), DEFAULT_MAX_PACKET_BYTES)
        validatePositiveInteger(maxPacketBytes, 'maxPacketBytes')
        pcmByteLength = len(toUint8Array(pcm))
        pcmPtr = self.ensurePcmBytes(pcmByteLength)
        packetPtr = self.ensurePacketBytes(maxPacketBytes)
        self.module.HEAPF32[pcmPtr >> 2:(pcmPtr >> 2) + len(pcm)] = pcm
        encodedBytes = self.module._oc_encode_float(self.ptr, pcmPtr, frameSize, packetPtr, maxPacketBytes)
        if encodedBytes < 0:
            raise createOpusError(self.module, encodedBytes, 'encodeFloat')
        return bytes(self.module.HEAPU8[packetPtr:packetPtr + encodedBytes])

    def encodeFrames(self, frames, options=None):
        return [self.encode(frame, options) for frame in frames]

    def encodeFloatFrames(self, frames, options=None):
        return [self.encodeFloat(frame, options) for frame in frames]

    def encoderCtl(self, request, value):
        self.assertLive()
        validateInteger(request, 'request')
        validateInteger(value, 'value')
        if request not in ENCODER_INTEGER_CTL_REQUESTS:
            raise RangeError('encoderCtl only supports integer setter requests')
        if request == EncoderCtl['SetUseSmpl'] and value != 0:
            ensureSmplGlobalsSync(self.module)
        self.check(self.module._oc_encoder_ctl(self.ptr, request, value), 'encoderCtl')

    def setBitrate(self, bitrate):
        self.encoderCtl(EncoderCtl['SetBitrate'], normalizeBitrate(bitrate))

    def getBitrate(self):
        self.assertLive()
        bitrate = self.module._oc_encoder_ctl_get_bitrate(self.ptr)
        if bitrate < 0:
            raise createOpusError(self.module, bitrate, 'getBitrate')
        return bitrate

    def getLookahead(self):
        self.assertLive()
        lookahead = self.module._oc_encoder_ctl_get_lookahead(self.ptr)
        if lookahead < 0:
            raise createOpusError(self.module, lookahead, 'getLookahead')
        return lookahead

    def getInDtx(self):
        self.assertLive()
        inDtx = self.module._oc_encoder_ctl_get_in_dtx(self.ptr)
        if inDtx < 0:
            raise createOpusError(self.module, inDtx, 'getInDtx')
        return inDtx != 0

    def setComplexity(self, complexity):
        validateIntegerRange(complexity, 0, 10, 'complexity')
        self.encoderCtl(EncoderCtl['SetComplexity'], complexity)

    def setDtx(self, enabled):
        self.encoderCtl(EncoderCtl['SetDtx'], 1 if enabled else 0)

    def setFec(self, enabled):
        self.encoderCtl(EncoderCtl['SetInBandFec'], 1 if enabled else 0)

    def setMaxBandwidth(self, bandwidth):
        validateBandwidth(bandwidth, 'maxBandwidth')
        self.encoderCtl(EncoderCtl['SetMaxBandwidth'], bandwidth)

    def setPacketLossPercent(self, percentage):
        validateIntegerRange(percentage, 0, 100, 'packetLossPercent')
        self.encoderCtl(EncoderCtl['SetPacketLossPercent'], percentage)

    def setSignal(self, signal):
        if signal not in list(dict.values(Signal)):
            raise RangeError('signal must be Signal.Auto, Signal.Voice, or Signal.Music')
        self.encoderCtl(EncoderCtl['SetSignal'], signal)

    def setVbr(self, enabled):
        self.encoderCtl(EncoderCtl['SetVbr'], 1 if enabled else 0)

    def setVbrConstraint(self, enabled):
        self.encoderCtl(EncoderCtl['SetVbrConstraint'], 1 if enabled else 0)

    def free(self):
        if self.freed:
            return
        self.freeScratch()
        self.module._oc_destroy_encoder(self.ptr)
        self.freed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.free()

    def assertLive(self):
        if self.freed:
            raise JSError('OpusEncoder has been freed')

    def check(self, code, operation):
        if code < 0:
            raise createOpusError(self.module, code, operation)

    def ensurePacketBytes(self, requiredBytes):
        if self.packetPtr != 0 and self.packetBytes >= requiredBytes:
            return self.packetPtr
        nextPtr = checkedMalloc(self.module, requiredBytes)
        if self.packetPtr != 0:
            self.module._free(self.packetPtr)
        self.packetPtr = nextPtr
        self.packetBytes = requiredBytes
        return self.packetPtr

    def ensurePcmBytes(self, requiredBytes):
        if self.pcmPtr != 0 and self.pcmBytes >= requiredBytes:
            return self.pcmPtr
        nextPtr = checkedMalloc(self.module, requiredBytes)
        if self.pcmPtr != 0:
            self.module._free(self.pcmPtr)
        self.pcmPtr = nextPtr
        self.pcmBytes = requiredBytes
        return self.pcmPtr

    def freeScratch(self):
        if self.packetPtr != 0:
            self.module._free(self.packetPtr)
        if self.pcmPtr != 0:
            self.module._free(self.pcmPtr)
        self.packetPtr = 0
        self.packetBytes = 0
        self.pcmPtr = 0
        self.pcmBytes = 0


class WasmOpusDecoder:
    def __init__(self, module, options):
        self.module = module
        self.channels = options.get('channels')
        self.maxFrameSize = options.get('maxFrameSize')
        self.sampleRate = options.get('sampleRate')
        self.freed = False
        self.packetBytes = 0
        self.packetPtr = 0
        self.pcmBytes = 0
        self.pcmPtr = 0
        self.ptr = None
        errorPtr = module._malloc(4)
        try:
            ptr = module._oc_create_decoder(options.get('sampleRate'), options.get('channels'), errorPtr)
            error = nullish(module.HEAP32[errorPtr >> 2], 0)
            if not ptr or error != 0:
                raise createOpusError(module, error, 'createDecoder')
            self.ptr = ptr
        finally:
            module._free(errorPtr)
        if options.get('useLpcPostfilter') is not None:
            self.decoderCtl(DecoderCtl['SetUseLpcPostfilter'], 1 if options.get('useLpcPostfilter') else 0)
        if options.get('useSmpl'):
            self.decoderCtl(DecoderCtl['SetUseSmpl'], 1)

    def decode(self, packet, options=None):
        opts = JSObject(nullish(options, {}))
        self.assertLive()
        frameSize = self.resolveDecodeFrameSize(packet, opts)
        pcmBytes = frameSize * self.channels * 2
        pcmPtr = self.ensurePcmBytes(pcmBytes)
        copied = self.copyPacket(packet, opts.get('decodeFec'))
        decodedSamples = self.module._oc_decode(self.ptr, copied.get('packetPtr'), copied.get('packetLength'), pcmPtr, frameSize, 1 if opts.get('decodeFec') else 0)
        if decodedSamples < 0:
            raise createOpusError(self.module, decodedSamples, 'decodePacketLoss' if packet is None else 'decode')
        sampleCount = decodedSamples * self.channels
        return self.module.HEAP16[pcmPtr >> 1:(pcmPtr >> 1) + sampleCount]

    def decodeFloat(self, packet, options=None):
        opts = JSObject(nullish(options, {}))
        self.assertLive()
        frameSize = self.resolveDecodeFrameSize(packet, opts)
        pcmBytes = frameSize * self.channels * 4
        pcmPtr = self.ensurePcmBytes(pcmBytes)
        copied = self.copyPacket(packet, opts.get('decodeFec'))
        decodedSamples = self.module._oc_decode_float(self.ptr, copied.get('packetPtr'), copied.get('packetLength'), pcmPtr, frameSize, 1 if opts.get('decodeFec') else 0)
        if decodedSamples < 0:
            raise createOpusError(self.module, decodedSamples, 'decodePacketLossFloat' if packet is None else 'decodeFloat')
        sampleCount = decodedSamples * self.channels
        return self.module.HEAPF32[pcmPtr >> 2:(pcmPtr >> 2) + sampleCount]

    def decodeFrames(self, packets, options=None):
        return [self.decode(packet, options) for packet in packets]

    def decodeFloatFrames(self, packets, options=None):
        return [self.decodeFloat(packet, options) for packet in packets]

    def decodePacketLoss(self, frameSize=None):
        if frameSize is None:
            frameSize = samplesForDuration(self.sampleRate, DEFAULT_FRAME_DURATION_MS)
        return self.decode(None, JSObject({'frameSize': frameSize}))

    def decodePacketLossFloat(self, frameSize=None):
        if frameSize is None:
            frameSize = samplesForDuration(self.sampleRate, DEFAULT_FRAME_DURATION_MS)
        return self.decodeFloat(None, JSObject({'frameSize': frameSize}))

    def decoderCtl(self, request, value):
        self.assertLive()
        validateInteger(request, 'request')
        validateInteger(value, 'value')
        if request not in DECODER_INTEGER_CTL_REQUESTS:
            raise RangeError('decoderCtl only supports integer setter requests')
        if request == DecoderCtl['SetUseSmpl'] and value != 0:
            ensureSmplGlobalsSync(self.module)
        code = self.module._oc_decoder_ctl(self.ptr, request, value)
        if code < 0:
            raise createOpusError(self.module, code, 'decoderCtl')

    def free(self):
        if self.freed:
            return
        self.freeScratch()
        self.module._oc_destroy_decoder(self.ptr)
        self.freed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.free()

    def assertLive(self):
        if self.freed:
            raise JSError('OpusDecoder has been freed')

    def copyPacket(self, packet, decodeFec):
        if packet is None:
            if decodeFec:
                raise RangeError('decodeFec requires a packet')
            return JSObject({'packetLength': 0, 'packetPtr': 0})
        if len(packet) == 0:
            raise RangeError('packet must not be empty; use null or decodePacketLoss() for PLC')
        packetPtr = self.ensurePacketBytes(len(packet))
        self.module.HEAPU8[packetPtr:packetPtr + len(packet)] = packet
        return JSObject({'packetLength': len(packet), 'packetPtr': packetPtr})

    def ensurePacketBytes(self, requiredBytes):
        if self.packetPtr != 0 and self.packetBytes >= requiredBytes:
            return self.packetPtr
        nextPtr = checkedMalloc(self.module, requiredBytes)
        if self.packetPtr != 0:
            self.module._free(self.packetPtr)
        self.packetPtr = nextPtr
        self.packetBytes = requiredBytes
        return self.packetPtr

    def ensurePcmBytes(self, requiredBytes):
        if self.pcmPtr != 0 and self.pcmBytes >= requiredBytes:
            return self.pcmPtr
        nextPtr = checkedMalloc(self.module, requiredBytes)
        if self.pcmPtr != 0:
            self.module._free(self.pcmPtr)
        self.pcmPtr = nextPtr
        self.pcmBytes = requiredBytes
        return self.pcmPtr

    def freeScratch(self):
        if self.packetPtr != 0:
            self.module._free(self.packetPtr)
        if self.pcmPtr != 0:
            self.module._free(self.pcmPtr)
        self.packetPtr = 0
        self.packetBytes = 0
        self.pcmPtr = 0
        self.pcmBytes = 0

    def resolveDecodeFrameSize(self, packet, options):
        if packet is None or options.get('decodeFec'):
            frameSize = nullish(options.get('frameSize'), nullish(options.get('maxFrameSize'), samplesForDuration(self.sampleRate, DEFAULT_FRAME_DURATION_MS)))
        else:
            frameSize = nullish(options.get('maxFrameSize'), self.maxFrameSize)
        if packet is None or options.get('decodeFec'):
            validatePlcFrameSize(frameSize, self.sampleRate, 'frameSize')
            return frameSize
        validateDecodeCapacity(frameSize, self.sampleRate, 'maxFrameSize')
        return frameSize


CODEC_SAMPLE_RATE = 16000
CODEC_CHANNELS = 1
FRAME_SIZE = 960
MAX_FRAME_SIZE = 1920
APPLICATION_VOIP = 2048
SIGNAL_VOICE = 3001
wasmReady = None


async def loadCodecModule():
    global wasmReady
    if wasmReady is None:
        try:
            await loadLibopus()
            wasmReady = JSObject({'createDecoder': createDecoder, 'createEncoder': createEncoder, 'loadLibopus': loadLibopus})
        except Exception:
            wasmReady = None
            raise
    return wasmReady


class AudioCodec:
    def __init__(self):
        self.encoder = None
        self.decoder = None
        self.frameSize = FRAME_SIZE
        self.decodeErrors = 0
        self.decodeSuccess = 0
        self.plcFrames = 0
        self.opts = JSObject({})

    @staticmethod
    async def create(opts=None):
        codec = AudioCodec()
        await codec.init(opts)
        return codec

    async def init(self, opts):
        opts = JSObject(nullish(opts, {}))
        self.opts = opts
        lib = await loadCodecModule()
        self.decoder = await lib.get('createDecoder')(JSObject({
            'channels': CODEC_CHANNELS,
            'sampleRate': CODEC_SAMPLE_RATE,
            'useSmpl': True,
            'maxFrameSize': MAX_FRAME_SIZE
        }))
        try:
            self.encoder = await lib.get('createEncoder')(JSObject({
                'channels': CODEC_CHANNELS,
                'sampleRate': CODEC_SAMPLE_RATE,
                'application': APPLICATION_VOIP,
                'frameSize': FRAME_SIZE,
                'useSmpl': True,
                'dtx': nullish(opts.get('dtx'), False),
                'fec': nullish(opts.get('fec'), False),
                'bitrate': nullish(opts.get('bitrate'), 25000),
                'complexity': nullish(opts.get('complexity'), 9),
                'signal': SIGNAL_VOICE
            }))
        except Exception:
            if self.decoder is not None:
                self.decoder.free()
            self.decoder = None
            raise

    def encode(self, float32Audio):
        if self.encoder is None:
            raise JSError('[AudioCodec] encoder not initialized')
        pcm = array('h', [0]) * len(float32Audio)
        for i in range(len(float32Audio)):
            sample = max(-1, min(1, float32Audio[i]))
            pcm[i] = math.floor(sample * 32767 + 0.5)
        return self.encoder.encode(pcm, JSObject({'frameSize': self.frameSize}))

    def decode(self, codecFrame):
        if self.decoder is None:
            raise JSError('[AudioCodec] decoder not initialized')
        if codecFrame is None:
            self.plcFrames += 1
            return self.decoder.decodePacketLossFloat(self.frameSize)
        try:
            audio = self.decoder.decodeFloat(codecFrame, JSObject({'frameSize': self.frameSize}))
            self.decodeSuccess += 1
            return audio
        except Exception:
            self.decodeErrors += 1
            return self.silence()

    def silence(self):
        return array('f', [0.0]) * self.frameSize

    def getStats(self):
        return JSObject({
            'success': self.decodeSuccess,
            'errors': self.decodeErrors,
            'plc': self.plcFrames
        })

    def getFrameSize(self):
        return self.frameSize

    def getFrameDurationMs(self):
        return (self.frameSize / CODEC_SAMPLE_RATE) * 1000

    def getSampleRate(self):
        return CODEC_SAMPLE_RATE

    async def reset(self):
        self.destroy()
        await self.init(self.opts)
        self.decodeErrors = 0
        self.decodeSuccess = 0
        self.plcFrames = 0

    def destroy(self):
        if self.encoder is not None:
            self.encoder.free()
        if self.decoder is not None:
            self.decoder.free()
        self.encoder = None
        self.decoder = None
