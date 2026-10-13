import math
import os
import re
import struct

from baileys.foundation.node_runtime import JSObject


def u32be(b, o):
    return struct.unpack_from('>I', b, o)[0]


def u64be(b, o):
    return struct.unpack_from('>Q', b, o)[0]


def ascii(b, o, n):
    return bytes(b[o:o + n]).decode('latin-1')


def _jsDiv(a, b):
    if b == 0:
        if a == 0 or math.isnan(a):
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def _jsTruthy(value):
    if value is None or value is False:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return value != 0


def _parseIntPrefix(text):
    match = re.match(r'[+-]?\d+', text)
    if match is None:
        return math.nan
    return int(match.group(0))


def _pow2(exponent):
    try:
        return 2.0 ** exponent
    except OverflowError:
        return math.inf


def oggDuration(buf):
    streams = {}
    pos = 0
    endOfStream = False
    while True:
        if pos + 27 > len(buf):
            endOfStream = True
            break
        if ascii(buf, pos, 4) != 'OggS':
            break
        headerType = buf[pos + 5]
        granule = struct.unpack_from('<q', buf, pos + 6)[0]
        serial = struct.unpack_from('<I', buf, pos + 14)[0]
        nseg = buf[pos + 26]
        s = streams.get(serial)
        if not s:
            s = JSObject({
                'codec': None,
                'sampleRate': None,
                'preSkip': 0,
                'last': None,
                'closed': False,
                'pageNumber': 0,
                'fullPages': 0,
                'pageCount': 0,
            })
            streams[serial] = s
        s['pageNumber'] = struct.unpack_from('<I', buf, pos + 18)[0]
        if pos + 27 + nseg > len(buf):
            endOfStream = True
            break
        dataLen = 0
        for i in range(nseg):
            dataLen += buf[pos + 27 + i]
        dataStart = pos + 27 + nseg
        if dataStart + dataLen > len(buf):
            endOfStream = True
            break
        if headerType & 0x02:
            idText = ''.join(chr(b) for b in buf[dataStart:dataStart + 7] if 32 <= b <= 126)
            if idText == 'OpusHea':
                s['codec'] = 'opus'
                s['preSkip'] = struct.unpack_from('<H', buf, dataStart + 10)[0]
                s['sampleRate'] = struct.unpack_from('<I', buf, dataStart + 12)[0]
            elif idText == 'vorbis':
                s['codec'] = 'vorbis'
                s['sampleRate'] = struct.unpack_from('<I', buf, dataStart + 12)[0]
            else:
                s['codec'] = 'other'
        if headerType & 0x04:
            s['closed'] = True
        elif not (headerType & 0x02):
            s['fullPages'] += 1
        s['pageCount'] += 1
        s['last'] = JSObject({'granule': granule, 'lastPage': bool(headerType & 0x04)})
        pos = dataStart + dataLen

        if all(x.get('closed') for x in streams.values()):
            break
    for s in streams.values():
        if not s.get('last') or not s.get('sampleRate'):
            continue
        if not (endOfStream or s.get('last').get('lastPage')):
            continue
        if s.get('last').get('granule') < 0:
            continue

        if s.get('codec') == 'vorbis' and endOfStream and not s.get('last').get('lastPage') and s.get('fullPages') == 0 and s.get('pageCount') > 0:
            return None
        g = float(s.get('last').get('granule'))
        if s.get('codec') == 'opus':
            return (g - s.get('preSkip')) / 48000
        if s.get('codec') == 'vorbis':
            return g / s.get('sampleRate')
    return None


def wavDuration(buf):
    if ascii(buf, 0, 4) != 'RIFF' or ascii(buf, 8, 4) != 'WAVE':
        return None
    pos = 12
    sampleRate = None
    blockAlign = 0
    fact = None
    while pos + 8 <= len(buf):
        chunkId = ascii(buf, pos, 4)
        size = struct.unpack_from('<I', buf, pos + 4)[0]
        body = pos + 8
        if chunkId == 'fmt ':
            sampleRate = struct.unpack_from('<I', buf, body + 4)[0]
            blockAlign = struct.unpack_from('<H', buf, body + 12)[0]
        elif chunkId == 'fact':
            fact = struct.unpack_from('<I', buf, body)[0]
        elif chunkId == 'data':
            chunkSize = size
            remaining = len(buf) - body
            if remaining < chunkSize:
                chunkSize = remaining
            if fact is not None:
                samples = fact
            elif chunkSize == 0xffffffff:
                samples = None
            else:
                samples = _jsDiv(chunkSize, blockAlign)
            if _jsTruthy(samples) and _jsTruthy(sampleRate):
                return samples / sampleRate
            return None
        pos = body + size + (size % 2)
    return None


def flacDuration(buf):
    if ascii(buf, 0, 4) != 'fLaC':
        return None
    pos = 4
    while pos + 4 <= len(buf):
        h = buf[pos]
        headerType = h & 0x7f
        length = (buf[pos + 1] << 16) | (buf[pos + 2] << 8) | buf[pos + 3]
        if headerType == 0:
            if length != 34:
                return None

            if pos + 4 + 34 > len(buf):
                return None
            o = pos + 4
            sampleRate = ((buf[o + 10] << 16) | (buf[o + 11] << 8) | buf[o + 12]) >> 4
            totalSamples = (buf[o + 13] & 0x0f) * 2 ** 32 + u32be(buf, o + 14)
            return totalSamples / sampleRate if totalSamples > 0 and sampleRate else None
        if h & 0x80:
            break
        pos += 4 + length
    return None


CONTAINERS = {'moov', 'trak', 'mdia', 'minf', 'stbl', 'moof', 'traf', 'mvex'}


def mp4Duration(buf):
    tracks = []
    truncated = False

    def walk(start, end, cur):
        nonlocal truncated
        pos = start
        while pos + 8 <= end:
            size = u32be(buf, pos)
            chunkType = ascii(buf, pos + 4, 4)
            hdr = 8
            if size == 1:
                size = u64be(buf, pos + 8)
                hdr = 16
            elif size == 0:
                size = end - pos

            if size >= hdr and pos + size > end:
                if chunkType == 'moov':
                    truncated = True
                size = end - pos
            elif size < hdr:
                size = min(max(size, hdr), end - pos)
            body = pos + hdr
            bodyEnd = pos + size
            if chunkType == 'trak':
                t = JSObject({'handler': None, 'timeScale': 0, 'duration': 0, 'hasMdhd': False})
                tracks.append(t)
                walk(body, bodyEnd, t)
            elif chunkType in CONTAINERS:
                walk(body, bodyEnd, cur)
            elif cur is not None and chunkType == 'hdlr':
                cur['handler'] = ascii(buf, body + 8, 4)
            elif cur is not None and chunkType == 'mdhd':
                v = buf[body] if body < len(buf) else None
                cur['hasMdhd'] = True
                if v == 1:
                    cur['timeScale'] = u32be(buf, body + 20)
                    cur['duration'] = u64be(buf, body + 24)
                else:
                    cur['timeScale'] = u32be(buf, body + 12)
                    cur['duration'] = u32be(buf, body + 16)
            pos += size

    walk(0, len(buf), None)
    if truncated:
        return None
    audio = next((t for t in tracks if t.get('handler') == 'soun' or t.get('handler') == 'audi'), None)
    if audio is not None and audio.get('timeScale') > 0 and audio.get('duration') > 0:
        return audio.get('duration') / audio.get('timeScale')
    return None


MPEG_BITRATES = JSObject({
    '1-1': [32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    '1-2': [32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    '1-3': [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    '2-1': [32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    '2-2': [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    '2-3': [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
})
MPEG_SAMPLE_RATES = JSObject({3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]})


def mpegHeader(buf, o):
    if o + 4 > len(buf) or buf[o] != 0xff or (buf[o + 1] & 0xe0) != 0xe0:
        return None
    versionBits = (buf[o + 1] >> 3) & 3
    layerBits = (buf[o + 1] >> 1) & 3
    bitrateIdx = buf[o + 2] >> 4
    srIdx = (buf[o + 2] >> 2) & 3
    padding = (buf[o + 2] >> 1) & 1
    if versionBits == 1 or layerBits == 0 or srIdx == 3:
        return None
    layer = 4 - layerBits
    verKey = 1 if versionBits == 3 else 2
    if bitrateIdx == 0 or bitrateIdx == 15:
        return None
    bitrate = MPEG_BITRATES[f'{verKey}-{layer}'][bitrateIdx - 1] * 1000
    sampleRate = MPEG_SAMPLE_RATES[versionBits][srIdx]
    if layer == 1:
        samplesPerFrame = 384
    elif layer == 2:
        samplesPerFrame = 1152
    elif versionBits == 3:
        samplesPerFrame = 1152
    else:
        samplesPerFrame = 576
    slot = 4 if layer == 1 else 1
    if layer == 1:
        frameSize = math.floor(((12 * bitrate) / sampleRate + padding) * 4)
    else:
        frameSize = math.floor((samplesPerFrame / 8) * bitrate / sampleRate + (slot if padding else 0))
    mono = (buf[o + 3] >> 6) == 3
    return JSObject({
        'versionBits': versionBits,
        'layer': layer,
        'bitrate': bitrate,
        'sampleRate': sampleRate,
        'samplesPerFrame': samplesPerFrame,
        'frameSize': frameSize,
        'padding': padding,
        'crc': (buf[o + 1] & 1) == 0,
        'mono': mono,
    })


def id3v2Size(buf):
    if len(buf) >= 10 and ascii(buf, 0, 3) == 'ID3':
        size = ((buf[6] & 0x7f) << 21) | ((buf[7] & 0x7f) << 14) | ((buf[8] & 0x7f) << 7) | (buf[9] & 0x7f)
        return 10 + size + (10 if buf[5] & 0x10 else 0)
    return 0


def mp3Duration(buf):
    pos = id3v2Size(buf)

    first = None
    while pos + 4 <= len(buf):
        h = mpegHeader(buf, pos)
        if h is not None and h.get('frameSize') > 0 and (
            pos + h.get('frameSize') == len(buf)
            or mpegHeader(buf, pos + h.get('frameSize')) is not None
            or pos + h.get('frameSize') > len(buf)
        ):
            first = JSObject({**h, 'pos': pos})
            break
        pos += 1
    if first is None:
        return None
    hasId3v1 = len(buf) >= 128 and ascii(buf, len(buf) - 128, 3) == 'TAG'

    if first.get('versionBits') == 3:
        sideInfo = 17 if first.get('mono') else 32
    else:
        sideInfo = 9 if first.get('mono') else 17
    tagOff = first.get('pos') + 4 + (2 if first.get('crc') else 0) + sideInfo
    tag = ascii(buf, tagOff, 4) if tagOff + 4 <= len(buf) else ''
    if tag == 'Xing' or tag == 'Info':
        flags = u32be(buf, tagOff + 4)
        o = tagOff + 8
        numFrames = None
        streamSize = None
        if flags & 1:
            numFrames = u32be(buf, o)
            o += 4
        if flags & 2:
            streamSize = u32be(buf, o)
            o += 4
        if flags & 4:
            o += 100
        if flags & 8:
            o += 4
        lameMusicLength = None
        if o + 4 <= len(buf) and ascii(buf, o, 4) == 'LAME':
            ver = ascii(buf, o + 4, 5)
            m = re.findall(r'\d+.\d+', ver)
            if m:
                parts = m[0].split('.')
                maj = _parseIntPrefix(parts[0])
                mn = _parseIntPrefix(parts[1]) if len(parts) > 1 else math.nan
                if maj >= 3 and mn >= 90:
                    lameMusicLength = u32be(buf, o + 9 + 0x14)

        if _jsTruthy(streamSize) and numFrames is not None:
            return (numFrames * first.get('samplesPerFrame')) / first.get('sampleRate')
        if lameMusicLength is not None and lameMusicLength > 0:
            return lameMusicLength / 1000

    bitrates = []
    p = first.get('pos')
    last = None
    i = 0
    while i < 4 and p + 4 <= len(buf):
        h = mpegHeader(buf, p)
        if h is None:
            break
        bitrates.append(h.get('bitrate'))
        last = h
        p += h.get('frameSize')
        i += 1
    if len(bitrates) == 4 and all(b == bitrates[0] for b in bitrates):
        mpegSize = len(buf) - first.get('pos') - (128 if hasId3v1 else 0)
        return (math.floor(mpegSize / last.get('frameSize') + 0.5) * first.get('samplesPerFrame')) / first.get('sampleRate')

    frames = 0
    p = first.get('pos')
    while p + 4 <= len(buf):
        h = mpegHeader(buf, p)
        if h is None:
            p += 1
            continue
        frames += 1
        p += h.get('frameSize')
    return (frames * first.get('samplesPerFrame')) / first.get('sampleRate') if frames else None


ADTS_SAMPLE_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]


def adtsDuration(buf):
    pos = id3v2Size(buf)
    frames = 0
    sampleRate = None
    while pos + 7 <= len(buf):
        if buf[pos] != 0xff or (buf[pos + 1] & 0xf6) != 0xf0:
            if frames == 0:
                pos += 1
                continue
            break
        sfi = (buf[pos + 2] >> 2) & 0x0f
        frameLength = ((buf[pos + 3] & 0x03) << 11) | (buf[pos + 4] << 3) | (buf[pos + 5] >> 5)
        if sfi > 12 or frameLength < 7:
            if frames == 0:
                pos += 1
                continue
            break
        if sampleRate is None:
            sampleRate = ADTS_SAMPLE_RATES[sfi]
        frames += 1
        pos += frameLength
    return (frames * 1024) / sampleRate if frames and sampleRate else None


def aiffDuration(buf):
    if ascii(buf, 0, 4) != 'FORM':
        return None
    kind = ascii(buf, 8, 4)
    if kind != 'AIFF' and kind != 'AIFC':
        return None
    pos = 12
    while pos + 8 <= len(buf):
        chunkId = ascii(buf, pos, 4)
        size = u32be(buf, pos + 4)
        body = pos + 8
        if chunkId == 'COMM':
            if size < 18 or body + size > len(buf):
                return None
            frames = u32be(buf, body + 2)

            exp = struct.unpack_from('>H', buf, body + 8)[0] & 0x7fff
            mantissa = struct.unpack_from('>Q', buf, body + 10)[0]
            sampleRate = float(mantissa) * _pow2(exp - 16383 - 63)
            return frames / sampleRate if sampleRate > 0 else None
        pos = body + size + (size & 1)
    return None


def ebmlVint(buf, pos, keepMarker):
    if pos >= len(buf):
        return None
    first = buf[pos]
    if first == 0:
        return None
    length = 9 - first.bit_length()
    if pos + length > len(buf):
        return None
    value = first if keepMarker else first & (0xff >> length)
    allOnes = (first & (0xff >> length)) == (0xff >> length)
    for i in range(1, length):
        value = value * 256 + buf[pos + i]
        if buf[pos + i] != 0xff:
            allOnes = False
    return JSObject({'value': value, 'length': length, 'unknown': (not keepMarker) and allOnes})


def matroskaDuration(buf):
    info = JSObject({'timecodeScale': None, 'duration': None})

    def readUInt(o, n):
        v = 0
        for i in range(n):
            v = v * 256 + buf[o + i]
        return v

    def walk(start, end, depth):
        pos = start
        while pos < end:
            eid = ebmlVint(buf, pos, True)
            if eid is None:
                return False
            size = ebmlVint(buf, pos + eid.get('length'), False)
            if size is None:
                return False
            body = pos + eid.get('length') + size.get('length')
            bodyEnd = end if size.get('unknown') else min(end, body + size.get('value'))
            if eid.get('value') == 0x18538067 and depth == 0:
                if walk(body, bodyEnd, 1):
                    return True
            elif eid.get('value') == 0x1549a966 and depth == 1:
                q = body
                while q < bodyEnd:
                    cid = ebmlVint(buf, q, True)
                    csz = ebmlVint(buf, q + cid.get('length'), False) if cid is not None else None
                    if csz is None:
                        break
                    cb = q + cid.get('length') + csz.get('length')
                    if cb + csz.get('value') > len(buf):
                        break
                    if cid.get('value') == 0x2ad7b1:
                        info['timecodeScale'] = readUInt(cb, csz.get('value'))
                    elif cid.get('value') == 0x4489:
                        if csz.get('value') == 4:
                            info['duration'] = struct.unpack_from('>f', buf, cb)[0]
                        elif csz.get('value') == 8:
                            info['duration'] = struct.unpack_from('>d', buf, cb)[0]
                        else:
                            info['duration'] = None
                    q = cb + csz.get('value')
                return True
            if size.get('unknown'):
                return False
            pos = body + size.get('value')
        return False

    walk(0, len(buf), 0)
    if not isinstance(info.get('duration'), float) or not math.isfinite(info.get('duration')):
        return None
    return (info.get('duration') * (info.get('timecodeScale') or 1000000)) / 1e9


AMR_NB_FRAME_BYTES = [13, 14, 16, 18, 20, 21, 27, 32, 6, 0, 0, 0, 0, 0, 0, 1]
AMR_WB_FRAME_BYTES = [18, 24, 33, 37, 41, 47, 51, 59, 61, 6, 0, 0, 0, 0, 1, 1]


def amrDuration(buf):
    wb = ascii(buf, 0, 9) == '#!AMR-WB\n'
    if not wb and ascii(buf, 0, 6) != '#!AMR\n':
        return None
    table = AMR_WB_FRAME_BYTES if wb else AMR_NB_FRAME_BYTES
    pos = 9 if wb else 6
    frames = 0
    while pos < len(buf):
        size = table[(buf[pos] >> 3) & 0x0f]
        if not size:
            break
        if pos + size > len(buf):
            break
        frames += 1
        pos += size
    return frames * 0.02 if frames else None


def durationFromBuffer(buf):
    if isinstance(buf, str):
        buf = buf.encode('utf-8')
    buf = bytes(buf)
    if len(buf) < 12:
        return None
    try:
        magic = ascii(buf, 0, 4)
        if magic == 'OggS':
            return oggDuration(buf)
        if magic == 'RIFF':
            return wavDuration(buf)
        if magic == 'fLaC':
            return flacDuration(buf)
        if ascii(buf, 4, 4) == 'ftyp':
            return mp4Duration(buf)
        if magic == 'FORM':
            return aiffDuration(buf)
        if buf[0] == 0x1a and buf[1] == 0x45 and buf[2] == 0xdf and buf[3] == 0xa3:
            return matroskaDuration(buf)
        if magic == '#!AM':
            return amrDuration(buf)
        if buf[0] == 0xff and (buf[1] & 0xf6) == 0xf0:
            return adtsDuration(buf)
        if magic.startswith('ID3') or (buf[0] == 0xff and (buf[1] & 0xe0) == 0xe0):
            return mp3Duration(buf)
    except Exception:
        return None
    return None


async def audioDuration(input):
    if isinstance(input, (bytes, bytearray, memoryview)):
        return durationFromBuffer(input)
    if isinstance(input, str):
        size = os.stat(input).st_size
        if size > 64 * 1024 * 1024:
            with open(input, 'rb') as fh:
                head = fh.read(min(size, 4 * 1024 * 1024))
            return durationFromBuffer(head)
        with open(input, 'rb') as fh:
            return durationFromBuffer(fh.read())
    chunks = []
    async for chunk in input:
        chunks.append(chunk)
    return durationFromBuffer(b''.join(chunks))
