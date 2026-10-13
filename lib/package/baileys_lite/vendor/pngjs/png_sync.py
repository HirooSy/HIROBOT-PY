from baileys_lite.foundation.node_runtime import JSObject
from baileys_lite.vendor.pngjs.packer_sync import default as pack
from baileys_lite.vendor.pngjs.parser_sync import default as parse


def read(buffer, options=None):
    return parse(buffer, JSObject(options or {}))


def write(png, options=None):
    return pack(JSObject(png), options)
