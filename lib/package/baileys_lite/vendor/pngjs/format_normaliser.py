import math

from baileys_lite.foundation.node_runtime import JSError


def dePalette(indata, outdata, width, height, palette):
    pxPos = 0

    for y in range(height):
        for x in range(width):
            index = indata[pxPos]
            color = palette[index] if index < len(palette) else None

            if color is None:
                raise JSError("index " + str(indata[pxPos]) + " not in palette")

            for i in range(4):
                outdata[pxPos + i] = color[i]
            pxPos += 4


def replaceTransparentColor(indata, outdata, width, height, transColor):
    pxPos = 0
    for y in range(height):
        for x in range(width):
            makeTrans = False

            if len(transColor) == 1:
                if transColor[0] == indata[pxPos]:
                    makeTrans = True
            elif (
                transColor[0] == indata[pxPos]
                and transColor[1] == indata[pxPos + 1]
                and transColor[2] == indata[pxPos + 2]
            ):
                makeTrans = True
            if makeTrans:
                for i in range(4):
                    outdata[pxPos + i] = 0
            pxPos += 4


def scaleDepth(indata, outdata, width, height, depth):
    maxOutSample = 255
    maxInSample = 2 ** depth - 1
    pxPos = 0

    for y in range(height):
        for x in range(width):
            for i in range(4):
                outdata[pxPos + i] = math.floor(
                    (indata[pxPos + i] * maxOutSample) / maxInSample + 0.5
                )
            pxPos += 4


def formatNormaliser(indata, imageData, skipRescale=False):
    depth = imageData.get('depth')
    width = imageData.get('width')
    height = imageData.get('height')
    colorType = imageData.get('colorType')
    transColor = imageData.get('transColor')
    palette = imageData.get('palette')

    outdata = indata

    if colorType == 3:
        dePalette(indata, outdata, width, height, palette)
    else:
        if transColor is not None:
            replaceTransparentColor(indata, outdata, width, height, transColor)

        if depth != 8 and not skipRescale:
            if depth == 16:
                outdata = bytearray(width * height * 4)
            scaleDepth(indata, outdata, width, height, depth)
    return outdata


default = formatNormaliser
