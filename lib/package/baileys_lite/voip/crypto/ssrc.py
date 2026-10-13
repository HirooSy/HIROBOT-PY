from baileys_lite.voip.shim.crypto import hkdf
from baileys_lite.voip.bytes import readUInt32LE, writeUInt32LE


def generateSecureSsrc(callId, selfJid, counter=0):
    key = callId.encode('utf-8')
    salt = bytearray(4)
    writeUInt32LE(salt, counter, 0)
    info = selfJid.encode('utf-8')
    result = hkdf(key, bytes(salt), info, 4)
    return readUInt32LE(result, 0)


WASM_RELAY_STREAM_SLOT_WORDS = [0, 1, 4, 2, 3, 5, 7, 8, 6]


def deriveWasmRelayStreamSsrcs(callId, participantId):
    return [generateSecureSsrc(callId, participantId, slotWord) for slotWord in WASM_RELAY_STREAM_SLOT_WORDS]
