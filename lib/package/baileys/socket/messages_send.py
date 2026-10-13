import asyncio
import datetime
import inspect
import sys
import traceback

from baileys.foundation.node_crypto import randomBytes
from baileys.foundation.ai_rich import AIRichBuilder
from baileys.foundation.boom import Boom
from baileys.foundation.cache import Cache
from baileys.foundation.concurrency import makeKeyedMutex, makeMutex
from baileys.WAProto import proto
from baileys.defaults import BIZ_BOT_SUPPORT_PAYLOAD, DEFAULT_CACHE_TTLS, WA_DEFAULT_EPHEMERAL
from baileys.constants import AssociationType
from baileys.foundation.node_runtime import JSObject, nextTick, now
from baileys.utils.wa_protocol_core import (
    bindWaitForEvent,
    delay,
    encodeNewsletterMessage,
    encodeSignedDeviceIdentity,
    encodeWAMessage,
    extractDeviceJids,
    generateMessageIDV2,
    generateParticipantHashV2,
    getMessageReportingToken,
    parseAndInjectE2ESessions,
    shouldIncludeReportingToken,
    unixTimestampSeconds,
)
from baileys.utils.message_compose import (
    aggregateMessageKeysNotFromMe,
    assertMediaContent,
    dateToMs,
    generateWAMessage,
    generateWAMessageFromContent,
    hasValidAlbumMedia,
    normalizeMessageContent,
    shouldIncludeBizBinaryNode,
)
from baileys.utils.media import (
    decryptMediaRetryData,
    DEF_MEDIA_HOST,
    encryptMediaRetryRequest,
    getStatusCodeForMediaRetry,
    getUrlFromDirectPath,
    getUrlInfo,
    getWAUploadToServer,
)
from baileys.utils.message_processing import MessageRetryManager
from baileys.utils.auth_state_core import (
    assertMeId,
    buildMergedTcTokenIndexWrite,
    isTcTokenExpired,
    resolveIssuanceJid,
    resolveTcTokenJid,
    shouldSendNewTcToken,
    storeTcTokensFromIqResult,
)
from baileys.binary.wa_binary import (
    areJidsSameUser,
    getBinaryNodeChild,
    getBinaryNodeChildren,
    getBizBinaryNode,
    isHostedLidUser,
    isHostedPnUser,
    isJidBot,
    isJidGroup,
    isJidMetaAI,
    isJidNewsletter,
    isLidUser,
    isPnUser,
    jidDecode,
    jidEncode,
    jidNormalizedUser,
    PSA_WID,
    S_WHATSAPP_NET,
    jsNumber,
)
import baileys.socket.usync as usync
import baileys.socket.groups_newsletter as groups_newsletter

STATUS_JID = 'status@broadcast'


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


def _hasMember(obj, name):
    if isinstance(obj, dict):
        return dict.get(obj, name) is not None
    return getattr(obj, name, None) is not None


async def _awaitValue(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _spawn(fn, *args):
    return asyncio.ensure_future(fn(*args))


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


def _or(a, b):
    return a if _jsTruthy(a) else b


def _nullish(a, b):
    return a if a is not None else b


def _numStr(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _errMessage(err):
    return getattr(err, 'message', None)


def _firstWithMessage(items):
    for item in items:
        if _jsTruthy(item.get('message')):
            return item
    return None


async def _cget(cache, key):
    return await _awaitValue(_m(cache, 'get')(key))


async def _cset(cache, key, value):
    return await _awaitValue(_m(cache, 'set')(key, value))


async def _cdel(cache, key):
    name = 'del' if isinstance(cache, dict) else 'del_'
    return await _awaitValue(_m(cache, name)(key))


def getMediaType(message):
    if _p(message, 'imageMessage') is not None:
        return 'image'
    if _p(message, 'videoMessage') is not None:
        return 'video' if not _jsTruthy(message.get('videoMessage').get('gifPlayback')) else 'gif'
    if _p(message, 'audioMessage') is not None:
        return 'ptt' if _jsTruthy(message.get('audioMessage').get('ptt')) else 'audio'
    if _p(message, 'contactMessage') is not None:
        return 'vcard'
    if _p(message, 'documentMessage') is not None:
        return 'document'
    if _p(message, 'contactsArrayMessage') is not None:
        return 'contact_array'
    if _p(message, 'liveLocationMessage') is not None:
        return 'livelocation'
    if _p(message, 'stickerMessage') is not None:
        return 'sticker'
    if _p(message, 'listMessage') is not None:
        return 'list'
    if _p(message, 'listResponseMessage') is not None:
        return 'list_response'
    if _p(message, 'buttonsResponseMessage') is not None:
        return 'buttons_response'
    if _p(message, 'orderMessage') is not None:
        return 'order'
    if _p(message, 'productMessage') is not None:
        return 'product'
    if _p(message, 'interactiveResponseMessage') is not None:
        return 'native_flow_response'
    if _p(message, 'groupInviteMessage') is not None:
        return 'url'
    return ''


def getMessageType(message):
    normalizedMessage = normalizeMessageContent(message)
    if not _jsTruthy(normalizedMessage):
        return 'text'
    if _jsTruthy(_p(normalizedMessage, 'reactionMessage')) or _jsTruthy(_p(normalizedMessage, 'encReactionMessage')):
        return 'reaction'
    if (_jsTruthy(_p(normalizedMessage, 'pollCreationMessage')) or
            _jsTruthy(_p(normalizedMessage, 'pollCreationMessageV2')) or
            _jsTruthy(_p(normalizedMessage, 'pollCreationMessageV3')) or
            _jsTruthy(_p(normalizedMessage, 'pollCreationMessageV5')) or
            _jsTruthy(_p(normalizedMessage, 'pollCreationMessageV6')) or
            _jsTruthy(_p(normalizedMessage, 'pollUpdateMessage'))):
        return 'poll'
    if _jsTruthy(_p(normalizedMessage, 'eventMessage')):
        return 'event'
    if getMediaType(normalizedMessage) != '':
        return 'media'
    return 'text'


def makeMessagesSocket(config):
    logger = config.get('logger')
    linkPreviewImageThumbnailWidth = config.get('linkPreviewImageThumbnailWidth')
    generateHighQualityLinkPreview = config.get('generateHighQualityLinkPreview')
    httpRequestOptions = config.get('options')
    cachedGroupMetadata = config.get('cachedGroupMetadata')
    enableRecentMessageCache = config.get('enableRecentMessageCache')
    maxMsgRetryCount = config.get('maxMsgRetryCount')
    patchBox = [config.get('patchMessageBeforeSending')]

    async def patchMessageBeforeSending(message, jids):
        return await _awaitValue(patchBox[0](message, jids))

    sock = groups_newsletter.makeNewsletterSocket(config)
    ev = sock.get('ev')
    authState = sock.get('authState')
    messageMutex = sock.get('messageMutex')
    signalRepository = sock.get('signalRepository')
    upsertMessage = sock.get('upsertMessage')
    query = sock.get('query')
    fetchPrivacySettings = sock.get('fetchPrivacySettings')
    sendNode = sock.get('sendNode')
    groupMetadata = sock.get('groupMetadata')
    groupToggleEphemeral = sock.get('groupToggleEphemeral')
    registerSocketEndHandler = sock.get('registerSocketEndHandler')
    getLIDForPN = signalRepository.get('lidMapping').get('getLIDForPN')

    inFlightTcTokenIssuance = set()
    userDevicesCache = _or(config.get('userDevicesCache'), Cache(JSObject({
        'ttl': DEFAULT_CACHE_TTLS['USER_DEVICES'] * 1000,
        'max': 1000
    })))

    devicesMutex = makeMutex()

    messageRetryManager = MessageRetryManager(logger, maxMsgRetryCount) if _jsTruthy(enableRecentMessageCache) else None

    encryptionMutex = makeKeyedMutex()
    mediaConn = [None]
    mediaHost = [DEF_MEDIA_HOST]

    async def fetchMediaConn():
        result = await _awaitValue(query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({
                'type': 'set',
                'xmlns': 'w:m',
                'to': S_WHATSAPP_NET
            }),
            'content': [JSObject({'tag': 'media_conn', 'attrs': JSObject({})})]
        })))
        mediaConnNode = getBinaryNodeChild(result, 'media_conn')
        hosts = []
        for item in getBinaryNodeChildren(mediaConnNode, 'host'):
            attrs = item.get('attrs')
            hosts.append(JSObject({
                'hostname': attrs.get('hostname'),
                'maxContentLengthBytes': jsNumber(attrs.get('maxContentLengthBytes'))
            }))
        node = JSObject({
            'hosts': hosts,
            'auth': mediaConnNode.get('attrs').get('auth'),
            'ttl': jsNumber(mediaConnNode.get('attrs').get('ttl')),
            'fetchDate': datetime.datetime.now(datetime.timezone.utc)
        })
        logger.debug('fetched media conn')
        if _jsTruthy(hosts[0] if len(hosts) else None):
            mediaHost[0] = hosts[0].get('hostname')
        return node

    async def refreshMediaConn(forceGet=False):
        media = await _awaitValue(mediaConn[0]) if mediaConn[0] is not None else None
        if media is None or forceGet or now() - dateToMs(media.get('fetchDate')) > media.get('ttl') * 1000:
            mediaConn[0] = asyncio.ensure_future(fetchMediaConn())
        return await _awaitValue(mediaConn[0])

    async def sendReceipt(jid, participant, messageIds, type_):
        if messageIds is None or len(messageIds) == 0:
            raise Boom('missing ids in receipt')
        node = JSObject({
            'tag': 'receipt',
            'attrs': JSObject({'id': messageIds[0]})
        })
        isReadReceipt = type_ == 'read' or type_ == 'read-self'
        if isReadReceipt:
            node.get('attrs')['t'] = str(unixTimestampSeconds())
        if type_ == 'sender' and (isPnUser(jid) or isLidUser(jid)):
            node.get('attrs')['recipient'] = jid
            node.get('attrs')['to'] = participant
        else:
            node.get('attrs')['to'] = jid
            if _jsTruthy(participant):
                node.get('attrs')['participant'] = participant
        if _jsTruthy(type_):
            node.get('attrs')['type'] = type_
        remainingMessageIds = list(messageIds[1:])
        if len(remainingMessageIds):
            node['content'] = [JSObject({
                'tag': 'list',
                'attrs': JSObject({}),
                'content': [JSObject({'tag': 'item', 'attrs': JSObject({'id': messageId})}) for messageId in remainingMessageIds]
            })]
        logger.debug(JSObject({'attrs': node.get('attrs'), 'messageIds': messageIds}), 'sending receipt for messages')
        await _awaitValue(sendNode(node))

    async def sendReceipts(keys, type_):
        recps = aggregateMessageKeysNotFromMe(keys)
        for recp in recps:
            await sendReceipt(recp.get('jid'), recp.get('participant'), recp.get('messageIds'), type_)

    async def readMessages(keys):
        privacySettings = await _awaitValue(fetchPrivacySettings())
        readType = 'read' if privacySettings.get('readreceipts') == 'all' else 'read-self'
        await sendReceipts(keys, readType)

    async def getUSyncDevices(jids, useCache, ignoreZeroDevices):
        deviceResults = []
        if not useCache:
            logger.debug('not using cache for devices')
        toFetch = []
        jidsWithUser = []
        for jid in jids:
            decoded = jidDecode(jid)
            user = _p(decoded, 'user')
            device = _p(decoded, 'device')
            isExplicitDevice = isinstance(device, (int, float)) and not isinstance(device, bool) and device >= 0
            if isExplicitDevice and _jsTruthy(user):
                deviceResults.append(JSObject({'user': user, 'device': device, 'jid': jid}))
                continue
            jidsWithUser.append(JSObject({'jid': jidNormalizedUser(jid), 'user': user}))

        mgetDevices = None
        hasMget = _hasMember(userDevicesCache, 'mget')
        if useCache and hasMget:
            usersToFetch = [entry.get('user') for entry in jidsWithUser if _jsTruthy(entry.get('user'))]
            mgetDevices = await _awaitValue(_m(userDevicesCache, 'mget')(usersToFetch))

        for entry in jidsWithUser:
            jid = entry.get('jid')
            user = entry.get('user')
            if useCache:
                devices = _p(mgetDevices, user) if mgetDevices is not None else None
                if not _jsTruthy(devices) and not hasMget:
                    devices = await _cget(userDevicesCache, user)
                if _jsTruthy(devices):
                    devicesWithJid = [JSObject({**d, 'jid': jidEncode(d.get('user'), d.get('server'), d.get('device'))}) for d in devices]
                    deviceResults.extend(devicesWithJid)
                    logger.trace(JSObject({'user': user}), 'using cache for devices')
                else:
                    toFetch.append(jid)
            else:
                toFetch.append(jid)

        if not len(toFetch):
            return deviceResults

        requestedLidUsers = set()
        for jid in toFetch:
            if isLidUser(jid) or isHostedLidUser(jid):
                user = _p(jidDecode(jid), 'user')
                if _jsTruthy(user):
                    requestedLidUsers.add(user)

        usyncQuery = usync.USyncQuery().withContext('message').withDeviceProtocol().withLIDProtocol()
        for jid in toFetch:
            usyncQuery.withUser(usync.USyncUser().withId(jid))

        result = await _awaitValue(sock.get('executeUSyncQuery')(usyncQuery))
        if _jsTruthy(result):
            lidResults = [a for a in result.get('list') if _jsTruthy(_p(a, 'lid'))]
            if len(lidResults) > 0:
                logger.trace('Storing LID maps from device call')
                await _awaitValue(signalRepository.get('lidMapping').get('storeLIDPNMappings')([
                    JSObject({'lid': a.get('lid'), 'pn': a.get('id')}) for a in lidResults
                ]))
                try:
                    lids = [a.get('lid') for a in lidResults]
                    if len(lids):
                        await assertSessions(lids, True)
                except Exception as e:
                    logger.warn(JSObject({'e': e, 'count': len(lidResults)}), 'failed to assert sessions for newly mapped LIDs')
            me = authState.get('creds').get('me')
            extracted = extractDeviceJids(_p(result, 'list'), me.get('id'), me.get('lid'), ignoreZeroDevices)
            deviceMap = {}
            for item in extracted:
                deviceMap.setdefault(item.get('user'), []).append(item)

            for user, userDevices in list(deviceMap.items()):
                isLidUserFlag = user in requestedLidUsers
                for item in userDevices:
                    finalJid = jidEncode(user, item.get('server'), item.get('device')) if isLidUserFlag else jidEncode(item.get('user'), item.get('server'), item.get('device'))
                    deviceResults.append(JSObject({**item, 'jid': finalJid}))
                    logger.debug(JSObject({
                        'user': item.get('user'),
                        'device': item.get('device'),
                        'finalJid': finalJid,
                        'usedLid': isLidUserFlag
                    }), 'Processed device with LID priority')

            async def cacheDeviceMap():
                if _hasMember(userDevicesCache, 'mset'):
                    await _awaitValue(_m(userDevicesCache, 'mset')([JSObject({'key': key, 'value': value}) for key, value in deviceMap.items()]))
                else:
                    for key in list(deviceMap.keys()):
                        if _jsTruthy(deviceMap[key]):
                            await _cset(userDevicesCache, key, deviceMap[key])

            await devicesMutex.get('mutex')(cacheDeviceMap)

            userDeviceUpdates = JSObject({})
            for userId, devices in deviceMap.items():
                if _jsTruthy(devices) and len(devices) > 0:
                    userDeviceUpdates[userId] = [_numStr(d.get('device')) if d.get('device') is not None else '0' for d in devices]
            if len(list(dict.keys(userDeviceUpdates))) > 0:
                try:
                    await _awaitValue(authState.get('keys').get('set')(JSObject({'device-list': userDeviceUpdates})))
                    logger.debug(JSObject({'userCount': len(list(dict.keys(userDeviceUpdates)))}), 'stored user device lists for bulk migration')
                except Exception as error:
                    logger.warn(JSObject({'error': error}), 'failed to store user device lists')
        return deviceResults

    async def updateMemberLabel(jid, memberLabel):
        return await relayMessage(jid, JSObject({
            'protocolMessage': JSObject({
                'type': proto.Message.ProtocolMessage.Type.GROUP_MEMBER_LABEL_CHANGE,
                'memberLabel': JSObject({
                    'label': memberLabel[:30] if memberLabel is not None else None,
                    'labelTimestamp': unixTimestampSeconds()
                })
            })
        }), JSObject({
            'additionalNodes': [
                JSObject({
                    'tag': 'meta',
                    'attrs': JSObject({
                        'tag_reason': 'user_update',
                        'appdata': 'member_tag'
                    }),
                    'content': None
                })
            ]
        }))

    async def assertSessions(jids, force=False):
        didFetchNewSession = False
        uniqueJids = list(dict.fromkeys(jids))
        jidsRequiringFetch = []
        logger.debug(JSObject({'jids': jids}), 'assertSessions call with jids')
        for jid in uniqueJids:
            if not force:
                sessionValidation = await _awaitValue(signalRepository.get('validateSession')(jid))
                if sessionValidation.get('exists'):
                    continue
            jidsRequiringFetch.append(jid)

        if len(jidsRequiringFetch):
            pnJids = [jid for jid in jidsRequiringFetch if _jsTruthy(isPnUser(jid)) or _jsTruthy(isHostedPnUser(jid))]
            lidMappings = await _awaitValue(signalRepository.get('lidMapping').get('getLIDsForPNs')(pnJids))
            if not _jsTruthy(lidMappings):
                lidMappings = []
            wireJids = [
                *[jid for jid in jidsRequiringFetch if _jsTruthy(isLidUser(jid)) or _jsTruthy(isHostedLidUser(jid))],
                *[a.get('lid') for a in lidMappings]
            ]
            logger.debug(JSObject({'jidsRequiringFetch': jidsRequiringFetch, 'wireJids': wireJids}), 'fetching sessions')
            keyContent = []
            for jid in wireJids:
                attrs = JSObject({'jid': jid})
                if force:
                    attrs['reason'] = 'identity'
                keyContent.append(JSObject({'tag': 'user', 'attrs': attrs}))
            result = await _awaitValue(query(JSObject({
                'tag': 'iq',
                'attrs': JSObject({
                    'xmlns': 'encrypt',
                    'type': 'get',
                    'to': S_WHATSAPP_NET
                }),
                'content': [
                    JSObject({
                        'tag': 'key',
                        'attrs': JSObject({}),
                        'content': keyContent
                    })
                ]
            })))
            await _awaitValue(parseAndInjectE2ESessions(result, signalRepository))
            didFetchNewSession = True
        return didFetchNewSession

    async def sendPeerDataOperationMessage(pdoMessage):
        if _p(_p(authState.get('creds'), 'me'), 'id') is None:
            raise Boom('Not authenticated')
        protocolMessage = JSObject({
            'protocolMessage': JSObject({
                'peerDataOperationRequestMessage': pdoMessage,
                'type': proto.Message.ProtocolMessage.Type.PEER_DATA_OPERATION_REQUEST_MESSAGE
            })
        })
        meJid = jidNormalizedUser(authState.get('creds').get('me').get('id'))
        msgId = await relayMessage(meJid, protocolMessage, JSObject({
            'additionalAttributes': JSObject({
                'category': 'peer',
                'push_priority': 'high_force'
            }),
            'additionalNodes': [
                JSObject({'tag': 'meta', 'attrs': JSObject({'appdata': 'default'})})
            ]
        }))
        return msgId

    async def createParticipantNodes(recipientJids, message, extraAttrs, dsmMessage=None, alreadyPatched=False):
        if not len(recipientJids):
            return JSObject({'nodes': [], 'shouldIncludeDeviceIdentity': False})
        patched = message if alreadyPatched else await patchMessageBeforeSending(message, recipientJids)
        if isinstance(patched, list):
            patchedMessages = patched
        else:
            patchedMessages = [JSObject({'recipientJid': jid, 'message': patched}) for jid in recipientJids]
        shouldIncludeDeviceIdentity = [False]
        meId = authState.get('creds').get('me').get('id')
        meLid = _p(authState.get('creds').get('me'), 'lid')
        meLidUser = _p(jidDecode(meLid), 'user') if _jsTruthy(meLid) else None

        async def encryptOne(item):
            jid = item.get('recipientJid')
            patchedMessage = item.get('message')
            try:
                if not _jsTruthy(jid) or patchedMessage is None:
                    return None
                msgToEncrypt = patchedMessage
                if dsmMessage is not None:
                    targetUser = _p(jidDecode(jid), 'user')
                    ownPnUser = _p(jidDecode(meId), 'user')
                    ownLidUser = meLidUser
                    isOwnUser = targetUser == ownPnUser or (_jsTruthy(ownLidUser) and targetUser == ownLidUser)
                    isExactSenderDevice = jid == meId or (_jsTruthy(meLid) and jid == meLid)
                    if isOwnUser and not isExactSenderDevice:
                        msgToEncrypt = dsmMessage
                        logger.debug(JSObject({'jid': jid, 'targetUser': targetUser}), 'Using DSM for own device')
                data = encodeWAMessage(msgToEncrypt)

                async def encryptWork():
                    encrypted = await _awaitValue(signalRepository.get('encryptMessage')(JSObject({'jid': jid, 'data': data})))
                    type_ = encrypted.get('type')
                    if type_ == 'pkmsg':
                        shouldIncludeDeviceIdentity[0] = True
                    attrs = JSObject({'v': '2', 'type': type_})
                    if extraAttrs:
                        attrs.update(extraAttrs)
                    return JSObject({
                        'tag': 'to',
                        'attrs': JSObject({'jid': jid}),
                        'content': [
                            JSObject({
                                'tag': 'enc',
                                'attrs': attrs,
                                'content': encrypted.get('ciphertext')
                            })
                        ]
                    })

                return await encryptionMutex.get('mutex')(jid, encryptWork)
            except Exception as err:
                logger.error(JSObject({'jid': jid, 'err': err}), 'Failed to encrypt for recipient')
                print('[sPR-DEBUG] encrypt failed for', jid, _errMessage(err), file=sys.stderr)
                return None

        results = await asyncio.gather(*[encryptOne(item) for item in patchedMessages])
        nodes = [node for node in results if node is not None]
        if len(recipientJids) > 0 and len(nodes) == 0:
            raise Boom('All encryptions failed', JSObject({'statusCode': 500}))
        return JSObject({'nodes': nodes, 'shouldIncludeDeviceIdentity': shouldIncludeDeviceIdentity[0]})

    async def relayMessage(jid, message, options=None):
        if options is None:
            options = JSObject({})
        msgId = options.get('messageId')
        participant = options.get('participant')
        additionalAttributes = options.get('additionalAttributes')
        additionalNodes = options.get('additionalNodes')
        useUserDevicesCache = options.get('useUserDevicesCache')
        useCachedGroupMetadata = options.get('useCachedGroupMetadata')
        addBizAttributes = options.get('addBizAttributes')
        statusJidList = options.get('statusJidList')

        meId = assertMeId(authState.get('creds'))
        meLid = _p(authState.get('creds').get('me'), 'lid')
        isRetryResend = _jsTruthy(_p(participant, 'jid'))
        shouldIncludeDeviceIdentity = isRetryResend
        decodedJid = jidDecode(jid)
        user = decodedJid.get('user')
        server = decodedJid.get('server')
        isGroup = server == 'g.us'
        isStatus = jid == STATUS_JID
        isLid = server == 'lid'
        isNewsletter = server == 'newsletter'
        isGroupOrStatus = isGroup or isStatus
        finalJid = jid
        msgId = msgId if _jsTruthy(msgId) else generateMessageIDV2(meId)
        useUserDevicesCache = useUserDevicesCache is not False
        useCachedGroupMetadata = useCachedGroupMetadata is not False and not isStatus
        participants = []
        destinationJid = finalJid if not isStatus else STATUS_JID
        binaryNodeContent = []
        devices = []
        reportingMessage = None
        meMsg = JSObject({
            'deviceSentMessage': JSObject({
                'destinationJid': destinationJid,
                'message': message
            }),
            'messageContextInfo': message.get('messageContextInfo')
        })
        extraAttrs = JSObject({})
        if _jsTruthy(participant):
            if not isGroup and not isStatus:
                additionalAttributes = JSObject({**_or(additionalAttributes, JSObject({})), 'device_fanout': 'false'})
            decodedParticipant = jidDecode(participant.get('jid'))
            devices.append(JSObject({
                'user': decodedParticipant.get('user'),
                'device': decodedParticipant.get('device'),
                'jid': participant.get('jid')
            }))

        async def transactionWork():
            nonlocal shouldIncludeDeviceIdentity, reportingMessage, additionalAttributes
            innerMessage = normalizeMessageContent(message)
            mediaType = getMediaType(innerMessage)
            if _jsTruthy(mediaType):
                extraAttrs['mediatype'] = mediaType

            if isNewsletter:
                patched = await patchMessageBeforeSending(message, [])
                data = encodeNewsletterMessage(patched)
                if additionalNodes is not None and len(additionalNodes) > 0:
                    binaryNodeContent.extend(additionalNodes)
                binaryNodeContent.append(JSObject({
                    'tag': 'plaintext',
                    'attrs': extraAttrs,
                    'content': data
                }))
                stanza = JSObject({
                    'tag': 'message',
                    'attrs': JSObject({
                        'to': jid,
                        'id': msgId,
                        'type': getMessageType(innerMessage),
                        **_or(additionalAttributes, JSObject({}))
                    }),
                    'content': binaryNodeContent
                })
                logger.debug(JSObject({'msgId': msgId}), f'sending newsletter message to {jid}')
                await _awaitValue(sendNode(stanza))
                return

            isNeedMetaAttrs = _jsTruthy(_p(innerMessage, 'pinInChatMessage')) or _jsTruthy(_p(innerMessage, 'keepInChatMessage')) or _jsTruthy(_p(innerMessage, 'reactionMessage'))
            isGroupStatus = _or(_p(message, 'groupStatusMessage'), _p(message, 'groupStatusMessageV2'))
            isPollUpdate = _p(innerMessage, 'pollUpdateMessage')
            if isNeedMetaAttrs or _jsTruthy(isGroupStatus) or _jsTruthy(isPollUpdate):
                metaAttrs = JSObject({})
                if isNeedMetaAttrs:
                    metaAttrs['content_type'] = 'add_on'
                if _jsTruthy(isPollUpdate) and not _jsTruthy(isGroupStatus):
                    metaAttrs['polltype'] = 'vote'
                if _jsTruthy(isGroupStatus):
                    metaAttrs['is_group_status'] = 'true'
                binaryNodeContent.append(JSObject({
                    'tag': 'meta',
                    'attrs': metaAttrs,
                    'content': None
                }))
            if (isNeedMetaAttrs or _jsTruthy(_p(_p(innerMessage, 'protocolMessage'), 'memberLabel')) or
                    _jsTruthy(_p(_p(innerMessage, 'protocolMessage'), 'editedMessage')) or
                    _jsTruthy(_p(_p(innerMessage, 'protocolMessage'), 'mediaNotifyMessage'))):
                extraAttrs['decrypt-fail'] = 'hide'
            nativeFlow = _p(_p(innerMessage, 'interactiveResponseMessage'), 'nativeFlowResponseMessage')
            if nativeFlow is not None:
                extraAttrs['native_flow_name'] = nativeFlow.get('name')

            if isGroupOrStatus and not isRetryResend:
                async def fetchGroupData():
                    groupData = None
                    if useCachedGroupMetadata and cachedGroupMetadata is not None:
                        groupData = await _awaitValue(cachedGroupMetadata(jid))
                    if groupData is not None and isinstance(_p(groupData, 'participants'), list):
                        logger.trace(JSObject({'jid': jid, 'participants': len(groupData.get('participants'))}), 'using cached group metadata')
                    elif not isStatus:
                        groupData = await _awaitValue(groupMetadata(jid))
                    return groupData

                async def fetchSenderKeyMap():
                    if not _jsTruthy(participant) and not isStatus:
                        result = await _awaitValue(authState.get('keys').get('get')('sender-key-memory', [jid]))
                        value = result[jid]
                        return value if value is not None else JSObject({})
                    return JSObject({})

                groupData, senderKeyMap = await asyncio.gather(fetchGroupData(), fetchSenderKeyMap())
                participantsList = [p.get('id') for p in groupData.get('participants')] if groupData is not None else []
                if groupData is not None and _jsTruthy(groupData.get('ephemeralDuration')) and groupData.get('ephemeralDuration') > 0:
                    additionalAttributes = JSObject({
                        **_or(additionalAttributes, JSObject({})),
                        'expiration': _numStr(groupData.get('ephemeralDuration'))
                    })
                if isStatus and statusJidList is not None:
                    participantsList.extend(statusJidList)
                additionalDevices = await getUSyncDevices(participantsList, bool(useUserDevicesCache), False)
                devices.extend(additionalDevices)
                if isGroup:
                    additionalAttributes = JSObject({
                        **_or(additionalAttributes, JSObject({})),
                        'addressing_mode': _or(_p(groupData, 'addressingMode'), 'lid')
                    })

                patched = await patchMessageBeforeSending(message, [d.get('jid') for d in devices] if isGroup else None)
                if isinstance(patched, list):
                    logger.debug(JSObject({'jid': jid, 'devices': len(devices)}), 'per-jid group patch: skmsg+pkmsg hybrid')

                    userMsgMap = {}
                    fallbackPatchedMsg = message
                    for item in patched:
                        msgItem = item.get('message')
                        if not _jsTruthy(msgItem):
                            continue
                        decoded = jidDecode(item.get('recipientJid'))
                        if _jsTruthy(decoded):
                            userMsgMap[decoded.get('user')] = msgItem
                        else:
                            fallbackPatchedMsg = msgItem
                    if not len(userMsgMap):
                        found = _firstWithMessage(patched)
                        fallbackPatchedMsg = _or(found.get('message') if found is not None else None, message)

                    groupAddressingMode = _or(_or(_p(additionalAttributes, 'addressing_mode'), _p(groupData, 'addressingMode')), 'lid')
                    groupSenderIdentity = meLid if (groupAddressingMode == 'lid' and _jsTruthy(meLid)) else meId
                    groupEncrypted = await _awaitValue(signalRepository.get('encryptGroupMessage')(JSObject({
                        'group': destinationJid,
                        'data': encodeWAMessage(fallbackPatchedMsg),
                        'meId': groupSenderIdentity
                    })))
                    ciphertext = groupEncrypted.get('ciphertext')
                    senderKeyDistributionMessage = groupEncrypted.get('senderKeyDistributionMessage')
                    reportingMessage = fallbackPatchedMsg

                    senderKeyRecipients = []
                    for device in devices:
                        deviceJid = device.get('jid')
                        if ((not _jsTruthy(senderKeyMap[deviceJid]) or _jsTruthy(participant)) and
                                not isHostedLidUser(deviceJid) and not isHostedPnUser(deviceJid) and device.get('device') != 99):
                            senderKeyRecipients.append(deviceJid)
                            senderKeyMap[deviceJid] = True
                    if len(senderKeyRecipients):
                        senderKeyMsg = JSObject({'senderKeyDistributionMessage': JSObject({
                            'axolotlSenderKeyDistributionMessage': senderKeyDistributionMessage,
                            'groupId': destinationJid
                        })})
                        await assertSessions(senderKeyRecipients)
                        created = await createParticipantNodes(senderKeyRecipients, senderKeyMsg, extraAttrs)
                        shouldIncludeDeviceIdentity = shouldIncludeDeviceIdentity or created.get('shouldIncludeDeviceIdentity')
                        participants.extend(created.get('nodes'))
                    binaryNodeContent.append(JSObject({
                        'tag': 'enc',
                        'attrs': JSObject({'v': '2', 'type': 'skmsg', **extraAttrs}),
                        'content': ciphertext
                    }))
                    await _awaitValue(authState.get('keys').get('set')(JSObject({'sender-key-memory': JSObject({jid: senderKeyMap})})))

                    customDevices = []
                    for device in devices:
                        decoded = jidDecode(device.get('jid'))
                        if _jsTruthy(decoded) and decoded.get('user') in userMsgMap:
                            customDevices.append(JSObject({'recipientJid': device.get('jid'), 'message': userMsgMap[decoded.get('user')]}))
                    if len(customDevices):
                        customJids = [d.get('recipientJid') for d in customDevices]
                        await assertSessions(customJids)
                        created = await createParticipantNodes(customJids, customDevices, extraAttrs, None, True)
                        shouldIncludeDeviceIdentity = shouldIncludeDeviceIdentity or created.get('shouldIncludeDeviceIdentity')
                        participants.extend(created.get('nodes'))
                else:
                    data = encodeWAMessage(patched)
                    reportingMessage = patched
                    groupAddressingMode = _or(_or(_p(additionalAttributes, 'addressing_mode'), _p(groupData, 'addressingMode')), 'lid')
                    groupSenderIdentity = meLid if (groupAddressingMode == 'lid' and _jsTruthy(meLid)) else meId
                    groupEncrypted = await _awaitValue(signalRepository.get('encryptGroupMessage')(JSObject({
                        'group': destinationJid,
                        'data': data,
                        'meId': groupSenderIdentity
                    })))
                    ciphertext = groupEncrypted.get('ciphertext')
                    senderKeyDistributionMessage = groupEncrypted.get('senderKeyDistributionMessage')
                    senderKeyRecipients = []
                    for device in devices:
                        deviceJid = device.get('jid')
                        hasKey = _jsTruthy(senderKeyMap[deviceJid])
                        if ((not hasKey or _jsTruthy(participant)) and
                                not isHostedLidUser(deviceJid) and
                                not isHostedPnUser(deviceJid) and
                                device.get('device') != 99):
                            senderKeyRecipients.append(deviceJid)
                            senderKeyMap[deviceJid] = True
                    if len(senderKeyRecipients):
                        logger.debug(JSObject({'senderKeyJids': senderKeyRecipients}), 'sending new sender key')
                        senderKeyMsg = JSObject({
                            'senderKeyDistributionMessage': JSObject({
                                'axolotlSenderKeyDistributionMessage': senderKeyDistributionMessage,
                                'groupId': destinationJid
                            })
                        })
                        await assertSessions(senderKeyRecipients)
                        created = await createParticipantNodes(senderKeyRecipients, senderKeyMsg, extraAttrs)
                        shouldIncludeDeviceIdentity = shouldIncludeDeviceIdentity or created.get('shouldIncludeDeviceIdentity')
                        participants.extend(created.get('nodes'))
                    binaryNodeContent.append(JSObject({
                        'tag': 'enc',
                        'attrs': JSObject({'v': '2', 'type': 'skmsg', **extraAttrs}),
                        'content': ciphertext
                    }))
                    await _awaitValue(authState.get('keys').get('set')(JSObject({'sender-key-memory': JSObject({jid: senderKeyMap})})))
            else:
                ownId = meId
                if isLid and _jsTruthy(meLid):
                    ownId = meLid
                    logger.debug(JSObject({'to': jid, 'ownId': ownId}), 'Using LID identity for @lid conversation')
                else:
                    logger.debug(JSObject({'to': jid, 'ownId': ownId}), 'Using PN identity for @s.whatsapp.net conversation')
                ownUser = jidDecode(ownId)['user']
                if not _jsTruthy(participant):
                    patchedForReporting = await patchMessageBeforeSending(message, [jid])
                    if isinstance(patchedForReporting, list):
                        found = next((item for item in patchedForReporting if item.get('recipientJid') == jid), None)
                        reportingMessage = found if found is not None else (patchedForReporting[0] if len(patchedForReporting) else None)
                    else:
                        reportingMessage = patchedForReporting
                if not isRetryResend:
                    targetUserServer = 'lid' if isLid else 's.whatsapp.net'
                    devices.append(JSObject({
                        'user': user,
                        'device': 0,
                        'jid': jidEncode(user, targetUserServer, 0)
                    }))
                    if user != ownUser:
                        ownUserServer = 'lid' if isLid else 's.whatsapp.net'
                        ownUserForAddressing = jidDecode(meLid)['user'] if (isLid and _jsTruthy(meLid)) else jidDecode(meId)['user']
                        devices.append(JSObject({
                            'user': ownUserForAddressing,
                            'device': 0,
                            'jid': jidEncode(ownUserForAddressing, ownUserServer, 0)
                        }))
                    if _p(additionalAttributes, 'category') != 'peer':
                        devices.clear()
                        if isLid and _jsTruthy(meLid):
                            senderIdentity = jidEncode(jidDecode(meLid)['user'], 'lid', None)
                        else:
                            senderIdentity = jidEncode(jidDecode(meId)['user'], 's.whatsapp.net', None)
                        sessionDevices = await getUSyncDevices([senderIdentity, jid], True, False)
                        devices.extend(sessionDevices)
                        logger.debug(JSObject({
                            'deviceCount': len(devices),
                            'devices': [f"{d.get('user')}:{_numStr(d.get('device'))}@{_p(jidDecode(d.get('jid')), 'server')}" for d in devices]
                        }), 'Device enumeration complete with unified addressing')
                allRecipients = []
                meRecipients = []
                otherRecipients = []
                mePnUser = jidDecode(meId)['user']
                meLidUser = jidDecode(meLid)['user'] if _jsTruthy(meLid) else None
                for device in devices:
                    deviceUser = device.get('user')
                    deviceJid = device.get('jid')
                    isExactSenderDevice = deviceJid == meId or (_jsTruthy(meLid) and deviceJid == meLid)
                    if isExactSenderDevice:
                        logger.debug(JSObject({'jid': deviceJid, 'meId': meId, 'meLid': meLid}), 'Skipping exact sender device (whatsmeow pattern)')
                        continue
                    isMe = deviceUser == mePnUser or (meLidUser is not None and deviceUser == meLidUser)
                    if isMe:
                        meRecipients.append(deviceJid)
                    else:
                        otherRecipients.append(deviceJid)
                    allRecipients.append(deviceJid)
                await assertSessions(allRecipients)
                meResult, otherResult = await asyncio.gather(
                    createParticipantNodes(meRecipients, meMsg, extraAttrs),
                    createParticipantNodes(otherRecipients, message, extraAttrs, meMsg)
                )
                participants.extend(meResult.get('nodes'))
                participants.extend(otherResult.get('nodes'))
                if len(meRecipients) > 0 or len(otherRecipients) > 0:
                    extraAttrs['phash'] = generateParticipantHashV2([*meRecipients, *otherRecipients])
                shouldIncludeDeviceIdentity = shouldIncludeDeviceIdentity or meResult.get('shouldIncludeDeviceIdentity') or otherResult.get('shouldIncludeDeviceIdentity')

            if isRetryResend:
                isParticipantLid = isLidUser(participant.get('jid'))
                isMe = areJidsSameUser(participant.get('jid'), meLid if isParticipantLid else meId)
                messageToSend = message
                if isGroupOrStatus:
                    groupSenderIdentity = None
                    if _jsTruthy(meLid) and await _awaitValue(signalRepository.get('hasSenderKey')(JSObject({'group': destinationJid, 'meId': meLid}))):
                        groupSenderIdentity = meLid
                    elif await _awaitValue(signalRepository.get('hasSenderKey')(JSObject({'group': destinationJid, 'meId': meId}))):
                        groupSenderIdentity = meId
                    if groupSenderIdentity is not None:
                        try:
                            skdm = await _awaitValue(signalRepository.get('getSenderKeyDistributionMessage')(JSObject({
                                'group': destinationJid,
                                'meId': groupSenderIdentity
                            })))
                            messageToSend = JSObject({
                                **message,
                                'senderKeyDistributionMessage': JSObject({
                                    'groupId': destinationJid,
                                    'axolotlSenderKeyDistributionMessage': skdm
                                })
                            })
                        except Exception as err:
                            logger.warn(JSObject({'err': err, 'jid': destinationJid}), 'failed to build SKDM for retry, sending without it')
                if isMe:
                    encodedMessageToSend = encodeWAMessage(JSObject({
                        'deviceSentMessage': JSObject({
                            'destinationJid': destinationJid,
                            'message': messageToSend
                        })
                    }))
                else:
                    encodedMessageToSend = encodeWAMessage(messageToSend)
                encrypted = await _awaitValue(signalRepository.get('encryptMessage')(JSObject({
                    'data': encodedMessageToSend,
                    'jid': participant.get('jid')
                })))
                binaryNodeContent.append(JSObject({
                    'tag': 'enc',
                    'attrs': JSObject({
                        'v': '2',
                        'type': encrypted.get('type'),
                        'count': _numStr(participant.get('count'))
                    }),
                    'content': encrypted.get('ciphertext')
                }))

            if len(participants):
                if _p(additionalAttributes, 'category') == 'peer':
                    firstContent = _p(participants[0], 'content')
                    peerNode = firstContent[0] if (firstContent is not None and len(firstContent)) else None
                    if peerNode is not None:
                        binaryNodeContent.append(peerNode)
                else:
                    binaryNodeContent.append(JSObject({
                        'tag': 'participants',
                        'attrs': JSObject({}),
                        'content': participants
                    }))

            stanza = JSObject({
                'tag': 'message',
                'attrs': JSObject({
                    'id': msgId,
                    'to': destinationJid,
                    'type': getMessageType(innerMessage),
                    **_or(additionalAttributes, JSObject({}))
                }),
                'content': binaryNodeContent
            })

            if _jsTruthy(participant):
                if isJidGroup(destinationJid):
                    stanza.get('attrs')['to'] = destinationJid
                    stanza.get('attrs')['participant'] = participant.get('jid')
                elif areJidsSameUser(participant.get('jid'), meId):
                    stanza.get('attrs')['to'] = participant.get('jid')
                    stanza.get('attrs')['recipient'] = destinationJid
                else:
                    stanza.get('attrs')['to'] = participant.get('jid')
            else:
                stanza.get('attrs')['to'] = destinationJid

            if shouldIncludeDeviceIdentity:
                stanza.get('content').append(JSObject({
                    'tag': 'device-identity',
                    'attrs': JSObject({}),
                    'content': encodeSignedDeviceIdentity(authState.get('creds').get('account'), True)
                }))
                logger.debug(JSObject({'jid': jid}), 'adding device identity')

            if (not isNewsletter and
                    not isRetryResend and
                    _jsTruthy(_p(_p(reportingMessage, 'messageContextInfo'), 'messageSecret')) and
                    shouldIncludeReportingToken(reportingMessage)):
                try:
                    encoded = encodeWAMessage(reportingMessage)
                    reportingKey = JSObject({
                        'id': msgId,
                        'fromMe': True,
                        'remoteJid': destinationJid,
                        'participant': _p(participant, 'jid')
                    })
                    reportingNode = await _awaitValue(getMessageReportingToken(encoded, reportingMessage, reportingKey))
                    if reportingNode is not None:
                        stanza.get('content').append(reportingNode)
                        logger.trace(JSObject({'jid': jid}), 'added reporting token to message')
                except Exception as error:
                    logger.warn(JSObject({'jid': jid, 'trace': traceback.format_exc()}), 'failed to attach reporting token')

            isPeerMessage = _p(additionalAttributes, 'category') == 'peer'
            is1on1Send = not isGroup and not isRetryResend and not isStatus and not isNewsletter and not isPeerMessage

            tcTokenJid = await resolveTcTokenJid(destinationJid, getLIDForPN) if is1on1Send else destinationJid
            contactTcTokenData = await _awaitValue(authState.get('keys').get('get')('tctoken', [tcTokenJid])) if is1on1Send else JSObject({})
            existingTokenEntry = contactTcTokenData[tcTokenJid]
            tcTokenBuffer = _p(existingTokenEntry, 'token')

            if tcTokenBuffer is not None and len(tcTokenBuffer) > 0 and isTcTokenExpired(existingTokenEntry.get('timestamp')):
                logger.debug(JSObject({'jid': destinationJid, 'timestamp': existingTokenEntry.get('timestamp')}), 'tctoken expired, clearing')
                tcTokenBuffer = None
                cleared = JSObject({'token': b'', 'senderTimestamp': existingTokenEntry.get('senderTimestamp')}) if existingTokenEntry.get('senderTimestamp') is not None else None
                try:
                    await _awaitValue(authState.get('keys').get('set')(JSObject({'tctoken': JSObject({tcTokenJid: cleared})})))
                except Exception as err:
                    logger.debug(JSObject({'jid': destinationJid, 'err': _errMessage(err)}), 'failed to persist tctoken expiry cleanup')
            if tcTokenBuffer is not None and len(tcTokenBuffer) > 0 and _jsTruthy(sock.get('serverProps').get('privacyTokenOn1to1')):
                stanza.get('content').append(JSObject({
                    'tag': 'tctoken',
                    'attrs': JSObject({}),
                    'content': tcTokenBuffer
                }))

            alreadyHasBizNode = False
            if additionalNodes is not None and len(additionalNodes) > 0:
                stanza.get('content').extend(additionalNodes)
                alreadyHasBizNode = (not _jsTruthy(addBizAttributes)) and any(n.get('tag') == 'biz' for n in additionalNodes)
            if (not alreadyHasBizNode and shouldIncludeBizBinaryNode(innerMessage)) or _jsTruthy(addBizAttributes):
                stanza.get('content').append(getBizBinaryNode(innerMessage))

            logger.debug(JSObject({'msgId': msgId}), f'sending message to {len(participants)} devices')
            await _awaitValue(sendNode(stanza))

            isProtocolMsg = _jsTruthy(_p(innerMessage, 'protocolMessage'))
            isBotOrPSA = destinationJid == PSA_WID or isJidBot(destinationJid) or isJidMetaAI(destinationJid)
            if (is1on1Send and
                    not isProtocolMsg and
                    not isBotOrPSA and
                    shouldSendNewTcToken(_p(existingTokenEntry, 'senderTimestamp')) and
                    tcTokenJid not in inFlightTcTokenIssuance):
                inFlightTcTokenIssuance.add(tcTokenJid)
                issueTimestamp = unixTimestampSeconds()
                getPNForLID = signalRepository.get('lidMapping').get('getPNForLID')

                async def issueTcToken():
                    try:
                        issueJid = await resolveIssuanceJid(destinationJid, sock.get('serverProps').get('lidTrustedTokenIssueToLid'), getLIDForPN, getPNForLID)
                        result = await _awaitValue(issuePrivacyTokens([issueJid], issueTimestamp))
                        await storeTcTokensFromIqResult(JSObject({
                            'result': result,
                            'fallbackJid': tcTokenJid,
                            'keys': authState.get('keys'),
                            'getLIDForPN': getLIDForPN
                        }))
                        currentData = await _awaitValue(authState.get('keys').get('get')('tctoken', [tcTokenJid]))
                        currentEntry = currentData[tcTokenJid]
                        indexWrite = await buildMergedTcTokenIndexWrite(authState.get('keys'), [tcTokenJid])
                        entry = JSObject({'token': b''})
                        if currentEntry is not None:
                            entry.update(currentEntry)
                        entry['senderTimestamp'] = issueTimestamp
                        tctokenWrite = JSObject({tcTokenJid: entry})
                        tctokenWrite.update(indexWrite)
                        await _awaitValue(authState.get('keys').get('set')(JSObject({'tctoken': tctokenWrite})))
                    except Exception as err:
                        logger.debug(JSObject({'jid': destinationJid, 'err': _errMessage(err)}), 'fire-and-forget tctoken issuance failed')
                    finally:
                        inFlightTcTokenIssuance.discard(tcTokenJid)

                asyncio.ensure_future(issueTcToken())

            if messageRetryManager is not None and not _jsTruthy(participant):
                messageRetryManager.addRecentMessage(destinationJid, msgId, message)

        await _awaitValue(authState.get('keys').get('transaction')(transactionWork, meId))
        return msgId

    async def issuePrivacyTokens(jids, timestamp=None):
        t = str(timestamp if timestamp is not None else unixTimestampSeconds())
        result = await _awaitValue(query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({
                'to': S_WHATSAPP_NET,
                'type': 'set',
                'xmlns': 'privacy'
            }),
            'content': [
                JSObject({
                    'tag': 'tokens',
                    'attrs': JSObject({}),
                    'content': [
                        JSObject({
                            'tag': 'token',
                            'attrs': JSObject({
                                'jid': jidNormalizedUser(jid),
                                't': t,
                                'type': 'trusted_contact'
                            })
                        })
                        for jid in jids
                    ]
                })
            ]
        })))
        return result

    waUploadToServer = getWAUploadToServer(config, refreshMediaConn)
    waitForMsgMediaUpdate = bindWaitForEvent(ev, 'messages.media-update')

    def onSocketEnd(*args):
        if _p(config, 'userDevicesCache') is None and _hasMember(userDevicesCache, 'close'):
            _m(userDevicesCache, 'close')()
        mediaConn[0] = None
        if messageRetryManager is not None:
            messageRetryManager.clear()

    registerSocketEndHandler(onSocketEnd)

    async def updateMediaMessage(message):
        content = assertMediaContent(message.get('message'))
        mediaKey = content.get('mediaKey')
        meId = authState.get('creds').get('me').get('id')
        node = encryptMediaRetryRequest(message.get('key'), mediaKey, meId)
        error = [None]

        async def handleMediaUpdate(update):
            result = next((c for c in update if c.get('key').get('id') == message.get('key').get('id')), None)
            if result is not None:
                if _jsTruthy(result.get('error')):
                    error[0] = result.get('error')
                else:
                    try:
                        media = decryptMediaRetryData(result.get('media'), mediaKey, result.get('key').get('id'))
                        if media.get('result') != proto.MediaRetryNotification.ResultType.SUCCESS:
                            resultStr = proto.MediaRetryNotification.ResultType[media.get('result')]
                            raise Boom(
                                f'Media re-upload failed by device ({resultStr})',
                                JSObject({
                                    'data': media,
                                    'statusCode': _or(getStatusCodeForMediaRetry(media.get('result')), 404)
                                })
                            )
                        content['directPath'] = media.get('directPath')
                        content['url'] = getUrlFromDirectPath(content.get('directPath'), mediaHost[0])
                        logger.debug(JSObject({'directPath': media.get('directPath'), 'key': result.get('key')}), 'media update successful')
                    except Exception as err:
                        error[0] = err
                return True
            return None

        await asyncio.gather(
            _awaitValue(sendNode(node)),
            _awaitValue(waitForMsgMediaUpdate(handleMediaUpdate, None))
        )
        if error[0] is not None:
            raise error[0]
        _m(ev, 'emit')('messages.update', [JSObject({'key': message.get('key'), 'update': JSObject({'message': message.get('message')})})])
        return message

    def setPatchMessageBeforeSending(fn):
        patchBox[0] = fn

    def getPatchMessageBeforeSending():
        return patchBox[0]

    def getMediaHost():
        return mediaHost[0]

    def emitOwnLater(message, kind):
        async def upsertOwn():
            await _awaitValue(messageMutex.get('mutex')(lambda: upsertMessage(message, kind)))
        nextTick(lambda: _spawn(upsertOwn))

    async def sendMessage(jid, content, options=None):
        if options is None:
            options = JSObject({})
        userJid = authState.get('creds').get('me').get('id')
        if isinstance(jid, list):
            delayMs = _nullish(options.get('delayMs'), 1500)
            allUsers = {}
            fullMsg = await generateWAMessage('status@broadcast', content, JSObject({
                'logger': logger,
                'userJid': userJid,
                'upload': waUploadToServer,
                'mediaCache': config.get('mediaCache'),
                'options': config.get('options'),
                'messageId': generateMessageIDV2(userJid),
                **options
            }))
            for id_ in jid:
                if isJidGroup(id_):
                    try:
                        cached = await _awaitValue(cachedGroupMetadata(id_)) if cachedGroupMetadata is not None else None
                        groupData = cached if _jsTruthy(cached) else await _awaitValue(groupMetadata(id_))
                        for participant in groupData.get('participants'):
                            if participant.get('id') in allUsers:
                                continue
                            allUsers[participant.get('id')] = True
                    except Exception as error:
                        logger.error(f'Error getting metadata group from {id_}: {error}')
                elif id_ not in allUsers:
                    allUsers[id_] = True

            await relayMessage(STATUS_JID, fullMsg.get('message'), JSObject({
                'messageId': fullMsg.get('key').get('id'),
                'statusJidList': list(dict.keys(allUsers)),
                'additionalNodes': [
                    JSObject({
                        'tag': 'meta',
                        'attrs': JSObject({}),
                        'content': [
                            JSObject({
                                'tag': 'mentioned_users',
                                'attrs': JSObject({}),
                                'content': [
                                    JSObject({
                                        'tag': 'to',
                                        'attrs': JSObject({'jid': id_}),
                                        'content': None
                                    })
                                    for id_ in jid
                                ]
                            })
                        ]
                    })
                ]
            }))
            if _jsTruthy(config.get('emitOwnEvents')):
                emitOwnLater(fullMsg, 'append')

            for id_ in jid:
                isGroup = isJidGroup(id_)
                sendType = 'groupStatusMentionMessage' if isGroup else 'statusMentionMessage'
                mentionMsg = generateWAMessageFromContent(id_, JSObject({
                    'messageContextInfo': JSObject({
                        'messageSecret': randomBytes(32)
                    }),
                    sendType: JSObject({
                        'message': JSObject({
                            'protocolMessage': JSObject({
                                'key': fullMsg.get('key'),
                                'type': 25
                            })
                        })
                    })
                }), JSObject({'userJid': userJid}))
                await relayMessage(id_, mentionMsg.get('message'), JSObject({
                    'additionalNodes': [
                        JSObject({
                            'tag': 'meta',
                            'attrs': JSObject({'is_group_status_mention': 'true'}) if isGroup else JSObject({'is_status_mention': 'true'}),
                            'content': None
                        })
                    ]
                }))
                if _jsTruthy(config.get('emitOwnEvents')):
                    emitOwnLater(mentionMsg, 'append')
                await delay(delayMs)
            return fullMsg

        if 'disappearingMessagesInChat' in content and content.get('disappearingMessagesInChat') is not None and isJidGroup(jid):
            disappearingMessagesInChat = content.get('disappearingMessagesInChat')
            if isinstance(disappearingMessagesInChat, bool):
                value = WA_DEFAULT_EPHEMERAL if disappearingMessagesInChat else 0
            else:
                value = disappearingMessagesInChat
            await _awaitValue(groupToggleEphemeral(jid, value))
            return None

        fullMsg = await generateWAMessage(jid, content, JSObject({
            'logger': logger,
            'userJid': userJid,
            'getUrlInfo': lambda text: getUrlInfo(text, JSObject({
                'thumbnailWidth': linkPreviewImageThumbnailWidth,
                'fetchOpts': JSObject({
                    'timeout': 3000,
                    **_or(httpRequestOptions, JSObject({}))
                }),
                'logger': logger,
                'uploadImage': waUploadToServer if _jsTruthy(generateHighQualityLinkPreview) else None
            })),
            'getProfilePicUrl': sock.get('profilePictureUrl'),
            'getCallLink': sock.get('createCallLink'),
            'upload': waUploadToServer,
            'mediaCache': config.get('mediaCache'),
            'options': config.get('options'),
            **options,
            'messageId': generateMessageIDV2(userJid)
        }))
        isNewsletter = isJidNewsletter(jid)
        isEventMsg = 'event' in content and _jsTruthy(content.get('event'))
        isDeleteMsg = 'delete' in content and _jsTruthy(content.get('delete'))
        isEditMsg = 'edit' in content and _jsTruthy(content.get('edit'))
        isPinMsg = 'pin' in content and _jsTruthy(content.get('pin'))
        isKeepMsg = 'keep' in content and _jsTruthy(content.get('keep'))
        isPollMsg = 'poll' in content and _jsTruthy(content.get('poll'))
        isQuizMsg = 'poll' in content and _jsTruthy(_p(content.get('poll'), 'pollType'))
        isAiMsg = 'ai' in content and _jsTruthy(content.get('ai'))
        isNeedBizAttrs = 'secureMetaServiceLabel' in content and _jsTruthy(content.get('secureMetaServiceLabel'))
        additionalAttributes = _or(options.get('additionalAttributes'), JSObject({}))
        additionalNodes = _or(options.get('additionalNodes'), [])

        if isDeleteMsg or isKeepMsg:
            remoteJidOfDelete = _p(content.get('delete'), 'remoteJid')
            if _jsTruthy(remoteJidOfDelete) and isJidGroup(remoteJidOfDelete) and not _jsTruthy(_p(content.get('delete'), 'fromMe')):
                additionalAttributes['edit'] = '8'
            else:
                additionalAttributes['edit'] = '7'
        elif isEditMsg:
            additionalAttributes['edit'] = '3' if isNewsletter else '1'
        elif isPinMsg:
            additionalAttributes['edit'] = '2'
        elif isPollMsg:
            if not isNewsletter and isQuizMsg:
                raise Boom('Quiz are only allowed for newsletter', JSObject({'statusCode': 400}))
            pollAttrs = JSObject({'polltype': 'quiz_creation' if isQuizMsg else 'creation'})
            if isPollMsg and isNewsletter:
                pollAttrs['contenttype'] = 'text'
            additionalNodes.append(JSObject({
                'tag': 'meta',
                'attrs': pollAttrs,
                'content': None
            }))
        elif isEventMsg:
            additionalNodes.append(JSObject({
                'tag': 'meta',
                'attrs': JSObject({'event_type': 'creation'}),
                'content': None
            }))
        elif isAiMsg:
            if not (isPnUser(jid) or isLidUser(jid)):
                raise Boom('AI icon on message are only allowed in private chat', JSObject({'statusCode': 400}))
            if _jsTruthy(_p(fullMsg.get('message'), 'messageContextInfo')):
                fullMsg.get('message').get('messageContextInfo')['supportPayload'] = BIZ_BOT_SUPPORT_PAYLOAD
            additionalNodes.append(JSObject({
                'tag': 'bot',
                'attrs': JSObject({'biz_bot': '1'}),
                'content': None
            }))
            dict.pop(content, 'ai', None)

        await relayMessage(jid, fullMsg.get('message'), JSObject({
            'messageId': fullMsg.get('key').get('id'),
            'useCachedGroupMetadata': options.get('useCachedGroupMetadata'),
            'addBizAttributes': isNeedBizAttrs,
            'statusJidList': options.get('statusJidList'),
            'additionalAttributes': additionalAttributes,
            'additionalNodes': additionalNodes
        }))

        if messageRetryManager is not None and (isDeleteMsg or isEditMsg):
            targetKey = content.get('delete') if isDeleteMsg else content.get('edit')
            if _jsTruthy(_p(targetKey, 'id')):
                messageRetryManager.invalidateMessage(_or(_p(targetKey, 'remoteJid'), jid), targetKey.get('id'))
        if _jsTruthy(config.get('emitOwnEvents')):
            emitOwnLater(fullMsg, 'append')

        if 'album' in content:
            delayMs = _nullish(options.get('delayMs'), 1500)
            for albumMedia in content.get('album'):
                albumMsg = await generateWAMessage(jid, albumMedia, JSObject({
                    'logger': logger,
                    'userJid': userJid,
                    'upload': waUploadToServer,
                    'mediaCache': config.get('mediaCache'),
                    'options': config.get('options'),
                    **options,
                    'messageId': generateMessageIDV2(userJid)
                }))
                if not hasValidAlbumMedia(normalizeMessageContent(albumMsg.get('message'))):
                    raise Boom('Invalid message type for album', JSObject({'statusCode': 400}))
                if not _jsTruthy(albumMsg.get('message').get('messageContextInfo')):
                    albumMsg.get('message')['messageContextInfo'] = JSObject({})
                albumMsg.get('message').get('messageContextInfo')['messageAssociation'] = JSObject({
                    'parentMessageKey': fullMsg.get('key'),
                    'associationType': AssociationType.MEDIA_ALBUM
                })
                await relayMessage(jid, albumMsg.get('message'), JSObject({
                    'messageId': albumMsg.get('key').get('id'),
                    'useCachedGroupMetadata': options.get('useCachedGroupMetadata'),
                    'addBizAttributes': isNeedBizAttrs,
                    'statusJidList': options.get('statusJidList'),
                    'additionalAttributes': additionalAttributes,
                    'additionalNodes': additionalNodes
                }))
                if _jsTruthy(config.get('emitOwnEvents')):
                    emitOwnLater(albumMsg, 'append')
                await delay(delayMs)
        return fullMsg

    fullSock = JSObject({
        **sock,
        'userDevicesCache': userDevicesCache,
        'devicesMutex': devicesMutex,
        'issuePrivacyTokens': issuePrivacyTokens,
        'assertSessions': assertSessions,
        'relayMessage': relayMessage,
        'sendReceipt': sendReceipt,
        'sendReceipts': sendReceipts,
        'readMessages': readMessages,
        'refreshMediaConn': refreshMediaConn,
        'getMediaHost': getMediaHost,
        'waUploadToServer': waUploadToServer,
        'fetchPrivacySettings': fetchPrivacySettings,
        'sendPeerDataOperationMessage': sendPeerDataOperationMessage,
        'createParticipantNodes': createParticipantNodes,
        'getUSyncDevices': getUSyncDevices,
        'messageRetryManager': messageRetryManager,
        'updateMemberLabel': updateMemberLabel,
        'getPatchMessageBeforeSending': getPatchMessageBeforeSending,
        'setPatchMessageBeforeSending': setPatchMessageBeforeSending,
        'updateMediaMessage': updateMediaMessage,
        'sendMessage': sendMessage
    })
    fullSock['aiRich'] = lambda: AIRichBuilder(fullSock)
    return fullSock
