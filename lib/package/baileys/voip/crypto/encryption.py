from baileys.foundation.node_runtime import JSObject
from baileys.voip.shim.crypto import hkdf
from baileys.voip.crypto.primitives import randomBytes


def derivePerJidSrtpKey(callKey, deviceJid):
    output = hkdf(callKey, b'', deviceJid.encode('utf-8'), 46)
    return JSObject({
        'masterKey': output[0:16],
        'masterSalt': output[16:30]
    })


def generateCallKey():
    return randomBytes(32)
