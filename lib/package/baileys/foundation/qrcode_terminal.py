import math

from baileys.foundation.node_runtime import JSError, JSObject


_QRErrorCorrectLevel = JSObject({'L': 1, 'M': 0, 'Q': 3, 'H': 2})


def _qrGlog(n):
    if n < 1:
        raise JSError('glog(' + str(n) + ')')
    return _QR_LOG[n]


def _qrGexp(n):
    return _QR_EXP[((n % 255) + 255) % 255]


def _buildQrMathTables():
    exp = [0] * 256
    log = [0] * 256
    for i in range(8):
        exp[i] = 1 << i
    for i in range(8, 256):
        exp[i] = exp[i - 4] ^ exp[i - 5] ^ exp[i - 6] ^ exp[i - 8]
    for i in range(255):
        log[exp[i]] = i
    return exp, log


_QR_EXP, _QR_LOG = _buildQrMathTables()

class _QRPoly:
    def __init__(self, num, shift):
        o = 0
        while o < len(num) and num[o] == 0:
            o += 1
        self.num = [0] * (len(num) - o + shift)
        for i in range(len(num) - o):
            self.num[i] = num[i + o]

    def get(self, i):
        return self.num[i]

    def getLength(self):
        return len(self.num)

    def multiply(self, e):
        n = [0] * (self.getLength() + e.getLength() - 1)
        for i in range(self.getLength()):
            for j in range(e.getLength()):
                n[i + j] ^= _qrGexp(_qrGlog(self.get(i)) + _qrGlog(e.get(j)))
        return _QRPoly(n, 0)

    def mod(self, e):
        poly = self
        while True:
            if poly.getLength() - e.getLength() < 0:
                return poly
            r = _qrGlog(poly.get(0)) - _qrGlog(e.get(0))
            n = poly.num[:]
            for i in range(e.getLength()):
                n[i] ^= _qrGexp(_qrGlog(e.get(i)) + r)
            poly = _QRPoly(n, 0)


_RS_TABLE = [
  [1,26,19],[1,26,16],[1,26,13],[1,26,9],[1,44,34],[1,44,28],[1,44,22],[1,44,16],
  [1,70,55],[1,70,44],[2,35,17],[2,35,13],[1,100,80],[2,50,32],[2,50,24],[4,25,9],
  [1,134,108],[2,67,43],[2,33,15,2,34,16],[2,33,11,2,34,12],[2,86,68],[4,43,27],[4,43,19],[4,43,15],
  [2,98,78],[4,49,31],[2,32,14,4,33,15],[4,39,13,1,40,14],[2,121,97],[2,60,38,2,61,39],[4,40,18,2,41,19],[4,40,14,2,41,15],
  [2,146,116],[3,58,36,2,59,37],[4,36,16,4,37,17],[4,36,12,4,37,13],[2,86,68,2,87,69],[4,69,43,1,70,44],[6,43,19,2,44,20],[6,43,15,2,44,16],
  [4,101,81],[1,80,50,4,81,51],[4,50,22,4,51,23],[3,36,12,8,37,13],[2,116,92,2,117,93],[6,58,36,2,59,37],[4,46,20,6,47,21],[7,42,14,4,43,15],
  [4,133,107],[8,59,37,1,60,38],[8,44,20,4,45,21],[12,33,11,4,34,12],[3,145,115,1,146,116],[4,64,40,5,65,41],[11,36,16,5,37,17],[11,36,12,5,37,13],
  [5,109,87,1,110,88],[5,65,41,5,66,42],[5,54,24,7,55,25],[11,36,12,7,37,13],[5,122,98,1,123,99],[7,73,45,3,74,46],[15,43,19,2,44,20],[3,45,15,13,46,16],
  [1,135,107,5,136,108],[10,74,46,1,75,47],[1,50,22,15,51,23],[2,42,14,17,43,15],[5,150,120,1,151,121],[9,69,43,4,70,44],[17,50,22,1,51,23],[2,42,14,19,43,15],
  [3,141,113,4,142,114],[3,70,44,11,71,45],[17,47,21,4,48,22],[9,39,13,16,40,14],[3,135,107,5,136,108],[3,67,41,13,68,42],[15,54,24,5,55,25],[15,43,15,10,44,16],
  [4,144,116,4,145,117],[17,68,42],[17,50,22,6,51,23],[19,46,16,6,47,17],[2,139,111,7,140,112],[17,74,46],[7,54,24,16,55,25],[34,37,13],
  [4,151,121,5,152,122],[4,75,47,14,76,48],[11,54,24,14,55,25],[16,45,15,14,46,16],[6,147,117,4,148,118],[6,73,45,14,74,46],[11,54,24,16,55,25],[30,46,16,2,47,17],
  [8,132,106,4,133,107],[8,75,47,13,76,48],[7,54,24,22,55,25],[22,45,15,13,46,16],[10,142,114,2,143,115],[19,74,46,4,75,47],[28,50,22,6,51,23],[33,46,16,4,47,17],
  [8,152,122,4,153,123],[22,73,45,3,74,46],[8,53,23,26,54,24],[12,45,15,28,46,16],[3,147,117,10,148,118],[3,73,45,23,74,46],[4,54,24,31,55,25],[11,45,15,31,46,16],
  [7,146,116,7,147,117],[21,73,45,7,74,46],[1,53,23,37,54,24],[19,45,15,26,46,16],[5,145,115,10,146,116],[19,75,47,10,76,48],[15,54,24,25,55,25],[23,45,15,25,46,16],
  [13,145,115,3,146,116],[2,74,46,29,75,47],[42,54,24,1,55,25],[23,45,15,28,46,16],[17,145,115],[10,74,46,23,75,47],[10,54,24,35,55,25],[19,45,15,35,46,16],
  [17,145,115,1,146,116],[14,74,46,21,75,47],[29,54,24,19,55,25],[11,45,15,46,46,16],[13,145,115,6,146,116],[14,74,46,23,75,47],[44,54,24,7,55,25],[59,46,16,1,47,17],
  [12,151,121,7,152,122],[12,75,47,26,76,48],[39,54,24,14,55,25],[22,45,15,41,46,16],[6,151,121,14,152,122],[6,75,47,34,76,48],[46,54,24,10,55,25],[2,45,15,64,46,16],
  [17,152,122,4,153,123],[29,74,46,14,75,47],[49,54,24,10,55,25],[24,45,15,46,46,16],[4,152,122,18,153,123],[13,74,46,32,75,47],[48,54,24,14,55,25],[42,45,15,32,46,16],
  [20,147,117,4,148,118],[40,75,47,7,76,48],[43,54,24,22,55,25],[10,45,15,67,46,16],[19,148,118,6,149,119],[18,75,47,31,76,48],[34,54,24,34,55,25],[20,45,15,61,46,16],
]


class _QRRSBlock:
    def __init__(self, t, d):
        self.totalCount = t
        self.dataCount = d

    @staticmethod
    def get(typeNumber, ecLevel):
        t = _RS_TABLE[(typeNumber - 1) * 4 + [1, 0, 3, 2][ecLevel]]
        blocks = []
        for i in range(0, len(t), 3):
            for _ in range(t[i]):
                blocks.append(_QRRSBlock(t[i + 1], t[i + 2]))
        return blocks


class _QRBitBuf:
    def __init__(self):
        self.buffer = []
        self.length = 0

    def put(self, num, length):
        for i in range(length):
            self.putBit(((num >> (length - i - 1)) & 1) == 1)

    def getLengthInBits(self):
        return self.length

    def putBit(self, bit):
        bi = self.length // 8
        if len(self.buffer) <= bi:
            self.buffer.append(0)
        if bit:
            self.buffer[bi] |= 0x80 >> (self.length % 8)
        self.length += 1


class _QRData:
    def __init__(self, data):
        self.mode = 4
        self.parsedData = []
        try:
            raw = data.encode('utf-8', 'strict')
        except UnicodeEncodeError:
            raise JSError('URI malformed', name='URIError')
        self.parsedData = list(raw)

    def getLength(self):
        return len(self.parsedData)

    def write(self, buf):
        for value in self.parsedData:
            buf.put(value, 8)


_MASK_FUNCS = [
    lambda i, j: (i + j) % 2 == 0,
    lambda i, j: i % 2 == 0,
    lambda i, j: j % 3 == 0,
    lambda i, j: (i + j) % 3 == 0,
    lambda i, j: (i // 2 + j // 3) % 2 == 0,
    lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
    lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0,
]

_PATPOS = [[],[6,18],[6,22],[6,26],[6,30],[6,34],[6,22,38],[6,24,42],[6,26,46],[6,28,50],[6,30,54],[6,32,58],[6,34,62],[6,26,46,66],[6,26,48,70],[6,26,50,74],[6,30,54,78],[6,30,56,82],[6,30,58,86],[6,34,62,90],[6,28,50,72,94],[6,26,50,74,98],[6,30,54,78,102],[6,28,54,80,106],[6,32,58,84,110],[6,30,58,86,114],[6,34,62,90,118],[6,26,50,74,98,122],[6,30,54,78,102,126],[6,26,52,78,104,130],[6,30,56,82,108,134],[6,34,60,86,112,138],[6,30,58,86,114,142],[6,34,62,90,118,146],[6,30,54,78,102,126,150],[6,24,50,76,102,128,154],[6,28,54,80,106,132,158],[6,32,58,84,110,136,162],[6,26,54,82,110,138,166],[6,30,58,86,114,142,170]]


class _QRCode:
    def __init__(self, typeNumber, ecLevel):
        self.typeNumber = typeNumber
        self.errorCorrectLevel = ecLevel
        self.modules = None
        self.moduleCount = 0
        self.dataCache = None
        self.dataList = []

    def addData(self, data):
        if not isinstance(data, str):
            data = str(data)
        self.dataList.append(_QRData(data))
        self.dataCache = None

    def isDark(self, r, c):
        return self.modules[r][c]

    def getModuleCount(self):
        return self.moduleCount

    def make(self):
        self._make(False, self._bestMask())

    def _make(self, test, mask):
        if self.typeNumber < 1:
            t = 1
            while t < 40:
                rs = _QRRSBlock.get(t, self.errorCorrectLevel)
                buf = _QRBitBuf()
                total = sum(b.dataCount for b in rs)
                for d in self.dataList:
                    buf.put(d.mode, 4)
                    buf.put(d.getLength(), self._lenBits(d.mode, t))
                    d.write(buf)
                if buf.getLengthInBits() <= total * 8:
                    break
                t += 1
            self.typeNumber = t
        self.moduleCount = self.typeNumber * 4 + 17
        self.modules = [[None] * self.moduleCount for _ in range(self.moduleCount)]
        self._probe(0, 0)
        self._probe(self.moduleCount - 7, 0)
        self._probe(0, self.moduleCount - 7)
        self._adjust()
        self._timing()
        self._typeInfo(test, mask)
        if self.typeNumber >= 7:
            self._typeNum(test)
        if not self.dataCache:
            self.dataCache = self._buildData()
        self._map(self.dataCache, mask)

    def _probe(self, row, col):
        mc = self.moduleCount
        for r in range(-1, 8):
            for c in range(-1, 8):
                if row + r < 0 or mc <= row + r or col + c < 0 or mc <= col + c:
                    continue
                self.modules[row + r][col + c] = (
                    (0 <= r <= 6 and (c == 0 or c == 6))
                    or (0 <= c <= 6 and (r == 0 or r == 6))
                    or (2 <= r <= 4 and 2 <= c <= 4)
                )

    def _bestMask(self):
        mn = 0
        pat = 0
        for i in range(8):
            self._make(True, i)
            lp = self._lostPoint()
            if i == 0 or mn > lp:
                mn = lp
                pat = i
        return pat

    def _timing(self):
        mc = self.moduleCount
        for r in range(8, mc - 8):
            if self.modules[r][6] is None:
                self.modules[r][6] = r % 2 == 0
        for c in range(8, mc - 8):
            if self.modules[6][c] is None:
                self.modules[6][c] = c % 2 == 0

    def _adjust(self):
        pos = self._patPos()
        for i in range(len(pos)):
            for j in range(len(pos)):
                row = pos[i]
                col = pos[j]
                if self.modules[row][col] is not None:
                    continue
                for r in range(-2, 3):
                    for c in range(-2, 3):
                        self.modules[row + r][col + c] = r == -2 or r == 2 or c == -2 or c == 2 or (r == 0 and c == 0)

    def _typeNum(self, test):
        mc = self.moduleCount
        bits = self._bchTypeNum(self.typeNumber)
        for i in range(18):
            m = (not test) and ((bits >> i) & 1) == 1
            self.modules[i // 3][i % 3 + mc - 8 - 3] = m
            self.modules[i % 3 + mc - 8 - 3][i // 3] = m

    def _typeInfo(self, test, mask):
        mc = self.moduleCount
        bits = self._bchTypeInfo((self.errorCorrectLevel << 3) | mask)
        for i in range(15):
            m = (not test) and ((bits >> i) & 1) == 1
            if i < 6:
                self.modules[i][8] = m
            elif i < 8:
                self.modules[i + 1][8] = m
            else:
                self.modules[mc - 15 + i][8] = m
            if i < 8:
                self.modules[8][mc - i - 1] = m
            elif i < 9:
                self.modules[8][15 - i] = m
            else:
                self.modules[8][15 - i - 1] = m
        self.modules[mc - 8][8] = not test

    def _map(self, data, mask):
        mc = self.moduleCount
        mf = _MASK_FUNCS[mask]
        inc = -1
        row = mc - 1
        bi = 7
        by = 0
        col = mc - 1
        while col > 0:
            if col == 6:
                col -= 1
            while True:
                for c in range(2):
                    if self.modules[row][col - c] is None:
                        dark = by < len(data) and ((data[by] >> bi) & 1) == 1
                        if mf(row, col - c):
                            dark = not dark
                        self.modules[row][col - c] = dark
                        bi -= 1
                        if bi == -1:
                            by += 1
                            bi = 7
                row += inc
                if row < 0 or mc <= row:
                    row -= inc
                    inc = -inc
                    break
            col -= 2

    def _lostPoint(self):
        m = self.moduleCount
        lp = 0
        for r in range(m):
            for c in range(m):
                sc = 0
                dark = self.isDark(r, c)
                for dr in range(-1, 2):
                    for dc in range(-1, 2):
                        if r + dr < 0 or m <= r + dr or c + dc < 0 or m <= c + dc or (not dr and not dc):
                            continue
                        if dark == self.isDark(r + dr, c + dc):
                            sc += 1
                if sc > 5:
                    lp += 3 + sc - 5
        for r in range(m - 1):
            for c in range(m - 1):
                cnt = 0
                if self.isDark(r, c):
                    cnt += 1
                if self.isDark(r + 1, c):
                    cnt += 1
                if self.isDark(r, c + 1):
                    cnt += 1
                if self.isDark(r + 1, c + 1):
                    cnt += 1
                if cnt == 0 or cnt == 4:
                    lp += 3
        for r in range(m):
            for c in range(m - 6):
                if (self.isDark(r, c) and not self.isDark(r, c + 1) and self.isDark(r, c + 2)
                        and self.isDark(r, c + 3) and self.isDark(r, c + 4) and not self.isDark(r, c + 5)
                        and self.isDark(r, c + 6)):
                    lp += 40
        for c in range(m):
            for r in range(m - 6):
                if (self.isDark(r, c) and not self.isDark(r + 1, c) and self.isDark(r + 2, c)
                        and self.isDark(r + 3, c) and self.isDark(r + 4, c) and not self.isDark(r + 5, c)
                        and self.isDark(r + 6, c)):
                    lp += 40
        dcount = 0
        for c in range(m):
            for r in range(m):
                if self.isDark(r, c):
                    dcount += 1
        lp += abs(math.floor(dcount * 100 / m / m - 50)) / 5 * 10
        return lp

    def _patPos(self):
        return _PATPOS[self.typeNumber - 1]

    def _bchTypeInfo(self, d):
        x = d << 10
        while self._bchDigit(x) - self._bchDigit(0x537) >= 0:
            x ^= 0x537 << (self._bchDigit(x) - self._bchDigit(0x537))
        return ((d << 10) | x) ^ 0x5412

    def _bchTypeNum(self, d):
        x = d << 12
        while self._bchDigit(x) - self._bchDigit(0x1F25) >= 0:
            x ^= 0x1F25 << (self._bchDigit(x) - self._bchDigit(0x1F25))
        return (d << 12) | x

    def _bchDigit(self, d):
        n = 0
        while d != 0:
            n += 1
            d >>= 1
        return n

    def _lenBits(self, mode, t):
        if mode == 1:
            return 10 if t < 10 else 12 if t < 27 else 14
        if mode == 2:
            return 9 if t < 10 else 11 if t < 27 else 13
        if mode == 4:
            return 8 if t < 10 else 16
        if mode == 8:
            return 8 if t < 10 else 10 if t < 27 else 12
        return None

    def _buildData(self):
        rs = _QRRSBlock.get(self.typeNumber, self.errorCorrectLevel)
        buf = _QRBitBuf()
        for d in self.dataList:
            buf.put(d.mode, 4)
            buf.put(d.getLength(), self._lenBits(d.mode, self.typeNumber))
            d.write(buf)
        total = sum(b.dataCount for b in rs)
        if buf.getLengthInBits() > total * 8:
            raise JSError('code length overflow')
        if buf.getLengthInBits() + 4 <= total * 8:
            buf.put(0, 4)
        while buf.getLengthInBits() % 8 != 0:
            buf.putBit(False)
        while True:
            if buf.getLengthInBits() >= total * 8:
                break
            buf.put(0xEC, 8)
            if buf.getLengthInBits() >= total * 8:
                break
            buf.put(0x11, 8)
        return self._buildBytes(buf, rs)

    def _buildBytes(self, buf, rs):
        off = 0
        maxDc = 0
        maxEc = 0
        dc = [None] * len(rs)
        ec = [None] * len(rs)
        for r in range(len(rs)):
            dCnt = rs[r].dataCount
            eCnt = rs[r].totalCount - dCnt
            maxDc = max(maxDc, dCnt)
            maxEc = max(maxEc, eCnt)
            dc[r] = [0xFF & buf.buffer[i + off] for i in range(dCnt)]
            off += dCnt
            rsp = self._ecPoly(eCnt)
            raw = _QRPoly(dc[r], rsp.getLength() - 1)
            mod = raw.mod(rsp)
            ec[r] = [0] * (rsp.getLength() - 1)
            for i in range(len(ec[r])):
                mi = i + mod.getLength() - len(ec[r])
                ec[r][i] = mod.get(mi) if mi >= 0 else 0
        total = sum(b.totalCount for b in rs)
        data = [0] * total
        idx = 0
        for i in range(maxDc):
            for r in range(len(rs)):
                if i < len(dc[r]):
                    data[idx] = dc[r][i]
                    idx += 1
        for i in range(maxEc):
            for r in range(len(rs)):
                if i < len(ec[r]):
                    data[idx] = ec[r][i]
                    idx += 1
        return data

    def _ecPoly(self, length):
        a = _QRPoly([1], 0)
        for i in range(length):
            a = a.multiply(_QRPoly([1, _qrGexp(i)], 0))
        return a


def generateQR(input, opts=None, cb=None):
    if callable(opts):
        cb = opts
        opts = None
    if opts is None:
        opts = JSObject()
    qr = _QRCode(-1, _QRErrorCorrectLevel['L'])
    qr.addData(input)
    qr.make()
    black = '\x1b[40m  \x1b[0m'
    white = '\x1b[47m  \x1b[0m'
    output = ''
    if opts.get('small'):
        mc = qr.getModuleCount()
        md = [list(row) for row in qr.modules]
        if mc % 2 == 1:
            md.append([False] * mc)
        ww = '█'
        wb = '▀'
        bw = '▄'
        bb = ' '
        output += bw * (mc + 3) + '\n'
        for r in range(0, mc, 2):
            output += ww
            for c in range(mc):
                t = md[r][c]
                b = md[r + 1][c]
                if not t and not b:
                    output += ww
                elif not t and b:
                    output += wb
                elif t and not b:
                    output += bw
                else:
                    output += bb
            output += ww + '\n'
        if mc % 2 == 0:
            output += wb * (mc + 3)
    else:
        border = white * (qr.getModuleCount() + 2)
        output += border + '\n'
        for row in qr.modules:
            output += white + ''.join(black if c else white for c in row) + white + '\n'
        output += border
    if cb is not None:
        cb(output)
    else:
        print(output)
