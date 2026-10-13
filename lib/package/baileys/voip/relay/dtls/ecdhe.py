from baileys.foundation.node_crypto import createECDH, createSign, createVerify
from baileys.foundation.node_runtime import JSObject


NAMED_CURVE_SECP256R1 = 23


def generateEcdheKeypair():
    ecdh = createECDH('prime256v1')
    ecdh.generateKeys()
    publicKeyPoint = ecdh.getPublicKey()

    def computeSharedSecret(peerPublicKeyPoint):
        return ecdh.computeSecret(peerPublicKeyPoint)

    return JSObject({'publicKeyPoint': publicKeyPoint, 'computeSharedSecret': computeSharedSecret})


def buildEcdheSignedParams(clientRandom, serverRandom, publicKeyPoint):
    return (clientRandom + serverRandom + bytes([3]) + bytes([0, NAMED_CURVE_SECP256R1])
            + bytes([len(publicKeyPoint)]) + publicKeyPoint)


def signEcdheParams(privateKey, signedParams):
    signer = createSign('SHA256')
    signer.update(signedParams)
    signer.end()
    return signer.sign(privateKey)


def verifyEcdheParams(peerPublicKey, signedParams, signature):
    verifier = createVerify('SHA256')
    verifier.update(signedParams)
    verifier.end()
    return verifier.verify(peerPublicKey, signature)
