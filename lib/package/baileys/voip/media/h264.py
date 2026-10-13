from baileys.foundation.node_runtime import JSError

H264_STAP_A_TYPE = 24
H264_FUA_TYPE = 28
MTU_PAYLOAD_MAX = 800


def auHasIDR(au):
    for nalu in splitAnnexB(au):
        if len(nalu) > 0 and (nalu[0] & 0x1f) == 5:
            return True
    return False


def packageH264NALU(nalu):
    if not nalu or len(nalu) == 0:
        return []
    if len(nalu) <= MTU_PAYLOAD_MAX:
        return [bytes(nalu)]
    naluHeader = nalu[0]
    fbitAndNri = naluHeader & 0xe0
    originalType = naluHeader & 0x1f
    fuIndicator = fbitAndNri | H264_FUA_TYPE

    body = nalu[1:]
    fragSize = MTU_PAYLOAD_MAX - 2

    out = []
    offset = 0
    while offset < len(body):
        end = min(offset + fragSize, len(body))
        chunk = body[offset:end]

        fuHeader = originalType
        if offset == 0:
            fuHeader |= 0x80
        if end == len(body):
            fuHeader |= 0x40

        pkt = bytearray(2 + len(chunk))
        pkt[0] = fuIndicator
        pkt[1] = fuHeader
        pkt[2:] = chunk
        out.append(bytes(pkt))

        offset = end
    return out


def annexBStartCodeLen(data, offset):
    if offset + 3 < len(data) and data[offset] == 0 and data[offset + 1] == 0 and data[offset + 2] == 0 and data[offset + 3] == 1:
        return 4
    if offset + 2 < len(data) and data[offset] == 0 and data[offset + 1] == 0 and data[offset + 2] == 1:
        return 3
    return 0


def splitAnnexB(data):
    nalus = []
    start = -1
    i = 0
    while i < len(data):
        sc = annexBStartCodeLen(data, i)
        if sc > 0:
            if start >= 0:
                end = i
                while end > start and data[end - 1] == 0:
                    end -= 1
                if end > start:
                    nalus.append(data[start:end])
            i += sc
            start = i
            continue
        i += 1
    if start >= 0 and start < len(data):
        nalus.append(data[start:])
    return nalus


DROP_NAL_TYPES = frozenset([6, 9, 10, 11, 12])


def buildAccessUnitPayload(au):
    nalus = splitAnnexB(au)
    parts = []
    for n in nalus:
        if len(n) == 0 or (n[0] & 0x1f) in DROP_NAL_TYPES:
            continue
        if len(parts) > 0:
            parts.append(bytes((0, 0, 0, 1)))
        parts.append(n)
    if len(parts) == 0:
        return None
    return b''.join(parts)


MAX_FU_REASSEMBLY_BYTES = 4 << 20
MAX_ACCESS_UNIT_BYTES = 8 << 20


class H264Depacketizer:
    def __init__(self):
        self.fuBuf = bytearray()
        self.fuActive = False
        self.overflow = False

    def depacketize(self, payload):
        if not payload or len(payload) < 1:
            return []
        naluType = payload[0] & 0x1f
        fbitAndNri = payload[0] & 0xe0

        if 1 <= naluType <= 23:
            self.fuActive = False
            return [bytes(payload)]

        if naluType == H264_STAP_A_TYPE:
            self.fuActive = False
            body = payload[1:]
            out = []
            while len(body) >= 2:
                size = (body[0] << 8) | body[1]
                body = body[2:]
                if size <= 0 or size > len(body):
                    return out
                out.append(body[0:size])
                body = body[size:]
            return out

        if naluType == H264_FUA_TYPE:
            if len(payload) < 2:
                self.fuActive = False
                return []
            fuHeader = payload[1]
            startBit = fuHeader & 0x80
            endBit = fuHeader & 0x40
            origType = fuHeader & 0x1f
            body = payload[2:]

            if startBit != 0:
                if 1 + len(body) > MAX_FU_REASSEMBLY_BYTES:
                    self.fuActive = False
                    self.fuBuf = bytearray()
                    self.overflow = True
                    return []
                self.fuActive = True
                self.fuBuf = bytearray(1 + len(body))
                self.fuBuf[0] = fbitAndNri | origType
                self.fuBuf[1:] = body
            elif self.fuActive:
                if len(self.fuBuf) + len(body) > MAX_FU_REASSEMBLY_BYTES:
                    self.fuActive = False
                    self.fuBuf = bytearray()
                    self.overflow = True
                    return []
                merged = bytearray(len(self.fuBuf) + len(body))
                merged[0:len(self.fuBuf)] = self.fuBuf
                merged[len(self.fuBuf):] = body
                self.fuBuf = merged
            else:
                return []

            if endBit != 0 and self.fuActive:
                self.fuActive = False
                out = self.fuBuf
                self.fuBuf = bytearray()
                return [out]
            return []

        self.fuActive = False
        return []


class H264AccessUnitAssembler:
    def __init__(self):
        self.depacketizer = H264Depacketizer()
        self.accessUnit = bytearray()
        self.expectedSeq = 0
        self.hasSequence = False
        self.keyframeNeeded = False

    def reset(self):
        self.depacketizer = H264Depacketizer()
        self.accessUnit = bytearray()

    def push(self, sequence, marker, payload):
        recoveryNeeded = False
        if self.hasSequence and sequence != self.expectedSeq:
            recoveryNeeded = not self.keyframeNeeded
            self.reset()
            self.keyframeNeeded = True
        self.hasSequence = True
        self.expectedSeq = (sequence + 1) & 0xffff

        for nalu in self.depacketizer.depacketize(payload):
            current = len(self.accessUnit)
            if current + 4 + len(nalu) > MAX_ACCESS_UNIT_BYTES:
                recoveryNeeded = recoveryNeeded or not self.keyframeNeeded
                self.reset()
                self.keyframeNeeded = True
                break
            merged = bytearray(current + 4 + len(nalu))
            merged[0:current] = self.accessUnit
            merged[current:current + 4] = bytes((0, 0, 0, 1))
            merged[current + 4:] = nalu
            self.accessUnit = merged

        if self.depacketizer.overflow:
            recoveryNeeded = recoveryNeeded or not self.keyframeNeeded
            self.reset()
            self.keyframeNeeded = True

        if not marker:
            return [None, False, recoveryNeeded]

        accessUnit = self.accessUnit
        self.reset()
        if len(accessUnit) == 0:
            return [None, False, recoveryNeeded]
        if self.keyframeNeeded:
            if not auHasIDR(accessUnit):
                return [None, False, recoveryNeeded]
            self.keyframeNeeded = False
        return [accessUnit, True, recoveryNeeded]
