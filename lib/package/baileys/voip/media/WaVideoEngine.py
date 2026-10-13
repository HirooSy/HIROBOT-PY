import asyncio
import math
import os
import subprocess
import threading

from baileys.foundation.node_runtime import JSError, JSObject, now
from baileys.voip.shim.core import createNoopLogger
from baileys.voip.shim.util import toError
from baileys.voip.types import DEFAULT_VIDEO_CONFIG
from baileys.voip.media.h264 import auHasIDR

FFMPEG_BIN = 'ffmpeg'
MAX_STDERR_CHARS = 16 * 1024
MAX_PENDING_BYTES = 8 * 1024 * 1024
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


def startCodeLen(data, offset):
    if offset + 3 < len(data) and data[offset] == 0 and data[offset + 1] == 0 and data[offset + 2] == 0 and data[offset + 3] == 1:
        return 4
    if offset + 2 < len(data) and data[offset] == 0 and data[offset + 1] == 0 and data[offset + 2] == 1:
        return 3
    return 0


def pickH264Level(width, height, frameRate):
    mbs = math.ceil(width / 16) * math.ceil(height / 16)
    mbps = mbs * frameRate
    if mbs <= 1620 and mbps <= 40500:
        return '3.0'
    if mbs <= 3600 and mbps <= 108000:
        return '3.1'
    if mbs <= 5120 and mbps <= 216000:
        return '3.2'
    return '4.0'


def align16(n):
    return max(16, math.ceil(n / 16) * 16)


class WaVideoEngine:
    def __init__(self, config=None):
        cfg = JSObject(config if config is not None else {})
        c = JSObject({**DEFAULT_VIDEO_CONFIG, **cfg})
        self.logger = cfg.get('logger') if cfg.get('logger') is not None else createNoopLogger()
        self.videoSender = None
        self.proc = None
        self.width = c.get('width')
        self.height = c.get('height')
        self.frameRate = c.get('frameRate')
        self.frameDurationMs = 1000 / self.frameRate
        self.running = False
        self.pending = b''
        self.accessUnitsSent = 0
        self.videoPath = None
        self.waitingForKeyFrame = True
        self.sourceKind = None
        self._deliberateStop = False
        self._restartWindowStartedAt = None
        self._restartCount = None

    def setVideoSender(self, sender):
        self.videoSender = sender

    def isRunning(self):
        return self.running

    def hasSource(self):
        return self.sourceKind is not None

    async def loadVideoFile(self, videoPath):
        self.logger.get('debug')('loading video file', JSObject({'videoPath': videoPath}))
        if not os.access(videoPath, os.F_OK):
            raise JSError(f'File not found: {videoPath}')
        if not await hasFfmpeg(FFMPEG_BIN):
            raise JSError('ffmpeg not found on PATH (install ffmpeg to load video files)')
        self.videoPath = videoPath
        self.sourceKind = 'file'

    async def loadBlankSource(self):
        if not await hasFfmpeg(FFMPEG_BIN):
            raise JSError('ffmpeg not found on PATH (install ffmpeg for the black-screen video fallback)')
        self.sourceKind = 'blank'

    def start(self):
        if not self.sourceKind or self.proc is not None:
            return
        outW = align16(self.width)
        outH = align16(self.height)
        scaleFilter = f'scale={outW}:{outH}:force_original_aspect_ratio=decrease,pad={outW}:{outH}:(ow-iw)/2:(oh-ih)/2,setsar=1'

        keyframeIntervalFrames = max(1, math.floor(self.frameRate + 0.5))
        kbpsCap = 600
        targetBitrateKbps = max(200, min(kbpsCap, math.floor((outW * outH * self.frameRate) / 1000 * 0.08 + 0.5)))
        bufsizeKbps = max(60, math.floor(targetBitrateKbps * 0.25 + 0.5))
        level = pickH264Level(outW, outH, self.frameRate)
        if self.sourceKind == 'blank':
            inputArgs = ['-f', 'lavfi', '-re', '-i', f'color=c=black:s={outW}x{outH}:r={self.frameRate}']
        else:
            inputArgs = ['-stream_loop', '-1', '-re', '-i', self.videoPath]

        args = [
            FFMPEG_BIN,
            '-hide_banner', '-loglevel', 'error',
            *inputArgs,
            '-an',
            '-vf', scaleFilter,
            '-r', str(self.frameRate),
            '-c:v', 'libx264',
            '-threads', '1',
            '-profile:v', 'baseline',
            '-level', level,
            '-preset', 'veryfast',
            '-tune', 'zerolatency',
            '-pix_fmt', 'yuv420p',
            '-bf', '0',
            '-b:v', f'{targetBitrateKbps}k',
            '-maxrate', f'{targetBitrateKbps}k',
            '-bufsize', f'{bufsizeKbps}k',
            '-x264-params', f'aud=1:repeat-headers=1:keyint={keyframeIntervalFrames}:min-keyint={keyframeIntervalFrames}:scenecut=0:rc-lookahead=0:sync-lookahead=0:open-gop=0:ref=1',
            '-f', 'h264',
            'pipe:1'
        ]
        try:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as err:
            self.logger.get('error')('ffmpeg video process error', JSObject({'message': str(err)}))
            self.running = False
            return
        self.proc = proc
        self.pending = b''
        self.accessUnitsSent = 0
        self.waitingForKeyFrame = True
        self.running = True
        self._deliberateStop = False
        self.logger.get('warn')('[DIAG] ffmpeg video process started', JSObject({
            'pid': proc.pid,
            'frameRate': self.frameRate,
            'width': outW,
            'height': outH,
            'level': level,
            'bitrateKbps': targetBitrateKbps
        }))
        stderrBox = ['']

        loop = self.eventLoop()

        def postToLoop(callback, *args):
            try:
                loop.call_soon_threadsafe(callback, *args)
            except RuntimeError:
                return False
            return True

        def readOut():
            while True:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    return
                if not postToLoop(self.handleChunk, chunk):
                    return

        def readErr():
            while True:
                chunk = os.read(proc.stderr.fileno(), 65536)
                if not chunk:
                    return
                stderrBox[0] = (stderrBox[0] + chunk.decode('utf-8', 'replace'))[-MAX_STDERR_CHARS:]

        outThread = threading.Thread(target=readOut, daemon=True)
        errThread = threading.Thread(target=readErr, daemon=True)

        def onClose(*args):
            code = proc.wait()
            outThread.join()
            errThread.join()
            postToLoop(self.handleProcessExit, proc, code, stderrBox)

        outThread.start()
        errThread.start()
        threading.Thread(target=onClose, daemon=True).start()
        self.logger.get('media')('video source starting', JSObject({
            'source': self.sourceKind,
            'videoPath': self.videoPath,
            'width': self.width,
            'height': self.height,
            'fps': self.frameRate
        }))

    def eventLoop(self):
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.get_event_loop()

    def handleChunk(self, chunk):
        try:
            self.onData(chunk)
        except Exception as err:
            self.logger.get('error')('video stream parse error', JSObject({'message': toError(err).message}))

    def handleProcessExit(self, proc, code, stderrBox):
        if self.proc is proc:
            self.proc = None
        self.running = False
        if code > 0:
            self.logger.get('error')('ffmpeg video process exited unexpectedly', JSObject({'code': code, 'stderr': stderrBox[0].strip()}))
        if not self._deliberateStop and self.sourceKind:
            nowMs = now()
            if not self._restartWindowStartedAt or nowMs - self._restartWindowStartedAt > 60000:
                self._restartWindowStartedAt = nowMs
                self._restartCount = 0
            self._restartCount = (0 if self._restartCount is None else self._restartCount) + 1
            if self._restartCount > 5:
                self.logger.get('error')('video encoder crash-looped, giving up on auto-restart', JSObject({
                    'source': self.sourceKind,
                    'videoPath': self.videoPath,
                    'restartCount': self._restartCount
                }))
                return
            self.logger.get('error')('restarting video encoder after unexpected exit', JSObject({
                'source': self.sourceKind,
                'videoPath': self.videoPath,
                'restartCount': self._restartCount
            }))
            self.start()

    def onData(self, chunk):
        merged = self.pending + bytes(chunk)
        self.pending = merged
        if len(self.pending) > MAX_PENDING_BYTES:
            self.logger.get('debug')('video pending buffer exceeded cap without a second AUD, dropping', JSObject({
                'bytes': len(self.pending)
            }))
            self.pending = b''
            return
        audPositions = []
        i = 0
        data = self.pending
        while i < len(data):
            sc = startCodeLen(data, i)
            if sc > 0:
                naluStart = i + sc
                if naluStart < len(data) and (data[naluStart] & 0x1f) == 9:
                    audPositions.append(i)
                i += sc
                continue
            i += 1
        if len(audPositions) < 2:
            return
        for k in range(len(audPositions) - 1):
            au = data[audPositions[k]:audPositions[k + 1]]
            self.emitAccessUnit(au)
        self.pending = data[audPositions[-1]:]

    def emitAccessUnit(self, au):
        if self.waitingForKeyFrame:
            if not auHasIDR(au):
                return
            self.waitingForKeyFrame = False
        self.accessUnitsSent += 1
        if self.videoSender is not None:
            try:
                self.videoSender.sendCapturedVideoAU(au, self.frameDurationMs)
            except Exception as err:
                self.logger.get('trace')('captured video send failed', JSObject({'message': toError(err).message}))
        if self.accessUnitsSent == 1 or self.accessUnitsSent % 300 == 0:
            self.logger.get('trace')('video access unit emitted', JSObject({
                'count': self.accessUnitsSent,
                'bytes': len(au)
            }))

    def stop(self, options=None):
        opts = JSObject(options if options is not None else {})
        self._deliberateStop = True
        self.running = False
        if self.proc is not None:
            self.logger.get('warn')('[DIAG] ffmpeg video process stopping', JSObject({'pid': self.proc.pid}))
            try:
                self.proc.kill()
            except Exception as err:
                self.logger.get('trace')('ffmpeg video kill failed', JSObject({'message': toError(err).message}))
            self.proc = None
        self.pending = b''
        if not opts.get('keepSource'):
            self.videoPath = None
            self.sourceKind = None
