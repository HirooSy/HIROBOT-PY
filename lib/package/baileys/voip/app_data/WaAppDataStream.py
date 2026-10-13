from baileys.foundation.node_runtime import JSObject, clearInterval, now, setInterval
from baileys.voip.app_data.protocol import decodeAppDataPayload, encodeReactionPayload
from baileys.voip.crypto.primitives import randomInt
from baileys.voip.media.rtp import RtpHeader, RtpPacket
from baileys.voip.shim.util import nullish, optGet, toError


DEFAULT_RETRANSMISSION_INTERVAL_MS = 60
DEFAULT_CLEAR_INTERVAL_MS = 600

MAX_TRACKED_TRANSACTIONS = 64

WA_APP_DATA_PAYLOAD_TYPE = 119

EMPTY_REACTIONS = []


class WaAppDataStream:
    def __init__(self, options):
        self.logger = optGet(options, 'logger')
        self.ssrc = optGet(options, 'ssrc')
        self.sendPacket = optGet(options, 'sendPacket')
        self.sequenceNumber = randomInt(0, 65536)
        self.timestamp = randomInt(0, 0xffffffff)
        self.outgoing = None
        self.retransmitTimer = None
        self.seenTransactions = {}
        self.nextTransactionId = 1
        self.learnedPayloadType = None
        self.configuredPayloadType = optGet(options, 'payloadType')
        self.sframeRequired = False
        self.sframeProtect = None
        self.txReactionCount = 0
        self.txReactionErrorCount = 0
        self.rxReactionCount = 0
        self.retransmissionIntervalMs = nullish(optGet(options, 'retransmissionIntervalMs'), DEFAULT_RETRANSMISSION_INTERVAL_MS)
        self.clearIntervalMs = nullish(optGet(options, 'clearIntervalMs'), DEFAULT_CLEAR_INTERVAL_MS)

    @property
    def payloadType(self):
        return nullish(self.configuredPayloadType, WA_APP_DATA_PAYLOAD_TYPE)

    @property
    def peerPayloadType(self):
        return self.learnedPayloadType

    def observeInboundPayloadType(self, payloadType):
        if self.configuredPayloadType is not None or self.learnedPayloadType == payloadType:
            return
        self.learnedPayloadType = payloadType
        self.logger.get('debug')('app data payload type learned from peer', JSObject({
            'payloadType': payloadType,
            'ssrc': f'0x{self.ssrc:x}',
        }))

    def setSframe(self, required, protect):
        self.sframeRequired = required
        self.sframeProtect = protect

    def sendReaction(self, reaction):
        transactionId = self.nextTransactionId
        self.nextTransactionId += 1
        payload = encodeReactionPayload(JSObject({'transactionId': transactionId, 'reaction': reaction}))
        clearAt = now() + self.clearIntervalMs
        self.outgoing = JSObject({'payload': payload, 'transactionId': transactionId, 'reaction': reaction, 'clearAt': clearAt})
        self.txReactionCount += 1
        sent = self.transmit()
        self.armRetransmission()
        self.logger.get('debug')('call reaction queued', JSObject({
            'reaction': reaction,
            'transactionId': str(transactionId),
            'ssrc': f'0x{self.ssrc:x}',
            'firstAttemptSent': sent,
            'total': self.txReactionCount,
        }))
        return sent

    def receive(self, payload, ssrc):
        if len(payload) == 0:
            return EMPTY_REACTIONS
        decoded = decodeAppDataPayload(payload)
        if decoded is None:
            self.logger.get('debug')('app data payload not understood', JSObject({
                'ssrc': f'0x{self.ssrc:x}',
                'bytes': len(payload),
                'sframeRequired': self.sframeRequired,
                'sframeKeyed': self.sframeProtect is not None,
            }))
            return EMPTY_REACTIONS
        if decoded.get('truncated'):
            self.logger.get('debug')('app data payload carried more messages than are read', JSObject({
                'ssrc': f'0x{ssrc:x}',
                'read': len(decoded.get('items')),
            }))
        fresh = []
        for item in decoded.get('items'):
            reaction = optGet(item, 'reaction')
            if reaction is None:
                continue
            key = f"{ssrc}:{reaction.get('transactionId')}"
            if key in self.seenTransactions:
                continue
            self.rememberTransaction(key)
            self.rxReactionCount += 1
            fresh.append(reaction)
        if len(fresh) > 0:
            self.logger.get('debug')('call reaction received', JSObject({
                'shape': decoded.get('shape'),
                'count': len(fresh),
                'total': self.rxReactionCount,
            }))
        return fresh

    def close(self):
        self.clearOutgoing()
        self.seenTransactions.clear()

    def rememberTransaction(self, key):
        if len(self.seenTransactions) >= MAX_TRACKED_TRANSACTIONS:
            oldest = next(iter(self.seenTransactions), None)
            if oldest is not None:
                del self.seenTransactions[oldest]
        self.seenTransactions[key] = True

    def armRetransmission(self):
        if self.retransmitTimer is not None:
            return
        self.retransmitTimer = setInterval(lambda: self.onRetransmissionTick(), self.retransmissionIntervalMs)
        self.retransmitTimer.unref()

    def onRetransmissionTick(self):
        outgoing = self.outgoing
        if outgoing is None:
            self.clearOutgoing()
            return
        if now() >= outgoing.get('clearAt'):
            self.logger.get('debug')('call reaction cleared from send buffer', JSObject({
                'transactionId': str(outgoing.get('transactionId')),
            }))
            self.clearOutgoing()
            return
        self.transmit()

    def clearOutgoing(self):
        self.outgoing = None
        if self.retransmitTimer is not None:
            clearInterval(self.retransmitTimer)
            self.retransmitTimer = None

    def transmit(self):
        outgoing = self.outgoing
        if outgoing is None:
            return False
        payloadType = self.payloadType
        try:
            header = RtpHeader(payloadType, self.sequenceNumber, self.timestamp, self.ssrc)
            self.sequenceNumber = (self.sequenceNumber + 1) & 0xffff
            payload = self.sframeProtect(outgoing.get('payload')) if self.sframeProtect is not None else outgoing.get('payload')
            return self.sendPacket(RtpPacket(header, payload))
        except Exception as err:
            self.txReactionErrorCount += 1
            self.logger.get('debug')('failed to send app data rtp packet', JSObject({
                'ssrc': f'0x{self.ssrc:x}',
                'errors': self.txReactionErrorCount,
                'message': toError(err).message,
            }))
            return False
