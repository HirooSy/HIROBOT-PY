from baileys.foundation.node_runtime import JSObject
from baileys.vendor.pngjs.filter_parse import default as Filter
from baileys.vendor.pngjs.sync_reader import default as SyncReader


def process(inBuffer, bitmapInfo):
    outBuffers = []
    reader = SyncReader(inBuffer)
    filter = Filter(bitmapInfo, JSObject({
        'read': reader.read,
        'write': lambda bufferPart: outBuffers.append(bufferPart),
        'complete': lambda: None,
    }))

    filter.start()
    reader.process()

    return b''.join(outBuffers)
