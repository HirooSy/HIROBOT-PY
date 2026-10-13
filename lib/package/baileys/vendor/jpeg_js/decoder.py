import math

from baileys.foundation.node_runtime import JSError, JSObject


dctZigZag = [
    0,
    1, 8,
    16, 9, 2,
    3, 10, 17, 24,
    32, 25, 18, 11, 4,
    5, 12, 19, 26, 33, 40,
    48, 41, 34, 27, 20, 13, 6,
    7, 14, 21, 28, 35, 42, 49, 56,
    57, 50, 43, 36, 29, 22, 15,
    23, 30, 37, 44, 51, 58,
    59, 52, 45, 38, 31,
    39, 46, 53, 60,
    61, 54, 47,
    55, 62,
    63,
]

dctCos1 = 4017
dctSin1 = 799
dctCos3 = 3406
dctSin3 = 2276
dctCos6 = 1567
dctSin6 = 3784
dctSqrt2 = 5793
dctSqrt1d2 = 2896

totalBytesAllocated = 0
maxMemoryUsageBytes = 0


def _i32(value):
    return ((int(value) + 0x80000000) & 0xFFFFFFFF) - 0x80000000


def _sar(value, count):
    return _i32(value) >> count


def _shl(value, count):
    return _i32(_i32(value) << count)


def _u8(value):
    return int(value) & 0xFF


def _at(data, index):
    if 0 <= index < len(data):
        return data[index]
    return None


def _b(data, index):
    value = _at(data, index)
    return 0 if value is None else value


def _ceilDiv(a, b):
    return -(-a // b)


def _zzIndex(k):
    if 0 <= k < 64:
        return dctZigZag[k]
    return None


def _load(zz, index):
    if index is None:
        return 0
    return zz[index]


def _store(zz, index, value):
    if index is not None:
        zz[index] = _i32(value)


def requestMemoryAllocation(increaseAmount=0):
    global totalBytesAllocated
    totalMemoryImpactBytes = totalBytesAllocated + increaseAmount
    if totalMemoryImpactBytes > maxMemoryUsageBytes:
        exceededAmount = math.ceil((totalMemoryImpactBytes - maxMemoryUsageBytes) / 1024 / 1024)
        raise JSError(
            'maxMemoryUsageInMB limit exceeded by at least ' + str(exceededAmount) + 'MB'
        )
    totalBytesAllocated = totalMemoryImpactBytes


def clampTo8bit(a):
    if a < 0:
        return 0
    if a > 255:
        return 255
    return a


def buildHuffmanTable(codeLengths, values):
    k = 0
    code = [JSObject({'children': {}, 'index': 0})]
    length = 16
    while length > 0 and not codeLengths[length - 1]:
        length -= 1
    p = code[0]
    for i in range(length):
        for j in range(codeLengths[i]):
            p = code.pop()
            p.get('children')[p.get('index')] = values[k]
            while p.get('index') > 0:
                if len(code) == 0:
                    raise JSError('Could not recreate Huffman Table')
                p = code.pop()
            p['index'] += 1
            code.append(p)
            while len(code) <= i:
                q = JSObject({'children': {}, 'index': 0})
                code.append(q)
                p.get('children')[p.get('index')] = q.get('children')
                p = q
            k += 1
        if i + 1 < length:
            q = JSObject({'children': {}, 'index': 0})
            code.append(q)
            p.get('children')[p.get('index')] = q.get('children')
            p = q
    return code[0].get('children')


def decodeScan(data, offset, frame, components, resetInterval,
               spectralStart, spectralEnd, successivePrev, successive, opts):
    progressive = frame.get('progressive')
    mcusPerLine = frame.get('mcusPerLine')

    startOffset = offset
    bitsData = 0
    bitsCount = 0
    eobrun = 0
    successiveACState = 0
    successiveACNextValue = None

    def readBit():
        nonlocal offset, bitsData, bitsCount
        if bitsCount > 0:
            bitsCount -= 1
            return (bitsData >> bitsCount) & 1
        bitsData = _b(data, offset)
        offset += 1
        if bitsData == 0xFF:
            nextByte = _b(data, offset)
            offset += 1
            if nextByte:
                raise JSError('unexpected marker: ' + format((bitsData << 8) | nextByte, 'x'))
        bitsCount = 7
        return bitsData >> 7

    def decodeHuffman(tree):
        node = tree
        while True:
            bit = readBit()
            node = node.get(bit)
            if isinstance(node, int):
                return node
            if not isinstance(node, dict):
                raise JSError('invalid huffman sequence')

    def receive(length):
        n = 0
        while length > 0:
            bit = readBit()
            n = (n << 1) | bit
            length -= 1
        return n

    def receiveAndExtend(length):
        n = receive(length)
        if n >= 1 << (length - 1):
            return n
        return n + (-1 << length) + 1

    def decodeBaseline(component, zz):
        t = decodeHuffman(component.get('huffmanTableDC'))
        diff = 0 if t == 0 else receiveAndExtend(t)
        component['pred'] += diff
        zz[0] = _i32(component.get('pred'))
        k = 1
        while k < 64:
            rs = decodeHuffman(component.get('huffmanTableAC'))
            s = rs & 15
            r = rs >> 4
            if s == 0:
                if r < 15:
                    break
                k += 16
                continue
            k += r
            _store(zz, _zzIndex(k), receiveAndExtend(s))
            k += 1

    def decodeDCFirst(component, zz):
        t = decodeHuffman(component.get('huffmanTableDC'))
        diff = 0 if t == 0 else _shl(receiveAndExtend(t), successive)
        component['pred'] += diff
        zz[0] = _i32(component.get('pred'))

    def decodeDCSuccessive(component, zz):
        zz[0] = _i32(zz[0] | (readBit() << successive))

    def decodeACFirst(component, zz):
        nonlocal eobrun
        if eobrun > 0:
            eobrun -= 1
            return
        k = spectralStart
        e = spectralEnd
        while k <= e:
            rs = decodeHuffman(component.get('huffmanTableAC'))
            s = rs & 15
            r = rs >> 4
            if s == 0:
                if r < 15:
                    eobrun = receive(r) + (1 << r) - 1
                    break
                k += 16
                continue
            k += r
            _store(zz, _zzIndex(k), receiveAndExtend(s) * (1 << successive))
            k += 1

    def decodeACSuccessive(component, zz):
        nonlocal eobrun, successiveACState, successiveACNextValue
        k = spectralStart
        e = spectralEnd
        r = 0
        while k <= e:
            z = _zzIndex(k)
            zv = _load(zz, z)
            direction = -1 if zv < 0 else 1
            if successiveACState == 0:
                rs = decodeHuffman(component.get('huffmanTableAC'))
                s = rs & 15
                r = rs >> 4
                if s == 0:
                    if r < 15:
                        eobrun = receive(r) + (1 << r)
                        successiveACState = 4
                    else:
                        r = 16
                        successiveACState = 1
                else:
                    if s != 1:
                        raise JSError('invalid ACn encoding')
                    successiveACNextValue = receiveAndExtend(s)
                    successiveACState = 2 if r else 3
                continue
            elif successiveACState == 1 or successiveACState == 2:
                if zv:
                    _store(zz, z, zv + (readBit() << successive) * direction)
                else:
                    r -= 1
                    if r == 0:
                        successiveACState = 3 if successiveACState == 2 else 0
            elif successiveACState == 3:
                if zv:
                    _store(zz, z, zv + (readBit() << successive) * direction)
                else:
                    _store(zz, z, _shl(successiveACNextValue, successive))
                    successiveACState = 0
            elif successiveACState == 4:
                if zv:
                    _store(zz, z, zv + (readBit() << successive) * direction)
            k += 1
        if successiveACState == 4:
            eobrun -= 1
            if eobrun == 0:
                successiveACState = 0

    def decodeMcu(component, decode, mcu, row, col):
        mcuRow = mcu // mcusPerLine
        mcuCol = mcu % mcusPerLine
        blockRow = mcuRow * component.get('v') + row
        blockCol = mcuCol * component.get('h') + col

        blocks = component.get('blocks')
        if blockRow >= len(blocks) and opts.get('tolerantDecoding'):
            return
        decode(component, blocks[blockRow][blockCol])

    def decodeBlock(component, decode, mcu):
        blockRow = mcu // component.get('blocksPerLine')
        blockCol = mcu % component.get('blocksPerLine')

        blocks = component.get('blocks')
        if blockRow >= len(blocks) and opts.get('tolerantDecoding'):
            return
        decode(component, blocks[blockRow][blockCol])

    componentsLength = len(components)
    if progressive:
        if spectralStart == 0:
            decodeFn = decodeDCFirst if successivePrev == 0 else decodeDCSuccessive
        else:
            decodeFn = decodeACFirst if successivePrev == 0 else decodeACSuccessive
    else:
        decodeFn = decodeBaseline

    mcu = 0
    if componentsLength == 1:
        mcuExpected = components[0].get('blocksPerLine') * components[0].get('blocksPerColumn')
    else:
        mcuExpected = mcusPerLine * frame.get('mcusPerColumn')
    if not resetInterval:
        resetInterval = mcuExpected

    while mcu < mcuExpected:
        for i in range(componentsLength):
            components[i]['pred'] = 0
        eobrun = 0

        if componentsLength == 1:
            component = components[0]
            for n in range(resetInterval):
                decodeBlock(component, decodeFn, mcu)
                mcu += 1
        else:
            for n in range(resetInterval):
                for i in range(componentsLength):
                    component = components[i]
                    h = component.get('h')
                    v = component.get('v')
                    for j in range(v):
                        for k in range(h):
                            decodeMcu(component, decodeFn, mcu, j, k)
                mcu += 1

                if mcu == mcuExpected:
                    break

        if mcu == mcuExpected:
            while True:
                if _at(data, offset) == 0xFF:
                    if _at(data, offset + 1) != 0x00:
                        break
                offset += 1
                if not (offset < len(data) - 2):
                    break

        bitsCount = 0
        marker = (_b(data, offset) << 8) | _b(data, offset + 1)
        if marker < 0xFF00:
            raise JSError('marker was not found')

        if 0xFFD0 <= marker <= 0xFFD7:
            offset += 2
        else:
            break

    return offset - startOffset


def buildComponentData(frame, component):
    lines = []
    blocksPerLine = component.get('blocksPerLine')
    blocksPerColumn = component.get('blocksPerColumn')
    samplesPerLine = blocksPerLine << 3

    R = [0] * 64
    r = [0] * 64

    def quantizeAndInverse(zz, dataOut, dataIn):
        qt = component.get('quantizationTable')
        p = dataIn

        for i in range(64):
            p[i] = _i32(zz[i] * qt[i])

        for i in range(8):
            row = 8 * i

            if (p[1 + row] == 0 and p[2 + row] == 0 and p[3 + row] == 0 and
                    p[4 + row] == 0 and p[5 + row] == 0 and p[6 + row] == 0 and
                    p[7 + row] == 0):
                t = _sar(dctSqrt2 * p[0 + row] + 512, 10)
                p[0 + row] = t
                p[1 + row] = t
                p[2 + row] = t
                p[3 + row] = t
                p[4 + row] = t
                p[5 + row] = t
                p[6 + row] = t
                p[7 + row] = t
                continue

            v0 = _sar(dctSqrt2 * p[0 + row] + 128, 8)
            v1 = _sar(dctSqrt2 * p[4 + row] + 128, 8)
            v2 = p[2 + row]
            v3 = p[6 + row]
            v4 = _sar(dctSqrt1d2 * (p[1 + row] - p[7 + row]) + 128, 8)
            v7 = _sar(dctSqrt1d2 * (p[1 + row] + p[7 + row]) + 128, 8)
            v5 = _shl(p[3 + row], 4)
            v6 = _shl(p[5 + row], 4)

            t = _sar(v0 - v1 + 1, 1)
            v0 = _sar(v0 + v1 + 1, 1)
            v1 = t
            t = _sar(v2 * dctSin6 + v3 * dctCos6 + 128, 8)
            v2 = _sar(v2 * dctCos6 - v3 * dctSin6 + 128, 8)
            v3 = t
            t = _sar(v4 - v6 + 1, 1)
            v4 = _sar(v4 + v6 + 1, 1)
            v6 = t
            t = _sar(v7 + v5 + 1, 1)
            v5 = _sar(v7 - v5 + 1, 1)
            v7 = t

            t = _sar(v0 - v3 + 1, 1)
            v0 = _sar(v0 + v3 + 1, 1)
            v3 = t
            t = _sar(v1 - v2 + 1, 1)
            v1 = _sar(v1 + v2 + 1, 1)
            v2 = t
            t = _sar(v4 * dctSin3 + v7 * dctCos3 + 2048, 12)
            v4 = _sar(v4 * dctCos3 - v7 * dctSin3 + 2048, 12)
            v7 = t
            t = _sar(v5 * dctSin1 + v6 * dctCos1 + 2048, 12)
            v5 = _sar(v5 * dctCos1 - v6 * dctSin1 + 2048, 12)
            v6 = t

            p[0 + row] = _i32(v0 + v7)
            p[7 + row] = _i32(v0 - v7)
            p[1 + row] = _i32(v1 + v6)
            p[6 + row] = _i32(v1 - v6)
            p[2 + row] = _i32(v2 + v5)
            p[5 + row] = _i32(v2 - v5)
            p[3 + row] = _i32(v3 + v4)
            p[4 + row] = _i32(v3 - v4)

        for i in range(8):
            col = i

            if (p[1 * 8 + col] == 0 and p[2 * 8 + col] == 0 and p[3 * 8 + col] == 0 and
                    p[4 * 8 + col] == 0 and p[5 * 8 + col] == 0 and p[6 * 8 + col] == 0 and
                    p[7 * 8 + col] == 0):
                t = _sar(dctSqrt2 * dataIn[i + 0] + 8192, 14)
                p[0 * 8 + col] = t
                p[1 * 8 + col] = t
                p[2 * 8 + col] = t
                p[3 * 8 + col] = t
                p[4 * 8 + col] = t
                p[5 * 8 + col] = t
                p[6 * 8 + col] = t
                p[7 * 8 + col] = t
                continue

            v0 = _sar(dctSqrt2 * p[0 * 8 + col] + 2048, 12)
            v1 = _sar(dctSqrt2 * p[4 * 8 + col] + 2048, 12)
            v2 = p[2 * 8 + col]
            v3 = p[6 * 8 + col]
            v4 = _sar(dctSqrt1d2 * (p[1 * 8 + col] - p[7 * 8 + col]) + 2048, 12)
            v7 = _sar(dctSqrt1d2 * (p[1 * 8 + col] + p[7 * 8 + col]) + 2048, 12)
            v5 = p[3 * 8 + col]
            v6 = p[5 * 8 + col]

            t = _sar(v0 - v1 + 1, 1)
            v0 = _sar(v0 + v1 + 1, 1)
            v1 = t
            t = _sar(v2 * dctSin6 + v3 * dctCos6 + 2048, 12)
            v2 = _sar(v2 * dctCos6 - v3 * dctSin6 + 2048, 12)
            v3 = t
            t = _sar(v4 - v6 + 1, 1)
            v4 = _sar(v4 + v6 + 1, 1)
            v6 = t
            t = _sar(v7 + v5 + 1, 1)
            v5 = _sar(v7 - v5 + 1, 1)
            v7 = t

            t = _sar(v0 - v3 + 1, 1)
            v0 = _sar(v0 + v3 + 1, 1)
            v3 = t
            t = _sar(v1 - v2 + 1, 1)
            v1 = _sar(v1 + v2 + 1, 1)
            v2 = t
            t = _sar(v4 * dctSin3 + v7 * dctCos3 + 2048, 12)
            v4 = _sar(v4 * dctCos3 - v7 * dctSin3 + 2048, 12)
            v7 = t
            t = _sar(v5 * dctSin1 + v6 * dctCos1 + 2048, 12)
            v5 = _sar(v5 * dctCos1 - v6 * dctSin1 + 2048, 12)
            v6 = t

            p[0 * 8 + col] = _i32(v0 + v7)
            p[7 * 8 + col] = _i32(v0 - v7)
            p[1 * 8 + col] = _i32(v1 + v6)
            p[6 * 8 + col] = _i32(v1 - v6)
            p[2 * 8 + col] = _i32(v2 + v5)
            p[5 * 8 + col] = _i32(v2 - v5)
            p[3 * 8 + col] = _i32(v3 + v4)
            p[4 * 8 + col] = _i32(v3 - v4)

        for i in range(64):
            sample = 128 + _sar(p[i] + 8, 4)
            dataOut[i] = 0 if sample < 0 else (0xFF if sample > 0xFF else sample)

    requestMemoryAllocation(samplesPerLine * blocksPerColumn * 8)

    for blockRow in range(blocksPerColumn):
        scanLine = blockRow << 3
        for i in range(8):
            lines.append(bytearray(samplesPerLine))
        for blockCol in range(blocksPerLine):
            quantizeAndInverse(component.get('blocks')[blockRow][blockCol], r, R)

            offset = 0
            sample = blockCol << 3
            for j in range(8):
                line = lines[scanLine + j]
                for i in range(8):
                    line[sample + i] = r[offset]
                    offset += 1
    return lines


def prepareComponents(frame):
    maxH = 1
    maxV = 1
    components = frame.get('components')
    for componentId in list(dict.keys(components)):
        component = components[componentId]
        if maxH < component.get('h'):
            maxH = component.get('h')
        if maxV < component.get('v'):
            maxV = component.get('v')
    mcusPerLine = _ceilDiv(frame.get('samplesPerLine'), 8 * maxH)
    mcusPerColumn = _ceilDiv(frame.get('scanLines'), 8 * maxV)
    for componentId in list(dict.keys(components)):
        component = components[componentId]
        blocksPerLine = _ceilDiv(_ceilDiv(frame.get('samplesPerLine'), 8) * component.get('h'), maxH)
        blocksPerColumn = _ceilDiv(_ceilDiv(frame.get('scanLines'), 8) * component.get('v'), maxV)
        blocksPerLineForMcu = mcusPerLine * component.get('h')
        blocksPerColumnForMcu = mcusPerColumn * component.get('v')
        blocksToAllocate = blocksPerColumnForMcu * blocksPerLineForMcu
        blocks = []

        requestMemoryAllocation(blocksToAllocate * 256)

        for i in range(blocksPerColumnForMcu):
            row = []
            for j in range(blocksPerLineForMcu):
                row.append([0] * 64)
            blocks.append(row)
        component['blocksPerLine'] = blocksPerLine
        component['blocksPerColumn'] = blocksPerColumn
        component['blocks'] = blocks
    frame['maxH'] = maxH
    frame['maxV'] = maxV
    frame['mcusPerLine'] = mcusPerLine
    frame['mcusPerColumn'] = mcusPerColumn


class JpegImage:
    def __init__(self):
        self.exifBuffer = None
        self.comments = []
        self.opts = None
        self.jfif = None
        self.adobe = None
        self.width = 0
        self.height = 0
        self.components = []

    def load(self, path):
        raise JSError('XMLHttpRequest is not defined', name='ReferenceError')

    def parse(self, data):
        offset = 0
        length = len(data)

        def readUint16():
            nonlocal offset
            value = (_b(data, offset) << 8) | _b(data, offset + 1)
            offset += 2
            return value

        def readDataBlock():
            nonlocal offset
            blockLength = readUint16()
            array = data[offset:offset + blockLength - 2]
            offset += len(array)
            return array

        maxResolutionInPixels = self.opts.get('maxResolutionInMP') * 1000 * 1000

        jfif = None
        adobe = None
        frame = None
        resetInterval = None
        quantizationTables = {}
        frames = []
        huffmanTablesAC = {}
        huffmanTablesDC = {}
        fileMarker = readUint16()
        malformedDataOffset = -1
        self.comments = []
        if fileMarker != 0xFFD8:
            raise JSError('SOI not found')

        fileMarker = readUint16()
        while fileMarker != 0xFFD9:
            if fileMarker == 0xFF00:
                pass
            elif 0xFFE0 <= fileMarker <= 0xFFEF or fileMarker == 0xFFFE:
                appData = readDataBlock()

                if fileMarker == 0xFFFE:
                    comment = ''.join(chr(byte) for byte in appData)
                    self.comments.append(comment)

                if fileMarker == 0xFFE0:
                    if (_at(appData, 0) == 0x4A and _at(appData, 1) == 0x46 and _at(appData, 2) == 0x49 and
                            _at(appData, 3) == 0x46 and _at(appData, 4) == 0):
                        jfif = JSObject({
                            'version': JSObject({'major': _at(appData, 5), 'minor': _at(appData, 6)}),
                            'densityUnits': _at(appData, 7),
                            'xDensity': (_b(appData, 8) << 8) | _b(appData, 9),
                            'yDensity': (_b(appData, 10) << 8) | _b(appData, 11),
                            'thumbWidth': _at(appData, 12),
                            'thumbHeight': _at(appData, 13),
                            'thumbData': appData[14:14 + 3 * _b(appData, 12) * _b(appData, 13)],
                        })

                if fileMarker == 0xFFE1:
                    if (_at(appData, 0) == 0x45 and _at(appData, 1) == 0x78 and _at(appData, 2) == 0x69 and
                            _at(appData, 3) == 0x66 and _at(appData, 4) == 0):
                        self.exifBuffer = appData[5:len(appData)]

                if fileMarker == 0xFFEE:
                    if (_at(appData, 0) == 0x41 and _at(appData, 1) == 0x64 and _at(appData, 2) == 0x6F and
                            _at(appData, 3) == 0x62 and _at(appData, 4) == 0x65 and _at(appData, 5) == 0):
                        adobe = JSObject({
                            'version': _at(appData, 6),
                            'flags0': (_b(appData, 7) << 8) | _b(appData, 8),
                            'flags1': (_b(appData, 9) << 8) | _b(appData, 10),
                            'transformCode': _at(appData, 11),
                        })

            elif fileMarker == 0xFFDB:
                quantizationTablesLength = readUint16()
                quantizationTablesEnd = quantizationTablesLength + offset - 2
                while offset < quantizationTablesEnd:
                    quantizationTableSpec = _b(data, offset)
                    offset += 1
                    requestMemoryAllocation(64 * 4)
                    tableData = [0] * 64
                    if (quantizationTableSpec >> 4) == 0:
                        for j in range(64):
                            z = dctZigZag[j]
                            tableData[z] = _b(data, offset)
                            offset += 1
                    elif (quantizationTableSpec >> 4) == 1:
                        for j in range(64):
                            z = dctZigZag[j]
                            tableData[z] = readUint16()
                    else:
                        raise JSError('DQT: invalid table spec')
                    quantizationTables[quantizationTableSpec & 15] = tableData

            elif fileMarker in (0xFFC0, 0xFFC1, 0xFFC2):
                readUint16()
                frame = JSObject()
                frame['extended'] = fileMarker == 0xFFC1
                frame['progressive'] = fileMarker == 0xFFC2
                frame['precision'] = _at(data, offset)
                offset += 1
                frame['scanLines'] = readUint16()
                frame['samplesPerLine'] = readUint16()
                frame['components'] = JSObject()
                frame['componentsOrder'] = []

                pixelsInFrame = frame.get('scanLines') * frame.get('samplesPerLine')
                if pixelsInFrame > maxResolutionInPixels:
                    exceededAmount = math.ceil((pixelsInFrame - maxResolutionInPixels) / 1e6)
                    raise JSError('maxResolutionInMP limit exceeded by ' + str(exceededAmount) + 'MP')

                componentsCount = _b(data, offset)
                offset += 1
                for i in range(componentsCount):
                    componentId = _at(data, offset)
                    h = _b(data, offset + 1) >> 4
                    v = _b(data, offset + 1) & 15
                    qId = _at(data, offset + 2)

                    if h <= 0 or v <= 0:
                        raise JSError('Invalid sampling factor, expected values above 0')

                    frame.get('componentsOrder').append(componentId)
                    frame.get('components')[componentId] = JSObject({
                        'h': h,
                        'v': v,
                        'quantizationIdx': qId,
                    })
                    offset += 3
                prepareComponents(frame)
                frames.append(frame)

            elif fileMarker == 0xFFC4:
                huffmanLength = readUint16()
                i = 2
                while i < huffmanLength:
                    huffmanTableSpec = _b(data, offset)
                    offset += 1
                    codeLengths = [0] * 16
                    codeLengthSum = 0
                    for j in range(16):
                        codeLengths[j] = _b(data, offset)
                        codeLengthSum += codeLengths[j]
                        offset += 1
                    requestMemoryAllocation(16 + codeLengthSum)
                    huffmanValues = [0] * codeLengthSum
                    for j in range(codeLengthSum):
                        huffmanValues[j] = _b(data, offset)
                        offset += 1
                    i += 17 + codeLengthSum

                    table = huffmanTablesDC if (huffmanTableSpec >> 4) == 0 else huffmanTablesAC
                    table[huffmanTableSpec & 15] = buildHuffmanTable(codeLengths, huffmanValues)

            elif fileMarker == 0xFFDD:
                readUint16()
                resetInterval = readUint16()

            elif fileMarker == 0xFFDC:
                readUint16()
                readUint16()

            elif fileMarker == 0xFFDA:
                readUint16()
                selectorsCount = _b(data, offset)
                offset += 1
                components = []
                for i in range(selectorsCount):
                    component = frame.get('components')[_at(data, offset)]
                    offset += 1
                    tableSpec = _b(data, offset)
                    offset += 1
                    component['huffmanTableDC'] = huffmanTablesDC.get(tableSpec >> 4)
                    component['huffmanTableAC'] = huffmanTablesAC.get(tableSpec & 15)
                    components.append(component)
                spectralStart = _b(data, offset)
                offset += 1
                spectralEnd = _b(data, offset)
                offset += 1
                successiveApproximation = _b(data, offset)
                offset += 1
                processed = decodeScan(
                    data, offset, frame, components, resetInterval,
                    spectralStart, spectralEnd,
                    successiveApproximation >> 4, successiveApproximation & 15, self.opts,
                )
                offset += processed

            elif fileMarker == 0xFFFF:
                if _at(data, offset) != 0xFF:
                    offset -= 1

            else:
                if _b(data, offset - 3) == 0xFF and 0xC0 <= _b(data, offset - 2) <= 0xFE:
                    offset -= 3
                elif fileMarker == 0xE0 or fileMarker == 0xE1:
                    if malformedDataOffset != -1:
                        raise JSError(
                            'first unknown JPEG marker at offset ' + format(malformedDataOffset, 'x') +
                            ', second unknown JPEG marker ' + format(fileMarker, 'x') +
                            ' at offset ' + format(offset - 1, 'x')
                        )
                    malformedDataOffset = offset - 1
                    nextOffset = readUint16()
                    if _at(data, offset + nextOffset - 2) == 0xFF:
                        offset += nextOffset - 2
                    else:
                        raise JSError('unknown JPEG marker ' + format(fileMarker, 'x'))
                else:
                    raise JSError('unknown JPEG marker ' + format(fileMarker, 'x'))
            fileMarker = readUint16()

        if len(frames) != 1:
            raise JSError('only single frame JPEGs supported')

        for frameEntry in frames:
            cp = frameEntry.get('components')
            for j in list(dict.keys(cp)):
                cp[j]['quantizationTable'] = quantizationTables.get(cp[j].get('quantizationIdx'))
                del cp[j]['quantizationIdx']

        self.width = frame.get('samplesPerLine')
        self.height = frame.get('scanLines')
        self.jfif = jfif
        self.adobe = adobe
        self.components = []
        for i in range(len(frame.get('componentsOrder'))):
            component = frame.get('components')[frame.get('componentsOrder')[i]]
            self.components.append(JSObject({
                'lines': buildComponentData(frame, component),
                'scaleX': component.get('h') / frame.get('maxH'),
                'scaleY': component.get('v') / frame.get('maxV'),
            }))

    def getData(self, width, height):
        scaleX = self.width / width
        scaleY = self.height / height

        dataLength = width * height * len(self.components)
        requestMemoryAllocation(dataLength)
        data = bytearray(dataLength)
        offset = 0
        count = len(self.components)
        if count == 1:
            component1 = self.components[0]
            for y in range(height):
                component1Line = component1.get('lines')[int(y * component1.get('scaleY') * scaleY)]
                for x in range(width):
                    Y = component1Line[int(x * component1.get('scaleX') * scaleX)]
                    data[offset] = Y
                    offset += 1
        elif count == 2:
            component1 = self.components[0]
            component2 = self.components[1]
            for y in range(height):
                component1Line = component1.get('lines')[int(y * component1.get('scaleY') * scaleY)]
                component2Line = component2.get('lines')[int(y * component2.get('scaleY') * scaleY)]
                for x in range(width):
                    Y = component1Line[int(x * component1.get('scaleX') * scaleX)]
                    data[offset] = Y
                    offset += 1
                    Y = component2Line[int(x * component2.get('scaleX') * scaleX)]
                    data[offset] = Y
                    offset += 1
        elif count == 3:
            colorTransform = True

            if self.adobe is not None and self.adobe.get('transformCode'):
                colorTransform = True
            elif self.opts.get('colorTransform') is not None:
                colorTransform = bool(self.opts.get('colorTransform'))

            component1 = self.components[0]
            component2 = self.components[1]
            component3 = self.components[2]
            for y in range(height):
                component1Line = component1.get('lines')[int(y * component1.get('scaleY') * scaleY)]
                component2Line = component2.get('lines')[int(y * component2.get('scaleY') * scaleY)]
                component3Line = component3.get('lines')[int(y * component3.get('scaleY') * scaleY)]
                for x in range(width):
                    if not colorTransform:
                        R = component1Line[int(x * component1.get('scaleX') * scaleX)]
                        G = component2Line[int(x * component2.get('scaleX') * scaleX)]
                        B = component3Line[int(x * component3.get('scaleX') * scaleX)]
                    else:
                        Y = component1Line[int(x * component1.get('scaleX') * scaleX)]
                        Cb = component2Line[int(x * component2.get('scaleX') * scaleX)]
                        Cr = component3Line[int(x * component3.get('scaleX') * scaleX)]

                        R = clampTo8bit(Y + 1.402 * (Cr - 128))
                        G = clampTo8bit(Y - 0.3441363 * (Cb - 128) - 0.71413636 * (Cr - 128))
                        B = clampTo8bit(Y + 1.772 * (Cb - 128))

                    data[offset] = _u8(R)
                    data[offset + 1] = _u8(G)
                    data[offset + 2] = _u8(B)
                    offset += 3
        elif count == 4:
            if self.adobe is None:
                raise JSError('Unsupported color mode (4 components)')

            colorTransform = False

            if self.adobe is not None and self.adobe.get('transformCode'):
                colorTransform = True
            elif self.opts.get('colorTransform') is not None:
                colorTransform = bool(self.opts.get('colorTransform'))

            component1 = self.components[0]
            component2 = self.components[1]
            component3 = self.components[2]
            component4 = self.components[3]
            for y in range(height):
                component1Line = component1.get('lines')[int(y * component1.get('scaleY') * scaleY)]
                component2Line = component2.get('lines')[int(y * component2.get('scaleY') * scaleY)]
                component3Line = component3.get('lines')[int(y * component3.get('scaleY') * scaleY)]
                component4Line = component4.get('lines')[int(y * component4.get('scaleY') * scaleY)]
                for x in range(width):
                    if not colorTransform:
                        C = component1Line[int(x * component1.get('scaleX') * scaleX)]
                        M = component2Line[int(x * component2.get('scaleX') * scaleX)]
                        Ye = component3Line[int(x * component3.get('scaleX') * scaleX)]
                        K = component4Line[int(x * component4.get('scaleX') * scaleX)]
                    else:
                        Y = component1Line[int(x * component1.get('scaleX') * scaleX)]
                        Cb = component2Line[int(x * component2.get('scaleX') * scaleX)]
                        Cr = component3Line[int(x * component3.get('scaleX') * scaleX)]
                        K = component4Line[int(x * component4.get('scaleX') * scaleX)]

                        C = 255 - clampTo8bit(Y + 1.402 * (Cr - 128))
                        M = 255 - clampTo8bit(Y - 0.3441363 * (Cb - 128) - 0.71413636 * (Cr - 128))
                        Ye = 255 - clampTo8bit(Y + 1.772 * (Cb - 128))

                    data[offset] = _u8(255 - C)
                    data[offset + 1] = _u8(255 - M)
                    data[offset + 2] = _u8(255 - Ye)
                    data[offset + 3] = _u8(255 - K)
                    offset += 4
        else:
            raise JSError('Unsupported color mode')
        return data

    def copyToImageData(self, imageData, formatAsRGBA):
        width = imageData.get('width')
        height = imageData.get('height')
        imageDataArray = imageData.get('data')
        data = self.getData(width, height)
        i = 0
        j = 0
        count = len(self.components)
        if count == 1:
            for y in range(height):
                for x in range(width):
                    Y = data[i]
                    i += 1

                    imageDataArray[j] = Y
                    imageDataArray[j + 1] = Y
                    imageDataArray[j + 2] = Y
                    j += 3
                    if formatAsRGBA:
                        imageDataArray[j] = 255
                        j += 1
        elif count == 3:
            for y in range(height):
                for x in range(width):
                    R = data[i]
                    G = data[i + 1]
                    B = data[i + 2]
                    i += 3

                    imageDataArray[j] = R
                    imageDataArray[j + 1] = G
                    imageDataArray[j + 2] = B
                    j += 3
                    if formatAsRGBA:
                        imageDataArray[j] = 255
                        j += 1
        elif count == 4:
            for y in range(height):
                for x in range(width):
                    C = data[i]
                    M = data[i + 1]
                    Y = data[i + 2]
                    K = data[i + 3]
                    i += 4

                    R = 255 - clampTo8bit(C * (1 - K / 255) + K)
                    G = 255 - clampTo8bit(M * (1 - K / 255) + K)
                    B = 255 - clampTo8bit(Y * (1 - K / 255) + K)

                    imageDataArray[j] = _u8(R)
                    imageDataArray[j + 1] = _u8(G)
                    imageDataArray[j + 2] = _u8(B)
                    j += 3
                    if formatAsRGBA:
                        imageDataArray[j] = 255
                        j += 1
        else:
            raise JSError('Unsupported color mode')

    @staticmethod
    def resetMaxMemoryUsage(maxMemoryUsageBytes_):
        global totalBytesAllocated, maxMemoryUsageBytes
        totalBytesAllocated = 0
        maxMemoryUsageBytes = maxMemoryUsageBytes_

    @staticmethod
    def getBytesAllocated():
        return totalBytesAllocated

    requestMemoryAllocation = staticmethod(requestMemoryAllocation)


def decode(jpegData, userOpts=None):
    defaultOpts = {
        'colorTransform': None,
        'useTArray': False,
        'formatAsRGBA': True,
        'tolerantDecoding': True,
        'maxResolutionInMP': 100,
        'maxMemoryUsageInMB': 512,
    }

    opts = JSObject({**defaultOpts, **(userOpts or {})})
    arr = bytes(jpegData)
    decoder = JpegImage()
    decoder.opts = opts

    JpegImage.resetMaxMemoryUsage(opts.get('maxMemoryUsageInMB') * 1024 * 1024)
    decoder.parse(arr)

    channels = 4 if opts.get('formatAsRGBA') else 3
    bytesNeeded = decoder.width * decoder.height * channels
    try:
        JpegImage.requestMemoryAllocation(bytesNeeded)
        image = JSObject({
            'width': decoder.width,
            'height': decoder.height,
            'exifBuffer': decoder.exifBuffer,
            'data': bytearray(bytesNeeded),
        })
        if len(decoder.comments) > 0:
            image['comments'] = decoder.comments
    except (MemoryError, OverflowError):
        raise JSError('Could not allocate enough memory for the image. Required: ' + str(bytesNeeded))

    decoder.copyToImageData(image, opts.get('formatAsRGBA'))

    return image


default = decode
