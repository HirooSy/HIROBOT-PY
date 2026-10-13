import asyncio
import base64
import inspect
import json
from baileys_lite.foundation.node_runtime import jsonDefault
import math

from baileys_lite.WAProto import proto
from baileys_lite.binary.wa_binary import getBinaryNodeChild, getBinaryNodeChildren, isJidGroup, isLidUser, isPnUser, jidNormalizedUser
from baileys_lite.constants import LabelAssociationType, WAMessageStatus
from baileys_lite.foundation.boom import Boom
from baileys_lite.foundation.long import stringToNumber
from baileys_lite.foundation.node_runtime import EventEmitter, JSError, JSObject, clearTimeout, now, setTimeout
from baileys_lite.foundation.wa_crypto import expandAppStateKeys
from baileys_lite.utils.media import downloadContentFromMessage
from baileys_lite.utils.message_compose import updateMessageWithReaction, updateMessageWithReceipt
from baileys_lite.utils.message_processing import isRealMessage, shouldIncrementChatUnread
from baileys_lite.utils.wa_protocol_core import LT_HASH_ANTI_TAMPERING, aesDecrypt, aesEncrypt, hmacSign, toNumber, trimUndefined


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


def _path(value, *names):
    current = value
    for name in names:
        if current is None:
            return None
        current = current[name] if isinstance(current, dict) else getattr(current, name, None)
    return current


def _method(obj, name):
    if isinstance(obj, dict):
        return obj[name]
    return getattr(obj, name)


def _log(logger, level, *args):
    if logger is not None:
        _method(logger, level)(*args)


def _emit(ev, event, data):
    return _method(ev, 'emit')(event, data)


def _idx(items, index):
    if items is None or len(items) <= index:
        return None
    return items[index]


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


def _jsKeys(obj):
    keys = list(dict.keys(obj))
    numeric = sorted((k for k in keys if isinstance(k, str) and k.isdigit()), key=int)
    others = [k for k in keys if not (isinstance(k, str) and k.isdigit())]
    return numeric + others


def _jsValues(obj):
    return [obj[k] for k in _jsKeys(obj)]


def _fireAndForget(result):
    if inspect.isawaitable(result):
        asyncio.ensure_future(result)


async def _maybeAwait(value):
    if inspect.isawaitable(value):
        return await value
    return value


def mutationKeys(keydata):
    keys = expandAppStateKeys(keydata)
    return JSObject({
        'indexKey': keys.get('indexKey'),
        'valueEncryptionKey': keys.get('valueEncryptionKey'),
        'valueMacKey': keys.get('valueMacKey'),
        'snapshotMacKey': keys.get('snapshotMacKey'),
        'patchMacKey': keys.get('patchMacKey')
    })


def generateMac(operation, data, keyId, key):
    opByte = 0x01 if operation == proto.SyncdMutation.SyncdOperation.SET else 0x02
    keyIdBuffer = base64.b64decode(keyId) if isinstance(keyId, str) else bytes(keyId)
    keyData = bytes([opByte]) + keyIdBuffer
    last = bytearray(8)
    last[7] = len(keyData)
    total = keyData + bytes(data) + bytes(last)
    hmac = hmacSign(total, key, 'sha512')
    return hmac[:32]


def to64BitNetworkOrder(e):
    buff = bytearray(8)
    buff[4:8] = (e & 0xFFFFFFFF).to_bytes(4, 'big')
    return bytes(buff)


def makeLtHashGenerator(state):
    indexValueMap = JSObject({**(state.get('indexValueMap') if state.get('indexValueMap') is not None else {})})
    addBuffs = []
    subBuffs = []

    def mix(params):
        indexMac = params.get('indexMac')
        valueMac = params.get('valueMac')
        operation = params.get('operation')
        indexMacBase64 = base64.b64encode(bytes(indexMac)).decode()
        prevOp = indexValueMap[indexMacBase64]
        if operation == proto.SyncdMutation.SyncdOperation.REMOVE:
            if not _truthy(prevOp):
                return
            dict.pop(indexValueMap, indexMacBase64, None)
        else:
            addBuffs.append(valueMac)
            indexValueMap[indexMacBase64] = JSObject({'valueMac': valueMac})
        if _truthy(prevOp):
            subBuffs.append(prevOp.get('valueMac'))

    def finish():
        result = LT_HASH_ANTI_TAMPERING.subtractThenAdd(state.get('hash'), subBuffs, addBuffs)
        return JSObject({'hash': bytes(result), 'indexValueMap': indexValueMap})

    return JSObject({'mix': mix, 'finish': finish})


def generateSnapshotMac(lthash, version, name, key):
    return hmacSign(bytes(lthash) + to64BitNetworkOrder(version) + name.encode('utf-8'), key, 'sha256')


def generatePatchMac(snapshotMac, valueMacs, version, type_, key):
    return hmacSign(bytes(snapshotMac) + b''.join(bytes(v) for v in valueMacs) + to64BitNetworkOrder(version) + type_.encode('utf-8'), key)


def newLTHashState():
    return JSObject({'version': 0, 'hash': bytes(128), 'indexValueMap': JSObject({})})


def ensureLTHashStateVersion(state):
    v = state.get('version')
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
        state['version'] = 0
    return state


MAX_SYNC_ATTEMPTS = 2


def isMissingKeyError(error):
    return _path(error, 'data', 'isMissingKey') is True


def isAppStateSyncIrrecoverable(error, attempts):
    name = _path(error, 'name')
    if name is None and isinstance(error, BaseException):
        name = type(error).__name__
    return attempts >= MAX_SYNC_ATTEMPTS or name == 'TypeError'


async def encodeSyncdPatch(params, myAppStateKeyId, state, getAppStateSyncKey):
    type_ = params.get('type')
    index = params.get('index')
    syncAction = params.get('syncAction')
    apiVersion = params.get('apiVersion')
    operation = params.get('operation')
    key = await _maybeAwait(getAppStateSyncKey(myAppStateKeyId)) if _truthy(myAppStateKeyId) else None
    if not _truthy(key):
        raise Boom(f'myAppStateKey ("{_numStr(myAppStateKeyId) if myAppStateKeyId is not None else "undefined"}") not present',
                   JSObject({'data': JSObject({'isMissingKey': True})}))
    encKeyId = base64.b64decode(myAppStateKeyId)
    state = JSObject({**state, 'indexValueMap': JSObject({**state.get('indexValueMap')})})
    indexBuffer = _bin(_jsonStringify(index))
    dataProto = proto.SyncActionData.fromObject(JSObject({'index': indexBuffer, 'value': syncAction, 'padding': b'', 'version': apiVersion}))
    encoded = proto.SyncActionData.encode(dataProto).finish()
    keyValue = mutationKeys(key.get('keyData'))
    encValue = aesEncrypt(encoded, keyValue.get('valueEncryptionKey'))
    valueMac = generateMac(operation, encValue, encKeyId, keyValue.get('valueMacKey'))
    indexMac = hmacSign(indexBuffer, keyValue.get('indexKey'))
    generator = makeLtHashGenerator(state)
    generator.get('mix')(JSObject({'indexMac': indexMac, 'valueMac': valueMac, 'operation': operation}))
    finished = generator.get('finish')()
    state['hash'] = finished.get('hash')
    state['indexValueMap'] = finished.get('indexValueMap')
    state['version'] += 1
    snapshotMac = generateSnapshotMac(state.get('hash'), state.get('version'), type_, keyValue.get('snapshotMacKey'))
    patch = JSObject({
        'patchMac': generatePatchMac(snapshotMac, [valueMac], state.get('version'), type_, keyValue.get('patchMacKey')),
        'snapshotMac': snapshotMac,
        'keyId': JSObject({'id': encKeyId}),
        'mutations': [JSObject({
            'operation': operation,
            'record': JSObject({
                'index': JSObject({'blob': indexMac}),
                'value': JSObject({'blob': bytes(encValue) + bytes(valueMac)}),
                'keyId': JSObject({'id': encKeyId})
            })
        })]
    })
    state.get('indexValueMap')[base64.b64encode(bytes(indexMac)).decode()] = JSObject({'valueMac': valueMac})
    return JSObject({'patch': patch, 'state': state})


async def _eachChunk(stream):
    if hasattr(stream, '__aiter__'):
        async for chunk in stream:
            yield chunk
    else:
        for chunk in stream:
            yield chunk


def _bufToStr(value):
    if value is None:
        return 'undefined'
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode('utf-8', 'replace')
    return str(value)


def _errMessage(err):
    message = getattr(err, 'message', None)
    return message if message is not None else str(err)


def _noop(*args):
    return None


def _asBytes(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, dict):
        return bytes(int(v) & 0xFF for v in _jsValues(value))
    return bytes(int(v) & 0xFF for v in value)


async def decodeSyncdMutations(msgMutations, initialState, getAppStateSyncKey, onMutation, validateMacs):
    ltGenerator = makeLtHashGenerator(initialState)
    derivedKeyCache = {}

    async def getKey(keyId):
        base64Key = base64.b64encode(bytes(keyId)).decode()
        cached = derivedKeyCache.get(base64Key)
        if cached is not None:
            return cached
        keyEnc = await _maybeAwait(getAppStateSyncKey(base64Key))
        if not _truthy(keyEnc):
            raise Boom(f'failed to find key "{base64Key}" to decode mutation', JSObject({'data': JSObject({'isMissingKey': True, 'msgMutations': msgMutations})}))
        keys = mutationKeys(keyEnc.get('keyData'))
        derivedKeyCache[base64Key] = keys
        return keys

    for msgMutation in msgMutations:
        operation = msgMutation.get('operation') if 'operation' in msgMutation else proto.SyncdMutation.SyncdOperation.SET
        record = msgMutation.get('record') if ('record' in msgMutation and msgMutation.get('record') is not None) else msgMutation
        try:
            key = await getKey(record.get('keyId').get('id'))
        except Exception as err:
            if isMissingKeyError(err):
                raise
            continue
        content = bytes(record.get('value').get('blob'))
        encContent = content[:-32]
        ogValueMac = content[-32:]
        if validateMacs:
            contentHmac = generateMac(operation, encContent, record.get('keyId').get('id'), key.get('valueMacKey'))
            if contentHmac != ogValueMac:
                continue
        try:
            result = aesDecrypt(encContent, key.get('valueEncryptionKey'))
        except Exception:
            continue
        syncAction = proto.SyncActionData.decode(result)
        if validateMacs:
            hmac = hmacSign(syncAction.get('index'), key.get('indexKey'))
            if hmac != bytes(record.get('index').get('blob')):
                raise Boom('HMAC index verification failed')
        indexStr = bytes(syncAction.get('index')).decode('utf-8', 'replace')
        onMutation(JSObject({'syncAction': syncAction, 'index': _jsonParse(indexStr)}))
        ltGenerator.get('mix')(JSObject({'indexMac': record.get('index').get('blob'), 'valueMac': ogValueMac, 'operation': operation}))
    return ltGenerator.get('finish')()


async def decodeSyncdPatch(msg, name, initialState, getAppStateSyncKey, onMutation, validateMacs):
    if validateMacs:
        base64Key = base64.b64encode(bytes(msg.get('keyId').get('id'))).decode()
        mainKeyObj = await _maybeAwait(getAppStateSyncKey(base64Key))
        if not _truthy(mainKeyObj):
            raise Boom(f'failed to find key "{base64Key}" to decode patch', JSObject({'data': JSObject({'isMissingKey': True, 'msg': msg})}))
        mainKey = mutationKeys(mainKeyObj.get('keyData'))
        mutationmacs = [bytes(m.get('record').get('value').get('blob'))[-32:] for m in msg.get('mutations')]
        patchMac = generatePatchMac(msg.get('snapshotMac'), mutationmacs, toNumber(msg.get('version').get('version')), name, mainKey.get('patchMacKey'))
        if patchMac != bytes(msg.get('patchMac')):
            raise Boom('Invalid patch mac')
    return await decodeSyncdMutations(msg.get('mutations'), initialState, getAppStateSyncKey, onMutation, validateMacs)


async def downloadExternalBlob(blob, options):
    stream = await _maybeAwait(downloadContentFromMessage(blob, 'md-app-state', JSObject({'options': options})))
    bufferArray = []
    async for chunk in _eachChunk(stream):
        bufferArray.append(bytes(chunk))
    return b''.join(bufferArray)


async def downloadExternalPatch(blob, options):
    buffer = await downloadExternalBlob(blob, options)
    return proto.SyncdMutations.decode(buffer)


async def extractSyncdPatches(result, options):
    syncNode = getBinaryNodeChild(result, 'sync')
    collectionNodes = getBinaryNodeChildren(syncNode, 'collection')
    final = JSObject({})

    async def handle(collectionNode):
        patchesNode = getBinaryNodeChild(collectionNode, 'patches')
        patches = getBinaryNodeChildren(patchesNode if patchesNode is not None else collectionNode, 'patch')
        snapshotNode = getBinaryNodeChild(collectionNode, 'snapshot')
        syncds = []
        name = collectionNode.get('attrs').get('name')
        hasMorePatches = collectionNode.get('attrs').get('has_more_patches') == 'true'
        snapshot = None
        if snapshotNode is not None and snapshotNode.get('content') is not None:
            snapshotNode['content'] = _asBytes(snapshotNode.get('content'))
            blobRef = proto.ExternalBlobReference.decode(snapshotNode.get('content'))
            data = await downloadExternalBlob(blobRef, options)
            snapshot = proto.SyncdSnapshot.decode(data)
        for patch in patches:
            content = patch.get('content')
            if content is not None:
                content = _asBytes(content)
                syncd = proto.SyncdPatch.decode(content)
                if not _truthy(syncd.get('version')):
                    syncd['version'] = JSObject({'version': _plus(collectionNode.get('attrs').get('version')) + 1})
                syncds.append(syncd)
        final[name] = JSObject({'patches': syncds, 'hasMorePatches': hasMorePatches, 'snapshot': snapshot})

    await asyncio.gather(*[handle(c) for c in collectionNodes])
    return final


async def decodeSyncdSnapshot(name, snapshot, getAppStateSyncKey, minimumVersionNumber, validateMacs=True, logger=None):
    newState = newLTHashState()
    newState['version'] = toNumber(snapshot.get('version').get('version'))
    mutationMap = JSObject({})
    areMutationsRequired = minimumVersionNumber is None or newState.get('version') > minimumVersionNumber

    def onMutation(mutation):
        index = _bufToStr(mutation.get('syncAction').get('index'))
        mutationMap[index] = mutation

    result = await decodeSyncdMutations(
        snapshot.get('records'),
        newState,
        getAppStateSyncKey,
        onMutation if areMutationsRequired else _noop,
        validateMacs
    )
    newState['hash'] = result.get('hash')
    newState['indexValueMap'] = result.get('indexValueMap')
    if validateMacs:
        base64Key = base64.b64encode(bytes(snapshot.get('keyId').get('id'))).decode()
        keyEnc = await _maybeAwait(getAppStateSyncKey(base64Key))
        if not _truthy(keyEnc):
            raise Boom(f'failed to find key "{base64Key}" to decode mutation', JSObject({'data': JSObject({'isMissingKey': True})}))
        keys = mutationKeys(keyEnc.get('keyData'))
        computedSnapshotMac = generateSnapshotMac(newState.get('hash'), newState.get('version'), name, keys.get('snapshotMacKey'))
        if bytes(snapshot.get('mac')) != computedSnapshotMac:
            _log(logger, 'warn', JSObject({'name': name, 'version': newState.get('version')}), 'LTHash verification failed on snapshot, continuing with partial state')
    return JSObject({'state': newState, 'mutationMap': mutationMap})


async def decodePatches(name, syncds, initial, getAppStateSyncKey, options, minimumVersionNumber, logger, validateMacs=True):
    newState = JSObject({**initial, 'indexValueMap': JSObject({**initial.get('indexValueMap')})})
    mutationMap = JSObject({})
    for syncd in syncds:
        version = syncd.get('version')
        keyId = syncd.get('keyId')
        snapshotMac = syncd.get('snapshotMac')
        if syncd.get('externalMutations') is not None:
            _log(logger, 'trace', JSObject({'name': name, 'version': version}), 'downloading external patch')
            ref = await downloadExternalPatch(syncd.get('externalMutations'), options)
            _log(logger, 'debug', JSObject({'name': name, 'version': version, 'mutations': len(ref.get('mutations'))}), 'downloaded external patch')
            if syncd.get('mutations') is not None:
                syncd.get('mutations').extend(ref.get('mutations'))
        patchVersion = toNumber(version.get('version'))
        newState['version'] = patchVersion
        shouldMutate = minimumVersionNumber is None or patchVersion > minimumVersionNumber

        def onMutation(mutation):
            index = _bufToStr(mutation.get('syncAction').get('index'))
            mutationMap[index] = mutation

        try:
            decodeResult = await decodeSyncdPatch(
                syncd,
                name,
                newState,
                getAppStateSyncKey,
                onMutation if shouldMutate else _noop,
                validateMacs
            )
        except Exception as err:
            if isMissingKeyError(err):
                raise
            _log(logger, 'warn', JSObject({'name': name, 'version': patchVersion, 'error': _errMessage(err)}), 'failed to decode patch, skipping')
            continue
        newState['hash'] = decodeResult.get('hash')
        newState['indexValueMap'] = decodeResult.get('indexValueMap')
        if validateMacs:
            base64Key = base64.b64encode(bytes(keyId.get('id'))).decode()
            keyEnc = await _maybeAwait(getAppStateSyncKey(base64Key))
            if not _truthy(keyEnc):
                raise Boom(f'failed to find key "{base64Key}" to decode mutation', JSObject({'data': JSObject({'isMissingKey': True})}))
            keys = mutationKeys(keyEnc.get('keyData'))
            computedSnapshotMac = generateSnapshotMac(newState.get('hash'), newState.get('version'), name, keys.get('snapshotMacKey'))
            if bytes(snapshotMac) != computedSnapshotMac:
                _log(logger, 'warn', JSObject({'name': name, 'version': newState.get('version')}), 'LTHash verification failed, skipping remaining patches')
                break
        syncd['mutations'] = []
    return JSObject({'state': newState, 'mutationMap': mutationMap})


def chatModificationToAppPatch(mod, jid):
    OP = proto.SyncdMutation.SyncdOperation

    def getMessageRange(lastMessages):
        if not isinstance(lastMessages, list):
            return lastMessages
        lastMsg = lastMessages[-1] if len(lastMessages) else None
        messages = None
        if len(lastMessages):
            messages = []
            for m in lastMessages:
                if _path(m, 'key', 'id') is None or _path(m, 'key', 'remoteJid') is None:
                    raise Boom('Incomplete key', JSObject({'statusCode': 400, 'data': m}))
                if isJidGroup(m.get('key').get('remoteJid')) and not m.get('key').get('fromMe') and not m.get('key').get('participant'):
                    raise Boom('Expected not from me message to have participant', JSObject({'statusCode': 400, 'data': m}))
                if not _truthy(m.get('messageTimestamp')) or not _truthy(toNumber(m.get('messageTimestamp'))):
                    raise Boom('Missing timestamp in last message list', JSObject({'statusCode': 400, 'data': m}))
                if _truthy(m.get('key').get('participant')):
                    m.get('key')['participant'] = jidNormalizedUser(m.get('key').get('participant'))
                messages.append(m)
        return JSObject({
            'lastMessageTimestamp': _path(lastMsg, 'messageTimestamp'),
            'messages': messages
        })

    if 'mute' in mod:
        patch = JSObject({
            'syncAction': JSObject({'muteAction': JSObject({'muted': _truthy(mod.get('mute')), 'muteEndTimestamp': _or(mod.get('mute'), None)})}),
            'index': ['mute', jid],
            'type': 'regular_high',
            'apiVersion': 2,
            'operation': OP.SET
        })
    elif 'archive' in mod:
        patch = JSObject({
            'syncAction': JSObject({'archiveChatAction': JSObject({'archived': _truthy(mod.get('archive')), 'messageRange': getMessageRange(mod.get('lastMessages'))})}),
            'index': ['archive', jid],
            'type': 'regular_low',
            'apiVersion': 3,
            'operation': OP.SET
        })
    elif 'markRead' in mod:
        patch = JSObject({
            'syncAction': JSObject({'markChatAsReadAction': JSObject({'read': mod.get('markRead'), 'messageRange': getMessageRange(mod.get('lastMessages'))})}),
            'index': ['markChatAsRead', jid],
            'type': 'regular_low',
            'apiVersion': 3,
            'operation': OP.SET
        })
    elif 'deleteForMe' in mod:
        deleteForMe = mod.get('deleteForMe')
        timestamp = deleteForMe.get('timestamp')
        key = deleteForMe.get('key')
        deleteMedia = deleteForMe.get('deleteMedia')
        patch = JSObject({
            'syncAction': JSObject({'deleteMessageForMeAction': JSObject({'deleteMedia': deleteMedia, 'messageTimestamp': timestamp})}),
            'index': ['deleteMessageForMe', jid, key.get('id'), '1' if key.get('fromMe') else '0', '0'],
            'type': 'regular_high',
            'apiVersion': 3,
            'operation': OP.SET
        })
    elif 'clear' in mod:
        patch = JSObject({
            'syncAction': JSObject({'clearChatAction': JSObject({'messageRange': getMessageRange(mod.get('lastMessages'))})}),
            'index': ['clearChat', jid, '1', '0'],
            'type': 'regular_high',
            'apiVersion': 6,
            'operation': OP.SET
        })
    elif 'pin' in mod:
        patch = JSObject({
            'syncAction': JSObject({'pinAction': JSObject({'pinned': _truthy(mod.get('pin'))})}),
            'index': ['pin_v1', jid],
            'type': 'regular_low',
            'apiVersion': 5,
            'operation': OP.SET
        })
    elif 'contact' in mod:
        patch = JSObject({
            'syncAction': JSObject({'contactAction': _or(mod.get('contact'), JSObject({}))}),
            'index': ['contact', jid],
            'type': 'critical_unblock_low',
            'apiVersion': 2,
            'operation': OP.SET if _truthy(mod.get('contact')) else OP.REMOVE
        })
    elif 'disableLinkPreviews' in mod:
        patch = JSObject({
            'syncAction': JSObject({'privacySettingDisableLinkPreviewsAction': _or(mod.get('disableLinkPreviews'), JSObject({}))}),
            'index': ['setting_disableLinkPreviews'],
            'type': 'regular',
            'apiVersion': 8,
            'operation': OP.SET
        })
    elif 'star' in mod:
        key = mod.get('star').get('messages')[0]
        patch = JSObject({
            'syncAction': JSObject({'starAction': JSObject({'starred': _truthy(mod.get('star').get('star'))})}),
            'index': ['star', jid, key.get('id'), '1' if key.get('fromMe') else '0', '0'],
            'type': 'regular_low',
            'apiVersion': 2,
            'operation': OP.SET
        })
    elif 'delete' in mod:
        patch = JSObject({
            'syncAction': JSObject({'deleteChatAction': JSObject({'messageRange': getMessageRange(mod.get('lastMessages'))})}),
            'index': ['deleteChat', jid, '1'],
            'type': 'regular_high',
            'apiVersion': 6,
            'operation': OP.SET
        })
    elif 'pushNameSetting' in mod:
        patch = JSObject({
            'syncAction': JSObject({'pushNameSetting': JSObject({'name': mod.get('pushNameSetting')})}),
            'index': ['setting_pushName'],
            'type': 'critical_block',
            'apiVersion': 1,
            'operation': OP.SET
        })
    elif 'quickReply' in mod:
        quickReply = mod.get('quickReply')
        patch = JSObject({
            'syncAction': JSObject({
                'quickReplyAction': JSObject({
                    'count': 0,
                    'deleted': _or(quickReply.get('deleted'), False),
                    'keywords': [],
                    'message': _or(quickReply.get('message'), ''),
                    'shortcut': _or(quickReply.get('shortcut'), '')
                })
            }),
            'index': ['quick_reply', _or(quickReply.get('timestamp'), str(math.floor(now() / 1000)))],
            'type': 'regular',
            'apiVersion': 2,
            'operation': OP.SET
        })
    elif 'addLabel' in mod:
        addLabel = mod.get('addLabel')
        patch = JSObject({
            'syncAction': JSObject({'labelEditAction': JSObject({
                'name': addLabel.get('name'),
                'color': addLabel.get('color'),
                'predefinedId': addLabel.get('predefinedId'),
                'deleted': addLabel.get('deleted')
            })}),
            'index': ['label_edit', addLabel.get('id')],
            'type': 'regular',
            'apiVersion': 3,
            'operation': OP.SET
        })
    elif 'addChatLabel' in mod:
        patch = JSObject({
            'syncAction': JSObject({'labelAssociationAction': JSObject({'labeled': True})}),
            'index': [LabelAssociationType['Chat'], mod.get('addChatLabel').get('labelId'), jid],
            'type': 'regular',
            'apiVersion': 3,
            'operation': OP.SET
        })
    elif 'removeChatLabel' in mod:
        patch = JSObject({
            'syncAction': JSObject({'labelAssociationAction': JSObject({'labeled': False})}),
            'index': [LabelAssociationType['Chat'], mod.get('removeChatLabel').get('labelId'), jid],
            'type': 'regular',
            'apiVersion': 3,
            'operation': OP.SET
        })
    elif 'addMessageLabel' in mod:
        patch = JSObject({
            'syncAction': JSObject({'labelAssociationAction': JSObject({'labeled': True})}),
            'index': [LabelAssociationType['Message'], mod.get('addMessageLabel').get('labelId'), jid, mod.get('addMessageLabel').get('messageId'), '0', '0'],
            'type': 'regular',
            'apiVersion': 3,
            'operation': OP.SET
        })
    elif 'removeMessageLabel' in mod:
        patch = JSObject({
            'syncAction': JSObject({'labelAssociationAction': JSObject({'labeled': False})}),
            'index': [LabelAssociationType['Message'], mod.get('removeMessageLabel').get('labelId'), jid, mod.get('removeMessageLabel').get('messageId'), '0', '0'],
            'type': 'regular',
            'apiVersion': 3,
            'operation': OP.SET
        })
    else:
        raise Boom('not supported')
    patch.get('syncAction')['timestamp'] = now()
    return patch


def processSyncAction(syncAction, ev, me, initialSyncOpts, logger):
    isInitialSync = initialSyncOpts is not None
    accountSettings = _path(initialSyncOpts, 'accountSettings')
    _log(logger, 'trace', JSObject({'syncAction': syncAction, 'initialSync': isInitialSync}), 'processing sync action')
    action = syncAction.get('syncAction').get('value')
    index = syncAction.get('index')
    type_ = _idx(index, 0)
    id_ = _idx(index, 1)
    msgId = _idx(index, 2)
    fromMe = _idx(index, 3)

    def isValidPatchBasedOnMessageRange(chat, msgRange):
        lastMsgTimestamp = _plus(_or(_path(msgRange, 'lastMessageTimestamp'), _path(msgRange, 'lastSystemMessageTimestamp'), 0))
        chatLastMsgTimestamp = _plus(_or(chat.get('lastMessageRecvTimestamp'), 0))
        return lastMsgTimestamp >= chatLastMsgTimestamp

    def getChatUpdateConditional(id__, msgRange):
        if not isInitialSync:
            return None

        def conditional(data):
            chat = _or(data.get('historySets').get('chats')[id__], data.get('chatUpserts')[id__])
            if _truthy(chat):
                return isValidPatchBasedOnMessageRange(chat, msgRange) if _truthy(msgRange) else True
            return None
        return conditional

    def has(name):
        return _truthy(_path(action, name))

    if has('muteAction'):
        muteAction = action.get('muteAction')
        _emit(ev, 'chats.update', [JSObject({
            'id': id_,
            'muteEndTime': toNumber(muteAction.get('muteEndTimestamp')) if _truthy(muteAction.get('muted')) else None,
            'conditional': getChatUpdateConditional(id_, None)
        })])
    elif has('archiveChatAction') or type_ in ('archive', 'unarchive'):
        archiveAction = _path(action, 'archiveChatAction')
        isArchived = archiveAction.get('archived') if archiveAction is not None else type_ == 'archive'
        msgRange = None if not _truthy(_path(accountSettings, 'unarchiveChats')) else _path(archiveAction, 'messageRange')
        _emit(ev, 'chats.update', [JSObject({'id': id_, 'archived': isArchived, 'conditional': getChatUpdateConditional(id_, msgRange)})])
    elif has('markChatAsReadAction'):
        markReadAction = action.get('markChatAsReadAction')
        isNullUpdate = isInitialSync and _truthy(markReadAction.get('read'))
        if isNullUpdate:
            unreadCount = None
        elif _truthy(_path(markReadAction, 'read')):
            unreadCount = 0
        else:
            unreadCount = -1
        _emit(ev, 'chats.update', [JSObject({
            'id': id_,
            'unreadCount': unreadCount,
            'conditional': getChatUpdateConditional(id_, _path(markReadAction, 'messageRange'))
        })])
    elif has('deleteMessageForMeAction') or type_ == 'deleteMessageForMe':
        _emit(ev, 'messages.delete', JSObject({'keys': [JSObject({'remoteJid': id_, 'id': msgId, 'fromMe': fromMe == '1'})]}))
    elif has('contactAction'):
        emitSyncActionResults(ev, processContactAction(action.get('contactAction'), id_, logger))
    elif has('pushNameSetting'):
        name = _path(action, 'pushNameSetting', 'name')
        if _truthy(name) and _path(me, 'name') != name:
            _emit(ev, 'creds.update', JSObject({'me': JSObject({**(me if me is not None else {}), 'name': name})}))
    elif has('pinAction'):
        pinned = _truthy(_path(action, 'pinAction', 'pinned'))
        _emit(ev, 'chats.update', [JSObject({
            'id': id_,
            'pinned': toNumber(action.get('timestamp')) if pinned else None,
            'conditional': getChatUpdateConditional(id_, None)
        })])
    elif has('unarchiveChatsSetting'):
        unarchiveChats = _truthy(action.get('unarchiveChatsSetting').get('unarchiveChats'))
        _emit(ev, 'creds.update', JSObject({'accountSettings': JSObject({'unarchiveChats': unarchiveChats})}))
        _log(logger, 'info', f"archive setting updated => '{_tpl(action.get('unarchiveChatsSetting').get('unarchiveChats'))}'")
        if accountSettings is not None:
            accountSettings['unarchiveChats'] = unarchiveChats
    elif has('starAction') or type_ == 'star':
        starred = _path(action, 'starAction', 'starred')
        if not isinstance(starred, bool):
            starred = syncAction.get('index')[-1] == '1'
        _emit(ev, 'messages.update', [JSObject({
            'key': JSObject({'remoteJid': id_, 'id': msgId, 'fromMe': fromMe == '1'}),
            'update': JSObject({'starred': starred})
        })])
    elif has('deleteChatAction') or type_ == 'deleteChat':
        if not isInitialSync:
            _emit(ev, 'chats.delete', [id_])
    elif has('labelEditAction'):
        labelEdit = action.get('labelEditAction')
        predefinedId = labelEdit.get('predefinedId')
        _emit(ev, 'labels.edit', JSObject({
            'id': id_,
            'name': labelEdit.get('name'),
            'color': labelEdit.get('color'),
            'deleted': labelEdit.get('deleted'),
            'predefinedId': _numStr(predefinedId) if _truthy(predefinedId) else None
        }))
    elif has('labelAssociationAction'):
        labeled = _truthy(action.get('labelAssociationAction').get('labeled'))
        if type_ == LabelAssociationType['Chat']:
            association = JSObject({'type': LabelAssociationType['Chat'], 'chatId': index[2], 'labelId': index[1]})
        else:
            association = JSObject({'type': LabelAssociationType['Message'], 'chatId': index[2], 'messageId': index[3], 'labelId': index[1]})
        _emit(ev, 'labels.association', JSObject({'type': 'add' if labeled else 'remove', 'association': association}))
    elif _truthy(_path(action, 'localeSetting', 'locale')):
        _emit(ev, 'settings.update', JSObject({'setting': 'locale', 'value': action.get('localeSetting').get('locale')}))
    elif has('timeFormatAction'):
        _emit(ev, 'settings.update', JSObject({'setting': 'timeFormat', 'value': action.get('timeFormatAction')}))
    elif has('pnForLidChatAction'):
        if _truthy(action.get('pnForLidChatAction').get('pnJid')):
            _emit(ev, 'lid-mapping.update', JSObject({'lid': id_, 'pn': action.get('pnForLidChatAction').get('pnJid')}))
    elif has('privacySettingRelayAllCalls'):
        _emit(ev, 'settings.update', JSObject({'setting': 'privacySettingRelayAllCalls', 'value': action.get('privacySettingRelayAllCalls')}))
    elif has('statusPrivacy'):
        _emit(ev, 'settings.update', JSObject({'setting': 'statusPrivacy', 'value': action.get('statusPrivacy')}))
    elif has('lockChatAction'):
        _emit(ev, 'chats.lock', JSObject({'id': id_, 'locked': _truthy(action.get('lockChatAction').get('locked'))}))
    elif has('privacySettingDisableLinkPreviewsAction'):
        _emit(ev, 'settings.update', JSObject({'setting': 'disableLinkPreviews', 'value': action.get('privacySettingDisableLinkPreviewsAction')}))
    elif has('notificationActivitySettingAction') and _truthy(_path(action, 'notificationActivitySettingAction', 'notificationActivitySetting')):
        _emit(ev, 'settings.update', JSObject({'setting': 'notificationActivitySetting', 'value': action.get('notificationActivitySettingAction').get('notificationActivitySetting')}))
    elif has('lidContactAction'):
        lidContact = action.get('lidContactAction')
        _emit(ev, 'contacts.upsert', [JSObject({
            'id': id_,
            'name': _or(lidContact.get('fullName'), lidContact.get('firstName'), lidContact.get('username'), None),
            'username': _or(lidContact.get('username'), None),
            'lid': id_,
            'phoneNumber': None
        })])
    elif has('privacySettingChannelsPersonalisedRecommendationAction'):
        _emit(ev, 'settings.update', JSObject({'setting': 'channelsPersonalisedRecommendation', 'value': action.get('privacySettingChannelsPersonalisedRecommendationAction')}))
    else:
        _log(logger, 'debug', JSObject({'syncAction': syncAction, 'id': id_}), 'unprocessable update')


def processContactAction(action, id_, logger):
    results = []
    if not _truthy(id_):
        _log(logger, 'warn', JSObject({'hasFullName': _truthy(action.get('fullName')), 'hasLidJid': _truthy(action.get('lidJid')), 'hasPnJid': _truthy(action.get('pnJid'))}), 'contactAction sync: missing id in index')
        return results
    lidJid = action.get('lidJid')
    idIsPn = isPnUser(id_)
    phoneNumber = id_ if idIsPn else _or(action.get('pnJid'), None)
    results.append(JSObject({
        'event': 'contacts.upsert',
        'data': [JSObject({
            'id': id_,
            'name': _or(action.get('fullName'), action.get('firstName'), action.get('username'), None),
            'username': _or(action.get('username'), None),
            'lid': _or(lidJid, None),
            'phoneNumber': phoneNumber
        })]
    }))
    if _truthy(lidJid) and isLidUser(lidJid) and idIsPn:
        results.append(JSObject({'event': 'lid-mapping.update', 'data': JSObject({'lid': lidJid, 'pn': id_})}))
    return results


def emitSyncActionResults(ev, results):
    for result in results:
        if result.get('event') == 'contacts.upsert':
            _emit(ev, 'contacts.upsert', result.get('data'))
        else:
            _emit(ev, 'lid-mapping.update', result.get('data'))


BUFFERABLE_EVENT_SET = {
    'messaging-history.set',
    'chats.upsert',
    'chats.update',
    'chats.delete',
    'contacts.upsert',
    'contacts.update',
    'messages.upsert',
    'messages.update',
    'messages.delete',
    'messages.reaction',
    'message-receipt.update',
    'groups.update'
}


def makeBufferData():
    return JSObject({
        'historySets': JSObject({'chats': JSObject({}), 'messages': JSObject({}), 'contacts': JSObject({}), 'isLatest': False, 'empty': True}),
        'chatUpserts': JSObject({}),
        'chatUpdates': JSObject({}),
        'chatDeletes': set(),
        'contactUpserts': JSObject({}),
        'contactUpdates': JSObject({}),
        'messageUpserts': JSObject({}),
        'messageUpdates': JSObject({}),
        'messageReactions': JSObject({}),
        'messageDeletes': JSObject({}),
        'messageReceipts': JSObject({}),
        'groupUpdates': JSObject({})
    })


def stringifyMessageKey(key):
    return f"{_tpl(key.get('remoteJid'))},{_tpl(key.get('id'))},{'1' if key.get('fromMe') else '0'}"


def _tpl(value):
    if value is None:
        return 'undefined'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _isNumber(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def concatChats(a, b):
    if 'unreadCount' in b and b.get('unreadCount') is None and _isNumber(a.get('unreadCount')) and a.get('unreadCount') < 0:
        a['unreadCount'] = None
        b['unreadCount'] = None
    if _isNumber(a.get('unreadCount')) and _isNumber(b.get('unreadCount')):
        b = JSObject({**b})
        if b.get('unreadCount') >= 0:
            b['unreadCount'] = max(b.get('unreadCount'), 0) + max(a.get('unreadCount'), 0)
    a.update(b)
    return a


def consolidateEvents(data):
    map_ = JSObject({})
    historySets = data.get('historySets')
    if not historySets.get('empty'):
        map_['messaging-history.set'] = JSObject({
            'chats': _jsValues(historySets.get('chats')),
            'messages': _jsValues(historySets.get('messages')),
            'contacts': _jsValues(historySets.get('contacts')),
            'pastParticipants': historySets.get('pastParticipants'),
            'syncType': historySets.get('syncType'),
            'progress': historySets.get('progress'),
            'isLatest': historySets.get('isLatest'),
            'chunkOrder': historySets.get('chunkOrder'),
            'peerDataRequestSessionId': historySets.get('peerDataRequestSessionId')
        })
    chatUpsertList = _jsValues(data.get('chatUpserts'))
    if len(chatUpsertList):
        map_['chats.upsert'] = chatUpsertList
    chatUpdateList = _jsValues(data.get('chatUpdates'))
    if len(chatUpdateList):
        map_['chats.update'] = chatUpdateList
    chatDeleteList = list(data.get('chatDeletes'))
    if len(chatDeleteList):
        map_['chats.delete'] = chatDeleteList
    messageUpsertList = _jsValues(data.get('messageUpserts'))
    if len(messageUpsertList):
        map_['messages.upsert'] = JSObject({'messages': [m.get('message') for m in messageUpsertList], 'type': messageUpsertList[0].get('type')})
    messageUpdateList = _jsValues(data.get('messageUpdates'))
    if len(messageUpdateList):
        map_['messages.update'] = messageUpdateList
    messageDeleteList = _jsValues(data.get('messageDeletes'))
    if len(messageDeleteList):
        map_['messages.delete'] = JSObject({'keys': messageDeleteList})
    messageReactionList = [
        JSObject({'key': entry.get('key'), 'reaction': reaction})
        for entry in _jsValues(data.get('messageReactions'))
        for reaction in entry.get('reactions')
    ]
    if len(messageReactionList):
        map_['messages.reaction'] = messageReactionList
    messageReceiptList = [
        JSObject({'key': entry.get('key'), 'receipt': receipt})
        for entry in _jsValues(data.get('messageReceipts'))
        for receipt in entry.get('userReceipt')
    ]
    if len(messageReceiptList):
        map_['message-receipt.update'] = messageReceiptList
    contactUpsertList = _jsValues(data.get('contactUpserts'))
    if len(contactUpsertList):
        map_['contacts.upsert'] = contactUpsertList
    contactUpdateList = _jsValues(data.get('contactUpdates'))
    if len(contactUpdateList):
        map_['contacts.update'] = contactUpdateList
    groupUpdateList = _jsValues(data.get('groupUpdates'))
    if len(groupUpdateList):
        map_['groups.update'] = groupUpdateList
    return map_


def append(data, historyCache, event, eventData, logger):
    def absorbingChatUpdate(existing):
        chatId = _or(existing.get('id'), '')
        update = data.get('chatUpdates')[chatId]
        if update is not None:
            conditionMatches = update.get('conditional')(data) if update.get('conditional') is not None else True
            if _truthy(conditionMatches):
                dict.pop(update, 'conditional', None)
                _log(logger, 'debug', JSObject({'chatId': chatId}), 'absorbed chat update in existing chat')
                existing.update(concatChats(update, existing))
                dict.pop(data.get('chatUpdates'), chatId, None)
            elif conditionMatches is False:
                _log(logger, 'debug', JSObject({'chatId': chatId}), 'chat update condition fail, removing')
                dict.pop(data.get('chatUpdates'), chatId, None)

    def decrementChatReadCounterIfMsgDidUnread(message):
        chatId = message.get('key').get('remoteJid')
        chat = _or(data.get('chatUpdates')[chatId], data.get('chatUpserts')[chatId])
        if chat is not None and isRealMessage(message) and shouldIncrementChatUnread(message) and _isNumber(chat.get('unreadCount')) and chat.get('unreadCount') > 0:
            _log(logger, 'debug', JSObject({'chatId': chat.get('id')}), 'decrementing chat counter')
            chat['unreadCount'] -= 1
            if chat.get('unreadCount') == 0:
                dict.pop(chat, 'unreadCount', None)

    if event == 'messaging-history.set':
        historySets = data.get('historySets')
        for chat in eventData.get('chats'):
            id_ = _or(chat.get('id'), '')
            existingChat = historySets.get('chats')[id_]
            if existingChat is not None:
                existingChat['endOfHistoryTransferType'] = chat.get('endOfHistoryTransferType')
            if existingChat is None and id_ not in historyCache:
                historySets.get('chats')[id_] = chat
                historyCache.add(id_)
                absorbingChatUpdate(chat)
        for contact in eventData.get('contacts'):
            existingContact = historySets.get('contacts')[contact.get('id')]
            if existingContact is not None:
                existingContact.update(trimUndefined(contact))
            else:
                historyContactId = f"c:{_tpl(contact.get('id'))}"
                hasAnyName = _or(contact.get('notify'), contact.get('name'), contact.get('verifiedName'))
                if historyContactId not in historyCache or _truthy(hasAnyName):
                    historySets.get('contacts')[contact.get('id')] = contact
                    historyCache.add(historyContactId)
        for message in eventData.get('messages'):
            key = stringifyMessageKey(message.get('key'))
            existingMsg = historySets.get('messages')[key]
            if existingMsg is None and key not in historyCache:
                historySets.get('messages')[key] = message
                historyCache.add(key)
        historySets['empty'] = False
        historySets['syncType'] = eventData.get('syncType')
        if len(_or(eventData.get('pastParticipants'), [])):
            merged = {}

            def sigOf(p):
                return f"{_tpl(_or(p.get('userJid'), ''))}:{_tpl(_or(p.get('leaveTs'), ''))}:{_tpl(_or(p.get('leaveReason'), ''))}"

            def ingest(entry):
                key = entry.get('groupJid') if entry.get('groupJid') is not None else _jsonStringify(entry)
                existing = merged.get(key)
                if existing is None:
                    merged[key] = JSObject({**entry, 'pastParticipants': list(_or(entry.get('pastParticipants'), []))})
                    return
                seen = set(sigOf(p) for p in _or(existing.get('pastParticipants'), []))
                for p in _or(entry.get('pastParticipants'), []):
                    sig = sigOf(p)
                    if sig not in seen:
                        existing.get('pastParticipants').append(p)
                        seen.add(sig)

            for entry in _or(historySets.get('pastParticipants'), []):
                ingest(entry)
            for entry in eventData.get('pastParticipants'):
                ingest(entry)
            historySets['pastParticipants'] = list(merged.values())
        historySets['progress'] = eventData.get('progress')
        historySets['chunkOrder'] = eventData.get('chunkOrder')
        historySets['peerDataRequestSessionId'] = eventData.get('peerDataRequestSessionId')
        historySets['isLatest'] = _or(eventData.get('isLatest'), historySets.get('isLatest'))
    elif event == 'chats.upsert':
        for chat in eventData:
            id_ = _or(chat.get('id'), '')
            upsert = data.get('chatUpserts')[id_]
            if _truthy(id_) and upsert is None:
                upsert = data.get('historySets').get('chats')[id_]
                if upsert is not None:
                    _log(logger, 'debug', JSObject({'chatId': id_}), 'absorbed chat upsert in chat set')
            if upsert is not None:
                upsert = concatChats(upsert, chat)
            else:
                upsert = chat
                data.get('chatUpserts')[id_] = upsert
            absorbingChatUpdate(upsert)
            data.get('chatDeletes').discard(id_)
    elif event == 'chats.update':
        for update in eventData:
            chatId = update.get('id')
            conditionMatches = update.get('conditional')(data) if update.get('conditional') is not None else True
            if _truthy(conditionMatches):
                dict.pop(update, 'conditional', None)
                upsert = _or(data.get('historySets').get('chats')[chatId], data.get('chatUpserts')[chatId])
                if _truthy(upsert):
                    concatChats(upsert, update)
                else:
                    chatUpdate = _or(data.get('chatUpdates')[chatId], JSObject({}))
                    data.get('chatUpdates')[chatId] = concatChats(chatUpdate, update)
            elif conditionMatches is None:
                data.get('chatUpdates')[chatId] = update
            data.get('chatDeletes').discard(chatId)
    elif event == 'chats.delete':
        for chatId in eventData:
            data.get('chatDeletes').add(chatId)
            dict.pop(data.get('chatUpdates'), chatId, None)
            dict.pop(data.get('chatUpserts'), chatId, None)
            dict.pop(data.get('historySets').get('chats'), chatId, None)
    elif event == 'contacts.upsert':
        for contact in eventData:
            upsert = data.get('contactUpserts')[contact.get('id')]
            if upsert is None:
                upsert = data.get('historySets').get('contacts')[contact.get('id')]
                if upsert is not None:
                    _log(logger, 'debug', JSObject({'contactId': contact.get('id')}), 'absorbed contact upsert in contact set')
            if upsert is not None:
                upsert.update(trimUndefined(contact))
            else:
                upsert = contact
                data.get('contactUpserts')[contact.get('id')] = upsert
            if data.get('contactUpdates')[contact.get('id')] is not None:
                data.get('contactUpdates')[contact.get('id')].update(trimUndefined(contact))
                dict.pop(data.get('contactUpdates'), contact.get('id'), None)
    elif event == 'contacts.update':
        for update in eventData:
            id_ = update.get('id')
            upsert = _or(data.get('historySets').get('contacts')[id_], data.get('contactUpserts')[id_])
            if upsert is not None:
                upsert.update(update)
            else:
                contactUpdate = _or(data.get('contactUpdates')[id_], JSObject({}))
                contactUpdate.update(update)
                data.get('contactUpdates')[id_] = contactUpdate
    elif event == 'messages.upsert':
        messages = eventData.get('messages')
        type_ = eventData.get('type')
        for message in messages:
            key = stringifyMessageKey(message.get('key'))
            existing = data.get('messageUpserts')[key].get('message') if data.get('messageUpserts')[key] is not None else None
            if existing is None:
                existing = data.get('historySets').get('messages')[key]
                if existing is not None:
                    _log(logger, 'debug', JSObject({'messageId': key}), 'absorbed message upsert in message set')
            if existing is not None:
                message['messageTimestamp'] = existing.get('messageTimestamp')
            if data.get('messageUpdates')[key] is not None:
                _log(logger, 'debug', 'absorbed prior message update in message upsert')
                message.update(data.get('messageUpdates')[key].get('update'))
                dict.pop(data.get('messageUpdates'), key, None)
            if data.get('historySets').get('messages')[key] is not None:
                data.get('historySets').get('messages')[key] = message
            else:
                prevType = data.get('messageUpserts')[key].get('type') if data.get('messageUpserts')[key] is not None else None
                data.get('messageUpserts')[key] = JSObject({'message': message, 'type': 'notify' if (type_ == 'notify' or prevType == 'notify') else type_})
    elif event == 'messages.update':
        for entry in eventData:
            key = entry.get('key')
            update = entry.get('update')
            keyStr = stringifyMessageKey(key)
            upserted = data.get('messageUpserts')[keyStr]
            existing = _or(data.get('historySets').get('messages')[keyStr], upserted.get('message') if upserted is not None else None)
            if existing is not None:
                existing.update(update)
                if update.get('status') == WAMessageStatus.READ and not key.get('fromMe'):
                    decrementChatReadCounterIfMsgDidUnread(existing)
            else:
                msgUpdate = _or(data.get('messageUpdates')[keyStr], JSObject({'key': key, 'update': JSObject({})}))
                msgUpdate.get('update').update(update)
                data.get('messageUpdates')[keyStr] = msgUpdate
    elif event == 'messages.delete':
        if 'keys' in eventData:
            for key in eventData.get('keys'):
                keyStr = stringifyMessageKey(key)
                if data.get('messageDeletes')[keyStr] is None:
                    data.get('messageDeletes')[keyStr] = key
                dict.pop(data.get('messageUpserts'), keyStr, None)
                dict.pop(data.get('messageUpdates'), keyStr, None)
    elif event == 'messages.reaction':
        for entry in eventData:
            key = entry.get('key')
            reaction = entry.get('reaction')
            keyStr = stringifyMessageKey(key)
            existing = data.get('messageUpserts')[keyStr]
            if existing is not None:
                updateMessageWithReaction(existing.get('message'), reaction)
            else:
                data.get('messageReactions')[keyStr] = _or(data.get('messageReactions')[keyStr], JSObject({'key': key, 'reactions': []}))
                updateMessageWithReaction(data.get('messageReactions')[keyStr], reaction)
    elif event == 'message-receipt.update':
        for entry in eventData:
            key = entry.get('key')
            receipt = entry.get('receipt')
            keyStr = stringifyMessageKey(key)
            existing = data.get('messageUpserts')[keyStr]
            if existing is not None:
                updateMessageWithReceipt(existing.get('message'), receipt)
            else:
                data.get('messageReceipts')[keyStr] = _or(data.get('messageReceipts')[keyStr], JSObject({'key': key, 'userReceipt': []}))
                updateMessageWithReceipt(data.get('messageReceipts')[keyStr], receipt)
    elif event == 'groups.update':
        for update in eventData:
            id_ = update.get('id')
            if data.get('groupUpdates')[id_] is None:
                data.get('groupUpdates')[id_] = JSObject({**update})
    else:
        raise JSError(f'"{event}" cannot be buffered')


def makeEventBuffer(logger):
    ev = EventEmitter()
    historyCache = set()
    data = makeBufferData()
    isBuffering = False
    bufferTimeout = None
    flushPendingTimeout = None
    bufferCount = 0
    MAX_HISTORY_CACHE_SIZE = 10000
    BUFFER_TIMEOUT_MS = 30000

    def dispatchEvents(map_):
        for event in _jsKeys(map_):
            ev.emit(event, map_[event])

    ev.on('event', dispatchEvents)

    def buffer():
        nonlocal isBuffering, bufferCount, bufferTimeout
        if not isBuffering:
            _log(logger, 'debug', 'Event buffer activated')
            isBuffering = True
            bufferCount = 0
            if bufferTimeout is not None:
                clearTimeout(bufferTimeout)

            def onBufferTimeout():
                if isBuffering:
                    _log(logger, 'warn', 'Buffer timeout reached, auto-flushing')
                    flush()

            bufferTimeout = setTimeout(onBufferTimeout, BUFFER_TIMEOUT_MS)
        bufferCount += 1

    def flush():
        nonlocal isBuffering, bufferCount, bufferTimeout, flushPendingTimeout, data
        if not isBuffering:
            return False
        _log(logger, 'debug', JSObject({'bufferCount': bufferCount}), 'Flushing event buffer')
        isBuffering = False
        bufferCount = 0
        if bufferTimeout is not None:
            clearTimeout(bufferTimeout)
            bufferTimeout = None
        if flushPendingTimeout is not None:
            clearTimeout(flushPendingTimeout)
            flushPendingTimeout = None
        if len(historyCache) > MAX_HISTORY_CACHE_SIZE:
            _log(logger, 'debug', JSObject({'cacheSize': len(historyCache)}), 'Clearing history cache')
            historyCache.clear()
        newData = makeBufferData()
        conditionalChatUpdatesLeft = 0
        for update in _jsValues(data.get('chatUpdates')):
            if update.get('conditional') is not None:
                conditionalChatUpdatesLeft += 1
                newData.get('chatUpdates')[update.get('id')] = update
                dict.pop(data.get('chatUpdates'), update.get('id'), None)
        consolidatedData = consolidateEvents(data)
        if len(dict.keys(consolidatedData)) > 0:
            ev.emit('event', consolidatedData)
        data = newData
        _log(logger, 'trace', JSObject({'conditionalChatUpdatesLeft': conditionalChatUpdatesLeft}), 'released buffered events')
        return True

    def process(handler):
        def listener(map_):
            _fireAndForget(handler(map_))
        ev.on('event', listener)
        return lambda: ev.off('event', listener)

    def emit(event, evData):
        if event == 'messages.upsert':
            type_ = evData.get('type')
            existingUpserts = _jsValues(data.get('messageUpserts'))
            if len(existingUpserts) > 0:
                bufferedType = existingUpserts[0].get('type')
                if bufferedType != type_:
                    _log(logger, 'debug', JSObject({'bufferedType': bufferedType, 'newType': type_}), 'messages.upsert type mismatch, emitting buffered messages')
                    ev.emit('event', JSObject({'messages.upsert': JSObject({'messages': [m.get('message') for m in existingUpserts], 'type': bufferedType})}))
                    data['messageUpserts'] = JSObject({})
        if isBuffering and event in BUFFERABLE_EVENT_SET:
            append(data, historyCache, event, evData, logger)
            return True
        return ev.emit('event', JSObject({event: evData}))

    def isBufferingFn():
        return isBuffering

    def createBufferedFunction(work):
        async def wrapped(*args):
            nonlocal bufferCount, flushPendingTimeout
            buffer()
            try:
                result = await _maybeAwait(work(*args))
                if bufferCount == 1:
                    def checkFlush():
                        if isBuffering and bufferCount == 1:
                            flush()
                    setTimeout(checkFlush, 100)
                return result
            finally:
                bufferCount = max(0, bufferCount - 1)
                if bufferCount == 0 and flushPendingTimeout is None:
                    flushPendingTimeout = setTimeout(flush, 100)
        return wrapped

    def destroy():
        nonlocal isBuffering, bufferCount, bufferTimeout, flushPendingTimeout, data
        if bufferTimeout is not None:
            clearTimeout(bufferTimeout)
            bufferTimeout = None
        if flushPendingTimeout is not None:
            clearTimeout(flushPendingTimeout)
            flushPendingTimeout = None
        historyCache.clear()
        data = makeBufferData()
        isBuffering = False
        bufferCount = 0
        ev.removeAllListeners()
        _log(logger, 'debug', 'Event buffer destroyed')

    return JSObject({
        'process': process,
        'emit': emit,
        'isBuffering': isBufferingFn,
        'buffer': buffer,
        'flush': flush,
        'createBufferedFunction': createBufferedFunction,
        'on': lambda *args: ev.on(*args),
        'off': lambda *args: ev.off(*args),
        'removeAllListeners': lambda *args: ev.removeAllListeners(*args),
        'destroy': destroy
    })
