import zlib

from baileys.foundation.node_runtime import JSError, JSObject
import baileys.vendor.pngjs.filter_parse_sync as FilterSync
from baileys.vendor.pngjs.parser import default as Parser
from baileys.vendor.pngjs.sync_inflate import default as inflateSync
from baileys.vendor.pngjs.sync_reader import default as SyncReader
import baileys.vendor.pngjs.bitmapper as bitmapper
from baileys.vendor.pngjs.format_normaliser import default as formatNormaliser


def parseSync(buffer, options):
    err = None

    metaData = None

    def handleError(_err_):
        nonlocal err
        err = _err_

    def handleMetaData(_metaData_):
        nonlocal metaData
        metaData = _metaData_

    def handleTransColor(transColor):
        metaData['transColor'] = transColor

    def handlePalette(palette):
        metaData['palette'] = palette

    def handleSimpleTransparency():
        metaData['alpha'] = True

    gamma = None

    def handleGamma(_gamma_):
        nonlocal gamma
        gamma = _gamma_

    inflateDataList = []

    def handleInflateData(inflatedData):
        inflateDataList.append(inflatedData)

    reader = SyncReader(buffer)

    parser = Parser(options, JSObject({
        'read': reader.read,
        'error': handleError,
        'metadata': handleMetaData,
        'gamma': handleGamma,
        'palette': handlePalette,
        'transColor': handleTransColor,
        'inflateData': handleInflateData,
        'simpleTransparency': handleSimpleTransparency,
    }))

    parser.start()
    reader.process()

    if err is not None:
        raise err

    inflateData = b''.join(inflateDataList)
    inflateDataList.clear()

    if metaData.get('interlace'):
        inflatedData = zlib.decompress(inflateData)
    else:
        rowSize = ((metaData.get('width') * metaData.get('bpp') * metaData.get('depth') + 7) >> 3) + 1
        imageSize = rowSize * metaData.get('height')
        inflatedData = inflateSync(inflateData, JSObject({
            'chunkSize': imageSize,
            'maxLength': imageSize,
        }))

    if not inflatedData:
        raise JSError("bad png - invalid inflate data response")

    unfilteredData = FilterSync.process(inflatedData, metaData)

    bitmapData = bitmapper.dataToBitMap(unfilteredData, metaData)

    normalisedBitmapData = formatNormaliser(bitmapData, metaData, options.get('skipRescale'))

    metaData['data'] = normalisedBitmapData
    metaData['gamma'] = gamma or 0

    return metaData


default = parseSync
