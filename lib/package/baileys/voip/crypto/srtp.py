from baileys.foundation.node_runtime import JSError
from baileys.voip.bytes import writeBigUInt64BE, writeUInt32BE
from baileys.voip.media.rtp import RtpHeader, RtpPacket
from baileys.voip.shim.util import uint8TimingSafeEqual
from baileys.voip.types import SRTP_AUTH_TAG_LEN, SRTP_LABEL
from baileys.voip.crypto.primitives import aesCtr128, hmacSha1

SRTP_REPLAY_WINDOW = 64
SRTP_INDEX_MASK = (1 << 64) - 1


class SrtpContext:
    def __init__(self, keying, authTagLen=None):
        self.roc = 0
        self.lastSeq = 0
        self.initialized = False
        self.highestIndex = 0
        self.replayMask = 0
        self.ivBuffer = bytearray(16)
        self.ssrcBuffer = bytearray(4)
        self.indexBuffer = bytearray(8)
        self.rocBuffer = bytearray(4)
        self.authTagLen = SRTP_AUTH_TAG_LEN if authTagLen is None else authTagLen
        self.sessionKey = deriveKey(keying.get('masterKey'), keying.get('masterSalt'), SRTP_LABEL['ENCRYPTION'], 16)
        self.authKey = deriveKey(keying.get('masterKey'), keying.get('masterSalt'), SRTP_LABEL['AUTH'], 20)
        self.sessionSalt = deriveKey(keying.get('masterKey'), keying.get('masterSalt'), SRTP_LABEL['SALT'], 14)

    def setAuthKeying(self, keying):
        self.authKey = deriveKey(keying.get('masterKey'), keying.get('masterSalt'), SRTP_LABEL['AUTH'], 20)

    def protect(self, packet):
        self.updateRoc(packet.header.sequenceNumber)
        index = self.packetIndex(packet.header.sequenceNumber)
        headerSize = packet.header.size()
        payloadLen = len(packet.payload)
        output = bytearray(headerSize + payloadLen + self.authTagLen)
        packet.header.encode(output)
        iv = self.generateIv(packet.header.ssrc, index)
        encrypted = aesCtr128(self.sessionKey, bytes(iv), packet.payload)
        output[headerSize:headerSize + len(encrypted)] = encrypted
        if self.authTagLen > 0:
            authData = bytes(output[0:headerSize + payloadLen])
            tag = self.computeAuthTag(authData, self.roc, self.authTagLen)
            output[headerSize + payloadLen:headerSize + payloadLen + len(tag)] = tag
        return bytes(output)

    def unprotect(self, data):
        if len(data) < 12:
            raise SrtpError('packet_too_short', f'Packet too short: {len(data)} bytes')
        header = RtpHeader.decode(data)
        headerSize = header.size()
        payloadLen = len(data) - headerSize - self.authTagLen
        if payloadLen <= 0:
            raise SrtpError('packet_too_short', f'No payload: {len(data)}B total, {headerSize}B header, auth={self.authTagLen}')
        seq = header.sequenceNumber
        estimatedRoc = self.estimateRoc(seq)
        index = (estimatedRoc << 16) | seq
        if self.isReplayed(index):
            raise SrtpError('replay', f'SRTP replay detected: index {index}')
        if self.authTagLen > 0:
            authStart = headerSize + payloadLen
            authData = data[0:authStart]
            expected = self.computeAuthTag(authData, estimatedRoc, self.authTagLen)
            received = data[authStart:authStart + self.authTagLen]
            if not uint8TimingSafeEqual(expected, received):
                raise SrtpError('auth_failed', 'SRTP auth tag verification failed')
        iv = self.generateIv(header.ssrc, index)
        decrypted = aesCtr128(self.sessionKey, bytes(iv), data[headerSize:headerSize + payloadLen])
        self.advanceReplay(index, estimatedRoc, seq)
        return RtpPacket(header, decrypted)

    def updateRoc(self, seq):
        if not self.initialized:
            self.lastSeq = seq
            self.initialized = True
            return
        diff = seq - self.lastSeq
        if diff < -32768:
            self.roc = (self.roc + 1) & 0xFFFFFFFF
        self.lastSeq = seq

    def estimateRoc(self, seq):
        if not self.initialized:
            return self.roc
        if self.lastSeq < 32768:
            return ((self.roc - 1) & 0xFFFFFFFF) if seq - self.lastSeq > 32768 else self.roc
        return ((self.roc + 1) & 0xFFFFFFFF) if self.lastSeq - seq > 32768 else self.roc

    def isReplayed(self, index):
        if not self.initialized:
            return False
        if index > self.highestIndex:
            return False
        offset = self.highestIndex - index
        if offset >= SRTP_REPLAY_WINDOW:
            return True
        return (self.replayMask & (1 << offset)) != 0

    def advanceReplay(self, index, estimatedRoc, seq):
        if self.initialized and index <= self.highestIndex:
            offset = self.highestIndex - index
            if offset < SRTP_REPLAY_WINDOW:
                self.replayMask |= 1 << offset
            return
        shift = (index - self.highestIndex) if self.initialized else SRTP_REPLAY_WINDOW
        if shift >= SRTP_REPLAY_WINDOW:
            self.replayMask = 1
        else:
            self.replayMask = ((self.replayMask << shift) | 1) & SRTP_INDEX_MASK
        self.highestIndex = index
        self.roc = estimatedRoc
        self.lastSeq = seq
        self.initialized = True

    def packetIndex(self, seq):
        return (self.roc << 16) | seq

    def generateIv(self, ssrc, index):
        self.ivBuffer[:] = bytes(16)
        salt = self.sessionSalt[0:14]
        self.ivBuffer[0:len(salt)] = salt
        writeUInt32BE(self.ssrcBuffer, ssrc, 0)
        for i in range(4):
            self.ivBuffer[4 + i] ^= self.ssrcBuffer[i]
        writeBigUInt64BE(self.indexBuffer, index, 0)
        for i in range(6):
            self.ivBuffer[8 + i] ^= self.indexBuffer[2 + i]
        return self.ivBuffer

    def computeAuthTag(self, data, roc, tagLen=SRTP_AUTH_TAG_LEN):
        writeUInt32BE(self.rocBuffer, roc, 0)
        result = hmacSha1(self.authKey, data, bytes(self.rocBuffer))
        return result[0:tagLen]


class SrtpSession:
    def __init__(self, sendKey, recvKey, sendAuthLen, recvAuthLen):
        self.sendCtx = SrtpContext(sendKey, sendAuthLen)
        self.recvCtx = SrtpContext(recvKey, recvAuthLen)

    def protect(self, packet):
        return self.sendCtx.protect(packet)

    def unprotect(self, data):
        return self.recvCtx.unprotect(data)

    def setSendAuthKeying(self, keying):
        self.sendCtx.setAuthKeying(keying)


def deriveKey(masterKey, masterSalt, label, length):
    iv = bytearray(16)
    salt = masterSalt[0:14]
    iv[0:len(salt)] = salt
    iv[7] ^= label
    zeros = bytes(length)
    return aesCtr128(masterKey, bytes(iv), zeros)


class SrtpError(JSError):
    def __init__(self, type, message):
        super().__init__(message)
        self.type = type
        self.name = 'SrtpError'
