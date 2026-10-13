import asyncio
import random
import socket

from baileys_lite.foundation.node_crypto import randomBytes
from baileys_lite.foundation.node_runtime import EventEmitter, JSError, JSObject, clearInterval, clearTimeout, setInterval, setTimeout
from baileys_lite.voip.relay.dtls.handshake import DtlsClient
from baileys_lite.voip.relay.datachannel.pre_negotiated import PreNegotiatedDataChannel
from baileys_lite.voip.shim.core import createNoopLogger
from baileys_lite.voip.types import PayloadType
from baileys_lite.voip.relay.stun import (
    buildAllocateForRelay, buildBindingRequest, buildRelayBindingSuccess, buildWhatsAppPing,
    createWasmStreamDescriptors, parseStunResponse,
)


CONFIG = JSObject({
    'TRUE_WEB_CLIENT_RELAY_PORT': 3480,
    'CONNECTION_TIMEOUT_MS': 20000,
    'ICE_BINDING_TIMEOUTS_MS': [250, 400, 600, 1000, 1500, 2000],
    'KEEPALIVE_INTERVAL_MS': 1000,
    'ICE_BINDING_ENABLED': False,
    'ICE_CONSENT_REFRESH_ENABLED': False,
    'ICE_CONSENT_REFRESH_MS': 400,
    'MAX_BUFFER_SIZE': 256 * 1024,
})

ConnectionState = JSObject({
    'None': 'None',
    'Connecting': 'Connecting',
    'Open': 'Open',
    'Closed': 'Closed',
    'Failed': 'Failed',
})


def errorMessage(err):
    if hasattr(err, 'message'):
        return err.message
    return str(err)


def byteLengthOf(data):
    if data is None:
        return 0
    return len(data)


def randomUfrag():
    chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
    out = ''
    for i in range(8):
        out += chars[int(random.random() * len(chars))]
    return out


class DgramSocket(EventEmitter):
    def __init__(self):
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.loop = None
        self.closed = False

    def bind(self, port=0, address=None, callback=None):
        if callable(address) and callback is None:
            callback = address
            address = None
        if self.closed:
            raise JSError('Not running', code='ERR_SOCKET_DGRAM_NOT_RUNNING')
        self.sock.bind(('0.0.0.0' if address is None else address, port))
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.sock.fileno(), self.onReadable)

        def fire():
            self.emit('listening')
            if callback is not None:
                result = callback()
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)

        self.loop.call_soon(fire)
        return self

    def onReadable(self):
        while not self.closed:
            try:
                data, addr = self.sock.recvfrom(65535)
            except BlockingIOError:
                return
            except OSError as err:
                self.emit('error', err)
                return
            self.emit('message', data, JSObject({
                'address': addr[0], 'family': 'IPv4', 'port': addr[1], 'size': len(data)}))

    def send(self, msg, port, address, callback=None):
        if self.closed:
            raise JSError('Not running', code='ERR_SOCKET_DGRAM_NOT_RUNNING')
        try:
            self.sock.sendto(bytes(msg), (address, port))
        except OSError as err:
            if callback is not None:
                callback(err)
            else:
                self.emit('error', err)

    def address(self):
        ip, port = self.sock.getsockname()
        return JSObject({'address': ip, 'family': 'IPv4', 'port': port})

    def close(self, callback=None):
        if self.closed:
            raise JSError('Not running', code='ERR_SOCKET_DGRAM_NOT_RUNNING')
        self.closed = True
        if self.loop is not None:
            self.loop.remove_reader(self.sock.fileno())
        self.sock.close()
        self.emit('close')
        if callback is not None:
            callback()
        return self


def createSocket(kind='udp4'):
    return DgramSocket()


class WaManualRelay(EventEmitter):
    def __init__(self, options=None):
        super().__init__()
        if options is None:
            options = JSObject()
        self.logger = options.get('logger') if options.get('logger') is not None else createNoopLogger()
        self.connections = {}
        self.relayMap = {}
        self.audioSsrc = 0
        self.videoSsrc = 0
        self.subscriptionSsrc = 0
        self.streamSsrcs = None
        self.rxStun = JSObject({'bindReq': 0, 'bindAnswered': 0, 'allocOk': 0, 'allocErr': 0, 'pong': 0, 'other': 0})
        self.configuring = False
        self.globalBuffer = []
        self.globalBufferedBytes = 0
        self.stats = JSObject({'sent': 0, 'received': 0, 'connected': 0})
        self.backgroundTasks = set()

    def setSsrc(self, ssrc):
        self.audioSsrc = ssrc
        self.logger.get('debug')('manual relay ssrc set', JSObject({'ssrc': '0x%08x' % ssrc}))

    def setStreamSsrcs(self, streamSsrcs):
        out = [v & 0xFFFFFFFF for v in streamSsrcs]
        used = set(v for v in out[:6] if v != 0)
        for i in range(6, 9):
            for attempt in range(64):
                candidate = int.from_bytes(randomBytes(4), 'little')
                if candidate == 0 or candidate in used:
                    continue
                while len(out) <= i:
                    out.append(0)
                out[i] = candidate
                used.add(candidate)
                break
        self.streamSsrcs = out

    def getRxStunStats(self):
        return JSObject(dict(self.rxStun))

    def setVideoSsrc(self, ssrc):
        self.videoSsrc = ssrc
        self.logger.get('debug')('manual relay video ssrc set', JSObject({'ssrc': '0x%08x' % ssrc}))

    def setSubscriptionSsrc(self, ssrc):
        self.subscriptionSsrc = ssrc
        self.logger.get('debug')('manual relay subscription ssrc set', JSObject({'ssrc': '0x%08x' % ssrc}))

    def makeConnectionId(self, ip, port, authTokenId):
        base = '[%s]:%s' % (ip, port) if ':' in ip else '%s:%s' % (ip, port)
        return '%s#%s' % (base, authTokenId) if authTokenId else base

    async def configureRelays(self, relays):
        self.logger.get('debug')('manual relay configuring relays', JSObject({'count': len(relays)}))
        self.configuring = True
        for relay in relays:
            port = relay.get('port') or CONFIG['TRUE_WEB_CLIENT_RELAY_PORT']
            connectionId = self.makeConnectionId(relay.get('ip'), port, relay.get('authTokenId'))
            self.relayMap[connectionId] = JSObject({
                'id': connectionId,
                'ip': relay.get('ip'),
                'port': port,
                'token': relay.get('token'),
                'authToken': relay.get('authToken'),
                'rawAuthToken': relay.get('rawAuthToken'),
                'rawToken': relay.get('rawToken'),
                'key': relay.get('key'),
                'relayId': relay.get('relayId'),
                'name': relay.get('name') or 'unknown',
                'authTokenId': relay.get('authTokenId'),
                'isFna': relay.get('isFna'),
            })
        connectionPromises = []
        for relayInfo in list(self.relayMap.values()):
            if relayInfo.get('id') not in self.connections:
                connectionPromises.append(self.ensureConnection(relayInfo))
        await asyncio.gather(*connectionPromises)
        self.logger.get('debug')('manual relay configuration done', JSObject({'connected': self.stats.get('connected')}))
        self.configuring = False
        if len(self.globalBuffer) > 0:
            for item in self.globalBuffer:
                self.sendToRelay(item.get('ip'), item.get('port'), item.get('data'))
            self.globalBuffer = []
            self.globalBufferedBytes = 0

    def getOrCreateConnection(self, relayInfo):
        conn = self.connections.get(relayInfo.get('id'))
        if conn is None:
            conn = JSObject({
                'state': ConnectionState['None'],
                'relayInfo': relayInfo,
                'socket': None,
                'dtlsClient': None,
                'dataChannel': None,
                'packetBuffer': [],
                'bufferedBytes': 0,
                'connectPromise': None,
                'connectionTimeout': None,
                'keepaliveTimer': None,
                'iceConsentTimer': None,
                'localUfrag': randomUfrag(),
                'stats': JSObject({'sentPackets': 0, 'receivedPackets': 0, 'sentBytes': 0, 'receivedBytes': 0}),
            })
            self.connections[relayInfo.get('id')] = conn
        return conn

    def ensureConnection(self, relayInfo):
        conn = self.getOrCreateConnection(relayInfo)
        if conn.get('state') == ConnectionState['Open'] or conn.get('state') == ConnectionState['Connecting']:
            return asyncio.ensure_future(self.awaitPendingConnect(conn))
        promise = self.connect(conn)
        conn['connectPromise'] = promise
        return asyncio.ensure_future(self.awaitConnect(conn, promise, relayInfo))

    async def awaitPendingConnect(self, conn):
        pending = conn.get('connectPromise')
        if pending is None:
            return
        try:
            await pending
        except Exception:
            pass

    async def awaitConnect(self, conn, promise, relayInfo):
        try:
            await promise
        except Exception as err:
            self.logger.get('debug')('manual relay connection attempt failed', JSObject({
                'connectionId': relayInfo.get('id'), 'message': errorMessage(err)}))
        finally:
            conn['connectPromise'] = None

    def performIceBinding(self, socket, relayInfo, conn):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def resolve(value):
            if not fut.done():
                fut.set_result(value)

        iceUfrag = relayInfo.get('authToken') or relayInfo.get('token') or ''
        icePwd = relayInfo.get('key') or ''

        username = ('%s:%s' % (iceUfrag, conn.get('localUfrag'))).encode('utf-8')
        hmacKey = icePwd.encode('utf-8')
        attempt = 0
        settled = False
        timer = None

        def onMessage(datagram, *rest):
            nonlocal settled, timer
            if settled or len(datagram) < 2:
                return
            if (datagram[0] & 0xC0) != 0:
                return
            info = parseStunResponse(datagram)
            if info is not None and info.get('method') == 'binding' and (info.get('isSuccess') or info.get('isError')):
                settled = True
                clearTimeout(timer)
                socket.removeListener('message', onMessage)
                self.logger.get('debug')('ice binding response', JSObject({
                    'connectionId': relayInfo.get('id'), 'class': info.get('stunClass'), 'errorCode': info.get('errorCode')}))
                resolve(info.get('isSuccess'))

        socket.on('message', onMessage)

        def sendAttempt():
            nonlocal settled, attempt, timer
            if settled:
                return
            if attempt >= len(CONFIG['ICE_BINDING_TIMEOUTS_MS']):
                settled = True
                socket.removeListener('message', onMessage)
                self.logger.get('debug')('ice binding got no response, proceeding to dtls anyway', JSObject({
                    'connectionId': relayInfo.get('id')}))
                resolve(False)
                return
            packet = buildBindingRequest(username, hmacKey, None, JSObject({'iceRole': 'controlling'}))
            try:
                socket.send(packet, relayInfo.get('port'), relayInfo.get('ip'))
            except Exception as err:
                self.logger.get('trace')('ice binding send failed', JSObject({
                    'connectionId': relayInfo.get('id'), 'message': errorMessage(err)}))
            wait = CONFIG['ICE_BINDING_TIMEOUTS_MS'][attempt]
            attempt += 1
            timer = setTimeout(sendAttempt, wait)

        sendAttempt()
        return fut

    def startIceConsentRefresh(self, socket, relayInfo, conn):
        iceUfrag = relayInfo.get('authToken') or relayInfo.get('token') or ''
        icePwd = relayInfo.get('key') or ''

        username = ('%s:%s' % (iceUfrag, conn.get('localUfrag'))).encode('utf-8')
        hmacKey = icePwd.encode('utf-8')
        clearInterval(conn.get('iceConsentTimer'))

        def tick():
            if conn.get('state') != ConnectionState['Open'] or conn.get('socket') is None:
                clearInterval(conn.get('iceConsentTimer'))
                conn['iceConsentTimer'] = None
                return
            try:
                packet = buildBindingRequest(username, hmacKey, None, JSObject({'iceRole': 'controlling'}))
                socket.send(packet, relayInfo.get('port'), relayInfo.get('ip'))
            except Exception as err:
                self.logger.get('trace')('ice consent refresh send failed', JSObject({
                    'connectionId': relayInfo.get('id'), 'message': errorMessage(err)}))

        conn['iceConsentTimer'] = setInterval(tick, CONFIG['ICE_CONSENT_REFRESH_MS'])

    def connect(self, conn):
        self.closePeerObjects(conn)
        conn['state'] = ConnectionState['Connecting']
        relayInfo = conn.get('relayInfo')
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        socket = createSocket('udp4')
        conn['socket'] = socket
        targetIp = relayInfo.get('ip')
        targetPort = relayInfo.get('port')
        settledConn = False
        iceBindingDone = False

        def resolveDone(value):
            if not done.done():
                done.set_result(value)

        def rejectDone(err):
            if not done.done():
                done.set_exception(err)

        def settle(err):
            nonlocal settledConn
            if settledConn:
                return
            settledConn = True
            if conn.get('connectionTimeout') is not None:
                clearTimeout(conn.get('connectionTimeout'))
                conn['connectionTimeout'] = None
            if err is not None:
                conn['state'] = ConnectionState['Failed']
                self.closePeerObjects(conn)
                rejectDone(err)
            else:
                resolveDone(None)

        childFn = self.logger.get('child')
        connLogger = childFn(JSObject({'connectionId': relayInfo.get('id')})) if childFn is not None else None
        if connLogger is None:
            connLogger = self.logger

        def sendDatagram(datagram):
            try:
                socket.send(datagram, targetPort, targetIp)
            except Exception:
                pass

        dtls = DtlsClient(sendDatagram=sendDatagram, logger=connLogger)
        conn['dtlsClient'] = dtls

        def sendDtlsPayload(plaintext):
            if not dtls.isConnected:
                return
            record = dtls.encryptApplicationData(plaintext)
            try:
                socket.send(record, targetPort, targetIp)
            except Exception:
                pass

        dc = PreNegotiatedDataChannel(sendDtlsPayload=sendDtlsPayload, logger=connLogger)
        conn['dataChannel'] = dc

        def onSocketMessage(datagram, *rest):
            if not iceBindingDone:
                return
            isDtlsRecord = len(datagram) >= 1 and 20 <= datagram[0] <= 63
            if not isDtlsRecord:
                return
            if dtls.isConnected:
                payloads = dtls.decryptApplicationData(datagram)
                for payload in payloads:
                    dc.handleDtlsPayload(payload)
            else:
                try:
                    dtls.handleDatagram(datagram)
                except Exception as err:
                    self.logger.get('trace')('handleDatagram threw', JSObject({
                        'connectionId': relayInfo.get('id'), 'message': errorMessage(err)}))

        def onSocketError(err, *rest):
            settle(err)

        socket.on('message', onSocketMessage)
        socket.on('error', onSocketError)

        def onDtlsConnected(*args):
            self.logger.get('debug')('dtls handshake complete', JSObject({'connectionId': relayInfo.get('id')}))
            dc.start()

        def onDtlsError(err, *args):
            self.logger.get('debug')('dtls error', JSObject({
                'connectionId': relayInfo.get('id'), 'message': errorMessage(err)}))
            settle(err)

        dtls.on('connected', onDtlsConnected)
        dtls.on('error', onDtlsError)

        def onDcOpen(*args):
            self.logger.get('debug')('datachannel open', JSObject({'connectionId': relayInfo.get('id')}))
            conn['state'] = ConnectionState['Open']
            self.flushBufferedPackets(conn)
            self.logger.get('warn')('[RELAY] mode=meowcaller v3 ice_binding=off consent_refresh=off allocate_streams='
                             + str(9 if self.streamSsrcs is not None else 2),
                             JSObject({'connectionId': relayInfo.get('id')}))
            self.startKeepalive(conn)
            if CONFIG['ICE_CONSENT_REFRESH_ENABLED']:
                self.startIceConsentRefresh(socket, relayInfo, conn)
            self.sendStunAllocateOnOpen(conn, relayInfo)
            self.sendToChannel(conn, bytes(buildWhatsAppPing()))
            self.stats['connected'] += 1
            self.emit('relay_connected', JSObject({'ip': relayInfo.get('ip'), 'port': relayInfo.get('port')}))
            settle(None)

        def onDcError(err, *args):
            self.logger.get('debug')('datachannel error', JSObject({
                'connectionId': relayInfo.get('id'), 'message': errorMessage(err)}))
            settle(err)

        def onDcMessage(payload, *args):
            self.handleRelayStun(conn, relayInfo, payload)
            conn.get('stats')['receivedPackets'] += 1
            conn.get('stats')['receivedBytes'] += byteLengthOf(payload)
            self.stats['received'] += 1
            self.emit('relay_receive', JSObject({'ip': relayInfo.get('ip'), 'port': relayInfo.get('port'), 'data': payload}))

        dc.on('open', onDcOpen)
        dc.on('error', onDcError)
        dc.on('message', onDcMessage)

        conn['connectionTimeout'] = setTimeout(
            lambda: settle(JSError('relay connect timed out after %dms' % CONFIG['CONNECTION_TIMEOUT_MS'])),
            CONFIG['CONNECTION_TIMEOUT_MS'])

        async def onBound():
            nonlocal iceBindingDone
            self.logger.get('warn')('[DIAG] udp socket bound, starting ice binding check', JSObject({
                'connectionId': relayInfo.get('id'), 'localPort': socket.address()['port']}))
            if CONFIG['ICE_BINDING_ENABLED']:
                bindingOk = await self.performIceBinding(socket, relayInfo, conn)
                if settledConn:
                    return
                if not bindingOk:
                    self.logger.get('debug')('ice binding failed, aborting before dtls', JSObject({
                        'connectionId': relayInfo.get('id')}))
                    settle(JSError('ICE binding to relay failed or timed out'))
                    return
            iceBindingDone = True
            self.logger.get('debug')('ice binding ok, starting dtls (client role)', JSObject({
                'connectionId': relayInfo.get('id')}))
            dtls.start()

        socket.bind(0, onBound)
        return done

    def resendSubscriptions(self):
        for conn in list(self.connections.values()):
            if conn.get('state') == ConnectionState['Open'] and conn.get('dataChannel') is not None:
                self.sendStunAllocateOnOpen(conn, conn.get('relayInfo'))
                self.logger.get('debug')('manual relay subscriptions resent', JSObject({
                    'connectionId': conn.get('relayInfo').get('id')}))

    def handleRelayStun(self, conn, relayInfo, payload):
        data = bytes(payload)
        if len(data) < 20 or (data[0] & 0xC0) != 0:
            return
        type = (data[0] << 8) | data[1]
        cookie = ((data[4] << 24) | (data[5] << 16) | (data[6] << 8) | data[7]) & 0xFFFFFFFF
        if cookie != 0x2112A442 and type != 0x0802:
            return
        if type == 0x0001:
            self.rxStun['bindReq'] += 1
            if not relayInfo.get('key'):
                return
            try:
                response = buildRelayBindingSuccess(data[8:20], relayInfo.get('key').encode('utf-8'))
                self.sendToChannel(conn, bytes(response))
                self.rxStun['bindAnswered'] += 1
            except Exception as err:
                self.logger.get('warn')('[RELAY] binding success send failed', JSObject({'message': errorMessage(err)}))
        elif type == 0x0103:
            old = self.rxStun.get('allocOk')
            self.rxStun['allocOk'] = old + 1
            if old == 0:
                self.logger.get('warn')('[RELAY] allocate success from relay', JSObject({'connectionId': relayInfo.get('id')}))
        elif type == 0x0113 or type == 0x0111:
            old = self.rxStun.get('allocErr')
            self.rxStun['allocErr'] = old + 1
            if old == 0:
                self.logger.get('warn')('[RELAY] allocate/binding ERROR from relay', JSObject({
                    'connectionId': relayInfo.get('id'), 'type': '0x%x' % type, 'bytes': len(data)}))
        elif type == 0x0802:
            self.rxStun['pong'] += 1
        else:
            self.rxStun['other'] += 1

    def sendStunAllocateOnOpen(self, conn, relayInfo):
        connectionId = relayInfo.get('id')
        if not relayInfo.get('rawToken') or len(relayInfo.get('rawToken')) == 0:
            self.logger.get('debug')('allocate skipped, no relay token', JSObject({'connectionId': connectionId}))
            return
        if not relayInfo.get('key'):
            self.logger.get('debug')('allocate skipped, no relay key', JSObject({'connectionId': connectionId}))
            return
        if not self.audioSsrc:
            self.logger.get('debug')('allocate skipped, no ssrc yet', JSObject({'connectionId': connectionId}))
            return
        hmacKey = relayInfo.get('key').encode('utf-8')

        if self.streamSsrcs is not None:
            streamSsrcs = list(self.streamSsrcs)
        else:
            streamSsrcs = [self.audioSsrc, 0, 0, self.videoSsrc or 0, 0, 0, 0, 0, 0]
        streamSsrcs[0] = self.audioSsrc
        descriptors = createWasmStreamDescriptors(streamSsrcs)
        packet = buildAllocateForRelay(relayInfo.get('rawToken'), descriptors, hmacKey, relayInfo.get('ip'), relayInfo.get('port'))
        conn['allocatePacket'] = bytes(packet)
        self.sendToChannel(conn, conn.get('allocatePacket'))
        self.logger.get('trace')('allocate sent', JSObject({'connectionId': connectionId, 'size': len(packet)}))

    def sendToChannel(self, conn, data):
        if conn.get('state') != ConnectionState['Open'] or conn.get('dataChannel') is None:
            return
        try:
            conn.get('dataChannel').send(data)
            conn.get('stats')['sentPackets'] += 1
            conn.get('stats')['sentBytes'] += byteLengthOf(data)
            self.stats['sent'] += 1
        except Exception as err:
            self.logger.get('trace')('sendToChannel failed', JSObject({
                'connectionId': conn.get('relayInfo').get('id'), 'message': errorMessage(err)}))

    def closePeerObjects(self, conn):
        if conn.get('keepaliveTimer') is not None:
            clearInterval(conn.get('keepaliveTimer'))
            conn['keepaliveTimer'] = None
        if conn.get('iceConsentTimer') is not None:
            clearInterval(conn.get('iceConsentTimer'))
            conn['iceConsentTimer'] = None
        try:
            if conn.get('dataChannel') is not None:
                conn.get('dataChannel').close()
        except Exception as err:
            self.logger.get('trace')('datachannel close failed', JSObject({'message': errorMessage(err)}))
        try:
            if conn.get('dtlsClient') is not None:
                conn.get('dtlsClient').close()
        except Exception as err:
            self.logger.get('trace')('dtls close failed', JSObject({'message': errorMessage(err)}))
        try:
            if conn.get('socket') is not None:
                conn.get('socket').close()
        except Exception as err:
            self.logger.get('trace')('socket close failed', JSObject({'message': errorMessage(err)}))
        conn['socket'] = None
        conn['dtlsClient'] = None
        conn['dataChannel'] = None

    def startKeepalive(self, conn):
        if conn.get('keepaliveTimer') is not None:
            clearInterval(conn.get('keepaliveTimer'))

        def tick():
            if conn.get('state') != ConnectionState['Open'] or conn.get('dataChannel') is None:
                return
            if conn.get('allocatePacket') is not None:
                self.sendToChannel(conn, conn.get('allocatePacket'))
            self.sendToChannel(conn, bytes(buildWhatsAppPing()))

        conn['keepaliveTimer'] = setInterval(tick, CONFIG['KEEPALIVE_INTERVAL_MS'])

    def flushBufferedPackets(self, conn):
        while len(conn.get('packetBuffer')) > 0:
            packet = conn.get('packetBuffer').pop(0)
            if packet is None:
                continue
            conn['bufferedBytes'] -= byteLengthOf(packet)
            try:
                conn.get('dataChannel').send(packet)
                conn.get('stats')['sentPackets'] += 1
                conn.get('stats')['sentBytes'] += byteLengthOf(packet)
                self.stats['sent'] += 1
            except Exception as err:
                self.logger.get('trace')('flush send failed', JSObject({
                    'connectionId': conn.get('relayInfo').get('id'), 'message': errorMessage(err)}))

    def bufferPacket(self, conn, packet):
        size = byteLengthOf(packet)
        if size > CONFIG['MAX_BUFFER_SIZE']:
            return False
        while len(conn.get('packetBuffer')) > 0 and conn.get('bufferedBytes') + size > CONFIG['MAX_BUFFER_SIZE']:
            dropped = conn.get('packetBuffer').pop(0)
            if dropped is not None:
                conn['bufferedBytes'] -= byteLengthOf(dropped)
        conn.get('packetBuffer').append(packet)
        conn['bufferedBytes'] += size
        return True

    def sendToRelay(self, ip, port, data):
        if self.configuring:
            self.globalBuffer.append(JSObject({'ip': ip, 'port': port, 'data': data}))
            self.globalBufferedBytes += byteLengthOf(data)
            return
        for conn in list(self.connections.values()):
            if conn.get('relayInfo').get('ip') != ip or conn.get('relayInfo').get('port') != port:
                continue
            if conn.get('state') == ConnectionState['Open'] and conn.get('dataChannel') is not None:
                try:
                    conn.get('dataChannel').send(data)
                    conn.get('stats')['sentPackets'] += 1
                    conn.get('stats')['sentBytes'] += byteLengthOf(data)
                    self.stats['sent'] += 1
                except Exception as err:
                    self.logger.get('trace')('sendToRelay failed', JSObject({
                        'connectionId': conn.get('relayInfo').get('id'), 'message': errorMessage(err)}))
            else:
                self.bufferPacket(conn, data)
                task = self.ensureConnection(conn.get('relayInfo'))
                self.backgroundTasks.add(task)
                task.add_done_callback(self.backgroundTasks.discard)

    def broadcast(self, data):
        bytesData = bytes(data)
        isAudio = len(bytesData) < 2 or (bytesData[1] & 0x7F) != PayloadType.H264
        sentAny = False
        for conn in list(self.connections.values()):
            if conn.get('state') == ConnectionState['Open'] and conn.get('dataChannel') is not None:
                try:
                    conn.get('dataChannel').send(data, isAudio)
                    conn.get('stats')['sentPackets'] += 1
                    conn.get('stats')['sentBytes'] += byteLengthOf(data)
                    self.stats['sent'] += 1
                    sentAny = True
                except Exception as err:
                    self.logger.get('trace')('broadcast send failed', JSObject({
                        'connectionId': conn.get('relayInfo').get('id'), 'message': errorMessage(err)}))
        return sentAny

    def hasConnection(self):
        for conn in list(self.connections.values()):
            if conn.get('state') == ConnectionState['Open']:
                return True
        return False

    def getSendBacklog(self):
        for conn in list(self.connections.values()):
            if conn.get('state') == ConnectionState['Open'] and conn.get('dataChannel') is not None:
                return conn.get('dataChannel').getSendBacklog()
        return None

    def getVideoFlushCount(self):
        total = 0
        for conn in list(self.connections.values()):
            dc = conn.get('dataChannel')
            backlog = dc.getSendBacklog() if dc is not None else None
            if backlog is not None:
                total += backlog.get('videoFlushes') if backlog.get('videoFlushes') is not None else 0
        return total

    def getConnectedCount(self):
        count = 0
        for conn in list(self.connections.values()):
            if conn.get('state') == ConnectionState['Open']:
                count += 1
        return count

    def cleanup(self):
        for conn in list(self.connections.values()):
            self.closePeerObjects(conn)
        self.connections.clear()
        self.relayMap.clear()
        self.globalBuffer = []
        self.globalBufferedBytes = 0
