import math
import struct

from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.vendor.pngjs.constants import default as constants
from baileys_lite.vendor.pngjs.crc import default as CrcStream
from baileys_lite.vendor.pngjs.bitpacker import default as bitPacker
from baileys_lite.vendor.pngjs.filter_pack import default as filter


def _isNumber(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class Packer:
    def __init__(self, options):
        self._options = options

        options['deflateChunkSize'] = options.get('deflateChunkSize') or 32 * 1024
        options['deflateLevel'] = options.get('deflateLevel') if options.get('deflateLevel') is not None else 9
        options['deflateStrategy'] = options.get('deflateStrategy') if options.get('deflateStrategy') is not None else 3
        options['inputHasAlpha'] = options.get('inputHasAlpha') if options.get('inputHasAlpha') is not None else True
        options['bitDepth'] = options.get('bitDepth') or 8

        options['colorType'] = options.get('colorType') if _isNumber(options.get('colorType')) else constants.get('COLORTYPE_COLOR_ALPHA')
        options['inputColorType'] = options.get('inputColorType') if _isNumber(options.get('inputColorType')) else constants.get('COLORTYPE_COLOR_ALPHA')

        supported = [
            constants.get('COLORTYPE_GRAYSCALE'),
            constants.get('COLORTYPE_COLOR'),
            constants.get('COLORTYPE_COLOR_ALPHA'),
            constants.get('COLORTYPE_ALPHA'),
        ]
        if options.get('colorType') not in supported:
            raise JSError(
                "option color type:" + str(options.get('colorType')) + " is not supported at present"
            )
        if options.get('inputColorType') not in supported:
            raise JSError(
                "option input color type:" + str(options.get('inputColorType')) + " is not supported at present"
            )
        if options.get('bitDepth') != 8 and options.get('bitDepth') != 16:
            raise JSError(
                "option bit depth:" + str(options.get('bitDepth')) + " is not supported at present"
            )

    def getDeflateOptions(self):
        return JSObject({
            'chunkSize': self._options.get('deflateChunkSize'),
            'level': self._options.get('deflateLevel'),
            'strategy': self._options.get('deflateStrategy'),
        })

    def filterData(self, data, width, height):
        packedData = bitPacker(data, width, height, self._options)

        bpp = constants.get('COLORTYPE_TO_BPP_MAP')[self._options.get('colorType')]
        filteredData = filter(packedData, width, height, self._options, bpp)
        return filteredData

    def _packChunk(self, type, data):
        length = len(data) if data is not None else 0
        buf = bytearray(length + 12)

        struct.pack_into('>I', buf, 0, length)
        struct.pack_into('>I', buf, 4, type)

        if data is not None:
            buf[8:8 + length] = data

        struct.pack_into('>i', buf, len(buf) - 4, CrcStream.crc32(buf[4:len(buf) - 4]))
        return bytes(buf)

    def packGAMA(self, gamma):
        buf = bytearray(4)
        struct.pack_into('>I', buf, 0, math.floor(gamma * constants.get('GAMMA_DIVISION')))
        return self._packChunk(constants.get('TYPE_gAMA'), bytes(buf))

    def packIHDR(self, width, height):
        buf = bytearray(13)
        struct.pack_into('>II', buf, 0, width, height)
        buf[8] = self._options.get('bitDepth')
        buf[9] = self._options.get('colorType')
        buf[10] = 0
        buf[11] = 0
        buf[12] = 0

        return self._packChunk(constants.get('TYPE_IHDR'), bytes(buf))

    def packIDAT(self, data):
        return self._packChunk(constants.get('TYPE_IDAT'), data)

    def packIEND(self):
        return self._packChunk(constants.get('TYPE_IEND'), None)


default = Packer
