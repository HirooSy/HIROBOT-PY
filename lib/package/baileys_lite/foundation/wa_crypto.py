from baileys_lite.foundation.node_crypto import createHash, hkdfSync
from baileys_lite.foundation.node_runtime import JSError, JSObject


def md5(buffer):
    return createHash('md5').update(buffer).digest()


def hkdf(buffer, expandedLength, options=None):
    if options is None:
        options = JSObject({})
    salt = options.get('salt')
    info = options.get('info')
    if expandedLength > 255 * 32:
        raise JSError('HKDF expansion failed')
    if expandedLength == 0:
        return b''
    saltBytes = salt if salt is not None else b''
    if isinstance(info, str):
        infoBytes = info.encode('utf-8')
    elif info is None:
        infoBytes = b''
    else:
        infoBytes = bytes(info)
    return bytes(hkdfSync('sha256', buffer, saltBytes, infoBytes, expandedLength))


def expandAppStateKeys(keyData):
    expanded = hkdf(keyData, 160, JSObject({'info': 'WhatsApp Mutation Keys'}))

    def part(i):
        return expanded[i * 32:i * 32 + 32]
    return JSObject({
        'indexKey': part(0),
        'valueEncryptionKey': part(1),
        'valueMacKey': part(2),
        'snapshotMacKey': part(3),
        'patchMacKey': part(4)
    })


class LTHashAntiTampering:
    def __init__(self):
        self.salt = 'WhatsApp Patch Integrity'

    def subtractThenAdd(self, base, subtract, add):
        if len(base) != 128:
            raise JSError(f'Base hash must be 128 bytes, got {len(base)}')
        view = bytes(base)
        lanes = [int.from_bytes(view[i * 2:i * 2 + 2], 'little') for i in range(64)]

        def apply(items, sign):
            for item in items:
                ex = hkdf(item, 128, JSObject({'info': self.salt}))
                for i in range(64):
                    lanes[i] = (lanes[i] + sign * int.from_bytes(ex[i * 2:i * 2 + 2], 'little')) & 0xFFFF
        apply(subtract, -1)
        apply(add, +1)
        out = bytearray(128)
        for i in range(64):
            out[i * 2:i * 2 + 2] = lanes[i].to_bytes(2, 'little')
        return bytes(out)
