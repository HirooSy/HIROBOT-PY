import asyncio
import base64
import contextvars
import inspect
import json
from baileys_lite.foundation.node_runtime import jsonDefault
import re

from baileys_lite.binary.wa_binary import (
    areJidsSameUser,
    getBinaryNodeChild,
    getBinaryNodeChildren,
    isHostedLidUser,
    isHostedPnUser,
    isJidBroadcast,
    isJidGroup,
    isJidMetaAI,
    isJidNewsletter,
    isJidStatusBroadcast,
    isLidUser,
    isPnUser,
    jidDecode,
    jidNormalizedUser
)
from baileys_lite.foundation.boom import Boom
from baileys_lite.foundation.cache import Cache
from baileys_lite.foundation.concurrency import makeKeyedSerialTaskQueues, makeMutex, makeSerialTaskQueue
from baileys_lite.foundation.long import jsParseInt, stringToNumber
from baileys_lite.foundation.node_crypto import randomBytes
from baileys_lite.foundation.node_runtime import JSError, JSObject, now
from baileys_lite.utils.wa_protocol_core import Curve, delay, generateRegistrationId, signedKeyPair, unpadRandomMax16
from baileys_lite.WAProto import proto


DEFAULT_CACHE_TTLS = JSObject({
    'SIGNAL_STORE': 5 * 60,
    'MSG_RETRY': 60 * 60,
    'CALL_OFFER': 5 * 60,
    'USER_DEVICES': 5 * 60
})


def truthy(value):
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and value == value
    if isinstance(value, str):
        return len(value) > 0
    return True


def jsString(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def jsonStringify(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False, default=jsonDefault)


def toNumber(value):
    if value is None:
        return float('nan')
    if isinstance(value, str):
        return stringToNumber(value)
    if isinstance(value, bool):
        return int(value)
    return value


def messageOf(err):
    message = getattr(err, 'message', None)
    if message is None:
        return str(err)
    return str(message)


async def maybeAwait(value):
    if inspect.isawaitable(value):
        return await value
    return value


class AsyncLocalStorage:
    def __init__(self):
        self._var = contextvars.ContextVar('asyncLocalStorage', default=None)

    def getStore(self):
        return self._var.get()

    async def run(self, store, callback):
        token = self._var.set(store)
        try:
            return await maybeAwait(callback())
        finally:
            self._var.reset(token)


class PreKeyManager:
    def __init__(self, store, logger):
        self.store = store
        self.logger = logger
        self.queues = makeKeyedSerialTaskQueues()

    async def processOperations(self, data, keyType, transactionCache, mutations, isInTransaction):
        keyData = data[keyType]
        if keyData is None:
            return None

        async def task():
            if transactionCache[keyType] is None:
                transactionCache[keyType] = JSObject({})
            if mutations[keyType] is None:
                mutations[keyType] = JSObject({})

            deletions = []
            updates = JSObject({})
            for keyId in list(dict.keys(keyData)):
                if keyData[keyId] is None:
                    deletions.append(keyId)
                else:
                    updates[keyId] = keyData[keyId]
            if len(list(dict.keys(updates))) > 0:
                dict.update(transactionCache[keyType], updates)
                dict.update(mutations[keyType], updates)
            if len(deletions) > 0:
                await self.processDeletions(keyType, deletions, transactionCache, mutations, isInTransaction)
        return await self.queues.get('get')(keyType)['add'](task)

    async def processDeletions(self, keyType, ids, transactionCache, mutations, isInTransaction):
        if isInTransaction:
            for keyId in ids:
                if truthy(transactionCache[keyType][keyId] if transactionCache[keyType] is not None else None):
                    transactionCache[keyType][keyId] = None
                    mutations[keyType][keyId] = None
                else:
                    self.logger.warn(f'Skipping deletion of non-existent {keyType} in transaction: {keyId}')
        else:
            existingKeys = await self.store.get('get')(keyType, ids)
            for keyId in ids:
                if truthy(existingKeys[keyId]):
                    transactionCache[keyType][keyId] = None
                    mutations[keyType][keyId] = None
                else:
                    self.logger.warn(f'Skipping deletion of non-existent {keyType}: {keyId}')

    async def validateDeletions(self, data, keyType):
        keyData = data[keyType]
        if keyData is None:
            return None

        async def task():
            deletionIds = [keyId for keyId in list(dict.keys(keyData)) if keyData[keyId] is None]
            if len(deletionIds) == 0:
                return None
            existingKeys = await self.store.get('get')(keyType, deletionIds)
            for keyId in deletionIds:
                if not truthy(existingKeys[keyId]):
                    self.logger.warn(f'Skipping deletion of non-existent {keyType}: {keyId}')
                    del data[keyType][keyId]
            return None
        return await self.queues.get('get')(keyType)['add'](task)


txStorage = AsyncLocalStorage()


def makeCacheableSignalKeyStore(store, logger, _cache):
    cache = _cache if _cache is not None else Cache(JSObject({'max': 8192, 'ttl': DEFAULT_CACHE_TTLS['SIGNAL_STORE'] * 1000}))
    cacheMutex = makeMutex()

    def getUniqueId(type, id):
        return f'{type}.{id}'

    async def get(type, ids):
        async def task():
            data = JSObject({})
            idsToFetch = []
            for id in ids:
                item = cache.get(getUniqueId(type, id))
                if item is not None:
                    data[id] = item
                else:
                    idsToFetch.append(id)
            if len(idsToFetch):
                if logger is not None:
                    logger.trace(JSObject({'items': len(idsToFetch)}), 'loading from store')
                fetched = await store.get('get')(type, idsToFetch)
                for id in idsToFetch:
                    item = fetched[id]
                    if truthy(item):
                        data[id] = item
                        cache.set(getUniqueId(type, id), item)
            return data
        return await cacheMutex.get('mutex')(task)

    async def setStore(data):
        async def task():
            keys = 0
            for type in list(dict.keys(data)):
                for id in list(dict.keys(data[type])):
                    cache.set(getUniqueId(type, id), data[type][id])
                    keys += 1
            if logger is not None:
                logger.trace(JSObject({'keys': keys}), 'updated cache')
            await store.get('set')(data)
        return await cacheMutex.get('mutex')(task)

    async def clear():
        cache.clear()
        if store.get('clear') is not None:
            await maybeAwait(store.get('clear')())

    return JSObject({'get': get, 'set': setStore, 'clear': clear})


def addTransactionCapability(state, logger, options):
    maxCommitRetries = options.get('maxCommitRetries')
    delayBetweenTriesMs = options.get('delayBetweenTriesMs')
    keyQueues = {}
    txMutexes = {}
    txMutexRefCounts = {}
    preKeyManager = PreKeyManager(state, logger)

    def getQueue(key):
        if key not in keyQueues:
            keyQueues[key] = makeSerialTaskQueue()
        return keyQueues[key]

    def getTxMutex(key):
        if key not in txMutexes:
            txMutexes[key] = makeMutex()
            txMutexRefCounts[key] = 0
        return txMutexes[key]

    def acquireTxMutexRef(key):
        count = txMutexRefCounts.get(key, 0)
        txMutexRefCounts[key] = count + 1

    def releaseTxMutexRef(key):
        count = txMutexRefCounts.get(key, 1) - 1
        txMutexRefCounts[key] = count
        if count <= 0:
            txMutexes.pop(key, None)
            txMutexRefCounts.pop(key, None)

    def isInTransaction():
        return txStorage.getStore() is not None

    async def commitWithRetry(mutations):
        if len(list(dict.keys(mutations))) == 0:
            logger.trace('no mutations in transaction')
            return None
        logger.trace('committing transaction')
        for attempt in range(maxCommitRetries):
            try:
                await state.get('set')(mutations)
                logger.trace(JSObject({'mutationCount': len(list(dict.keys(mutations)))}), 'committed transaction')
                return None
            except Exception as error:
                retriesLeft = maxCommitRetries - attempt - 1
                logger.warn(f'failed to commit mutations, retries left={retriesLeft}')
                if retriesLeft == 0:
                    raise
                await delay(delayBetweenTriesMs)
        return None

    async def get(type, ids):
        ctx = txStorage.getStore()
        if ctx is None:
            return await state.get('get')(type, ids)

        cached = ctx.get('cache')[type] if ctx.get('cache')[type] is not None else JSObject({})
        missing = [i for i in ids if i not in cached]
        if len(missing) > 0:
            ctx['dbQueries'] += 1
            logger.trace(JSObject({'type': type, 'count': len(missing)}), 'fetching missing keys in transaction')
            fetched = await getTxMutex(type)['mutex'](lambda: state.get('get')(type, missing))
            if ctx.get('cache')[type] is None:
                ctx.get('cache')[type] = JSObject({})
            dict.update(ctx.get('cache')[type], fetched)
        result = JSObject({})
        for id in ids:
            cacheType = ctx.get('cache')[type]
            value = cacheType[id] if cacheType is not None else None
            if value is not None:
                result[id] = value
        return result

    async def setStore(data):
        ctx = txStorage.getStore()
        if ctx is None:
            types = list(dict.keys(data))
            for type in types:
                if type == 'pre-key':
                    await preKeyManager.validateDeletions(data, type)

            async def runQueued(type):
                async def task():
                    await state.get('set')(JSObject({type: data[type]}))
                return await getQueue(type)['add'](task)
            await asyncio.gather(*[runQueued(type) for type in types])
            return None

        logger.trace(JSObject({'types': list(dict.keys(data))}), 'caching in transaction')
        for key in list(dict.keys(data)):
            if ctx.get('cache')[key] is None:
                ctx.get('cache')[key] = JSObject({})
            if ctx.get('mutations')[key] is None:
                ctx.get('mutations')[key] = JSObject({})
            if key == 'pre-key':
                await preKeyManager.processOperations(data, key, ctx.get('cache'), ctx.get('mutations'), True)
            else:
                dict.update(ctx.get('cache')[key], data[key])
                dict.update(ctx.get('mutations')[key], data[key])
        return None

    async def transaction(work, key):
        existing = txStorage.getStore()
        if existing is not None:
            logger.trace('reusing existing transaction context')
            return await maybeAwait(work())
        mutex = getTxMutex(key)
        acquireTxMutexRef(key)
        try:
            async def task():
                ctx = JSObject({'cache': JSObject({}), 'mutations': JSObject({}), 'dbQueries': 0})
                logger.trace('entering transaction')
                try:
                    result = await txStorage.run(ctx, work)
                    await commitWithRetry(ctx.get('mutations'))
                    logger.trace(JSObject({'dbQueries': ctx.get('dbQueries')}), 'transaction completed')
                    return result
                except Exception as error:
                    logger.error(JSObject({'error': error}), 'transaction failed, rolling back')
                    raise
            return await mutex.get('mutex')(task)
        finally:
            releaseTxMutexRef(key)

    return JSObject({
        'get': get,
        'set': setStore,
        'isInTransaction': isInTransaction,
        'transaction': transaction
    })


def assertMeId(creds):
    me = creds.get('me')
    id = me.get('id') if me is not None else None
    if not id:
        raise Boom('Cannot proceed: socket is not authenticated yet (creds.me.id is missing)', JSObject({'statusCode': 401}))
    return id


def initAuthCreds():
    identityKey = Curve['generateKeyPair']()
    return JSObject({
        'noiseKey': Curve['generateKeyPair'](),
        'pairingEphemeralKeyPair': Curve['generateKeyPair'](),
        'signedIdentityKey': identityKey,
        'signedPreKey': signedKeyPair(identityKey, 1),
        'registrationId': generateRegistrationId(),
        'advSecretKey': base64.b64encode(randomBytes(32)).decode(),
        'processedHistoryMessages': [],
        'nextPreKeyId': 1,
        'firstUnuploadedPreKeyId': 1,
        'accountSyncCounter': 0,
        'accountSettings': JSObject({'unarchiveChats': False}),
        'registered': False,
        'pairingCode': None,
        'lastPropHash': None,
        'routingInfo': None,
        'additionalData': None
    })


def isStringNullOrEmptyLocal(value):
    return value is None or value == ''


async def handleIdentityChange(node, ctx):
    attrs = node.get('attrs')
    fromJid = attrs.get('from')
    if not fromJid:
        return JSObject({'action': 'invalid_notification'})

    identityNode = getBinaryNodeChild(node, 'identity')
    if identityNode is None:
        return JSObject({'action': 'no_identity_node'})

    ctx.get('logger').info(JSObject({'jid': fromJid}), 'identity changed')
    decoded = jidDecode(fromJid)
    if decoded is not None and truthy(decoded.get('device')) and decoded.get('device') != 0:
        ctx.get('logger').debug(JSObject({'jid': fromJid, 'device': decoded.get('device')}), 'ignoring identity change from companion device')
        return JSObject({'action': 'skipped_companion_device', 'device': decoded.get('device')})

    isSelfPrimary = ctx.get('meId') and (areJidsSameUser(fromJid, ctx.get('meId')) or (ctx.get('meLid') and areJidsSameUser(fromJid, ctx.get('meLid'))))
    if isSelfPrimary:
        ctx.get('logger').info(JSObject({'jid': fromJid}), 'self primary identity changed')
        return JSObject({'action': 'skipped_self_primary'})

    if ctx.get('debounceCache').get(fromJid):
        ctx.get('logger').debug(JSObject({'jid': fromJid}), 'skipping identity assert (debounced)')
        return JSObject({'action': 'debounced'})
    ctx.get('debounceCache').set(fromJid, True)

    isOfflineNotification = not isStringNullOrEmptyLocal(attrs.get('offline'))
    hasExistingSession = await maybeAwait(ctx.get('validateSession')(fromJid))
    if not hasExistingSession.get('exists'):
        ctx.get('logger').debug(JSObject({'jid': fromJid}), 'no old session, skipping session refresh')
        return JSObject({'action': 'skipped_no_session'})

    ctx.get('logger').debug(JSObject({'jid': fromJid}), 'old session exists, will refresh session')
    if isOfflineNotification:
        ctx.get('logger').debug(JSObject({'jid': fromJid}), 'skipping session refresh during offline processing')
        return JSObject({'action': 'skipped_offline'})

    if ctx.get('onBeforeSessionRefresh') is not None:
        ctx.get('onBeforeSessionRefresh')(fromJid)
    try:
        await maybeAwait(ctx.get('assertSessions')([fromJid], True))
        return JSObject({'action': 'session_refreshed'})
    except Exception as error:
        ctx.get('logger').warn(JSObject({'error': error, 'jid': fromJid}), 'failed to assert sessions after identity change')
        return JSObject({'action': 'session_refresh_failed', 'error': error})


async def getDecryptionJid(sender, repository):
    if isLidUser(sender) or isHostedLidUser(sender):
        return sender
    mapped = await maybeAwait(repository.get('lidMapping').get('getLIDForPN')(sender))
    return mapped if truthy(mapped) else sender


async def storeMappingFromEnvelope(stanza, sender, repository, decryptionJid, logger):
    senderAlt = extractAddressingContext(stanza)['senderAlt']
    if senderAlt and isLidUser(senderAlt) and isPnUser(sender) and decryptionJid == sender:
        try:
            await maybeAwait(repository.get('lidMapping').get('storeLIDPNMappings')([JSObject({'lid': senderAlt, 'pn': sender})]))
            await maybeAwait(repository.get('migrateSession')(sender, senderAlt))
            logger.debug(JSObject({'sender': sender, 'senderAlt': senderAlt}), 'Stored LID mapping from envelope')
        except Exception as error:
            logger.warn(JSObject({'sender': sender, 'senderAlt': senderAlt, 'error': error}), 'Failed to store LID mapping')


NO_MESSAGE_FOUND_ERROR_TEXT = 'Message absent from node'
MISSING_KEYS_ERROR_TEXT = 'Key used already or never filled'
ACCOUNT_RESTRICTED_TEXT = 'Your account has been restricted'

DECRYPTION_RETRY_CONFIG = JSObject({
    'maxRetries': 3,
    'baseDelayMs': 100,
    'sessionRecordErrors': ['No session record', 'SessionError: No session record']
})

NACK_REASONS = JSObject({
    'SenderReachoutTimelocked': 463,
    'ParsingError': 487,
    'UnrecognizedStanza': 488,
    'UnrecognizedStanzaClass': 489,
    'UnrecognizedStanzaType': 490,
    'InvalidProtobuf': 491,
    'InvalidHostedCompanionStanza': 493,
    'MissingMessageSecret': 495,
    'SignalErrorOldCounter': 496,
    'MessageDeletedOnPeer': 499,
    'UnhandledError': 500,
    'UnsupportedAdminRevoke': 550,
    'UnsupportedLIDGroup': 551,
    'DBOperationFailed': 552
})

SERVER_ERROR_CODES = JSObject({
    'MessageAccountRestriction': '463',
    'SmaxInvalid': '479'
})


def extractAddressingContext(stanza):
    attrs = stanza.get('attrs')
    senderAlt = None
    recipientAlt = None
    sender = attrs.get('participant') or attrs.get('from')
    addressingMode = attrs.get('addressing_mode') or ('lid' if sender is not None and sender.endswith('lid') else 'pn')
    if addressingMode == 'lid':
        senderAlt = attrs.get('participant_pn') or attrs.get('sender_pn') or attrs.get('peer_recipient_pn')
        recipientAlt = attrs.get('recipient_pn')
    else:
        senderAlt = attrs.get('participant_lid') or attrs.get('sender_lid') or attrs.get('peer_recipient_lid')
        recipientAlt = attrs.get('recipient_lid')
    return JSObject({'addressingMode': addressingMode, 'senderAlt': senderAlt, 'recipientAlt': recipientAlt})


def decodeMessageNode(stanza, meId, meLid):
    attrs = stanza.get('attrs')
    msgType = None
    chatId = None
    author = None
    fromMe = False
    msgId = attrs.get('id')
    fromJid = attrs.get('from')
    participant = attrs.get('participant')
    recipient = attrs.get('recipient')

    if not msgId:
        raise Boom('Invalid message stanza: missing id attribute', JSObject({'data': stanza}))
    if not fromJid:
        raise Boom('Invalid message stanza: missing from attribute', JSObject({'data': stanza}))

    addressingContext = extractAddressingContext(stanza)

    def isMe(jid):
        return areJidsSameUser(jid, meId)

    def isMeLid(jid):
        return areJidsSameUser(jid, meLid)

    if isPnUser(fromJid) or isLidUser(fromJid) or isHostedLidUser(fromJid) or isHostedPnUser(fromJid):
        if recipient and not isJidMetaAI(recipient):
            if not isMe(fromJid) and not isMeLid(fromJid):
                raise Boom('receipient present, but msg not from me', JSObject({'data': stanza}))
            if isMe(fromJid) or isMeLid(fromJid):
                fromMe = True
            chatId = recipient
        else:
            if isMe(fromJid) or isMeLid(fromJid):
                fromMe = True
            chatId = fromJid
        msgType = 'chat'
        author = fromJid
    elif isJidGroup(fromJid):
        if not participant:
            raise Boom('No participant in group message')
        if isMe(participant) or isMeLid(participant):
            fromMe = True
        msgType = 'group'
        author = participant
        chatId = fromJid
    elif isJidBroadcast(fromJid):
        if not participant:
            raise Boom('No participant in group message')
        isParticipantMe = isMe(participant)
        if isJidStatusBroadcast(fromJid):
            msgType = 'direct_peer_status' if isParticipantMe else 'other_status'
        else:
            msgType = 'peer_broadcast' if isParticipantMe else 'other_broadcast'
        fromMe = isParticipantMe
        chatId = fromJid
        author = participant
    elif isJidNewsletter(fromJid):
        msgType = 'newsletter'
        chatId = fromJid
        author = fromJid
        if isMe(fromJid) or isMeLid(fromJid):
            fromMe = True
    else:
        raise Boom('Unknown message type', JSObject({'data': stanza}))

    pushname = attrs.get('notify')
    serverExtra = JSObject({'server_id': attrs.get('server_id')}) if msgType == 'newsletter' and attrs.get('server_id') else JSObject({})
    key = JSObject({
        'remoteJid': chatId,
        'remoteJidAlt': addressingContext.get('senderAlt') if not isJidGroup(chatId) else None,
        'remoteJidUsername': (attrs.get('peer_recipient_username') or attrs.get('recipient_username')) if not isJidGroup(chatId) else None,
        'fromMe': fromMe,
        'id': msgId,
        'participant': participant,
        'participantAlt': addressingContext.get('senderAlt') if isJidGroup(chatId) else None,
        'participantUsername': attrs.get('participant_username') if attrs.get('participant') else None,
        'addressingMode': addressingContext.get('addressingMode'),
        **serverExtra
    })
    fullMessage = JSObject({
        'key': key,
        'category': attrs.get('category'),
        'messageTimestamp': toNumber(attrs.get('t')),
        'pushName': pushname,
        'broadcast': isJidBroadcast(fromJid)
    })
    if key.get('fromMe'):
        fullMessage['status'] = proto.WebMessageInfo.Status.SERVER_ACK

    return JSObject({'fullMessage': fullMessage, 'author': author, 'sender': author if msgType == 'chat' else chatId})


def isSessionRecordError(error):
    errorMessage = messageOf(error) if error is not None else ''
    return any(pattern in errorMessage for pattern in DECRYPTION_RETRY_CONFIG['sessionRecordErrors'])


def decryptMessageNode(stanza, meId, meLid, repository, logger):
    decoded = decodeMessageNode(stanza, meId, meLid)
    fullMessage = decoded.get('fullMessage')
    author = decoded.get('author')
    sender = decoded.get('sender')

    async def decrypt():
        decryptables = 0
        if isinstance(stanza.get('content'), list):
            for child in stanza.get('content'):
                tag = child.get('tag')
                attrs = child.get('attrs')
                content = child.get('content')
                if tag == 'verified_name' and isinstance(content, (bytes, bytearray)):
                    cert = proto.VerifiedNameCertificate.decode(content)
                    details = proto.VerifiedNameCertificate.Details.decode(cert.get('details'))
                    fullMessage['verifiedBizName'] = details.get('verifiedName')
                if tag == 'unavailable' and attrs.get('type') == 'view_once':
                    fullMessage.get('key')['isViewOnce'] = True
                if attrs.get('count') and tag == 'enc':
                    fullMessage['retryCount'] = toNumber(attrs.get('count'))
                if tag != 'enc' and tag != 'plaintext':
                    continue
                if not isinstance(content, (bytes, bytearray)):
                    continue

                decryptables += 1
                msgBuffer = None
                decryptionJid = await getDecryptionJid(author, repository)
                if tag != 'plaintext':
                    await storeMappingFromEnvelope(stanza, author, repository, decryptionJid, logger)
                try:
                    e2eType = 'plaintext' if tag == 'plaintext' else attrs.get('type')
                    if e2eType == 'skmsg':
                        msgBuffer = await maybeAwait(repository.get('decryptGroupMessage')(JSObject({'group': sender, 'authorJid': author, 'msg': content})))
                    elif e2eType == 'pkmsg' or e2eType == 'msg':
                        msgBuffer = await maybeAwait(repository.get('decryptMessage')(JSObject({'jid': decryptionJid, 'type': e2eType, 'ciphertext': content})))
                    elif e2eType == 'plaintext':
                        msgBuffer = content
                    else:
                        raise JSError(f'Unknown e2e type: {e2eType}')
                    msg = proto.Message.decode(unpadRandomMax16(msgBuffer) if e2eType != 'plaintext' else msgBuffer)
                    deviceSent = msg.get('deviceSentMessage')
                    if deviceSent is not None and deviceSent.get('message') is not None:
                        msg = deviceSent.get('message')
                    if msg.get('senderKeyDistributionMessage') is not None:
                        try:
                            await maybeAwait(repository.get('processSenderKeyDistributionMessage')(JSObject({'authorJid': author, 'item': msg.get('senderKeyDistributionMessage')})))
                        except Exception as err:
                            logger.error(JSObject({'key': fullMessage.get('key'), 'err': err}), 'failed to process sender key distribution message')
                    if fullMessage.get('message') is not None:
                        dict.update(fullMessage.get('message'), msg)
                    else:
                        fullMessage['message'] = msg
                except Exception as err:
                    logger.error(
                        JSObject({
                            'key': fullMessage.get('key'),
                            'err': err,
                            'messageType': 'plaintext' if tag == 'plaintext' else attrs.get('type'),
                            'sender': sender,
                            'author': author,
                            'isSessionRecordError': isSessionRecordError(err)
                        }),
                        'failed to decrypt message'
                    )
                    fullMessage['messageStubType'] = proto.WebMessageInfo.StubType.CIPHERTEXT
                    fullMessage['messageStubParameters'] = [messageOf(err)]
        if not decryptables and not fullMessage.get('key').get('isViewOnce'):
            fullMessage['messageStubType'] = proto.WebMessageInfo.StubType.CIPHERTEXT
            fullMessage['messageStubParameters'] = [NO_MESSAGE_FOUND_ERROR_TEXT]

    return JSObject({
        'fullMessage': fullMessage,
        'category': stanza.get('attrs').get('category'),
        'author': author,
        'decrypt': decrypt
    })


BOT_PHONE_REGEX = re.compile(r'1313555\d{4}|131655500\d{2}')


def isRegularUser(jid):
    if not jid:
        return False
    user = jid.split('@')[0]
    if user == '0':
        return False
    if BOT_PHONE_REGEX.fullmatch(user) is not None:
        return False
    if isJidMetaAI(jid):
        return False
    return bool(isPnUser(jid) or isLidUser(jid) or isHostedPnUser(jid) or isHostedLidUser(jid) or jid.endswith('@c.us'))


TC_TOKEN_BUCKET_DURATION = 604800
TC_TOKEN_NUM_BUCKETS = 4

TC_TOKEN_INDEX_KEY = '__index'


async def readTcTokenIndex(keys):
    data = await keys.get('get')('tctoken', [TC_TOKEN_INDEX_KEY])
    entry = data[TC_TOKEN_INDEX_KEY]
    token = entry.get('token') if entry is not None else None
    if token is None or len(token) == 0:
        return []
    try:
        parsed = json.loads(bytes(token).decode('utf-8'))
        if not isinstance(parsed, list):
            return []
        return [j for j in parsed if isinstance(j, str) and len(j) > 0 and j != TC_TOKEN_INDEX_KEY]
    except Exception:
        return []


async def buildMergedTcTokenIndexWrite(keys, addedJids):
    persisted = await readTcTokenIndex(keys)
    merged = dict.fromkeys(persisted)
    for jid in addedJids:
        if jid and jid != TC_TOKEN_INDEX_KEY:
            merged[jid] = None
    return JSObject({TC_TOKEN_INDEX_KEY: JSObject({'token': jsonStringify(list(merged)).encode('utf-8')})})


def isTcTokenExpired(timestamp):
    if timestamp is None:
        return True
    ts = jsParseInt(timestamp, 10) if isinstance(timestamp, str) else timestamp
    if ts is None:
        return True
    if isinstance(ts, float) and ts != ts:
        return True
    current = now() // 1000
    currentBucket = current // TC_TOKEN_BUCKET_DURATION
    cutoffBucket = currentBucket - (TC_TOKEN_NUM_BUCKETS - 1)
    return ts < cutoffBucket * TC_TOKEN_BUCKET_DURATION


def shouldSendNewTcToken(senderTimestamp):
    if senderTimestamp is None:
        return True
    current = now() // 1000
    currentBucket = current // TC_TOKEN_BUCKET_DURATION
    senderBucket = senderTimestamp // TC_TOKEN_BUCKET_DURATION
    return currentBucket > senderBucket


async def resolveTcTokenJid(jid, getLIDForPN):
    if isLidUser(jid):
        return jid
    lid = await maybeAwait(getLIDForPN(jid))
    return lid if lid is not None else jid


async def resolveIssuanceJid(jid, issueToLid, getLIDForPN, getPNForLID):
    if issueToLid:
        if isLidUser(jid):
            return jid
        lid = await maybeAwait(getLIDForPN(jid))
        return lid if lid is not None else jid
    if not isLidUser(jid):
        return jid
    if getPNForLID is not None:
        pn = await maybeAwait(getPNForLID(jid))
        return pn if pn is not None else jid
    return jid


async def buildTcTokenFromJid(args):
    authState = args.get('authState')
    jid = args.get('jid')
    baseContent = args.get('baseContent') if args.get('baseContent') is not None else []
    getLIDForPN = args.get('getLIDForPN')
    try:
        storageJid = await resolveTcTokenJid(jid, getLIDForPN)
        tcTokenData = await authState.get('keys').get('get')('tctoken', [storageJid])
        entry = tcTokenData[storageJid] if tcTokenData is not None else None
        tcTokenBuffer = entry.get('token') if entry is not None else None
        timestamp = entry.get('timestamp') if entry is not None else None
        if tcTokenBuffer is None or len(tcTokenBuffer) == 0 or timestamp is None or isTcTokenExpired(timestamp):
            if tcTokenBuffer is not None:
                cleared = JSObject({'token': b'', 'senderTimestamp': entry.get('senderTimestamp')}) if entry.get('senderTimestamp') is not None else None
                await authState.get('keys').get('set')(JSObject({'tctoken': JSObject({storageJid: cleared})}))
            return baseContent if len(baseContent) > 0 else None
        baseContent.append(JSObject({'tag': 'tctoken', 'attrs': JSObject({'t': jsString(timestamp)}), 'content': tcTokenBuffer}))
        return baseContent
    except Exception:
        return baseContent if len(baseContent) > 0 else None


async def storeTcTokensFromIqResult(args):
    result = args.get('result')
    fallbackJid = args.get('fallbackJid')
    keys = args.get('keys')
    getLIDForPN = args.get('getLIDForPN')
    onNewJidStored = args.get('onNewJidStored')
    tokensNode = getBinaryNodeChild(result, 'tokens')
    if tokensNode is None:
        return None
    tokenNodes = getBinaryNodeChildren(tokensNode, 'token')
    for tokenNode in tokenNodes:
        attrs = tokenNode.get('attrs')
        if attrs.get('type') != 'trusted_contact' or not isinstance(tokenNode.get('content'), (bytes, bytearray)):
            continue

        rawJid = jidNormalizedUser(fallbackJid or attrs.get('jid'))
        if not isRegularUser(rawJid):
            continue
        storageJid = await resolveTcTokenJid(rawJid, getLIDForPN)
        existingTcData = await keys.get('get')('tctoken', [storageJid])
        existingEntry = existingTcData[storageJid]
        existingTs = toNumber(existingEntry.get('timestamp')) if existingEntry is not None and truthy(existingEntry.get('timestamp')) else 0
        incomingTs = toNumber(attrs.get('t')) if truthy(attrs.get('t')) else 0
        if not truthy(incomingTs):
            continue
        if existingTs > 0 and existingTs > incomingTs:
            continue
        spread = existingEntry if existingEntry is not None else {}
        await keys.get('set')(JSObject({
            'tctoken': JSObject({storageJid: JSObject({**spread, 'token': bytes(tokenNode.get('content')), 'timestamp': attrs.get('t')})})
        }))
        if onNewJidStored is not None:
            onNewJidStored(storageJid)
    return None
