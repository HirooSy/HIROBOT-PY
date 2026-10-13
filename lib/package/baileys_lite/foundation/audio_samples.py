import math
import struct

from baileys_lite.foundation.node_runtime import JSError, JSObject


def _f32(value):
    try:
        return struct.unpack('<f', struct.pack('<f', value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def _jsPow(base, exponent):
    try:
        return float(base) ** exponent
    except ZeroDivisionError:
        return math.inf


def _jsDiv(a, b):
    if b == 0:
        if a == 0 or math.isnan(a):
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def _jsMax(values):
    if len(values) == 0:
        return -math.inf
    result = -math.inf
    for value in values:
        if math.isnan(value):
            return math.nan
        if value > result:
            result = value
    return result


def _jsFloor(value):
    if math.isnan(value) or math.isinf(value):
        return value
    return math.floor(value)


def _toUint8(value):
    if math.isnan(value) or math.isinf(value):
        return 0
    return int(value) & 0xFF


def decodeWav(buf):
    if buf[0:4] != b'RIFF' or buf[8:12] != b'WAVE':
        return None
    pos = 12
    fmt = None
    while pos + 8 <= len(buf):
        chunkId = buf[pos:pos + 4].decode('latin-1')
        size = struct.unpack_from('<I', buf, pos + 4)[0]
        body = pos + 8
        if chunkId == 'fmt ':
            tag = struct.unpack_from('<H', buf, body)[0]
            bits = struct.unpack_from('<H', buf, body + 14)[0]
            if tag == 0xfffe and size >= 26:
                tag = struct.unpack_from('<H', buf, body + 24)[0]
            fmt = JSObject({
                'tag': tag,
                'channels': struct.unpack_from('<H', buf, body + 2)[0],
                'blockAlign': struct.unpack_from('<H', buf, body + 12)[0],
                'bits': bits,
            })
        elif chunkId == 'data':
            if not fmt or not fmt.get('channels') or not fmt.get('blockAlign'):
                return None
            end = min(len(buf), len(buf) if size == 0xffffffff else body + size)
            frames = math.floor((end - body) / fmt.get('blockAlign'))
            if frames < 0:
                raise JSError('Invalid typed array length')
            bytesPerSample = fmt.get('bits') >> 3
            out = [0.0] * frames
            for i in range(frames):
                o = body + i * fmt.get('blockAlign')
                if fmt.get('tag') == 1:
                    if fmt.get('bits') == 8:
                        v = (buf[o] - 128) / 128
                    elif fmt.get('bits') == 16:
                        v = struct.unpack_from('<h', buf, o)[0] / 32768
                    elif fmt.get('bits') == 24:
                        if o + 3 > len(buf):
                            raise JSError('Out of range')
                        v = int.from_bytes(buf[o:o + 3], 'little', signed=True) / 8388608
                    elif fmt.get('bits') == 32:
                        v = struct.unpack_from('<i', buf, o)[0] / 2147483648
                    else:
                        return None
                elif fmt.get('tag') == 3:
                    if bytesPerSample == 4:
                        v = struct.unpack_from('<f', buf, o)[0]
                    elif bytesPerSample == 8:
                        v = struct.unpack_from('<d', buf, o)[0]
                    else:
                        return None
                else:
                    return None
                out[i] = _f32(v)
            return out
        pos = body + size + (size & 1)
    return None


class Bits:
    def __init__(self, buf, pos):
        self.buf = buf
        self.pos = pos * 8

    @property
    def bytePos(self):
        return self.pos >> 3

    def read(self, n):
        v = 0
        while n > 0:
            index = self.pos >> 3
            if index >= len(self.buf):
                raise JSError('FLAC: unexpected end of data')
            byte = self.buf[index]
            avail = 8 - (self.pos & 7)
            take = min(avail, n)
            v = v * (1 << take) + ((byte >> (avail - take)) & ((1 << take) - 1))
            self.pos += take
            n -= take
        return v

    def readSigned(self, n):
        v = self.read(n)
        return v - 2 ** n if v >= 2 ** (n - 1) else v

    def unary(self):
        n = 0
        while self.read(1) == 0:
            n += 1
        return n

    def align(self):
        self.pos = (self.pos + 7) & ~7


FIXED = [[], [1], [2, -1], [3, -3, 1], [4, -6, 4, -1]]
BLOCK_SIZES = JSObject({1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608, 8: 256, 9: 512, 10: 1024, 11: 2048, 12: 4096, 13: 8192, 14: 16384, 15: 32768})
SAMPLE_BITS = JSObject({1: 8, 2: 12, 4: 16, 5: 20, 6: 24, 7: 32})


def readResidual(br, out, offset, blockSize, order):
    method = br.read(2)
    if method > 1:
        raise JSError('FLAC: bad residual coding method')
    paramBits = 4 if method == 0 else 5
    escape = 15 if method == 0 else 31
    partOrder = br.read(4)
    parts = 1 << partOrder
    n = offset + order
    for p in range(parts):
        count = (blockSize >> partOrder) - (order if p == 0 else 0)
        param = br.read(paramBits)
        if param == escape:
            bits = br.read(5)
            for i in range(count):
                out[n] = float(br.readSigned(bits)) if bits else 0.0
                n += 1
        else:
            for i in range(count):
                q = br.unary()
                r = br.read(param) if param else 0
                u = q * 2 ** param + r
                out[n] = u / 2 if u % 2 == 0 else -(u + 1) / 2
                n += 1


def readSubframe(br, blockSize, bps):
    br.read(1)
    type = br.read(6)
    wasted = 0
    if br.read(1):
        wasted = br.unary() + 1
    bits = bps - wasted
    s = [0.0] * blockSize
    if type == 0:
        v = br.readSigned(bits)
        s = [float(v)] * blockSize
    elif type == 1:
        for i in range(blockSize):
            s[i] = float(br.readSigned(bits))
    elif 8 <= type <= 12:
        order = type - 8
        for i in range(order):
            s[i] = float(br.readSigned(bits))
        readResidual(br, s, 0, blockSize, order)
        c = FIXED[order]
        for i in range(order, blockSize):
            pred = 0
            for j in range(order):
                pred += c[j] * s[i - 1 - j]
            s[i] += pred
    elif type >= 32:
        order = type - 31
        for i in range(order):
            s[i] = float(br.readSigned(bits))
        precision = br.read(4) + 1
        if precision == 16:
            raise JSError('FLAC: invalid LPC precision')
        shift = br.readSigned(5)
        coefs = []
        for i in range(order):
            coefs.append(br.readSigned(precision))
        readResidual(br, s, 0, blockSize, order)
        div = 2 ** shift
        for i in range(order, blockSize):
            total = 0
            for j in range(order):
                total += coefs[j] * s[i - 1 - j]
            s[i] += math.floor(total / div) if shift >= 0 else total * 2 ** -shift
    else:
        raise JSError('FLAC: reserved subframe type')
    if wasted:
        for i in range(blockSize):
            s[i] *= 2 ** wasted
    return s


def decodeFlac(buf):
    if buf[0:4] != b'fLaC':
        return None
    pos = 4
    streamBps = 0
    total = 0
    while pos + 4 <= len(buf):
        h = buf[pos]
        length = (buf[pos + 1] << 16) | (buf[pos + 2] << 8) | buf[pos + 3]
        if (h & 0x7f) == 0 and length == 34:
            streamBps = ((buf[pos + 4 + 12] & 1) << 4) + (buf[pos + 4 + 13] >> 4) + 1
            total = (buf[pos + 4 + 13] & 15) * 2 ** 32 + struct.unpack_from('>I', buf, pos + 4 + 14)[0]
        pos += 4 + length
        if h & 0x80:
            break
    if not streamBps:
        return None
    chunks = []
    count = 0
    br = Bits(buf, pos)
    while br.bytePos + 6 < len(buf):
        sync = br.read(14)
        if sync != 0x3ffe:
            break
        br.read(2)
        bsCode = br.read(4)
        srCode = br.read(4)
        chAssign = br.read(4)
        sizeCode = br.read(3)
        br.read(1)

        first = br.read(8)
        if first < 0x80:
            extra = 0
        elif first < 0xe0:
            extra = 1
        elif first < 0xf0:
            extra = 2
        elif first < 0xf8:
            extra = 3
        elif first < 0xfc:
            extra = 4
        elif first < 0xfe:
            extra = 5
        else:
            extra = 6
        for _ in range(extra):
            br.read(8)
        blockSize = BLOCK_SIZES[bsCode]
        if bsCode == 6:
            blockSize = br.read(8) + 1
        elif bsCode == 7:
            blockSize = br.read(16) + 1
        if not blockSize:
            raise JSError('FLAC: bad block size')
        if srCode == 12:
            br.read(8)
        elif srCode == 13 or srCode == 14:
            br.read(16)
        br.read(8)
        bps = streamBps if sizeCode == 0 else SAMPLE_BITS[sizeCode]
        if not bps:
            raise JSError('FLAC: reserved sample size')
        nch = chAssign + 1 if chAssign < 8 else 2
        chans = []
        for c in range(nch):
            extraBit = 1 if ((chAssign == 8 and c == 1) or (chAssign == 9 and c == 0) or (chAssign == 10 and c == 1)) else 0
            chans.append(readSubframe(br, blockSize, bps + extraBit))
        br.align()
        br.read(16)

        ch0 = chans[0]
        if chAssign == 8:
            ch0 = chans[0]
        elif chAssign == 9:
            ch0 = [0.0] * blockSize
            for i in range(blockSize):
                ch0[i] = chans[0][i] + chans[1][i]
        elif chAssign == 10:
            ch0 = [0.0] * blockSize
            for i in range(blockSize):
                side = chans[1][i]
                mid = chans[0][i] * 2 + ((math.fmod(side, 2) + 2) % 2)
                ch0[i] = (mid + side) / 2
        scale = 2 ** (bps - 1)
        f = [0.0] * blockSize
        for i in range(blockSize):
            f[i] = _f32(ch0[i] / scale)
        chunks.append(f)
        count += blockSize
    out = []
    for c in chunks:
        out.extend(c)
    return out[:total] if total and count > total else out


def decodeChannel0(buffer):
    if isinstance(buffer, str):
        buffer = buffer.encode('utf-8')
    buffer = bytes(buffer)
    if len(buffer) < 12:
        return None
    try:
        result = decodeWav(buffer)
        if result is None:
            result = decodeFlac(buffer)
        return result
    except Exception:
        return None


def waveformFromSamples(rawData, samples=64):
    blockSize = math.floor(len(rawData) / samples) if samples else 0
    filtered = []
    for i in range(samples):
        blockStart = blockSize * i
        total = 0
        for j in range(blockSize):
            total += abs(rawData[blockStart + j])
        filtered.append(_jsDiv(total, blockSize))
    multiplier = _jsPow(_jsMax(filtered), -1)
    result = bytearray()
    for n in filtered:
        result.append(_toUint8(_jsFloor(100 * (n * multiplier))))
    return bytes(result)
