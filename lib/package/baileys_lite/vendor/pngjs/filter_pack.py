from baileys_lite.foundation.node_runtime import JSError
from baileys_lite.vendor.pngjs.paeth_predictor import default as paethPredictor


def filterNone(pxData, pxPos, byteWidth, rawData, rawPos, bpp=None):
    for x in range(byteWidth):
        rawData[rawPos + x] = pxData[pxPos + x]


def filterSumNone(pxData, pxPos, byteWidth, bpp=None):
    sum = 0
    length = pxPos + byteWidth

    for i in range(pxPos, length):
        sum += abs(pxData[i])
    return sum


def filterSub(pxData, pxPos, byteWidth, rawData, rawPos, bpp):
    for x in range(byteWidth):
        left = pxData[pxPos + x - bpp] if x >= bpp else 0
        val = pxData[pxPos + x] - left

        rawData[rawPos + x] = val & 0xff


def filterSumSub(pxData, pxPos, byteWidth, bpp):
    sum = 0
    for x in range(byteWidth):
        left = pxData[pxPos + x - bpp] if x >= bpp else 0
        val = pxData[pxPos + x] - left

        sum += abs(val)

    return sum


def filterUp(pxData, pxPos, byteWidth, rawData, rawPos, bpp=None):
    for x in range(byteWidth):
        up = pxData[pxPos + x - byteWidth] if pxPos > 0 else 0
        val = pxData[pxPos + x] - up

        rawData[rawPos + x] = val & 0xff


def filterSumUp(pxData, pxPos, byteWidth, bpp=None):
    sum = 0
    length = pxPos + byteWidth
    for x in range(pxPos, length):
        up = pxData[x - byteWidth] if pxPos > 0 else 0
        val = pxData[x] - up

        sum += abs(val)

    return sum


def filterAvg(pxData, pxPos, byteWidth, rawData, rawPos, bpp):
    for x in range(byteWidth):
        left = pxData[pxPos + x - bpp] if x >= bpp else 0
        up = pxData[pxPos + x - byteWidth] if pxPos > 0 else 0
        val = pxData[pxPos + x] - ((left + up) >> 1)

        rawData[rawPos + x] = val & 0xff


def filterSumAvg(pxData, pxPos, byteWidth, bpp):
    sum = 0
    for x in range(byteWidth):
        left = pxData[pxPos + x - bpp] if x >= bpp else 0
        up = pxData[pxPos + x - byteWidth] if pxPos > 0 else 0
        val = pxData[pxPos + x] - ((left + up) >> 1)

        sum += abs(val)

    return sum


def filterPaeth(pxData, pxPos, byteWidth, rawData, rawPos, bpp):
    for x in range(byteWidth):
        left = pxData[pxPos + x - bpp] if x >= bpp else 0
        up = pxData[pxPos + x - byteWidth] if pxPos > 0 else 0
        upleft = pxData[pxPos + x - (byteWidth + bpp)] if (pxPos > 0 and x >= bpp) else 0
        val = pxData[pxPos + x] - paethPredictor(left, up, upleft)

        rawData[rawPos + x] = val & 0xff


def filterSumPaeth(pxData, pxPos, byteWidth, bpp):
    sum = 0
    for x in range(byteWidth):
        left = pxData[pxPos + x - bpp] if x >= bpp else 0
        up = pxData[pxPos + x - byteWidth] if pxPos > 0 else 0
        upleft = pxData[pxPos + x - (byteWidth + bpp)] if (pxPos > 0 and x >= bpp) else 0
        val = pxData[pxPos + x] - paethPredictor(left, up, upleft)

        sum += abs(val)

    return sum


filters = {
    0: filterNone,
    1: filterSub,
    2: filterUp,
    3: filterAvg,
    4: filterPaeth,
}

filterSums = {
    0: filterSumNone,
    1: filterSumSub,
    2: filterSumUp,
    3: filterSumAvg,
    4: filterSumPaeth,
}


def filterData(pxData, width, height, options, bpp):
    if 'filterType' not in options or options.get('filterType') == -1:
        filterTypes = [0, 1, 2, 3, 4]
    elif isinstance(options.get('filterType'), (int, float)) and not isinstance(options.get('filterType'), bool):
        filterTypes = [options.get('filterType')]
    else:
        raise JSError("unrecognised filter types")

    if options.get('bitDepth') == 16:
        bpp *= 2
    byteWidth = width * bpp
    rawPos = 0
    pxPos = 0
    rawData = bytearray((byteWidth + 1) * height)

    sel = filterTypes[0]

    for y in range(height):
        if len(filterTypes) > 1:

            min = float('inf')

            for i in range(len(filterTypes)):
                sum = filterSums[filterTypes[i]](pxData, pxPos, byteWidth, bpp)
                if sum < min:
                    sel = filterTypes[i]
                    min = sum

        rawData[rawPos] = sel
        rawPos += 1
        filters[sel](pxData, pxPos, byteWidth, rawData, rawPos, bpp)
        rawPos += byteWidth
        pxPos += byteWidth
    return rawData


default = filterData
