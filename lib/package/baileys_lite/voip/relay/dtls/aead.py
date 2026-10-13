from baileys_lite.foundation.node_crypto import createCipheriv, createDecipheriv
from baileys_lite.foundation.node_runtime import JSObject
from baileys_lite.voip.relay.dtls.record import encodeEpochSeq, DTLS_1_2_VERSION


GCM_TAG_LEN = 16
GCM_SALT_LEN = 4
GCM_EXPLICIT_NONCE_LEN = 8

aes128GcmParams = JSObject({'keyLen': 16, 'fixedIvLen': GCM_SALT_LEN})


def buildNonce(salt, epoch, sequenceNumber):
    return bytes(salt) + encodeEpochSeq(epoch, sequenceNumber)


def buildAdditionalData(epoch, sequenceNumber, type, plaintextLength):
    return (encodeEpochSeq(epoch, sequenceNumber) + bytes([type]) + DTLS_1_2_VERSION
            + plaintextLength.to_bytes(2, 'big'))


def encryptRecord(writeKey, salt, epoch, sequenceNumber, type, plaintext):
    nonce = buildNonce(salt, epoch, sequenceNumber)
    aad = buildAdditionalData(epoch, sequenceNumber, type, len(plaintext))
    cipher = createCipheriv('aes-128-gcm', writeKey, nonce, JSObject({'authTagLength': GCM_TAG_LEN}))
    cipher.setAAD(aad)
    ciphertext = cipher.update(plaintext) + cipher.final()
    tag = cipher.getAuthTag()
    explicitNonce = nonce[GCM_SALT_LEN:]
    return explicitNonce + ciphertext + tag


def decryptRecord(readKey, salt, epoch, sequenceNumber, type, fragment):
    explicitNonce = bytes(fragment[0:GCM_EXPLICIT_NONCE_LEN])
    tag = bytes(fragment[len(fragment) - GCM_TAG_LEN:])
    ciphertext = bytes(fragment[GCM_EXPLICIT_NONCE_LEN:len(fragment) - GCM_TAG_LEN])
    nonce = bytes(salt) + explicitNonce
    aad = buildAdditionalData(epoch, sequenceNumber, type, len(ciphertext))
    decipher = createDecipheriv('aes-128-gcm', readKey, nonce, JSObject({'authTagLength': GCM_TAG_LEN}))
    decipher.setAAD(aad)
    decipher.setAuthTag(tag)
    return decipher.update(ciphertext) + decipher.final()
