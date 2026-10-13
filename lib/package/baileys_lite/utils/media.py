import asyncio
import base64
import http.client
import json
from baileys_lite.foundation.node_runtime import jsonDefault
import math
import os
import re
import socket
import struct
import subprocess
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request

from baileys_lite.binary.wa_binary import getBinaryNodeChild, getBinaryNodeChildBuffer, jidNormalizedUser
from baileys_lite.foundation.audio_duration import audioDuration
from baileys_lite.foundation.audio_samples import decodeChannel0, waveformFromSamples
from baileys_lite.foundation.boom import Boom
from baileys_lite.foundation.link_preview import getLinkPreview
from baileys_lite.foundation.long import stringToNumber
from baileys_lite.foundation.node_crypto import createCipheriv, createDecipheriv, createHash, createHmac, randomBytes
from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.foundation.wa_crypto import hkdf
from baileys_lite.utils.wa_protocol_core import aesDecryptGCM, aesEncryptGCM, generateMessageIDV2
from baileys_lite.vendor.jpeg_js.decoder import decode as jpegDecode
from baileys_lite.vendor.jpeg_js.encoder import encode as jpegEncode
from baileys_lite.vendor.pngjs.png_sync import read as pngRead
from baileys_lite.WAProto import proto


DEFAULT_ORIGIN = 'https://web.whatsapp.com'
DEF_MEDIA_HOST = 'mmg.whatsapp.net'
AES_CHUNK_SIZE = 16
THUMBNAIL_WIDTH_PX = 192
READ_CHUNK_SIZE = 65536

MEDIA_HKDF_KEY_MAPPING = JSObject({
    'audio': 'Audio',
    'document': 'Document',
    'gif': 'Video',
    'image': 'Image',
    'ppic': '',
    'product': 'Image',
    'ptt': 'Audio',
    'sticker-pack': 'Sticker Pack',
    'thumbnail-sticker-pack': 'Sticker Pack Thumbnail',
    'sticker': 'Image',
    'video': 'Video',
    'thumbnail-document': 'Document Thumbnail',
    'thumbnail-image': 'Image Thumbnail',
    'thumbnail-video': 'Video Thumbnail',
    'thumbnail-link': 'Link Thumbnail',
    'md-msg-hist': 'History',
    'md-app-state': 'App State',
    'product-catalog-image': 'Product Catalog Image',
    'payment-bg-image': 'Payment Background',
    'ptv': 'Video',
    'biz-cover-photo': 'Image',
    'location': 'Location',
    'contact': 'Contact',
    'voip-token': 'Voip Token'
})

MEDIA_PATH_MAP = JSObject({
    'image': '/mms/image',
    'video': '/mms/video',
    'document': '/mms/document',
    'audio': '/mms/audio',
    'sticker': '/mms/image',
    'sticker-pack': '/mms/sticker-pack',
    'thumbnail-sticker-pack': '/mms/thumbnail-sticker-pack',
    'thumbnail-link': '/mms/image',
    'thumbnail-image': '/mms/image',
    'thumbnail-video': '/mms/video',
    'thumbnail-document': '/mms/document',
    'product-catalog-image': '/product/image',
    'md-app-state': '',
    'md-msg-hist': '/mms/md-app-state',
    'biz-cover-photo': '/pps/biz-cover-photo'
})

MEDIA_KEYS = list(dict.keys(MEDIA_PATH_MAP))

NEWSLETTER_MEDIA_PATH_MAP = JSObject({
    'image': '/newsletter/newsletter-image',
    'video': '/newsletter/newsletter-video',
    'document': '/newsletter/newsletter-document',
    'audio': '/newsletter/newsletter-audio',
    'sticker': '/newsletter/newsletter-image',
    'thumbnail-link': '/newsletter/newsletter-thumbnail-link'
})

MEDIA_RETRY_STATUS_MAP = JSObject({
    proto.MediaRetryNotification.ResultType.SUCCESS: 200,
    proto.MediaRetryNotification.ResultType.DECRYPTION_ERROR: 412,
    proto.MediaRetryNotification.ResultType.NOT_FOUND: 404,
    proto.MediaRetryNotification.ResultType.GENERAL_ERROR: 418
})


def jsStr(value):
    if value is None:
        return 'undefined'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def truthyNumber(value):
    return value is not None and value != 0 and value == value


def jsFalsy(value):
    if value is None or value is False or (isinstance(value, str) and value == ''):
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0 or value != value
    return False


def jsonStringify(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False, default=jsonDefault)


def parseJsonObject(text):
    return json.loads(text, object_pairs_hook=JSObject)


def messageOf(err):
    message = getattr(err, 'message', None)
    if message is None:
        return str(err)
    return str(message)


def stackOf(err):
    if not isinstance(err, BaseException):
        return None
    return ''.join(traceback.format_exception(type(err), err, err.__traceback__))


async def maybeAwait(value):
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value


def headersToDict(headers):
    if headers is None:
        return {}
    if isinstance(headers, list):
        return {pair[0]: pair[1] for pair in headers}
    return dict(headers)


def decodeBase64(text):
    out = []
    for ch in text:
        if ch == '=':
            break
        if ('A' <= ch <= 'Z') or ('a' <= ch <= 'z') or ('0' <= ch <= '9') or ch == '+' or ch == '/':
            out.append(ch)
        elif ch == '-':
            out.append('+')
        elif ch == '_':
            out.append('/')
    cleaned = ''.join(out)
    if len(cleaned) % 4 == 1:
        cleaned = cleaned[:-1]
    cleaned += '=' * (-len(cleaned) % 4)
    return base64.b64decode(cleaned)


def jsParseFloat(text):
    match = re.match(r'\s*([+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))', text)
    if match is None:
        return None
    token = match.group(1)
    if token.endswith('Infinity'):
        return float('-inf') if token.startswith('-') else float('inf')
    return float(token)


def toNumber(value):
    if value is None:
        return float('nan')
    if isinstance(value, str):
        return stringToNumber(value)
    return value


def toSmallestChunkSize(num):
    return math.floor(num / AES_CHUNK_SIZE) * AES_CHUNK_SIZE


def toBytesChunk(chunk):
    if isinstance(chunk, str):
        return chunk.encode('utf-8')
    return bytes(chunk)


class ReadableBase:
    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        if False:
            yield None

    def destroy(self, err=None):
        return None


class MemoryReadable(ReadableBase):
    def __init__(self, data):
        self.data = bytes(data)

    async def iterate(self):
        if len(self.data) > 0:
            yield self.data


class FileReadable(ReadableBase):
    def __init__(self, path):
        self.path = path

    async def iterate(self):
        with open(self.path, 'rb') as handle:
            while True:
                chunk = handle.read(READ_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk


class HttpReadable(ReadableBase):
    def __init__(self, response):
        self.response = response

    async def iterate(self):
        while True:
            chunk = self.response.read(READ_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

    def destroy(self, err=None):
        self.response.close()


class GeneratorReadable(ReadableBase):
    def __init__(self, generator):
        self.generator = generator

    async def iterate(self):
        async for chunk in self.generator:
            yield chunk

    def destroy(self, err=None):
        try:
            asyncio.ensure_future(self.generator.aclose())
        except RuntimeError:
            pass


async def iterChunks(source):
    if isinstance(source, (bytes, bytearray)):
        yield bytes(source)
    elif hasattr(source, '__aiter__'):
        async for chunk in source:
            yield toBytesChunk(chunk)
    else:
        for chunk in source:
            yield toBytesChunk(chunk)


def destroyStream(stream):
    destroy = getattr(stream, 'destroy', None)
    if callable(destroy):
        destroy()


def toReadable(buffer):
    return MemoryReadable(buffer)


async def toBuffer(stream):
    chunks = []
    async for chunk in iterChunks(stream):
        chunks.append(chunk)
    destroyStream(stream)
    return b''.join(chunks)


async def getImageProcessingLibrary():
    return JSObject({'pngjs': pngRead, 'jpegJs': jpegDecode})


def hkdfInfoKey(type):
    return f'WhatsApp {jsStr(MEDIA_HKDF_KEY_MAPPING[type])} Keys'


def decodeRaster(data):
    raw = bytes(data)
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        image = pngRead(raw)
        return int(image.get('width')), int(image.get('height')), bytes(image.get('data'))
    if raw[:2] == b'\xff\xd8':
        image = jpegDecode(raw)
        return int(image.get('width')), int(image.get('height')), bytes(image.get('data'))
    raise JSError('No image processing library available: only PNG and JPEG images can be processed without a native library')


def resizeRgba(data, srcW, srcH, dstW, dstH):
    out = bytearray(dstW * dstH * 4)
    xScale = srcW / dstW
    yScale = srcH / dstH
    for y in range(dstH):
        fy = (y + 0.5) * yScale - 0.5
        y0 = max(0, min(int(math.floor(fy)), srcH - 1))
        y1 = min(y0 + 1, srcH - 1)
        wy = min(max(fy - y0, 0.0), 1.0)
        row0 = y0 * srcW
        row1 = y1 * srcW
        for x in range(dstW):
            fx = (x + 0.5) * xScale - 0.5
            x0 = max(0, min(int(math.floor(fx)), srcW - 1))
            x1 = min(x0 + 1, srcW - 1)
            wx = min(max(fx - x0, 0.0), 1.0)
            p00 = (row0 + x0) * 4
            p10 = (row0 + x1) * 4
            p01 = (row1 + x0) * 4
            p11 = (row1 + x1) * 4
            base = (y * dstW + x) * 4
            for c in range(4):
                top = data[p00 + c] * (1.0 - wx) + data[p10 + c] * wx
                bottom = data[p01 + c] * (1.0 - wx) + data[p11 + c] * wx
                value = int(math.floor(top * (1.0 - wy) + bottom * wy + 0.5))
                out[base + c] = 255 if value > 255 else value
    return bytes(out)


def cropRgba(data, srcW, left, top, width, height):
    out = bytearray(width * height * 4)
    rowBytes = width * 4
    for y in range(height):
        start = ((top + y) * srcW + left) * 4
        out[y * rowBytes:(y + 1) * rowBytes] = data[start:start + rowBytes]
    return bytes(out)


def encodeJpeg(data, width, height, quality):
    result = jpegEncode(JSObject({'data': data, 'width': width, 'height': height}), quality)
    return bytes(result.get('data'))


def resizeToWidth(data, srcW, srcH, width):
    dstH = max(1, int(math.floor(srcH * width / srcW + 0.5)))
    return resizeRgba(data, srcW, srcH, width, dstH), width, dstH


async def getHttpStream(url, options=None):
    if options is None:
        options = JSObject({})
    headers = headersToDict(options.get('headers'))
    request = urllib.request.Request(str(url), headers=headers, method='GET')
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as error:
        raise Boom(f'Failed to fetch stream from {url}', JSObject({'statusCode': error.code, 'data': JSObject({'url': str(url)})}))
    return HttpReadable(response)


async def getStream(item, opts=None):
    if isinstance(item, (bytes, bytearray)):
        return JSObject({'stream': toReadable(item), 'type': 'buffer'})
    if 'stream' in item:
        return JSObject({'stream': item.get('stream'), 'type': 'readable'})
    urlStr = str(item.get('url'))
    if urlStr.startswith('data:'):
        buffer = decodeBase64(urlStr.split(',')[1])
        return JSObject({'stream': toReadable(buffer), 'type': 'buffer'})
    if urlStr.startswith('http://') or urlStr.startswith('https://'):
        return JSObject({'stream': await getHttpStream(item.get('url'), opts), 'type': 'remote'})
    return JSObject({'stream': FileReadable(item.get('url')), 'type': 'file'})


async def getRawMediaUploadData(media, mediaType, logger):
    stream = (await getStream(media))['stream']
    if logger is not None:
        logger.debug('got stream for raw upload')
    hasher = createHash('sha256')
    filePath = os.path.join(tempfile.gettempdir(), mediaType + generateMessageIDV2(None))
    handle = open(filePath, 'wb')
    fileLength = 0
    try:
        async for data in iterChunks(stream):
            fileLength += len(data)
            hasher.update(data)
            handle.write(data)
        handle.close()
        destroyStream(stream)
        fileSha256 = hasher.digest()
        if logger is not None:
            logger.debug('hashed data for raw upload')
        return JSObject({'filePath': filePath, 'fileSha256': fileSha256, 'fileLength': fileLength})
    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        destroyStream(stream)
        try:
            os.unlink(filePath)
        except Exception:
            pass
        raise


async def getMediaKeys(buffer, mediaType):
    if jsFalsy(buffer):
        raise Boom('Cannot derive from empty media key')
    if isinstance(buffer, str):
        buffer = decodeBase64(buffer.replace('data:;base64,', '', 1))
    expandedMediaKey = hkdf(buffer, 112, JSObject({'info': hkdfInfoKey(mediaType)}))
    return JSObject({
        'iv': expandedMediaKey[0:16],
        'cipherKey': expandedMediaKey[16:48],
        'macKey': expandedMediaKey[48:80]
    })


def runProcessSync(args, inputData=None, timeout=None):
    if inputData is None:
        return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return subprocess.run(args, input=inputData, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


async def extractVideoThumb(path, time, size):
    args = [
        'ffmpeg',
        '-loglevel',
        'error',
        '-ss',
        jsStr(time),
        '-i',
        path,
        '-an',
        '-sn',
        '-dn',
        '-map_metadata',
        '-1',
        '-vf',
        f"scale={jsStr(size.get('width'))}:-1",
        '-frames:v',
        '1',
        '-c:v',
        'mjpeg',
        '-f',
        'image2pipe',
        'pipe:1'
    ]
    try:
        completed = await asyncio.to_thread(runProcessSync, args)
    except OSError as error:
        raise JSError(str(error))
    code = completed.returncode
    if code != 0:
        raise Boom(f'FFmpeg failed (code {code}):\n' + completed.stderr.decode('utf-8', errors='replace'))
    return completed.stdout


async def extractImageThumb(bufferOrFilePath, width=32):
    if width is None:
        width = 32
    source = bufferOrFilePath
    if isinstance(source, ReadableBase):
        source = await toBuffer(source)
    if isinstance(source, str):
        with open(source, 'rb') as handle:
            source = handle.read()
    srcW, srcH, rgba = decodeRaster(source)
    resized, thumbW, thumbH = resizeToWidth(rgba, srcW, srcH, width)
    buffer = encodeJpeg(resized, thumbW, thumbH, 50)
    return JSObject({'buffer': buffer, 'original': JSObject({'width': srcW, 'height': srcH})})


def encodeBase64EncodedStringForUpload(b64):
    replaced = b64.replace('+', '-').replace('/', '_')
    return urllib.parse.quote(re.sub(r'=+$', '', replaced), safe="-_.!~*'()")


async def generateProfilePicture(mediaUpload, dimensions):
    dims = dimensions if dimensions is not None else JSObject({})
    w = dims.get('width') if dims.get('width') is not None else 720
    h = dims.get('height') if dims.get('height') is not None else 720
    if isinstance(mediaUpload, (bytes, bytearray)):
        buffer = bytes(mediaUpload)
    else:
        stream = (await getStream(mediaUpload))['stream']
        buffer = await toBuffer(stream)
    srcW, srcH, rgba = decodeRaster(buffer)
    scale = max(w / srcW, h / srcH)
    scaledW = max(w, int(math.floor(srcW * scale + 0.5)))
    scaledH = max(h, int(math.floor(srcH * scale + 0.5)))
    resized = resizeRgba(rgba, srcW, srcH, scaledW, scaledH)
    cropped = cropRgba(resized, scaledW, (scaledW - w) // 2, (scaledH - h) // 2, w, h)
    return JSObject({'img': encodeJpeg(cropped, w, h, 80)})


def mediaMessageSHA256B64(message):
    values = list(dict.values(message))
    media = values[0] if len(values) > 0 else None
    sha = media.get('fileSha256') if media is not None else None
    if sha is None or sha == '':
        return sha
    return base64.b64encode(bytes(sha)).decode()


async def ffprobeDuration(inputData):
    usesStdin = isinstance(inputData, (bytes, bytearray))
    args = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', 'pipe:0' if usesStdin else inputData]
    try:
        completed = await asyncio.to_thread(runProcessSync, args, bytes(inputData) if usesStdin else None)
        if completed.returncode != 0:
            return None
        seconds = jsParseFloat(completed.stdout.decode('utf-8', errors='replace').strip())
        if seconds is None or math.isnan(seconds) or math.isinf(seconds):
            return None
        return seconds
    except Exception:
        return None


def runFfmpegDecodeSync(audioData):
    try:
        completed = subprocess.run(
            ['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-af', 'pan=mono|c0=c0', '-ar', '16000', '-f', 'f32le', 'pipe:1'],
            input=audioData,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = completed.stdout
    if completed.returncode != 0 or len(raw) < 4:
        return None
    count = len(raw) // 4
    return list(struct.unpack('<%df' % count, raw[:count * 4]))


async def decodeChannel0WithFfmpeg(audioData):
    return await asyncio.to_thread(runFfmpegDecodeSync, audioData)


async def getAudioDuration(buffer):
    if not isinstance(buffer, (bytes, bytearray, str)):
        buffer = await toBuffer(buffer)
    native = await audioDuration(buffer)
    if isinstance(native, (int, float)) and not isinstance(native, bool) and math.isfinite(native):
        return native
    return await ffprobeDuration(buffer)


async def getAudioWaveform(buffer, logger):
    try:
        if isinstance(buffer, (bytes, bytearray)):
            audioData = bytes(buffer)
        elif isinstance(buffer, str):
            with open(buffer, 'rb') as handle:
                audioData = handle.read()
        else:
            audioData = await toBuffer(buffer)
        rawData = decodeChannel0(audioData)
        if rawData is None:
            rawData = await decodeChannel0WithFfmpeg(audioData)
        if rawData is None or len(rawData) == 0:
            if logger is not None:
                logger.debug('Failed to generate waveform: no decoder for this audio (built-in handles WAV/FLAC; other formats need ffmpeg)')
            return None
        return waveformFromSamples(rawData)
    except Exception as e:
        if logger is not None:
            logger.debug('Failed to generate waveform: ' + str(e))
        return None


async def generateThumbnail(file, mediaType, options):
    thumbnail = None
    originalImageDimensions = None
    if mediaType == 'image':
        result = await extractImageThumb(file)
        thumbnail = base64.b64encode(bytes(result.get('buffer'))).decode()
        original = result.get('original')
        if original.get('width') and original.get('height'):
            originalImageDimensions = JSObject({'width': original.get('width'), 'height': original.get('height')})
    elif mediaType == 'video':
        try:
            buff = await extractVideoThumb(file, '00:00:00', JSObject({'width': 32, 'height': 32}))
            thumbnail = base64.b64encode(bytes(buff)).decode()
        except Exception as err:
            logger = options.get('logger') if options is not None else None
            if logger is not None:
                logger.debug('could not generate video thumb: ' + messageOf(err))
    return JSObject({'thumbnail': thumbnail, 'originalImageDimensions': originalImageDimensions})


def getUrlFromDirectPath(directPath, host=None):
    if host is None:
        host = DEF_MEDIA_HOST
    return f'https://{host}{directPath}'


def extractHost(url):
    if not url:
        return None
    parts = urllib.parse.urlsplit(str(url))
    if not parts.scheme or not parts.netloc:
        return None
    hostname = parts.hostname or ''
    try:
        port = parts.port
    except ValueError:
        port = None
    defaultPort = 443 if parts.scheme == 'https' else (80 if parts.scheme == 'http' else None)
    if port is not None and port != defaultPort:
        return f'{hostname}:{port}'
    return hostname


async def downloadContentFromMessage(message, type, opts=None):
    if opts is None:
        opts = JSObject({})
    mediaKey = message.get('mediaKey')
    directPath = message.get('directPath')
    url = message.get('url')
    fallbackHost = opts.get('host') if opts.get('host') is not None else extractHost(url)
    downloadUrl = getUrlFromDirectPath(directPath, fallbackHost) if directPath else url
    if not downloadUrl:
        raise Boom('No valid media URL or directPath present in message', JSObject({'statusCode': 400}))
    keys = await getMediaKeys(mediaKey, type)
    return await downloadEncryptedContent(downloadUrl, keys, opts)


def pushBytesFactory(state, startByte, endByte):
    def pushBytes(chunk, out):
        if truthyNumber(startByte) or truthyNumber(endByte):
            if startByte is not None and state.get('bytesFetched') >= startByte:
                start = 0
            elif startByte is not None:
                start = max(startByte - state.get('bytesFetched'), 0)
            else:
                start = 0
            if endByte is not None and state.get('bytesFetched') + len(chunk) < endByte:
                end = None
            elif endByte is None:
                end = 0
            else:
                end = max(endByte - state.get('bytesFetched'), 0)
            out.append(chunk[start:] if end is None else chunk[start:end])
            state['bytesFetched'] += len(chunk)
        else:
            out.append(chunk)
    return pushBytes


async def downloadEncryptedContent(downloadUrl, keys, options=None):
    if options is None:
        options = JSObject({})
    cipherKey = keys.get('cipherKey')
    iv = keys.get('iv')
    startByte = options.get('startByte')
    endByte = options.get('endByte')
    opts = options.get('options')
    bytesFetched = 0
    startChunk = 0
    firstBlockIsIV = False
    if truthyNumber(startByte):
        chunk = toSmallestChunkSize(startByte or 0)
        if chunk:
            startChunk = chunk - AES_CHUNK_SIZE
            bytesFetched = chunk
            firstBlockIsIV = True
    endChunk = (toSmallestChunkSize(endByte or 0) + AES_CHUNK_SIZE) if truthyNumber(endByte) else None
    headers = headersToDict(opts.get('headers')) if opts is not None and opts.get('headers') else {}
    headers['Origin'] = DEFAULT_ORIGIN
    if startChunk or endChunk:
        headers['Range'] = f'bytes={startChunk}-' + (str(endChunk) if endChunk else '')

    fetched = await getHttpStream(downloadUrl, JSObject({**dict(options), 'headers': headers}))
    state = {'bytesFetched': bytesFetched, 'remaining': b'', 'aes': None}
    pushBytes = pushBytesFactory(state, startByte, endByte)

    async def run():
        try:
            async for chunk in iterChunks(fetched):
                data = state.get('remaining') + chunk if len(state.get('remaining')) > 0 else chunk
                decryptLength = toSmallestChunkSize(len(data))
                state['remaining'] = data[decryptLength:]
                data = data[:decryptLength]
                if state.get('aes') is None:
                    ivValue = iv
                    if firstBlockIsIV:
                        ivValue = data[:AES_CHUNK_SIZE]
                        data = data[AES_CHUNK_SIZE:]
                    state['aes'] = createDecipheriv('aes-256-cbc', cipherKey, ivValue)
                    if truthyNumber(endByte):
                        state.get('aes').setAutoPadding(False)
                outs = []
                pushBytes(state.get('aes').update(data), outs)
                for out in outs:
                    if len(out) > 0:
                        yield out
            if state.get('aes') is None:
                raise JSError("Cannot read properties of undefined (reading 'final')")
            outs = []
            pushBytes(state.get('aes').final(), outs)
            for out in outs:
                if len(out) > 0:
                    yield out
        finally:
            destroyStream(fetched)

    return GeneratorReadable(run())


def extensionForMediaMessage(message):
    keys = list(dict.keys(message))
    type = keys[0] if len(keys) > 0 else None
    if type in ('locationMessage', 'liveLocationMessage', 'productMessage'):
        return '.jpeg'
    mimetype = message[type].get('mimetype')
    parts = mimetype.split(';')[0].split('/')
    return parts[1] if len(parts) > 1 else None


def postFileSync(url, filePath, headers, timeoutMs):
    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme == 'https'
    port = parts.port or (443 if secure else 80)
    path = (parts.path or '/') + (('?' + parts.query) if parts.query else '')
    timeout = timeoutMs / 1000.0 if truthyNumber(timeoutMs) else None
    fileSize = os.stat(filePath).st_size
    requestHeaders = dict(headers)
    requestHeaders['Content-Length'] = fileSize
    if secure:
        connection = http.client.HTTPSConnection(parts.hostname, port, timeout=timeout)
    else:
        connection = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
    try:
        with open(filePath, 'rb') as body:
            connection.request('POST', path, body=body, headers=requestHeaders)
            response = connection.getresponse()
            status = response.status
            location = response.getheader('Location')
            raw = response.read()
    except socket.timeout:
        raise JSError('Upload timeout')
    finally:
        connection.close()
    return status, location, raw


async def uploadWithNodeHttp(params, redirectCount=0):
    if redirectCount > 5:
        raise JSError('Too many redirects')
    url = params.get('url')
    status, location, raw = await asyncio.to_thread(postFileSync, url, params.get('filePath'), params.get('headers'), params.get('timeoutMs'))
    if status is not None and 300 <= status < 400 and location:
        newUrl = urllib.parse.urljoin(url, location)
        return await uploadWithNodeHttp(JSObject({**dict(params), 'url': newUrl}), redirectCount + 1)
    try:
        return parseJsonObject(raw.decode('utf-8'))
    except Exception:
        return None


async def uploadMedia(params, logger):
    if logger is not None:
        logger.debug('Using Node.js https module for upload (avoids undici buffering bug)')
    return await uploadWithNodeHttp(params)


def getWAUploadToServer(options, refreshMediaConn):
    customUploadHosts = options.get('customUploadHosts')
    fetchAgent = options.get('fetchAgent')
    logger = options.get('logger')
    uploadOptions = options.get('options')

    async def upload(filePath, params):
        mediaType = params.get('mediaType')
        fileEncSha256B64 = params.get('fileEncSha256B64')
        timeoutMs = params.get('timeoutMs')
        newsletter = params.get('newsletter')
        uploadInfo = await maybeAwait(refreshMediaConn(False))
        urls = None
        hosts = list(customUploadHosts) + list(uploadInfo.get('hosts'))
        fileEncSha256B64 = encodeBase64EncodedStringForUpload(fileEncSha256B64)
        customHeaders = headersToDict(uploadOptions.get('headers')) if uploadOptions is not None and uploadOptions.get('headers') else {}
        headers = JSObject({**customHeaders, 'Content-Type': 'application/octet-stream', 'Origin': DEFAULT_ORIGIN})

        for host in hosts:
            hostname = host.get('hostname')
            logger.debug(f'uploading to "{jsStr(hostname)}"')
            auth = urllib.parse.quote(jsStr(uploadInfo.get('auth')), safe="-_.!~*'()")
            mediaPathMap = NEWSLETTER_MEDIA_PATH_MAP if newsletter else MEDIA_PATH_MAP
            serverThumb = '&server_thumb_gen=1' if newsletter else ''
            url = f"https://{jsStr(hostname)}{jsStr(mediaPathMap[mediaType])}/{fileEncSha256B64}?auth={auth}&token={fileEncSha256B64}{serverThumb}"
            result = None
            try:
                result = await uploadMedia(JSObject({'url': url, 'filePath': filePath, 'headers': headers, 'timeoutMs': timeoutMs, 'agent': fetchAgent}), logger)
                if result is not None and (result.get('url') or result.get('direct_path')):
                    thumbnailInfo = result.get('thumbnail_info')
                    urls = JSObject({
                        'mediaUrl': result.get('url'),
                        'directPath': result.get('direct_path'),
                        'meta_hmac': result.get('meta_hmac'),
                        'fbid': result.get('fbid'),
                        'ts': result.get('ts'),
                        'thumbnailDirectPath': thumbnailInfo.get('thumbnail_direct_path') if thumbnailInfo is not None else None,
                        'thumbnailSha256': thumbnailInfo.get('thumbnail_sha256') if thumbnailInfo is not None else None
                    })
                    break
                else:
                    uploadInfo = await maybeAwait(refreshMediaConn(True))
                    raise JSError(f'upload failed, reason: {jsonStringify(result)}')
            except Exception as error:
                lastIndex = len(uploadInfo.get('hosts')) - 1
                lastHost = hosts[lastIndex] if 0 <= lastIndex < len(hosts) else None
                isLast = lastHost is not None and hostname == lastHost.get('hostname')
                logger.warn(JSObject({'trace': stackOf(error), 'uploadResult': result}), f'Error in uploading to {jsStr(hostname)} {"" if isLast else ", retrying..."}')
        if urls is None:
            raise Boom('Media upload failed on all hosts', JSObject({'statusCode': 500}))
        return urls
    return upload


def getMediaRetryKey(mediaKey):
    return hkdf(mediaKey, 32, JSObject({'info': 'WhatsApp Media Retry Notification'}))


def encryptMediaRetryRequest(key, mediaKey, meId):
    recp = JSObject({'stanzaId': key.get('id')})
    recpBuffer = proto.ServerErrorReceipt.encode(recp).finish()
    iv = randomBytes(12)
    retryKey = getMediaRetryKey(mediaKey)
    ciphertext = aesEncryptGCM(recpBuffer, retryKey, iv, key.get('id').encode('utf-8'))
    return JSObject({
        'tag': 'receipt',
        'attrs': JSObject({'id': key.get('id'), 'to': jidNormalizedUser(meId), 'type': 'server-error'}),
        'content': [
            JSObject({
                'tag': 'encrypt',
                'attrs': JSObject({}),
                'content': [
                    JSObject({'tag': 'enc_p', 'attrs': JSObject({}), 'content': ciphertext}),
                    JSObject({'tag': 'enc_iv', 'attrs': JSObject({}), 'content': iv})
                ]
            }),
            JSObject({
                'tag': 'rmr',
                'attrs': JSObject({
                    'jid': key.get('remoteJid'),
                    'from_me': 'true' if bool(key.get('fromMe')) else 'false',
                    'participant': key.get('participant') or None
                })
            })
        ]
    })


def decodeMediaRetryNode(node):
    rmrNode = getBinaryNodeChild(node, 'rmr')
    event = JSObject({
        'key': JSObject({
            'id': node.get('attrs').get('id'),
            'remoteJid': rmrNode.get('attrs').get('jid'),
            'fromMe': rmrNode.get('attrs').get('from_me') == 'true',
            'participant': rmrNode.get('attrs').get('participant')
        })
    })
    errorNode = getBinaryNodeChild(node, 'error')
    if errorNode is not None:
        errorCode = toNumber(errorNode.get('attrs').get('code'))
        event['error'] = Boom(f'Failed to re-upload media ({jsStr(errorCode)})', JSObject({'data': errorNode.get('attrs'), 'statusCode': getStatusCodeForMediaRetry(errorCode)}))
    else:
        encryptedInfoNode = getBinaryNodeChild(node, 'encrypt')
        ciphertext = getBinaryNodeChildBuffer(encryptedInfoNode, 'enc_p')
        iv = getBinaryNodeChildBuffer(encryptedInfoNode, 'enc_iv')
        if ciphertext is not None and iv is not None:
            event['media'] = JSObject({'ciphertext': ciphertext, 'iv': iv})
        else:
            event['error'] = Boom('Failed to re-upload media (missing ciphertext)', JSObject({'statusCode': 404}))
    return event


def decryptMediaRetryData(retryInfo, mediaKey, msgId):
    retryKey = getMediaRetryKey(mediaKey)
    plaintext = aesDecryptGCM(retryInfo.get('ciphertext'), retryKey, retryInfo.get('iv'), msgId.encode('utf-8'))
    return proto.MediaRetryNotification.decode(plaintext)


def getStatusCodeForMediaRetry(code):
    return MEDIA_RETRY_STATUS_MAP[code]


async def getCompressedJpegThumbnail(url, options):
    stream = await getHttpStream(url, options.get('fetchOpts'))
    return await extractImageThumb(stream, options.get('thumbnailWidth'))


async def encryptedStream(media, mediaType, options=None):
    if options is None:
        options = JSObject({})
    logger = options.get('logger')
    saveOriginalFileIfRequired = options.get('saveOriginalFileIfRequired')
    opts = options.get('opts')
    streamInfo = await getStream(media, opts)
    stream = streamInfo.get('stream')
    streamType = streamInfo.get('type')
    if logger is not None:
        logger.debug('fetched media stream')
    mediaKey = randomBytes(32)
    keys = await getMediaKeys(mediaKey, mediaType)
    cipherKey = keys.get('cipherKey')
    iv = keys.get('iv')
    macKey = keys.get('macKey')
    tmpDir = tempfile.gettempdir()
    encFilePath = os.path.join(tmpDir, mediaType + generateMessageIDV2(None) + '-enc')
    encHandle = open(encFilePath, 'wb')
    originalHandle = None
    originalFilePath = None
    if saveOriginalFileIfRequired:
        originalFilePath = os.path.join(tmpDir, mediaType + generateMessageIDV2(None) + '-original')
        originalHandle = open(originalFilePath, 'wb')
    fileLength = 0
    aes = createCipheriv('aes-256-cbc', cipherKey, iv)
    hmac = createHmac('sha256', macKey)
    hmac.update(iv)
    sha256Plain = createHash('sha256')
    sha256Enc = createHash('sha256')
    maxContentLength = opts.get('maxContentLength') if opts is not None else None
    try:
        async for data in iterChunks(stream):
            fileLength += len(data)
            if streamType == 'remote' and truthyNumber(maxContentLength) and fileLength + len(data) > maxContentLength:
                raise Boom(f'content length exceeded when encrypting "{streamType}"', JSObject({'data': JSObject({'media': media, 'type': streamType})}))
            if originalHandle is not None:
                originalHandle.write(data)
            sha256Plain.update(data)
            encrypted = aes.update(data)
            sha256Enc.update(encrypted)
            hmac.update(encrypted)
            encHandle.write(encrypted)
        finalChunk = aes.final()
        sha256Enc.update(finalChunk)
        hmac.update(finalChunk)
        encHandle.write(finalChunk)
        mac = hmac.digest()[:10]
        sha256Enc.update(mac)
        fileSha256 = sha256Plain.digest()
        fileEncSha256 = sha256Enc.digest()
        encHandle.write(mac)
        encHandle.close()
        if originalHandle is not None:
            originalHandle.close()
        destroyStream(stream)
        if logger is not None:
            logger.debug('encrypted data successfully')
        return JSObject({
            'mediaKey': mediaKey,
            'originalFilePath': originalFilePath,
            'encFilePath': encFilePath,
            'mac': mac,
            'fileEncSha256': fileEncSha256,
            'fileSha256': fileSha256,
            'fileLength': fileLength
        })
    except Exception:
        for handle in (encHandle, originalHandle):
            try:
                if handle is not None:
                    handle.close()
            except Exception:
                pass
        destroyStream(stream)
        try:
            os.unlink(encFilePath)
            if originalFilePath:
                os.unlink(originalFilePath)
        except Exception as err:
            if logger is not None:
                logger.error(JSObject({'err': err}), 'failed deleting tmp files')
        raise


async def getUrlInfo(text, opts=None):
    if opts is None:
        opts = JSObject({'thumbnailWidth': THUMBNAIL_WIDTH_PX, 'fetchOpts': JSObject({'timeout': 3000})})
    fetchOpts = opts.get('fetchOpts')
    try:
        retries = 0
        maxRetry = 5
        previewLink = text
        if not text.startswith('https://') and not text.startswith('http://'):
            previewLink = 'https://' + previewLink

        def handleRedirects(baseURL, forwardedURL):
            nonlocal retries
            baseHost = urllib.parse.urlsplit(baseURL).hostname
            forwardedHost = urllib.parse.urlsplit(forwardedURL).hostname
            if retries >= maxRetry:
                return False
            if forwardedHost == baseHost or forwardedHost == 'www.' + baseHost or 'www.' + forwardedHost == baseHost:
                retries += 1
                return True
            return False

        linkOptions = JSObject({
            **(dict(fetchOpts) if fetchOpts is not None else {}),
            'followRedirects': 'follow',
            'handleRedirects': handleRedirects,
            'headers': fetchOpts.get('headers') if fetchOpts is not None else None
        })
        info = await getLinkPreview(previewLink, linkOptions)

        if info is not None and 'title' in info and info.get('title'):
            images = info.get('images')
            image = images[0] if len(images) > 0 else None
            urlInfo = JSObject({
                'canonical-url': info.get('url'),
                'matched-text': text,
                'title': info.get('title'),
                'description': info.get('description'),
                'originalThumbnailUrl': image
            })
            if opts.get('uploadImage') is not None:
                import baileys_lite.utils.message_compose as messageCompose
                prepared = await maybeAwait(messageCompose.prepareWAMessageMedia(
                    JSObject({'image': JSObject({'url': image})}),
                    JSObject({'upload': opts.get('uploadImage'), 'mediaTypeOverride': 'thumbnail-link', 'options': fetchOpts})
                ))
                imageMessage = prepared.get('imageMessage')
                urlInfo['jpegThumbnail'] = bytes(imageMessage.get('jpegThumbnail')) if imageMessage is not None and imageMessage.get('jpegThumbnail') is not None else None
                urlInfo['highQualityThumbnail'] = imageMessage
            else:
                try:
                    if image:
                        urlInfo['jpegThumbnail'] = (await getCompressedJpegThumbnail(image, opts))['buffer']
                except Exception as error:
                    if opts.get('logger') is not None:
                        opts.get('logger').debug(JSObject({'err': stackOf(error), 'url': previewLink}), 'error in generating thumbnail')
            return urlInfo
        return None
    except Exception as error:
        if 'receive a valid' not in messageOf(error):
            raise
        return None
