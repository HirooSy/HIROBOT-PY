import functools
import struct

from baileys.foundation.node_runtime import JSError, JSObject
from baileys.vendor.pngjs.constants import default as constants
from baileys.vendor.pngjs.crc import default as CrcCalculator


def _noop(*args):
    return None


class Parser:
    def __init__(self, options, dependencies):
        self._options = options
        options['checkCRC'] = options.get('checkCRC') is not False

        self._hasIHDR = False
        self._hasIEND = False
        self._emittedHeadersFinished = False

        self._palette = []
        self._colorType = 0

        self._chunks = {}
        self._chunks[constants.get('TYPE_IHDR')] = self._handleIHDR
        self._chunks[constants.get('TYPE_IEND')] = self._handleIEND
        self._chunks[constants.get('TYPE_IDAT')] = self._handleIDAT
        self._chunks[constants.get('TYPE_PLTE')] = self._handlePLTE
        self._chunks[constants.get('TYPE_tRNS')] = self._handleTRNS
        self._chunks[constants.get('TYPE_gAMA')] = self._handleGAMA

        self.read = dependencies.get('read')
        self.error = dependencies.get('error')
        self.metadata = dependencies.get('metadata')
        self.gamma = dependencies.get('gamma')
        self.transColor = dependencies.get('transColor')
        self.palette = dependencies.get('palette')
        self.parsed = dependencies.get('parsed')
        self.inflateData = dependencies.get('inflateData')
        self.finished = dependencies.get('finished')
        self.simpleTransparency = dependencies.get('simpleTransparency')
        self.headersFinished = dependencies.get('headersFinished') or _noop

    def start(self):
        self.read(len(constants.get('PNG_SIGNATURE')), self._parseSignature)

    def _parseSignature(self, data):
        signature = constants.get('PNG_SIGNATURE')

        for i in range(len(signature)):
            if data[i] != signature[i]:
                self.error(JSError("Invalid file signature"))
                return
        self.read(8, self._parseChunkBegin)

    def _parseChunkBegin(self, data):
        length = struct.unpack_from('>I', data, 0)[0]

        type = struct.unpack_from('>I', data, 4)[0]
        name = ""
        for i in range(4, 8):
            name += chr(data[i])

        ancillary = bool(data[4] & 0x20)

        if not self._hasIHDR and type != constants.get('TYPE_IHDR'):
            self.error(JSError("Expected IHDR on beggining"))
            return

        self._crc = CrcCalculator()
        self._crc.write(name.encode('utf-8'))

        if self._chunks.get(type):
            return self._chunks[type](length)

        if not ancillary:
            self.error(JSError("Unsupported critical chunk type " + name))
            return

        self.read(length + 4, self._skipChunk)

    def _skipChunk(self, *args):
        self.read(8, self._parseChunkBegin)

    def _handleChunkEnd(self):
        self.read(4, self._parseChunkEnd)

    def _parseChunkEnd(self, data):
        fileCrc = struct.unpack_from('>i', data, 0)[0]
        calcCrc = self._crc.crc32()

        if self._options.get('checkCRC') and calcCrc != fileCrc:
            self.error(JSError("Crc error - " + str(fileCrc) + " - " + str(calcCrc)))
            return

        if not self._hasIEND:
            self.read(8, self._parseChunkBegin)

    def _handleIHDR(self, length):
        self.read(length, self._parseIHDR)

    def _parseIHDR(self, data):
        self._crc.write(data)

        width = struct.unpack_from('>I', data, 0)[0]
        height = struct.unpack_from('>I', data, 4)[0]
        depth = data[8]
        colorType = data[9]
        compr = data[10]
        filter = data[11]
        interlace = data[12]

        if depth not in (8, 4, 2, 1, 16):
            self.error(JSError("Unsupported bit depth " + str(depth)))
            return
        if colorType not in constants.get('COLORTYPE_TO_BPP_MAP'):
            self.error(JSError("Unsupported color type"))
            return
        if compr != 0:
            self.error(JSError("Unsupported compression method"))
            return
        if filter != 0:
            self.error(JSError("Unsupported filter method"))
            return
        if interlace != 0 and interlace != 1:
            self.error(JSError("Unsupported interlace method"))
            return

        self._colorType = colorType

        bpp = constants.get('COLORTYPE_TO_BPP_MAP')[self._colorType]

        self._hasIHDR = True

        self.metadata(JSObject({
            'width': width,
            'height': height,
            'depth': depth,
            'interlace': bool(interlace),
            'palette': bool(colorType & constants.get('COLORTYPE_PALETTE')),
            'color': bool(colorType & constants.get('COLORTYPE_COLOR')),
            'alpha': bool(colorType & constants.get('COLORTYPE_ALPHA')),
            'bpp': bpp,
            'colorType': colorType,
        }))

        self._handleChunkEnd()

    def _handlePLTE(self, length):
        self.read(length, self._parsePLTE)

    def _parsePLTE(self, data):
        self._crc.write(data)

        entries = len(data) // 3

        for i in range(entries):
            self._palette.append([data[i * 3], data[i * 3 + 1], data[i * 3 + 2], 0xff])

        self.palette(self._palette)

        self._handleChunkEnd()

    def _handleTRNS(self, length):
        self.simpleTransparency()
        self.read(length, self._parseTRNS)

    def _parseTRNS(self, data):
        self._crc.write(data)

        if self._colorType == constants.get('COLORTYPE_PALETTE_COLOR'):
            if len(self._palette) == 0:
                self.error(JSError("Transparency chunk must be after palette"))
                return
            if len(data) > len(self._palette):
                self.error(JSError("More transparent colors than palette size"))
                return
            for i in range(len(data)):
                self._palette[i][3] = data[i]
            self.palette(self._palette)

        if self._colorType == constants.get('COLORTYPE_GRAYSCALE'):
            self.transColor([struct.unpack_from('>H', data, 0)[0]])
        if self._colorType == constants.get('COLORTYPE_COLOR'):
            self.transColor([
                struct.unpack_from('>H', data, 0)[0],
                struct.unpack_from('>H', data, 2)[0],
                struct.unpack_from('>H', data, 4)[0],
            ])

        self._handleChunkEnd()

    def _handleGAMA(self, length):
        self.read(length, self._parseGAMA)

    def _parseGAMA(self, data):
        self._crc.write(data)
        self.gamma(struct.unpack_from('>I', data, 0)[0] / constants.get('GAMMA_DIVISION'))

        self._handleChunkEnd()

    def _handleIDAT(self, length):
        if not self._emittedHeadersFinished:
            self._emittedHeadersFinished = True
            self.headersFinished()
        self.read(-length, functools.partial(self._parseIDAT, length))

    def _parseIDAT(self, length, data):
        self._crc.write(data)

        if self._colorType == constants.get('COLORTYPE_PALETTE_COLOR') and len(self._palette) == 0:
            raise JSError("Expected palette not found")

        self.inflateData(data)
        leftOverLength = length - len(data)

        if leftOverLength > 0:
            self._handleIDAT(leftOverLength)
        else:
            self._handleChunkEnd()

    def _handleIEND(self, length):
        self.read(length, self._parseIEND)

    def _parseIEND(self, data):
        self._crc.write(data)

        self._hasIEND = True
        self._handleChunkEnd()

        if self.finished:
            self.finished()


default = Parser
