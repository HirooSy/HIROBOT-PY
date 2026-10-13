import math

from baileys.foundation.node_runtime import JSError, JSObject
from baileys.voip.bytes import readUInt16BE, readUInt32BE, writeUInt16BE, writeUInt32BE
from baileys.voip.crypto.primitives import randomInt
from baileys.voip.types import PayloadType

RTP_VERSION = 2
MIN_HEADER_SIZE = 12


class RtpHeader:
    def __init__(self, payloadType, sequenceNumber, timestamp, ssrc):
        self.version = RTP_VERSION
        self.padding = False
        self.extension = False
        self.marker = False
        self.payloadType = payloadType
        self.sequenceNumber = sequenceNumber
        self.timestamp = timestamp
        self.ssrc = ssrc
        self.csrc = []
        self.extensionProfile = 0
        self.extensionData = b''

    @property
    def csrcCount(self):
        return len(self.csrc)

    def size(self):
        s = MIN_HEADER_SIZE + self.csrcCount * 4
        if self.extension:
            s += 4 + len(self.extensionData)
        return s

    def encode(self, buf):
        if len(buf) < self.size():
            raise JSError('buffer too small for RTP header')
        buf[0] = ((self.version & 0x03) << 6) | ((1 if self.padding else 0) << 5) | ((1 if self.extension else 0) << 4) | (self.csrcCount & 0x0f)
        buf[1] = ((1 if self.marker else 0) << 7) | (self.payloadType & 0x7f)
        writeUInt16BE(buf, self.sequenceNumber, 2)
        writeUInt32BE(buf, self.timestamp, 4)
        writeUInt32BE(buf, self.ssrc, 8)
        offset = 12
        for i in range(len(self.csrc)):
            writeUInt32BE(buf, self.csrc[i], offset)
            offset += 4
        if self.extension:
            if len(self.extensionData) % 4 != 0:
                raise JSError('RTP extension data must be 32-bit aligned')
            writeUInt16BE(buf, self.extensionProfile, offset)
            writeUInt16BE(buf, len(self.extensionData) // 4, offset + 2)
            buf[offset + 4:offset + 4 + len(self.extensionData)] = self.extensionData
        return self.size()

    @staticmethod
    def decode(buf):
        if len(buf) < MIN_HEADER_SIZE:
            raise JSError('buffer too small for RTP header')
        version = (buf[0] >> 6) & 0x03
        if version != RTP_VERSION:
            raise JSError(f'invalid RTP version: {version}')
        padding = ((buf[0] >> 5) & 0x01) != 0
        extension = ((buf[0] >> 4) & 0x01) != 0
        csrcCount = buf[0] & 0x0f
        marker = ((buf[1] >> 7) & 0x01) != 0
        payloadType = buf[1] & 0x7f
        sequenceNumber = readUInt16BE(buf, 2)
        timestamp = readUInt32BE(buf, 4)
        ssrc = readUInt32BE(buf, 8)
        headerSize = MIN_HEADER_SIZE + csrcCount * 4
        if len(buf) < headerSize:
            raise JSError('buffer too small for CSRC list')
        csrc = []
        offset = 12
        for i in range(csrcCount):
            csrc.append(readUInt32BE(buf, offset))
            offset += 4
        header = RtpHeader(payloadType, sequenceNumber, timestamp, ssrc)
        header.version = version
        header.padding = padding
        header.extension = extension
        header.marker = marker
        header.csrc = csrc
        if extension:
            if len(buf) < offset + 4:
                raise JSError('buffer too small for RTP extension header')
            header.extensionProfile = readUInt16BE(buf, offset)
            extWords = readUInt16BE(buf, offset + 2)
            extBytes = extWords * 4
            offset += 4
            if len(buf) < offset + extBytes:
                raise JSError('buffer too small for RTP extension data')
            header.extensionData = buf[offset:offset + extBytes]
        return header


class RtpPacket:
    def __init__(self, header, payload):
        self.header = header
        self.payload = payload

    def size(self):
        return self.header.size() + len(self.payload)

    def encode(self):
        buf = bytearray(self.size())
        headerSize = self.header.encode(buf)
        buf[headerSize:headerSize + len(self.payload)] = self.payload
        return bytes(buf)

    @staticmethod
    def decode(buf):
        header = RtpHeader.decode(buf)
        end = len(buf)
        if header.padding:
            padLen = buf[len(buf) - 1]
            if padLen > 0 and header.size() + padLen <= len(buf):
                end = len(buf) - padLen
        payload = buf[header.size():end]
        return RtpPacket(header, payload)


VideoMediaFrameInfo = JSObject({'IDR': 0x08, 'Delta': 0x20})
DEFAULT_VIDEO_RTP_STEP_SAMPLES = math.floor(90000 / 30 + 0.5)


def videoRtpDurationSamples(durationMs):
    if not (durationMs and durationMs > 0):
        return DEFAULT_VIDEO_RTP_STEP_SAMPLES
    samples = math.floor((durationMs * 90000) / 1000 + 0.5)
    return samples or DEFAULT_VIDEO_RTP_STEP_SAMPLES


def encodeWhatsappVideoExtension(ext):
    frameNumber = ext.get('frameNumber')
    hasFrameNumber = frameNumber is not None
    out = []
    out.append(0x30 | ((3 if hasFrameNumber else 1) - 1))
    out.append(ext.get('mediaFrameInfo') & 0xff)
    if hasFrameNumber:
        out.append(((frameNumber & 0xFFFFFFFF) >> 8) & 0xff)
        out.append(frameNumber & 0xff)
    initialBandwidth = ext.get('initialBandwidth')
    out.append(0x51)
    out.append(((initialBandwidth & 0xFFFFFFFF) >> 8) & 0xff)
    out.append(initialBandwidth & 0xff)
    shortOffset = ext.get('shortOffset')
    out.append(0x61)
    out.append(((shortOffset & 0xFFFFFFFF) >> 8) & 0xff)
    out.append(shortOffset & 0xff)
    transportSequence = ext.get('transportSequence')
    out.append(0x91)
    out.append(((transportSequence & 0xFFFFFFFF) >> 8) & 0xff)
    out.append(transportSequence & 0xff)
    while len(out) % 4 != 0:
        out.append(0)
    return bytes(out)


class VideoRtpStream:
    def __init__(self, ssrc, tsStride):
        self.ssrc = ssrc
        self.sequenceNumber = randomInt(0, 65536)
        self.timestamp = randomInt(0, 0xffffffff)
        self.tsStride = tsStride
        self.transportSequence = 0
        self.frameNumber = 1
        self.firstPacket = True

    def setTimestampStride(self, tsStride):
        if not tsStride:
            return False
        self.tsStride = tsStride
        return True

    def nextPacket(self, lastInAccessUnit, mediaFrameInfo):
        frameNumber = self.frameNumber if self.firstPacket else None
        ext = JSObject({
            'mediaFrameInfo': mediaFrameInfo,
            'frameNumber': frameNumber,
            'initialBandwidth': 0,
            'shortOffset': 0,
            'transportSequence': self.transportSequence
        })
        header = RtpHeader(PayloadType.H264, self.sequenceNumber, self.timestamp, self.ssrc)
        header.marker = lastInAccessUnit
        header.extension = True
        header.extensionProfile = 0xdebe
        header.extensionData = encodeWhatsappVideoExtension(ext)
        self.sequenceNumber = (self.sequenceNumber + 1) & 0xffff
        self.transportSequence = (self.transportSequence + 1) & 0xffff
        if lastInAccessUnit:
            self.timestamp = (self.timestamp + self.tsStride) & 0xFFFFFFFF
            self.frameNumber = (self.frameNumber + 1) & 0xffff
            self.firstPacket = True
        else:
            self.firstPacket = False
        return header


class RtpSession:
    def __init__(self, ssrc, payloadType, sampleRate, samplesPerPacket):
        self.ssrc = ssrc
        self.payloadType = payloadType
        self.sequenceNumber = randomInt(0, 65536)
        self.sampleRate = sampleRate
        self.timestamp = randomInt(0, 0xffffffff)
        self.samplesPerPacket = samplesPerPacket

    @staticmethod
    def whatsappOpus(ssrc):
        return RtpSession(ssrc, PayloadType.WhatsAppOpus, 16000, 960)

    def createPacket(self, payload, marker=False):
        header = RtpHeader(self.payloadType, self.sequenceNumber, self.timestamp, self.ssrc)
        header.marker = marker
        self.sequenceNumber = (self.sequenceNumber + 1) & 0xffff
        self.timestamp = (self.timestamp + self.samplesPerPacket) & 0xFFFFFFFF
        return RtpPacket(header, payload)

    def createPacketWithDuration(self, payload, durationSamples, marker=False):
        header = RtpHeader(self.payloadType, self.sequenceNumber, self.timestamp, self.ssrc)
        header.marker = marker
        self.sequenceNumber = (self.sequenceNumber + 1) & 0xffff
        self.timestamp = (self.timestamp + durationSamples) & 0xFFFFFFFF
        return RtpPacket(header, payload)
