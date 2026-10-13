from baileys_lite.foundation.node_crypto import createCipheriv, createHmac, randomBytes as nodeRandomBytes, randomInt as nodeRandomInt


def randomBytes(length):
    return nodeRandomBytes(length)


def randomInt(min, max):
    return nodeRandomInt(min, max)


def hmacSha1(key, *parts):
    hmac = createHmac('sha1', key)
    for part in parts:
        hmac.update(part)
    return hmac.digest()


def aesCtr128(key, iv, data):
    cipher = createCipheriv('aes-128-ctr', key, iv)
    output = cipher.update(data)
    cipher.final()
    return output
