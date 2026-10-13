import zlib

from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.vendor.pngjs.constants import default as constants
from baileys_lite.vendor.pngjs.packer import default as Packer


def deflateSync(data, opts):
    compressor = zlib.compressobj(opts.get('level'), zlib.DEFLATED, zlib.MAX_WBITS, 8, opts.get('strategy'))
    return compressor.compress(bytes(data)) + compressor.flush()


def packSync(metaData, opt):
    options = JSObject(opt or {})

    packer = Packer(options)

    chunks = []

    chunks.append(bytes(constants.get('PNG_SIGNATURE')))

    chunks.append(packer.packIHDR(metaData.get('width'), metaData.get('height')))

    if metaData.get('gamma'):
        chunks.append(packer.packGAMA(metaData.get('gamma')))

    filteredData = packer.filterData(metaData.get('data'), metaData.get('width'), metaData.get('height'))

    compressedData = deflateSync(filteredData, packer.getDeflateOptions())

    if not compressedData:
        raise JSError("bad png - invalid compressed data response")
    chunks.append(packer.packIDAT(compressedData))

    chunks.append(packer.packIEND())

    return b''.join(chunks)


default = packSync
