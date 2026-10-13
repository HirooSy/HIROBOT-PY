from baileys.foundation.node_crypto import hkdfSync
from baileys.voip.shim.util import EMPTY_BYTES, toBytesView


def hkdf(ikm, salt, info, outLength):
    return toBytesView(hkdfSync('sha256', ikm, EMPTY_BYTES if salt is None else salt, info, outLength))
