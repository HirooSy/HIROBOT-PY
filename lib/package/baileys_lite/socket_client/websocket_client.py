import asyncio
import base64
import hashlib
import os
import ssl
import urllib.parse

from baileys_lite.foundation.node_runtime import EventEmitter, JSError, JSObject, clearTimeout, queueMicrotask, setTimeout

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
CLOSE_TIMEOUT_SECONDS = 5
CLOSE_WAIT_SECONDS = 5
MAX_HANDSHAKE_BYTES = 65536
MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_CONTROL_PAYLOAD = 125
MAX_REASON_BYTES = 123
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA
RESERVED_HANDSHAKE_HEADERS = {
    'host',
    'upgrade',
    'connection',
    'sec-websocket-key',
    'sec-websocket-version',
    'sec-websocket-protocol',
    'sec-websocket-extensions',
}


class WebSocketProtocolError(JSError):
    def __init__(self, message, closeCode=1002):
        super().__init__(message)
        self.closeCode = closeCode


def optionalCall(target, name, *args):
    if target is None:
        return None
    if isinstance(target, dict):
        fn = target.get(name)
    else:
        fn = getattr(target, name, None)
    if callable(fn):
        return fn(*args)
    return None


def toJSError(err):
    if isinstance(err, JSError):
        return err
    wrapped = JSError(str(err) or type(err).__name__)
    wrapped.cause = err
    return wrapped


def closeQuietly(socket):
    try:
        socket.close()
    except Exception:
        pass


def toUrlString(url):
    if isinstance(url, str):
        return url
    href = url.get('href') if isinstance(url, dict) else getattr(url, 'href', None)
    if href is not None:
        return str(href)
    return str(url)


def parseTarget(url):
    parts = urllib.parse.urlsplit(toUrlString(url))
    scheme = parts.scheme.lower()
    if scheme in ('ws', 'http'):
        secure = False
    elif scheme in ('wss', 'https'):
        secure = True
    else:
        raise JSError('Expected a ws:// or wss:// URL')
    host = parts.hostname
    if not host:
        raise JSError('Expected a ws:// or wss:// URL')
    defaultPort = 443 if secure else 80
    port = parts.port or defaultPort
    path = parts.path or '/'
    if parts.query:
        path = path + '?' + parts.query
    hostHeader = f'[{host}]' if ':' in host else host
    if port != defaultPort:
        hostHeader = f'{hostHeader}:{port}'
    return secure, host, port, path, hostHeader


def buildHandshakeRequest(hostHeader, path, key, headers, protocols):
    lines = [
        f'GET {path} HTTP/1.1',
        f'Host: {hostHeader}',
        'Upgrade: websocket',
        'Connection: Upgrade',
        f'Sec-WebSocket-Key: {key}',
        'Sec-WebSocket-Version: 13',
    ]
    if protocols:
        lines.append('Sec-WebSocket-Protocol: ' + ', '.join(protocols))
    for name in headers:
        value = headers[name]
        if value is None:
            continue
        text = str(value)
        if ':' in name or '\r' in name or '\n' in name or '\r' in text or '\n' in text:
            raise JSError(f'Invalid handshake header: {name}')
        if name.lower() in RESERVED_HANDSHAKE_HEADERS:
            continue
        lines.append(f'{name}: {text}')
    lines.append('')
    lines.append('')
    return '\r\n'.join(lines).encode('latin-1')


def parseHandshakeResponse(head):
    lines = head.decode('latin-1').split('\r\n')
    statusLine = lines[0].split(' ', 2)
    if len(statusLine) < 2 or not statusLine[0].startswith('HTTP/1.') or not statusLine[1].isdigit():
        raise JSError('Invalid opening handshake response')
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(':')
        if not sep:
            raise JSError('Invalid opening handshake response')
        headers[name.strip().lower()] = value.strip()
    return int(statusLine[1]), headers


def computeAccept(key):
    digest = hashlib.sha1((key + WS_GUID).encode('ascii')).digest()
    return base64.b64encode(digest).decode('ascii')


def isValidReceivedCloseCode(code):
    return 1000 <= code <= 1003 or 1007 <= code <= 1014 or 3000 <= code <= 4999


def isValidUserCloseCode(code):
    return code == 1000 or 3000 <= code <= 4999


def parseClosePayload(payload):
    if len(payload) == 0:
        return 1005, ''
    if len(payload) == 1:
        raise WebSocketProtocolError('Invalid close frame payload')
    code = int.from_bytes(payload[:2], 'big')
    if not isValidReceivedCloseCode(code):
        raise WebSocketProtocolError(f'Invalid close code: {code}')
    try:
        reason = payload[2:].decode('utf-8')
    except UnicodeDecodeError:
        raise WebSocketProtocolError('Invalid UTF-8 in close reason', 1007)
    return code, reason


def xorMask(data, mask):
    length = len(data)
    if length == 0:
        return b''
    key = (mask * (length // 4 + 1))[:length]
    return (int.from_bytes(data, 'big') ^ int.from_bytes(key, 'big')).to_bytes(length, 'big')


class WebSocket(EventEmitter):
    CONNECTING = 0
    OPEN = 1
    CLOSING = 2
    CLOSED = 3

    def __init__(self, url, options=None):
        super().__init__()
        self.url = url
        self.options = options if options is not None else {}
        self.readyState = WebSocket.CONNECTING
        self.binaryType = 'arraybuffer'
        self.protocol = ''
        self.extensions = ''
        self.reader = None
        self.writer = None
        self.closeTimer = None
        self.closeSent = False
        self.maxPayloadBytes = self.options.get('maxPayloadBytes') or MAX_PAYLOAD_BYTES
        self.task = asyncio.get_running_loop().create_task(self.run())

    def emitError(self, err):
        if self.listenerCount('error') > 0:
            self.emit('error', err)

    async def run(self):
        try:
            await self.openSocket()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.failConnect(toJSError(err))
            return
        self.readyState = WebSocket.OPEN
        self.emit('open')
        await self.readLoop()

    async def openSocket(self):
        secure, host, port, path, hostHeader = parseTarget(self.url)
        sslContext = None
        if secure:
            sslContext = self.options.get('sslContext') or ssl.create_default_context()
        self.reader, self.writer = await asyncio.open_connection(
            host,
            port,
            ssl=sslContext,
            server_hostname=host if secure else None,
        )
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        protocols = list(self.options.get('protocols') or [])
        headers = self.options.get('headers') or {}
        self.writer.write(buildHandshakeRequest(hostHeader, path, key, headers, protocols))
        await self.writer.drain()
        try:
            head = await self.reader.readuntil(b'\r\n\r\n')
        except asyncio.LimitOverrunError:
            raise JSError('Opening handshake response is too large')
        except asyncio.IncompleteReadError:
            raise JSError('Connection closed during opening handshake')
        status, responseHeaders = parseHandshakeResponse(head)
        if status != 101:
            err = JSError(f'Unexpected server response: {status}')
            err.statusCode = status
            raise err
        if responseHeaders.get('upgrade', '').lower() != 'websocket':
            raise JSError('Invalid Upgrade header in opening handshake')
        tokens = [token.strip().lower() for token in responseHeaders.get('connection', '').split(',')]
        if 'upgrade' not in tokens:
            raise JSError('Invalid Connection header in opening handshake')
        if responseHeaders.get('sec-websocket-accept') != computeAccept(key):
            raise JSError('Invalid Sec-WebSocket-Accept in opening handshake')
        if 'sec-websocket-extensions' in responseHeaders:
            raise JSError('Unsupported extensions in opening handshake')
        selected = responseHeaders.get('sec-websocket-protocol', '')
        if selected and selected not in protocols:
            raise JSError('Server selected an unsupported subprotocol')
        self.protocol = selected

    def failConnect(self, err):
        if self.readyState == WebSocket.CLOSED:
            return
        self.readyState = WebSocket.CLOSED
        self.teardown()
        self.emitError(err)
        self.emit('close', 1006, '', False)

    def failProtocol(self, err):
        if self.readyState == WebSocket.CLOSED:
            return
        self.emitError(err)
        if self.readyState == WebSocket.OPEN:
            self.readyState = WebSocket.CLOSING
            self.sendClose(err.closeCode, '')
        self.finish(err.closeCode, '', False)

    def finish(self, code, reason, wasClean):
        if self.readyState == WebSocket.CLOSED:
            return
        self.readyState = WebSocket.CLOSED
        self.teardown()
        self.emit('close', code, reason, wasClean)

    def teardown(self):
        if self.closeTimer is not None:
            clearTimeout(self.closeTimer)
            self.closeTimer = None
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass

    async def readFrame(self):
        head = await self.reader.readexactly(2)
        first = head[0]
        second = head[1]
        fin = bool(first & 0x80)
        if first & 0x70:
            raise WebSocketProtocolError('Reserved bits are set')
        opcode = first & 0x0F
        if second & 0x80:
            raise WebSocketProtocolError('Server frames must not be masked')
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(await self.reader.readexactly(2), 'big')
        elif length == 127:
            length = int.from_bytes(await self.reader.readexactly(8), 'big')
            if length >> 63:
                raise WebSocketProtocolError('Frame length is too large')
        if opcode >= 8 and (not fin or length > MAX_CONTROL_PAYLOAD):
            raise WebSocketProtocolError('Invalid control frame')
        if length > self.maxPayloadBytes:
            raise WebSocketProtocolError('Frame payload is too big', 1009)
        payload = await self.reader.readexactly(length) if length else b''
        return fin, opcode, payload

    async def readLoop(self):
        fragments = []
        fragmentOpcode = None
        fragmentBytes = 0
        try:
            while True:
                fin, opcode, payload = await self.readFrame()
                if opcode == OP_CLOSE:
                    code, reason = parseClosePayload(payload)
                    if self.readyState == WebSocket.OPEN:
                        self.readyState = WebSocket.CLOSING
                        self.sendClose(None if code == 1005 else code, '')
                    self.finish(code, reason, True)
                    return
                if opcode == OP_PING:
                    if self.readyState != WebSocket.CLOSED:
                        self.writeFrame(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    self.emit('pong', payload)
                    continue
                if opcode == OP_CONTINUATION:
                    if fragmentOpcode is None:
                        raise WebSocketProtocolError('Unexpected continuation frame')
                elif opcode == OP_TEXT or opcode == OP_BINARY:
                    if fragmentOpcode is not None:
                        raise WebSocketProtocolError('Expected a continuation frame')
                    fragmentOpcode = opcode
                    fragments = []
                    fragmentBytes = 0
                else:
                    raise WebSocketProtocolError(f'Unknown opcode: {opcode}')
                fragments.append(payload)
                fragmentBytes += len(payload)
                if fragmentBytes > self.maxPayloadBytes:
                    raise WebSocketProtocolError('Message is too big', 1009)
                if fin:
                    message = b''.join(fragments)
                    isText = fragmentOpcode == OP_TEXT
                    fragmentOpcode = None
                    fragments = []
                    if isText:
                        try:
                            data = message.decode('utf-8')
                        except UnicodeDecodeError:
                            raise WebSocketProtocolError('Invalid UTF-8 in text message', 1007)
                    else:
                        data = message
                    self.emit('message', data)
        except asyncio.CancelledError:
            raise
        except WebSocketProtocolError as err:
            self.failProtocol(err)
        except asyncio.IncompleteReadError:
            self.finish(1006, '', False)
        except Exception as err:
            if self.readyState != WebSocket.CLOSED:
                self.emitError(toJSError(err))
                self.finish(1006, '', False)

    def writeFrame(self, opcode, payload):
        if self.writer is None or self.writer.is_closing():
            return False
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + length.to_bytes(2, 'big')
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + length.to_bytes(8, 'big')
        mask = os.urandom(4)
        self.writer.write(header + mask + xorMask(payload, mask))
        return True

    def sendClose(self, code, reason):
        payload = b''
        if code is not None:
            payload = code.to_bytes(2, 'big') + reason.encode('utf-8')
        self.closeSent = True
        return self.writeFrame(OP_CLOSE, payload)

    def forceClose(self):
        self.closeTimer = None
        self.finish(1006, '', False)

    def send(self, data):
        if self.readyState != WebSocket.OPEN:
            return False
        if isinstance(data, str):
            return self.writeFrame(OP_TEXT, data.encode('utf-8'))
        if isinstance(data, (bytes, bytearray, memoryview)):
            return self.writeFrame(OP_BINARY, bytes(data))
        raise JSError('Unsupported WebSocket message type')

    def ping(self, data=b''):
        if self.readyState != WebSocket.OPEN:
            return False
        if len(data) > MAX_CONTROL_PAYLOAD:
            raise JSError('Ping payload is too big')
        return self.writeFrame(OP_PING, bytes(data))

    def close(self, code=None, reason=''):
        if code is not None and not isValidUserCloseCode(code):
            raise JSError('The close code must be 1000 or in the range 3000-4999')
        if len(reason.encode('utf-8')) > MAX_REASON_BYTES:
            raise JSError('The close reason must not exceed 123 bytes')
        if self.readyState == WebSocket.CLOSED or self.readyState == WebSocket.CLOSING:
            return
        if self.readyState == WebSocket.CONNECTING:
            if self.task is not None:
                self.task.cancel()
            self.finish(1006, '', False)
            return
        self.readyState = WebSocket.CLOSING
        self.sendClose(code, reason)
        self.closeTimer = setTimeout(self.forceClose, CLOSE_TIMEOUT_SECONDS * 1000)


class AbstractSocketClient(EventEmitter):
    def __init__(self, url, config=None):
        super().__init__()
        self.url = url
        self.config = config
        self.setMaxListeners(0)


class WebSocketClient(AbstractSocketClient):
    def __init__(self, url, config=None):
        super().__init__(url, config)
        self.socket = None

    def emitError(self, err):
        if self.listenerCount('error') > 0:
            self.emit('error', err)
        else:
            logger = (self.config or {}).get('logger')
            optionalCall(logger, 'debug', JSObject({'err': err}), 'ws error ignored (no listener)')

    @property
    def isOpen(self):
        return self.socket is not None and self.socket.readyState == WebSocket.OPEN

    @property
    def isClosed(self):
        return self.socket is None or self.socket.readyState == WebSocket.CLOSED

    @property
    def isClosing(self):
        return self.socket is None or self.socket.readyState == WebSocket.CLOSING

    @property
    def isConnecting(self):
        return self.socket is not None and self.socket.readyState == WebSocket.CONNECTING

    def connect(self):
        if self.socket is not None:
            return
        from baileys_lite.defaults import DEFAULT_ORIGIN

        config = self.config or {}
        options = config.get('options') or {}
        connectTimeoutMs = config.get('connectTimeoutMs')
        logger = config.get('logger')
        headers = JSObject({'Origin': DEFAULT_ORIGIN, **(options.get('headers') or {})})
        if config.get('agent'):
            optionalCall(logger, 'warn', '`agent` is not supported by the WebSocket client')
        wsOptions = JSObject({'headers': headers})
        if config.get('sslContext'):
            wsOptions['sslContext'] = config.get('sslContext')

        socket = WebSocket(self.url, wsOptions)
        self.socket = socket

        handshakeTimer = None

        def clearHandshake():
            nonlocal handshakeTimer
            if handshakeTimer is not None:
                clearTimeout(handshakeTimer)
                handshakeTimer = None

        def onHandshakeTimeout():
            nonlocal handshakeTimer
            handshakeTimer = None
            if socket.readyState == WebSocket.CONNECTING:
                socket.removeAllListeners()
                self.emitError(JSError('Opening handshake has timed out'))
                queueMicrotask(closeQuietly, socket)

        if connectTimeoutMs and connectTimeoutMs > 0:
            handshakeTimer = setTimeout(onHandshakeTimeout, connectTimeoutMs)

        def onOpen():
            clearHandshake()
            self.emit('open')

        def onClose(code, reason, wasClean=False):
            clearHandshake()
            self.emit('close', code, reason)

        def onError(err):
            clearHandshake()
            raw = err if err is not None else JSError('WebSocket error')
            if not getattr(raw, 'message', None):
                cause = getattr(raw, 'cause', None)
                detail = getattr(cause, 'message', None) or getattr(cause, 'code', None) or 'unknown'
                raw.message = f'WebSocket error: {detail}'
            self.emitError(raw)

        def onMessage(data):
            self.emit('message', data)

        socket.on('open', onOpen)
        socket.on('close', onClose)
        socket.on('error', onError)
        socket.on('message', onMessage)

    async def close(self):
        socket = self.socket
        if socket is None:
            return
        if socket.readyState == WebSocket.CLOSED:
            self.socket = None
            return
        if socket.readyState == WebSocket.CONNECTING:
            socket.removeAllListeners()
            self.socket = None
            queueMicrotask(closeQuietly, socket)
            return

        closeFuture = asyncio.get_running_loop().create_future()

        def onClose(*args):
            if not closeFuture.done():
                closeFuture.set_result(None)

        self.once('close', onClose)
        closeQuietly(socket)
        try:
            await asyncio.wait_for(closeFuture, CLOSE_WAIT_SECONDS)
        except asyncio.TimeoutError:
            pass
        self.off('close', onClose)
        self.socket = None

    def send(self, str_, cb=None):
        socket = self.socket
        if socket is None or socket.readyState != WebSocket.OPEN:
            if cb is not None:
                cb(JSError('WebSocket is not open'))
            return False
        try:
            if not socket.send(str_):
                raise JSError('WebSocket is not open')
            if cb is not None:
                queueMicrotask(cb)
        except Exception as err:
            if cb is not None:
                cb(err)
            return False
        return True
