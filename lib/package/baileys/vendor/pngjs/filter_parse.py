import math

from baileys.foundation.node_runtime import JSError, JSObject
import baileys.vendor.pngjs.interlace as interlaceUtils
from baileys.vendor.pngjs.paeth_predictor import default as paethPredictor


def getByteWidth(width, bpp, depth):
    byteWidth = width * bpp
    if depth != 8:
        byteWidth = math.ceil(byteWidth / (8 / depth))
    return byteWidth


class Filter:
    def __init__(self, bitmapInfo, dependencies):
        width = bitmapInfo.get('width')
        height = bitmapInfo.get('height')
        interlace = bitmapInfo.get('interlace')
        bpp = bitmapInfo.get('bpp')
        depth = bitmapInfo.get('depth')

        self.read = dependencies.get('read')
        self.write = dependencies.get('write')
        self.complete = dependencies.get('complete')

        self._imageIndex = 0
        self._images = []
        self._lastLine = None
        if interlace:
            passes = interlaceUtils.getImagePasses(width, height)
            for i in range(len(passes)):
                self._images.append(JSObject({
                    'byteWidth': getByteWidth(passes[i].get('width'), bpp, depth),
                    'height': passes[i].get('height'),
                    'lineIndex': 0,
                }))
        else:
            self._images.append(JSObject({
                'byteWidth': getByteWidth(width, bpp, depth),
                'height': height,
                'lineIndex': 0,
            }))

        if depth == 8:
            self._xComparison = bpp
        elif depth == 16:
            self._xComparison = bpp * 2
        else:
            self._xComparison = 1

    def _imageAt(self):
        if self._imageIndex < len(self._images):
            return self._images[self._imageIndex]
        return None

    def start(self):
        self.read(self._imageAt()['byteWidth'] + 1, self._reverseFilterLine)

    def _unFilterType1(self, rawData, unfilteredLine, byteWidth):
        xComparison = self._xComparison
        xBiggerThan = xComparison - 1

        for x in range(byteWidth):
            rawByte = rawData[1 + x]
            f1Left = unfilteredLine[x - xComparison] if x > xBiggerThan else 0
            unfilteredLine[x] = (rawByte + f1Left) & 0xff

    def _unFilterType2(self, rawData, unfilteredLine, byteWidth):
        lastLine = self._lastLine

        for x in range(byteWidth):
            rawByte = rawData[1 + x]
            f2Up = lastLine[x] if lastLine is not None else 0
            unfilteredLine[x] = (rawByte + f2Up) & 0xff

    def _unFilterType3(self, rawData, unfilteredLine, byteWidth):
        xComparison = self._xComparison
        xBiggerThan = xComparison - 1
        lastLine = self._lastLine

        for x in range(byteWidth):
            rawByte = rawData[1 + x]
            f3Up = lastLine[x] if lastLine is not None else 0
            f3Left = unfilteredLine[x - xComparison] if x > xBiggerThan else 0
            f3Add = (f3Left + f3Up) // 2
            unfilteredLine[x] = (rawByte + f3Add) & 0xff

    def _unFilterType4(self, rawData, unfilteredLine, byteWidth):
        xComparison = self._xComparison
        xBiggerThan = xComparison - 1
        lastLine = self._lastLine

        for x in range(byteWidth):
            rawByte = rawData[1 + x]
            f4Up = lastLine[x] if lastLine is not None else 0
            f4Left = unfilteredLine[x - xComparison] if x > xBiggerThan else 0
            f4UpLeft = lastLine[x - xComparison] if (x > xBiggerThan and lastLine is not None) else 0
            f4Add = paethPredictor(f4Left, f4Up, f4UpLeft)
            unfilteredLine[x] = (rawByte + f4Add) & 0xff

    def _reverseFilterLine(self, rawData):
        filter = rawData[0]
        unfilteredLine = None
        currentImage = self._imageAt()
        byteWidth = currentImage.get('byteWidth')

        if filter == 0:
            unfilteredLine = bytes(rawData[1:byteWidth + 1])
        else:
            unfilteredLine = bytearray(byteWidth)

            if filter == 1:
                self._unFilterType1(rawData, unfilteredLine, byteWidth)
            elif filter == 2:
                self._unFilterType2(rawData, unfilteredLine, byteWidth)
            elif filter == 3:
                self._unFilterType3(rawData, unfilteredLine, byteWidth)
            elif filter == 4:
                self._unFilterType4(rawData, unfilteredLine, byteWidth)
            else:
                raise JSError("Unrecognised filter type - " + str(filter))

        self.write(unfilteredLine)

        currentImage['lineIndex'] += 1
        if currentImage.get('lineIndex') >= currentImage.get('height'):
            self._lastLine = None
            self._imageIndex += 1
            currentImage = self._imageAt()
        else:
            self._lastLine = unfilteredLine

        if currentImage is not None:
            self.read(currentImage.get('byteWidth') + 1, self._reverseFilterLine)
        else:
            self._lastLine = None
            self.complete()


default = Filter
