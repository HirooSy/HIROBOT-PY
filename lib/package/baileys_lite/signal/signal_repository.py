import asyncio
import inspect
import json
import re
from baileys_lite.foundation.cache import Cache
from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.signal import libsignal
from baileys_lite.signal.libsignal import PreKeyWhisperMessage
from baileys_lite.signal.signal_group import SenderKeyName, SenderKeyRecord, GroupCipher, GroupSessionBuilder, SenderKeyDistributionMessage, asBuffer
from baileys_lite.utils.wa_protocol_core import generateSignalPubKey
from baileys_lite.binary.wa_binary import isHostedLidUser, isHostedPnUser, isLidUser, isPnUser, jidDecode, jidNormalizedUser, transferDevice, WAJIDDomains


async def awaitIfNeeded(value):
    if inspect.isawaitable(value):
        return await value
    return value


def parseIntPrefix(text):
    match = re.match(r'\s*([+-]?[0-9]+)', text)
    if match is None:
        return float('nan')
    return int(match.group(1))


def jsGetKey(obj, key):
    if obj is None:
        return None
    return dict.get(obj, key)


class LIDMappingStore:
    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __init__(self, keys, logger, pnToLIDFunc):
        self.mappingCache = Cache(JSObject({'ttl': 3 * 24 * 60 * 60 * 1000, 'updateAgeOnGet': True}))
        self.inflightLIDLookups = {}
        self.inflightPNLookups = {}
        self.keys = keys
        self.pnToLIDFunc = pnToLIDFunc
        self.logger = logger

    async def storeLIDPNMappings(self, pairs):
        if len(pairs) == 0:
            return
        validatedPairs = []
        for pair in pairs:
            lid = pair.get('lid')
            pn = pair.get('pn')
            if not ((isLidUser(lid) and isPnUser(pn)) or (isPnUser(lid) and isLidUser(pn))):
                self.logger.warn(f'Invalid LID-PN mapping: {lid}, {pn}')
                continue
            lidDecoded = jidDecode(lid)
            pnDecoded = jidDecode(pn)
            if not lidDecoded or not pnDecoded:
                continue
            validatedPairs.append(JSObject({'pnUser': pnDecoded.get('user'), 'lidUser': lidDecoded.get('user')}))
        if len(validatedPairs) == 0:
            return

        cacheMissSet = {}
        existingMappings = {}
        for pair in validatedPairs:
            pnUser = pair.get('pnUser')
            cached = self.mappingCache.get(f'pn:{pnUser}')
            if cached:
                existingMappings[pnUser] = cached
            else:
                cacheMissSet[pnUser] = True

        if len(cacheMissSet) > 0:
            cacheMisses = list(cacheMissSet.keys())
            self.logger.trace(f'Batch fetching {len(cacheMisses)} LID mappings from database')
            stored = await self.keys.get('get')('lid-mapping', cacheMisses)
            for pnUser in cacheMisses:
                existingLidUser = jsGetKey(stored, pnUser)
                if existingLidUser:
                    existingMappings[pnUser] = existingLidUser
                    self.mappingCache.set(f'pn:{pnUser}', existingLidUser)
                    self.mappingCache.set(f'lid:{existingLidUser}', pnUser)

        pairMap = {}
        for pair in validatedPairs:
            pnUser = pair.get('pnUser')
            lidUser = pair.get('lidUser')
            existingLidUser = existingMappings.get(pnUser)
            if existingLidUser == lidUser:
                self.logger.debug(JSObject({'pnUser': pnUser, 'lidUser': lidUser}), 'LID mapping already exists, skipping')
                continue
            pairMap[pnUser] = lidUser
        if len(pairMap) == 0:
            return

        self.logger.trace(JSObject({'pairMap': pairMap}), f'Storing {len(pairMap)} pn mappings')
        batchData = JSObject({})
        for pnUser, lidUser in list(pairMap.items()):
            batchData[pnUser] = lidUser
            batchData[f'{lidUser}_reverse'] = pnUser

        async def txn():
            await self.keys.get('set')(JSObject({'lid-mapping': batchData}))

        await self.keys.get('transaction')(txn, 'lid-mapping')

        for pnUser, lidUser in list(pairMap.items()):
            self.mappingCache.set(f'pn:{pnUser}', lidUser)
            self.mappingCache.set(f'lid:{lidUser}', pnUser)

    async def getLIDForPN(self, pn):
        result = await self.getLIDsForPNs([pn])
        if not result:
            return None
        return result[0].get('lid') or None

    async def getLIDsForPNs(self, pns):
        if len(pns) == 0:
            return None
        sortedPns = sorted(set(pns))
        cacheKey = ','.join(sortedPns)
        if cacheKey in self.inflightLIDLookups:
            self.logger.trace(f'Coalescing getLIDsForPNs request for {len(sortedPns)} PNs')
            return await self.inflightLIDLookups[cacheKey]
        promise = asyncio.ensure_future(self._getLIDsForPNsImpl(pns))
        self.inflightLIDLookups[cacheKey] = promise
        try:
            return await promise
        finally:
            self.inflightLIDLookups.pop(cacheKey, None)

    async def _getLIDsForPNsImpl(self, pns):
        usyncFetch = {}
        successfulPairs = {}
        pending = []

        def addResolvedPair(pn, decoded, lidUser):
            normalizedLidUser = str(lidUser)
            if not normalizedLidUser:
                self.logger.warn(f'Invalid or empty LID user for PN {pn}: lidUser = "{lidUser}"')
                return False
            pnDevice = decoded.get('device') if decoded.get('device') is not None else 0
            devicePart = f':{pnDevice}' if pnDevice else ''
            serverPart = 'hosted.lid' if decoded.get('server') == 'hosted' else 'lid'
            deviceSpecificLid = f'{normalizedLidUser}{devicePart}@{serverPart}'
            self.logger.trace(f'getLIDForPN: {pn} → {deviceSpecificLid} (user mapping with device {pnDevice})')
            successfulPairs[pn] = JSObject({'lid': deviceSpecificLid, 'pn': pn})
            return True

        for pn in pns:
            if not isPnUser(pn) and not isHostedPnUser(pn):
                continue
            decoded = jidDecode(pn)
            if not decoded:
                continue
            pnUser = decoded.get('user')
            cached = self.mappingCache.get(f'pn:{pnUser}')
            if cached and isinstance(cached, str):
                if not addResolvedPair(pn, decoded, cached):
                    self.logger.warn(f'Invalid entry for {pn} (pair not resolved)')
                    continue
                continue
            pending.append(JSObject({'pn': pn, 'pnUser': pnUser, 'decoded': decoded}))

        if len(pending):
            pnUsers = list(dict.fromkeys(item.get('pnUser') for item in pending))
            stored = await self.keys.get('get')('lid-mapping', pnUsers)
            for pnUser in pnUsers:
                lidUser = jsGetKey(stored, pnUser)
                if lidUser and isinstance(lidUser, str):
                    self.mappingCache.set(f'pn:{pnUser}', lidUser)
                    self.mappingCache.set(f'lid:{lidUser}', pnUser)
            for item in pending:
                pn = item.get('pn')
                pnUser = item.get('pnUser')
                decoded = item.get('decoded')
                cached = self.mappingCache.get(f'pn:{pnUser}')
                if cached and isinstance(cached, str):
                    if not addResolvedPair(pn, decoded, cached):
                        self.logger.warn(f'Invalid entry for {pn} (pair not resolved)')
                        continue
                else:
                    self.logger.trace(f'No LID mapping found for PN user {pnUser}; batch getting from USync')
                    device = decoded.get('device') or 0
                    normalizedPn = jidNormalizedUser(pn)
                    if isHostedPnUser(normalizedPn):
                        normalizedPn = f'{pnUser}@s.whatsapp.net'
                    usyncFetch.setdefault(normalizedPn, []).append(device)

        if len(usyncFetch) > 0:
            result = None
            if self.pnToLIDFunc is not None:
                result = await awaitIfNeeded(self.pnToLIDFunc(list(usyncFetch.keys())))
            if result and len(result) > 0:
                await self.storeLIDPNMappings(result)
                for pair in result:
                    pnDecoded = jidDecode(pair.get('pn'))
                    pnUser = pnDecoded.get('user') if pnDecoded else None
                    if not pnUser:
                        continue
                    lidDecoded = jidDecode(pair.get('lid'))
                    lidUser = lidDecoded.get('user') if lidDecoded else None
                    if not lidUser:
                        continue
                    for device in usyncFetch[pair.get('pn')]:
                        devicePart = f':{device}' if device else ''
                        deviceSpecificLid = f"{lidUser}{devicePart}@{'hosted.lid' if device == 99 else 'lid'}"
                        self.logger.trace(f'getLIDForPN: USYNC success for {pair.get('pn')} → {deviceSpecificLid} (user mapping with device {device})')
                        deviceSpecificPn = f"{pnUser}{devicePart}@{'hosted' if device == 99 else 's.whatsapp.net'}"
                        successfulPairs[deviceSpecificPn] = JSObject({'lid': deviceSpecificLid, 'pn': deviceSpecificPn})
            else:
                self.logger.warn('USync fetch yielded no results for pending PNs')

        return list(successfulPairs.values()) if len(successfulPairs) > 0 else None

    async def getPNForLID(self, lid):
        result = await self.getPNsForLIDs([lid])
        if not result:
            return None
        return result[0].get('pn') or None

    async def getPNsForLIDs(self, lids):
        if len(lids) == 0:
            return None
        sortedLids = sorted(set(lids))
        cacheKey = ','.join(sortedLids)
        if cacheKey in self.inflightPNLookups:
            self.logger.trace(f'Coalescing getPNsForLIDs request for {len(sortedLids)} LIDs')
            return await self.inflightPNLookups[cacheKey]
        promise = asyncio.ensure_future(self._getPNsForLIDsImpl(lids))
        self.inflightPNLookups[cacheKey] = promise
        try:
            return await promise
        finally:
            self.inflightPNLookups.pop(cacheKey, None)

    async def _getPNsForLIDsImpl(self, lids):
        successfulPairs = {}
        pending = []

        def addResolvedPair(lid, decoded, pnUser):
            if not pnUser or not isinstance(pnUser, str):
                return False
            lidDevice = decoded.get('device') if decoded.get('device') is not None else 0
            serverPart = 'hosted' if decoded.get('domainType') == WAJIDDomains['HOSTED_LID'] else 's.whatsapp.net'
            pnJid = f'{pnUser}:{lidDevice}@{serverPart}'
            self.logger.trace(f'Found reverse mapping: {lid} → {pnJid}')
            successfulPairs[lid] = JSObject({'lid': lid, 'pn': pnJid})
            return True

        for lid in lids:
            if not isLidUser(lid):
                continue
            decoded = jidDecode(lid)
            if not decoded:
                continue
            lidUser = decoded.get('user')
            cached = self.mappingCache.get(f'lid:{lidUser}')
            if cached and isinstance(cached, str):
                addResolvedPair(lid, decoded, cached)
                continue
            pending.append(JSObject({'lid': lid, 'lidUser': lidUser, 'decoded': decoded}))

        if len(pending):
            reverseKeys = list(dict.fromkeys(f"{item.get('lidUser')}_reverse" for item in pending))
            stored = await self.keys.get('get')('lid-mapping', reverseKeys)
            for item in pending:
                lid = item.get('lid')
                lidUser = item.get('lidUser')
                decoded = item.get('decoded')
                pnUser = self.mappingCache.get(f'lid:{lidUser}')
                if not pnUser or not isinstance(pnUser, str):
                    pnUser = jsGetKey(stored, f'{lidUser}_reverse')
                    if pnUser and isinstance(pnUser, str):
                        self.mappingCache.set(f'lid:{lidUser}', pnUser)
                        self.mappingCache.set(f'pn:{pnUser}', lidUser)
                if pnUser and isinstance(pnUser, str):
                    addResolvedPair(lid, decoded, pnUser)
                else:
                    self.logger.trace(f'No reverse mapping found for LID user: {lidUser}')

        return list(successfulPairs.values()) if len(successfulPairs) else None

    def close(self):
        self.mappingCache.clear()


def extractIdentityFromPkmsg(ciphertext):
    try:
        if not ciphertext or len(ciphertext) < 2:
            return None
        version = ciphertext[0]
        if (version & 0xf) != 3:
            return None
        preKeyProto = PreKeyWhisperMessage.decode(ciphertext[1:])
        if len(preKeyProto.get('identityKey')) == 33:
            return bytes(preKeyProto.get('identityKey'))
        return None
    except Exception:
        return None


def makeLibSignalRepository(auth, logger, pnToLIDFunc):
    lidMapping = LIDMappingStore(auth.get('keys'), logger, pnToLIDFunc)
    storage = signalStorage(auth, lidMapping)
    parsedKeys = auth.get('keys')
    migratedSessionCache = Cache(JSObject({'ttl': 3 * 24 * 60 * 60 * 1000, 'updateAgeOnGet': True}))

    async def ensureSenderKeyAndCreateSkdm(group, meId):
        senderName = jidToSignalSenderKeyName(group, meId)
        senderNameStr = senderName.toString()
        result = await auth.get('keys').get('get')('sender-key', [senderNameStr])
        senderKey = jsGetKey(result, senderNameStr)
        if senderKey is None:
            await storage.get('storeSenderKey')(senderName, SenderKeyRecord())
        skdm = await GroupSessionBuilder(storage).create(senderName)
        return JSObject({'senderName': senderName, 'skdm': skdm})

    async def decryptGroupMessage(opts):
        senderName = jidToSignalSenderKeyName(opts.get('group'), opts.get('authorJid'))
        cipher = GroupCipher(storage, senderName)

        async def run():
            return await cipher.decrypt(opts.get('msg'))

        return await parsedKeys.get('transaction')(run, opts.get('group'))

    async def processSenderKeyDistributionMessage(opts):
        item = opts.get('item')
        builder = GroupSessionBuilder(storage)
        if not item.get('groupId'):
            raise JSError('Group ID is required for sender key distribution message')
        senderName = jidToSignalSenderKeyName(item.get('groupId'), opts.get('authorJid'))
        senderMsg = SenderKeyDistributionMessage(None, None, None, None, item.get('axolotlSenderKeyDistributionMessage'))
        senderNameStr = senderName.toString()
        result = await auth.get('keys').get('get')('sender-key', [senderNameStr])
        senderKey = jsGetKey(result, senderNameStr)
        if senderKey is None:
            await storage.get('storeSenderKey')(senderName, SenderKeyRecord())

        async def run():
            result2 = await auth.get('keys').get('get')('sender-key', [senderNameStr])
            senderKey2 = jsGetKey(result2, senderNameStr)
            if senderKey2 is None:
                await storage.get('storeSenderKey')(senderName, SenderKeyRecord())
            await builder.process(senderName, senderMsg)

        return await parsedKeys.get('transaction')(run, item.get('groupId'))

    async def decryptMessage(opts):
        jid = opts.get('jid')
        type = opts.get('type')
        ciphertext = opts.get('ciphertext')
        addr = jidToSignalProtocolAddress(jid)
        session = libsignal.SessionCipher(storage, addr)

        if type == 'pkmsg':
            identityKey = extractIdentityFromPkmsg(ciphertext)
            if identityKey is not None:
                addrStr = addr.toString()
                identityChanged = await storage.get('saveIdentity')(addrStr, identityKey)
                if identityChanged:
                    logger.info(JSObject({'jid': jid, 'addr': addrStr}), 'identity key changed or new contact, session will be re-established')

        async def doDecrypt():
            if type == 'pkmsg':
                return await session.decryptPreKeyWhisperMessage(ciphertext)
            if type == 'msg':
                return await session.decryptWhisperMessage(ciphertext)
            return None

        async def run():
            return await doDecrypt()

        return await parsedKeys.get('transaction')(run, jid)

    async def encryptMessage(opts):
        jid = opts.get('jid')
        data = opts.get('data')
        addr = jidToSignalProtocolAddress(jid)
        cipher = libsignal.SessionCipher(storage, addr)

        async def run():
            result = await cipher.encrypt(data)
            sigType = result.get('type')
            body = result.get('body')
            msgType = 'pkmsg' if sigType == 3 else 'msg'
            return JSObject({'type': msgType, 'ciphertext': bytes(body)})

        return await parsedKeys.get('transaction')(run, jid)

    async def encryptGroupMessage(opts):
        group = opts.get('group')
        meId = opts.get('meId')
        data = opts.get('data')

        async def run():
            ensured = await ensureSenderKeyAndCreateSkdm(group, meId)
            ciphertext = await GroupCipher(storage, ensured.get('senderName')).encrypt(data)
            return JSObject({'ciphertext': ciphertext, 'senderKeyDistributionMessage': ensured.get('skdm').serialize()})

        return await parsedKeys.get('transaction')(run, group)

    async def getSenderKeyDistributionMessage(opts):
        group = opts.get('group')
        meId = opts.get('meId')

        async def run():
            ensured = await ensureSenderKeyAndCreateSkdm(group, meId)
            return ensured.get('skdm').serialize()

        return await parsedKeys.get('transaction')(run, group)

    async def hasSenderKey(opts):
        senderName = jidToSignalSenderKeyName(opts.get('group'), opts.get('meId')).toString()
        result = await auth.get('keys').get('get')('sender-key', [senderName])
        key = jsGetKey(result, senderName)
        return key is not None

    async def getSessionInfo(jid):
        addr = jidToSignalProtocolAddress(jid).toString()
        session = await storage.get('loadSession')(addr)
        if not session:
            return None
        open = session.getOpenSession()
        if open is None:
            return None
        baseKey = open.indexInfo.get('baseKey')
        registrationId = open.registrationId
        if baseKey is None or not isinstance(registrationId, (int, float)) or isinstance(registrationId, bool):
            return None
        return JSObject({'baseKey': bytes(baseKey), 'registrationId': registrationId})

    async def injectE2ESession(opts):
        jid = opts.get('jid')
        session = opts.get('session')
        logger.trace(JSObject({'jid': jid}), 'injecting E2EE session')
        cipher = libsignal.SessionBuilder(storage, jidToSignalProtocolAddress(jid))

        async def run():
            await cipher.initOutgoing(session)

        return await parsedKeys.get('transaction')(run, jid)

    async def validateSession(jid):
        try:
            addr = jidToSignalProtocolAddress(jid)
            session = await storage.get('loadSession')(addr.toString())
            if not session:
                return JSObject({'exists': False, 'reason': 'no session'})
            if not session.haveOpenSession():
                return JSObject({'exists': False, 'reason': 'no open session'})
            return JSObject({'exists': True})
        except Exception:
            return JSObject({'exists': False, 'reason': 'validation error'})

    async def deleteSession(jids):
        if not jids:
            return None
        sessionUpdates = {}
        for jid in jids:
            addr = jidToSignalProtocolAddress(jid)
            sessionUpdates[addr.toString()] = None

        async def run():
            await auth.get('keys').get('set')(JSObject({'session': JSObject(sessionUpdates)}))

        return await parsedKeys.get('transaction')(run, f'delete-{len(jids)}-sessions')

    def close():
        migratedSessionCache.clear()
        lidMapping.close()

    async def migrateSession(fromJid, toJid):
        if not fromJid or (not isLidUser(toJid) and not isHostedLidUser(toJid)):
            return JSObject({'migrated': 0, 'skipped': 0, 'total': 0})
        if not isPnUser(fromJid) and not isHostedPnUser(fromJid):
            return JSObject({'migrated': 0, 'skipped': 0, 'total': 1})

        user = jidDecode(fromJid)['user']
        logger.debug(JSObject({'fromJid': fromJid}), 'bulk device migration - loading all user devices')
        result = await parsedKeys.get('get')('device-list', [user])
        userDevices = jsGetKey(result, user)
        if userDevices is None:
            return JSObject({'migrated': 0, 'skipped': 0, 'total': 0})

        fromDevice = jidDecode(fromJid)['device']
        fromDeviceStr = str(fromDevice) if fromDevice is not None else '0'
        if fromDeviceStr not in userDevices:
            userDevices.append(fromDeviceStr)

        uncachedDevices = [device for device in userDevices if not migratedSessionCache.has(f'{user}.{device}')]
        deviceSessionKeys = [f'{user}.{device}' for device in uncachedDevices]
        existingSessions = await parsedKeys.get('get')('session', deviceSessionKeys)

        deviceJids = []
        for sessionKey, sessionData in list(dict.items(existingSessions)):
            if sessionData is not None:
                parts = sessionKey.split('.')
                deviceStr = parts[1] if len(parts) > 1 else None
                if not deviceStr:
                    continue
                deviceNum = parseIntPrefix(deviceStr)
                jid = f'{user}@s.whatsapp.net' if deviceNum == 0 else f'{user}:{deviceNum}@s.whatsapp.net'
                if deviceNum == 99:
                    jid = f'{user}:99@hosted'
                deviceJids.append(jid)
        logger.debug(
            JSObject({'fromJid': fromJid, 'totalDevices': len(userDevices), 'devicesWithSessions': len(deviceJids), 'devices': deviceJids}),
            'bulk device migration complete - all user devices processed'
        )

        async def run():
            migrationOps = []
            for jid in deviceJids:
                lidWithDevice = transferDevice(jid, toJid)
                fromDecoded = jidDecode(jid)
                toDecoded = jidDecode(lidWithDevice)
                migrationOps.append(JSObject({
                    'fromJid': jid,
                    'toJid': lidWithDevice,
                    'pnUser': fromDecoded.get('user'),
                    'lidUser': toDecoded.get('user'),
                    'deviceId': fromDecoded.get('device') or 0,
                    'fromAddr': jidToSignalProtocolAddress(jid),
                    'toAddr': jidToSignalProtocolAddress(lidWithDevice)
                }))
            totalOps = len(migrationOps)
            migratedCount = 0

            pnAddrStrings = list(dict.fromkeys(op.get('fromAddr').toString() for op in migrationOps))
            pnSessions = await parsedKeys.get('get')('session', pnAddrStrings)

            sessionUpdates = {}
            for op in migrationOps:
                pnAddrStr = op.get('fromAddr').toString()
                lidAddrStr = op.get('toAddr').toString()
                pnSession = jsGetKey(pnSessions, pnAddrStr)
                if pnSession is not None:
                    fromSession = libsignal.SessionRecord.deserialize(pnSession)
                    if fromSession.haveOpenSession():
                        sessionUpdates[lidAddrStr] = fromSession.serialize()
                        sessionUpdates[pnAddrStr] = None
                        migratedCount += 1

            if len(sessionUpdates) > 0:
                await parsedKeys.get('set')(JSObject({'session': JSObject(sessionUpdates)}))
                logger.debug(JSObject({'migratedSessions': migratedCount}), 'bulk session migration complete')
                for op in migrationOps:
                    if sessionUpdates.get(op.get('toAddr').toString()) is not None:
                        migratedSessionCache.set(f"{op.get('pnUser')}.{op.get('deviceId')}", True)

            skippedCount = totalOps - migratedCount
            return JSObject({'migrated': migratedCount, 'skipped': skippedCount, 'total': totalOps})

        toUser = jidDecode(toJid)
        return await parsedKeys.get('transaction')(run, f"migrate-{len(deviceJids)}-sessions-{toUser.get('user') if toUser else 'undefined'}")

    return JSObject({
        'decryptGroupMessage': decryptGroupMessage,
        'processSenderKeyDistributionMessage': processSenderKeyDistributionMessage,
        'decryptMessage': decryptMessage,
        'encryptMessage': encryptMessage,
        'encryptGroupMessage': encryptGroupMessage,
        'getSenderKeyDistributionMessage': getSenderKeyDistributionMessage,
        'hasSenderKey': hasSenderKey,
        'getSessionInfo': getSessionInfo,
        'injectE2ESession': injectE2ESession,
        'jidToSignalProtocolAddress': lambda jid: jidToSignalProtocolAddress(jid).toString(),
        'lidMapping': lidMapping,
        'validateSession': validateSession,
        'deleteSession': deleteSession,
        'close': close,
        'migrateSession': migrateSession
    })


def jidToSignalProtocolAddress(jid):
    decoded = jidDecode(jid)
    user = decoded.get('user')
    device = decoded.get('device')
    server = decoded.get('server')
    domainType = decoded.get('domainType')
    if not user:
        raise JSError(f'JID decoded but user is empty: "{jid}" -> user: "{user}", server: "{server}", device: {device}')
    signalUser = f'{user}_{domainType}' if domainType != WAJIDDomains['WHATSAPP'] else user
    finalDevice = device or 0
    if device == 99 and server != 'hosted' and server != 'hosted.lid':
        raise JSError('Unexpected non-hosted device JID with device 99. This ID seems invalid. ID:' + jid)
    return libsignal.ProtocolAddress(signalUser, finalDevice)


def jidToSignalSenderKeyName(group, user):
    return SenderKeyName(group, jidToSignalProtocolAddress(user))


def signalStorage(auth, lidMapping):
    creds = auth.get('creds')
    keys = auth.get('keys')

    async def resolveLIDSignalAddress(id):
        if '.' in id:
            deviceId, device = id.split('.')[0], id.split('.')[1]
            parts = deviceId.split('_')
            user = parts[0]
            domainType_ = parts[1] if len(parts) > 1 else None
            domainType = parseIntPrefix(domainType_ or '0')
            if domainType == WAJIDDomains['LID'] or domainType == WAJIDDomains['HOSTED_LID']:
                return id
            devicePart = f':{device}' if device != '0' else ''
            serverPart = 'hosted' if domainType == WAJIDDomains['HOSTED'] else 's.whatsapp.net'
            pnJid = f'{user}{devicePart}@{serverPart}'
            lidForPN = await lidMapping.getLIDForPN(pnJid)
            if lidForPN:
                lidAddr = jidToSignalProtocolAddress(lidForPN)
                return lidAddr.toString()
        return id

    async def loadSession(id):
        try:
            wireJid = await resolveLIDSignalAddress(id)
            result = await keys.get('get')('session', [wireJid])
            sess = jsGetKey(result, wireJid)
            if sess is not None:
                return libsignal.SessionRecord.deserialize(sess)
        except Exception:
            return None
        return None

    async def storeSession(id, session):
        wireJid = await resolveLIDSignalAddress(id)
        await keys.get('set')(JSObject({'session': JSObject({wireJid: session.serialize()})}))

    async def isTrustedIdentity(*args):
        return True

    async def loadIdentityKey(id):
        wireJid = await resolveLIDSignalAddress(id)
        result = await keys.get('get')('identity-key', [wireJid])
        key = jsGetKey(result, wireJid)
        return key if key is not None else None

    async def saveIdentity(id, identityKey):
        wireJid = await resolveLIDSignalAddress(id)
        result = await keys.get('get')('identity-key', [wireJid])
        existingKey = jsGetKey(result, wireJid)
        keysMatch = existingKey is not None and len(existingKey) == len(identityKey) and all(
            byte == identityKey[i] for i, byte in enumerate(existingKey)
        )
        if existingKey is not None and not keysMatch:
            await keys.get('set')(JSObject({'session': JSObject({wireJid: None}), 'identity-key': JSObject({wireJid: identityKey})}))
            return True
        if existingKey is None:
            await keys.get('set')(JSObject({'identity-key': JSObject({wireJid: identityKey})}))
            return True
        return False

    async def loadPreKey(id):
        keyId = str(id)
        result = await keys.get('get')('pre-key', [keyId])
        key = jsGetKey(result, keyId)
        if key is not None:
            return JSObject({'privKey': asBuffer(key.get('private')), 'pubKey': asBuffer(key.get('public'))})
        return None

    async def removePreKey(id):
        return await keys.get('set')(JSObject({'pre-key': JSObject({str(id): None})}))

    async def loadSignedPreKey(*args):
        key = creds.get('signedPreKey')
        return JSObject({'privKey': asBuffer(key.get('keyPair').get('private')), 'pubKey': asBuffer(key.get('keyPair').get('public'))})

    async def loadSenderKey(senderKeyName):
        keyId = senderKeyName.toString()
        result = await keys.get('get')('sender-key', [keyId])
        key = jsGetKey(result, keyId)
        if key is not None:
            return SenderKeyRecord.deserialize(key)
        return SenderKeyRecord()

    async def storeSenderKey(senderKeyName, key):
        keyId = senderKeyName.toString()
        serialized = json.dumps(key.serialize(), default=bufferToJSON)
        await keys.get('set')(JSObject({'sender-key': JSObject({keyId: serialized.encode('utf-8')})}))

    async def getOurRegistrationId(*args):
        return creds.get('registrationId')

    async def getOurIdentity(*args):
        signedIdentityKey = creds.get('signedIdentityKey')
        return JSObject({
            'privKey': asBuffer(signedIdentityKey.get('private')),
            'pubKey': bytes(generateSignalPubKey(signedIdentityKey.get('public')))
        })

    return JSObject({
        'loadSession': loadSession,
        'storeSession': storeSession,
        'isTrustedIdentity': isTrustedIdentity,
        'loadIdentityKey': loadIdentityKey,
        'saveIdentity': saveIdentity,
        'loadPreKey': loadPreKey,
        'removePreKey': removePreKey,
        'loadSignedPreKey': loadSignedPreKey,
        'loadSenderKey': loadSenderKey,
        'storeSenderKey': storeSenderKey,
        'getOurRegistrationId': getOurRegistrationId,
        'getOurIdentity': getOurIdentity
    })


def bufferToJSON(value):
    if isinstance(value, (bytes, bytearray)):
        return JSObject({'type': 'Buffer', 'data': list(bytes(value))})
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')
