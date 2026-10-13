import asyncio

from baileys.WAProto import proto
from baileys.binary.wa_binary import (
    getBinaryNodeChild,
    getBinaryNodeChildren,
    isHostedLidUser,
    isHostedPnUser,
    isLidUser,
    isPnUser,
    isTruthy,
    jidDecode,
    jidNormalizedUser,
    jsNumber,
    reduceBinaryNodeToDictionary,
    S_WHATSAPP_NET
)
from baileys.constants import ALL_WA_PATCH_NAMES, SyncState
from baileys.defaults import DEFAULT_CACHE_TTLS, HISTORY_SYNC_PAUSED_TIMEOUT_MS, PROCESSABLE_HISTORY_TYPES
from baileys.foundation.boom import Boom
from baileys.foundation.cache import Cache
from baileys.foundation.concurrency import makeMutex
from baileys.foundation.node_runtime import JSObject, clearTimeout, setTimeout
from baileys.socket.socket_core import makeSocket, orValue, getp, spawnCatch, spawnTask, asyncListener, spreadSocket, textOf, tpl
from baileys.socket.usync import USyncQuery, USyncUser
from baileys.utils.auth_state_core import buildTcTokenFromJid
from baileys.utils.chat_sync import (
    chatModificationToAppPatch,
    decodePatches,
    decodeSyncdSnapshot,
    encodeSyncdPatch,
    ensureLTHashStateVersion,
    extractSyncdPatches,
    isAppStateSyncIrrecoverable,
    isMissingKeyError,
    MAX_SYNC_ATTEMPTS,
    newLTHashState,
    processSyncAction
)
from baileys.utils.media import generateProfilePicture
from baileys.utils.message_processing import getHistoryMsg, processMessage


def buildProfilePictureQueryContent(type_, tcTokenContent):
    picture = JSObject({'tag': 'picture', 'attrs': JSObject({'type': type_, 'query': 'url'})})
    if tcTokenContent is not None and len(tcTokenContent):
        picture['content'] = tcTokenContent
    return [picture]


def makeChatsSocket(config):
    logger = config.get('logger')
    markOnlineOnConnect = config.get('markOnlineOnConnect')
    fireInitQueries = config.get('fireInitQueries')
    appStateMacVerification = config.get('appStateMacVerification')
    shouldIgnoreJid = config.get('shouldIgnoreJid')
    shouldSyncHistoryMessage = config.get('shouldSyncHistoryMessage')
    getMessage = config.get('getMessage')
    sock = makeSocket(config)
    ev = sock.get('ev')
    ws = sock.get('ws')
    authState = sock.get('authState')
    generateMessageTag = sock.get('generateMessageTag')
    sendNode = sock.get('sendNode')
    query = sock.get('query')
    signalRepository = sock.get('signalRepository')
    onUnexpectedError = sock.get('onUnexpectedError')
    sendUnifiedSession = sock.get('sendUnifiedSession')
    registerSocketEndHandler = sock.get('registerSocketEndHandler')
    creds = authState.get('creds')
    keys = authState.get('keys')
    getLIDForPN = signalRepository.get('lidMapping').getLIDForPN
    privacySettings = None

    serverProps = JSObject({
        'privacyTokenOn1to1': True,
        'profilePicPrivacyToken': True,
        'lidTrustedTokenIssueToLid': False
    })
    syncState = SyncState['Connecting']
    messageMutex = makeMutex()
    receiptMutex = makeMutex()
    appStatePatchMutex = makeMutex()
    notificationMutex = makeMutex()
    awaitingSyncTimeout = None
    historySyncStatus = JSObject({'initialBootstrapComplete': False, 'recentSyncComplete': False})
    historySyncPausedTimeout = None
    blockedCollections = {}
    placeholderResendCache = orValue(config.get('placeholderResendCache'), Cache(JSObject({'ttl': DEFAULT_CACHE_TTLS['MSG_RETRY'] * 1000})))

    async def getAppStateSyncKey(keyId):
        result = await keys.get('get')('app-state-sync-key', [keyId])
        return result[keyId]

    async def fetchPrivacySettings(force=False):
        nonlocal privacySettings
        if privacySettings is None or force:
            result = await query(JSObject({'tag': 'iq', 'attrs': JSObject({'xmlns': 'privacy', 'to': S_WHATSAPP_NET, 'type': 'get'}), 'content': [JSObject({'tag': 'privacy', 'attrs': JSObject({})})]}))
            content = result.get('content')
            first = content[0] if isinstance(content, list) and len(content) else None
            privacySettings = reduceBinaryNodeToDictionary(first, 'category')
        return privacySettings

    async def privacyQuery(name, value):
        await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'xmlns': 'privacy', 'to': S_WHATSAPP_NET, 'type': 'set'}),
            'content': [JSObject({'tag': 'privacy', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'category', 'attrs': JSObject({'name': name, 'value': value})})]})]
        }))

    def makePrivacySetter(name):
        async def setter(value):
            await privacyQuery(name, value)
        return setter

    updateMessagesPrivacy = makePrivacySetter('messages')
    updateCallPrivacy = makePrivacySetter('calladd')
    updateLastSeenPrivacy = makePrivacySetter('last')
    updateOnlinePrivacy = makePrivacySetter('online')
    updateProfilePicturePrivacy = makePrivacySetter('profile')
    updateStatusPrivacy = makePrivacySetter('status')
    updateReadReceiptsPrivacy = makePrivacySetter('readreceipts')
    updateGroupsAddPrivacy = makePrivacySetter('groupadd')

    async def updateDefaultDisappearingMode(duration):
        await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'xmlns': 'disappearing_mode', 'to': S_WHATSAPP_NET, 'type': 'set'}),
            'content': [JSObject({'tag': 'disappearing_mode', 'attrs': JSObject({'duration': str(duration)})})]
        }))

    async def getBotListV2():
        resp = await query(JSObject({'tag': 'iq', 'attrs': JSObject({'xmlns': 'bot', 'to': S_WHATSAPP_NET, 'type': 'get'}), 'content': [JSObject({'tag': 'bot', 'attrs': JSObject({'v': '2'})})]}))
        botNode = getBinaryNodeChild(resp, 'bot')
        botList = []
        for section in getBinaryNodeChildren(botNode, 'section'):
            if section.get('attrs').get('type') == 'all':
                for bot in getBinaryNodeChildren(section, 'bot'):
                    botList.append(JSObject({'jid': bot.get('attrs').get('jid'), 'personaId': bot.get('attrs').get('persona_id')}))
        return botList

    async def fetchStatus(*jids):
        usyncQuery = USyncQuery().withStatusProtocol()
        for jid in jids:
            usyncQuery.withUser(USyncUser().withId(jid))
        result = await sock.get('executeUSyncQuery')(usyncQuery)
        if result is not None:
            return result.get('list')
        return None

    async def fetchDisappearingDuration(*jids):
        usyncQuery = USyncQuery().withDisappearingModeProtocol()
        for jid in jids:
            usyncQuery.withUser(USyncUser().withId(jid))
        result = await sock.get('executeUSyncQuery')(usyncQuery)
        if result is not None:
            return result.get('list')
        return None

    async def findUserId(pnLid):
        normalizedJid = jidNormalizedUser(pnLid)
        userId = JSObject({'lid': None, 'phoneNumber': None})
        if isPnUser(normalizedJid) or isHostedPnUser(normalizedJid):
            userId['phoneNumber'] = normalizedJid
            lids = await signalRepository.get('lidMapping').getLIDsForPNs([normalizedJid])
            first = lids[0] if lids else None
            userId['lid'] = jidNormalizedUser(getp(first, 'lid'))
        elif isLidUser(normalizedJid) or isHostedLidUser(normalizedJid):
            userId['lid'] = normalizedJid
            pns = await signalRepository.get('lidMapping').getPNsForLIDs([normalizedJid])
            first = pns[0] if pns else None
            userId['phoneNumber'] = jidNormalizedUser(getp(first, 'pn'))
        else:
            raise Boom('Invalid id input to find user ids', JSObject({'statusCode': 400}))
        return userId

    async def updateProfilePicture(jid, content, dimensions=None):
        if not isTruthy(jid):
            raise Boom('Illegal no-jid profile update. Please specify either your ID or the ID of the chat you wish to update')
        targetJid = jidNormalizedUser(jid) if jidNormalizedUser(jid) != jidNormalizedUser(authState.get('creds').get('me').get('id')) else None
        generated = await generateProfilePicture(content, dimensions)
        attrs = JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'w:profile:picture'})
        if isTruthy(targetJid):
            attrs['target'] = targetJid
        await query(JSObject({
            'tag': 'iq',
            'attrs': attrs,
            'content': [JSObject({'tag': 'picture', 'attrs': JSObject({'type': 'image'}), 'content': generated.get('img')})]
        }))

    async def removeProfilePicture(jid):
        if not isTruthy(jid):
            raise Boom('Illegal no-jid profile update. Please specify either your ID or the ID of the chat you wish to update')
        targetJid = jidNormalizedUser(jid) if jidNormalizedUser(jid) != jidNormalizedUser(authState.get('creds').get('me').get('id')) else None
        attrs = JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'w:profile:picture'})
        if isTruthy(targetJid):
            attrs['target'] = targetJid
        await query(JSObject({'tag': 'iq', 'attrs': attrs}))

    async def updateProfileStatus(status):
        await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'status'}),
            'content': [JSObject({'tag': 'status', 'attrs': JSObject({}), 'content': status.encode('utf-8')})]
        }))

    async def updateProfileName(name):
        return await chatModify(JSObject({'pushNameSetting': name}), '')

    async def fetchBlocklist():
        result = await query(JSObject({'tag': 'iq', 'attrs': JSObject({'xmlns': 'blocklist', 'to': S_WHATSAPP_NET, 'type': 'get'})}))
        listNode = getBinaryNodeChild(result, 'list')
        return [n.get('attrs').get('jid') for n in getBinaryNodeChildren(listNode, 'item')]

    async def updateBlockStatus(jid, action):
        normalizedJid = jidNormalizedUser(jid)
        lid = None
        pn_jid = None
        if isLidUser(normalizedJid) or isHostedLidUser(normalizedJid):
            lid = normalizedJid
            if action == 'block':
                pn = (await findUserId(normalizedJid))['phoneNumber']
                if not isTruthy(pn):
                    raise Boom(f'Unable to resolve PN JID for LID: {jid}', JSObject({'statusCode': 400}))
                pn_jid = jidNormalizedUser(pn)
        elif isPnUser(normalizedJid) or isHostedPnUser(normalizedJid):
            mapped = (await findUserId(normalizedJid))['lid']
            if not isTruthy(mapped):
                raise Boom(f'Unable to resolve LID for PN JID: {jid}', JSObject({'statusCode': 400}))
            lid = mapped
            if action == 'block':
                pn_jid = jidNormalizedUser(normalizedJid)
        else:
            raise Boom(f'Invalid jid: {jid}', JSObject({'statusCode': 400}))
        itemAttrs = JSObject({'action': action, 'jid': lid})
        if action == 'block':
            if not isTruthy(pn_jid):
                raise Boom(f'pn_jid required for block: {jid}', JSObject({'statusCode': 400}))
            itemAttrs['pn_jid'] = pn_jid
        await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'xmlns': 'blocklist', 'to': S_WHATSAPP_NET, 'type': 'set'}),
            'content': [JSObject({'tag': 'item', 'attrs': itemAttrs})]
        }))

    async def getBusinessProfile(jid):
        results = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': 's.whatsapp.net', 'xmlns': 'w:biz', 'type': 'get'}),
            'content': [JSObject({'tag': 'business_profile', 'attrs': JSObject({'v': '244'}), 'content': [JSObject({'tag': 'profile', 'attrs': JSObject({'jid': jid})})]})]
        }))
        profileNode = getBinaryNodeChild(results, 'business_profile')
        profiles = getBinaryNodeChild(profileNode, 'profile')
        if profiles is not None:
            address = getBinaryNodeChild(profiles, 'address')
            description = getBinaryNodeChild(profiles, 'description')
            website = getBinaryNodeChild(profiles, 'website')
            email = getBinaryNodeChild(profiles, 'email')
            category = getBinaryNodeChild(getBinaryNodeChild(profiles, 'categories'), 'category')
            businessHours = getBinaryNodeChild(profiles, 'business_hours')
            businessHoursConfig = getBinaryNodeChildren(businessHours, 'business_hours_config') if businessHours is not None else None
            websiteStr = textOf(website.get('content')) if website is not None else None
            return JSObject({
                'wid': getp(profiles.get('attrs'), 'jid'),
                'address': textOf(address.get('content')) if address is not None else None,
                'description': orValue(textOf(description.get('content')) if description is not None else None, ''),
                'website': [websiteStr] if isTruthy(websiteStr) else [],
                'email': textOf(email.get('content')) if email is not None else None,
                'category': textOf(category.get('content')) if category is not None else None,
                'business_hours': JSObject({
                    'timezone': getp(getp(businessHours, 'attrs'), 'timezone'),
                    'business_config': [c.get('attrs') for c in businessHoursConfig] if businessHoursConfig is not None else None
                })
            })
        return None

    async def cleanDirtyBits(type_, fromTimestamp=None):
        logger.info(JSObject({'fromTimestamp': fromTimestamp}), 'clean dirty bits ' + type_)
        attrs = JSObject({'type': type_})
        if isTruthy(fromTimestamp):
            attrs['timestamp'] = str(fromTimestamp)
        await sendNode(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'urn:xmpp:whatsapp:dirty', 'id': generateMessageTag()}),
            'content': [JSObject({'tag': 'clean', 'attrs': attrs})]
        }))

    def newAppStateChunkHandler(isInitialSync):
        def onMutation(mutation):
            processSyncAction(mutation, ev, authState.get('creds').get('me'), JSObject({'accountSettings': creds.get('accountSettings')}) if isInitialSync else None, logger)
        return JSObject({'onMutation': onMutation})

    async def resyncAppStateWork(collections, isInitialSync):
        appStateSyncKeyCache = {}

        async def getCachedAppStateSyncKey(keyId):
            if keyId in appStateSyncKeyCache:
                return appStateSyncKeyCache[keyId]
            key = await getAppStateSyncKey(keyId)
            appStateSyncKeyCache[keyId] = key
            return key

        initialVersionMap = {}
        globalMutationMap = {}

        async def transactionWork():
            collectionsToHandle = dict.fromkeys(collections)
            attemptsMap = {}
            forceSnapshotCollections = set()
            while len(collectionsToHandle):
                states = {}
                nodes = []
                for name in list(collectionsToHandle):
                    result = await keys.get('get')('app-state-sync-version', [name])
                    state = result[name]
                    if isTruthy(state):
                        state = ensureLTHashStateVersion(state)
                        if name not in initialVersionMap:
                            initialVersionMap[name] = state.get('version')
                    else:
                        state = newLTHashState()
                    states[name] = state
                    shouldForceSnapshot = name in forceSnapshotCollections
                    if shouldForceSnapshot:
                        forceSnapshotCollections.discard(name)
                    suffix = ' (forcing snapshot)' if shouldForceSnapshot else ''
                    logger.info(f"resyncing {name} from v{tpl(state.get('version'))}{suffix}")
                    nodes.append(JSObject({'tag': 'collection', 'attrs': JSObject({
                        'name': name,
                        'version': tpl(state.get('version')),
                        'return_snapshot': tpl(shouldForceSnapshot or not isTruthy(state.get('version')))
                    })}))
                result = await query(JSObject({
                    'tag': 'iq',
                    'attrs': JSObject({'to': S_WHATSAPP_NET, 'xmlns': 'w:sync:app:state', 'type': 'set'}),
                    'content': [JSObject({'tag': 'sync', 'attrs': JSObject({}), 'content': nodes})]
                }))
                decoded = await extractSyncdPatches(result, config.get('options'))
                for name in list(dict.keys(decoded)):
                    entry = decoded[name]
                    patches = entry.get('patches')
                    hasMorePatches = entry.get('hasMorePatches')
                    snapshot = entry.get('snapshot')
                    try:
                        if snapshot is not None:
                            restored = await decodeSyncdSnapshot(name, snapshot, getCachedAppStateSyncKey, initialVersionMap.get(name), appStateMacVerification.get('snapshot'), logger)
                            newState = restored.get('state')
                            states[name] = newState
                            dict.update(globalMutationMap, restored.get('mutationMap'))
                            logger.info(f"restored state of {name} from snapshot to v{tpl(newState.get('version'))} with mutations")
                            await keys.get('set')(JSObject({'app-state-sync-version': JSObject({name: newState})}))
                        if len(patches):
                            decodedPatches = await decodePatches(
                                name,
                                patches,
                                states[name],
                                getCachedAppStateSyncKey,
                                config.get('options'),
                                initialVersionMap.get(name),
                                logger,
                                appStateMacVerification.get('patch')
                            )
                            newState = decodedPatches.get('state')
                            await keys.get('set')(JSObject({'app-state-sync-version': JSObject({name: newState})}))
                            logger.info(f"synced {name} to v{tpl(newState.get('version'))}")
                            initialVersionMap[name] = newState.get('version')
                            dict.update(globalMutationMap, decodedPatches.get('mutationMap'))
                        if hasMorePatches:
                            logger.info(f'{name} has more patches...')
                        else:
                            collectionsToHandle.pop(name, None)
                    except Exception as error:
                        attemptsMap[name] = attemptsMap.get(name, 0) + 1
                        logData = JSObject({
                            'name': name,
                            'attempt': attemptsMap[name],
                            'version': states[name].get('version'),
                            'statusCode': error.output.get('statusCode') if isinstance(getattr(error, 'output', None), dict) else None,
                            'errorType': getattr(error, 'name', None),
                            'error': str(error)
                        })
                        if isMissingKeyError(error) and attemptsMap[name] >= MAX_SYNC_ATTEMPTS:
                            logger.warn(logData, f"{name} blocked on missing key from v{tpl(states[name].get('version'))}, parking after {attemptsMap[name]} attempts")
                            blockedCollections[name] = True
                            collectionsToHandle.pop(name, None)
                        elif isMissingKeyError(error):
                            logger.info(logData, f"{name} blocked on missing key from v{tpl(states[name].get('version'))}, retrying with snapshot")
                            forceSnapshotCollections.add(name)
                        elif isAppStateSyncIrrecoverable(error, attemptsMap[name]):
                            logger.warn(logData, f"failed to sync {name} from v{tpl(states[name].get('version'))}, giving up")
                            collectionsToHandle.pop(name, None)
                        else:
                            logger.info(logData, f"failed to sync {name} from v{tpl(states[name].get('version'))}, forcing snapshot retry")
                            forceSnapshotCollections.add(name)

        await keys.get('transaction')(transactionWork, orValue(getp(creds.get('me'), 'id'), 'resync-app-state'))
        handler = newAppStateChunkHandler(isInitialSync)
        for key in list(dict.keys(globalMutationMap)):
            handler.get('onMutation')(globalMutationMap[key])

    resyncAppState = ev.get('createBufferedFunction')(resyncAppStateWork)

    async def profilePictureUrl(jid, type_='image', timeoutMs=5000, shouldIncludeTcToken=True):
        normalizedJid = jidNormalizedUser(jid)
        isUserJid = isPnUser(normalizedJid) or isLidUser(normalizedJid)
        me = authState.get('creds').get('me')
        isSelf = me is not None and (normalizedJid == jidNormalizedUser(me.get('id')) or (isTruthy(me.get('lid')) and normalizedJid == jidNormalizedUser(me.get('lid'))))
        tcTokenContent = None
        if shouldIncludeTcToken and serverProps.get('profilePicPrivacyToken') and isUserJid and not isSelf:
            tcTokenContent = await buildTcTokenFromJid(JSObject({'authState': authState, 'jid': normalizedJid, 'getLIDForPN': getLIDForPN}))
        jid = jidNormalizedUser(jid)
        result = await query(
            JSObject({
                'tag': 'iq',
                'attrs': JSObject({'target': jid, 'to': S_WHATSAPP_NET, 'type': 'get', 'xmlns': 'w:profile:picture'}),
                'content': buildProfilePictureQueryContent(type_, tcTokenContent)
            }),
            timeoutMs
        )
        child = getBinaryNodeChild(result, 'picture')
        return getp(getp(child, 'attrs'), 'url')

    async def createCallLink(type_, event, timeoutMs=None):
        content = None
        if event is not None:
            content = [JSObject({'tag': 'event', 'attrs': JSObject({'start_time': str(event.get('startTime'))})})]
        result = await query(
            JSObject({
                'tag': 'call',
                'attrs': JSObject({'id': generateMessageTag(), 'to': '@call'}),
                'content': [JSObject({'tag': 'link_create', 'attrs': JSObject({'media': type_}), 'content': content})]
            }),
            timeoutMs
        )
        child = getBinaryNodeChild(result, 'link_create')
        return getp(getp(child, 'attrs'), 'token')

    async def sendPresenceUpdate(type_, toJid=None):
        me = authState.get('creds').get('me')
        isAvailableType = type_ == 'available'
        if isAvailableType or type_ == 'unavailable':
            if not isTruthy(me.get('name')):
                logger.warn('no name present, ignoring presence update request...')
                return
            ev.get('emit')('connection.update', JSObject({'isOnline': isAvailableType}))
            if isAvailableType:
                spawnTask(sendUnifiedSession())
            await sendNode(JSObject({'tag': 'presence', 'attrs': JSObject({'name': me.get('name').replace('@', ''), 'type': type_})}))
        else:
            server = jidDecode(toJid)['server']
            isLid = server == 'lid'
            if type_ == 'recording':
                tag = 'composing'
                attrs = JSObject({'media': 'audio'})
            else:
                tag = type_
                attrs = JSObject({})
            await sendNode(JSObject({
                'tag': 'chatstate',
                'attrs': JSObject({'from': me.get('lid') if isLid else me.get('id'), 'to': toJid}),
                'content': [JSObject({'tag': tag, 'attrs': attrs})]
            }))

    async def presenceSubscribe(toJid):
        normalizedToJid = jidNormalizedUser(toJid)
        isUserJid = isPnUser(normalizedToJid) or isLidUser(normalizedToJid)
        tcTokenContent = None
        if isUserJid:
            tcTokenContent = await buildTcTokenFromJid(JSObject({'authState': authState, 'jid': normalizedToJid, 'getLIDForPN': getLIDForPN}))
        return await sendNode(JSObject({
            'tag': 'presence',
            'attrs': JSObject({'to': toJid, 'id': generateMessageTag(), 'type': 'subscribe'}),
            'content': tcTokenContent
        }))

    def handlePresenceUpdate(node):
        tag = node.get('tag')
        attrs = node.get('attrs')
        content = node.get('content')
        presence = None
        jid = attrs.get('from')
        participant = orValue(attrs.get('participant'), attrs.get('from'))
        if shouldIgnoreJid(jid) and jid != S_WHATSAPP_NET:
            return
        if tag == 'presence':
            presence = JSObject({
                'lastKnownPresence': 'unavailable' if attrs.get('type') == 'unavailable' else 'available',
                'lastSeen': jsNumber(attrs.get('last')) if isTruthy(attrs.get('last')) and attrs.get('last') != 'deny' else None,
                'groupOnlineCount': jsNumber(attrs.get('count')) if isTruthy(attrs.get('count')) else None
            })
        elif isinstance(content, list):
            firstChild = content[0]
            kind = firstChild.get('tag')
            if kind == 'paused':
                kind = 'available'
            if getp(firstChild.get('attrs'), 'media') == 'audio':
                kind = 'recording'
            presence = JSObject({'lastKnownPresence': kind})
        else:
            logger.error(JSObject({'tag': tag, 'attrs': attrs, 'content': content}), 'recv invalid presence node')
        if presence is not None:
            ev.get('emit')('presence.update', JSObject({'id': jid, 'presences': JSObject({participant: presence})}))

    async def appPatch(patchCreate):
        name = patchCreate.get('type')
        myAppStateKeyId = authState.get('creds').get('myAppStateKeyId')
        if not isTruthy(myAppStateKeyId):
            raise Boom('App state key not present!', JSObject({'statusCode': 400}))
        result = JSObject({'initial': None, 'encodeResult': None})

        async def transactionWork():
            logger.debug(JSObject({'patch': patchCreate}), 'applying app patch')
            await resyncAppState([name], False)
            current = (await keys.get('get')('app-state-sync-version', [name]))[name]
            initial = ensureLTHashStateVersion(current) if isTruthy(current) else newLTHashState()
            result['initial'] = initial
            encodeResult = await encodeSyncdPatch(patchCreate, myAppStateKeyId, initial, getAppStateSyncKey)
            result['encodeResult'] = encodeResult
            patch = encodeResult.get('patch')
            state = encodeResult.get('state')
            node = JSObject({
                'tag': 'iq',
                'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'w:sync:app:state'}),
                'content': [JSObject({
                    'tag': 'sync',
                    'attrs': JSObject({}),
                    'content': [JSObject({
                        'tag': 'collection',
                        'attrs': JSObject({'name': name, 'version': str(state.get('version') - 1), 'return_snapshot': 'false'}),
                        'content': [JSObject({'tag': 'patch', 'attrs': JSObject({}), 'content': proto.SyncdPatch.encode(patch).finish()})]
                    })]
                })]
            })
            await query(node)
            await keys.get('set')(JSObject({'app-state-sync-version': JSObject({name: state})}))

        async def mutationWork():
            await keys.get('transaction')(transactionWork, orValue(getp(creds.get('me'), 'id'), 'app-patch'))

        await appStatePatchMutex.get('mutex')(mutationWork)
        if config.get('emitOwnEvents'):
            handler = newAppStateChunkHandler(False)
            encodeResult = result.get('encodeResult')
            patchWithVersion = JSObject({**encodeResult.get('patch'), 'version': JSObject({'version': encodeResult.get('state').get('version')})})
            decodedPatches = await decodePatches(name, [patchWithVersion], result.get('initial'), getAppStateSyncKey, config.get('options'), None, logger)
            mutationMap = decodedPatches.get('mutationMap')
            for key in list(dict.keys(mutationMap)):
                handler.get('onMutation')(mutationMap[key])

    async def fetchProps():
        resultNode = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'xmlns': 'abt', 'type': 'get'}),
            'content': [JSObject({'tag': 'props', 'attrs': JSObject({'protocol': '1', **({'hash': creds.get('lastPropHash')} if isTruthy(creds.get('lastPropHash')) else {})})})]
        }))
        propsNode = getBinaryNodeChild(resultNode, 'props')
        props = JSObject({})
        if propsNode is not None:
            if isTruthy(getp(propsNode.get('attrs'), 'hash')):
                creds['lastPropHash'] = propsNode.get('attrs').get('hash')
                ev.get('emit')('creds.update', creds)
            props = reduceBinaryNodeToDictionary(propsNode, 'prop')
        privacyTokenProp = props.get('10518') if props.get('10518') is not None else props.get('privacy_token_sending_on_all_1_on_1_messages')
        if privacyTokenProp is not None:
            serverProps['privacyTokenOn1to1'] = privacyTokenProp == 'true' or privacyTokenProp == '1'
        profilePicProp = props.get('9666') if props.get('9666') is not None else props.get('profile_scraping_privacy_token_in_photo_iq')
        if profilePicProp is not None:
            serverProps['profilePicPrivacyToken'] = profilePicProp == 'true' or profilePicProp == '1'
        lidIssueProp = props.get('14303') if props.get('14303') is not None else props.get('lid_trusted_token_issue_to_lid')
        if lidIssueProp is not None:
            serverProps['lidTrustedTokenIssueToLid'] = lidIssueProp == 'true' or lidIssueProp == '1'
        logger.debug(JSObject({'serverProps': serverProps}), 'fetched props')
        return props

    async def chatModify(mod, jid):
        return await appPatch(chatModificationToAppPatch(mod, jid))

    async def updateDisableLinkPreviewsPrivacy(isPreviewsDisabled):
        return await chatModify(JSObject({'disableLinkPreviews': JSObject({'isPreviewsDisabled': isPreviewsDisabled})}), '')

    async def star(jid, messages, starFlag):
        return await chatModify(JSObject({'star': JSObject({'messages': messages, 'star': starFlag})}), jid)

    async def addOrEditContact(jid, contact):
        return await chatModify(JSObject({'contact': contact}), jid)

    async def removeContact(jid):
        return await chatModify(JSObject({'contact': None}), jid)

    async def addLabel(jid, labels):
        return await chatModify(JSObject({'addLabel': JSObject({**labels})}), jid)

    async def addChatLabel(jid, labelId):
        return await chatModify(JSObject({'addChatLabel': JSObject({'labelId': labelId})}), jid)

    async def removeChatLabel(jid, labelId):
        return await chatModify(JSObject({'removeChatLabel': JSObject({'labelId': labelId})}), jid)

    async def addMessageLabel(jid, messageId, labelId):
        return await chatModify(JSObject({'addMessageLabel': JSObject({'messageId': messageId, 'labelId': labelId})}), jid)

    async def removeMessageLabel(jid, messageId, labelId):
        return await chatModify(JSObject({'removeMessageLabel': JSObject({'messageId': messageId, 'labelId': labelId})}), jid)

    async def addOrEditQuickReply(quickReply):
        return await chatModify(JSObject({'quickReply': quickReply}), '')

    async def removeQuickReply(timestamp):
        return await chatModify(JSObject({'quickReply': JSObject({'timestamp': timestamp, 'deleted': True})}), '')

    async def executeInitQueries():
        await asyncio.gather(fetchProps(), fetchBlocklist(), fetchPrivacySettings())

    async def upsertWork(msg, type_):
        nonlocal syncState, awaitingSyncTimeout, historySyncPausedTimeout

        ev.get('emit')('messages.upsert', JSObject({'messages': [msg], 'type': type_}))
        if isTruthy(msg.get('pushName')):
            if msg.get('key').get('fromMe'):
                jid = authState.get('creds').get('me').get('id')
            else:
                jid = orValue(msg.get('key').get('participant'), msg.get('key').get('remoteJid'))
            jid = jidNormalizedUser(jid)
            if not msg.get('key').get('fromMe'):
                ev.get('emit')('contacts.update', [JSObject({'id': jid, 'notify': msg.get('pushName'), 'verifiedName': msg.get('verifiedBizName')})])
            if msg.get('key').get('fromMe') and isTruthy(msg.get('pushName')) and getp(creds.get('me'), 'name') != msg.get('pushName'):
                ev.get('emit')('creds.update', JSObject({'me': JSObject({**(creds.get('me') or {}), 'name': msg.get('pushName')})}))
        historyMsg = getHistoryMsg(msg.get('message'))
        shouldProcessHistoryMsg = False
        if historyMsg is not None:
            shouldProcessHistoryMsg = isTruthy(shouldSyncHistoryMessage(historyMsg)) and historyMsg.get('syncType') in PROCESSABLE_HISTORY_TYPES
        if historyMsg is not None and shouldProcessHistoryMsg:
            syncType = historyMsg.get('syncType')
            if syncType == proto.HistorySync.HistorySyncType.INITIAL_BOOTSTRAP and not historySyncStatus.get('initialBootstrapComplete'):
                historySyncStatus['initialBootstrapComplete'] = True
                ev.get('emit')('messaging-history.status', JSObject({'syncType': syncType, 'status': 'complete', 'explicit': True}))
            if syncType == proto.HistorySync.HistorySyncType.RECENT and historyMsg.get('progress') == 100 and not historySyncStatus.get('recentSyncComplete'):
                historySyncStatus['recentSyncComplete'] = True
                clearTimeout(historySyncPausedTimeout)
                historySyncPausedTimeout = None
                ev.get('emit')('messaging-history.status', JSObject({'syncType': syncType, 'status': 'complete', 'explicit': True}))
            if syncType == proto.HistorySync.HistorySyncType.RECENT and not historySyncStatus.get('recentSyncComplete'):
                clearTimeout(historySyncPausedTimeout)

                def onPausedTimeout():
                    nonlocal historySyncPausedTimeout
                    if not historySyncStatus.get('recentSyncComplete'):
                        historySyncStatus['recentSyncComplete'] = True
                        ev.get('emit')('messaging-history.status', JSObject({'syncType': proto.HistorySync.HistorySyncType.RECENT, 'status': 'paused', 'explicit': False}))
                    historySyncPausedTimeout = None
                historySyncPausedTimeout = setTimeout(onPausedTimeout, HISTORY_SYNC_PAUSED_TIMEOUT_MS)
        if historyMsg is not None and syncState == SyncState['AwaitingInitialSync']:
            if awaitingSyncTimeout is not None:
                clearTimeout(awaitingSyncTimeout)
                awaitingSyncTimeout = None
            if shouldProcessHistoryMsg:
                syncState = SyncState['Syncing']
                logger.info('Transitioned to Syncing state')
            else:
                syncState = SyncState['Online']
                logger.info('History sync skipped, transitioning to Online state and flushing buffer')
                ev.get('flush')()

        async def doAppStateSync():
            nonlocal syncState
            if syncState == SyncState['Syncing']:
                blockedCollections.clear()
                logger.info('Doing app state sync')
                await resyncAppState(ALL_WA_PATCH_NAMES, True)
                syncState = SyncState['Online']
                logger.info('App state sync complete, transitioning to Online state and flushing buffer')
                ev.get('flush')()
                accountSyncCounter = orValue(creds.get('accountSyncCounter'), 0) + 1
                ev.get('emit')('creds.update', JSObject({'accountSyncCounter': accountSyncCounter}))

        async def historyPart():
            if shouldProcessHistoryMsg:
                await doAppStateSync()

        async def messagePart():
            await processMessage(msg, JSObject({
                'signalRepository': signalRepository,
                'shouldProcessHistoryMsg': shouldProcessHistoryMsg,
                'placeholderResendCache': placeholderResendCache,
                'ev': ev,
                'creds': creds,
                'keyStore': keys,
                'logger': logger,
                'options': config.get('options'),
                'getMessage': getMessage
            }))
        await asyncio.gather(historyPart(), messagePart())
        protocolMessage = getp(msg.get('message'), 'protocolMessage')
        if getp(protocolMessage, 'appStateSyncKeyShare') is not None and syncState == SyncState['Syncing']:
            logger.info('App state sync key arrived, triggering app state sync')
            await doAppStateSync()

    upsertMessage = ev.get('createBufferedFunction')(upsertWork)

    ws.on('CB:presence', handlePresenceUpdate)
    ws.on('CB:chatstate', handlePresenceUpdate)

    async def onDirty(node):
        attrs = getBinaryNodeChild(node, 'dirty')['attrs']
        kind = attrs.get('type')
        if kind == 'account_sync':
            if isTruthy(attrs.get('timestamp')):
                lastAccountSyncTimestamp = creds.get('lastAccountSyncTimestamp')
                if isTruthy(lastAccountSyncTimestamp):
                    await cleanDirtyBits('account_sync', lastAccountSyncTimestamp)
                lastAccountSyncTimestamp = jsNumber(attrs.get('timestamp'))
                ev.get('emit')('creds.update', JSObject({'lastAccountSyncTimestamp': lastAccountSyncTimestamp}))
        elif kind == 'groups':
            pass
        else:
            logger.info(JSObject({'node': node}), 'received unknown sync')
    ws.on('CB:ib,,dirty', asyncListener(onDirty))

    def onConnectionUpdate(update):
        nonlocal syncState, awaitingSyncTimeout, historySyncPausedTimeout
        connection = update.get('connection')
        receivedPendingNotifications = update.get('receivedPendingNotifications')
        if connection == 'close':
            blockedCollections.clear()
            clearTimeout(historySyncPausedTimeout)
            historySyncPausedTimeout = None
        if connection == 'open':
            if isTruthy(fireInitQueries):
                spawnCatch(executeInitQueries(), lambda error: onUnexpectedError(error, 'init queries'))
            spawnCatch(sendPresenceUpdate('available' if isTruthy(markOnlineOnConnect) else 'unavailable'), lambda error: onUnexpectedError(error, 'presence update requests'))
        if not isTruthy(receivedPendingNotifications) or syncState != SyncState['Connecting']:
            return
        historySyncStatus['initialBootstrapComplete'] = False
        historySyncStatus['recentSyncComplete'] = False
        clearTimeout(historySyncPausedTimeout)
        historySyncPausedTimeout = None
        syncState = SyncState['AwaitingInitialSync']
        logger.info('Connection is now AwaitingInitialSync, buffering events')
        ev.get('buffer')()
        willSyncHistory = shouldSyncHistoryMessage(proto.Message.HistorySyncNotification.create(JSObject({'syncType': proto.HistorySync.HistorySyncType.RECENT})))
        if not isTruthy(willSyncHistory):
            logger.info('History sync is disabled by config, not waiting for notification. Transitioning to Online.')
            syncState = SyncState['Online']
            setTimeout(lambda: ev.get('flush')(), 0)
            return
        if orValue(creds.get('accountSyncCounter'), 0) > 0:
            logger.info('Reconnection with existing sync data, skipping history sync wait. Transitioning to Online.')
            syncState = SyncState['Online']
            setTimeout(lambda: ev.get('flush')(), 0)
            return
        logger.info('First connection, awaiting history sync notification with a 20s timeout.')
        if awaitingSyncTimeout is not None:
            clearTimeout(awaitingSyncTimeout)

        def onAwaitTimeout():
            nonlocal syncState
            if syncState == SyncState['AwaitingInitialSync']:
                logger.warn('Timeout in AwaitingInitialSync, forcing state to Online and flushing buffer')
                syncState = SyncState['Online']
                ev.get('flush')()
                ev.get('emit')('creds.update', JSObject({'accountSyncCounter': orValue(creds.get('accountSyncCounter'), 0) + 1}))
        awaitingSyncTimeout = setTimeout(onAwaitTimeout, 20000)
    ev.get('on')('connection.update', onConnectionUpdate)

    def onCredsUpdateBlocked(update):
        nonlocal syncState
        myAppStateKeyId = getp(update, 'myAppStateKeyId')
        if not isTruthy(myAppStateKeyId) or len(blockedCollections) == 0:
            return
        if syncState == SyncState['Syncing']:
            blockedCollections.clear()
            return
        collections = list(dict.keys(blockedCollections))
        blockedCollections.clear()
        logger.info(JSObject({'collections': collections}), 'app state sync key arrived, re-syncing blocked collections')
        spawnCatch(resyncAppState(collections, False), lambda error: onUnexpectedError(error, 'blocked collections resync'))
    ev.get('on')('creds.update', onCredsUpdateBlocked)

    async def onLidMappingUpdate(update):
        lid = update.get('lid')
        pn = update.get('pn')
        try:
            await signalRepository.get('lidMapping').storeLIDPNMappings([JSObject({'lid': lid, 'pn': pn})])
        except Exception as error:
            logger.warn(JSObject({'lid': lid, 'pn': pn, 'error': error}), 'Failed to store LID-PN mapping')
    ev.get('on')('lid-mapping.update', asyncListener(onLidMappingUpdate))

    def onSocketEnd(error=None, *args):
        nonlocal awaitingSyncTimeout, syncState, privacySettings
        if awaitingSyncTimeout is not None:
            clearTimeout(awaitingSyncTimeout)
            awaitingSyncTimeout = None
        if not isTruthy(config.get('placeholderResendCache')):
            placeholderResendCache.close()
        syncState = SyncState['Connecting']
        privacySettings = None
    registerSocketEndHandler(onSocketEnd)

    return spreadSocket(sock, JSObject({
        'findUserId': findUserId,
        'serverProps': serverProps,
        'createCallLink': createCallLink,
        'getBotListV2': getBotListV2,
        'messageMutex': messageMutex,
        'receiptMutex': receiptMutex,
        'appStatePatchMutex': appStatePatchMutex,
        'notificationMutex': notificationMutex,
        'fetchPrivacySettings': fetchPrivacySettings,
        'upsertMessage': upsertMessage,
        'appPatch': appPatch,
        'sendPresenceUpdate': sendPresenceUpdate,
        'presenceSubscribe': presenceSubscribe,
        'profilePictureUrl': profilePictureUrl,
        'fetchBlocklist': fetchBlocklist,
        'fetchStatus': fetchStatus,
        'fetchDisappearingDuration': fetchDisappearingDuration,
        'updateProfilePicture': updateProfilePicture,
        'removeProfilePicture': removeProfilePicture,
        'updateProfileStatus': updateProfileStatus,
        'updateProfileName': updateProfileName,
        'updateBlockStatus': updateBlockStatus,
        'updateDisableLinkPreviewsPrivacy': updateDisableLinkPreviewsPrivacy,
        'updateCallPrivacy': updateCallPrivacy,
        'updateMessagesPrivacy': updateMessagesPrivacy,
        'updateLastSeenPrivacy': updateLastSeenPrivacy,
        'updateOnlinePrivacy': updateOnlinePrivacy,
        'updateProfilePicturePrivacy': updateProfilePicturePrivacy,
        'updateStatusPrivacy': updateStatusPrivacy,
        'updateReadReceiptsPrivacy': updateReadReceiptsPrivacy,
        'updateGroupsAddPrivacy': updateGroupsAddPrivacy,
        'updateDefaultDisappearingMode': updateDefaultDisappearingMode,
        'getBusinessProfile': getBusinessProfile,
        'resyncAppState': resyncAppState,
        'chatModify': chatModify,
        'cleanDirtyBits': cleanDirtyBits,
        'addOrEditContact': addOrEditContact,
        'removeContact': removeContact,
        'placeholderResendCache': placeholderResendCache,
        'addLabel': addLabel,
        'addChatLabel': addChatLabel,
        'removeChatLabel': removeChatLabel,
        'addMessageLabel': addMessageLabel,
        'removeMessageLabel': removeMessageLabel,
        'star': star,
        'addOrEditQuickReply': addOrEditQuickReply,
        'removeQuickReply': removeQuickReply
    }))
