import datetime
import struct
import zlib

from baileys_lite.foundation.node_runtime import JSObject


def dosDateTime(date=None):
    if date is None:
        date = datetime.datetime.now()
    dosTime = (date.hour << 11) | (date.minute << 5) | (date.second >> 1)
    dosDate = ((date.year - 1980) << 9) | (date.month << 5) | date.day
    return JSObject({'dosTime': dosTime, 'dosDate': dosDate})


CRC_TABLE = None


def crc32Of(buf):
    return zlib.crc32(bytes(buf)) & 0xFFFFFFFF


def crc32Fallback(buf):
    global CRC_TABLE
    if CRC_TABLE is None:
        CRC_TABLE = [0] * 256
        for n in range(256):
            c = n
            for _ in range(8):
                c = (0xEDB88320 ^ (c >> 1)) if c & 1 else (c >> 1)
            CRC_TABLE[n] = c & 0xFFFFFFFF
    crc = 0xFFFFFFFF
    for byte in bytes(buf):
        crc = CRC_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


def deflateRaw(data, level):
    compressor = zlib.compressobj(level, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def zip(files, callback):
    try:
        localChunks = []
        centralChunks = []
        offset = 0
        stamp = dosDateTime()
        dosTime = stamp.get('dosTime')
        dosDate = stamp.get('dosDate')

        names = list(dict.keys(files))
        for filename in names:
            entry = files[filename]
            rawData = entry[0]
            opts = entry[1] if len(entry) > 1 and entry[1] is not None else JSObject({})
            if isinstance(rawData, str):
                data = rawData.encode('utf-8')
            else:
                data = bytes(rawData)
            level = opts.get('level') if opts.get('level') is not None else 6
            store = level == 0
            compressed = data if store else deflateRaw(data, level)
            method = 0 if store else 8
            crc = crc32Of(data)
            nameBuf = filename.encode('utf-8')

            localHeader = struct.pack('<IHHHHHIIIHH', 0x04034B50, 20, 0, method, dosTime, dosDate, crc, len(compressed), len(data), len(nameBuf), 0)
            localChunks.extend([localHeader, nameBuf, compressed])

            centralHeader = struct.pack('<IHHHHHHIIIHHHHHII', 0x02014B50, 20, 20, 0, method, dosTime, dosDate, crc, len(compressed), len(data), len(nameBuf), 0, 0, 0, 0, 0, offset)
            centralChunks.extend([centralHeader, nameBuf])

            offset += len(localHeader) + len(nameBuf) + len(compressed)

        centralDirSize = sum(len(c) for c in centralChunks)
        centralDirOffset = offset
        fileCount = len(names)

        eocd = struct.pack('<IHHHHIIH', 0x06054B50, 0, 0, fileCount, fileCount, centralDirSize, centralDirOffset, 0)

        result = b''.join(localChunks + centralChunks + [eocd])
        callback(None, result)
    except Exception as err:
        callback(err)
