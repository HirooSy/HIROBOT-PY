import zlib

from baileys.foundation.node_runtime import JSError


def inflateSync(buffer, opts=None):
    if isinstance(buffer, str):
        buffer = buffer.encode('utf-8')
    if not isinstance(buffer, (bytes, bytearray, memoryview)):
        raise JSError('Not a string or buffer', name='TypeError')

    maxLength = None
    if opts is not None and opts.get('maxLength') is not None:
        maxLength = opts.get('maxLength')

    if maxLength == 0:
        return b''

    decompressor = zlib.decompressobj()
    if maxLength is None:
        output = decompressor.decompress(bytes(buffer))
        if not decompressor.eof:
            raise JSError('unexpected end of file')
        return output
    return decompressor.decompress(bytes(buffer), maxLength)


default = inflateSync
