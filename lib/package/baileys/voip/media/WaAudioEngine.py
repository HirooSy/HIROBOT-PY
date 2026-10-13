import asyncio
import math
import os
import subprocess
import threading
from array import array

from baileys.foundation.node_runtime import JSError, JSObject, now, performanceNow, setTimeout, clearTimeout
from baileys.voip.shim.core import createNoopLogger
from baileys.voip.shim.util import toError
from baileys.voip.types import DEFAULT_AUDIO_CONFIG

FFMPEG_BIN = 'ffmpeg'
EXT_FEED_PAUSE_FRACTION = 0.12
EXT_FEED_RESUME_FRACTION = 0.06
MAX_DECODE_BYTES = 128 * 1024 * 1024
MAX_STDERR_CHARS = 16 * 1024
ffmpegProbeCache = {}


def probeBinarySync(bin):
    try:
        result = subprocess.run([bin, '-version'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


async def probeBinary(bin):
    return await asyncio.to_thread(probeBinarySync, bin)


async def hasFfmpeg(bin):
    available = ffmpegProbeCache.get(bin)
    if available is None:
        available = await probeBinary(bin)
        if available:
            ffmpegProbeCache[bin] = available
    return available


def createSelfCorrectingInterval(callback, intervalMs):
    expected = now() + intervalMs
    timer = None
    stopped = False

    def tick():
        nonlocal expected, timer
        if stopped:
            return
        callback()
        expected += intervalMs
        drift = now() - expected
        timer = setTimeout(tick, max(0, intervalMs - drift))

    def clear():
        nonlocal stopped
        stopped = True
        if timer is not None:
            clearTimeout(timer)

    timer = setTimeout(tick, intervalMs)
    return JSObject({'clear': clear})


def fillBuffer(buf, value):
    buf[:] = array(buf.typecode, [value]) * len(buf)


def copyWithin(buf, target, start, end):
    chunk = buf[start:end]
    buf[target:target + len(chunk)] = chunk


def runFfmpegDecode(sampleRate, inputPath):
    try:
        proc = subprocess.Popen([
            FFMPEG_BIN,
            '-hide_banner',
            '-loglevel',
            'error',
            '-i',
            inputPath,
            '-ac',
            '1',
            '-ar',
            str(sampleRate),
            '-acodec',
            'pcm_s16le',
            '-f',
            's16le',
            'pipe:1'
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as err:
        raise JSError(f'ffmpeg not available (install ffmpeg on PATH): {err}')
    stderrBox = ['']

    def pumpStderr():
        while True:
            data = os.read(proc.stderr.fileno(), 4096)
            if not data:
                return
            stderrBox[0] = (stderrBox[0] + data.decode('utf-8', 'replace'))[0:MAX_STDERR_CHARS]

    stderrThread = threading.Thread(target=pumpStderr, daemon=True)
    stderrThread.start()
    chunks = []
    decodedBytes = 0
    while True:
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            break
        decodedBytes += len(chunk)
        if decodedBytes > MAX_DECODE_BYTES:
            proc.kill()
            proc.wait()
            stderrThread.join()
            raise JSError(f'ffmpeg output exceeded {MAX_DECODE_BYTES} bytes: {inputPath}')
        chunks.append(chunk)
    code = proc.wait()
    stderrThread.join()
    if code != 0:
        raise JSError(f'ffmpeg exited with code {code}: {stderrBox[0].strip()}')
    pcmBytes = b''.join(chunks)
    sampleCount = len(pcmBytes) >> 1
    pcm = array('h')
    pcm.frombytes(pcmBytes[0:sampleCount * 2])
    return pcm


class WaAudioEngine:
    def __init__(self, config=None):
        cfg = JSObject(config if config is not None else {})
        c = JSObject({**DEFAULT_AUDIO_CONFIG, **cfg})
        self.logger = cfg.get('logger') if cfg.get('logger') is not None else createNoopLogger()
        self.audioSender = None
        self.audioBuffer = None
        self.audioPosition = 0
        self.audioFinished = False
        self.onAudioFinished = None
        self.playbackInterval = None
        self.captureInterval = None
        self.bufferWritePos = 0
        self.bufferReadPos = 0
        self.bufferLength = 0
        self.sampleRate = c.get('sampleRate')
        self.captureChunkSize = c.get('captureChunkSize')
        self.maxBuffer = c.get('maxBufferSize')
        self.outputSize = c.get('playbackOutputSize')
        self.intervalMs = c.get('intervalMs')
        self.silenceMode = False
        self.lastTickPerf = 0
        self.pacing = JSObject({'ticks': 0, 'late90': 0, 'late200': 0, 'burst20': 0, 'maxGap': 0})
        self.loopDelay = None
        self.externalMode = False
        self.loopMode = False
        self.liveWritePos = 0
        self.extStarted = False
        self.extSkipCount = 0
        self.extDropCount = 0
        self.circularBuffer = array('f', [0.0]) * self.maxBuffer
        self.captureChunkBuffer = array('f', [0.0]) * self.captureChunkSize
        self.silenceChunkBuffer = array('f', [0.0]) * self.captureChunkSize
        self.playbackOutputBuffer = array('f', [0.0]) * self.outputSize
        self.extPreBufferSize = math.floor(self.sampleRate * EXT_FEED_RESUME_FRACTION)
        self.extTargetBuffer = math.floor(self.sampleRate * 0.06)
        self.extHighWater = math.floor(self.sampleRate * 0.2)
        self.extMaxBuffer = math.floor(self.sampleRate * 0.5)

    def setAudioSender(self, sender):
        self.audioSender = sender

    def setLoopMode(self, enabled):
        self.loopMode = enabled

    def setOnAudioFinished(self, callback):
        self.onAudioFinished = callback

    def setExternalMode(self, enabled):
        self.externalMode = enabled
        self.extStarted = False
        self.extSkipCount = 0
        self.extDropCount = 0
        if enabled:
            self.audioBuffer = array('f', [0.0]) * self.extMaxBuffer
            self.audioPosition = 0
            self.liveWritePos = 0
            self.audioFinished = False
        self.logger.get('debug')('external audio mode changed', JSObject({
            'enabled': enabled,
            'preBufferSamples': self.extPreBufferSize
        }))

    def isExternalMode(self):
        return self.externalMode

    def feedExternalAudio(self, data):
        if not self.externalMode or self.audioBuffer is None:
            return 0
        incoming = array('f', data)
        if len(incoming) > self.extMaxBuffer:
            incoming = incoming[len(incoming) - self.extMaxBuffer:]
        if self.liveWritePos + len(incoming) > len(self.audioBuffer):
            unconsumed = self.liveWritePos - self.audioPosition
            if unconsumed > 0 and self.audioPosition > 0:
                copyWithin(self.audioBuffer, 0, self.audioPosition, self.liveWritePos)
            self.liveWritePos = max(0, unconsumed)
            self.audioPosition = 0
        overflow = self.liveWritePos + len(incoming) - self.extMaxBuffer
        if overflow > 0:
            drop = min(overflow, self.liveWritePos)
            if drop > 0:
                copyWithin(self.audioBuffer, 0, drop, self.liveWritePos)
                self.liveWritePos -= drop
            self.extDropCount += 1
            if self.extDropCount <= 5 or self.extDropCount % 100 == 0:
                self.logger.get('debug')('live buffer overflow, dropped oldest', JSObject({
                    'droppedSamples': drop,
                    'dropCount': self.extDropCount
                }))
        self.audioBuffer[self.liveWritePos:self.liveWritePos + len(incoming)] = incoming
        self.liveWritePos += len(incoming)
        return ((self.liveWritePos - self.audioPosition) / self.sampleRate) * 1000

    def getLiveBufferMs(self):
        if not self.externalMode or self.audioBuffer is None:
            return 0
        return ((self.liveWritePos - self.audioPosition) / self.sampleRate) * 1000

    @staticmethod
    def feedWatermarksMs():
        return JSObject({
            'pauseMs': math.floor(EXT_FEED_PAUSE_FRACTION * 1000 + 0.5),
            'resumeMs': math.floor(EXT_FEED_RESUME_FRACTION * 1000 + 0.5)
        })

    def isAudioFinished(self):
        return self.audioFinished

    async def loadAudioFile(self, audioPath):
        self.logger.get('debug')('loading audio file', JSObject({'audioPath': audioPath}))
        if not os.access(audioPath, os.F_OK):
            raise JSError(f'File not found: {audioPath}')
        if not await hasFfmpeg(FFMPEG_BIN):
            raise JSError('ffmpeg not found on PATH (install ffmpeg to load audio files)')
        pcmData = await self.decodeWithFFmpeg(audioPath)
        self.audioBuffer = self.int16ToFloat32(pcmData)
        self.audioPosition = 0
        self.audioFinished = False
        duration = len(self.audioBuffer) / self.sampleRate
        self.logger.get('media')('audio file loaded', JSObject({
            'samples': len(self.audioBuffer),
            'durationSec': duration
        }))

    def int16ToFloat32(self, pcmData):
        return array('f', [pcmData[i] / 32768.0 for i in range(len(pcmData))])

    async def decodeWithFFmpeg(self, inputPath):
        return await asyncio.to_thread(runFfmpegDecode, self.sampleRate, inputPath)

    def generateTestTone(self, frequency=440, duration=3, amplitude=0.3):
        samples = int(self.sampleRate * duration)
        self.audioBuffer = array('f', [0.0]) * samples
        self.audioPosition = 0
        self.audioFinished = False
        for i in range(samples):
            t = i / self.sampleRate
            self.audioBuffer[i] = math.sin(2 * math.pi * frequency * t) * amplitude
        self.logger.get('debug')('test tone generated', JSObject({'samples': samples, 'durationSec': duration}))

    def startPlayback(self):
        if self.playbackInterval is not None:
            return
        self.logger.get('debug')('starting playback')
        self.resetBuffer()
        self.playbackInterval = createSelfCorrectingInterval(lambda: self.readFromBuffer(self.outputSize), self.intervalMs)

    def stopPlayback(self):
        if self.playbackInterval is not None:
            self.playbackInterval.get('clear')()
            self.playbackInterval = None

    def onPlaybackData(self, audioData):
        self.writeToBuffer(audioData)

    def startSilenceCapture(self):
        if self.captureInterval is not None:
            return
        self.silenceMode = True
        self.logger.get('debug')('starting silence capture for pre-accept warmup')

        def tick():
            if self.audioSender is not None:
                try:
                    self.audioSender.sendCapturedAudio(self.silenceChunkBuffer)
                except Exception as err:
                    self.logger.get('trace')('silence send failed', JSObject({'message': toError(err).message}))

        self.captureInterval = createSelfCorrectingInterval(tick, self.intervalMs)

    def startCapture(self):
        if self.captureInterval is not None and self.silenceMode:
            self.captureInterval.get('clear')()
            self.captureInterval = None
        if self.captureInterval is not None:
            return
        self.silenceMode = False
        if self.externalMode:
            self.audioPosition = max(0, self.liveWritePos - self.extPreBufferSize)
            available = self.liveWritePos - self.audioPosition
            self.extStarted = available >= self.extPreBufferSize
            self.logger.get('debug')('starting live capture', JSObject({
                'readPos': self.audioPosition,
                'writePos': self.liveWritePos,
                'runwaySamples': available,
                'started': self.extStarted
            }))
        else:
            self.audioPosition = 0
            if self.audioBuffer is not None:
                durationSec = len(self.audioBuffer) / self.sampleRate
                self.logger.get('debug')('starting capture with loaded audio', JSObject({'durationSec': durationSec}))
            else:
                self.logger.get('debug')('starting capture with silence, no audio loaded')
        frameCount = 0
        self.lastTickPerf = 0

        def tick():
            nonlocal frameCount
            frameCount += 1
            nowPerf = performanceNow()
            if self.lastTickPerf:
                gap = nowPerf - self.lastTickPerf
                self.pacing['ticks'] += 1
                if gap > 90:
                    self.pacing['late90'] += 1
                if gap > 200:
                    self.pacing['late200'] += 1
                if gap < 20:
                    self.pacing['burst20'] += 1
                if gap > self.pacing.get('maxGap'):
                    self.pacing['maxGap'] = gap
            self.lastTickPerf = nowPerf
            chunk = self.getNextChunk()
            if self.audioSender is not None:
                try:
                    self.audioSender.sendCapturedAudio(chunk)
                except Exception as err:
                    self.logger.get('trace')('captured audio send failed', JSObject({
                        'message': toError(err).message
                    }))
            if frameCount % 500 == 0:
                if self.audioBuffer is not None:
                    positionSec = self.audioPosition / self.sampleRate
                    self.logger.get('trace')('capture frame', JSObject({'frameCount': frameCount, 'positionSec': positionSec}))
                else:
                    self.logger.get('trace')('capture frame with silence', JSObject({'frameCount': frameCount}))

        self.captureInterval = createSelfCorrectingInterval(tick, self.intervalMs)

    def takePacingStats(self):
        p = self.pacing
        out = JSObject({**p, 'maxGap': math.floor(p.get('maxGap') + 0.5)})
        self.pacing = JSObject({'ticks': 0, 'late90': 0, 'late200': 0, 'burst20': 0, 'maxGap': 0})
        return out

    def stopCapture(self):
        if self.captureInterval is not None:
            self.captureInterval.get('clear')()
            self.captureInterval = None

    def stop(self):
        self.stopPlayback()
        self.stopCapture()

    def hasAudio(self):
        return self.audioBuffer is not None and len(self.audioBuffer) > 0

    def resetBuffer(self):
        self.bufferWritePos = 0
        self.bufferReadPos = 0
        self.bufferLength = 0

    def writeToBuffer(self, data):
        for i in range(len(data)):
            if not (self.bufferLength < self.maxBuffer):
                break
            self.circularBuffer[self.bufferWritePos] = data[i]
            self.bufferWritePos = (self.bufferWritePos + 1) % self.maxBuffer
            self.bufferLength += 1

    def readFromBuffer(self, count):
        fillBuffer(self.playbackOutputBuffer, 0)
        for i in range(count):
            if self.bufferLength > 0:
                if i < len(self.playbackOutputBuffer):
                    self.playbackOutputBuffer[i] = self.circularBuffer[self.bufferReadPos]
                self.bufferReadPos = (self.bufferReadPos + 1) % self.maxBuffer
                self.bufferLength -= 1
        return self.playbackOutputBuffer

    def getNextChunk(self):
        if self.audioBuffer is None:
            return self.silenceChunkBuffer
        endPos = self.liveWritePos if self.externalMode else len(self.audioBuffer)
        if endPos == 0 or (self.audioFinished and not self.externalMode):
            return self.silenceChunkBuffer
        if self.externalMode:
            available = endPos - self.audioPosition
            if not self.extStarted:
                if available < self.extPreBufferSize:
                    return self.silenceChunkBuffer
                self.extStarted = True
                self.logger.get('debug')('live buffer ready, starting read', JSObject({
                    'availableSamples': available
                }))
            if available > self.extHighWater:
                skipTo = endPos - self.extTargetBuffer
                skipped = skipTo - self.audioPosition
                self.audioPosition = skipTo
                self.extSkipCount += 1
                if self.extSkipCount <= 5:
                    self.logger.get('debug')('live buffer overflow, skipped samples', JSObject({
                        'availableSamples': available,
                        'skippedSamples': skipped,
                        'targetSamples': self.extTargetBuffer,
                        'skipCount': self.extSkipCount
                    }))
            if self.audioPosition >= endPos:
                return self.silenceChunkBuffer
        fillBuffer(self.captureChunkBuffer, 0)
        for i in range(self.captureChunkSize):
            if self.audioPosition >= endPos:
                if self.loopMode and not self.externalMode and endPos > 0:
                    self.audioPosition = 0
                    if self.audioPosition >= endPos:
                        break
                    continue
                if not self.externalMode and not self.audioFinished:
                    self.audioFinished = True
                    self.logger.get('debug')('audio playback finished, sending silence')
                    if self.onAudioFinished is not None:
                        cb = self.onAudioFinished
                        setTimeout(lambda: cb(), 0)
                break
            self.captureChunkBuffer[i] = self.audioBuffer[self.audioPosition]
            self.audioPosition += 1
        return self.captureChunkBuffer
