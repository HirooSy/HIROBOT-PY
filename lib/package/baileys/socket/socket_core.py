import asyncio
import base64
import inspect
import json
import math
import re
import urllib.parse
from datetime import datetime, timezone

from baileys.WAProto import proto
from baileys.binary.wa_binary import (
    assertNodeErrorFree,
    binaryNodeToString,
    encodeBinaryNode,
    getAllBinaryNodeChildren,
    getBinaryNodeChild,
    getBinaryNodeChildren,
    isLidUser,
    isTruthy,
    jidDecode,
    jsNumber,
    jidEncode,
    jidNormalizedUser,
    S_WHATSAPP_NET
)
from baileys.constants import DisconnectReason, QueryIds, ReachoutTimelockEnforcementType, XWAPaths
from baileys.defaults import DEF_CALLBACK_PREFIX, DEF_TAG_PREFIX, INITIAL_PREKEY_COUNT, MIN_PREKEY_COUNT, NOISE_WA_HEADER, PROCESSABLE_HISTORY_TYPES, TimeMs, UPLOAD_TIMEOUT
from baileys.foundation.boom import Boom
from baileys.foundation.long import jsParseInt
from baileys.foundation.node_crypto import randomBytes
from baileys.foundation.node_runtime import JSError, JSObject, clearInterval, clearTimeout, now, nextTick, setImmediate, setInterval, setTimeout, sleep
from baileys.foundation.qrcode_terminal import generateQR
from baileys.socket.usync import USyncQuery, USyncUser
from baileys.socket_client.websocket_client import WebSocketClient
from baileys.utils.auth_state_core import addTransactionCapability
from baileys.utils.chat_sync import makeEventBuffer
from baileys.utils.message_compose import jsonStringify
from baileys.utils.username import isValidUsername, isValidUsernameKey, normalizeUsername
from baileys.utils.wa_protocol_core import (
    aesEncryptCTR,
    bindWaitForConnectionUpdate,
    buildPairingQRData,
    bytesToCrockford,
    configureSuccessfulPairing,
    Curve,
    derivePairingCodeKey,
    generateLoginNode,
    generateMdTagPrefix,
    generateRegistrationNode,
    getCodeFromWSError,
    getCompanionPlatformId,
    getErrorCodeFromStreamError,
    getNextPreKeysNode,
    makeNoiseHandler,
    promiseTimeout,
    signedKeyPair,
    xmppSignedPreKey
)
from baileys.wam.wam import BinaryInfo

_pendingTasks = set()


def spawnTask(awaitable):
    task = asyncio.ensure_future(awaitable)
    _pendingTasks.add(task)
    task.add_done_callback(_pendingTasks.discard)
    return task


def asyncListener(fn):
    def listener(*args):
        result = fn(*args)
        if inspect.isawaitable(result):
            return spawnTask(result)
        return result
    return listener


def spawnCatch(awaitable, onError):
    async def run():
        try:
            return await awaitable
        except Exception as err:
            onError(err)
    return spawnTask(run())


async def settle(awaitable, resolve, reject):
    try:
        value = await awaitable
    except Exception as err:
        reject(err)
        return
    resolve(value)


async def maybeAwait(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def raceFirst(awaitable, ms):
    task = spawnTask(awaitable)
    timer = spawnTask(sleep(ms))
    await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
    if not timer.done():
        timer.cancel()
    if task.done() and not task.cancelled():
        task.exception()
    return None


async def raceWithTimeout(awaitable, ms, makeError):
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def onTimeout():
        if not future.done():
            future.set_exception(makeError())

    handle = setTimeout(onTimeout, ms)

    def onSettled(task):
        if future.done():
            return
        if task.cancelled():
            future.set_exception(JSError('Task was cancelled'))
        elif task.exception() is not None:
            future.set_exception(task.exception())
        else:
            future.set_result(task.result())

    task = spawnTask(awaitable)
    task.add_done_callback(onSettled)
    try:
        return await future
    finally:
        clearTimeout(handle)


def dateFromMs(ms):
    if ms is None or ms != ms or math.isinf(ms):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def nowDate():
    return dateFromMs(now())


def textOf(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode('utf-8', 'replace')
    if isinstance(value, str):
        return value
    return None


def toBytes(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return str(value).encode('utf-8')


def tpl(value):
    if value is None:
        return 'undefined'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    return str(value)


def orValue(value, fallback):
    return value if isTruthy(value) else fallback


def getp(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return obj[key]
    except Exception:
        return getattr(obj, key, None)


def hasKey(obj, key):
    return isinstance(obj, dict) and dict.__contains__(obj, key)


def toJSObject(value):
    if isinstance(value, dict):
        return JSObject({key: toJSObject(item) for key, item in dict.items(value)})
    if isinstance(value, list):
        return [toJSObject(item) for item in value]
    return value


def mapWebSocketError(handler):
    def onError(error=None, *rest):
        message = getp(error, 'message') if isinstance(error, dict) else getattr(error, 'message', None)
        handler(Boom(f'WebSocket Error ({tpl(message)})', JSObject({'statusCode': getCodeFromWSError(error), 'data': error})))
    return onError


def urlHref(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get('href')
    return getattr(value, 'href')


def urlProtocol(href):
    return urllib.parse.urlsplit(href).scheme + ':'


def hasSearchParam(href, key):
    return any(k == key for k, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(href).query, keep_blank_values=True))


def setSearchParam(href, key, value):
    parts = urllib.parse.urlsplit(href)
    pairs = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != key]
    pairs.append((key, value))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(pairs), parts.fragment))


class LazyProperty:
    def __init__(self, getter):
        self.getter = getter


class SocketObject(JSObject):
    __slots__ = ()

    def __getitem__(self, key):
        value = dict.__getitem__(self, key)
        if isinstance(value, LazyProperty):
            return value.getter()
        return value


def spreadSocket(sock, extra):
    base = {key: sock[key] for key in list(dict.keys(sock))}
    return JSObject({**base, **extra})


async def wMexQuery(variables, queryId, query, generateMessageTag):
    return await query(JSObject({
        'tag': 'iq',
        'attrs': JSObject({'id': generateMessageTag(), 'type': 'get', 'to': S_WHATSAPP_NET, 'xmlns': 'w:mex'}),
        'content': [JSObject({
            'tag': 'query',
            'attrs': JSObject({'query_id': queryId}),
            'content': jsonStringify(JSObject({'variables': variables})).encode('utf-8')
        })]
    }))


async def executeWMexQuery(variables, queryId, dataPath, query, generateMessageTag):
    result = await wMexQuery(variables, queryId, query, generateMessageTag)
    child = getBinaryNodeChild(result, 'result')
    if child is not None and child.get('content') is not None:
        data = toJSObject(json.loads(textOf(child.get('content'))))
        errors = data.get('errors')
        if isinstance(errors, list) and len(errors) > 0:
            errorMessages = ', '.join(tpl(orValue(err.get('message'), 'Unknown error')) for err in errors)
            firstError = errors[0]
            extensions = firstError.get('extensions')
            errorCode = orValue(getp(extensions, 'error_code'), 400)
            raise Boom(f'GraphQL server error: {errorMessages}', JSObject({'statusCode': errorCode, 'data': firstError}))
        dataRoot = data.get('data')
        if dataPath:
            found = hasKey(dataRoot, dataPath)
            response = dataRoot[dataPath] if found else None
        else:
            found = hasKey(data, 'data')
            response = dataRoot
        if found:
            return response
    dataPathText = dataPath if dataPath is not None else ''
    if dataPathText.startswith('xwa2_'):
        action = dataPathText[5:].replace('_', ' ')
    else:
        action = dataPath.replace('_', ' ') if dataPath is not None else 'undefined'
    raise Boom(f'Failed to {action}, unexpected response structure.', JSObject({'statusCode': 400, 'data': result}))


def isUnfinishedPairing(creds):
    me = getp(creds, 'me')
    return me is not None and me.get('name') == '~' and not creds.get('account')


def makeSocket(config):
    waWebSocketUrl = config.get('waWebSocketUrl')
    connectTimeoutMs = config.get('connectTimeoutMs')
    logger = config.get('logger')
    keepAliveIntervalMs = config.get('keepAliveIntervalMs')
    browser = config.get('browser')
    authState = config.get('auth')
    printQRInTerminal = config.get('printQRInTerminal')
    defaultQueryTimeoutMs = config.get('defaultQueryTimeoutMs')
    transactionOpts = config.get('transactionOpts')
    qrTimeout = config.get('qrTimeout')
    makeSignalRepository = config.get('makeSignalRepository')

    publicWAMBuffer = BinaryInfo()
    serverTimeOffsetMs = 0
    uqTagId = generateMdTagPrefix()
    epoch = 1

    def generateMessageTag():
        nonlocal epoch
        tag = f'{uqTagId}{epoch}'
        epoch += 1
        return tag

    if 'android' in browser[1].lower():
        logger.warn('Using the Android browser is experimental and may lead to unexpected behavior. Use at your own risk.')

    decisions = [config.get('shouldSyncHistoryMessage')(JSObject({'syncType': syncType})) for syncType in PROCESSABLE_HISTORY_TYPES]
    syncDisabled = sum(1 for decision in decisions if decision is False) == len(PROCESSABLE_HISTORY_TYPES)
    if syncDisabled:
        logger.warn('DANGER: DISABLING ALL SYNC BY shouldSyncHistoryMsg PREVENTS BAILEYS FROM ACCESSING INITIAL LID MAPPINGS, LEADING TO INSTABILIY AND SESSION ERRORS')

    href = urlHref(waWebSocketUrl)
    protocolName = urlProtocol(href)
    if isTruthy(config.get('mobile')) or protocolName == 'tcp:':
        raise Boom('Mobile API is not supported anymore', JSObject({'statusCode': DisconnectReason['loggedOut']}))
    routingInfo = getp(getp(authState, 'creds'), 'routingInfo')
    wsConfig = config
    if routingInfo is not None and len(routingInfo) and not isTruthy(config.get('disableStickyRouting')) and protocolName in ('wss:', 'ws:'):
        if not hasSearchParam(href, 'ED'):
            href = setSearchParam(href, 'ED', base64.urlsafe_b64encode(bytes(routingInfo)).decode().rstrip('='))
        headers = JSObject({**(getp(getp(config, 'options'), 'headers') or {})})
        cookieKey = next((key for key in list(dict.keys(headers)) if key.lower() == 'cookie'), None)
        cookie = headers[cookieKey] if cookieKey is not None else ''
        if cookieKey is not None:
            dict.pop(headers, cookieKey)
        if 'sticky_routing=' in cookie:
            headers['Cookie'] = cookie
        elif cookie:
            headers['Cookie'] = f'{cookie}; sticky_routing='
        else:
            headers['Cookie'] = 'sticky_routing='
        wsConfig = JSObject({**config, 'options': JSObject({**(getp(config, 'options') or {}), 'headers': headers})})

    ephemeralKeyPair = Curve['generateKeyPair']()

    noise = makeNoiseHandler(JSObject({
        'keyPair': ephemeralKeyPair,
        'NOISE_HEADER': NOISE_WA_HEADER,
        'logger': logger,
        'routingInfo': routingInfo
    }))

    ws = WebSocketClient(href, wsConfig)
    ws.connect()

    async def sendPromise(data):
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def callback(err=None, *rest):
            if future.done():
                return
            if err is not None:
                future.set_exception(err if isinstance(err, BaseException) else JSError(str(err)))
            else:
                future.set_result(None)
        ws.send(data, callback)
        return await future

    async def sendRawMessage(data):
        if not ws.isOpen:
            raise Boom('Connection Closed', JSObject({'statusCode': DisconnectReason['connectionClosed']}))
        frameBytes = noise.get('encodeFrame')(data)

        def executor(resolve, reject):
            spawnTask(settle(sendPromise(frameBytes), resolve, reject))
        await promiseTimeout(connectTimeoutMs, executor)

    async def sendNode(frame):
        if logger.level == 'trace':
            logger.trace(JSObject({'xml': binaryNodeToString(frame), 'msg': 'xml send'}))
        buff = encodeBinaryNode(frame)
        return await sendRawMessage(buff)

    async def waitForMessage(msgId, timeoutMs=None):
        if timeoutMs is None:
            timeoutMs = defaultQueryTimeoutMs
        onRecv = None
        onErr = None

        def executor(resolve, reject):
            nonlocal onRecv, onErr
            onRecv = lambda data=None, *rest: resolve(data)
            onErr = lambda err=None, *rest: reject(err if isTruthy(err) else Boom('Connection Closed', JSObject({'statusCode': DisconnectReason['connectionClosed']})))
            ws.on(f'TAG:{msgId}', onRecv)
            ws.on('close', onErr)
            ws.on('error', onErr)

        try:
            return await promiseTimeout(timeoutMs, executor)
        except Exception as error:
            if isinstance(error, Boom) and error.output.get('statusCode') == DisconnectReason['timedOut']:
                if logger is not None:
                    logger.warn(JSObject({'msgId': msgId}), 'timed out waiting for message')
                return None
            raise
        finally:
            if onRecv is not None:
                ws.off(f'TAG:{msgId}', onRecv)
            if onErr is not None:
                ws.off('close', onErr)
                ws.off('error', onErr)

    async def query(node, timeoutMs=None):
        if not isTruthy(node.get('attrs').get('id')):
            node.get('attrs')['id'] = generateMessageTag()
        msgId = node.get('attrs').get('id')

        def executor(resolve, reject):
            waiter = spawnTask(waitForMessage(msgId, timeoutMs))

            def onWaiterDone(task):
                if task.cancelled():
                    reject(JSError('Query Cancelled'))
                elif task.exception() is not None:
                    reject(task.exception())
            waiter.add_done_callback(onWaiterDone)

            async def sendThenResolve():
                try:
                    await sendNode(node)
                except Exception as err:
                    reject(err)
                    return
                try:
                    resolve(await waiter)
                except Exception as err:
                    reject(err)
            spawnTask(sendThenResolve())
        result = await promiseTimeout(timeoutMs, executor)
        if isinstance(result, dict) and 'tag' in result:
            assertNodeErrorFree(result)
        return result

    async def digestKeyBundle():
        res = await query(JSObject({'tag': 'iq', 'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'get', 'xmlns': 'encrypt'}), 'content': [JSObject({'tag': 'digest', 'attrs': JSObject({})})]}))
        digestNode = getBinaryNodeChild(res, 'digest')
        if digestNode is None:
            await uploadPreKeys()
            raise JSError('encrypt/get digest returned no digest node')

    async def rotateSignedPreKey():
        newId = (orValue(creds.get('signedPreKey').get('keyId'), 0)) + 1
        skey = signedKeyPair(creds.get('signedIdentityKey'), newId)
        await query(JSObject({'tag': 'iq', 'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'encrypt'}), 'content': [JSObject({'tag': 'rotate', 'attrs': JSObject({}), 'content': [xmppSignedPreKey(skey)]})]}))
        ev.get('emit')('creds.update', JSObject({'signedPreKey': skey}))

    async def executeUSyncQuery(usyncQuery):
        if len(usyncQuery.protocols) == 0:
            raise Boom('USyncQuery must have at least one protocol')
        validUsers = usyncQuery.users
        userNodes = []
        for user in validUsers:
            content = [element for element in (protocol.getUserElement(user) for protocol in usyncQuery.protocols) if element is not None]
            userNodes.append(JSObject({
                'tag': 'user',
                'attrs': JSObject({'jid': None if isTruthy(user.phone) else user.id}),
                'content': content
            }))
        listNode = JSObject({'tag': 'list', 'attrs': JSObject({}), 'content': userNodes})
        queryNode = JSObject({'tag': 'query', 'attrs': JSObject({}), 'content': [protocol.getQueryElement() for protocol in usyncQuery.protocols]})
        iq = JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'get', 'xmlns': 'usync'}),
            'content': [JSObject({
                'tag': 'usync',
                'attrs': JSObject({'context': usyncQuery.context, 'mode': usyncQuery.mode, 'sid': generateMessageTag(), 'last': 'true', 'index': '0'}),
                'content': [queryNode, listNode]
            })]
        })
        result = await query(iq)
        return usyncQuery.parseUSyncQueryResult(result)

    async def onWhatsApp(*phoneNumber):
        usyncQuery = USyncQuery()
        contactEnabled = False
        for jid in phoneNumber:
            if isLidUser(jid):
                if logger is not None:
                    logger.warn('LIDs are not supported with onWhatsApp')
                continue
            if not contactEnabled:
                contactEnabled = True
                usyncQuery = usyncQuery.withContactProtocol()
            phone = '+' + jid.replace('+', '', 1).split('@')[0].split(':')[0]
            usyncQuery.withUser(USyncUser().withPhone(phone))
        if len(usyncQuery.users) == 0:
            return []
        results = await executeUSyncQuery(usyncQuery)
        if results is not None:
            return [JSObject({'jid': entry.get('id'), 'exists': entry.get('contact')}) for entry in results.get('list') if isTruthy(entry.get('contact'))]
        return None

    async def pnFromLIDUSync(jids):
        usyncQuery = USyncQuery().withLIDProtocol().withContext('background')
        for jid in jids:
            if isLidUser(jid):
                if logger is not None:
                    logger.warn('LID user found in LID fetch call')
                continue
            usyncQuery.withUser(USyncUser().withId(jid))
        if len(usyncQuery.users) == 0:
            return []
        results = await executeUSyncQuery(usyncQuery)
        if results is not None:
            return [JSObject({'pn': entry.get('id'), 'lid': entry.get('lid')}) for entry in results.get('list') if isTruthy(entry.get('lid'))]
        return []

    async def resolveUsername(handle, usernameKey=None):
        username = normalizeUsername(handle)
        if not isValidUsername(username):
            raise Boom('Invalid username', JSObject({'statusCode': 400}))
        if usernameKey is not None and not isValidUsernameKey(usernameKey):
            raise Boom('Invalid username key: expected a 4-digit string', JSObject({'statusCode': 400}))
        usyncUser = USyncUser().withUsername(username)
        if isTruthy(usernameKey):
            usyncUser.withUsernameKey(usernameKey)
        usyncQuery = USyncQuery().withContactProtocol().withUser(usyncUser)
        userElement = usyncQuery.protocols[0].getUserElement(usyncUser)
        userNode = JSObject({'tag': 'user', 'attrs': JSObject({}), 'content': [userElement] if userElement is not None else []})
        iq = JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'get', 'xmlns': 'usync'}),
            'content': [JSObject({
                'tag': 'usync',
                'attrs': JSObject({'context': usyncQuery.context, 'mode': usyncQuery.mode, 'sid': generateMessageTag(), 'last': 'true', 'index': '0'}),
                'content': [
                    JSObject({'tag': 'query', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'contact', 'attrs': JSObject({})})]}),
                    JSObject({'tag': 'list', 'attrs': JSObject({}), 'content': [userNode]})
                ]
            })]
        })
        result = await query(iq)
        usyncNode = getBinaryNodeChild(result, 'usync')
        listNode = getBinaryNodeChild(usyncNode, 'list') if usyncNode is not None else None
        resultUserNode = getBinaryNodeChild(listNode, 'user') if listNode is not None else None
        if resultUserNode is None:
            return JSObject({'status': 'not-found'})
        contactNode = getBinaryNodeChild(resultUserNode, 'contact')
        if contactNode is None:
            return JSObject({'status': 'not-found'})
        errorNode = getBinaryNodeChild(contactNode, 'error')
        if errorNode is not None:
            return JSObject({'status': 'key-required'})
        if getp(contactNode.get('attrs'), 'type') != 'in':
            return JSObject({'status': 'not-found'})
        jid = getp(resultUserNode.get('attrs'), 'jid')
        if not isTruthy(jid):
            return JSObject({'status': 'key-required'})
        return JSObject({'status': 'ok', 'jid': jidNormalizedUser(jid)})

    ev = makeEventBuffer(logger)
    creds = authState.get('creds')

    if isUnfinishedPairing(creds):
        logger.info(JSObject({'me': getp(creds.get('me'), 'id')}), 'discarding unfinished pairing state from a previous attempt')
        creds['me'] = None
        creds['registered'] = False

    keys = addTransactionCapability(authState.get('keys'), logger, transactionOpts)
    signalRepository = makeSignalRepository(JSObject({'creds': creds, 'keys': keys}), logger, pnFromLIDUSync)
    socketEndHandlers = []
    uploadPreKeysPromise = None
    didStartBuffer = False
    MAX_PING_FAILURES = 3
    KEEPALIVE_STALL_TOLERANCE_MS = 1000

    def onUnexpectedError(err, msg):
        logger.error(JSObject({'err': err}), f"unexpected error in '{msg}'")

    lastDateRecv = None
    keepAliveReq = None
    qrTimer = None
    closed = False
    consecutivePingFailures = 0
    keepAliveInFlight = False
    lastKeepAliveTick = 0

    async def end(error=None):
        nonlocal closed, consecutivePingFailures
        if closed:
            logger.trace(JSObject({'trace': None}), 'connection already closed')
            return
        closed = True
        logger.info(JSObject({'trace': None}), 'connection errored' if error is not None else 'connection closed')
        clearInterval(keepAliveReq)
        clearTimeout(qrTimer)
        consecutivePingFailures = 0
        ws.removeAllListeners()
        ws.on('error', lambda *args: None)
        noise.get('destroy')()
        if signalRepository.get('close') is not None:
            signalRepository.get('close')()
        if not ws.isClosed and not ws.isClosing:
            try:
                await raceFirst(ws.close(), 5000)
            except Exception:
                pass
        for handler in list(socketEndHandlers):
            try:
                result = handler(error)
                if inspect.isawaitable(result):
                    await result
            except Exception as err:
                logger.error(JSObject({'err': err}), 'error in socket end handler')
        ev.get('emit')('connection.update', JSObject({'connection': 'close', 'lastDisconnect': JSObject({'error': error, 'date': nowDate()})}))
        ev.get('removeAllListeners')('connection.update')
        ev.get('destroy')()

    async def waitForSocketOpen():
        if ws.isOpen:
            return
        if ws.isClosed or ws.isClosing:
            raise Boom('Connection Closed', JSObject({'statusCode': DisconnectReason['connectionClosed']}))
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def resolveOpen(*args):
            if not future.done():
                future.set_result(None)

        def rejectOpen(err=None):
            if not future.done():
                future.set_exception(err if isinstance(err, BaseException) else JSError(str(err)))
        onClose = mapWebSocketError(rejectOpen)
        ws.on('open', resolveOpen)
        ws.on('close', onClose)
        ws.on('error', onClose)
        try:
            await future
        finally:
            ws.off('open', resolveOpen)
            ws.off('close', onClose)
            ws.off('error', onClose)

    async def awaitNextMessage(sendMsg):
        if not ws.isOpen:
            raise Boom('Connection Closed', JSObject({'statusCode': DisconnectReason['connectionClosed']}))
        handlers = JSObject({'onOpen': None, 'onClose': None})

        def executor(resolve, reject):
            handlers['onOpen'] = resolve
            handlers['onClose'] = mapWebSocketError(reject)
            ws.on('frame', handlers.get('onOpen'))
            ws.on('close', handlers.get('onClose'))
            ws.on('error', handlers.get('onClose'))

        async def guarded():
            try:
                return await promiseTimeout(connectTimeoutMs, executor)
            finally:
                ws.off('frame', handlers.get('onOpen'))
                ws.off('close', handlers.get('onClose'))
                ws.off('error', handlers.get('onClose'))
        result = spawnTask(guarded())
        if sendMsg is not None:
            spawnCatch(sendRawMessage(sendMsg), lambda err: handlers.get('onClose')(err))
        return await result

    async def validateConnection():
        helloMsg = proto.HandshakeMessage.fromObject(JSObject({'clientHello': JSObject({'ephemeral': ephemeralKeyPair.get('public')})}))
        logger.info(JSObject({'browser': browser, 'helloMsg': helloMsg}), 'connected to WA')
        init = proto.HandshakeMessage.encode(helloMsg).finish()
        result = await awaitNextMessage(init)
        handshake = proto.HandshakeMessage.decode(result)
        logger.trace(JSObject({'handshake': handshake}), 'handshake recv from WA')
        keyEnc = noise.get('processHandshake')(handshake, creds.get('noiseKey'))
        if creds.get('me') is None:
            node = generateRegistrationNode(creds, config)
            logger.info(JSObject({'node': node}), 'not logged in, attempting registration...')
        else:
            node = generateLoginNode(creds.get('me').get('id'), config)
            logger.info(JSObject({'node': node}), 'logging in...')
        payloadEnc = noise.get('encrypt')(proto.ClientPayload.encode(node).finish())
        await sendRawMessage(proto.HandshakeMessage.encode(JSObject({'clientFinish': JSObject({'static': keyEnc, 'payload': payloadEnc})})).finish())
        await noise.get('finishInit')()
        startKeepAliveRequest()

    async def getAvailablePreKeysOnServer():
        result = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'id': generateMessageTag(), 'xmlns': 'encrypt', 'type': 'get', 'to': S_WHATSAPP_NET}),
            'content': [JSObject({'tag': 'count', 'attrs': JSObject({})})]
        }))
        countChild = getBinaryNodeChild(result, 'count')
        return jsNumber(countChild.get('attrs').get('value'))

    async def uploadPreKeys(count=None):
        nonlocal uploadPreKeysPromise
        if count is None:
            count = MIN_PREKEY_COUNT
        if uploadPreKeysPromise is not None:
            logger.debug('Pre-key upload already in progress, waiting for completion')
            await uploadPreKeysPromise
            return

        async def uploadLogic(retryCount):
            logger.info(JSObject({'count': count, 'retryCount': retryCount}), 'uploading pre-keys')

            async def generate():
                logger.debug(JSObject({'requestedCount': count}), 'generating pre-keys with requested count')
                generated = await getNextPreKeysNode(JSObject({'creds': creds, 'keys': keys}), count)
                ev.get('emit')('creds.update', generated.get('update'))
                return generated.get('node')
            node = await keys.get('transaction')(generate, orValue(getp(creds.get('me'), 'id'), 'upload-pre-keys'))
            try:
                await query(node)
                logger.info(JSObject({'count': count}), 'uploaded pre-keys successfully')
            except Exception as uploadError:
                logger.error(JSObject({'uploadError': str(uploadError), 'count': count}), 'Failed to upload pre-keys to server')
                if retryCount < 3:
                    backoffDelay = min(1000 * (2 ** retryCount), 10000)
                    logger.info(f'Retrying pre-key upload in {backoffDelay}ms')
                    await sleep(backoffDelay)
                    return await uploadLogic(retryCount + 1)
                raise uploadError

        uploadPreKeysPromise = spawnTask(raceWithTimeout(
            uploadLogic(0),
            UPLOAD_TIMEOUT,
            lambda: Boom('Pre-key upload timeout', JSObject({'statusCode': 408}))
        ))
        try:
            await uploadPreKeysPromise
        finally:
            uploadPreKeysPromise = None

    async def verifyCurrentPreKeyExists():
        currentPreKeyId = orValue(creds.get('nextPreKeyId'), 0) - 1
        if currentPreKeyId <= 0:
            return JSObject({'exists': False, 'currentPreKeyId': 0})
        preKeys = await keys.get('get')('pre-key', [str(currentPreKeyId)])
        return JSObject({'exists': isTruthy(preKeys[str(currentPreKeyId)]), 'currentPreKeyId': currentPreKeyId})

    async def uploadPreKeysToServerIfRequired():
        try:
            preKeyCount = await getAvailablePreKeysOnServer()
            count = INITIAL_PREKEY_COUNT if preKeyCount == 0 else MIN_PREKEY_COUNT
            verified = await verifyCurrentPreKeyExists()
            currentPreKeyExists = verified.get('exists')
            currentPreKeyId = verified.get('currentPreKeyId')
            logger.info(f'{tpl(preKeyCount)} pre-keys found on server')
            logger.info(f'Current prekey ID: {tpl(currentPreKeyId)}, exists in storage: {tpl(currentPreKeyExists)}')
            lowServerCount = preKeyCount <= count
            missingCurrentPreKey = (not currentPreKeyExists) and currentPreKeyId > 0
            shouldUpload = lowServerCount or missingCurrentPreKey
            if shouldUpload:
                reasons = []
                if lowServerCount:
                    reasons.append(f'server count low ({tpl(preKeyCount)})')
                if missingCurrentPreKey:
                    reasons.append(f'current prekey {tpl(currentPreKeyId)} missing from storage')
                logger.info('Uploading PreKeys due to: ' + ', '.join(reasons))
                await uploadPreKeys(count)
            else:
                logger.info(f'PreKey validation passed - Server: {tpl(preKeyCount)}, Current prekey {tpl(currentPreKeyId)} exists')
        except Exception as error:
            logger.error(JSObject({'error': error}), 'Failed to check/upload pre-keys during initialization')

    async def onMessageReceived(data):
        nonlocal lastDateRecv

        def onFrame(frame):
            nonlocal lastDateRecv
            lastDateRecv = now()
            anyTriggered = bool(ws.emit('frame', frame))
            if not isinstance(frame, (bytes, bytearray)):
                msgId = frame.get('attrs').get('id')
                if logger.level == 'trace':
                    logger.trace(JSObject({'xml': binaryNodeToString(frame), 'msg': 'recv xml'}))
                anyTriggered = bool(ws.emit(f'{DEF_TAG_PREFIX}{tpl(msgId)}', frame)) or anyTriggered
                l0 = frame.get('tag')
                l1 = frame.get('attrs') if frame.get('attrs') is not None else JSObject({})
                content = frame.get('content')
                if isinstance(content, list):
                    l2 = content[0].get('tag') if len(content) and content[0] is not None else None
                else:
                    l2 = ''
                for key in list(dict.keys(l1)):
                    anyTriggered = bool(ws.emit(f'{DEF_CALLBACK_PREFIX}{tpl(l0)},{key}:{tpl(l1[key])},{tpl(l2)}', frame)) or anyTriggered
                    anyTriggered = bool(ws.emit(f'{DEF_CALLBACK_PREFIX}{tpl(l0)},{key}:{tpl(l1[key])}', frame)) or anyTriggered
                    anyTriggered = bool(ws.emit(f'{DEF_CALLBACK_PREFIX}{tpl(l0)},{key}', frame)) or anyTriggered
                anyTriggered = bool(ws.emit(f'{DEF_CALLBACK_PREFIX}{tpl(l0)},,{tpl(l2)}', frame)) or anyTriggered
                anyTriggered = bool(ws.emit(f'{DEF_CALLBACK_PREFIX}{tpl(l0)}', frame)) or anyTriggered
                if not anyTriggered and logger.level == 'debug':
                    logger.debug(JSObject({'unhandled': True, 'msgId': msgId, 'fromMe': False, 'frame': frame}), 'communication recv')

        try:
            await noise.get('decodeFrame')(data, onFrame)
        except Exception as err:
            if closed:
                return
            logger.error(JSObject({'err': err}), 'failed to process inbound frame, closing connection')
            spawnTask(end(Boom('Connection Terminated (inbound frame failed)', JSObject({'statusCode': DisconnectReason['connectionLost'], 'data': JSObject({'cause': err})}))))

    def runKeepAliveCheck():
        nonlocal lastDateRecv, keepAliveInFlight, consecutivePingFailures
        if closed:
            return
        if not lastDateRecv:
            lastDateRecv = now()
        diff = now() - lastDateRecv
        if diff > keepAliveIntervalMs * 2 + 5000:
            logger.warn(JSObject({'diff': diff, 'keepAliveIntervalMs': keepAliveIntervalMs}), 'connection silent for too long')
            spawnTask(end(Boom('Connection was lost', JSObject({'statusCode': DisconnectReason['connectionLost']}))))
            return
        if not ws.isOpen:
            logger.warn('keep alive called when WS not open')
            return
        if keepAliveInFlight:
            logger.trace('keep alive skipped: ping already in flight')
            return
        if diff < keepAliveIntervalMs / 2:
            logger.trace('keep alive skipped: recent inbound activity', JSObject({'diff': diff}))
            return
        keepAliveInFlight = True

        async def ping():
            nonlocal keepAliveInFlight, consecutivePingFailures
            try:
                await query(JSObject({
                    'tag': 'iq',
                    'attrs': JSObject({'id': generateMessageTag(), 'to': S_WHATSAPP_NET, 'type': 'get', 'xmlns': 'w:p'}),
                    'content': [JSObject({'tag': 'ping', 'attrs': JSObject({})})]
                }), keepAliveIntervalMs)
                consecutivePingFailures = 0
            except Exception as err:
                consecutivePingFailures += 1
                logger.warn(JSObject({'consecutivePingFailures': consecutivePingFailures, 'maxFailures': MAX_PING_FAILURES}), 'keep alive ping failed')
                if consecutivePingFailures >= MAX_PING_FAILURES:
                    logger.warn('max ping failures reached, closing connection')
                    spawnTask(end(Boom('Connection was lost (ping failures)', JSObject({'statusCode': DisconnectReason['connectionLost'], 'data': JSObject({'cause': err})}))))
            finally:
                keepAliveInFlight = False
        spawnTask(ping())

    def startKeepAliveRequest():
        nonlocal lastKeepAliveTick, keepAliveReq
        lastKeepAliveTick = now()

        def tick():
            nonlocal lastKeepAliveTick, lastDateRecv
            currentMs = now()
            stalledMs = max(0, currentMs - lastKeepAliveTick - keepAliveIntervalMs)
            lastKeepAliveTick = currentMs
            if not lastDateRecv:
                lastDateRecv = currentMs
            if stalledMs > KEEPALIVE_STALL_TOLERANCE_MS:
                lastDateRecv = min(currentMs, lastDateRecv + stalledMs)
                logger.warn(JSObject({'stalledMs': stalledMs, 'keepAliveIntervalMs': keepAliveIntervalMs}), 'event loop was blocked, keep-alive silence window shifted')
            setImmediate(runKeepAliveCheck)
        keepAliveReq = setInterval(tick, keepAliveIntervalMs)

    async def sendPassiveIq(tag):
        return await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'xmlns': 'passive', 'type': 'set'}),
            'content': [JSObject({'tag': tag, 'attrs': JSObject({})})]
        }))

    async def logout(msg=None):
        jid = getp(creds.get('me'), 'id')
        if isTruthy(jid):
            await sendNode(JSObject({
                'tag': 'iq',
                'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'id': generateMessageTag(), 'xmlns': 'md'}),
                'content': [JSObject({'tag': 'remove-companion-device', 'attrs': JSObject({'jid': jid, 'reason': 'user_initiated'})})]
            }))
        spawnTask(end(Boom(msg if isTruthy(msg) else 'Intentional Logout', JSObject({'statusCode': DisconnectReason['loggedOut']}))))

    async def generatePairingKey():
        salt = randomBytes(32)
        randomIv = randomBytes(16)
        key = await derivePairingCodeKey(creds.get('pairingCode'), salt)
        ciphered = aesEncryptCTR(creds.get('pairingEphemeralKeyPair').get('public'), key, randomIv)
        return b''.join([bytes(salt), bytes(randomIv), bytes(ciphered)])

    async def requestPairingCode(phoneNumber, customPairingCode=None):
        customCode = '' if customPairingCode is None else str(customPairingCode)
        if isTruthy(customCode) and len(customCode) != 8:
            raise JSError('Custom pairing code must be exactly 8 characters')
        phone = re.sub(r'[^0-9]', '', '' if phoneNumber is None else str(phoneNumber))
        if len(phone) < 7 or len(phone) > 15:
            raise JSError('Invalid phone number: use the international format, digits only (e.g. 6281234567890)')
        if creds.get('account') is not None:
            raise Boom('Session is already paired, log out before requesting a new pairing code', JSObject({'statusCode': 400}))
        if closed or not ws.isOpen:
            raise Boom('Connection Closed', JSObject({'statusCode': DisconnectReason['connectionClosed']}))
        previous = JSObject({'me': creds.get('me'), 'pairingCode': creds.get('pairingCode')})
        pairingCode = customCode if isTruthy(customCode) else bytesToCrockford(randomBytes(5))
        creds['pairingCode'] = pairingCode
        creds['me'] = JSObject({'id': jidEncode(phone, 's.whatsapp.net'), 'name': '~'})
        ev.get('emit')('creds.update', creds)
        try:
            await sendNode(JSObject({
                'tag': 'iq',
                'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'id': generateMessageTag(), 'xmlns': 'md'}),
                'content': [JSObject({
                    'tag': 'link_code_companion_reg',
                    'attrs': JSObject({'jid': creds.get('me').get('id'), 'stage': 'companion_hello', 'should_show_push_notification': 'true'}),
                    'content': [
                        JSObject({'tag': 'link_code_pairing_wrapped_companion_ephemeral_pub', 'attrs': JSObject({}), 'content': await generatePairingKey()}),
                        JSObject({'tag': 'companion_server_auth_key_pub', 'attrs': JSObject({}), 'content': creds.get('noiseKey').get('public')}),
                        JSObject({'tag': 'companion_platform_id', 'attrs': JSObject({}), 'content': getCompanionPlatformId(browser)}),
                        JSObject({'tag': 'companion_platform_display', 'attrs': JSObject({}), 'content': f'{browser[1]} ({browser[0]})'}),
                        JSObject({'tag': 'link_code_pairing_nonce', 'attrs': JSObject({}), 'content': '0'})
                    ]
                })]
            }))
        except Exception as error:
            creds['me'] = previous.get('me')
            creds['pairingCode'] = previous.get('pairingCode')
            ev.get('emit')('creds.update', creds)
            raise error
        return creds.get('pairingCode')

    async def sendWAMBuffer(wamBuffer):
        return await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'id': generateMessageTag(), 'xmlns': 'w:stats'}),
            'content': [JSObject({'tag': 'add', 'attrs': JSObject({'t': str(math.floor(now() / 1000 + 0.5))}), 'content': wamBuffer})]
        }))

    def updateServerTimeOffset(stanza):
        nonlocal serverTimeOffsetMs
        tValue = getp(getp(stanza, 'attrs'), 't')
        if not isTruthy(tValue):
            return
        parsed = jsNumber(tValue)
        if parsed != parsed or parsed <= 0:
            return
        localMs = now()
        serverTimeOffsetMs = parsed * 1000 - localMs
        logger.debug(JSObject({'offset': serverTimeOffsetMs}), 'calculated server time offset')

    def getUnifiedSessionId():
        offsetMs = 3 * TimeMs['Day']
        currentMs = now() + serverTimeOffsetMs
        return str(int((currentMs + offsetMs) % TimeMs['Week']))

    async def sendUnifiedSession():
        if not ws.isOpen:
            return
        node = JSObject({'tag': 'ib', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'unified_session', 'attrs': JSObject({'id': getUnifiedSessionId()})})]})
        try:
            await sendNode(node)
        except Exception as error:
            logger.debug(JSObject({'error': error}), 'failed to send unified_session telemetry')

    def registerSocketEndHandler(handler):
        socketEndHandlers.append(handler)

    async def fetchAccountReachoutTimelock():
        queryResult = await executeWMexQuery(JSObject({}), QueryIds['REACHOUT_TIMELOCK'], XWAPaths['xwa2_fetch_account_reachout_timelock'], query, generateMessageTag)
        endsRaw = getp(queryResult, 'time_enforcement_ends')
        enforcementType = getp(queryResult, 'enforcement_type')
        endsText = str(endsRaw) if endsRaw is not None else ''
        result = JSObject({
            'isActive': isTruthy(getp(queryResult, 'is_active')),
            'timeEnforcementEnds': dateFromMs(jsParseInt(endsText, 10) * 1000) if isTruthy(endsRaw) and endsText != '0' else None,
            'enforcementType': enforcementType if enforcementType is not None else ReachoutTimelockEnforcementType['DEFAULT']
        })
        ev.get('emit')('connection.update', JSObject({'reachoutTimeLock': result}))
        return result

    async def fetchNewChatMessageCap():
        return await executeWMexQuery(JSObject({'input': JSObject({'type': 'INDIVIDUAL_NEW_CHAT_MSG'})}), QueryIds['MESSAGE_CAPPING_INFO'], XWAPaths['xwa2_message_capping_info'], query, generateMessageTag)

    ws.on('message', asyncListener(onMessageReceived))

    async def onOpen(*args):
        try:
            await validateConnection()
        except Exception as err:
            logger.error(JSObject({'err': err}), 'error in validating connection')
            spawnTask(end(err))
    ws.on('open', asyncListener(onOpen))
    ws.on('error', mapWebSocketError(asyncListener(end)))

    def onClose(code, reason=None, *args):
        wsReason = textOf(reason) if isTruthy(reason) else ''
        logger.info(JSObject({'wsCode': code, 'wsReason': wsReason}), 'websocket closed')
        spawnTask(end(Boom('Connection Terminated', JSObject({'statusCode': DisconnectReason['connectionClosed'], 'data': JSObject({'wsCode': code, 'wsReason': wsReason})}))))
    ws.on('close', onClose)

    ws.on('CB:xmlstreamend', lambda *args: spawnTask(end(Boom('Connection Terminated by Server', JSObject({'statusCode': DisconnectReason['connectionClosed']})))))

    async def onPairDevice(stanza):
        nonlocal qrTimer
        iq = JSObject({'tag': 'iq', 'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'result', 'id': stanza.get('attrs').get('id')})})
        await sendNode(iq)
        pairDeviceNode = getBinaryNodeChild(stanza, 'pair-device')
        refNodes = getBinaryNodeChildren(pairDeviceNode, 'ref')
        noiseKeyB64 = base64.b64encode(bytes(creds.get('noiseKey').get('public'))).decode()
        identityKeyB64 = base64.b64encode(bytes(creds.get('signedIdentityKey').get('public'))).decode()
        advB64 = creds.get('advSecretKey')
        qrMs = orValue(qrTimeout, 60000)

        def genPairQR():
            nonlocal qrTimer, qrMs
            if not ws.isOpen:
                return
            refNode = refNodes.pop(0) if len(refNodes) else None
            if refNode is None:
                spawnTask(end(Boom('QR refs attempts ended', JSObject({'statusCode': DisconnectReason['timedOut']}))))
                return
            ref = textOf(refNode.get('content'))
            qr = buildPairingQRData(ref, noiseKeyB64, identityKeyB64, advB64, browser)
            ev.get('emit')('connection.update', JSObject({'qr': qr}))
            if printQRInTerminal:
                generateQR(qr, JSObject({'small': True}), lambda output: print('\n' + output))
            qrTimer = setTimeout(genPairQR, qrMs)
            qrMs = orValue(qrTimeout, 20000)
        genPairQR()
    ws.on('CB:iq,type:set,pair-device', asyncListener(onPairDevice))

    async def onPairSuccess(stanza):
        logger.debug('pair success recv')
        try:
            updateServerTimeOffset(stanza)
            configured = configureSuccessfulPairing(stanza, creds)
            reply = configured.get('reply')
            updatedCreds = configured.get('creds')
            logger.info(JSObject({'me': updatedCreds.get('me'), 'platform': updatedCreds.get('platform')}), 'pairing configured successfully, expect to restart the connection...')
            ev.get('emit')('creds.update', updatedCreds)
            ev.get('emit')('connection.update', JSObject({'isNewLogin': True, 'qr': None}))
            await sendNode(reply)
            spawnTask(sendUnifiedSession())
        except Exception as error:
            logger.info(JSObject({'trace': None}), 'error in pairing')
            spawnTask(end(error))
    ws.on('CB:iq,,pair-success', asyncListener(onPairSuccess))

    async def onSuccess(node):
        try:
            updateServerTimeOffset(node)
            await uploadPreKeysToServerIfRequired()
            await sendPassiveIq('active')
            try:
                await digestKeyBundle()
            except Exception as e:
                logger.warn(JSObject({'e': e}), 'failed to run digest after login')
        except Exception as err:
            logger.warn(JSObject({'err': err}), 'failed to send initial passive iq')
        logger.info('opened connection to WA')
        clearTimeout(qrTimer)
        ev.get('emit')('creds.update', JSObject({'me': JSObject({**(creds.get('me') or {}), 'lid': node.get('attrs').get('lid')})}))
        ev.get('emit')('connection.update', JSObject({'connection': 'open'}))
        spawnTask(sendUnifiedSession())
        if isTruthy(node.get('attrs').get('lid')) and creds.get('me') is not None and isTruthy(creds.get('me').get('id')):
            myLID = node.get('attrs').get('lid')

            async def ownLidSession():
                try:
                    myPN = creds.get('me').get('id')
                    await signalRepository.get('lidMapping').storeLIDPNMappings([JSObject({'lid': myLID, 'pn': myPN})])
                    decoded = jidDecode(myPN)
                    deviceText = str(decoded.get('device')) if decoded.get('device') is not None else '0'
                    await authState.get('keys').get('set')(JSObject({'device-list': JSObject({decoded.get('user'): [deviceText]})}))
                    await signalRepository.get('migrateSession')(myPN, myLID)
                    logger.info(JSObject({'myPN': myPN, 'myLID': myLID}), 'Own LID session created successfully')
                except Exception as error:
                    logger.error(JSObject({'error': error, 'lid': myLID}), 'Failed to create own LID session')
            nextTick(lambda: spawnTask(ownLidSession()))
    ws.on('CB:success', asyncListener(onSuccess))

    def onStreamError(node):
        children = getAllBinaryNodeChildren(node)
        reasonNode = children[0] if len(children) else None
        logger.error(JSObject({'reasonNode': reasonNode, 'fullErrorNode': node}), 'stream errored out')
        info = getErrorCodeFromStreamError(node)
        spawnTask(end(Boom(f"Stream Errored ({tpl(info.get('reason'))})", JSObject({'statusCode': info.get('statusCode'), 'data': reasonNode if reasonNode is not None else node}))))
    ws.on('CB:stream:error', onStreamError)

    def onFailure(node):
        reason = jsNumber(orValue(getp(node.get('attrs'), 'reason'), 500))
        spawnTask(end(Boom('Connection Failure', JSObject({'statusCode': reason, 'data': node.get('attrs')}))))
    ws.on('CB:failure', onFailure)

    ws.on('CB:ib,,downgrade_webclient', lambda *args: spawnTask(end(Boom('Multi-device beta not joined', JSObject({'statusCode': DisconnectReason['multideviceMismatch']})))))

    async def onOfflinePreview(node):
        try:
            rendered = jsonStringify(node)
        except Exception:
            rendered = str(node)
        logger.info('offline preview received', rendered)
        await sendNode(JSObject({'tag': 'ib', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'offline_batch', 'attrs': JSObject({'count': '100'})})]}))
    ws.on('CB:ib,,offline_preview', asyncListener(onOfflinePreview))

    def onEdgeRouting(node):
        edgeRoutingNode = getBinaryNodeChild(node, 'edge_routing')
        routing = getBinaryNodeChild(edgeRoutingNode, 'routing_info')
        if routing is not None and routing.get('content') is not None:
            creds['routingInfo'] = toBytes(routing.get('content'))
            ev.get('emit')('creds.update', creds)
    ws.on('CB:ib,,edge_routing', onEdgeRouting)

    def onInitialTick():
        nonlocal didStartBuffer
        if isTruthy(getp(creds.get('me'), 'id')):
            ev.get('buffer')()
            didStartBuffer = True
        ev.get('emit')('connection.update', JSObject({'connection': 'connecting', 'receivedPendingNotifications': False, 'qr': None}))
    nextTick(onInitialTick)

    def onOffline(node):
        child = getBinaryNodeChild(node, 'offline')
        offlineNotifs = jsNumber(orValue(getp(getp(child, 'attrs'), 'count'), 0))
        logger.info(f'handled {tpl(offlineNotifs)} offline messages/notifications')
        if didStartBuffer:
            ev.get('flush')()
            logger.trace('flushed events for initial buffer')
        ev.get('emit')('connection.update', JSObject({'receivedPendingNotifications': True}))
    ws.on('CB:ib,,offline', onOffline)

    def onCredsUpdate(update):
        name = getp(getp(update, 'me'), 'name')
        if getp(creds.get('me'), 'name') != name:
            logger.debug(JSObject({'name': name}), 'updated pushName')
            spawnCatch(sendNode(JSObject({'tag': 'presence', 'attrs': JSObject({'name': name})})), lambda err: logger.warn(JSObject({'trace': None}), 'error in sending presence update on name change'))
        dict.update(creds, update)
    ev.get('on')('creds.update', onCredsUpdate)

    return SocketObject(JSObject({
        'type': 'md',
        'ws': ws,
        'ev': ev,
        'authState': JSObject({'creds': creds, 'keys': keys}),
        'signalRepository': signalRepository,
        'user': LazyProperty(lambda: authState.get('creds').get('me')),
        'connectionHealth': LazyProperty(lambda: JSObject({'lastMessageReceived': dateFromMs(lastDateRecv), 'consecutivePingFailures': consecutivePingFailures})),
        'generateMessageTag': generateMessageTag,
        'query': query,
        'waitForMessage': waitForMessage,
        'waitForSocketOpen': waitForSocketOpen,
        'sendRawMessage': sendRawMessage,
        'sendNode': sendNode,
        'logout': logout,
        'end': end,
        'registerSocketEndHandler': registerSocketEndHandler,
        'onUnexpectedError': onUnexpectedError,
        'uploadPreKeys': uploadPreKeys,
        'uploadPreKeysToServerIfRequired': uploadPreKeysToServerIfRequired,
        'digestKeyBundle': digestKeyBundle,
        'rotateSignedPreKey': rotateSignedPreKey,
        'requestPairingCode': requestPairingCode,
        'updateServerTimeOffset': updateServerTimeOffset,
        'sendUnifiedSession': sendUnifiedSession,
        'wamBuffer': publicWAMBuffer,
        'waitForConnectionUpdate': bindWaitForConnectionUpdate(ev),
        'sendWAMBuffer': sendWAMBuffer,
        'executeUSyncQuery': executeUSyncQuery,
        'onWhatsApp': onWhatsApp,
        'resolveUsername': resolveUsername,
        'fetchAccountReachoutTimelock': fetchAccountReachoutTimelock,
        'fetchNewChatMessageCap': fetchNewChatMessageCap
    }))

