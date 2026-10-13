import baileys_lite.foundation.node_runtime as nodeRuntime
from baileys_lite.foundation.node_crypto import randomBytes
from baileys_lite.foundation.node_runtime import EventEmitter, JSError, JSObject, clearInterval, clearTimeout, setInterval, setTimeout
from baileys_lite.voip.shim.core import createNoopLogger
import baileys_lite.voip.relay.sctp.wire as sctp


SCTP_PORT = 5000
RETRANSMIT_TIMEOUT_MS = 1000
MAX_INIT_RETRIES = 5
HEARTBEAT_INTERVAL_MS = 5000

SEND_WINDOW_SIZE = 32
MAX_VIDEO_QUEUE_AGE_MS = 400
MAX_QUEUED_VIDEO_PACKETS = 200
MAX_QUEUED_AUDIO_FRAMES = 20
MAX_VIDEO_IN_FLIGHT = 24
STALE_GAP_TSN_LIMIT = 512


def randomUint32():
    return int.from_bytes(randomBytes(4), 'big')


def postInc(owner, name):
    value = getattr(owner, name)
    setattr(owner, name, value + 1)
    return value


def sackAcksTsn(cumAck, tsn):
    diff = (cumAck - tsn) & 0xFFFFFFFF
    return diff < 0x80000000


class SctpAssociation(EventEmitter):
    def __init__(self, sendDtlsPayload=None, logger=None):
        super().__init__()
        self.sendDtlsPayload = sendDtlsPayload
        self.logger = logger if logger is not None else createNoopLogger()
        self.state = 'idle'
        self.myTag = randomUint32()
        self.peerVerificationTag = 0
        self.initialTsn = randomUint32()
        self.nextTsn = self.initialTsn
        self.peerInitialTsn = 0
        self.cumulativeAckReceived = -1
        self.stateCookie = None
        self.retransmitTimer = None
        self.initRetries = 0
        self.heartbeatTimer = None
        self.outboundStreamSeq = 0
        self.outboundVideoStreamSeq = 0
        self.inboundExpectedStreamSeq = 0
        self.reorderBuffer = {}
        self.inFlight = {}
        self.videoInFlightCount = 0
        self.videoFlushCount = 0
        self.audioQueue = []
        self.videoQueue = []
        self.inboundCumulativeTsn = -1
        self.inboundReceivedAheadTsns = set()
        self.retransmitCount = 0

    def start(self):
        self.state = 'wait_init_ack'
        self.sendInit()

    def close(self):
        self.state = 'closed'
        clearTimeout(self.retransmitTimer)
        clearInterval(self.heartbeatTimer)
        for entry in self.inFlight.values():
            clearTimeout(entry.get('timer'))
        self.inFlight.clear()
        self.videoInFlightCount = 0
        self.audioQueue = []
        self.videoQueue = []
        self.reorderBuffer.clear()
        self.inboundReceivedAheadTsns.clear()

    @property
    def isConnected(self):
        return self.state == 'connected'

    def getSendBacklog(self):
        return JSObject({
            'queued': len(self.audioQueue) + len(self.videoQueue),
            'queuedAudio': len(self.audioQueue),
            'queuedVideo': len(self.videoQueue),
            'inFlight': len(self.inFlight),
            'retransmits': self.retransmitCount,
            'videoFlushes': self.videoFlushCount,
        })

    def send(self, payload, isAudio=True):
        if self.state == 'closed':
            return
        if isAudio:
            self.audioQueue.append(payload)
            if len(self.audioQueue) > MAX_QUEUED_AUDIO_FRAMES:
                del self.audioQueue[:len(self.audioQueue) - MAX_QUEUED_AUDIO_FRAMES]
        else:
            nowMs = nodeRuntime.now()
            self.videoQueue.append(JSObject({'payload': payload, 'queuedAtMs': nowMs}))
            oldest = self.videoQueue[0]
            if len(self.videoQueue) > MAX_QUEUED_VIDEO_PACKETS or \
                    nowMs - oldest.get('queuedAtMs') > MAX_VIDEO_QUEUE_AGE_MS:
                dropped = len(self.videoQueue)
                self.videoQueue = []
                self.videoFlushCount += 1
                self.logger.get('trace')('sctp flushed stale video queue to protect audio latency',
                                  JSObject({'dropped': dropped}))
        self.pumpSendQueue()

    def handleDtlsPayload(self, plaintext):
        if self.state == 'closed':
            return
        try:
            header = sctp.parsePacketHeader(plaintext)
        except Exception as e:
            self.logger.get('trace')('sctp payload failed to parse', JSObject({
                'message': str(e), 'bytes': len(plaintext), 'hex': bytes(plaintext).hex()}))
            return
        chunkTypesForLog = [c.get('type') for c in sctp.splitChunks(plaintext, header.get('chunksStart'))]
        self.logger.get('trace')('sctp recv packet', JSObject({
            'vtag': '0x%x' % header.get('verificationTag'),
            'expectVtag': '0x%x' % self.myTag,
            'chunkTypes': chunkTypesForLog,
            'state': self.state,
        }))
        if self.state != 'wait_init_ack' and header.get('verificationTag') != self.myTag:
            return
        chunks = sctp.splitChunks(plaintext, header.get('chunksStart'))
        for chunk in chunks:
            self.handleChunk(chunk, header)

    def sendInit(self):
        chunk = sctp.buildInit(initiateTag=self.myTag, advertisedReceiverWindow=131072,
                               outboundStreams=65535, inboundStreams=65535, initialTsn=self.initialTsn)
        packet = sctp.buildPacket(SCTP_PORT, SCTP_PORT, 0, [chunk])
        self.logger.get('trace')('sctp sending INIT', JSObject({
            'myTag': '0x%x' % self.myTag, 'initialTsn': self.initialTsn, 'attempt': self.initRetries}))
        if self.initRetries == 0:
            self.logger.get('trace')('sctp INIT packet hex (first attempt only)', JSObject({'hex': packet.hex()}))
        self.sendDtlsPayload(packet)
        clearTimeout(self.retransmitTimer)

        def onInitRetransmit():
            if self.state != 'wait_init_ack':
                return
            self.initRetries += 1
            if self.initRetries > MAX_INIT_RETRIES:
                self.fail(JSError('SCTP: INIT retransmit limit exceeded'))
                return
            self.sendInit()

        self.retransmitTimer = setTimeout(onInitRetransmit,
                                          RETRANSMIT_TIMEOUT_MS * min(2 ** self.initRetries, 8))

    def sendCookieEcho(self):
        chunk = sctp.buildCookieEcho(self.stateCookie)
        packet = sctp.buildPacket(SCTP_PORT, SCTP_PORT, self.peerVerificationTag, [chunk])
        self.sendDtlsPayload(packet)
        clearTimeout(self.retransmitTimer)

        def onCookieRetransmit():
            if self.state != 'wait_cookie_ack':
                return
            self.sendCookieEcho()

        self.retransmitTimer = setTimeout(onCookieRetransmit, RETRANSMIT_TIMEOUT_MS)

    def onConnected(self):
        self.state = 'connected'
        clearTimeout(self.retransmitTimer)
        self.heartbeatTimer = setInterval(lambda: self.sendHeartbeat(), HEARTBEAT_INTERVAL_MS)
        self.emit('connected')
        self.pumpSendQueue()

    def pumpSendQueue(self):
        if self.state != 'connected':
            return
        while len(self.inFlight) < SEND_WINDOW_SIZE:
            if len(self.audioQueue) > 0:
                payload = self.audioQueue.pop(0)
                isAudio = True
            elif len(self.videoQueue) > 0 and self.videoInFlightCount < MAX_VIDEO_IN_FLIGHT:
                payload = self.videoQueue.pop(0)['payload']
                isAudio = False
            else:
                break
            tsn = self.nextTsn
            self.nextTsn = (self.nextTsn + 1) & 0xFFFFFFFF
            streamSeq = postInc(self, 'outboundStreamSeq') if isAudio else postInc(self, 'outboundVideoStreamSeq')
            chunk = sctp.buildData(tsn=tsn, streamId=0, streamSeq=streamSeq, ppid=53, payload=payload,
                                   unordered=isAudio)
            packet = sctp.buildPacket(SCTP_PORT, SCTP_PORT, self.peerVerificationTag, [chunk])
            entry = JSObject({'packet': packet, 'timer': None, 'isAudio': isAudio})
            self.inFlight[tsn] = entry
            if not isAudio:
                self.videoInFlightCount += 1
            self.sendDtlsPayload(packet)
            self.armDataRetransmit(tsn, entry)

    def armDataRetransmit(self, tsn, entry):
        clearTimeout(entry.get('timer'))

        def onDataRetransmit():
            if tsn not in self.inFlight:
                return
            self.retransmitCount += 1
            self.sendDtlsPayload(entry.get('packet'))
            self.armDataRetransmit(tsn, entry)

        entry['timer'] = setTimeout(onDataRetransmit, RETRANSMIT_TIMEOUT_MS)

    def handleChunk(self, chunk, header):
        chunkType = chunk.get('type')
        if chunkType == sctp.ChunkType['INIT_ACK']:
            if self.state != 'wait_init_ack':
                return
            parsed = sctp.parseInitOrInitAck(chunk.get('value'))
            if parsed.get('stateCookie') is None:
                self.fail(JSError('SCTP: INIT ACK missing State Cookie'))
                return
            self.peerVerificationTag = parsed.get('initiateTag')
            self.peerInitialTsn = parsed.get('initialTsn')
            self.inboundExpectedStreamSeq = 0
            self.inboundCumulativeTsn = (parsed.get('initialTsn') - 1) & 0xFFFFFFFF
            self.inboundReceivedAheadTsns.clear()
            self.stateCookie = parsed.get('stateCookie')
            self.state = 'wait_cookie_ack'
            self.initRetries = 0
            self.sendCookieEcho()
            return
        if chunkType == sctp.ChunkType['COOKIE_ACK']:
            if self.state != 'wait_cookie_ack':
                return
            self.onConnected()
            return
        if chunkType == sctp.ChunkType['DATA']:
            parsed = sctp.parseData(chunk.get('flags'), chunk.get('value'))
            self.handleIncomingData(parsed)
            return
        if chunkType == sctp.ChunkType['SACK']:
            parsed = sctp.parseSack(chunk.get('value'))
            self.handleSack(parsed)
            return
        if chunkType == sctp.ChunkType['HEARTBEAT']:
            info = sctp.parseHeartbeatInfo(chunk.get('value'))
            ackChunk = sctp.buildHeartbeatAck(info)
            packet = sctp.buildPacket(SCTP_PORT, SCTP_PORT, self.peerVerificationTag, [ackChunk])
            self.sendDtlsPayload(packet)
            return
        if chunkType == sctp.ChunkType['HEARTBEAT_ACK']:
            return
        if chunkType == sctp.ChunkType['ABORT']:
            self.fail(JSError('SCTP: received ABORT from peer'))
            return
        if chunkType == sctp.ChunkType['ERROR']:
            return
        return

    def handleIncomingData(self, parsed):
        tsn = parsed.get('tsn')
        streamId = parsed.get('streamId')
        streamSeq = parsed.get('streamSeq')
        ppid = parsed.get('ppid')
        payload = parsed.get('payload')
        unordered = parsed.get('unordered')

        self.recordInboundTsn(tsn)
        self.buildAndSendSack()

        if streamId != 0 or ppid != 53:
            return

        if unordered:
            self.emit('message', payload)
            return

        if streamSeq == self.inboundExpectedStreamSeq:
            self.inboundExpectedStreamSeq += 1
            self.emit('message', payload)

            while self.inboundExpectedStreamSeq in self.reorderBuffer:
                buffered = self.reorderBuffer.pop(self.inboundExpectedStreamSeq)
                self.inboundExpectedStreamSeq += 1
                self.emit('message', buffered)
        elif streamSeq > self.inboundExpectedStreamSeq:
            self.reorderBuffer[streamSeq] = payload
            if len(self.reorderBuffer) > STALE_GAP_TSN_LIMIT:
                bufferedSeqs = sorted(self.reorderBuffer.keys())
                self.logger.get('debug')('sctp: forcing stream sequence past a stuck gap', JSObject({
                    'from': self.inboundExpectedStreamSeq, 'bufferedCount': len(bufferedSeqs)}))
                self.inboundExpectedStreamSeq = bufferedSeqs[0]
                buffered = self.reorderBuffer.pop(self.inboundExpectedStreamSeq)
                self.inboundExpectedStreamSeq += 1
                self.emit('message', buffered)
                while self.inboundExpectedStreamSeq in self.reorderBuffer:
                    nxt = self.reorderBuffer.pop(self.inboundExpectedStreamSeq)
                    self.inboundExpectedStreamSeq += 1
                    self.emit('message', nxt)

    def recordInboundTsn(self, tsn):
        if sackAcksTsn(self.inboundCumulativeTsn, tsn):
            return
        self.inboundReceivedAheadTsns.add(tsn)
        nxt = (self.inboundCumulativeTsn + 1) & 0xFFFFFFFF
        while nxt in self.inboundReceivedAheadTsns:
            self.inboundReceivedAheadTsns.discard(nxt)
            self.inboundCumulativeTsn = nxt
            nxt = (self.inboundCumulativeTsn + 1) & 0xFFFFFFFF
        if len(self.inboundReceivedAheadTsns) > STALE_GAP_TSN_LIMIT:
            maxAheadOffset = 0
            for aheadTsn in self.inboundReceivedAheadTsns:
                offset = (aheadTsn - self.inboundCumulativeTsn) & 0xFFFFFFFF
                if offset > maxAheadOffset:
                    maxAheadOffset = offset
            jumpTo = (self.inboundCumulativeTsn + maxAheadOffset) & 0xFFFFFFFF
            self.logger.get('debug')('sctp: forcing cumulative TSN past a stuck gap', JSObject({
                'from': self.inboundCumulativeTsn, 'to': jumpTo,
                'aheadCount': len(self.inboundReceivedAheadTsns)}))
            self.inboundReceivedAheadTsns.discard(jumpTo)
            self.inboundCumulativeTsn = jumpTo

    def buildAndSendSack(self):
        gapAckBlocks = []
        if len(self.inboundReceivedAheadTsns) > 0:
            cum = self.inboundCumulativeTsn

            def sortKey(tsn):
                return (tsn - cum) & 0xFFFFFFFF

            sortedTsns = sorted(self.inboundReceivedAheadTsns, key=sortKey)
            blockStart = None
            blockEnd = None
            for tsn in sortedTsns:
                offset = (tsn - self.inboundCumulativeTsn) & 0xFFFFFFFF
                if offset > 0xFFFF:
                    continue
                if blockStart is not None and offset == blockEnd + 1:
                    blockEnd = offset
                else:
                    if blockStart is not None:
                        gapAckBlocks.append(JSObject({'start': blockStart, 'end': blockEnd}))
                    blockStart = offset
                    blockEnd = offset
            if blockStart is not None:
                gapAckBlocks.append(JSObject({'start': blockStart, 'end': blockEnd}))
        sackChunk = sctp.buildSack(cumulativeTsnAck=self.inboundCumulativeTsn,
                                   advertisedReceiverWindow=131072, gapAckBlocks=gapAckBlocks)
        sackPacket = sctp.buildPacket(SCTP_PORT, SCTP_PORT, self.peerVerificationTag, [sackChunk])
        self.sendDtlsPayload(sackPacket)

    def handleSack(self, parsed):
        cumulativeTsnAck = parsed.get('cumulativeTsnAck')
        gapAckBlocks = parsed.get('gapAckBlocks')
        if len(self.inFlight) == 0:
            return
        acked = False
        for tsn, entry in list(self.inFlight.items()):
            if sackAcksTsn(cumulativeTsnAck, tsn):
                clearTimeout(entry.get('timer'))
                del self.inFlight[tsn]
                if not entry.get('isAudio'):
                    self.videoInFlightCount -= 1
                acked = True
        if gapAckBlocks and len(gapAckBlocks) > 0 and len(self.inFlight) > 0:
            for block in gapAckBlocks:
                for tsn, entry in list(self.inFlight.items()):
                    offset = (tsn - cumulativeTsnAck) & 0xFFFFFFFF
                    if block.get('start') <= offset <= block.get('end'):
                        clearTimeout(entry.get('timer'))
                        del self.inFlight[tsn]
                        if not entry.get('isAudio'):
                            self.videoInFlightCount -= 1
                        acked = True
        if acked:
            self.pumpSendQueue()

    def sendHeartbeat(self):
        if self.state != 'connected':
            return
        info = randomBytes(16)
        chunk = sctp.buildHeartbeat(info)
        packet = sctp.buildPacket(SCTP_PORT, SCTP_PORT, self.peerVerificationTag, [chunk])
        self.sendDtlsPayload(packet)

    def fail(self, err):
        self.state = 'closed'
        clearTimeout(self.retransmitTimer)
        clearInterval(self.heartbeatTimer)
        for entry in self.inFlight.values():
            clearTimeout(entry.get('timer'))
        self.inFlight.clear()
        self.videoInFlightCount = 0
        self.audioQueue = []
        self.videoQueue = []
        self.reorderBuffer.clear()
        self.inboundReceivedAheadTsns.clear()
        self.emit('error', err)
