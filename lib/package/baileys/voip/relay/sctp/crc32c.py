from baileys.foundation.node_runtime import JSObject


def _buildTable():
    table = []
    poly = 0x82F63B78
    for n in range(256):
        c = n
        for k in range(8):
            c = ((c >> 1) ^ poly) if (c & 1) else (c >> 1)
        table.append(c & 0xFFFFFFFF)
    return table


TABLE = _buildTable()


def crc32c(data):
    crc = 0xFFFFFFFF
    for i in range(len(data)):
        crc = TABLE[(crc ^ data[i]) & 0xFF] ^ (crc >> 8)
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF
