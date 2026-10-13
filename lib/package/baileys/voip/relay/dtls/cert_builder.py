import datetime

from baileys.foundation.node_crypto import generateKeyPairSync, createSign, randomBytes
from baileys.foundation.node_runtime import JSObject, now
from baileys.voip.relay.dtls.der import der, readTlv


EC_PUBLIC_KEY_OID = '1.2.840.10045.2.1'
PRIME256V1_OID = '1.2.840.10045.3.1.7'
ECDSA_WITH_SHA256_OID = '1.2.840.10045.4.3.2'
COMMON_NAME_OID = '2.5.4.3'


def rdnSequence(commonName):
    return der.get('sequence')(
        der.get('set')(
            der.get('sequence')(
                der.get('objectIdentifier')(COMMON_NAME_OID),
                der.get('utf8String')(commonName),
            ),
        ),
    )


def algorithmIdentifierEcdsaSha256():
    return der.get('sequence')(der.get('objectIdentifier')(ECDSA_WITH_SHA256_OID))


def msToDate(ms):
    return datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)


def generateSelfSignedCert(commonName='wa-voip'):
    keyPair = generateKeyPairSync('ec', JSObject({'namedCurve': 'prime256v1'}))
    publicKey = keyPair.get('publicKey')
    privateKey = keyPair.get('privateKey')
    spkiDer = publicKey.export(JSObject({'format': 'der', 'type': 'spki'}))

    serialNumber = randomBytes(16)
    notBefore = msToDate(now() - 24 * 60 * 60 * 1000)
    notAfter = msToDate(now() + 24 * 60 * 60 * 1000)

    tbsCertificate = der.get('sequence')(
        der.get('contextConstructed')(0, der.get('integer')(2)),
        der.get('integer')(serialNumber),
        algorithmIdentifierEcdsaSha256(),
        rdnSequence(commonName),
        der.get('sequence')(der.get('utcTime')(notBefore), der.get('utcTime')(notAfter)),
        rdnSequence(commonName),
        spkiDer,
    )

    signer = createSign('SHA256')
    signer.update(tbsCertificate)
    signer.end()
    signature = signer.sign(privateKey)

    certDer = der.get('sequence')(
        tbsCertificate,
        algorithmIdentifierEcdsaSha256(),
        der.get('bitString')(signature),
    )

    spkiParsed = spkiOuterBitStringContent(spkiDer)
    publicKeyPoint = spkiParsed[1:]

    return JSObject({'certDer': certDer, 'privateKey': privateKey, 'publicKeyPoint': publicKeyPoint})


def spkiOuterBitStringContent(spkiDer):
    tag = spkiDer[0]
    if tag != 0x30:
        raise Exception('generateSelfSignedCert: unexpected SPKI outer tag')
    outer = readTlv(spkiDer, 0)
    algId = readTlv(outer.get('content'), 0)
    bitString = readTlv(outer.get('content'), algId.get('next'))
    if bitString.get('tag') != 0x03:
        raise Exception('generateSelfSignedCert: expected BIT STRING after AlgorithmIdentifier')
    return bitString.get('content')
