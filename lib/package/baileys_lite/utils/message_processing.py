import asyncio
import base64
import inspect
import json
from baileys_lite.foundation.node_runtime import jsonDefault
import math
import os
import tempfile
import zlib

from baileys_lite.WAProto import proto
from baileys_lite.binary.wa_binary import (
    areJidsSameUser,
    getBinaryNodeChild,
    getBinaryNodeChildren,
    getBinaryNodeChildString,
    isHostedLidUser,
    isHostedPnUser,
    isJidBroadcast,
    isJidStatusBroadcast,
    isLidUser,
    isPnUser,
    jidDecode,
    jidEncode,
    jidNormalizedUser
)
from baileys_lite.constants import WAMessageStubType
from baileys_lite.foundation.boom import Boom
from baileys_lite.foundation.cache import Cache
from baileys_lite.foundation.long import jsParseInt, stringToNumber
from baileys_lite.foundation.node_crypto import createHash
from baileys_lite.foundation.node_runtime import JSError, JSObject, clearTimeout, now, setTimeout
from baileys_lite.utils.auth_state_core import buildMergedTcTokenIndexWrite, resolveTcTokenJid
from baileys_lite.utils.media import downloadContentFromMessage, getStream, getUrlFromDirectPath
from baileys_lite.utils.message_compose import getContentType, normalizeMessageContent
from baileys_lite.utils.wa_protocol_core import aesDecryptGCM, generateMessageIDV2, getKeyAuthor, hmacSign, toNumber


def _truthy(value):
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value != ''
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == value and value != 0
    return True


def _or(*values):
    result = None
    for value in values:
        result = value
        if _truthy(value):
            return value
    return result


def _plus(value):
    if isinstance(value, str):
        return stringToNumber(value)
    if value is None:
        return math.nan
    if isinstance(value, bool):
        return int(value)
    return value


def _numStr(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _bin(text):
    if isinstance(text, (bytes, bytearray)):
        return bytes(text)
    return text.encode('utf-8')


def _userOf(decoded):
    return decoded.get('user') if decoded is not None else None


def _stringifyKey(key):
    return f"{key.get('remoteJid')},{key.get('id')},{'1' if key.get('fromMe') else '0'}"


def _jsonStringify(value):
    def clean(item):
        if isinstance(item, dict):
            return {k: clean(v) for k, v in dict.items(item) if v is not None}
        if isinstance(item, list):
            return [clean(v) for v in item]
        return item
    return json.dumps(clean(value), separators=(',', ':'), ensure_ascii=False, default=jsonDefault)


def _jsonParse(text):
    return json.loads(text, object_hook=JSObject)


async def _maybeAwait(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _eachChunk(stream):
    if hasattr(stream, '__aiter__'):
        async for chunk in stream:
            yield chunk
    else:
        for chunk in stream:
            yield chunk


REAL_MSG_STUB_TYPES = {
    WAMessageStubType.CALL_MISSED_GROUP_VIDEO,
    WAMessageStubType.CALL_MISSED_GROUP_VOICE,
    WAMessageStubType.CALL_MISSED_VIDEO,
    WAMessageStubType.CALL_MISSED_VOICE
}
REAL_MSG_REQ_ME_STUB_TYPES = {WAMessageStubType.GROUP_PARTICIPANT_ADD}

RECENT_MESSAGES_SIZE = 1024
MESSAGE_KEY_SEPARATOR = '\x00'
RECREATE_SESSION_TIMEOUT = 60 * 60 * 1000
PHONE_REQUEST_DELAY = 3000

RetryReason = JSObject({
    'UnknownError': 0,
    'SignalErrorNoSession': 1,
    'SignalErrorInvalidKey': 2,
    'SignalErrorInvalidKeyId': 3,
    'SignalErrorInvalidMessage': 4,
    'SignalErrorInvalidSignature': 5,
    'SignalErrorFutureMessage': 6,
    'SignalErrorBadMac': 7,
    'SignalErrorInvalidSession': 8,
    'SignalErrorInvalidMsgKey': 9,
    'BadBroadcastEphemeralSetting': 10,
    'UnknownCompanionNoPrekey': 11,
    'AdvFailure': 12,
    'StatusRevokeDelay': 13
})

for _name, _value in list(dict.items(RetryReason)):
    RetryReason[_value] = _name

MAC_ERROR_CODES = {RetryReason['SignalErrorInvalidMessage'], RetryReason['SignalErrorBadMac']}


async def storeTcTokensFromHistorySync(chats, signalRepository, keyStore, logger):
    lidMapping = signalRepository.get('lidMapping')
    getLIDForPN = lidMapping.get('getLIDForPN')
    candidates = []
    for chat in chats:
        ts = toNumber(chat.get('tcTokenTimestamp')) if _truthy(chat.get('tcTokenTimestamp')) else 0
        if chat.get('tcToken') is not None and len(chat.get('tcToken')) > 0 and ts > 0:
            jid = jidNormalizedUser(chat.get('id'))
            storageJid = await _maybeAwait(resolveTcTokenJid(jid, getLIDForPN))
            senderTs = toNumber(chat.get('tcTokenSenderTimestamp')) if _truthy(chat.get('tcTokenSenderTimestamp')) else None
            candidates.append(JSObject({'storageJid': storageJid, 'token': bytes(chat.get('tcToken')), 'ts': ts, 'senderTs': senderTs}))
    if not candidates:
        return
    jids = [c.get('storageJid') for c in candidates]
    existing = await _maybeAwait(keyStore.get('get')('tctoken', jids))
    entries = JSObject({})
    for c in candidates:
        existingEntry = existing[c.get('storageJid')]
        tsValue = existingEntry.get('timestamp') if existingEntry is not None else None
        existingTs = _plus(tsValue) if _truthy(tsValue) else 0
        if existingTs > 0 and existingTs >= c.get('ts'):
            continue
        merged = JSObject({**(existingEntry if existingEntry is not None else {}), 'token': c.get('token'), 'timestamp': _numStr(c.get('ts'))})
        if c.get('senderTs') is not None:
            merged['senderTimestamp'] = c.get('senderTs')
        entries[c.get('storageJid')] = merged
    if len(dict.keys(entries)) > 0:
        if logger is not None:
            logger.debug(JSObject({'count': len(dict.keys(entries))}), 'storing tctokens from history sync')
        try:
            indexWrite = await _maybeAwait(buildMergedTcTokenIndexWrite(keyStore, list(dict.keys(entries))))
            await _maybeAwait(keyStore.get('set')(JSObject({'tctoken': JSObject({**entries, **indexWrite})})))
        except Exception as err:
            if logger is not None:
                logger.warn(JSObject({'err': err}), 'failed to store tctokens from history sync')


def cleanMessage(message, meId, meLid):
    key = message.get('key')
    if isHostedPnUser(key.get('remoteJid')) or isHostedLidUser(key.get('remoteJid')):
        key['remoteJid'] = jidEncode(_userOf(jidDecode(key.get('remoteJid'))), 's.whatsapp.net' if isHostedPnUser(key.get('remoteJid')) else 'lid')
    else:
        key['remoteJid'] = jidNormalizedUser(key.get('remoteJid'))
    if isHostedPnUser(key.get('participant')) or isHostedLidUser(key.get('participant')):
        key['participant'] = jidEncode(_userOf(jidDecode(key.get('participant'))), 's.whatsapp.net' if isHostedPnUser(key.get('participant')) else 'lid')
    else:
        key['participant'] = jidNormalizedUser(key.get('participant'))
    content = normalizeMessageContent(message.get('message'))
    if content is not None and content.get('reactionMessage') is not None:
        normaliseKey(content.get('reactionMessage').get('key'), message, meId, meLid)
    if content is not None and content.get('pollUpdateMessage') is not None:
        normaliseKey(content.get('pollUpdateMessage').get('pollCreationMessageKey'), message, meId, meLid)


def normaliseKey(msgKey, message, meId, meLid):
    key = message.get('key')
    if not key.get('fromMe'):
        if not msgKey.get('fromMe'):
            target = _or(msgKey.get('participant'), msgKey.get('remoteJid'))
            msgKey['fromMe'] = areJidsSameUser(target, meId) or areJidsSameUser(target, meLid)
        else:
            msgKey['fromMe'] = False
        msgKey['remoteJid'] = key.get('remoteJid')
        msgKey['participant'] = _or(msgKey.get('participant'), key.get('participant'))


def isRealMessage(message):
    normalizedContent = normalizeMessageContent(message.get('message'))
    hasSomeContent = _truthy(getContentType(normalizedContent))
    return (
        (normalizedContent is not None or message.get('messageStubType') in REAL_MSG_STUB_TYPES or message.get('messageStubType') in REAL_MSG_REQ_ME_STUB_TYPES)
        and hasSomeContent
        and not _truthy(normalizedContent.get('protocolMessage') if normalizedContent is not None else None)
        and not _truthy(normalizedContent.get('reactionMessage') if normalizedContent is not None else None)
        and not _truthy(normalizedContent.get('pollUpdateMessage') if normalizedContent is not None else None)
    )


def shouldIncrementChatUnread(message):
    return not message.get('key').get('fromMe') and not _truthy(message.get('messageStubType'))


def getChatId(key):
    remoteJid = key.get('remoteJid')
    participant = key.get('participant')
    fromMe = key.get('fromMe')
    if not _truthy(remoteJid):
        raise Boom('Cannot derive chat id: message key is missing remoteJid', JSObject({
            'data': JSObject({'remoteJid': remoteJid, 'participant': participant, 'fromMe': fromMe})
        }))
    if isJidBroadcast(remoteJid) and not isJidStatusBroadcast(remoteJid) and not fromMe:
        if not _truthy(participant):
            raise Boom('Cannot derive chat id: broadcast message key is missing participant', JSObject({
                'data': JSObject({'remoteJid': remoteJid, 'fromMe': fromMe})
            }))
        return participant
    return remoteJid


def decryptPollVote(encrypted, params):
    sign = _bin(params.get('pollMsgId')) + _bin(params.get('pollCreatorJid')) + _bin(params.get('voterJid')) + b'Poll Vote' + bytes([1])
    key0 = hmacSign(params.get('pollEncKey'), bytes(32), 'sha256')
    decKey = hmacSign(sign, key0, 'sha256')
    aad = _bin(f"{params.get('pollMsgId')}\x00{params.get('voterJid')}")
    decrypted = aesDecryptGCM(encrypted.get('encPayload'), decKey, encrypted.get('encIv'), aad)
    return proto.Message.PollVoteMessage.decode(decrypted)


def decryptEventResponse(encrypted, params):
    sign = _bin(params.get('eventMsgId')) + _bin(params.get('eventCreatorJid')) + _bin(params.get('responderJid')) + b'Event Response' + bytes([1])
    key0 = hmacSign(params.get('eventEncKey'), bytes(32), 'sha256')
    decKey = hmacSign(sign, key0, 'sha256')
    aad = _bin(f"{params.get('eventMsgId')}\x00{params.get('responderJid')}")
    decrypted = aesDecryptGCM(encrypted.get('encPayload'), decKey, encrypted.get('encIv'), aad)
    return proto.Message.EventResponseMessage.decode(decrypted)


def decryptMessageEdit(encrypted, params):
    sign = _bin(params.get('originalMsgId')) + _bin(params.get('originalSenderJid')) + _bin(params.get('editorJid')) + b'Message Edit' + bytes([1])
    key0 = hmacSign(params.get('editEncKey'), bytes(32), 'sha256')
    decKey = hmacSign(sign, key0, 'sha256')
    aad = b''
    decrypted = aesDecryptGCM(encrypted.get('encPayload'), decKey, encrypted.get('encIv'), aad)
    return proto.Message.decode(decrypted)


def buildEditUpdate(args):
    editedInner = decryptMessageEdit(
        JSObject({'encPayload': args.get('encPayload'), 'encIv': args.get('encIv')}),
        JSObject({'editEncKey': args.get('editEncKey'), 'originalSenderJid': args.get('originalSenderJid'), 'originalMsgId': args.get('originalMsgId'), 'editorJid': args.get('editorJid')})
    )
    editProtocol = editedInner.get('protocolMessage')
    innerEdited = editProtocol.get('editedMessage') if editProtocol is not None else None
    if innerEdited is None:
        if args.get('logger') is not None:
            args.get('logger').warn(JSObject({'targetKey': args.get('targetKey')}), 'decrypted MESSAGE_EDIT plaintext had no protocolMessage.editedMessage — skipping update')
        return None
    timestampMs = editProtocol.get('timestampMs') if editProtocol is not None else None
    return JSObject({
        'key': JSObject({**args.get('messageKey'), 'id': args.get('targetKeyId')}),
        'update': JSObject({
            'message': JSObject({'editedMessage': JSObject({'message': innerEdited})}),
            'messageTimestamp': math.floor(toNumber(timestampMs) / 1000) if _truthy(timestampMs) else args.get('fallbackTimestamp')
        })
    })


def _method(obj, name):
    if isinstance(obj, dict):
        return obj[name]
    return getattr(obj, name)


def _log(logger, level, *args):
    if logger is not None:
        _method(logger, level)(*args)


def _emit(ev, event, data):
    return _method(ev, 'emit')(event, data)


def _path(value, *names):
    current = value
    for name in names:
        if current is None:
            return None
        current = current[name] if isinstance(current, dict) else getattr(current, name, None)
    return current


def _idx(items, index):
    if items is None or len(items) <= index:
        return None
    return items[index]


def _tpl(value):
    return 'undefined' if value is None else _numStr(value)


async def processMessage(message, params):
    shouldProcessHistoryMsg = params.get('shouldProcessHistoryMsg')
    placeholderResendCache = params.get('placeholderResendCache')
    ev = params.get('ev')
    creds = params.get('creds')
    signalRepository = params.get('signalRepository')
    keyStore = params.get('keyStore')
    logger = params.get('logger')
    options = params.get('options')
    getMessage = params.get('getMessage')

    meId = creds.get('me').get('id')
    accountSettings = creds.get('accountSettings')
    chat = JSObject({'id': jidNormalizedUser(getChatId(message.get('key')))})
    isRealMsg = isRealMessage(message)
    if isRealMsg:
        chat['messages'] = [JSObject({'message': message})]
        chat['conversationTimestamp'] = toNumber(message.get('messageTimestamp'))
        if shouldIncrementChatUnread(message):
            chat['unreadCount'] = _or(chat.get('unreadCount'), 0) + 1

    content = normalizeMessageContent(message.get('message'))
    reactionFromMe = _path(content, 'reactionMessage', 'key', 'fromMe')
    if (isRealMsg or _truthy(reactionFromMe)) and _truthy(_path(accountSettings, 'unarchiveChats')):
        chat['archived'] = False
        chat['readOnly'] = False

    protocolMsg = content.get('protocolMessage') if content is not None else None
    if protocolMsg is not None:
        SELF_ONLY_TYPES = {
            proto.Message.ProtocolMessage.Type.HISTORY_SYNC_NOTIFICATION,
            proto.Message.ProtocolMessage.Type.APP_STATE_SYNC_KEY_SHARE,
            proto.Message.ProtocolMessage.Type.LID_MIGRATION_MAPPING_SYNC,
            proto.Message.ProtocolMessage.Type.PEER_DATA_OPERATION_REQUEST_RESPONSE_MESSAGE
        }
        protocolType = protocolMsg.get('type')
        if protocolType is not None and protocolType in SELF_ONLY_TYPES and not message.get('key').get('fromMe'):
            _log(logger, 'warn', JSObject({
                'msgId': message.get('key').get('id'),
                'type': protocolType,
                'from': _or(message.get('key').get('participant'), message.get('key').get('remoteJid'))
            }), 'dropping spoofed self-only protocolMessage from non-self origin')
            return

        ProtocolType = proto.Message.ProtocolMessage.Type
        if protocolType == ProtocolType.HISTORY_SYNC_NOTIFICATION:
            histNotification = protocolMsg.get('historySyncNotification')
            process = shouldProcessHistoryMsg
            isLatest = len(_or(creds.get('processedHistoryMessages'), [])) == 0
            _log(logger, 'info', JSObject({'histNotification': histNotification, 'process': process, 'id': message.get('key').get('id'), 'isLatest': isLatest}), 'got history notification')
            if process:
                if histNotification.get('syncType') != proto.HistorySync.HistorySyncType.ON_DEMAND:
                    _emit(ev, 'creds.update', JSObject({
                        'processedHistoryMessages': [*_or(creds.get('processedHistoryMessages'), []), JSObject({'key': message.get('key'), 'messageTimestamp': message.get('messageTimestamp')})]
                    }))
                data = await downloadAndProcessHistorySyncNotification(histNotification, options, logger)
                if len(_or(data.get('lidPnMappings'), [])) > 0:
                    _log(logger, 'debug', JSObject({'count': len(data.get('lidPnMappings'))}), 'processing LID-PN mappings from history sync')
                    try:
                        await _maybeAwait(_method(signalRepository.get('lidMapping'), 'storeLIDPNMappings')(data.get('lidPnMappings')))
                    except Exception as err:
                        _log(logger, 'warn', JSObject({'err': err}), 'failed to store LID-PN mappings from history sync')
                await storeTcTokensFromHistorySync(data.get('chats'), signalRepository, keyStore, logger)
                isOnDemand = histNotification.get('syncType') == proto.HistorySync.HistorySyncType.ON_DEMAND
                _emit(ev, 'messaging-history.set', JSObject({
                    **data,
                    'isLatest': None if isOnDemand else isLatest,
                    'chunkOrder': histNotification.get('chunkOrder'),
                    'peerDataRequestSessionId': histNotification.get('peerDataRequestSessionId')
                }))
        elif protocolType == ProtocolType.APP_STATE_SYNC_KEY_SHARE:
            keys = protocolMsg.get('appStateSyncKeyShare').get('keys')
            if keys is not None and len(keys) > 0:
                state = JSObject({'newAppStateSyncKeyId': ''})

                async def injectKeys(tx=None):
                    newKeys = []
                    for entry in keys:
                        strKeyId = base64.b64encode(bytes(entry.get('keyId').get('keyId'))).decode()
                        newKeys.append(strKeyId)
                        await _maybeAwait(keyStore.get('set')(JSObject({'app-state-sync-key': JSObject({strKeyId: entry.get('keyData')})})))
                        state['newAppStateSyncKeyId'] = strKeyId
                    _log(logger, 'info', JSObject({'newAppStateSyncKeyId': state.get('newAppStateSyncKeyId'), 'newKeys': newKeys}), 'injecting new app state sync keys')

                await _maybeAwait(keyStore.get('transaction')(injectKeys, meId))
                _emit(ev, 'creds.update', JSObject({'myAppStateKeyId': state.get('newAppStateSyncKeyId')}))
            else:
                _log(logger, 'info', JSObject({'protocolMsg': protocolMsg}), 'recv app state sync with 0 keys')
        elif protocolType == ProtocolType.REVOKE:
            _emit(ev, 'messages.update', [JSObject({
                'key': JSObject({**message.get('key'), 'id': protocolMsg.get('key').get('id')}),
                'update': JSObject({'message': None, 'messageStubType': WAMessageStubType.REVOKE, 'key': message.get('key')})
            })])
        elif protocolType == ProtocolType.EPHEMERAL_SETTING:
            chat['ephemeralSettingTimestamp'] = toNumber(message.get('messageTimestamp'))
            chat['ephemeralExpiration'] = _or(protocolMsg.get('ephemeralExpiration'), None)
        elif protocolType == ProtocolType.PEER_DATA_OPERATION_REQUEST_RESPONSE_MESSAGE:
            response = protocolMsg.get('peerDataOperationRequestResponseMessage')
            if response is not None:
                peerDataOperationResult = _or(response.get('peerDataOperationResult'), [])
                for result in peerDataOperationResult:
                    retryResponse = result.get('placeholderMessageResendResponse') if result is not None else None
                    if retryResponse is None or retryResponse.get('webMessageInfoBytes') is None:
                        continue
                    try:
                        webMessageInfo = proto.WebMessageInfo.decode(retryResponse.get('webMessageInfoBytes'))
                        msgId = _path(webMessageInfo, 'key', 'id')
                        cachedData = None
                        if _truthy(msgId) and placeholderResendCache is not None:
                            cachedData = await _maybeAwait(_method(placeholderResendCache, 'get')(msgId))
                        if _truthy(msgId) and placeholderResendCache is not None:
                            await _maybeAwait(_method(placeholderResendCache, 'del_')(msgId))
                        if cachedData is not None and isinstance(cachedData, dict):
                            cachedData['message'] = webMessageInfo.get('message')
                            if _truthy(webMessageInfo.get('messageTimestamp')):
                                cachedData['messageTimestamp'] = webMessageInfo.get('messageTimestamp')
                            finalMsg = cachedData
                        else:
                            finalMsg = webMessageInfo
                        _log(logger, 'debug', JSObject({'msgId': msgId, 'requestId': response.get('stanzaId')}), 'received placeholder resend')
                        _emit(ev, 'messages.upsert', JSObject({'messages': [finalMsg], 'type': 'notify', 'requestId': response.get('stanzaId')}))
                    except Exception as err:
                        _log(logger, 'warn', JSObject({'err': err, 'stanzaId': response.get('stanzaId')}), 'failed to decode placeholder resend response')
        elif protocolType == ProtocolType.MESSAGE_EDIT:
            _emit(ev, 'messages.update', [JSObject({
                'key': JSObject({**message.get('key'), 'id': _path(protocolMsg, 'key', 'id')}),
                'update': JSObject({
                    'message': JSObject({'editedMessage': JSObject({'message': protocolMsg.get('editedMessage')})}),
                    'messageTimestamp': math.floor(toNumber(protocolMsg.get('timestampMs')) / 1000) if _truthy(protocolMsg.get('timestampMs')) else message.get('messageTimestamp')
                })
            })])
        elif protocolType == ProtocolType.GROUP_MEMBER_LABEL_CHANGE:
            labelAssociationMsg = protocolMsg.get('memberLabel')
            if labelAssociationMsg is not None and _truthy(labelAssociationMsg.get('label')):
                _emit(ev, 'group.member-tag.update', JSObject({
                    'groupId': chat.get('id'),
                    'label': labelAssociationMsg.get('label'),
                    'participant': message.get('key').get('participant'),
                    'participantAlt': message.get('key').get('participantAlt'),
                    'messageTimestamp': _plus(message.get('messageTimestamp'))
                }))
        elif protocolType == ProtocolType.LID_MIGRATION_MAPPING_SYNC:
            mappingMessage = protocolMsg.get('lidMigrationMappingSyncMessage')
            encodedPayload = mappingMessage.get('encodedMappingPayload') if mappingMessage is not None else None
            payload = proto.LIDMigrationMappingSyncPayload.decode(encodedPayload)
            pnToLidMappings = payload.get('pnToLidMappings')
            chatDbMigrationTimestamp = payload.get('chatDbMigrationTimestamp')
            _log(logger, 'debug', JSObject({'pnToLidMappings': pnToLidMappings, 'chatDbMigrationTimestamp': chatDbMigrationTimestamp}), 'got lid mappings and chat db migration timestamp')
            pairs = []
            for mapping in pnToLidMappings:
                lid = _or(mapping.get('latestLid'), mapping.get('assignedLid'))
                pairs.append(JSObject({'lid': f"{_tpl(lid)}@lid", 'pn': f"{_tpl(mapping.get('pn'))}@s.whatsapp.net"}))
            await _maybeAwait(_method(signalRepository.get('lidMapping'), 'storeLIDPNMappings')(pairs))
            if len(pairs) > 0:
                for pair in pairs:
                    await _maybeAwait(_method(signalRepository, 'migrateSession')(pair.get('pn'), pair.get('lid')))
    elif content is not None and content.get('reactionMessage') is not None:
        reaction = JSObject({**content.get('reactionMessage'), 'key': message.get('key')})
        _emit(ev, 'messages.reaction', [JSObject({'reaction': reaction, 'key': content.get('reactionMessage').get('key')})])
    elif content is not None and content.get('encEventResponseMessage') is not None:
        encEventResponse = content.get('encEventResponseMessage')
        creationMsgKey = encEventResponse.get('eventCreationMessageKey')
        eventMsg = await _maybeAwait(getMessage(creationMsgKey))
        if eventMsg is not None:
            try:
                meIdNormalised = jidNormalizedUser(meId)
                eventCreatorKey = _or(creationMsgKey.get('participant'), creationMsgKey.get('remoteJid'))
                eventCreatorPn = (await _maybeAwait(_method(signalRepository.get('lidMapping'), 'getPNForLID')(eventCreatorKey))) if isLidUser(eventCreatorKey) else eventCreatorKey
                eventCreatorJid = getKeyAuthor(JSObject({'remoteJid': jidNormalizedUser(eventCreatorPn), 'fromMe': meIdNormalised == eventCreatorPn}), meIdNormalised)
                responderJid = getKeyAuthor(message.get('key'), meIdNormalised)
                eventEncKey = _path(eventMsg, 'messageContextInfo', 'messageSecret')
                if not _truthy(eventEncKey):
                    _log(logger, 'warn', JSObject({'creationMsgKey': creationMsgKey}), 'event response: missing messageSecret for decryption')
                else:
                    responseMsg = decryptEventResponse(encEventResponse, JSObject({
                        'eventEncKey': eventEncKey,
                        'eventCreatorJid': eventCreatorJid,
                        'eventMsgId': creationMsgKey.get('id'),
                        'responderJid': responderJid
                    }))
                    eventResponse = JSObject({'eventResponseMessageKey': message.get('key'), 'senderTimestampMs': responseMsg.get('timestampMs'), 'response': responseMsg})
                    _emit(ev, 'messages.update', [JSObject({'key': creationMsgKey, 'update': JSObject({'eventResponses': [eventResponse]})})])
            except Exception as err:
                _log(logger, 'warn', JSObject({'err': err, 'creationMsgKey': creationMsgKey}), 'failed to decrypt event response')
        else:
            _log(logger, 'warn', JSObject({'creationMsgKey': creationMsgKey}), 'event creation message not found, cannot decrypt response')
    elif content is not None and content.get('secretEncryptedMessage') is not None and content.get('secretEncryptedMessage').get('secretEncType') == proto.Message.SecretEncryptedMessage.SecretEncType.MESSAGE_EDIT:
        secEnc = content.get('secretEncryptedMessage')
        targetKey = secEnc.get('targetMessageKey')
        targetMsg = await _maybeAwait(getMessage(targetKey))
        if targetMsg is not None:
            try:
                meIdNormalised = jidNormalizedUser(meId)
                origSenderRaw = _or(targetKey.get('participant'), targetKey.get('remoteJid'))
                origSenderPn = (await _maybeAwait(_method(signalRepository.get('lidMapping'), 'getPNForLID')(origSenderRaw))) if isLidUser(origSenderRaw) else origSenderRaw
                originalSenderJid = getKeyAuthor(JSObject({'remoteJid': jidNormalizedUser(origSenderPn), 'fromMe': meIdNormalised == origSenderPn}), meIdNormalised)
                editorJid = getKeyAuthor(message.get('key'), meIdNormalised)
                editEncKey = _path(targetMsg, 'messageContextInfo', 'messageSecret')
                if not _truthy(editEncKey):
                    _log(logger, 'warn', JSObject({'targetKey': targetKey}), 'message edit: missing messageSecret on original message — cannot decrypt')
                    return
                update = buildEditUpdate(JSObject({
                    'editEncKey': editEncKey,
                    'encPayload': secEnc.get('encPayload'),
                    'encIv': secEnc.get('encIv'),
                    'originalSenderJid': originalSenderJid,
                    'originalMsgId': targetKey.get('id'),
                    'editorJid': editorJid,
                    'messageKey': message.get('key'),
                    'targetKeyId': targetKey.get('id'),
                    'fallbackTimestamp': toNumber(message.get('messageTimestamp')),
                    'logger': logger,
                    'targetKey': targetKey
                }))
                if update is not None:
                    _emit(ev, 'messages.update', [update])
            except Exception as err:
                _log(logger, 'warn', JSObject({'err': err, 'targetKey': targetKey}), 'failed to decrypt secretEncryptedMessage MESSAGE_EDIT')
        else:
            _log(logger, 'warn', JSObject({'targetKey': targetKey}), 'original message not found in store, cannot decrypt secretEncryptedMessage edit')
    elif message.get('messageStubType'):
        _processStubMessage(message, chat, ev, meId, logger)
    if len(dict.keys(chat)) > 1:
        _emit(ev, 'chats.update', [chat])


def _jsonList(values):
    return [_jsonParse(a) for a in values]


def _processStubMessage(message, chat, ev, meId, logger):
    jid = _path(message, 'key', 'remoteJid')
    state = JSObject({'participants': None})
    key = message.get('key')

    def emitParticipantsUpdate(action):
        _emit(ev, 'group-participants.update', JSObject({
            'id': jid,
            'author': key.get('participant'),
            'authorPn': key.get('participantAlt'),
            'authorUsername': key.get('participantUsername'),
            'participants': state.get('participants'),
            'action': action
        }))

    def emitGroupUpdate(update):
        _emit(ev, 'groups.update', [JSObject({
            'id': jid,
            **update,
            'author': key.get('participant'),
            'authorPn': key.get('participantAlt'),
            'authorUsername': key.get('participantUsername')
        })])

    def emitGroupRequestJoin(participant, action, method):
        _emit(ev, 'group.join-request', JSObject({
            'id': jid,
            'author': key.get('participant'),
            'authorPn': key.get('participantAlt'),
            'authorUsername': key.get('participantUsername'),
            'participant': participant.get('lid'),
            'participantPn': participant.get('pn'),
            'action': action,
            'method': method
        }))

    def participantsIncludesMe():
        return next((p for p in state.get('participants') if areJidsSameUser(meId, p.get('phoneNumber'))), None)

    parameters = message.get('messageStubParameters')
    stubType = message.get('messageStubType')
    S = WAMessageStubType

    if stubType == S.GROUP_PARTICIPANT_CHANGE_NUMBER:
        state['participants'] = _jsonList(parameters)
        emitParticipantsUpdate('modify')
    elif stubType in (S.GROUP_PARTICIPANT_LEAVE, S.GROUP_PARTICIPANT_REMOVE):
        state['participants'] = _jsonList(parameters)
        emitParticipantsUpdate('remove')
        if participantsIncludesMe() is not None:
            chat['readOnly'] = True
    elif stubType in (S.GROUP_PARTICIPANT_ADD, S.GROUP_PARTICIPANT_INVITE, S.GROUP_PARTICIPANT_ADD_REQUEST_JOIN):
        state['participants'] = _jsonList(parameters)
        if participantsIncludesMe() is not None:
            chat['readOnly'] = False
        emitParticipantsUpdate('add')
    elif stubType == S.GROUP_PARTICIPANT_DEMOTE:
        state['participants'] = _jsonList(parameters)
        emitParticipantsUpdate('demote')
    elif stubType == S.GROUP_PARTICIPANT_PROMOTE:
        state['participants'] = _jsonList(parameters)
        emitParticipantsUpdate('promote')
    elif stubType == S.GROUP_CHANGE_ANNOUNCE:
        value = _idx(parameters, 0)
        emitGroupUpdate(JSObject({'announce': value == 'true' or value == 'on'}))
    elif stubType == S.GROUP_CHANGE_RESTRICT:
        value = _idx(parameters, 0)
        emitGroupUpdate(JSObject({'restrict': value == 'true' or value == 'on'}))
    elif stubType == S.GROUP_CHANGE_SUBJECT:
        name = _idx(parameters, 0)
        chat['name'] = name
        emitGroupUpdate(JSObject({'subject': name}))
    elif stubType == S.GROUP_CHANGE_DESCRIPTION:
        description = _idx(parameters, 0)
        chat['description'] = description
        emitGroupUpdate(JSObject({'desc': description}))
    elif stubType == S.GROUP_CHANGE_INVITE_LINK:
        emitGroupUpdate(JSObject({'inviteCode': _idx(parameters, 0)}))
    elif stubType == S.GROUP_MEMBER_ADD_MODE:
        emitGroupUpdate(JSObject({'memberAddMode': _idx(parameters, 0) == 'all_member_add'}))
    elif stubType == S.GROUP_MEMBERSHIP_JOIN_APPROVAL_MODE:
        emitGroupUpdate(JSObject({'joinApprovalMode': _idx(parameters, 0) == 'on'}))
    elif stubType == S.GROUP_MEMBERSHIP_JOIN_APPROVAL_REQUEST_NON_ADMIN_ADD:
        participant = _jsonParse(_idx(parameters, 0))
        action = _idx(parameters, 1)
        method = _idx(parameters, 2)
        emitGroupRequestJoin(participant, action, method)


class MessageRetryManager:
    def __init__(self, logger, maxMsgRetryCount):
        self.logger = logger

        def dispose(_value, key):
            separatorIndex = key.rfind(MESSAGE_KEY_SEPARATOR)
            if separatorIndex > -1:
                self.messageKeyIndex.pop(key[separatorIndex + len(MESSAGE_KEY_SEPARATOR):], None)

        self.recentMessagesMap = Cache(JSObject({'max': RECENT_MESSAGES_SIZE, 'ttl': 5 * 60 * 1000, 'dispose': dispose}))
        self.messageKeyIndex = {}

        self.sessionRecreateHistory = Cache(JSObject({'max': 4096, 'ttl': RECREATE_SESSION_TIMEOUT * 2}))
        self.retryCounters = Cache(JSObject({'max': 4096, 'ttl': 15 * 60 * 1000, 'updateAgeOnGet': True}))
        self.baseKeys = Cache(JSObject({'max': 1024, 'ttl': 15 * 60 * 1000}))
        self.pendingPhoneRequests = {}
        self.maxMsgRetryCount = maxMsgRetryCount
        self.statistics = JSObject({'totalRetries': 0, 'successfulRetries': 0, 'failedRetries': 0, 'mediaRetries': 0, 'sessionRecreations': 0, 'phoneRequests': 0})

        self.invalidatedMessages = Cache(JSObject({'max': 512, 'ttl': 5 * 60 * 1000}))

    def invalidatedKey(self, to, id_):
        return self.keyToString(JSObject({'to': to, 'id': id_}))

    def invalidateMessage(self, to, id_):
        if not _truthy(to) or not _truthy(id_):
            return
        self.invalidatedMessages.set(self.invalidatedKey(to, id_), True)

    def isMessageInvalidated(self, to, id_):
        if not _truthy(to) or not _truthy(id_):
            return False
        return self.invalidatedMessages.get(self.invalidatedKey(to, id_)) is True

    def addRecentMessage(self, to, id_, message):
        keyStr = self.keyToString(JSObject({'to': to, 'id': id_}))
        self.recentMessagesMap.set(keyStr, JSObject({'message': message, 'timestamp': now()}))
        self.messageKeyIndex[id_] = keyStr
        _log(self.logger, 'debug', f'Added message to retry cache: {_tpl(to)}/{_tpl(id_)}')

    def getRecentMessage(self, to, id_):
        return self.recentMessagesMap.get(self.keyToString(JSObject({'to': to, 'id': id_})))

    def shouldRecreateSession(self, jid, hasSession, errorCode):
        if not hasSession:
            self.sessionRecreateHistory.set(jid, now())
            self.statistics['sessionRecreations'] += 1
            return JSObject({'reason': "we don't have a Signal session with them", 'recreate': True})
        if errorCode is not None and errorCode in MAC_ERROR_CODES:
            self.sessionRecreateHistory.set(jid, now())
            self.statistics['sessionRecreations'] += 1
            _log(self.logger, 'warn', JSObject({'jid': jid, 'errorCode': RetryReason[errorCode]}), 'MAC error detected, forcing immediate session recreation')
            return JSObject({'reason': f"MAC error (code {errorCode}: {RetryReason[errorCode]}), immediate session recreation", 'recreate': True})
        current = now()
        prevTime = self.sessionRecreateHistory.get(jid)
        if not _truthy(prevTime) or current - prevTime > RECREATE_SESSION_TIMEOUT:
            self.sessionRecreateHistory.set(jid, current)
            self.statistics['sessionRecreations'] += 1
            return JSObject({'reason': 'retry count > 1 and over an hour since last recreation', 'recreate': True})
        return JSObject({'reason': '', 'recreate': False})

    def parseRetryErrorCode(self, errorAttr):
        if errorAttr is None or errorAttr == '':
            return None
        code = jsParseInt(_tpl(errorAttr), 10)
        if code is None:
            return None
        if RetryReason['UnknownError'] <= code <= RetryReason['StatusRevokeDelay']:
            return code
        return RetryReason['UnknownError']

    def isMacError(self, errorCode):
        return errorCode is not None and errorCode in MAC_ERROR_CODES

    def incrementRetryCount(self, messageId):
        self.retryCounters.set(messageId, _or(self.retryCounters.get(messageId), 0) + 1)
        self.statistics['totalRetries'] += 1
        return self.retryCounters.get(messageId)

    def getRetryCount(self, messageId):
        return _or(self.retryCounters.get(messageId), 0)

    def hasExceededMaxRetries(self, messageId):
        return self.getRetryCount(messageId) >= self.maxMsgRetryCount

    def markRetrySuccess(self, messageId):
        self.statistics['successfulRetries'] += 1
        self.retryCounters.delete(messageId)
        self.cancelPendingPhoneRequest(messageId)
        self.removeRecentMessage(messageId)

    def markRetryFailed(self, messageId):
        self.statistics['failedRetries'] += 1
        self.retryCounters.delete(messageId)
        self.cancelPendingPhoneRequest(messageId)
        self.removeRecentMessage(messageId)

    def schedulePhoneRequest(self, messageId, callback, delay_=PHONE_REQUEST_DELAY):
        self.cancelPendingPhoneRequest(messageId)

        def fire():
            self.pendingPhoneRequests.pop(messageId, None)
            self.statistics['phoneRequests'] += 1
            _fireAndForget(callback())

        self.pendingPhoneRequests[messageId] = setTimeout(fire, delay_)
        _log(self.logger, 'debug', f'Scheduled phone request for message {_tpl(messageId)} with {delay_}ms delay')

    def cancelPendingPhoneRequest(self, messageId):
        timeout = self.pendingPhoneRequests.get(messageId)
        if timeout is not None:
            clearTimeout(timeout)
            self.pendingPhoneRequests.pop(messageId, None)
            _log(self.logger, 'debug', f'Cancelled pending phone request for message {_tpl(messageId)}')

    def clear(self):
        self.recentMessagesMap.clear()
        self.messageKeyIndex.clear()
        self.sessionRecreateHistory.clear()
        self.retryCounters.clear()
        self.baseKeys.clear()
        self.invalidatedMessages.clear()
        for messageId in list(self.pendingPhoneRequests.keys()):
            self.cancelPendingPhoneRequest(messageId)
        self.statistics = JSObject({'totalRetries': 0, 'successfulRetries': 0, 'failedRetries': 0, 'mediaRetries': 0, 'sessionRecreations': 0, 'phoneRequests': 0})

    def saveBaseKey(self, addr, msgId, baseKey):
        self.baseKeys.set(f'{_tpl(addr)}:{_tpl(msgId)}', baseKey)

    def hasSameBaseKey(self, addr, msgId, baseKey):
        stored = self.baseKeys.get(f'{_tpl(addr)}:{_tpl(msgId)}')
        if stored is None or len(stored) != len(baseKey):
            return False
        return bytes(stored) == bytes(baseKey)

    def deleteBaseKey(self, addr, msgId):
        self.baseKeys.delete(f'{_tpl(addr)}:{_tpl(msgId)}')

    def keyToString(self, key):
        return f"{_tpl(key.get('to'))}{MESSAGE_KEY_SEPARATOR}{_tpl(key.get('id'))}"

    def removeRecentMessage(self, messageId):
        keyStr = self.messageKeyIndex.get(messageId)
        if not _truthy(keyStr):
            return
        self.recentMessagesMap.delete(keyStr)
        self.messageKeyIndex.pop(messageId, None)


def _fireAndForget(result):
    if inspect.isawaitable(result):
        asyncio.ensure_future(result)


def makeOfflineNodeProcessor(nodeProcessorMap, deps, batchSize=10):
    nodes = []
    state = JSObject({'isProcessing': False})

    async def run():
        processedInBatch = 0
        while len(nodes) and _method(deps, 'isWsOpen')():
            item = nodes.pop(0)
            type_ = item.get('type')
            node = item.get('node')
            nodeProcessor = nodeProcessorMap.get(type_)
            if nodeProcessor is None:
                _method(deps, 'onUnexpectedError')(JSError(f'unknown offline node type: {_tpl(type_)}'), 'processing offline node')
                continue
            try:
                await _maybeAwait(nodeProcessor(node))
            except Exception as err:
                await _maybeAwait(_method(deps, 'onUnexpectedError')(err, f'processing offline {_tpl(type_)}'))
            processedInBatch += 1
            if processedInBatch >= batchSize:
                processedInBatch = 0
                await _maybeAwait(_method(deps, 'yieldToEventLoop')())
        state['isProcessing'] = False

    async def runGuarded():
        try:
            await run()
        except Exception as error:
            await _maybeAwait(_method(deps, 'onUnexpectedError')(error, 'processing offline nodes'))

    def enqueue(type_, node):
        nodes.append(JSObject({'type': type_, 'node': node}))
        if state.get('isProcessing'):
            return
        state['isProcessing'] = True
        asyncio.ensure_future(runGuarded())

    return JSObject({'enqueue': enqueue})


def _extractPnFromMessages(messages):
    for msgItem in messages:
        message = msgItem.get('message')
        if not _truthy(_path(message, 'key', 'fromMe')):
            continue
        receipts = _or(message.get('userReceipt'), []) if message is not None else []
        if len(receipts) == 0:
            continue
        userJid = _path(receipts[0], 'userJid')
        if _truthy(userJid) and (isPnUser(userJid) or isHostedPnUser(userJid)):
            return userJid
    return None


async def downloadHistory(msg, options):
    stream = await _maybeAwait(downloadContentFromMessage(msg, 'md-msg-hist', JSObject({'options': options})))
    inflater = zlib.decompressobj()
    chunks = []
    async for block in _eachChunk(stream):
        chunks.append(inflater.decompress(bytes(block)))
    chunks.append(inflater.flush())
    return proto.HistorySync.decode(b''.join(chunks))


def processHistoryMessage(item, logger):
    messages = []
    contacts = []
    chats = []
    lidPnMappings = []
    _log(logger, 'trace', JSObject({'progress': item.get('progress')}), 'processing history of type ' + _tpl(item.get('syncType')))
    for m in _or(item.get('phoneNumberToLidMappings'), []):
        if _truthy(m.get('lidJid')) and _truthy(m.get('pnJid')):
            lidPnMappings.append(JSObject({'lid': m.get('lidJid'), 'pn': m.get('pnJid')}))
    HistorySyncType = proto.HistorySync.HistorySyncType
    syncType = item.get('syncType')
    if syncType in (HistorySyncType.INITIAL_BOOTSTRAP, HistorySyncType.RECENT, HistorySyncType.FULL, HistorySyncType.ON_DEMAND):
        for chat in item.get('conversations'):
            contacts.append(JSObject({
                'id': chat.get('id'),
                'name': _or(chat.get('displayName'), chat.get('name'), chat.get('username'), None),
                'username': _or(chat.get('username'), None),
                'lid': _or(chat.get('lidJid'), chat.get('accountLid'), None),
                'phoneNumber': _or(chat.get('pnJid'), None)
            }))
            chatId = chat.get('id')
            isLid = isLidUser(chatId) or isHostedLidUser(chatId)
            isPn = isPnUser(chatId) or isHostedPnUser(chatId)
            if isLid and _truthy(chat.get('pnJid')):
                lidPnMappings.append(JSObject({'lid': chatId, 'pn': chat.get('pnJid')}))
            elif isPn and _truthy(chat.get('lidJid')):
                lidPnMappings.append(JSObject({'lid': chat.get('lidJid'), 'pn': chatId}))
            elif isLid and not _truthy(chat.get('pnJid')):
                pnFromReceipt = _extractPnFromMessages(_or(chat.get('messages'), []))
                if _truthy(pnFromReceipt):
                    lidPnMappings.append(JSObject({'lid': chatId, 'pn': pnFromReceipt}))
            msgs = _or(chat.get('messages'), [])
            dict.pop(chat, 'messages', None)
            for item_ in msgs:
                message = item_.get('message')
                messages.append(message)
                if len(_or(chat.get('messages'), [])) == 0:
                    chat['messages'] = [JSObject({'message': message})]
                if not message.get('key').get('fromMe') and not _truthy(chat.get('lastMessageRecvTimestamp')):
                    chat['lastMessageRecvTimestamp'] = toNumber(message.get('messageTimestamp'))
                stubType = message.get('messageStubType')
                if stubType in (WAMessageStubType.BIZ_PRIVACY_MODE_TO_BSP, WAMessageStubType.BIZ_PRIVACY_MODE_TO_FB) and _truthy(_idx(message.get('messageStubParameters'), 0)):
                    contacts.append(JSObject({
                        'id': _or(message.get('key').get('participant'), message.get('key').get('remoteJid')),
                        'verifiedName': _idx(message.get('messageStubParameters'), 0)
                    }))
            chats.append(chat)
    elif syncType == HistorySyncType.PUSH_NAME:
        for c in item.get('pushnames'):
            contacts.append(JSObject({'id': c.get('id'), 'notify': c.get('pushname')}))
    return JSObject({
        'chats': chats,
        'contacts': contacts,
        'messages': messages,
        'lidPnMappings': lidPnMappings,
        'pastParticipants': item.get('pastParticipants'),
        'syncType': item.get('syncType'),
        'progress': item.get('progress')
    })


async def downloadAndProcessHistorySyncNotification(msg, options, logger):
    payload = msg.get('initialHistBootstrapInlinePayload')
    if _truthy(payload):
        historyMsg = proto.HistorySync.decode(zlib.decompress(bytes(payload)))
    else:
        historyMsg = await downloadHistory(msg, options)
    return processHistoryMessage(historyMsg, logger)


def getHistoryMsg(message):
    normalizedContent = normalizeMessageContent(message) if message is not None else None
    return _path(normalizedContent, 'protocolMessage', 'historySyncNotification')


def parseCatalogNode(node):
    catalogNode = getBinaryNodeChild(node, 'product_catalog')
    products = [parseProductNode(p) for p in getBinaryNodeChildren(catalogNode, 'product')]
    paging = getBinaryNodeChild(catalogNode, 'paging')
    return JSObject({
        'products': products,
        'nextPageCursor': getBinaryNodeChildString(paging, 'after') if paging is not None else None
    })


def parseCollectionsNode(node):
    collectionsNode = getBinaryNodeChild(node, 'collections')
    collections = [
        JSObject({
            'id': getBinaryNodeChildString(collectionNode, 'id'),
            'name': getBinaryNodeChildString(collectionNode, 'name'),
            'products': [parseProductNode(p) for p in getBinaryNodeChildren(collectionNode, 'product')],
            'status': parseStatusInfo(collectionNode)
        })
        for collectionNode in getBinaryNodeChildren(collectionsNode, 'collection')
    ]
    return JSObject({'collections': collections})


def parseOrderDetailsNode(node):
    orderNode = getBinaryNodeChild(node, 'order')
    products = []
    for productNode in getBinaryNodeChildren(orderNode, 'product'):
        imageNode = getBinaryNodeChild(productNode, 'image')
        products.append(JSObject({
            'id': getBinaryNodeChildString(productNode, 'id'),
            'name': getBinaryNodeChildString(productNode, 'name'),
            'imageUrl': getBinaryNodeChildString(imageNode, 'url'),
            'price': _plus(getBinaryNodeChildString(productNode, 'price')),
            'currency': getBinaryNodeChildString(productNode, 'currency'),
            'quantity': _plus(getBinaryNodeChildString(productNode, 'quantity'))
        }))
    priceNode = getBinaryNodeChild(orderNode, 'price')
    return JSObject({
        'price': JSObject({
            'total': _plus(getBinaryNodeChildString(priceNode, 'total')),
            'currency': getBinaryNodeChildString(priceNode, 'currency')
        }),
        'products': products
    })


def _boolStr(value):
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    return _numStr(value)


def toProductNode(productId, product):
    attrs = JSObject({})
    content = []
    if productId is not None:
        content.append(JSObject({'tag': 'id', 'attrs': JSObject({}), 'content': _bin(productId)}))
    if product.get('name') is not None:
        content.append(JSObject({'tag': 'name', 'attrs': JSObject({}), 'content': _bin(product.get('name'))}))
    if product.get('description') is not None:
        content.append(JSObject({'tag': 'description', 'attrs': JSObject({}), 'content': _bin(product.get('description'))}))
    if product.get('retailerId') is not None:
        content.append(JSObject({'tag': 'retailer_id', 'attrs': JSObject({}), 'content': _bin(product.get('retailerId'))}))
    if len(product.get('images')):
        media = []
        for img in product.get('images'):
            if 'url' not in img:
                raise Boom('Expected img for product to already be uploaded', JSObject({'statusCode': 400}))
            media.append(JSObject({
                'tag': 'image',
                'attrs': JSObject({}),
                'content': [JSObject({'tag': 'url', 'attrs': JSObject({}), 'content': _bin(_tpl(img.get('url')))})]
            }))
        content.append(JSObject({'tag': 'media', 'attrs': JSObject({}), 'content': media}))
    if product.get('price') is not None:
        content.append(JSObject({'tag': 'price', 'attrs': JSObject({}), 'content': _bin(_numStr(product.get('price')))}))
    if product.get('currency') is not None:
        content.append(JSObject({'tag': 'currency', 'attrs': JSObject({}), 'content': _bin(product.get('currency'))}))
    if 'originCountryCode' in product:
        if product.get('originCountryCode') is None:
            attrs['compliance_category'] = 'COUNTRY_ORIGIN_EXEMPT'
        else:
            content.append(JSObject({
                'tag': 'compliance_info',
                'attrs': JSObject({}),
                'content': [JSObject({'tag': 'country_code_origin', 'attrs': JSObject({}), 'content': _bin(product.get('originCountryCode'))})]
            }))
    if product.get('isHidden') is not None:
        attrs['is_hidden'] = _boolStr(product.get('isHidden'))
    return JSObject({'tag': 'product', 'attrs': attrs, 'content': content})


def parseProductNode(productNode):
    isHidden = productNode.get('attrs').get('is_hidden') == 'true'
    id_ = getBinaryNodeChildString(productNode, 'id')
    mediaNode = getBinaryNodeChild(productNode, 'media')
    statusInfoNode = getBinaryNodeChild(productNode, 'status_info')
    return JSObject({
        'id': id_,
        'imageUrls': parseImageUrls(mediaNode),
        'reviewStatus': JSObject({'whatsapp': getBinaryNodeChildString(statusInfoNode, 'status')}),
        'availability': 'in stock',
        'name': getBinaryNodeChildString(productNode, 'name'),
        'retailerId': getBinaryNodeChildString(productNode, 'retailer_id'),
        'url': getBinaryNodeChildString(productNode, 'url'),
        'description': getBinaryNodeChildString(productNode, 'description'),
        'price': _plus(getBinaryNodeChildString(productNode, 'price')),
        'currency': getBinaryNodeChildString(productNode, 'currency'),
        'isHidden': isHidden
    })


async def uploadingNecessaryImagesOfProduct(product, waUploadToServer, timeoutMs=30000):
    images = product.get('images')
    uploaded = await uploadingNecessaryImages(images, waUploadToServer, timeoutMs) if images is not None else images
    return JSObject({**product, 'images': uploaded})


async def uploadingNecessaryImages(images, waUploadToServer, timeoutMs=30000):
    async def uploadOne(img):
        if 'url' in img:
            url = _tpl(img.get('url'))
            if '.whatsapp.net' in url:
                return JSObject({'url': url})
        result = await _maybeAwait(getStream(img))
        stream = result.get('stream')
        hasher = createHash('sha256')
        filePath = os.path.join(tempfile.gettempdir(), 'img' + generateMessageIDV2(None))
        with open(filePath, 'wb') as encFile:
            async for block in _eachChunk(stream):
                hasher.update(block)
                encFile.write(block)
        sha = base64.b64encode(hasher.digest()).decode()
        uploaded = await _maybeAwait(waUploadToServer(filePath, JSObject({
            'mediaType': 'product-catalog-image',
            'fileEncSha256B64': sha,
            'timeoutMs': timeoutMs
        })))
        try:
            os.remove(filePath)
        except Exception as err:
            print('Error deleting temp file ', err)
        return JSObject({'url': getUrlFromDirectPath(uploaded.get('directPath'))})

    return list(await asyncio.gather(*[uploadOne(img) for img in images]))


def parseImageUrls(mediaNode):
    imgNode = getBinaryNodeChild(mediaNode, 'image')
    return JSObject({
        'requested': getBinaryNodeChildString(imgNode, 'request_image_url'),
        'original': getBinaryNodeChildString(imgNode, 'original_image_url')
    })


def parseStatusInfo(mediaNode):
    node = getBinaryNodeChild(mediaNode, 'status_info')
    return JSObject({
        'status': getBinaryNodeChildString(node, 'status'),
        'canAppeal': getBinaryNodeChildString(node, 'can_appeal') == 'true'
    })


default = processMessage
