import asyncio
import re

from baileys_lite.foundation.node_runtime import JSError, JSObject, now, setTimeout
from baileys_lite.voip.shim.baileys_resolve import resolveBaileysModule
from baileys_lite.voip.shim.protocol import parseSignalAddressFromJid
from baileys_lite.voip.shim.util import optGet


SESSION_CACHE_TTL_MS = 5 * 60 * 1000
TC_TOKEN_REQUEST_TIMEOUT_MS = 3500

cachedBaileysHelpers = None


async def loadBaileysHelpers():
    global cachedBaileysHelpers
    if cachedBaileysHelpers is not None:
        return cachedBaileysHelpers
    mod = await resolveBaileysModule()
    cachedBaileysHelpers = JSObject({
        'jidDecode': getattr(mod, 'jidDecode', None),
        'jidEncode': getattr(mod, 'jidEncode', None),
        'parseAndInjectE2ESessions': getattr(mod, 'parseAndInjectE2ESessions', None),
        'encodeSignedDeviceIdentity': getattr(mod, 'encodeSignedDeviceIdentity', None),
        'getBinaryNodeChild': getattr(mod, 'getBinaryNodeChild', None),
        'getAllBinaryNodeChildren': getattr(mod, 'getAllBinaryNodeChildren', None),
    })
    return cachedBaileysHelpers


def addressToJid(address):
    if optGet(address, 'device'):
        return f"{address.get('user')}:{address.get('device')}@{address.get('server')}"
    return f"{address.get('user')}@{address.get('server')}"


def normalizeDeviceList(baileysHelpers, jids):
    jidDecode = baileysHelpers.get('jidDecode')
    jidEncode = baileysHelpers.get('jidEncode')
    result = {}
    for jid in jids:
        decoded = jidDecode(jid)
        if not optGet(decoded, 'user'):
            result[jid] = None
            continue
        server = 'lid' if str(jid).endswith('@lid') else 's.whatsapp.net'
        result[jidEncode(decoded.get('user'), server)] = None
        if optGet(decoded, 'device') is not None:
            result[f"{decoded.get('user')}:{decoded.get('device')}@{server}"] = None
    return list(dict.keys(result))[:5]


async def resolvePeerLid(sock, target):
    raw = str(target or '').strip()
    if not raw:
        raise JSError('resolvePeerLid: target is required')
    if raw.endswith('@lid'):
        return raw
    pnJid = raw if '@' in raw else re.sub(r'[^0-9]', '', raw) + '@s.whatsapp.net'
    mapping = optGet(optGet(sock, 'signalRepository'), 'lidMapping')
    lid = None if mapping is None else await mapping.getLIDForPN(pnJid)
    if lid:
        return lid
    return pnJid


async def discoverPeerDevices(sock, baileysHelpers, peerLid):
    devices = await sock.get('getUSyncDevices')([peerLid], True, False)
    jids = [jid for jid in (optGet(device, 'jid') for device in devices) if jid]
    if not jids:
        return []
    return normalizeDeviceList(baileysHelpers, jids)


async def ensureSignalSessions(sock, baileysHelpers, jids, cache):
    repo = sock.get('signalRepository')
    missing = []
    for jid in list(dict.keys(dict.fromkeys([j for j in jids if j]))):
        signalId = repo.get('jidToSignalProtocolAddress')(jid)
        cachedAt = cache.get(signalId)
        if cachedAt and now() - cachedAt < SESSION_CACHE_TTL_MS:
            continue
        validation = await repo.get('validateSession')(jid)
        if validation is not None and optGet(validation, 'exists'):
            cache[signalId] = now()
            continue
        missing.append(jid)
    if not missing:
        return None
    sessionNode = await sock.get('query')(JSObject({
        'tag': 'iq',
        'attrs': JSObject({'xmlns': 'encrypt', 'type': 'get', 'to': 's.whatsapp.net'}),
        'content': [
            JSObject({
                'tag': 'key',
                'attrs': JSObject({}),
                'content': [JSObject({'tag': 'user', 'attrs': JSObject({'jid': jid})}) for jid in missing],
            }),
        ],
    }))
    await baileysHelpers.get('parseAndInjectE2ESessions')(sessionNode, repo)
    for jid in missing:
        cache[sock.get('signalRepository').get('jidToSignalProtocolAddress')(jid)] = now()
    return None


def toBareJid(jid):
    text = str(jid)
    at = text.find('@')
    if at < 0:
        return jid
    user = text[:at].split(':')[0]
    return f'{user}@{text[at + 1:]}'


async def issueTcToken(sock, baileysHelpers, jid):
    userJid = toBareJid(jid)
    issuedAt = now() // 1000
    try:
        response = await sock.get('query')(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': 's.whatsapp.net', 'type': 'set', 'xmlns': 'privacy', 'id': sock.get('generateMessageTag')()}),
            'content': [
                JSObject({
                    'tag': 'tokens',
                    'attrs': JSObject({}),
                    'content': [JSObject({'tag': 'token', 'attrs': JSObject({'jid': userJid, 't': str(issuedAt), 'type': 'trusted_contact'})})],
                }),
            ],
        }))
        getBinaryNodeChild = baileysHelpers.get('getBinaryNodeChild')
        getAllBinaryNodeChildren = baileysHelpers.get('getAllBinaryNodeChildren')
        tokensNode = getBinaryNodeChild(response, 'tokens')
        tokenNodes = [c for c in getAllBinaryNodeChildren(tokensNode) if optGet(c, 'tag') == 'token'] if tokensNode is not None else []
        for tokenNode in tokenNodes:
            if tokenNode.get('attrs').get('type') != 'trusted_contact':
                continue
            content = tokenNode.get('content')
            if isinstance(content, (bytes, bytearray)) and len(content) > 0:
                t = tokenNode.get('attrs').get('t')
                timestamp = issuedAt if t is None else t
                await sock.get('authState').get('keys').get('set')(JSObject({
                    'tctoken': JSObject({userJid: JSObject({'token': bytes(content), 'timestamp': str(timestamp)})}),
                }))
                return bytes(content)
        return None
    except Exception:
        return None


async def getCachedTcToken(sock, jid):
    try:
        result = await sock.get('authState').get('keys').get('get')('tctoken', [jid])
        cached = optGet(result, jid)
        token = optGet(cached, 'token')
        if token is not None and len(token) > 0:
            return bytes(token)
        return None
    except Exception:
        return None


async def ensureTcToken(sock, baileysHelpers, jid):
    bare = toBareJid(jid)
    cached = await getCachedTcToken(sock, bare)
    if cached is not None and len(cached) > 0:
        return cached
    timeout = asyncio.get_running_loop().create_future()

    def expire():
        if not timeout.done():
            timeout.set_result(None)

    setTimeout(expire, TC_TOKEN_REQUEST_TIMEOUT_MS)
    issue = asyncio.ensure_future(issueTcToken(sock, baileysHelpers, bare))
    await asyncio.wait([issue, timeout], return_when=asyncio.FIRST_COMPLETED)
    if issue.done():
        return issue.result()
    return None


async def createVoipDeps(sock):
    baileysHelpers = await loadBaileysHelpers()
    sessionCache = {}

    def getCurrentCredentials():
        creds = optGet(optGet(sock, 'authState'), 'creds')
        account = optGet(creds, 'account')
        signedIdentity = None
        if account is not None:
            try:
                signedIdentity = baileysHelpers.get('encodeSignedDeviceIdentity')(account, True)
            except Exception:
                signedIdentity = None
        me = optGet(creds, 'me')
        return JSObject({
            'meLid': optGet(me, 'lid'),
            'meJid': optGet(me, 'id'),
            'signedIdentity': signedIdentity,
        })

    async def sendNode(node):
        await sock.get('sendNode')(node)

    async def syncDeviceList(jids):
        out = []
        for jid in jids:
            try:
                peerLid = await resolvePeerLid(sock, jid)
                deviceJids = await discoverPeerDevices(sock, baileysHelpers, peerLid)
                out.append(JSObject({'deviceJids': deviceJids}))
            except Exception:
                out.append(JSObject({'deviceJids': []}))
        return out

    async def queryLidsByPhoneJids(jids):
        out = []
        for jid in jids:
            try:
                out.append(JSObject({'lidJid': await resolvePeerLid(sock, jid)}))
            except Exception:
                out.append(JSObject({'lidJid': None}))
        return out

    async def ensureSessionsBatch(devices):
        await ensureSignalSessions(sock, baileysHelpers, devices, sessionCache)
        return [JSObject({'address': parseSignalAddressFromJid(jid), 'session': None}) for jid in devices]

    async def encryptMessage(address, plaintext):
        jid = addressToJid(address)
        result = await sock.get('signalRepository').get('encryptMessage')(JSObject({'jid': jid, 'data': bytes(plaintext)}))
        return JSObject({'type': optGet(result, 'type'), 'ciphertext': bytes(optGet(result, 'ciphertext'))})

    async def decryptMessage(address, encrypted):
        jid = addressToJid(address)
        decrypted = await sock.get('signalRepository').get('decryptMessage')(JSObject({
            'jid': jid,
            'type': optGet(encrypted, 'type'),
            'ciphertext': optGet(encrypted, 'ciphertext'),
        }))
        return bytes(decrypted)

    async def syncSignalSession(jid):
        await ensureSignalSessions(sock, baileysHelpers, [jid], sessionCache)

    async def getByJid(jid):
        token = await ensureTcToken(sock, baileysHelpers, jid)
        if token is not None and len(token) > 0:
            return JSObject({'tcToken': token})
        return None

    deps = JSObject({
        'authClient': JSObject({'getCurrentCredentials': getCurrentCredentials}),
        'lowLevelCoordinator': JSObject({'sendNode': sendNode}),
        'signalDeviceSync': JSObject({
            'syncDeviceList': syncDeviceList,
            'queryLidsByPhoneJids': queryLidsByPhoneJids,
        }),
        'sessionResolver': JSObject({'ensureSessionsBatch': ensureSessionsBatch}),
        'signalProtocol': JSObject({
            'encryptMessage': encryptMessage,
            'decryptMessage': decryptMessage,
        }),
        'messageDispatch': JSObject({'syncSignalSession': syncSignalSession}),
    })
    stores = JSObject({'privacyToken': JSObject({'getByJid': getByJid})})
    return JSObject({'deps': deps, 'stores': stores})
