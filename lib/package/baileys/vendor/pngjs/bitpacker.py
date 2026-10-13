import math
import struct
import sys

from baileys.foundation.node_runtime import JSError, JSObject
from baileys.vendor.pngjs.constants import default as constants


def _u8(value):
    return int(value) & 0xff


def _writeU16BE(buf, index, value):
    v = int(value)
    buf[index] = (v >> 8) & 0xff
    buf[index + 1] = v & 0xff


def _uint16Values(dataIn):
    if isinstance(dataIn, list):
        return list(dataIn)
    count = len(dataIn) // 2
    return list(struct.unpack('<%dH' % count, bytes(dataIn[:count * 2])))


def _isNumber(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def bitPacker(dataIn, width, height, options):
    outHasAlpha = options.get('colorType') in (constants.get('COLORTYPE_COLOR_ALPHA'), constants.get('COLORTYPE_ALPHA'))
    if options.get('colorType') == options.get('inputColorType'):
        bigEndian = sys.byteorder == 'big'

        if options.get('bitDepth') == 8 or (options.get('bitDepth') == 16 and bigEndian):
            return dataIn

    if options.get('bitDepth') != 16:
        data = dataIn
    else:
        data = _uint16Values(dataIn)

    maxValue = 255
    inBpp = constants.get('COLORTYPE_TO_BPP_MAP')[options.get('inputColorType')]
    if inBpp == 4 and not options.get('inputHasAlpha'):
        inBpp = 3
    outBpp = constants.get('COLORTYPE_TO_BPP_MAP')[options.get('colorType')]
    if options.get('bitDepth') == 16:
        maxValue = 65535
        outBpp *= 2
    outData = bytearray(width * height * outBpp)

    inIndex = 0
    outIndex = 0

    bgColor = JSObject(options.get('bgColor') or {})
    if bgColor.get('red') is None:
        bgColor['red'] = maxValue
    if bgColor.get('green') is None:
        bgColor['green'] = maxValue
    if bgColor.get('blue') is None:
        bgColor['blue'] = maxValue

    def getRGBA():
        alpha = maxValue
        inputColorType = options.get('inputColorType')
        if inputColorType == constants.get('COLORTYPE_COLOR_ALPHA'):
            alpha = data[inIndex + 3]
            red = data[inIndex]
            green = data[inIndex + 1]
            blue = data[inIndex + 2]
        elif inputColorType == constants.get('COLORTYPE_COLOR'):
            red = data[inIndex]
            green = data[inIndex + 1]
            blue = data[inIndex + 2]
        elif inputColorType == constants.get('COLORTYPE_ALPHA'):
            alpha = data[inIndex + 1]
            red = data[inIndex]
            green = red
            blue = red
        elif inputColorType == constants.get('COLORTYPE_GRAYSCALE'):
            red = data[inIndex]
            green = red
            blue = red
        else:
            raise JSError(
                "input color type:" + str(inputColorType) + " is not supported at present"
            )

        if options.get('inputHasAlpha'):
            if not outHasAlpha:
                alpha /= maxValue
                red = min(max(math.floor((1 - alpha) * bgColor.get('red') + alpha * red + 0.5), 0), maxValue)
                green = min(max(math.floor((1 - alpha) * bgColor.get('green') + alpha * green + 0.5), 0), maxValue)
                blue = min(max(math.floor((1 - alpha) * bgColor.get('blue') + alpha * blue + 0.5), 0), maxValue)
        return JSObject({'red': red, 'green': green, 'blue': blue, 'alpha': alpha})

    for y in range(height):
        for x in range(width):
            rgba = getRGBA()

            colorType = options.get('colorType')
            if colorType in (constants.get('COLORTYPE_COLOR_ALPHA'), constants.get('COLORTYPE_COLOR')):
                if options.get('bitDepth') == 8:
                    outData[outIndex] = _u8(rgba.get('red'))
                    outData[outIndex + 1] = _u8(rgba.get('green'))
                    outData[outIndex + 2] = _u8(rgba.get('blue'))
                    if outHasAlpha:
                        outData[outIndex + 3] = _u8(rgba.get('alpha'))
                else:
                    _writeU16BE(outData, outIndex, rgba.get('red'))
                    _writeU16BE(outData, outIndex + 2, rgba.get('green'))
                    _writeU16BE(outData, outIndex + 4, rgba.get('blue'))
                    if outHasAlpha:
                        _writeU16BE(outData, outIndex + 6, rgba.get('alpha'))
            elif colorType in (constants.get('COLORTYPE_ALPHA'), constants.get('COLORTYPE_GRAYSCALE')):
                grayscale = (rgba.get('red') + rgba.get('green') + rgba.get('blue')) / 3
                if options.get('bitDepth') == 8:
                    outData[outIndex] = _u8(grayscale)
                    if outHasAlpha:
                        outData[outIndex + 1] = _u8(rgba.get('alpha'))
                else:
                    _writeU16BE(outData, outIndex, grayscale)
                    if outHasAlpha:
                        _writeU16BE(outData, outIndex + 2, rgba.get('alpha'))
            else:
                raise JSError("unrecognised color Type " + str(colorType))

            inIndex += inBpp
            outIndex += outBpp

    return outData


default = bitPacker
