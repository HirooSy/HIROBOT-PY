import types


class _HybridMethod:
    def __init__(self, instanceFn, staticFn):
        self.instanceFn = instanceFn
        self.staticFn = staticFn

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self.staticFn
        return types.MethodType(self.instanceFn, obj)


def _buildCrcTable():
    table = []
    for i in range(256):
        currentCrc = i
        for j in range(8):
            if currentCrc & 1:
                currentCrc = 0xedb88320 ^ (currentCrc >> 1)
            else:
                currentCrc = currentCrc >> 1
        table.append(currentCrc)
    return table


crcTable = _buildCrcTable()


def _toInt32(value):
    value &= 0xffffffff
    if value & 0x80000000:
        return value - 0x100000000
    return value


def _crcWrite(self, data):
    crc = self._crc
    for byte in data:
        crc = crcTable[(crc ^ byte) & 0xff] ^ (crc >> 8)
    self._crc = crc
    return True


def _crcInstanceCrc32(self):
    return _toInt32(self._crc ^ 0xffffffff)


def _crcStaticCrc32(buf):
    crc = 0xffffffff
    for byte in buf:
        crc = crcTable[(crc ^ byte) & 0xff] ^ (crc >> 8)
    return _toInt32(crc ^ 0xffffffff)


class CrcCalculator:
    def __init__(self):
        self._crc = 0xffffffff

    write = _crcWrite
    crc32 = _HybridMethod(_crcInstanceCrc32, _crcStaticCrc32)


default = CrcCalculator
