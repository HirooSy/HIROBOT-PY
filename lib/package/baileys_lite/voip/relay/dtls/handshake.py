from baileys_lite.foundation.node_crypto import randomBytes, createHash, X509Certificate
from baileys_lite.foundation.node_runtime import EventEmitter, JSError, JSObject, clearTimeout, setTimeout
from baileys_lite.voip.relay.dtls.record import ContentType, encodeRecord, decodeRecords
from baileys_lite.voip.relay.dtls.handshake_framing import (
    HandshakeType, encodeHandshakeMessage, decodeHandshakeFragment, HandshakeReassembler, logicalHandshakeBytes,
)
import baileys_lite.voip.relay.dtls.handshake_messages as msg
from baileys_lite.voip.relay.dtls.cert_builder import generateSelfSignedCert
from baileys_lite.voip.relay.dtls.ecdhe import (
    generateEcdheKeypair, buildEcdheSignedParams, verifyEcdheParams, signEcdheParams,
)
from baileys_lite.voip.relay.dtls.prf import deriveMasterSecret, deriveKeyBlock, deriveVerifyData
from baileys_lite.voip.relay.dtls.aead import encryptRecord, decryptRecord, aes128GcmParams
from baileys_lite.voip.shim.core import createNoopLogger


RETRANSMIT_TIMEOUTS_MS = [1000, 2000, 4000, 8000, 8000]
HANDSHAKE_TIMEOUT_MS = 25000


def takeSeq(owner, name):
    value = getattr(owner, name)
    setattr(owner, name, value + 1)
    return value


class DtlsClient(EventEmitter):
    def __init__(self, sendDatagram=None, logger=None):
        super().__init__()
        self.sendDatagram = sendDatagram
        self.logger = logger if logger is not None else createNoopLogger()
        self.state = 'idle'
        self.messageSeq = 0
        self.reassembler = HandshakeReassembler()
        self.handshakeMessages = []
        self.clientRandom = None
        self.serverRandom = None
        self.cookie = b''
        self.ecdhe = None
        self.cert = None
        self.peerCertificates = []
        self.pendingServerKeyExchange = None
        self.serverRequestedClientCert = False
        self.masterSecret = None
        self.writeEpoch = 0
        self.writeSeq = 0
        self.readSeqByEpoch = {0: -1}
        self.keys = None
        self.retransmitTimer = None
        self.retransmitAttempt = 0
        self.lastFlightDatagrams = []
        self.overallTimeoutTimer = None

    def start(self):
        self.clientRandom = randomBytes(32)
        self.cert = generateSelfSignedCert('wa-voip')
        self.state = 'wait_hvr'
        self.sendFlight1()
        self.overallTimeoutTimer = setTimeout(
            lambda: self.fail(JSError('DTLS handshake timed out overall')), HANDSHAKE_TIMEOUT_MS)

    def close(self):
        self.state = 'closed'
        clearTimeout(self.retransmitTimer)
        clearTimeout(self.overallTimeoutTimer)

    def handleDatagram(self, datagram):
        if self.state == 'closed':
            return
        try:
            records = decodeRecords(datagram)
        except Exception:
            return
        for record in records:
            self.handleRecord(record)

    @property
    def isConnected(self):
        return self.state == 'connected'

    def encryptApplicationData(self, plaintext):
        if not self.isConnected:
            raise JSError('DtlsClient.encryptApplicationData: not connected yet')
        seq = takeSeq(self, 'writeSeq')
        fragment = encryptRecord(self.keys.get('clientWriteKey'), self.keys.get('clientWriteIv'),
                                 self.writeEpoch, seq, ContentType['APPLICATION_DATA'], plaintext)
        return encodeRecord(ContentType['APPLICATION_DATA'], self.writeEpoch, seq, fragment)

    def decryptApplicationData(self, datagram):
        out = []
        try:
            records = decodeRecords(datagram)
        except Exception as e:
            self.logger.get('trace')('post-handshake datagram failed to decode as DTLS records', JSObject({
                'message': str(e), 'bytes': len(datagram), 'hex': bytes(datagram).hex()}))
            return out
        for record in records:
            if record.get('type') != ContentType['APPLICATION_DATA']:
                self.logger.get('trace')('post-handshake non-application_data record', JSObject({
                    'type': record.get('type'), 'epoch': record.get('epoch'), 'seq': record.get('sequenceNumber'),
                    'fragmentLen': len(record.get('fragment'))}))
                self.handleRecord(record)
                continue
            readFloor = self.readSeqByEpoch.get(record.get('epoch'), -1)
            if record.get('sequenceNumber') <= readFloor:
                self.logger.get('trace')('dropping application_data as replay/duplicate', JSObject({
                    'epoch': record.get('epoch'), 'seq': str(record.get('sequenceNumber')), 'readFloor': str(readFloor)}))
                continue
            try:
                plaintext = decryptRecord(self.keys.get('serverWriteKey'), self.keys.get('serverWriteIv'),
                                          record.get('epoch'), record.get('sequenceNumber'), record.get('type'),
                                          record.get('fragment'))
                self.readSeqByEpoch[record.get('epoch')] = record.get('sequenceNumber')
                self.logger.get('trace')('decrypted application_data', JSObject({
                    'epoch': record.get('epoch'), 'seq': str(record.get('sequenceNumber')),
                    'plaintextLen': len(plaintext)}))
                out.append(plaintext)
            except Exception as e:
                self.logger.get('trace')('FAILED to decrypt post-handshake record', JSObject({
                    'epoch': record.get('epoch'), 'seq': str(record.get('sequenceNumber')),
                    'fragmentLen': len(record.get('fragment')), 'message': str(e)}))
        return out

    def sendFlight1(self):
        chBody = msg.buildClientHello(self.clientRandom, cookie=self.cookie)
        datagram = self.packHandshakeMessage(HandshakeType['CLIENT_HELLO'], chBody)
        self.transmitFlight([datagram])

    def sendFlight3(self, serverPublicKeyPoint):
        self.ecdhe = generateEcdheKeypair()
        preMasterSecret = self.ecdhe.get('computeSharedSecret')(serverPublicKeyPoint)
        self.masterSecret = deriveMasterSecret(preMasterSecret, self.clientRandom, self.serverRandom)

        certPart = b''
        if self.serverRequestedClientCert:
            certPart = self.packHandshakeMessage(HandshakeType['CERTIFICATE'],
                                                 msg.buildCertificate(self.cert.get('certDer')))

        cke = self.packHandshakeMessage(HandshakeType['CLIENT_KEY_EXCHANGE'],
                                        msg.buildClientKeyExchange(self.ecdhe.get('publicKeyPoint')))

        certVerifyPart = b''
        if self.serverRequestedClientCert:
            certVerifyPart = self.packHandshakeMessage(
                HandshakeType['CERTIFICATE_VERIFY'],
                msg.buildCertificateVerify(msg.SIGNATURE_SCHEME_ECDSA_SECP256R1_SHA256,
                                           signEcdheParams(self.cert.get('privateKey'),
                                                           b''.join(self.handshakeMessages))))

        self.keys = deriveKeyBlock(self.masterSecret, self.clientRandom, self.serverRandom, aes128GcmParams)
        handshakeHashBeforeFinished = createHash('sha256').update(b''.join(self.handshakeMessages)).digest()
        verifyData = deriveVerifyData(self.masterSecret, 'client finished', handshakeHashBeforeFinished)
        finishedBody = msg.buildFinished(verifyData)
        finishedMsgSeq = takeSeq(self, 'messageSeq')
        finishedLogical = logicalHandshakeBytes(HandshakeType['FINISHED'], finishedMsgSeq, finishedBody)
        finishedFragments = encodeHandshakeMessage(HandshakeType['FINISHED'], finishedMsgSeq, finishedBody)

        self.handshakeMessages.append(finishedLogical)

        ccsRecord = encodeRecord(ContentType['CHANGE_CIPHER_SPEC'], self.writeEpoch,
                                 takeSeq(self, 'writeSeq'), bytes([1]))
        self.writeEpoch = 1
        self.writeSeq = 0

        finishedSeq = takeSeq(self, 'writeSeq')
        encryptedFinished = encryptRecord(self.keys.get('clientWriteKey'), self.keys.get('clientWriteIv'),
                                          self.writeEpoch, finishedSeq, ContentType['HANDSHAKE'],
                                          finishedFragments[0])
        finishedRecord = encodeRecord(ContentType['HANDSHAKE'], self.writeEpoch, finishedSeq, encryptedFinished)

        self.state = 'wait_server_finished'

        self.transmitFlight([certPart + cke + certVerifyPart + ccsRecord + finishedRecord])

    def packHandshakeMessage(self, msgType, body):
        seq = takeSeq(self, 'messageSeq')
        fragments = encodeHandshakeMessage(msgType, seq, body)
        self.handshakeMessages.append(logicalHandshakeBytes(msgType, seq, body))
        out = b''
        for f in fragments:
            out += encodeRecord(ContentType['HANDSHAKE'], self.writeEpoch, takeSeq(self, 'writeSeq'), f)
        return out

    def transmitFlight(self, datagrams):
        self.lastFlightDatagrams = datagrams
        self.retransmitAttempt = 0
        for d in datagrams:
            self.sendDatagram(d)
        self.armRetransmit()

    def armRetransmit(self):
        clearTimeout(self.retransmitTimer)
        if self.state == 'connected' or self.state == 'closed':
            return
        timeout = RETRANSMIT_TIMEOUTS_MS[min(self.retransmitAttempt, len(RETRANSMIT_TIMEOUTS_MS) - 1)]

        def onRetransmit():
            self.retransmitAttempt += 1
            for d in self.lastFlightDatagrams:
                self.sendDatagram(d)
            self.armRetransmit()

        self.retransmitTimer = setTimeout(onRetransmit, timeout)

    def handleRecord(self, record):
        if record.get('type') == ContentType['HANDSHAKE']:
            if record.get('epoch') == 0:
                decoded = decodeHandshakeFragment(record.get('fragment'))
                complete = self.reassembler.addFragment(decoded)
                if complete is not None:
                    self.handleHandshakeMessage(complete)
                return
            if record.get('epoch') == 1 and self.state == 'wait_server_finished':
                self.tryDecryptServerFinished(record)
                return
            return
        if record.get('type') == ContentType['CHANGE_CIPHER_SPEC']:
            return
        if record.get('type') == ContentType['ALERT']:
            self.fail(JSError('DTLS alert received (epoch %s): %s' % (
                record.get('epoch'), bytes(record.get('fragment')).hex())))
            return

    def handleHandshakeMessage(self, complete):
        msgType = complete.get('msgType')
        messageSeq = complete.get('messageSeq')
        body = complete.get('body')
        if self.state == 'wait_hvr' and msgType == HandshakeType['HELLO_VERIFY_REQUEST']:
            self.cookie = msg.parseHelloVerifyRequest(body)['cookie']
            self.handshakeMessages = []
            self.messageSeq = 0
            self.state = 'wait_server_flight'
            chBody = msg.buildClientHello(self.clientRandom, cookie=self.cookie)
            datagram = self.packHandshakeMessage(HandshakeType['CLIENT_HELLO'], chBody)
            self.transmitFlight([datagram])
            return
        if self.state == 'wait_server_flight' or self.state == 'wait_hvr':
            self.state = 'wait_server_flight'
            self.handshakeMessages.append(logicalHandshakeBytes(msgType, messageSeq, body))
            if msgType == HandshakeType['SERVER_HELLO']:
                self.serverRandom = msg.parseServerHello(body)['serverRandom']
                return
            if msgType == HandshakeType['CERTIFICATE']:
                self.peerCertificates = msg.parseCertificate(body)['certificates']
                return
            if msgType == HandshakeType['SERVER_KEY_EXCHANGE']:
                self.pendingServerKeyExchange = msg.parseServerKeyExchange(body)
                return
            if msgType == HandshakeType['CERTIFICATE_REQUEST']:
                self.serverRequestedClientCert = True
                return
            if msgType == HandshakeType['SERVER_HELLO_DONE']:
                self.onServerHelloDone()
                return

    def onServerHelloDone(self):
        if self.serverRandom is None or len(self.peerCertificates) == 0 or self.pendingServerKeyExchange is None:
            self.fail(JSError('DTLS: ServerHelloDone arrived before a required prior message'))
            return
        try:
            publicKeyPoint = self.pendingServerKeyExchange.get('publicKeyPoint')
            signature = self.pendingServerKeyExchange.get('signature')
            signedParams = buildEcdheSignedParams(self.clientRandom, self.serverRandom, publicKeyPoint)
            leaf = X509Certificate(self.peerCertificates[0])
            ok = verifyEcdheParams(leaf.publicKey, signedParams, signature)
            if not ok:
                raise JSError('ServerKeyExchange signature verification failed')
            self.sendFlight3(publicKeyPoint)
        except Exception as e:
            self.fail(JSError('DTLS: ServerKeyExchange verification failed: %s' % str(e)))

    def tryDecryptServerFinished(self, record):
        readFloor = self.readSeqByEpoch.get(1, -1)
        if record.get('sequenceNumber') <= readFloor:
            return
        try:
            plaintext = decryptRecord(self.keys.get('serverWriteKey'), self.keys.get('serverWriteIv'), 1,
                                      record.get('sequenceNumber'), ContentType['HANDSHAKE'], record.get('fragment'))
        except Exception as e:
            self.fail(JSError('DTLS: failed to decrypt server Finished: %s' % str(e)))
            return
        self.readSeqByEpoch[1] = record.get('sequenceNumber')
        decoded = decodeHandshakeFragment(plaintext)
        if decoded.get('msgType') != HandshakeType['FINISHED']:
            return
        verifyData = msg.parseFinished(decoded.get('body'))['verifyData']
        handshakeHash = createHash('sha256').update(b''.join(self.handshakeMessages)).digest()
        expected = deriveVerifyData(self.masterSecret, 'server finished', handshakeHash)
        if expected != verifyData:
            self.fail(JSError('DTLS: server Finished verify_data mismatch — handshake integrity check failed'))
            return
        self.state = 'connected'
        clearTimeout(self.retransmitTimer)
        clearTimeout(self.overallTimeoutTimer)
        self.emit('connected')

    def fail(self, err):
        self.state = 'closed'
        clearTimeout(self.retransmitTimer)
        clearTimeout(self.overallTimeoutTimer)
        self.emit('error', err)


class DtlsServer(EventEmitter):
    def __init__(self, sendDatagram=None, logger=None):
        super().__init__()
        self.sendDatagram = sendDatagram
        self.logger = logger if logger is not None else createNoopLogger()
        self.state = 'idle'
        self.messageSeq = 0
        self.reassembler = HandshakeReassembler()
        self.handshakeMessages = []
        self.clientRandom = None
        self.serverRandom = None
        self.ecdhe = None
        self.cert = None
        self.masterSecret = None
        self.writeEpoch = 0
        self.writeSeq = 0
        self.readSeqByEpoch = {0: -1}
        self.keys = None
        self.retransmitTimer = None
        self.retransmitAttempt = 0
        self.lastFlightDatagrams = []
        self.overallTimeoutTimer = None

    def startPassive(self):
        self.cert = generateSelfSignedCert('wa-voip')
        self.state = 'wait_client_hello'
        self.overallTimeoutTimer = setTimeout(
            lambda: self.fail(JSError('DTLS handshake timed out overall (passive/server role)')),
            HANDSHAKE_TIMEOUT_MS)

    def close(self):
        self.state = 'closed'
        clearTimeout(self.retransmitTimer)
        clearTimeout(self.overallTimeoutTimer)

    def handleDatagram(self, datagram):
        if self.state == 'closed':
            return
        try:
            records = decodeRecords(datagram)
        except Exception:
            return
        for record in records:
            self.handleRecord(record)

    @property
    def isConnected(self):
        return self.state == 'connected'

    def encryptApplicationData(self, plaintext):
        if not self.isConnected:
            raise JSError('DtlsServer.encryptApplicationData: not connected yet')
        seq = takeSeq(self, 'writeSeq')
        fragment = encryptRecord(self.keys.get('serverWriteKey'), self.keys.get('serverWriteIv'),
                                 self.writeEpoch, seq, ContentType['APPLICATION_DATA'], plaintext)
        return encodeRecord(ContentType['APPLICATION_DATA'], self.writeEpoch, seq, fragment)

    def decryptApplicationData(self, datagram):
        out = []
        try:
            records = decodeRecords(datagram)
        except Exception as e:
            self.logger.get('trace')('post-handshake datagram failed to decode as DTLS records', JSObject({
                'message': str(e), 'bytes': len(datagram), 'hex': bytes(datagram).hex()}))
            return out
        for record in records:
            if record.get('type') != ContentType['APPLICATION_DATA']:
                self.logger.get('trace')('post-handshake non-application_data record', JSObject({
                    'type': record.get('type'), 'epoch': record.get('epoch'), 'seq': record.get('sequenceNumber'),
                    'fragmentLen': len(record.get('fragment'))}))
                self.handleRecord(record)
                continue
            readFloor = self.readSeqByEpoch.get(record.get('epoch'), -1)
            if record.get('sequenceNumber') <= readFloor:
                continue
            try:
                plaintext = decryptRecord(self.keys.get('clientWriteKey'), self.keys.get('clientWriteIv'),
                                          record.get('epoch'), record.get('sequenceNumber'), record.get('type'),
                                          record.get('fragment'))
                self.readSeqByEpoch[record.get('epoch')] = record.get('sequenceNumber')
                out.append(plaintext)
            except Exception as e:
                self.logger.get('trace')('FAILED to decrypt post-handshake record', JSObject({
                    'epoch': record.get('epoch'), 'seq': str(record.get('sequenceNumber')),
                    'fragmentLen': len(record.get('fragment')), 'message': str(e)}))
        return out

    def handleRecord(self, record):
        if record.get('type') == ContentType['HANDSHAKE']:
            if record.get('epoch') == 0:
                decoded = decodeHandshakeFragment(record.get('fragment'))
                complete = self.reassembler.addFragment(decoded)
                if complete is not None:
                    self.handleHandshakeMessage(complete)
                return
            if record.get('epoch') == 1 and self.state == 'wait_client_finished':
                self.tryDecryptClientFinished(record)
                return
            return
        if record.get('type') == ContentType['CHANGE_CIPHER_SPEC']:
            return
        if record.get('type') == ContentType['ALERT']:
            self.fail(JSError('DTLS alert received (epoch %s): %s' % (
                record.get('epoch'), bytes(record.get('fragment')).hex())))
            return

    def handleHandshakeMessage(self, complete):
        msgType = complete.get('msgType')
        messageSeq = complete.get('messageSeq')
        body = complete.get('body')
        if self.state == 'wait_client_hello' and msgType == HandshakeType['CLIENT_HELLO']:
            self.handshakeMessages.append(logicalHandshakeBytes(msgType, messageSeq, body))
            self.clientRandom = msg.parseClientHello(body)['clientRandom']
            self.sendFlight2()
            return
        if self.state == 'wait_client_finished' and msgType == HandshakeType['CLIENT_KEY_EXCHANGE']:
            self.handshakeMessages.append(logicalHandshakeBytes(msgType, messageSeq, body))
            publicKeyPoint = msg.parseClientKeyExchange(body)['publicKeyPoint']
            self.finishKeyExchange(publicKeyPoint)
            return

    def sendFlight2(self):
        self.serverRandom = randomBytes(32)
        self.ecdhe = generateEcdheKeypair()

        shBody = msg.buildServerHello(self.serverRandom, cipherSuite=msg.CIPHER_SUITE_ECDHE_ECDSA_AES_128_GCM_SHA256)
        shDatagram = self.packHandshakeMessage(HandshakeType['SERVER_HELLO'], shBody)

        certBody = msg.buildCertificate(self.cert.get('certDer'))
        certDatagram = self.packHandshakeMessage(HandshakeType['CERTIFICATE'], certBody)

        signedParams = buildEcdheSignedParams(self.clientRandom, self.serverRandom, self.ecdhe.get('publicKeyPoint'))
        signature = signEcdheParams(self.cert.get('privateKey'), signedParams)
        skeBody = msg.buildServerKeyExchange(self.ecdhe.get('publicKeyPoint'), signature)
        skeDatagram = self.packHandshakeMessage(HandshakeType['SERVER_KEY_EXCHANGE'], skeBody)

        shdBody = msg.buildServerHelloDone()
        shdDatagram = self.packHandshakeMessage(HandshakeType['SERVER_HELLO_DONE'], shdBody)

        self.state = 'wait_client_finished'
        self.transmitFlight([shDatagram + certDatagram + skeDatagram + shdDatagram])

    def finishKeyExchange(self, clientPublicKeyPoint):
        try:
            preMasterSecret = self.ecdhe.get('computeSharedSecret')(clientPublicKeyPoint)
            self.masterSecret = deriveMasterSecret(preMasterSecret, self.clientRandom, self.serverRandom)
            self.keys = deriveKeyBlock(self.masterSecret, self.clientRandom, self.serverRandom, aes128GcmParams)
        except Exception as e:
            self.fail(JSError('DTLS: server-side key derivation failed: %s' % str(e)))

    def tryDecryptClientFinished(self, record):
        readFloor = self.readSeqByEpoch.get(1, -1)
        if record.get('sequenceNumber') <= readFloor:
            return
        if self.keys is None:
            self.logger.get('trace')('client Finished arrived before key derivation completed', JSObject({}))
            return
        try:
            plaintext = decryptRecord(self.keys.get('clientWriteKey'), self.keys.get('clientWriteIv'), 1,
                                      record.get('sequenceNumber'), ContentType['HANDSHAKE'], record.get('fragment'))
        except Exception as e:
            self.fail(JSError('DTLS: failed to decrypt client Finished: %s' % str(e)))
            return
        self.readSeqByEpoch[1] = record.get('sequenceNumber')
        decoded = decodeHandshakeFragment(plaintext)
        if decoded.get('msgType') != HandshakeType['FINISHED']:
            return
        verifyData = msg.parseFinished(decoded.get('body'))['verifyData']

        handshakeHashForClient = createHash('sha256').update(b''.join(self.handshakeMessages)).digest()
        expected = deriveVerifyData(self.masterSecret, 'client finished', handshakeHashForClient)
        if expected != verifyData:
            self.fail(JSError('DTLS: client Finished verify_data mismatch — handshake integrity check failed'))
            return

        clientFinishedLogical = logicalHandshakeBytes(decoded.get('msgType'), decoded.get('messageSeq'), decoded.get('body'))
        self.handshakeMessages.append(clientFinishedLogical)
        self.sendServerFinished()

    def sendServerFinished(self):
        handshakeHash = createHash('sha256').update(b''.join(self.handshakeMessages)).digest()
        verifyData = deriveVerifyData(self.masterSecret, 'server finished', handshakeHash)
        finishedBody = msg.buildFinished(verifyData)
        finishedMsgSeq = takeSeq(self, 'messageSeq')
        finishedFragments = encodeHandshakeMessage(HandshakeType['FINISHED'], finishedMsgSeq, finishedBody)

        ccsRecord = encodeRecord(ContentType['CHANGE_CIPHER_SPEC'], self.writeEpoch,
                                 takeSeq(self, 'writeSeq'), bytes([1]))
        self.writeEpoch = 1
        self.writeSeq = 0

        finishedSeq = takeSeq(self, 'writeSeq')
        encryptedFinished = encryptRecord(self.keys.get('serverWriteKey'), self.keys.get('serverWriteIv'),
                                          self.writeEpoch, finishedSeq, ContentType['HANDSHAKE'],
                                          finishedFragments[0])
        finishedRecord = encodeRecord(ContentType['HANDSHAKE'], self.writeEpoch, finishedSeq, encryptedFinished)

        self.state = 'connected'
        clearTimeout(self.retransmitTimer)
        clearTimeout(self.overallTimeoutTimer)
        self.transmitFlight([ccsRecord + finishedRecord])
        self.emit('connected')

    def packHandshakeMessage(self, msgType, body):
        seq = takeSeq(self, 'messageSeq')
        fragments = encodeHandshakeMessage(msgType, seq, body)
        self.handshakeMessages.append(logicalHandshakeBytes(msgType, seq, body))
        out = b''
        for f in fragments:
            out += encodeRecord(ContentType['HANDSHAKE'], self.writeEpoch, takeSeq(self, 'writeSeq'), f)
        return out

    def transmitFlight(self, datagrams):
        self.lastFlightDatagrams = datagrams
        self.retransmitAttempt = 0
        for d in datagrams:
            self.sendDatagram(d)
        self.armRetransmit()

    def armRetransmit(self):
        clearTimeout(self.retransmitTimer)
        if self.state == 'connected' or self.state == 'closed':
            return
        timeout = RETRANSMIT_TIMEOUTS_MS[min(self.retransmitAttempt, len(RETRANSMIT_TIMEOUTS_MS) - 1)]

        def onRetransmit():
            self.retransmitAttempt += 1
            for d in self.lastFlightDatagrams:
                self.sendDatagram(d)
            self.armRetransmit()

        self.retransmitTimer = setTimeout(onRetransmit, timeout)

    def fail(self, err):
        self.state = 'closed'
        clearTimeout(self.retransmitTimer)
        clearTimeout(self.overallTimeoutTimer)
        self.emit('error', err)
