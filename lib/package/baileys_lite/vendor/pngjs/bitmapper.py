from baileys_lite.foundation.node_runtime import JSError, JSObject
import baileys_lite.vendor.pngjs.interlace as interlaceUtils


def _mapper1(pxData, data, pxPos, rawPos):
    if rawPos == len(data):
        raise JSError("Ran out of data")

    pixel = data[rawPos]
    pxData[pxPos] = pixel
    pxData[pxPos + 1] = pixel
    pxData[pxPos + 2] = pixel
    pxData[pxPos + 3] = 0xff


def _mapper2(pxData, data, pxPos, rawPos):
    if rawPos + 1 >= len(data):
        raise JSError("Ran out of data")

    pixel = data[rawPos]
    pxData[pxPos] = pixel
    pxData[pxPos + 1] = pixel
    pxData[pxPos + 2] = pixel
    pxData[pxPos + 3] = data[rawPos + 1]


def _mapper3(pxData, data, pxPos, rawPos):
    if rawPos + 2 >= len(data):
        raise JSError("Ran out of data")

    pxData[pxPos] = data[rawPos]
    pxData[pxPos + 1] = data[rawPos + 1]
    pxData[pxPos + 2] = data[rawPos + 2]
    pxData[pxPos + 3] = 0xff


def _mapper4(pxData, data, pxPos, rawPos):
    if rawPos + 3 >= len(data):
        raise JSError("Ran out of data")

    pxData[pxPos] = data[rawPos]
    pxData[pxPos + 1] = data[rawPos + 1]
    pxData[pxPos + 2] = data[rawPos + 2]
    pxData[pxPos + 3] = data[rawPos + 3]


pixelBppMapper = [
    lambda *args: None,
    _mapper1,
    _mapper2,
    _mapper3,
    _mapper4,
]


def _customMapper1(pxData, pixelData, pxPos, maxBit):
    pixel = pixelData[0]
    pxData[pxPos] = pixel
    pxData[pxPos + 1] = pixel
    pxData[pxPos + 2] = pixel
    pxData[pxPos + 3] = maxBit


def _customMapper2(pxData, pixelData, pxPos, maxBit=None):
    pixel = pixelData[0]
    pxData[pxPos] = pixel
    pxData[pxPos + 1] = pixel
    pxData[pxPos + 2] = pixel
    pxData[pxPos + 3] = pixelData[1]


def _customMapper3(pxData, pixelData, pxPos, maxBit):
    pxData[pxPos] = pixelData[0]
    pxData[pxPos + 1] = pixelData[1]
    pxData[pxPos + 2] = pixelData[2]
    pxData[pxPos + 3] = maxBit


def _customMapper4(pxData, pixelData, pxPos, maxBit=None):
    pxData[pxPos] = pixelData[0]
    pxData[pxPos + 1] = pixelData[1]
    pxData[pxPos + 2] = pixelData[2]
    pxData[pxPos + 3] = pixelData[3]


pixelBppCustomMapper = [
    lambda *args: None,
    _customMapper1,
    _customMapper2,
    _customMapper3,
    _customMapper4,
]


def bitRetriever(data, depth):
    leftOver = []
    i = 0

    def split():
        nonlocal i
        if i == len(data):
            raise JSError("Ran out of data")
        byte = data[i]
        i += 1
        if depth == 16:
            byte2 = data[i]
            i += 1
            leftOver.append((byte << 8) + byte2)
        elif depth == 4:
            byte2 = byte & 0x0f
            byte1 = byte >> 4
            leftOver.extend([byte1, byte2])
        elif depth == 2:
            byte4 = byte & 3
            byte3 = (byte >> 2) & 3
            byte2 = (byte >> 4) & 3
            byte1 = (byte >> 6) & 3
            leftOver.extend([byte1, byte2, byte3, byte4])
        elif depth == 1:
            byte8 = byte & 1
            byte7 = (byte >> 1) & 1
            byte6 = (byte >> 2) & 1
            byte5 = (byte >> 3) & 1
            byte4 = (byte >> 4) & 1
            byte3 = (byte >> 5) & 1
            byte2 = (byte >> 6) & 1
            byte1 = (byte >> 7) & 1
            leftOver.extend([byte1, byte2, byte3, byte4, byte5, byte6, byte7, byte8])
        else:
            raise JSError("unrecognised depth")

    def get(count):
        nonlocal leftOver
        while len(leftOver) < count:
            split()
        returner = leftOver[:count]
        leftOver = leftOver[count:]
        return returner

    def resetAfterLine():
        del leftOver[:]

    def end():
        if i != len(data):
            raise JSError("extra data found")

    return JSObject({
        'get': get,
        'resetAfterLine': resetAfterLine,
        'end': end,
    })


def mapImage8Bit(image, pxData, getPxPos, bpp, data, rawPos):
    imageWidth = image.get('width')
    imageHeight = image.get('height')
    imagePass = image.get('index')
    for y in range(imageHeight):
        for x in range(imageWidth):
            pxPos = getPxPos(x, y, imagePass)
            pixelBppMapper[bpp](pxData, data, pxPos, rawPos)
            rawPos += bpp
    return rawPos


def mapImageCustomBit(image, pxData, getPxPos, bpp, bits, maxBit):
    imageWidth = image.get('width')
    imageHeight = image.get('height')
    imagePass = image.get('index')
    for y in range(imageHeight):
        for x in range(imageWidth):
            pixelData = bits.get('get')(bpp)
            pxPos = getPxPos(x, y, imagePass)
            pixelBppCustomMapper[bpp](pxData, pixelData, pxPos, maxBit)
        bits.get('resetAfterLine')()


def dataToBitMap(data, bitmapInfo):
    width = bitmapInfo.get('width')
    height = bitmapInfo.get('height')
    depth = bitmapInfo.get('depth')
    bpp = bitmapInfo.get('bpp')
    interlace = bitmapInfo.get('interlace')
    bits = None

    if depth != 8:
        bits = bitRetriever(data, depth)
    if depth <= 8:
        pxData = bytearray(width * height * 4)
    else:
        pxData = [0] * (width * height * 4)
    maxBit = 2 ** depth - 1
    rawPos = 0

    if interlace:
        images = interlaceUtils.getImagePasses(width, height)
        getPxPos = interlaceUtils.getInterlaceIterator(width)
    else:
        nonInterlacedPxPos = 0

        def getPxPos(*args):
            nonlocal nonInterlacedPxPos
            returner = nonInterlacedPxPos
            nonInterlacedPxPos += 4
            return returner

        images = [JSObject({'width': width, 'height': height})]

    for imageIndex in range(len(images)):
        if depth == 8:
            rawPos = mapImage8Bit(images[imageIndex], pxData, getPxPos, bpp, data, rawPos)
        else:
            mapImageCustomBit(images[imageIndex], pxData, getPxPos, bpp, bits, maxBit)

    if depth == 8:
        if rawPos != len(data):
            raise JSError("extra data found")
    else:
        bits.get('end')()

    return pxData
