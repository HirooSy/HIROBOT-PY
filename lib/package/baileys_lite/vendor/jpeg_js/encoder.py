import math

from baileys_lite.foundation.node_runtime import JSError, JSObject


ZigZag = [
    0, 1, 5, 6, 14, 15, 27, 28,
    2, 4, 7, 13, 16, 26, 29, 42,
    3, 8, 12, 17, 25, 30, 41, 43,
    9, 11, 18, 24, 31, 40, 44, 53,
    10, 19, 23, 32, 39, 45, 52, 54,
    20, 22, 33, 38, 46, 51, 55, 60,
    21, 34, 37, 47, 50, 56, 59, 61,
    35, 36, 48, 49, 57, 58, 62, 63,
]

std_dc_luminance_nrcodes = [0, 0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
std_dc_luminance_values = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
std_ac_luminance_nrcodes = [0, 0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7d]
std_ac_luminance_values = [
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12,
    0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07,
    0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xa1, 0x08,
    0x23, 0x42, 0xb1, 0xc1, 0x15, 0x52, 0xd1, 0xf0,
    0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0a, 0x16,
    0x17, 0x18, 0x19, 0x1a, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2a, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39,
    0x3a, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
    0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
    0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79,
    0x7a, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98,
    0x99, 0x9a, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7,
    0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6,
    0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5,
    0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2, 0xd3, 0xd4,
    0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xe1, 0xe2,
    0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea,
    0xf1, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8,
    0xf9, 0xfa,
]

std_dc_chrominance_nrcodes = [0, 0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
std_dc_chrominance_values = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
std_ac_chrominance_nrcodes = [0, 0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77]
std_ac_chrominance_values = [
    0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21,
    0x31, 0x06, 0x12, 0x41, 0x51, 0x07, 0x61, 0x71,
    0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
    0xa1, 0xb1, 0xc1, 0x09, 0x23, 0x33, 0x52, 0xf0,
    0x15, 0x62, 0x72, 0xd1, 0x0a, 0x16, 0x24, 0x34,
    0xe1, 0x25, 0xf1, 0x17, 0x18, 0x19, 0x1a, 0x26,
    0x27, 0x28, 0x29, 0x2a, 0x35, 0x36, 0x37, 0x38,
    0x39, 0x3a, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48,
    0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
    0x59, 0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68,
    0x69, 0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78,
    0x79, 0x7a, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8a, 0x92, 0x93, 0x94, 0x95, 0x96,
    0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3, 0xa4, 0xa5,
    0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4,
    0xb5, 0xb6, 0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3,
    0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2,
    0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda,
    0xe2, 0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9,
    0xea, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8,
    0xf9, 0xfa,
]


def computeHuffmanTbl(nrcodes, std_table):
    codevalue = 0
    pos_in_table = 0
    HT = {}
    for k in range(1, 17):
        for j in range(1, nrcodes[k] + 1):
            HT[std_table[pos_in_table]] = [codevalue, k]
            pos_in_table += 1
            codevalue += 1
        codevalue *= 2
    return HT


class JPEGEncoder:
    def __init__(self, quality):
        self.quality = quality
        self.YTable = [0] * 64
        self.UVTable = [0] * 64
        self.fdtbl_Y = [0] * 64
        self.fdtbl_UV = [0] * 64
        self.YDC_HT = None
        self.UVDC_HT = None
        self.YAC_HT = None
        self.UVAC_HT = None

        self.bitcode = {}
        self.category = {}
        self.outputfDCTQuant = [0] * 64
        self.DU = [0] * 64
        self.byteout = []
        self.bytenew = 0
        self.bytepos = 7

        self.YDU = [0] * 64
        self.UDU = [0] * 64
        self.VDU = [0] * 64
        self.clt = [None] * 256
        self.RGB_YUV_TABLE = [0] * 2048
        self.currentQuality = None
        self.init()

    def initQuantTables(self, sf):
        YQT = [
            16, 11, 10, 16, 24, 40, 51, 61,
            12, 12, 14, 19, 26, 58, 60, 55,
            14, 13, 16, 24, 40, 57, 69, 56,
            14, 17, 22, 29, 51, 87, 80, 62,
            18, 22, 37, 56, 68, 109, 103, 77,
            24, 35, 55, 64, 81, 104, 113, 92,
            49, 64, 78, 87, 103, 121, 120, 101,
            72, 92, 95, 98, 112, 100, 103, 99,
        ]

        for i in range(64):
            t = math.floor((YQT[i] * sf + 50) / 100)
            if t < 1:
                t = 1
            elif t > 255:
                t = 255
            self.YTable[ZigZag[i]] = t
        UVQT = [
            17, 18, 24, 47, 99, 99, 99, 99,
            18, 21, 26, 66, 99, 99, 99, 99,
            24, 26, 56, 99, 99, 99, 99, 99,
            47, 66, 99, 99, 99, 99, 99, 99,
            99, 99, 99, 99, 99, 99, 99, 99,
            99, 99, 99, 99, 99, 99, 99, 99,
            99, 99, 99, 99, 99, 99, 99, 99,
            99, 99, 99, 99, 99, 99, 99, 99,
        ]
        for j in range(64):
            u = math.floor((UVQT[j] * sf + 50) / 100)
            if u < 1:
                u = 1
            elif u > 255:
                u = 255
            self.UVTable[ZigZag[j]] = u
        aasf = [
            1.0, 1.387039845, 1.306562965, 1.175875602,
            1.0, 0.785694958, 0.541196100, 0.275899379,
        ]
        k = 0
        for row in range(8):
            for col in range(8):
                self.fdtbl_Y[k] = (1.0 / (self.YTable[ZigZag[k]] * aasf[row] * aasf[col] * 8.0))
                self.fdtbl_UV[k] = (1.0 / (self.UVTable[ZigZag[k]] * aasf[row] * aasf[col] * 8.0))
                k += 1

    def initHuffmanTbl(self):
        self.YDC_HT = computeHuffmanTbl(std_dc_luminance_nrcodes, std_dc_luminance_values)
        self.UVDC_HT = computeHuffmanTbl(std_dc_chrominance_nrcodes, std_dc_chrominance_values)
        self.YAC_HT = computeHuffmanTbl(std_ac_luminance_nrcodes, std_ac_luminance_values)
        self.UVAC_HT = computeHuffmanTbl(std_ac_chrominance_nrcodes, std_ac_chrominance_values)

    def initCategoryNumber(self):
        nrlower = 1
        nrupper = 2
        for cat in range(1, 16):
            for nr in range(nrlower, nrupper):
                self.category[32767 + nr] = cat
                self.bitcode[32767 + nr] = [nr, cat]

            for nrneg in range(-(nrupper - 1), -nrlower + 1):
                self.category[32767 + nrneg] = cat
                self.bitcode[32767 + nrneg] = [nrupper - 1 + nrneg, cat]
            nrlower <<= 1
            nrupper <<= 1

    def initRGBYUVTable(self):
        T = self.RGB_YUV_TABLE
        for i in range(256):
            T[i] = 19595 * i
            T[i + 256] = 38470 * i
            T[i + 512] = 7471 * i + 0x8000
            T[i + 768] = -11059 * i
            T[i + 1024] = -21709 * i
            T[i + 1280] = 32768 * i + 0x807FFF
            T[i + 1536] = -27439 * i
            T[i + 1792] = -5329 * i

    def writeBits(self, bs):
        value = bs[0]
        posval = bs[1] - 1
        while posval >= 0:
            if value & (1 << posval):
                self.bytenew |= (1 << self.bytepos)
            posval -= 1
            self.bytepos -= 1
            if self.bytepos < 0:
                if self.bytenew == 0xFF:
                    self.writeByte(0xFF)
                    self.writeByte(0)
                else:
                    self.writeByte(self.bytenew)
                self.bytepos = 7
                self.bytenew = 0

    def writeByte(self, value):
        self.byteout.append(value)

    def writeWord(self, value):
        self.writeByte((value >> 8) & 0xFF)
        self.writeByte(value & 0xFF)

    def fDCTQuant(self, data, fdtbl):
        for i in range(8):
            dataOff = i * 8
            d0 = data[dataOff]
            d1 = data[dataOff + 1]
            d2 = data[dataOff + 2]
            d3 = data[dataOff + 3]
            d4 = data[dataOff + 4]
            d5 = data[dataOff + 5]
            d6 = data[dataOff + 6]
            d7 = data[dataOff + 7]

            tmp0 = d0 + d7
            tmp7 = d0 - d7
            tmp1 = d1 + d6
            tmp6 = d1 - d6
            tmp2 = d2 + d5
            tmp5 = d2 - d5
            tmp3 = d3 + d4
            tmp4 = d3 - d4

            tmp10 = tmp0 + tmp3
            tmp13 = tmp0 - tmp3
            tmp11 = tmp1 + tmp2
            tmp12 = tmp1 - tmp2

            data[dataOff] = tmp10 + tmp11
            data[dataOff + 4] = tmp10 - tmp11

            z1 = (tmp12 + tmp13) * 0.707106781
            data[dataOff + 2] = tmp13 + z1
            data[dataOff + 6] = tmp13 - z1

            tmp10 = tmp4 + tmp5
            tmp11 = tmp5 + tmp6
            tmp12 = tmp6 + tmp7

            z5 = (tmp10 - tmp12) * 0.382683433
            z2 = 0.541196100 * tmp10 + z5
            z4 = 1.306562965 * tmp12 + z5
            z3 = tmp11 * 0.707106781

            z11 = tmp7 + z3
            z13 = tmp7 - z3

            data[dataOff + 5] = z13 + z2
            data[dataOff + 3] = z13 - z2
            data[dataOff + 1] = z11 + z4
            data[dataOff + 7] = z11 - z4

        for i in range(8):
            dataOff = i
            d0 = data[dataOff]
            d1 = data[dataOff + 8]
            d2 = data[dataOff + 16]
            d3 = data[dataOff + 24]
            d4 = data[dataOff + 32]
            d5 = data[dataOff + 40]
            d6 = data[dataOff + 48]
            d7 = data[dataOff + 56]

            tmp0p2 = d0 + d7
            tmp7p2 = d0 - d7
            tmp1p2 = d1 + d6
            tmp6p2 = d1 - d6
            tmp2p2 = d2 + d5
            tmp5p2 = d2 - d5
            tmp3p2 = d3 + d4
            tmp4p2 = d3 - d4

            tmp10p2 = tmp0p2 + tmp3p2
            tmp13p2 = tmp0p2 - tmp3p2
            tmp11p2 = tmp1p2 + tmp2p2
            tmp12p2 = tmp1p2 - tmp2p2

            data[dataOff] = tmp10p2 + tmp11p2
            data[dataOff + 32] = tmp10p2 - tmp11p2

            z1p2 = (tmp12p2 + tmp13p2) * 0.707106781
            data[dataOff + 16] = tmp13p2 + z1p2
            data[dataOff + 48] = tmp13p2 - z1p2

            tmp10p2 = tmp4p2 + tmp5p2
            tmp11p2 = tmp5p2 + tmp6p2
            tmp12p2 = tmp6p2 + tmp7p2

            z5p2 = (tmp10p2 - tmp12p2) * 0.382683433
            z2p2 = 0.541196100 * tmp10p2 + z5p2
            z4p2 = 1.306562965 * tmp12p2 + z5p2
            z3p2 = tmp11p2 * 0.707106781

            z11p2 = tmp7p2 + z3p2
            z13p2 = tmp7p2 - z3p2

            data[dataOff + 40] = z13p2 + z2p2
            data[dataOff + 24] = z13p2 - z2p2
            data[dataOff + 8] = z11p2 + z4p2
            data[dataOff + 56] = z11p2 - z4p2

        outputfDCTQuant = self.outputfDCTQuant
        for i in range(64):
            fDCTQuantValue = data[i] * fdtbl[i]
            outputfDCTQuant[i] = int(fDCTQuantValue + 0.5) if fDCTQuantValue > 0.0 else int(fDCTQuantValue - 0.5)
        return outputfDCTQuant

    def writeAPP0(self):
        self.writeWord(0xFFE0)
        self.writeWord(16)
        self.writeByte(0x4A)
        self.writeByte(0x46)
        self.writeByte(0x49)
        self.writeByte(0x46)
        self.writeByte(0)
        self.writeByte(1)
        self.writeByte(1)
        self.writeByte(0)
        self.writeWord(1)
        self.writeWord(1)
        self.writeByte(0)
        self.writeByte(0)

    def writeAPP1(self, exifBuffer):
        if exifBuffer is None:
            return

        self.writeWord(0xFFE1)

        if (
            len(exifBuffer) >= 4
            and exifBuffer[0] == 0x45
            and exifBuffer[1] == 0x78
            and exifBuffer[2] == 0x69
            and exifBuffer[3] == 0x66
        ):
            self.writeWord(len(exifBuffer) + 2)
        else:
            self.writeWord(len(exifBuffer) + 5 + 2)
            self.writeByte(0x45)
            self.writeByte(0x78)
            self.writeByte(0x69)
            self.writeByte(0x66)
            self.writeByte(0)

        for i in range(len(exifBuffer)):
            self.writeByte(exifBuffer[i])

    def writeSOF0(self, width, height):
        self.writeWord(0xFFC0)
        self.writeWord(17)
        self.writeByte(8)
        self.writeWord(height)
        self.writeWord(width)
        self.writeByte(3)
        self.writeByte(1)
        self.writeByte(0x11)
        self.writeByte(0)
        self.writeByte(2)
        self.writeByte(0x11)
        self.writeByte(1)
        self.writeByte(3)
        self.writeByte(0x11)
        self.writeByte(1)

    def writeDQT(self):
        self.writeWord(0xFFDB)
        self.writeWord(132)
        self.writeByte(0)
        for i in range(64):
            self.writeByte(self.YTable[i])
        self.writeByte(1)
        for j in range(64):
            self.writeByte(self.UVTable[j])

    def writeDHT(self):
        self.writeWord(0xFFC4)
        self.writeWord(0x01A2)

        self.writeByte(0)
        for i in range(16):
            self.writeByte(std_dc_luminance_nrcodes[i + 1])
        for j in range(12):
            self.writeByte(std_dc_luminance_values[j])

        self.writeByte(0x10)
        for k in range(16):
            self.writeByte(std_ac_luminance_nrcodes[k + 1])
        for l in range(162):
            self.writeByte(std_ac_luminance_values[l])

        self.writeByte(1)
        for m in range(16):
            self.writeByte(std_dc_chrominance_nrcodes[m + 1])
        for n in range(12):
            self.writeByte(std_dc_chrominance_values[n])

        self.writeByte(0x11)
        for o in range(16):
            self.writeByte(std_ac_chrominance_nrcodes[o + 1])
        for p in range(162):
            self.writeByte(std_ac_chrominance_values[p])

    def writeCOM(self, comments):
        if comments is None or not isinstance(comments, list):
            return
        for e in comments:
            if not isinstance(e, str):
                continue
            self.writeWord(0xFFFE)
            units = e.encode('utf-16-le', 'surrogatepass')
            codeUnits = [units[i] | (units[i + 1] << 8) for i in range(0, len(units), 2)]
            self.writeWord(len(codeUnits) + 2)
            for unit in codeUnits:
                self.writeByte(unit)

    def writeSOS(self):
        self.writeWord(0xFFDA)
        self.writeWord(12)
        self.writeByte(3)
        self.writeByte(1)
        self.writeByte(0)
        self.writeByte(2)
        self.writeByte(0x11)
        self.writeByte(3)
        self.writeByte(0x11)
        self.writeByte(0)
        self.writeByte(0x3f)
        self.writeByte(0)

    def processDU(self, CDU, fdtbl, DC, HTDC, HTAC):
        EOB = HTAC[0x00]
        M16zeroes = HTAC[0xF0]
        DU = self.DU
        DU_DCT = self.fDCTQuant(CDU, fdtbl)

        for j in range(64):
            DU[ZigZag[j]] = DU_DCT[j]
        Diff = DU[0] - DC
        DC = DU[0]

        if Diff == 0:
            self.writeBits(HTDC[0])
        else:
            pos = 32767 + Diff
            self.writeBits(HTDC[self.category[pos]])
            self.writeBits(self.bitcode[pos])

        end0pos = 63
        while end0pos > 0 and DU[end0pos] == 0:
            end0pos -= 1

        if end0pos == 0:
            self.writeBits(EOB)
            return DC
        i = 1
        while i <= end0pos:
            startpos = i
            while DU[i] == 0 and i <= end0pos:
                i += 1
            nrzeroes = i - startpos
            if nrzeroes >= 16:
                lng = nrzeroes >> 4
                for nrmarker in range(1, lng + 1):
                    self.writeBits(M16zeroes)
                nrzeroes = nrzeroes & 0xF
            pos = 32767 + DU[i]
            self.writeBits(HTAC[(nrzeroes << 4) + self.category[pos]])
            self.writeBits(self.bitcode[pos])
            i += 1
        if end0pos != 63:
            self.writeBits(EOB)
        return DC

    def initCharLookupTable(self):
        for i in range(256):
            self.clt[i] = chr(i)

    def encode(self, image, quality=None):
        if quality:
            self.setQuality(quality)

        self.byteout = []
        self.bytenew = 0
        self.bytepos = 7

        self.writeWord(0xFFD8)
        self.writeAPP0()
        self.writeCOM(image.get('comments'))
        self.writeAPP1(image.get('exifBuffer'))
        self.writeDQT()
        self.writeSOF0(image.get('width'), image.get('height'))
        self.writeDHT()
        self.writeSOS()

        DCY = 0
        DCU = 0
        DCV = 0

        self.bytenew = 0
        self.bytepos = 7

        imageData = image.get('data')
        width = image.get('width')
        height = image.get('height')

        quadWidth = width * 4

        YDU = self.YDU
        UDU = self.UDU
        VDU = self.VDU
        RGB = self.RGB_YUV_TABLE

        y = 0
        while y < height:
            x = 0
            while x < quadWidth:
                start = quadWidth * y + x
                p = start
                col = -1
                row = 0

                for pos in range(64):
                    row = pos >> 3
                    col = (pos & 7) * 4
                    p = start + (row * quadWidth) + col

                    if y + row >= height:
                        p -= (quadWidth * (y + 1 + row - height))

                    if x + col >= quadWidth:
                        p -= ((x + col) - quadWidth + 4)

                    r = imageData[p]
                    g = imageData[p + 1]
                    b = imageData[p + 2]

                    YDU[pos] = ((RGB[r] + RGB[g + 256] + RGB[b + 512]) >> 16) - 128
                    UDU[pos] = ((RGB[r + 768] + RGB[g + 1024] + RGB[b + 1280]) >> 16) - 128
                    VDU[pos] = ((RGB[r + 1280] + RGB[g + 1536] + RGB[b + 1792]) >> 16) - 128

                DCY = self.processDU(YDU, self.fdtbl_Y, DCY, self.YDC_HT, self.YAC_HT)
                DCU = self.processDU(UDU, self.fdtbl_UV, DCU, self.UVDC_HT, self.UVAC_HT)
                DCV = self.processDU(VDU, self.fdtbl_UV, DCV, self.UVDC_HT, self.UVAC_HT)
                x += 32
            y += 8

        if self.bytepos >= 0:
            fillbits = [0, 0]
            fillbits[1] = self.bytepos + 1
            fillbits[0] = (1 << (self.bytepos + 1)) - 1
            self.writeBits(fillbits)

        self.writeWord(0xFFD9)

        return bytes([value & 0xFF for value in self.byteout])

    def setQuality(self, quality):
        if quality <= 0:
            quality = 1
        if quality > 100:
            quality = 100

        if self.currentQuality == quality:
            return

        sf = 0
        if quality < 50:
            sf = math.floor(5000 / quality)
        else:
            sf = math.floor(200 - quality * 2)

        self.initQuantTables(sf)
        self.currentQuality = quality

    def init(self):
        quality = self.quality
        if not quality:
            quality = 50
            self.quality = quality

        self.initCharLookupTable()
        self.initHuffmanTbl()
        self.initCategoryNumber()
        self.initRGBYUVTable()

        self.setQuality(quality)


def encode(imgData, qu=None):
    if qu is None:
        qu = 50
    imgData = JSObject(imgData)
    encoder = JPEGEncoder(qu)
    data = encoder.encode(imgData, qu)
    return JSObject({
        'data': data,
        'width': imgData.get('width'),
        'height': imgData.get('height'),
    })


def getImageDataFromImage(idOrElement):
    raise JSError('document is not defined', name='ReferenceError')


default = encode
