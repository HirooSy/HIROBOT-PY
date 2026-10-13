import asyncio
import base64
import datetime
import inspect
import json
from baileys_lite.foundation.node_runtime import jsonDefault
import math
import traceback

from baileys_lite.foundation.node_crypto import randomBytes
from baileys_lite.WAProto import proto
from baileys_lite.foundation.boom import Boom
from baileys_lite.foundation.cache import Cache
from baileys_lite.foundation.concurrency import makeMutex
from baileys_lite.foundation.node_runtime import JSObject, clearTimeout, nextTick, now, setImmediate, setTimeout
from baileys_lite.defaults import DEFAULT_CACHE_TTLS, KEY_BUNDLE_TYPE, MIN_PREKEY_COUNT, PLACEHOLDER_MAX_AGE_SECONDS, STATUS_EXPIRY_SECONDS
from baileys_lite.constants import ReachoutTimelockEnforcementType, WAMessageStatus, WAMessageStubType
from baileys_lite.utils.wa_protocol_core import (
    aesDecryptCTR,
    aesEncryptGCM,
    buildAckStanza,
    Curve,
    delay,
    derivePairingCodeKey,
    encodeBigEndian,
    encodeSignedDeviceIdentity,
    extractE2ESessionFromRetryReceipt,
    getCallStatusFromNode,
    getNextPreKeys,
    getStatusFromReceiptType,
    hkdf,
    toNumber,
    unixTimestampSeconds,
    xmppPreKey,
    xmppSignedPreKey,
)
from baileys_lite.utils.auth_state_core import (
    ACCOUNT_RESTRICTED_TEXT,
    buildMergedTcTokenIndexWrite,
    decodeMessageNode,
    decryptMessageNode,
    extractAddressingContext,
    handleIdentityChange,
    isTcTokenExpired,
    MISSING_KEYS_ERROR_TEXT,
    NACK_REASONS,
    NO_MESSAGE_FOUND_ERROR_TEXT,
    readTcTokenIndex,
    resolveIssuanceJid,
    resolveTcTokenJid,
    SERVER_ERROR_CODES,
    storeTcTokensFromIqResult,
    TC_TOKEN_INDEX_KEY,
)
from baileys_lite.utils.message_processing import cleanMessage, getHistoryMsg, makeOfflineNodeProcessor
from baileys_lite.utils.media import decodeMediaRetryNode
from baileys_lite.utils.message_compose import dateToMs
from baileys_lite.binary.wa_binary import (
    areJidsSameUser,
    binaryNodeToString,
    getAllBinaryNodeChildren,
    getBinaryNodeChild,
    getBinaryNodeChildBuffer,
    getBinaryNodeChildren,
    getBinaryNodeChildString,
    getBinaryNodeChildUInt,
    isJidGroup,
    isJidNewsletter,
    isJidStatusBroadcast,
    isLidUser,
    isPnUser,
    jidDecode,
    jidNormalizedUser,
    S_WHATSAPP_NET,
    jsNumber,
)
import baileys_lite.socket.groups_newsletter as groups_newsletter
import baileys_lite.socket.messages_send as messages_send

ACKED_BY_VOIP_KEY = 'baileys.ackedByVoip'

ENFORCEMENT_TYPE_VALUES = set(dict.values(ReachoutTimelockEnforcementType))


def _p(o, k):
    if o is None:
        return None
    if isinstance(o, dict):
        return o.get(k)
    try:
        return o[k]
    except Exception:
        return getattr(o, k, None)


def _m(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name)


async def _awaitValue(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _spawn(fn, *args):
    return asyncio.ensure_future(fn(*args))


def _asyncListener(fn):
    def listener(*args):
        result = fn(*args)
        if inspect.isawaitable(result):
            return asyncio.ensure_future(result)
        return result
    return listener


async def _cget(cache, key):
    return await _awaitValue(_m(cache, 'get')(key))


async def _cset(cache, key, value):
    return await _awaitValue(_m(cache, 'set')(key, value))


async def _cdel(cache, key):
    name = 'del' if isinstance(cache, dict) else 'del_'
    return await _awaitValue(_m(cache, name)(key))


def _hasMember(obj, name):
    if isinstance(obj, dict):
        return dict.get(obj, name) is not None
    return getattr(obj, name, None) is not None


def _toRequiredBuffer(data):
    if data is None:
        raise Boom('Invalid buffer', JSObject({'statusCode': 400}))
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode('utf-8')
    return bytes(data)


def _jsTruthy(value):
    if value is None or value is False:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value != ''
    if isinstance(value, (int, float)):
        return value == value and value != 0
    return True


def _nullish(a, b):
    return a if a is not None else b


def _or(a, b):
    return a if _jsTruthy(a) else b


def _bufToStr(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode('utf-8', errors='replace')
    if isinstance(value, str):
        return value
    return str(value)


def _parseInt(value):
    text = _bufToStr(value).strip()
    digits = ''
    for index, ch in enumerate(text):
        if ch.isdigit() or (index == 0 and ch in '+-'):
            digits += ch
        else:
            break
    try:
        return int(digits)
    except ValueError:
        return float('nan')


def _first(items):
    return items[0] if len(items) else None


def _jsonParse(text):
    return json.loads(text, object_hook=JSObject)


def _dateFromMs(ms):
    if ms is None or ms != ms:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)


def _secondsOfDate(date):
    if date is None:
        return float('nan')
    return unixTimestampSeconds(dateToMs(date))


def isValidEnforcementType(value):
    return isinstance(value, str) and value in ENFORCEMENT_TYPE_VALUES


def _isNodeAcked(node):
    return _jsTruthy(dict.get(node, ACKED_BY_VOIP_KEY))


def _utf8Bytes(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return str(value).encode('utf-8')


def _isFiniteNumber(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _undef(value):
    return 'undefined' if value is None else str(value)


def _retryKey(msgId, participant):
    return f'{_undef(msgId)}:{_undef(participant)}'


def _errMessage(err):
    return getattr(err, 'message', None)


def _jsonDropUndef(obj):
    return json.dumps({k: v for k, v in dict.items(obj) if v is not None}, separators=(',', ':'), default=jsonDefault)


def _participantJson(p):
    undefinedKeys = ('phoneNumber', 'lid', 'username')
    return json.dumps({k: v for k, v in dict.items(p) if not (k in undefinedKeys and v is None)}, separators=(',', ':'), default=jsonDefault)


async def _cgetOpt(cache, key):
    if cache is None:
        return None
    return await _cget(cache, key)


async def _csetOpt(cache, key, value):
    if cache is None:
        return None
    return await _cset(cache, key, value)


async def _cdelOpt(cache, key):
    if cache is None:
        return None
    return await _cdel(cache, key)


def makeMessagesRecvSocket(config):
    logger = config.get('logger')
    retryRequestDelayMs = config.get('retryRequestDelayMs')
    maxMsgRetryCount = config.get('maxMsgRetryCount')
    getMessage = config.get('getMessage')
    shouldIgnoreJid = config.get('shouldIgnoreJid')
    enableAutoSessionRecreation = config.get('enableAutoSessionRecreation')

    sock = messages_send.makeMessagesSocket(config)
    userDevicesCache = sock.get('userDevicesCache')
    devicesMutex = sock.get('devicesMutex')
    ev = sock.get('ev')
    authState = sock.get('authState')
    ws = sock.get('ws')
    messageMutex = sock.get('messageMutex')
    notificationMutex = sock.get('notificationMutex')
    receiptMutex = sock.get('receiptMutex')
    signalRepository = sock.get('signalRepository')
    query = sock.get('query')
    upsertMessage = sock.get('upsertMessage')
    resyncAppState = sock.get('resyncAppState')
    onUnexpectedError = sock.get('onUnexpectedError')
    assertSessions = sock.get('assertSessions')
    sendNode = sock.get('sendNode')
    relayMessage = sock.get('relayMessage')
    sendReceipt = sock.get('sendReceipt')
    uploadPreKeys = sock.get('uploadPreKeys')
    sendPeerDataOperationMessage = sock.get('sendPeerDataOperationMessage')
    messageRetryManager = sock.get('messageRetryManager')
    registerSocketEndHandler = sock.get('registerSocketEndHandler')
    issuePrivacyTokens = sock.get('issuePrivacyTokens')
    fetchAccountReachoutTimelock = sock.get('fetchAccountReachoutTimelock')
    placeholderResendCache = sock.get('placeholderResendCache')
    getLIDForPN = signalRepository.get('lidMapping').get('getLIDForPN')

    retryMutex = makeMutex()

    msgRetryCache = _or(config.get('msgRetryCounterCache'), Cache(JSObject({
        'max': 8192,
        'stdTTL': DEFAULT_CACHE_TTLS['MSG_RETRY'],
        'useClones': False
    })))
    callOfferCache = _or(config.get('callOfferCache'), Cache(JSObject({
        'max': 2048,
        'stdTTL': DEFAULT_CACHE_TTLS['CALL_OFFER'],
        'useClones': False
    })))

    identityAssertDebounce = Cache(JSObject({'stdTTL': 5, 'useClones': False}))
    sendActiveReceipts = [False]

    async def fetchMessageHistory(count, oldestMsgKey, oldestMsgTimestamp):
        if _p(_p(authState.get('creds'), 'me'), 'id') is None:
            raise Boom('Not authenticated')
        pdoMessage = JSObject({
            'historySyncOnDemandRequest': JSObject({
                'chatJid': oldestMsgKey.get('remoteJid'),
                'oldestMsgFromMe': oldestMsgKey.get('fromMe'),
                'oldestMsgId': oldestMsgKey.get('id'),
                'oldestMsgTimestampMs': oldestMsgTimestamp,
                'onDemandMsgCount': count
            }),
            'peerDataOperationRequestType': proto.Message.PeerDataOperationRequestType.HISTORY_SYNC_ON_DEMAND
        })
        return await _awaitValue(sendPeerDataOperationMessage(pdoMessage))

    async def requestPlaceholderResend(messageKey, msgData=None):
        if _p(_p(authState.get('creds'), 'me'), 'id') is None:
            raise Boom('Not authenticated')
        if await _cget(placeholderResendCache, _p(messageKey, 'id')):
            logger.debug(JSObject({'messageKey': messageKey}), 'already requested resend')
            return None
        await _cset(placeholderResendCache, _p(messageKey, 'id'), msgData if msgData is not None else True)
        await delay(2000)
        if not await _cget(placeholderResendCache, _p(messageKey, 'id')):
            logger.debug(JSObject({'messageKey': messageKey}), 'message received while resend requested')
            return 'RESOLVED'
        pdoMessage = JSObject({
            'placeholderMessageResendRequest': [JSObject({'messageKey': messageKey})],
            'peerDataOperationRequestType': proto.Message.PeerDataOperationRequestType.PLACEHOLDER_MESSAGE_RESEND
        })

        async def expirePlaceholder():
            if await _cget(placeholderResendCache, _p(messageKey, 'id')):
                logger.debug(JSObject({'messageKey': messageKey}), 'PDO message without response after 8 seconds. Phone possibly offline')
                await _cdel(placeholderResendCache, _p(messageKey, 'id'))

        setTimeout(lambda: _spawn(expirePlaceholder), 8000)
        return await _awaitValue(sendPeerDataOperationMessage(pdoMessage))

    def handleReachoutTimelockNotification(data):
        payload = _p(data, 'xwa2_notify_account_reachout_timelock')
        if payload is None:
            logger.warn('reachout timelock notification missing payload')
            return
        if not _jsTruthy(payload.get('is_active')):
            logger.info('reachout timelock restriction lifted')
            _m(ev, 'emit')('connection.update', JSObject({
                'reachoutTimeLock': JSObject({
                    'isActive': False,
                    'enforcementType': ReachoutTimelockEnforcementType['DEFAULT']
                })
            }))
            return

        timeEnforcementEnds = _dateFromMs(_parseInt(payload.get('time_enforcement_ends')) * 1000) if _jsTruthy(payload.get('time_enforcement_ends')) else _dateFromMs(now() + 60000)
        enforcementType = payload.get('enforcement_type') if isValidEnforcementType(payload.get('enforcement_type')) else ReachoutTimelockEnforcementType['DEFAULT']
        logger.info(JSObject({'enforcementType': enforcementType, 'timeEnforcementEnds': timeEnforcementEnds}), 'reachout timelock restriction set')
        _m(ev, 'emit')('connection.update', JSObject({
            'reachoutTimeLock': JSObject({
                'isActive': True,
                'timeEnforcementEnds': timeEnforcementEnds,
                'enforcementType': enforcementType
            })
        }))

    def handleMessageCappingNotification(data):
        payload = _p(data, 'xwa2_notify_new_chat_messages_capping_info_update')
        if payload is None:
            logger.warn('message capping notification missing payload')
            return
        logger.info(JSObject({'payload': payload}), 'received message capping update')
        _m(ev, 'emit')('message-capping.update', payload)

    async def handleLegacyMexNewsletterNotification(node):
        mexNode = getBinaryNodeChild(node, 'mex')
        hasMexContent = _p(mexNode, 'content') is not None and _jsTruthy(mexNode.get('content'))
        if hasMexContent:
            updateNode = None
        else:
            updateNode = getBinaryNodeChild(node, 'update')
            if updateNode is None:
                updateNode = _first(getAllBinaryNodeChildren(node))
        payloadNode = mexNode if hasMexContent else updateNode
        if not _jsTruthy(_p(payloadNode, 'content')):
            logger.warn(JSObject({'node': binaryNodeToString(node)}), 'invalid mex newsletter notification')
            return
        try:
            payloadContent = payloadNode.get('content')
            if isinstance(payloadContent, list):
                logger.warn(JSObject({'payloadNode': payloadNode}), 'invalid mex newsletter notification payload format')
                return
            if isinstance(payloadContent, str):
                contentBuf = payloadContent.encode('latin-1', errors='replace')
            else:
                contentBuf = bytes(payloadContent)
            data = _jsonParse(contentBuf.decode('utf-8', errors='replace'))
        except Exception as error:
            logger.error(JSObject({'error': error, 'node': binaryNodeToString(node)}), 'failed to parse mex newsletter notification')
            return
        operation = _nullish(_p(data, 'operation'), _p(_p(payloadNode, 'attrs'), 'op_name'))
        updates = _p(data, 'updates')
        if updates is None:
            linkedProfiles = _p(_p(data, 'data'), 'xwa2_notify_linked_profiles')
            if linkedProfiles is not None:
                updates = [linkedProfiles]
        if updates is None or not _jsTruthy(operation):
            logger.warn(JSObject({'data': data}), 'invalid mex newsletter notification content')
            return
        logger.info(JSObject({'operation': operation, 'updates': updates}), 'got mex newsletter notification')
        if operation == 'NotificationNewsletterUpdate':
            for update in updates:
                settings = _p(update, 'settings')
                if _jsTruthy(_p(update, 'jid')) and settings is not None and len(list(dict.keys(settings))) > 0:
                    _m(ev, 'emit')('newsletter-settings.update', JSObject({
                        'id': update.get('jid'),
                        'update': settings
                    }))
        elif operation == 'NotificationNewsletterAdminPromote':
            for update in updates:
                if _jsTruthy(_p(update, 'jid')) and _jsTruthy(_p(update, 'user')):
                    _m(ev, 'emit')('newsletter-participants.update', JSObject({
                        'id': update.get('jid'),
                        'author': node.get('attrs').get('from'),
                        'user': update.get('user'),
                        'new_role': 'ADMIN',
                        'action': 'promote'
                    }))
        elif operation == 'NotificationLinkedProfilesUpdates':
            for update in updates:
                lid = _p(update, 'jid')
                addedProfiles = _p(update, 'added_profiles')
                if not isinstance(addedProfiles, list):
                    addedProfiles = []
                mappings = []
                for profile in addedProfiles:
                    if isinstance(profile, str):
                        pn = profile
                    else:
                        pn = _nullish(_p(profile, 'pn'), _p(profile, 'jid'))
                    if _jsTruthy(lid) and _jsTruthy(pn):
                        mapping = JSObject({'lid': lid, 'pn': pn})
                        _m(ev, 'emit')('lid-mapping.update', mapping)
                        mappings.append(mapping)
                await _awaitValue(signalRepository.get('lidMapping').get('storeLIDPNMappings')(mappings))
        else:
            logger.info(JSObject({'operation': operation, 'data': data}), 'unhandled mex newsletter notification')

    async def handleMexNotification(node):
        updateNode = getBinaryNodeChild(node, 'update')
        if updateNode is not None:
            opName = _p(updateNode.get('attrs'), 'op_name') if updateNode.get('attrs') is not None else None
            if not _jsTruthy(opName):
                logger.warn(JSObject({'node': binaryNodeToString(node)}), 'mex notification missing op_name, fallback to legacy')
                await handleLegacyMexNewsletterNotification(node)
                return
            try:
                mexResponse = _jsonParse(_bufToStr(updateNode.get('content')))
            except Exception as error:
                logger.error(JSObject({'err': error, 'opName': opName}), 'failed to parse mex notification JSON')
                return
            errors = _p(mexResponse, 'errors')
            if errors is not None and len(errors):
                logger.warn(JSObject({'errors': errors, 'opName': opName}), 'mex notification has GQL errors')
                return
            data = _p(mexResponse, 'data')
            if data is None:
                logger.warn(JSObject({'opName': opName}), 'mex notification has null data')
                return
            logger.debug(JSObject({'opName': opName}), 'processing mex notification')
            if opName == 'NotificationUserReachoutTimelockUpdate':
                handleReachoutTimelockNotification(data)
            elif opName == 'MessageCappingInfoNotification':
                handleMessageCappingNotification(data)
            elif opName in (
                'NotificationNewsletterUpdate',
                'NotificationLinkedProfilesUpdates',
                'NotificationNewsletterAdminPromote',
                'NotificationNewsletterAdminDemote',
                'NotificationNewsletterUserSettingChange',
                'NotificationNewsletterJoin',
                'NotificationNewsletterLeave',
                'NotificationNewsletterStateChange',
                'NotificationNewsletterAdminMetadataUpdate',
                'NotificationNewsletterOwnerUpdate',
                'NotificationNewsletterAdminInviteRevoke',
                'NotificationNewsletterWamoSubStatusChange',
                'NotificationNewsletterBlockUser',
                'NotificationNewsletterPaidPartnership',
                'NotificationNewsletterMilestone',
                'NewsletterResponseStateUpdate'
            ):
                await handleLegacyMexNewsletterNotification(node)
            else:
                logger.debug(JSObject({'opName': opName}), 'unhandled mex notification')
            return
        await handleLegacyMexNewsletterNotification(node)

    async def handleNewsletterNotification(node):
        fromJid = node.get('attrs').get('from')
        children = getAllBinaryNodeChildren(node)
        author = node.get('attrs').get('participant')
        for child in children:
            logger.debug(JSObject({'from': fromJid, 'child': child}), 'got newsletter notification')
            tag = child.get('tag')
            if tag == 'reaction':
                reactionUpdate = JSObject({
                    'id': fromJid,
                    'server_id': child.get('attrs').get('message_id'),
                    'reaction': JSObject({
                        'code': getBinaryNodeChildString(child, 'reaction'),
                        'count': 1
                    })
                })
                _m(ev, 'emit')('newsletter.reaction', reactionUpdate)
            elif tag == 'view':
                text = _bufToStr(child.get('content')) if _jsTruthy(child.get('content')) else ''
                if text == '':
                    text = '0'
                viewUpdate = JSObject({
                    'id': fromJid,
                    'server_id': child.get('attrs').get('message_id'),
                    'count': _parseInt(text)
                })
                _m(ev, 'emit')('newsletter.view', viewUpdate)
            elif tag == 'participant':
                participantUpdate = JSObject({
                    'id': fromJid,
                    'author': author,
                    'user': child.get('attrs').get('jid'),
                    'action': child.get('attrs').get('action'),
                    'new_role': child.get('attrs').get('role')
                })
                _m(ev, 'emit')('newsletter-participants.update', participantUpdate)
            elif tag == 'update':
                settingsNode = getBinaryNodeChild(child, 'settings')
                if settingsNode is not None:
                    update = JSObject({})
                    nameNode = getBinaryNodeChild(settingsNode, 'name')
                    if _jsTruthy(_p(nameNode, 'content')):
                        update['name'] = _bufToStr(nameNode.get('content'))
                    descriptionNode = getBinaryNodeChild(settingsNode, 'description')
                    if _jsTruthy(_p(descriptionNode, 'content')):
                        update['description'] = _bufToStr(descriptionNode.get('content'))
                    _m(ev, 'emit')('newsletter-settings.update', JSObject({
                        'id': fromJid,
                        'update': update
                    }))
            elif tag == 'message':
                plaintextNode = getBinaryNodeChild(child, 'plaintext')
                if _jsTruthy(_p(plaintextNode, 'content')):
                    try:
                        plainContent = plaintextNode.get('content')
                        if isinstance(plainContent, str):
                            contentBuf = plainContent.encode('latin-1', errors='replace')
                        else:
                            contentBuf = bytes(plainContent)
                        messageProto = proto.Message.decode(contentBuf).toJSON()
                        fullMessage = proto.WebMessageInfo.fromObject(JSObject({
                            'key': JSObject({
                                'remoteJid': fromJid,
                                'id': _or(child.get('attrs').get('message_id'), child.get('attrs').get('server_id')),
                                'fromMe': False
                            }),
                            'message': messageProto,
                            'messageTimestamp': jsNumber(child.get('attrs').get('t'))
                        })).toJSON()
                        await _awaitValue(upsertMessage(fullMessage, 'append'))
                        logger.debug('Processed plaintext newsletter message')
                    except Exception as error:
                        logger.error(JSObject({'error': error}), 'Failed to decode plaintext newsletter message')
            else:
                logger.warn(JSObject({'node': node, 'child': child}), 'Unknown newsletter notification child')

    async def sendMessageAck(node, errorCode=None):
        stanza = buildAckStanza(node, errorCode, _p(_p(authState.get('creds'), 'me'), 'id'))
        logger.debug(JSObject({'recv': JSObject({'tag': node.get('tag'), 'attrs': node.get('attrs')}), 'sent': stanza.get('attrs')}), 'sent ack')
        await _awaitValue(sendNode(stanza))

    async def rejectCall(callId, callFrom):
        stanza = JSObject({
            'tag': 'call',
            'attrs': JSObject({
                'from': authState.get('creds').get('me').get('id'),
                'to': callFrom
            }),
            'content': [
                JSObject({
                    'tag': 'reject',
                    'attrs': JSObject({
                        'call-id': callId,
                        'call-creator': callFrom,
                        'count': '0'
                    }),
                    'content': None
                })
            ]
        })
        await _awaitValue(query(stanza))

    inFlightPreKeyLow = set()

    def trackTcTokenJid(jid):
        if _jsTruthy(jid) and jid != TC_TOKEN_INDEX_KEY and jid not in tcTokenKnownJids:
            tcTokenKnownJids.add(jid)
            scheduleTcTokenIndexSave()

    async def reissueTcTokenAfterIdentityChangeTask(fromJid):
        normalizedJid = jidNormalizedUser(fromJid)
        tcJid = await resolveTcTokenJid(normalizedJid, getLIDForPN)
        tcTokenData = await _awaitValue(authState.get('keys').get('get')('tctoken', [tcJid]))
        senderTs = _p(_p(tcTokenData, tcJid), 'senderTimestamp')
        if senderTs is None or isTcTokenExpired(senderTs):
            return
        logger.debug(JSObject({'jid': normalizedJid, 'senderTimestamp': senderTs}), 'identity changed, re-issuing tctoken')
        getPNForLID = signalRepository.get('lidMapping').get('getPNForLID')
        issueJid = await resolveIssuanceJid(normalizedJid, sock.get('serverProps').get('lidTrustedTokenIssueToLid'), getLIDForPN, getPNForLID)
        result = await _awaitValue(issuePrivacyTokens([issueJid], senderTs))
        await storeTcTokensFromIqResult(JSObject({
            'result': result,
            'fallbackJid': tcJid,
            'keys': authState.get('keys'),
            'getLIDForPN': getLIDForPN,
            'onNewJidStored': trackTcTokenJid
        }))

    async def reissueTcTokenAfterIdentityChangeGuarded(fromJid):
        try:
            await reissueTcTokenAfterIdentityChangeTask(fromJid)
        except Exception as err:
            logger.debug(JSObject({'jid': fromJid, 'err': _errMessage(err)}), 'failed to re-issue tctoken after identity change')

    def reissueTcTokenAfterIdentityChange(fromJid):
        asyncio.ensure_future(reissueTcTokenAfterIdentityChangeGuarded(fromJid))

    async def sendRetryRequest(node, forceIncludeKeys=False):
        decoded = decodeMessageNode(node, authState.get('creds').get('me').get('id'), _or(authState.get('creds').get('me').get('lid'), ''))
        msgKey = decoded.get('fullMessage').get('key')
        msgId = msgKey.get('id')
        if messageRetryManager is not None:
            if messageRetryManager.hasExceededMaxRetries(msgId):
                logger.debug(JSObject({'msgId': msgId}), 'reached retry limit with new retry manager, clearing')
                messageRetryManager.markRetryFailed(msgId)
                return
            retryCount = messageRetryManager.incrementRetryCount(msgId)
            key = _retryKey(msgId, msgKey.get('participant'))
            await _cset(msgRetryCache, key, retryCount)
        else:
            key = _retryKey(msgId, msgKey.get('participant'))
            current = await _cget(msgRetryCache, key)
            retryCount = current if _jsTruthy(current) else 0
            if retryCount >= maxMsgRetryCount:
                logger.debug(JSObject({'retryCount': retryCount, 'msgId': msgId}), 'reached retry limit, clearing')
                await _cdel(msgRetryCache, key)
                return
            retryCount += 1
            await _cset(msgRetryCache, key, retryCount)
        key = _retryKey(msgId, msgKey.get('participant'))
        current = await _cget(msgRetryCache, key)
        retryCount = current if _jsTruthy(current) else 1
        creds = authState.get('creds')
        account = creds.get('account')
        signedPreKey = creds.get('signedPreKey')
        identityKey = creds.get('signedIdentityKey')
        fromJid = node.get('attrs').get('from')

        shouldRecreateSession = False
        recreateReason = ''
        if enableAutoSessionRecreation and messageRetryManager is not None and retryCount > 1:
            try:
                sessionId = signalRepository.get('jidToSignalProtocolAddress')(fromJid)
                hasSession = await _awaitValue(signalRepository.get('validateSession')(fromJid))
                result = messageRetryManager.shouldRecreateSession(fromJid, hasSession.get('exists'))
                shouldRecreateSession = result.get('recreate')
                recreateReason = result.get('reason')
                if shouldRecreateSession:
                    logger.debug(JSObject({'fromJid': fromJid, 'retryCount': retryCount, 'reason': recreateReason}), 'recreating session for retry')
                    await _awaitValue(authState.get('keys').get('set')(JSObject({'session': JSObject({sessionId: None})})))
                    forceIncludeKeys = True
            except Exception as error:
                logger.warn(JSObject({'error': error, 'fromJid': fromJid}), 'failed to check session recreation')
        if retryCount <= 2:
            if messageRetryManager is not None:
                async def scheduledPhoneRequest():
                    try:
                        requestId = await requestPlaceholderResend(msgKey)
                        logger.debug(f'sendRetryRequest: requested placeholder resend ({_undef(requestId)}) for message {_undef(msgId)} (scheduled)')
                    except Exception as error:
                        logger.warn(JSObject({'error': error, 'msgId': msgId}), 'failed to send scheduled phone request')
                messageRetryManager.schedulePhoneRequest(msgId, scheduledPhoneRequest)
            else:
                requestId = await requestPlaceholderResend(msgKey)
                logger.debug(f'sendRetryRequest: requested placeholder resend for message {_undef(requestId)}')
        deviceIdentity = encodeSignedDeviceIdentity(account, True)

        async def transactionWork():
            receipt = JSObject({
                'tag': 'receipt',
                'attrs': JSObject({
                    'id': msgId,
                    'type': 'retry',
                    'to': node.get('attrs').get('from')
                }),
                'content': [
                    JSObject({
                        'tag': 'retry',
                        'attrs': JSObject({
                            'count': str(retryCount),
                            'id': node.get('attrs').get('id'),
                            't': node.get('attrs').get('t'),
                            'v': '1',
                            'error': '0'
                        })
                    }),
                    JSObject({
                        'tag': 'registration',
                        'attrs': JSObject({}),
                        'content': encodeBigEndian(authState.get('creds').get('registrationId'))
                    })
                ]
            })
            if _jsTruthy(node.get('attrs').get('recipient')):
                receipt.get('attrs')['recipient'] = node.get('attrs').get('recipient')
            if _jsTruthy(node.get('attrs').get('participant')):
                receipt.get('attrs')['participant'] = node.get('attrs').get('participant')
            if retryCount > 1 or forceIncludeKeys or shouldRecreateSession:
                result = await getNextPreKeys(authState, 1)
                preKeys = result.get('preKeys')
                keyId = _first(list(dict.keys(preKeys)))
                key = preKeys[keyId]
                receipt.get('content').append(JSObject({
                    'tag': 'keys',
                    'attrs': JSObject({}),
                    'content': [
                        JSObject({'tag': 'type', 'attrs': JSObject({}), 'content': KEY_BUNDLE_TYPE}),
                        JSObject({'tag': 'identity', 'attrs': JSObject({}), 'content': identityKey.get('public')}),
                        xmppPreKey(key, jsNumber(keyId)),
                        xmppSignedPreKey(signedPreKey),
                        JSObject({'tag': 'device-identity', 'attrs': JSObject({}), 'content': deviceIdentity})
                    ]
                }))
                _m(ev, 'emit')('creds.update', result.get('update'))
            await _awaitValue(sendNode(receipt))
            logger.info(JSObject({'msgAttrs': node.get('attrs'), 'retryCount': retryCount}), 'sent retry receipt')

        await _awaitValue(authState.get('keys').get('transaction')(transactionWork, _or(_p(_p(authState.get('creds'), 'me'), 'id'), 'sendRetryRequest')))

    async def handleEncryptNotification(node):
        fromJid = node.get('attrs').get('from')
        if fromJid == S_WHATSAPP_NET:
            stanzaId = node.get('attrs').get('id')
            if _jsTruthy(stanzaId) and stanzaId in inFlightPreKeyLow:
                return
            countChild = getBinaryNodeChild(node, 'count')
            count = jsNumber(countChild.get('attrs').get('value'))
            shouldUploadMorePreKeys = count < MIN_PREKEY_COUNT
            logger.debug(JSObject({'count': count, 'shouldUploadMorePreKeys': shouldUploadMorePreKeys}), 'recv pre-key count')
            if shouldUploadMorePreKeys:
                if _jsTruthy(stanzaId):
                    inFlightPreKeyLow.add(stanzaId)
                try:
                    await _awaitValue(uploadPreKeys())
                finally:
                    if _jsTruthy(stanzaId):
                        inFlightPreKeyLow.discard(stanzaId)
        else:
            me = authState.get('creds').get('me')
            result = await handleIdentityChange(node, JSObject({
                'meId': _p(me, 'id'),
                'meLid': _p(me, 'lid'),
                'validateSession': signalRepository.get('validateSession'),
                'assertSessions': assertSessions,
                'debounceCache': identityAssertDebounce,
                'logger': logger,
                'onBeforeSessionRefresh': reissueTcTokenAfterIdentityChange
            }))
            if result.get('action') == 'no_identity_node':
                logger.info(JSObject({'node': node}), 'unknown encrypt notification')

    def handleGroupNotification(fullNode, child, msg):
        actingParticipantLid = fullNode.get('attrs').get('participant')
        actingParticipantPn = fullNode.get('attrs').get('participant_pn')
        actingParticipantUsername = fullNode.get('attrs').get('participant_username')
        partChild = getBinaryNodeChild(child, 'participant')
        affectedParticipantLid = _or(_p(_p(partChild, 'attrs'), 'jid'), actingParticipantLid)
        affectedParticipantPn = _or(_p(_p(partChild, 'attrs'), 'phone_number'), actingParticipantPn)
        tag = _p(child, 'tag')
        if tag == 'create':
            metadata = groups_newsletter.extractGroupMetadata(child)
            msg['messageStubType'] = WAMessageStubType['GROUP_CREATE']
            msg['messageStubParameters'] = [metadata.get('subject')]
            msg['key'] = JSObject({'participant': metadata.get('owner'), 'participantAlt': metadata.get('ownerPn')})
            _m(ev, 'emit')('chats.upsert', [JSObject({
                'id': metadata.get('id'),
                'name': metadata.get('subject'),
                'conversationTimestamp': metadata.get('creation')
            })])
            _m(ev, 'emit')('groups.upsert', [JSObject({
                **metadata,
                'author': actingParticipantLid,
                'authorPn': actingParticipantPn,
                'authorUsername': actingParticipantUsername
            })])
        elif tag in ('ephemeral', 'not_ephemeral'):
            msg['message'] = JSObject({
                'protocolMessage': JSObject({
                    'type': proto.Message.ProtocolMessage.Type.EPHEMERAL_SETTING,
                    'ephemeralExpiration': jsNumber(_or(child.get('attrs').get('expiration'), 0))
                })
            })
        elif tag == 'modify':
            oldNumber = [p.get('attrs').get('jid') for p in getBinaryNodeChildren(child, 'participant')]
            msg['messageStubParameters'] = oldNumber
            msg['messageStubType'] = WAMessageStubType['GROUP_PARTICIPANT_CHANGE_NUMBER']
        elif tag in ('promote', 'demote', 'remove', 'add', 'leave'):
            stubType = f'GROUP_PARTICIPANT_{tag.upper()}'
            msg['messageStubType'] = WAMessageStubType[stubType]
            participants = []
            for participantNode in getBinaryNodeChildren(child, 'participant'):
                attrs = participantNode.get('attrs')
                participants.append(JSObject({
                    'id': attrs.get('jid'),
                    'phoneNumber': attrs.get('phone_number') if (isLidUser(attrs.get('jid')) and isPnUser(attrs.get('phone_number'))) else None,
                    'lid': attrs.get('lid') if (isPnUser(attrs.get('jid')) and isLidUser(attrs.get('lid'))) else None,
                    'username': _or(_or(attrs.get('participant_username'), attrs.get('username')), None),
                    'admin': _or(attrs.get('type'), None)
                }))
            if (len(participants) == 1 and
                    (areJidsSameUser(participants[0].get('id'), actingParticipantLid) or
                     areJidsSameUser(participants[0].get('id'), actingParticipantPn)) and
                    tag == 'remove'):
                msg['messageStubType'] = WAMessageStubType['GROUP_PARTICIPANT_LEAVE']
            msg['messageStubParameters'] = [_participantJson(p) for p in participants]
        elif tag == 'subject':
            msg['messageStubType'] = WAMessageStubType['GROUP_CHANGE_SUBJECT']
            msg['messageStubParameters'] = [child.get('attrs').get('subject')]
        elif tag == 'description':
            bodyNode = getBinaryNodeChild(child, 'body')
            bodyContent = _p(bodyNode, 'content')
            description = _bufToStr(bodyContent) if bodyContent is not None else None
            msg['messageStubType'] = WAMessageStubType['GROUP_CHANGE_DESCRIPTION']
            msg['messageStubParameters'] = [description] if _jsTruthy(description) else None
        elif tag in ('announcement', 'not_announcement'):
            msg['messageStubType'] = WAMessageStubType['GROUP_CHANGE_ANNOUNCE']
            msg['messageStubParameters'] = ['on' if tag == 'announcement' else 'off']
        elif tag in ('locked', 'unlocked'):
            msg['messageStubType'] = WAMessageStubType['GROUP_CHANGE_RESTRICT']
            msg['messageStubParameters'] = ['on' if tag == 'locked' else 'off']
        elif tag == 'invite':
            msg['messageStubType'] = WAMessageStubType['GROUP_CHANGE_INVITE_LINK']
            msg['messageStubParameters'] = [child.get('attrs').get('code')]
        elif tag == 'member_add_mode':
            addMode = child.get('content')
            if _jsTruthy(addMode):
                msg['messageStubType'] = WAMessageStubType['GROUP_MEMBER_ADD_MODE']
                msg['messageStubParameters'] = [_bufToStr(addMode)]
        elif tag == 'membership_approval_mode':
            approvalMode = getBinaryNodeChild(child, 'group_join')
            if approvalMode is not None:
                msg['messageStubType'] = WAMessageStubType['GROUP_MEMBERSHIP_JOIN_APPROVAL_MODE']
                msg['messageStubParameters'] = [approvalMode.get('attrs').get('state')]
        elif tag == 'created_membership_requests':
            msg['messageStubType'] = WAMessageStubType['GROUP_MEMBERSHIP_JOIN_APPROVAL_REQUEST_NON_ADMIN_ADD']
            msg['messageStubParameters'] = [
                _jsonDropUndef(JSObject({'lid': affectedParticipantLid, 'pn': affectedParticipantPn})),
                'created',
                child.get('attrs').get('request_method')
            ]
        elif tag == 'revoked_membership_requests':
            isDenied = areJidsSameUser(affectedParticipantLid, actingParticipantLid)
            msg['messageStubType'] = WAMessageStubType['GROUP_MEMBERSHIP_JOIN_APPROVAL_REQUEST_NON_ADMIN_ADD']
            msg['messageStubParameters'] = [
                _jsonDropUndef(JSObject({'lid': affectedParticipantLid, 'pn': affectedParticipantPn})),
                'revoked' if isDenied else 'rejected'
            ]

    async def handleDevicesNotification(node):
        children = getAllBinaryNodeChildren(node)
        child = _first(children)
        fromJid = jidNormalizedUser(node.get('attrs').get('from'))
        if child is None:
            logger.debug(JSObject({'from': fromJid}), 'devices notification missing child, skipping')
            return
        tag = child.get('tag')
        deviceHash = child.get('attrs').get('device_hash')
        devices = getBinaryNodeChildren(child, 'device')
        me = authState.get('creds').get('me')
        if areJidsSameUser(fromJid, me.get('id')) or areJidsSameUser(fromJid, me.get('lid')):
            deviceJids = [d.get('attrs').get('jid') for d in devices]
            logger.info(JSObject({'deviceJids': deviceJids}), 'got my own devices')
        if not len(devices):
            logger.debug(JSObject({'from': fromJid, 'tag': tag}), 'no devices in notification, skipping')
            return
        decoded = []
        for d in devices:
            jid = d.get('attrs').get('jid')
            if not _jsTruthy(jid):
                continue
            parts = jidDecode(jid)
            if parts is None:
                logger.debug(JSObject({'jid': jid}), 'failed to decode device jid, skipping')
                continue
            decoded.append(JSObject({'jid': jid, 'user': parts.get('user'), 'server': parts.get('server'), 'device': parts.get('device')}))
        if not len(decoded):
            return

        async def updateDeviceCache():
            byUser = {}
            for d in decoded:
                byUser.setdefault(d.get('user'), []).append(d)
            for user, entries in list(byUser.items()):
                if tag == 'update':
                    logger.debug(JSObject({'user': user}), f"{_undef(user)}'s device list updated, dropping cached devices")
                    await _cdelOpt(userDevicesCache, user)
                    continue
                if tag == 'remove':
                    await _awaitValue(signalRepository.get('deleteSession')([e.get('jid') for e in entries]))
                existingCache = await _cgetOpt(userDevicesCache, user)
                if not _jsTruthy(existingCache):
                    existingCache = []
                if not len(existingCache):
                    logger.debug(JSObject({'user': user, 'tag': tag}), 'device list not cached, deferring to USync refresh')
                    continue
                affected = set(e.get('device') for e in entries)
                if tag == 'add':
                    logger.info(JSObject({'deviceHash': deviceHash, 'count': len(entries)}), 'devices added')
                    updatedDevices = [d for d in existingCache if d.get('device') not in affected] + [
                        JSObject({'user': e.get('user'), 'server': e.get('server'), 'device': e.get('device')}) for e in entries
                    ]
                elif tag == 'remove':
                    logger.info(JSObject({'deviceHash': deviceHash, 'count': len(entries)}), 'devices removed')
                    updatedDevices = [d for d in existingCache if d.get('device') not in affected]
                else:
                    logger.debug(JSObject({'tag': tag}), 'Unknown device list change tag')
                    continue
                if len(updatedDevices) == 0:
                    await _cdelOpt(userDevicesCache, user)
                else:
                    await _csetOpt(userDevicesCache, user, updatedDevices)

        await devicesMutex.get('mutex')(updateDeviceCache)

    inFlight463Recoveries = set()
    lastTcTokenPruneTs = [0]
    tcTokenKnownJids = set()
    tcTokenIndexTimer = [None]
    tcTokenIndexLoadTask = [None]

    async def loadTcTokenIndex():
        try:
            jids = await _awaitValue(readTcTokenIndex(authState.get('keys')))
            for jid in jids:
                tcTokenKnownJids.add(jid)
            logger.debug(JSObject({'count': len(tcTokenKnownJids)}), 'loaded tctoken index')
        except Exception as err:
            logger.warn(JSObject({'err': _errMessage(err)}), 'failed to load tctoken index')

    def startTcTokenIndexLoad():
        if tcTokenIndexLoadTask[0] is None:
            tcTokenIndexLoadTask[0] = asyncio.ensure_future(loadTcTokenIndex())
        return tcTokenIndexLoadTask[0]

    try:
        startTcTokenIndexLoad()
    except RuntimeError:
        pass

    async def flushTcTokenIndex():
        if tcTokenIndexTimer[0] is not None:
            clearTimeout(tcTokenIndexTimer[0])
            tcTokenIndexTimer[0] = None
        write = await buildMergedTcTokenIndexWrite(authState.get('keys'), tcTokenKnownJids)
        return await _awaitValue(authState.get('keys').get('set')(JSObject({'tctoken': write})))

    def scheduleTcTokenIndexSave():
        if tcTokenIndexTimer[0] is not None:
            clearTimeout(tcTokenIndexTimer[0])

        async def fireTcTokenIndexSave():
            tcTokenIndexTimer[0] = None
            try:
                await flushTcTokenIndex()
            except Exception as err:
                logger.warn(JSObject({'err': _errMessage(err)}), 'failed to save tctoken index')

        tcTokenIndexTimer[0] = setTimeout(lambda: _spawn(fireTcTokenIndexSave), 5000)

    async def handlePrivacyTokenNotification(node):
        tokensNode = getBinaryNodeChild(node, 'tokens')
        if tokensNode is None:
            return
        fromJid = jidNormalizedUser(node.get('attrs').get('from'))
        senderLidAttr = node.get('attrs').get('sender_lid')
        senderLid = jidNormalizedUser(senderLidAttr) if (_jsTruthy(senderLidAttr) and isLidUser(jidNormalizedUser(senderLidAttr))) else None
        fallbackJid = senderLid if senderLid is not None else await resolveTcTokenJid(fromJid, getLIDForPN)
        logger.debug(JSObject({'from': fromJid, 'storageJid': fallbackJid}), 'processing privacy token notification')
        await storeTcTokensFromIqResult(JSObject({
            'result': node,
            'fallbackJid': fallbackJid,
            'keys': authState.get('keys'),
            'getLIDForPN': getLIDForPN,
            'onNewJidStored': trackTcTokenJid
        }))

    async def decipherLinkPublicKey(data):
        buffer = _toRequiredBuffer(data)
        salt = buffer[0:32]
        secretKey = await derivePairingCodeKey(authState.get('creds').get('pairingCode'), salt)
        iv = buffer[32:48]
        payload = buffer[48:80]
        return aesDecryptCTR(payload, secretKey, iv)

    async def processNotification(node):
        result = JSObject({})
        children = getAllBinaryNodeChildren(node)
        child = _first(children)
        nodeType = node.get('attrs').get('type')
        fromJid = jidNormalizedUser(node.get('attrs').get('from'))
        if nodeType == 'newsletter':
            await handleNewsletterNotification(node)
        elif nodeType == 'mex':
            await handleMexNotification(node)
        elif nodeType == 'w:gp2':
            handleGroupNotification(node, child, result)
        elif nodeType == 'mediaretry':
            event = decodeMediaRetryNode(node)
            _m(ev, 'emit')('messages.media-update', [event])
        elif nodeType == 'encrypt':
            await handleEncryptNotification(node)
        elif nodeType == 'devices':
            try:
                await handleDevicesNotification(node)
            except Exception as error:
                logger.error(JSObject({'error': error, 'node': node}), 'failed to handle devices notification')
        elif nodeType == 'server_sync':
            update = getBinaryNodeChild(node, 'collection')
            if update is not None:
                name = update.get('attrs').get('name')
                await _awaitValue(resyncAppState([name], False))
        elif nodeType == 'picture':
            setPicture = getBinaryNodeChild(node, 'set')
            delPicture = getBinaryNodeChild(node, 'delete')
            pictureNode = setPicture if setPicture is not None else delPicture
            pictureId = _or(_or(jidNormalizedUser(_p(_p(node, 'attrs'), 'from')), _p(_p(pictureNode, 'attrs'), 'hash')), '')
            _m(ev, 'emit')('contacts.update', [JSObject({
                'id': pictureId,
                'imgUrl': 'changed' if setPicture is not None else 'removed'
            })])
            if isJidGroup(fromJid):
                result['messageStubType'] = WAMessageStubType['GROUP_CHANGE_ICON']
                if setPicture is not None:
                    result['messageStubParameters'] = [setPicture.get('attrs').get('id')]
                result['participant'] = _p(_p(pictureNode, 'attrs'), 'author')
                prevKey = result.get('key') if result.get('key') is not None else JSObject({})
                result['key'] = JSObject({
                    **prevKey,
                    'participant': _p(_p(setPicture, 'attrs'), 'author')
                })
        elif nodeType == 'account_sync':
            if _p(child, 'tag') == 'disappearing_mode':
                newDuration = jsNumber(child.get('attrs').get('duration'))
                timestamp = jsNumber(child.get('attrs').get('t'))
                logger.info(JSObject({'newDuration': newDuration}), 'updated account disappearing mode')
                prevSettings = authState.get('creds').get('accountSettings')
                prevSettings = prevSettings if prevSettings is not None else JSObject({})
                _m(ev, 'emit')('creds.update', JSObject({
                    'accountSettings': JSObject({
                        **prevSettings,
                        'defaultDisappearingMode': JSObject({
                            'ephemeralExpiration': newDuration,
                            'ephemeralSettingTimestamp': timestamp
                        })
                    })
                }))
            elif _p(child, 'tag') == 'blocklist':
                blocklists = getBinaryNodeChildren(child, 'item')
                for item in blocklists:
                    attrs = item.get('attrs')
                    blocklist = [attrs.get('jid')]
                    blockType = 'add' if attrs.get('action') == 'block' else 'remove'
                    _m(ev, 'emit')('blocklist.update', JSObject({'blocklist': blocklist, 'type': blockType}))
        elif nodeType == 'link_code_companion_reg':
            linkCodeCompanionReg = getBinaryNodeChild(node, 'link_code_companion_reg')
            ref = _toRequiredBuffer(getBinaryNodeChildBuffer(linkCodeCompanionReg, 'link_code_pairing_ref'))
            primaryIdentityPublicKey = _toRequiredBuffer(getBinaryNodeChildBuffer(linkCodeCompanionReg, 'primary_identity_pub'))
            primaryEphemeralPublicKeyWrapped = _toRequiredBuffer(getBinaryNodeChildBuffer(linkCodeCompanionReg, 'link_code_pairing_wrapped_primary_ephemeral_pub'))
            codePairingPublicKey = await decipherLinkPublicKey(primaryEphemeralPublicKeyWrapped)
            creds = authState.get('creds')
            companionSharedKey = Curve['sharedKey'](creds.get('pairingEphemeralKeyPair').get('private'), codePairingPublicKey)
            random = randomBytes(32)
            linkCodeSalt = randomBytes(32)
            linkCodePairingExpanded = hkdf(companionSharedKey, 32, JSObject({
                'salt': linkCodeSalt,
                'info': 'link_code_pairing_key_bundle_encryption_key'
            }))
            encryptPayload = bytes(creds.get('signedIdentityKey').get('public')) + primaryIdentityPublicKey + random
            encryptIv = randomBytes(12)
            encrypted = aesEncryptGCM(encryptPayload, linkCodePairingExpanded, encryptIv, b'')
            encryptedPayload = linkCodeSalt + encryptIv + encrypted
            identitySharedKey = Curve['sharedKey'](creds.get('signedIdentityKey').get('private'), primaryIdentityPublicKey)
            identityPayload = companionSharedKey + identitySharedKey + random
            creds['advSecretKey'] = base64.b64encode(hkdf(identityPayload, 32, JSObject({'info': 'adv_secret'}))).decode()
            await _awaitValue(query(JSObject({
                'tag': 'iq',
                'attrs': JSObject({
                    'to': S_WHATSAPP_NET,
                    'type': 'set',
                    'id': sock.get('generateMessageTag')(),
                    'xmlns': 'md'
                }),
                'content': [
                    JSObject({
                        'tag': 'link_code_companion_reg',
                        'attrs': JSObject({
                            'jid': creds.get('me').get('id'),
                            'stage': 'companion_finish'
                        }),
                        'content': [
                            JSObject({'tag': 'link_code_pairing_wrapped_key_bundle', 'attrs': JSObject({}), 'content': encryptedPayload}),
                            JSObject({'tag': 'companion_identity_public', 'attrs': JSObject({}), 'content': creds.get('signedIdentityKey').get('public')}),
                            JSObject({'tag': 'link_code_pairing_ref', 'attrs': JSObject({}), 'content': ref})
                        ]
                    })
                ]
            })))
            creds['registered'] = True
            _m(ev, 'emit')('creds.update', creds)
        elif nodeType == 'privacy_token':
            await handlePrivacyTokenNotification(node)
        if len(list(dict.keys(result))):
            return result
        return None

    async def willSendMessageAgain(msgId, participant):
        key = _retryKey(msgId, participant)
        current = await _cget(msgRetryCache, key)
        retryCount = current if _jsTruthy(current) else 0
        return retryCount < maxMsgRetryCount

    async def updateSendMessageAgainCount(msgId, participant):
        key = _retryKey(msgId, participant)
        current = await _cget(msgRetryCache, key)
        newValue = (current if _jsTruthy(current) else 0) + 1
        await _cset(msgRetryCache, key, newValue)

    async def sendMessagesAgain(key, ids, retryNode, receiptNode):
        remoteJid = key.get('remoteJid')
        participant = _or(key.get('participant'), remoteJid)
        retryCount = _or(jsNumber(retryNode.get('attrs').get('count')), 1)
        msgId = ids[0]

        msgs = []
        for msgIdItem in ids:
            msg = None
            if messageRetryManager is not None:
                cachedMsg = messageRetryManager.getRecentMessage(remoteJid, msgIdItem)
                if cachedMsg is not None:
                    msg = cachedMsg.get('message')
                    logger.debug(JSObject({'jid': remoteJid, 'id': msgIdItem}), 'found message in retry cache')
                    messageRetryManager.markRetrySuccess(msgIdItem)
            if not _jsTruthy(msg):
                msg = await _awaitValue(getMessage(JSObject({**key, 'id': msgIdItem})))
                if _jsTruthy(msg):
                    logger.debug(JSObject({'jid': remoteJid, 'id': msgIdItem}), 'found message via getMessage')
                    if messageRetryManager is not None:
                        messageRetryManager.markRetrySuccess(msgIdItem)
            msgs.append(msg)

        sendToAll = not _jsTruthy(_p(jidDecode(participant), 'device'))
        sessionId = signalRepository.get('jidToSignalProtocolAddress')(participant)
        injectedFromBundle = False
        bundle = extractE2ESessionFromRetryReceipt(receiptNode)
        if bundle is not None:
            try:
                await _awaitValue(signalRepository.get('injectE2ESession')(JSObject({'jid': participant, 'session': bundle})))
                injectedFromBundle = True
                logger.debug(JSObject({'participant': participant, 'retryCount': retryCount}), 'injected session from retry receipt key bundle')
            except Exception as error:
                logger.warn(JSObject({'error': error, 'participant': participant}), 'failed to inject session from retry receipt')
        if not injectedFromBundle:
            receivedRegId = getBinaryNodeChildUInt(receiptNode, 'registration', 4)
            if isinstance(receivedRegId, int) and not isinstance(receivedRegId, bool):
                info = await _awaitValue(signalRepository.get('getSessionInfo')(participant))
                if info is not None and info.get('registrationId') != 0 and info.get('registrationId') != receivedRegId:
                    logger.info(JSObject({'participant': participant, 'stored': info.get('registrationId'), 'received': receivedRegId}), 'reg id mismatch on retry without bundle, deleting session')
                    await _awaitValue(authState.get('keys').get('set')(JSObject({'session': JSObject({sessionId: None})})))
        BASE_KEY_CHECK_RETRY = 2
        if _jsTruthy(msgId) and messageRetryManager is not None:
            info = await _awaitValue(signalRepository.get('getSessionInfo')(participant))
            if info is not None:
                if retryCount == BASE_KEY_CHECK_RETRY:
                    messageRetryManager.saveBaseKey(sessionId, msgId, info.get('baseKey'))
                elif retryCount > BASE_KEY_CHECK_RETRY:
                    if messageRetryManager.hasSameBaseKey(sessionId, msgId, info.get('baseKey')):
                        logger.warn(JSObject({'participant': participant, 'retryCount': retryCount}), 'base key collision on retry, forcing fresh session')
                        await _awaitValue(authState.get('keys').get('set')(JSObject({'session': JSObject({sessionId: None})})))
                    messageRetryManager.deleteBaseKey(sessionId, msgId)
        shouldRecreateSession = False
        recreateReason = ''
        if enableAutoSessionRecreation and messageRetryManager is not None and retryCount > 1 and not injectedFromBundle:
            try:
                hasSession = await _awaitValue(signalRepository.get('validateSession')(participant))
                result = messageRetryManager.shouldRecreateSession(participant, hasSession.get('exists'))
                shouldRecreateSession = result.get('recreate')
                recreateReason = result.get('reason')
                if shouldRecreateSession:
                    logger.debug(JSObject({'participant': participant, 'retryCount': retryCount, 'reason': recreateReason}), 'recreating session for outgoing retry')
                    await _awaitValue(authState.get('keys').get('set')(JSObject({'session': JSObject({sessionId: None})})))
            except Exception as error:
                logger.warn(JSObject({'error': error, 'participant': participant}), 'failed to check session recreation for outgoing retry')
        if not injectedFromBundle:
            await _awaitValue(assertSessions([participant], True))
        if isJidGroup(remoteJid):
            await _awaitValue(authState.get('keys').get('set')(JSObject({'sender-key-memory': JSObject({remoteJid: None})})))
        logger.debug(JSObject({'participant': participant, 'sendToAll': sendToAll, 'shouldRecreateSession': shouldRecreateSession, 'reason': recreateReason, 'injectedFromBundle': injectedFromBundle}), 'prepared session for retry resend')
        for i, msg in enumerate(msgs):
            if not _jsTruthy(ids[i]):
                continue
            if messageRetryManager is not None and messageRetryManager.isMessageInvalidated(remoteJid, ids[i]):
                logger.debug(JSObject({'jid': remoteJid, 'id': ids[i]}), 'skipping retry for edited or revoked message')
                continue
            if _jsTruthy(msg) and await willSendMessageAgain(ids[i], participant):
                await updateSendMessageAgainCount(ids[i], participant)
                msgRelayOpts = JSObject({'messageId': ids[i]})
                if sendToAll:
                    msgRelayOpts['useUserDevicesCache'] = False
                else:
                    msgRelayOpts['participant'] = JSObject({
                        'jid': participant,
                        'count': jsNumber(retryNode.get('attrs').get('count'))
                    })
                await _awaitValue(relayMessage(key.get('remoteJid'), msg, msgRelayOpts))
            else:
                logger.debug(JSObject({'jid': key.get('remoteJid'), 'id': ids[i]}), 'recv retry request, but message not available')

    async def handleReceipt(node):
        attrs = node.get('attrs')
        content = node.get('content')
        isLid = 'lid' in attrs.get('from')
        me = authState.get('creds').get('me')
        isNodeFromMe = areJidsSameUser(_or(attrs.get('participant'), attrs.get('from')), _p(me, 'lid') if isLid else _p(me, 'id'))
        remoteJid = attrs.get('from') if (not isNodeFromMe or isJidGroup(attrs.get('from'))) else attrs.get('recipient')
        fromMe = (not _jsTruthy(attrs.get('recipient'))) or ((attrs.get('type') == 'retry' or attrs.get('type') == 'sender') and isNodeFromMe)
        key = JSObject({
            'remoteJid': remoteJid,
            'id': '',
            'fromMe': fromMe,
            'participant': attrs.get('participant')
        })
        ids = [attrs.get('id')]
        if isinstance(content, list):
            items = getBinaryNodeChildren(content[0], 'item') if len(content) else []
            ids.extend(i.get('attrs').get('id') for i in items)

        async def receiptWork():
            status = getStatusFromReceiptType(attrs.get('type'))
            if status is not None and (status >= proto.WebMessageInfo.Status.SERVER_ACK or not isNodeFromMe):
                if isJidGroup(remoteJid) or isJidStatusBroadcast(remoteJid):
                    if _jsTruthy(attrs.get('participant')):
                        updateKey = 'receiptTimestamp' if status == proto.WebMessageInfo.Status.DELIVERY_ACK else 'readTimestamp'
                        _m(ev, 'emit')('message-receipt.update', [
                            JSObject({
                                'key': JSObject({**key, 'id': msgIdItem}),
                                'receipt': JSObject({
                                    'userJid': jidNormalizedUser(attrs.get('participant')),
                                    updateKey: jsNumber(attrs.get('t'))
                                })
                            }) for msgIdItem in ids
                        ])
                else:
                    _m(ev, 'emit')('messages.update', [
                        JSObject({
                            'key': JSObject({**key, 'id': msgIdItem}),
                            'update': JSObject({
                                'status': status,
                                'messageTimestamp': toNumber(jsNumber(_nullish(attrs.get('t'), 0)))
                            })
                        }) for msgIdItem in ids
                    ])
            if attrs.get('type') == 'retry':
                key['participant'] = _or(key.get('participant'), attrs.get('from'))
                retryNode = getBinaryNodeChild(node, 'retry')
                if _jsTruthy(ids[0]) and _jsTruthy(key.get('participant')) and await willSendMessageAgain(ids[0], key.get('participant')):
                    if key.get('fromMe'):
                        try:
                            await updateSendMessageAgainCount(ids[0], key.get('participant'))
                            logger.debug(JSObject({'attrs': attrs, 'key': key}), 'recv retry request')
                            await sendMessagesAgain(key, ids, retryNode, node)
                        except Exception as error:
                            logger.error(JSObject({'key': key, 'ids': ids, 'trace': traceback.format_exc()}), 'error in sending message again')
                    else:
                        logger.info(JSObject({'attrs': attrs, 'key': key}), 'recv retry for not fromMe message')
                else:
                    logger.info(JSObject({'attrs': attrs, 'key': key}), 'will not send message again, as sent too many times')

        try:
            await receiptMutex.get('mutex')(receiptWork)
        finally:
            if not _isNodeAcked(node):
                try:
                    await sendMessageAck(node)
                except Exception as ackErr:
                    logger.error(JSObject({'ackErr': ackErr}), 'failed to ack receipt')

    async def handleNotification(node):
        remoteJid = node.get('attrs').get('from')

        async def notificationWork():
            msg = await processNotification(node)
            if msg is not None:
                me = authState.get('creds').get('me')
                fromMe = areJidsSameUser(_or(node.get('attrs').get('participant'), remoteJid), me.get('id'))
                addressing = extractAddressingContext(node)
                prevKey = msg.get('key') if msg.get('key') is not None else {}
                msg['key'] = JSObject({
                    'remoteJid': remoteJid,
                    'fromMe': fromMe,
                    'participant': node.get('attrs').get('participant'),
                    'participantAlt': addressing.get('senderAlt'),
                    'participantUsername': node.get('attrs').get('participant_username'),
                    'addressingMode': addressing.get('addressingMode'),
                    'id': node.get('attrs').get('id'),
                    **prevKey
                })
                if msg.get('participant') is None:
                    msg['participant'] = node.get('attrs').get('participant')
                msg['messageTimestamp'] = jsNumber(node.get('attrs').get('t'))
                fullMsg = proto.WebMessageInfo.fromObject(msg)
                await _awaitValue(upsertMessage(fullMsg, 'append'))

        try:
            await notificationMutex.get('mutex')(notificationWork)
        finally:
            try:
                await sendMessageAck(node)
            except Exception as ackErr:
                logger.error(JSObject({'ackErr': ackErr}), 'failed to ack notification')

    async def handleMessage(node):
        encNode = getBinaryNodeChild(node, 'enc')
        if _p(_p(encNode, 'attrs'), 'type') == 'msmsg':
            logger.debug(JSObject({'key': node.get('attrs').get('key')}), 'ignored msmsg')
            await sendMessageAck(node, NACK_REASONS['MissingMessageSecret'])
            return
        acked = [False]
        try:
            me = authState.get('creds').get('me')
            decoded = decryptMessageNode(node, me.get('id'), _or(me.get('lid'), ''), signalRepository, logger)
            msg = decoded.get('fullMessage')
            category = decoded.get('category')
            author = decoded.get('author')
            decrypt = decoded.get('decrypt')
            alt = _or(msg.get('key').get('participantAlt'), msg.get('key').get('remoteJidAlt'))

            if _jsTruthy(alt):
                altServer = _p(jidDecode(alt), 'server')
                primaryJid = _or(msg.get('key').get('participant'), msg.get('key').get('remoteJid'))
                if altServer == 'lid':
                    if not await _awaitValue(signalRepository.get('lidMapping').get('getPNForLID')(alt)):
                        await _awaitValue(signalRepository.get('lidMapping').get('storeLIDPNMappings')([JSObject({'lid': alt, 'pn': primaryJid})]))
                        await _awaitValue(signalRepository.get('migrateSession')(primaryJid, alt))
                else:
                    await _awaitValue(signalRepository.get('lidMapping').get('storeLIDPNMappings')([JSObject({'lid': primaryJid, 'pn': alt})]))
                    await _awaitValue(signalRepository.get('migrateSession')(alt, primaryJid))

            async def messageWork():
                await _awaitValue(decrypt())
                if _jsTruthy(msg.get('key').get('remoteJid')) and _jsTruthy(msg.get('key').get('id')) and _jsTruthy(msg.get('message')) and messageRetryManager is not None:
                    messageRetryManager.addRecentMessage(msg.get('key').get('remoteJid'), msg.get('key').get('id'), msg.get('message'))

                if msg.get('messageStubType') == proto.WebMessageInfo.StubType.CIPHERTEXT and msg.get('category') != 'peer':
                    params = msg.get('messageStubParameters')
                    firstParam = params[0] if (params is not None and len(params)) else None
                    if firstParam == MISSING_KEYS_ERROR_TEXT:
                        acked[0] = True
                        await sendMessageAck(node, NACK_REASONS['ParsingError'])
                        return
                    if firstParam == NO_MESSAGE_FOUND_ERROR_TEXT:
                        unavailableNode = getBinaryNodeChild(node, 'unavailable')
                        unavailableType = _p(_p(unavailableNode, 'attrs'), 'type')
                        if unavailableType in ('bot_unavailable_fanout', 'hosted_unavailable_fanout', 'view_once_unavailable_fanout'):
                            logger.debug(JSObject({'msgId': msg.get('key').get('id'), 'unavailableType': unavailableType}), 'skipping placeholder resend for excluded unavailable type')
                            acked[0] = True
                            await sendMessageAck(node)
                            return
                        messageAge = unixTimestampSeconds() - toNumber(msg.get('messageTimestamp'))
                        if messageAge > PLACEHOLDER_MAX_AGE_SECONDS:
                            logger.debug(JSObject({'msgId': msg.get('key').get('id'), 'messageAge': messageAge}), 'skipping placeholder resend for old message')
                            acked[0] = True
                            await sendMessageAck(node)
                            return

                        cleanKey = JSObject({
                            'remoteJid': msg.get('key').get('remoteJid'),
                            'fromMe': msg.get('key').get('fromMe'),
                            'id': msg.get('key').get('id'),
                            'participant': msg.get('key').get('participant')
                        })
                        msgData = JSObject({
                            'key': msg.get('key'),
                            'messageTimestamp': msg.get('messageTimestamp'),
                            'pushName': msg.get('pushName'),
                            'participant': msg.get('participant'),
                            'verifiedBizName': msg.get('verifiedBizName')
                        })

                        async def resendPlaceholder():
                            try:
                                requestId = await requestPlaceholderResend(cleanKey, msgData)
                                if _jsTruthy(requestId) and requestId != 'RESOLVED':
                                    logger.debug(JSObject({'msgId': msg.get('key').get('id'), 'requestId': requestId}), 'requested placeholder resend for unavailable message')
                                    _m(ev, 'emit')('messages.update', [JSObject({
                                        'key': msg.get('key'),
                                        'update': JSObject({'messageStubParameters': [NO_MESSAGE_FOUND_ERROR_TEXT, requestId]})
                                    })])
                            except Exception as err:
                                logger.warn(JSObject({'err': err, 'msgId': msg.get('key').get('id')}), 'failed to request placeholder resend for unavailable message')

                        _spawn(resendPlaceholder)
                        acked[0] = True
                        await sendMessageAck(node)
                    else:
                        if isJidStatusBroadcast(msg.get('key').get('remoteJid')):
                            messageAge = unixTimestampSeconds() - toNumber(msg.get('messageTimestamp'))
                            if messageAge > STATUS_EXPIRY_SECONDS:
                                logger.debug(JSObject({'msgId': msg.get('key').get('id'), 'messageAge': messageAge, 'remoteJid': msg.get('key').get('remoteJid')}), 'skipping retry for expired status message')
                                acked[0] = True
                                await sendMessageAck(node)
                                return
                        logger.debug('[handleMessage] Attempting retry request for failed decryption')

                        async def retryWork():
                            try:
                                if not ws.isOpen:
                                    logger.debug(JSObject({'node': node}), 'Connection closed, skipping retry')
                                    return
                                encNodeNow = getBinaryNodeChild(node, 'enc')
                                await sendRetryRequest(node, encNodeNow is None)
                                if _jsTruthy(retryRequestDelayMs):
                                    await delay(retryRequestDelayMs)
                            except Exception as err:
                                logger.error(JSObject({'err': err}), 'Failed to send retry')
                            acked[0] = True
                            await sendMessageAck(node, NACK_REASONS['UnhandledError'])

                        await retryMutex.get('mutex')(retryWork)
                else:
                    if messageRetryManager is not None and _jsTruthy(msg.get('key').get('id')):
                        messageRetryManager.cancelPendingPhoneRequest(msg.get('key').get('id'))
                    isNewsletter = isJidNewsletter(msg.get('key').get('remoteJid'))
                    if not isNewsletter:
                        receiptType = None
                        participant = msg.get('key').get('participant')
                        if category == 'peer':
                            receiptType = 'peer_msg'
                        elif msg.get('key').get('fromMe'):
                            receiptType = 'sender'
                            if isLidUser(msg.get('key').get('remoteJid')) or isLidUser(msg.get('key').get('remoteJidAlt')):
                                participant = author
                        elif not sendActiveReceipts[0]:
                            receiptType = 'inactive'
                        acked[0] = True
                        await _awaitValue(sendReceipt(msg.get('key').get('remoteJid'), participant, [msg.get('key').get('id')], receiptType))

                        isAnyHistoryMsg = getHistoryMsg(msg.get('message'))
                        if isAnyHistoryMsg is not None:
                            jid = jidNormalizedUser(msg.get('key').get('remoteJid'))
                            await _awaitValue(sendReceipt(jid, None, [msg.get('key').get('id')], 'hist_sync'))
                    else:
                        acked[0] = True
                        await sendMessageAck(node)
                        logger.debug(JSObject({'key': msg.get('key')}), 'processed newsletter message without receipts')
                cleanMessage(msg, me.get('id'), me.get('lid'))
                await _awaitValue(upsertMessage(msg, 'append' if _jsTruthy(_p(node.get('attrs'), 'offline')) else 'notify'))

            await messageMutex.get('mutex')(messageWork)
        except Exception as error:
            logger.error(JSObject({'error': error, 'node': binaryNodeToString(node)}), 'error in handling message')
            if not acked[0]:
                try:
                    await sendMessageAck(node, NACK_REASONS['UnhandledError'])
                except Exception as ackErr:
                    logger.error(JSObject({'ackErr': ackErr}), 'failed to ack message after error')

    async def handleCall(node):
        try:
            attrs = node.get('attrs')
            infoChild = _first(getAllBinaryNodeChildren(node))
            if infoChild is None:
                raise Boom('Missing call info in call node')
            status = getCallStatusFromNode(infoChild)
            callId = infoChild.get('attrs').get('call-id')
            fromJid = _or(infoChild.get('attrs').get('from'), infoChild.get('attrs').get('call-creator'))
            call = JSObject({
                'chatId': attrs.get('from'),
                'from': fromJid,
                'callerPn': infoChild.get('attrs').get('caller_pn'),
                'id': callId,
                'date': _dateFromMs(jsNumber(attrs.get('t')) * 1000),
                'offline': _jsTruthy(attrs.get('offline')),
                'status': status
            })
            if status == 'relaylatency':
                latencyValue = _or(_or(infoChild.get('attrs').get('latency'), infoChild.get('attrs').get('latency_ms')), infoChild.get('attrs').get('latency-ms'))
                latencyMs = jsNumber(latencyValue) if _jsTruthy(latencyValue) else None
                if _isFiniteNumber(latencyMs):
                    call['latencyMs'] = latencyMs
            if status == 'offer':
                call['isVideo'] = getBinaryNodeChild(infoChild, 'video') is not None
                call['isGroup'] = infoChild.get('attrs').get('type') == 'group' or _jsTruthy(infoChild.get('attrs').get('group-jid'))
                call['groupJid'] = infoChild.get('attrs').get('group-jid')
                await _cset(callOfferCache, call.get('id'), call)

            existingCall = await _cget(callOfferCache, call.get('id'))
            if existingCall is not None:
                call['isVideo'] = existingCall.get('isVideo')
                call['isGroup'] = existingCall.get('isGroup')
                call['callerPn'] = _or(call.get('callerPn'), existingCall.get('callerPn'))

            if status in ('reject', 'accept', 'timeout', 'terminate'):
                await _cdel(callOfferCache, call.get('id'))
            _m(ev, 'emit')('call', [call])
        except Exception as error:
            logger.error(JSObject({'error': error, 'node': binaryNodeToString(node)}), 'error in handling call')
        finally:
            if not _isNodeAcked(node):
                try:
                    await sendMessageAck(node)
                except Exception as ackErr:
                    logger.error(JSObject({'ackErr': ackErr}), 'failed to ack call')

    async def recoverMessageAccountRestriction(ackFrom):
        try:
            getPNForLID = signalRepository.get('lidMapping').get('getPNForLID')
            tcStorageJid = await resolveTcTokenJid(ackFrom, getLIDForPN)
            issueJid = await resolveIssuanceJid(ackFrom, sock.get('serverProps').get('lidTrustedTokenIssueToLid'), getLIDForPN, getPNForLID)
            result = await _awaitValue(issuePrivacyTokens([issueJid], unixTimestampSeconds()))
            await storeTcTokensFromIqResult(JSObject({
                'result': result,
                'fallbackJid': tcStorageJid,
                'keys': authState.get('keys'),
                'getLIDForPN': getLIDForPN,
                'onNewJidStored': trackTcTokenJid
            }))
            logger.debug(JSObject({'from': ackFrom}), 'completed 463 token recovery issuance')
        except Exception as err:
            logger.debug(JSObject({'from': ackFrom, 'err': _errMessage(err)}), 'failed 463 token recovery issuance')
        finally:
            inFlight463Recoveries.discard(ackFrom)

    async def handleBadAck(node):
        attrs = node.get('attrs')
        key = JSObject({'remoteJid': attrs.get('from'), 'fromMe': True, 'id': attrs.get('id')})
        if _jsTruthy(attrs.get('error')):
            isReachoutTimelocked = attrs.get('error') == str(NACK_REASONS['SenderReachoutTimelocked'])
            if attrs.get('error') == SERVER_ERROR_CODES['MessageAccountRestriction']:
                logger.warn(JSObject({'msgId': attrs.get('id'), 'from': attrs.get('from')}), 'error 463: account restricted or missing tctoken for contact')
                ackFrom = attrs.get('from')
                if _jsTruthy(ackFrom) and ackFrom not in inFlight463Recoveries:
                    inFlight463Recoveries.add(ackFrom)
                    asyncio.ensure_future(recoverMessageAccountRestriction(ackFrom))
            elif attrs.get('error') == SERVER_ERROR_CODES['SmaxInvalid']:
                logger.warn(JSObject({'msgId': attrs.get('id'), 'from': attrs.get('from')}), 'smax-invalid (479): stanza rejected by server — likely stale device session or malformed addressing')
            elif isReachoutTimelocked:
                try:
                    await _awaitValue(fetchAccountReachoutTimelock())
                except Exception as err:
                    logger.warn(JSObject({'err': err}), 'failed to fetch reachout timelock')
                logger.warn(JSObject({'attrs': attrs}), 'received error in ack')
            else:
                logger.warn(JSObject({'attrs': attrs}), 'received error in ack')
            _m(ev, 'emit')('messages.update', [JSObject({
                'key': key,
                'update': JSObject({
                    'status': WAMessageStatus['ERROR'],
                    'messageStubParameters': [attrs.get('error'), ACCOUNT_RESTRICTED_TEXT] if isReachoutTimelocked else [attrs.get('error')]
                })
            })])

    async def yieldToEventLoop():
        await asyncio.sleep(0)

    async def processNodeWithBuffer(node, identifier, exec_):
        _m(ev, 'buffer')()

        async def execTask():
            try:
                return await _awaitValue(exec_(node))
            except Exception as err:
                return await _awaitValue(onUnexpectedError(err, identifier))

        await execTask()
        _m(ev, 'flush')()

    offlineNodeProcessor = makeOfflineNodeProcessor({
        'message': handleMessage,
        'call': handleCall,
        'receipt': handleReceipt,
        'notification': handleNotification
    }, JSObject({
        'isWsOpen': lambda: ws.isOpen,
        'onUnexpectedError': onUnexpectedError,
        'yieldToEventLoop': yieldToEventLoop
    }))

    async def processNode(nodeType, node, identifier, exec_):
        fromJid = node.get('attrs').get('from')
        ignoreJid = fromJid
        if nodeType == 'receipt' and _jsTruthy(fromJid):
            attrs = node.get('attrs')
            isLid = 'lid' in attrs.get('from')
            me = authState.get('creds').get('me')
            isNodeFromMe = areJidsSameUser(_or(attrs.get('participant'), attrs.get('from')), _p(me, 'lid') if isLid else _p(me, 'id'))
            ignoreJid = attrs.get('from') if (not isNodeFromMe or isJidGroup(attrs.get('from'))) else attrs.get('recipient')
        if _jsTruthy(ignoreJid) and ignoreJid != S_WHATSAPP_NET and shouldIgnoreJid(ignoreJid):
            await sendMessageAck(node, NACK_REASONS['UnhandledError'] if nodeType == 'message' else None)
            return
        isOffline = _jsTruthy(node.get('attrs').get('offline'))
        if isOffline:
            offlineNodeProcessor.get('enqueue')(nodeType, node)
        else:
            await processNodeWithBuffer(node, identifier, exec_)

    async def badAckGuarded(node):
        try:
            await handleBadAck(node)
        except Exception as error:
            await _awaitValue(onUnexpectedError(error, 'handling bad ack'))

    async def onCall(callsArray):
        call = _first(callsArray)
        if call is None:
            return
        if call.get('status') == 'timeout' or (call.get('status') == 'offer' and _jsTruthy(call.get('isGroup'))):
            msg = JSObject({
                'key': JSObject({
                    'remoteJid': call.get('chatId'),
                    'id': call.get('id'),
                    'fromMe': False
                }),
                'messageTimestamp': _secondsOfDate(call.get('date'))
            })
            if call.get('status') == 'timeout':
                if _jsTruthy(call.get('isGroup')):
                    msg['messageStubType'] = WAMessageStubType['CALL_MISSED_GROUP_VIDEO'] if _jsTruthy(call.get('isVideo')) else WAMessageStubType['CALL_MISSED_GROUP_VOICE']
                else:
                    msg['messageStubType'] = WAMessageStubType['CALL_MISSED_VIDEO'] if _jsTruthy(call.get('isVideo')) else WAMessageStubType['CALL_MISSED_VOICE']
            else:
                msg['message'] = JSObject({'call': JSObject({'callKey': _utf8Bytes(call.get('id'))})})
            protoMsg = proto.WebMessageInfo.fromObject(msg)
            await _awaitValue(upsertMessage(protoMsg, 'append' if call.get('offline') else 'notify'))

    def onConnectionUpdate(update):
        isOnline = _p(update, 'isOnline')
        connection = _p(update, 'connection')
        if isOnline is not None:
            sendActiveReceipts[0] = isOnline
            logger.trace('sendActiveReceipts set to "' + ('true' if _jsTruthy(sendActiveReceipts[0]) else 'false') + '"')

        if connection == 'close' and tcTokenIndexTimer[0] is not None:
            clearTimeout(tcTokenIndexTimer[0])
            tcTokenIndexTimer[0] = None
            try:
                asyncio.ensure_future(flushTcTokenIndexQuiet())
            except RuntimeError:
                pass

        if _jsTruthy(isOnline):
            current = now()
            DAY_MS = 24 * 60 * 60 * 1000
            if current - lastTcTokenPruneTs[0] >= DAY_MS:
                lastTcTokenPruneTs[0] = current
                asyncio.ensure_future(pruneExpiredTcTokens())

    async def flushTcTokenIndexQuiet():
        try:
            await flushTcTokenIndex()
        except Exception:
            pass

    def onSocketEnd(*args):
        if _p(config, 'msgRetryCounterCache') is None and _hasMember(msgRetryCache, 'close'):
            _m(msgRetryCache, 'close')()
        if _p(config, 'callOfferCache') is None and _hasMember(callOfferCache, 'close'):
            _m(callOfferCache, 'close')()
        identityAssertDebounce.close()
        sendActiveReceipts[0] = False

    async def pruneExpiredTcTokens():
        try:
            await _awaitValue(startTcTokenIndexLoad())

            persisted = await _awaitValue(readTcTokenIndex(authState.get('keys')))
            allJids = set(tcTokenKnownJids)
            for jid in persisted:
                allJids.add(jid)
            if not len(allJids):
                return
            jids = list(allJids)
            allTokens = await _awaitValue(authState.get('keys').get('get')('tctoken', jids))
            writes = JSObject({})
            survivors = {}
            mutated = 0
            for jid in jids:
                entry = allTokens[jid]
                if entry is None:
                    mutated += 1
                    continue
                hasPeerToken = entry.get('token') is not None and len(entry.get('token')) > 0
                peerTokenExpired = hasPeerToken and isTcTokenExpired(entry.get('timestamp'))
                hasSenderTs = entry.get('senderTimestamp') is not None
                senderTsExpired = hasSenderTs and isTcTokenExpired(entry.get('senderTimestamp'))
                keepPeerToken = hasPeerToken and not peerTokenExpired
                keepSenderTs = hasSenderTs and not senderTsExpired
                if not keepPeerToken and not keepSenderTs:
                    writes[jid] = None
                    mutated += 1
                elif peerTokenExpired and keepSenderTs:
                    writes[jid] = JSObject({'token': b'', 'senderTimestamp': entry.get('senderTimestamp')})
                    survivors[jid] = True
                    mutated += 1
                else:
                    survivors[jid] = True
            if mutated == 0:
                return
            survivorList = list(dict.keys(survivors))
            tctokenWrite = JSObject({**writes})
            tctokenWrite[TC_TOKEN_INDEX_KEY] = JSObject({
                'token': json.dumps(survivorList, separators=(',', ':'), default=jsonDefault).encode('utf-8')
            })
            await _awaitValue(authState.get('keys').get('set')(JSObject({'tctoken': tctokenWrite})))
            tcTokenKnownJids.clear()
            for jid in survivorList:
                tcTokenKnownJids.add(jid)
            logger.debug(JSObject({'mutated': mutated, 'remaining': len(survivorList)}), 'pruned expired tctokens')
        except Exception as err:
            logger.warn(JSObject({'err': _errMessage(err)}), 'failed to prune expired tctokens')

    ws.on('CB:message', _asyncListener(lambda node: processNode('message', node, 'processing message', handleMessage)))
    ws.on('CB:call', _asyncListener(lambda node: processNode('call', node, 'handling call', handleCall)))
    ws.on('CB:receipt', _asyncListener(lambda node: processNode('receipt', node, 'handling receipt', handleReceipt)))
    ws.on('CB:notification', _asyncListener(lambda node: processNode('notification', node, 'handling notification', handleNotification)))
    ws.on('CB:ack,class:message', lambda node: asyncio.ensure_future(badAckGuarded(node)))
    _m(ev, 'on')('call', _asyncListener(onCall))
    _m(ev, 'on')('connection.update', onConnectionUpdate)
    registerSocketEndHandler(onSocketEnd)

    return JSObject({
        **sock,
        'sendMessageAck': sendMessageAck,
        'sendRetryRequest': sendRetryRequest,
        'rejectCall': rejectCall,
        'fetchMessageHistory': fetchMessageHistory,
        'requestPlaceholderResend': requestPlaceholderResend,
        'messageRetryManager': messageRetryManager
    })
