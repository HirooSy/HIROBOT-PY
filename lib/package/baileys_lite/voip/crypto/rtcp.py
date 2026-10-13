from baileys_lite.voip.bytes import writeUInt32BE, writeBigUInt64BE
from baileys_lite.voip.crypto.primitives import aesCtr128, hmacSha1
from baileys_lite.voip.types import RTCP_AUTH_TAG_LEN, SRTCP_LABEL

RTCP_HEADER_LEN = 8


def deriveKey(masterKey, masterSalt, label, length):
    iv = bytearray(16)
    salt = masterSalt[0:14]
    iv[0:len(salt)] = salt
    iv[7] ^= label
    zeros = bytes(length)
    return aesCtr128(masterKey, bytes(iv), zeros)


class SrtcpSendContext:
    def __init__(self, keying):
        self.cipherKey = deriveKey(keying.get('masterKey'), keying.get('masterSalt'), SRTCP_LABEL['ENCRYPTION'], 16)
        self.authKey = deriveKey(keying.get('masterKey'), keying.get('masterSalt'), SRTCP_LABEL['AUTH'], 20)
        self.salt = deriveKey(keying.get('masterKey'), keying.get('masterSalt'), SRTCP_LABEL['SALT'], 14)
        self.index = 1
        self.ssrcBuffer = bytearray(4)
        self.ivBuffer = bytearray(16)
        self.indexBuffer = bytearray(8)

    def generateIv(self, ssrc, roc, seq):
        self.ivBuffer[:] = bytes(16)
        salt = self.salt[0:14]
        self.ivBuffer[0:len(salt)] = salt
        writeUInt32BE(self.ssrcBuffer, ssrc, 0)
        for i in range(4):
            self.ivBuffer[4 + i] ^= self.ssrcBuffer[i]
        index = ((roc & 0xFFFFFFFF) << 16) | (seq & 0xFFFF)
        writeBigUInt64BE(self.indexBuffer, index, 0)
        for i in range(6):
            self.ivBuffer[8 + i] ^= self.indexBuffer[2 + i]
        return self.ivBuffer

    def protect(self, senderSsrc, rtcpPacket):
        splitAt = min(len(rtcpPacket), RTCP_HEADER_LEN)
        header = rtcpPacket[0:splitAt]
        body = rtcpPacket[splitAt:]

        index = self.index & 0xFFFFFFFF
        self.index = (self.index + 1) & 0xFFFFFFFF

        roc = index >> 16
        seq = index & 0xFFFF
        iv = self.generateIv(senderSsrc, roc, seq)
        encryptedBody = aesCtr128(self.cipherKey, bytes(iv), body)

        withIndexWord = bytearray(len(header) + len(encryptedBody) + 4)
        withIndexWord[0:len(header)] = header
        withIndexWord[len(header):len(header) + len(encryptedBody)] = encryptedBody
        writeUInt32BE(withIndexWord, 0x80000000 | index, len(header) + len(encryptedBody))

        tag = hmacSha1(self.authKey, bytes(withIndexWord))[0:RTCP_AUTH_TAG_LEN]

        out = bytearray(len(withIndexWord) + RTCP_AUTH_TAG_LEN)
        out[0:len(withIndexWord)] = withIndexWord
        out[len(withIndexWord):] = tag
        return bytes(out)
