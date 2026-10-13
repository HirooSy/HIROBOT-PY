import asyncio
import base64
import json
from baileys.foundation.node_runtime import jsonDefault
import os
import sqlite3

from baileys.foundation.cache import Cache
from baileys.foundation.concurrency import makeKeyedMutex
from baileys.foundation.long import jsParseInt
from baileys.foundation.node_runtime import JSError, JSObject, setTimeout
from baileys.utils.auth_state_core import initAuthCreds
from baileys.WAProto import proto


class CompletedValue:
    def __init__(self, value=None):
        self.value = value

    def __await__(self):
        if False:
            yield
        return self.value


def bufferJsonPrepare(value):
    if isinstance(value, (bytes, bytearray)):
        return JSObject({'type': 'Buffer', 'data': base64.b64encode(bytes(value)).decode()})
    if isinstance(value, list):
        return [bufferJsonPrepare(item) for item in value]
    if isinstance(value, dict):
        if dict.get(value, 'type') == 'Buffer':
            payload = dict.get(value, 'data')
            if payload is None:
                payload = value
            if isinstance(payload, (bytes, bytearray)):
                raw = bytes(payload)
            elif isinstance(payload, str):
                raw = payload.encode('utf-8')
            elif isinstance(payload, list):
                raw = bytes(int(item) & 0xFF for item in payload)
            else:
                raw = b''
            return JSObject({'type': 'Buffer', 'data': base64.b64encode(raw).decode()})
        return JSObject({key: bufferJsonPrepare(item) for key, item in dict.items(value) if item is not None})
    return value


def bufferJsonStringify(value):
    return json.dumps(bufferJsonPrepare(value), separators=(',', ':'), ensure_ascii=False, default=jsonDefault)


def bufferJsonRevive(value):
    if isinstance(value, list):
        return [bufferJsonRevive(item) for item in value]
    if isinstance(value, dict):
        revived = JSObject({key: bufferJsonRevive(item) for key, item in dict.items(value)})
        if dict.get(revived, 'type') == 'Buffer' and isinstance(dict.get(revived, 'data'), str):
            return base64.b64decode(revived.get('data'))
        keys = list(dict.keys(revived))
        if len(keys) > 0 and all(jsParseInt(key, 10) is not None for key in keys):
            values = list(dict.values(revived))
            if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in values):
                return bytes(int(item) & 0xFF for item in values)
        return revived
    return value


def bufferJsonParse(text):
    return bufferJsonRevive(json.loads(text, object_pairs_hook=JSObject))


def fixFileName(file):
    if file is None:
        return None
    return file.replace('/', '__').replace(':', '-')


def readTextFile(path):
    with open(path, 'r', encoding='utf-8') as handle:
        return handle.read()


def writeTextFile(path, text):
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)


async def useMultiFileAuthState(folder):
    fileLock = makeKeyedMutex()

    async def writeData(data, file):
        filePath = os.path.join(folder, fixFileName(file))

        async def task():
            writeTextFile(filePath, bufferJsonStringify(data))
        return await fileLock.get('mutex')(filePath, task)

    async def readData(file):
        try:
            filePath = os.path.join(folder, fixFileName(file))

            async def task():
                return bufferJsonParse(readTextFile(filePath))
            return await fileLock.get('mutex')(filePath, task)
        except Exception:
            return None

    async def removeData(file):
        try:
            filePath = os.path.join(folder, fixFileName(file))

            async def task():
                try:
                    os.unlink(filePath)
                except Exception:
                    pass
            await fileLock.get('mutex')(filePath, task)
        except Exception:
            pass

    try:
        folderInfo = os.stat(folder)
    except Exception:
        folderInfo = None
    if folderInfo is not None:
        if not os.path.isdir(folder):
            raise JSError(f'found something that is not a directory at {folder}, either delete it or specify a different location')
    else:
        os.makedirs(folder, exist_ok=True)

    loadedCreds = await readData('creds.json')
    creds = loadedCreds if loadedCreds is not None else initAuthCreds()

    async def keysGet(type, ids):
        data = JSObject({})

        async def fetchOne(id):
            value = await readData(f'{type}-{id}.json')
            if type == 'app-state-sync-key' and value is not None:
                value = proto.Message.AppStateSyncKeyData.fromObject(value)
            return value
        values = await asyncGather([fetchOne(id) for id in ids])
        for index, id in enumerate(ids):
            data[id] = values[index]
        return data

    async def keysSet(data):
        tasks = []
        for category in list(dict.keys(data)):
            for id in list(dict.keys(data[category])):
                value = data[category][id]
                file = f'{category}-{id}.json'
                tasks.append(writeData(value, file) if value is not None else removeData(file))
        await asyncGather(tasks)

    async def saveCreds():
        return await writeData(creds, 'creds.json')

    return JSObject({
        'state': JSObject({
            'creds': creds,
            'keys': JSObject({
                'get': keysGet,
                'set': keysSet
            })
        }),
        'saveCreds': saveCreds
    })


async def asyncGather(awaitables):
    return await asyncio.gather(*awaitables)


SIGNAL_STORE_TTL_MS = 5 * 60 * 1000
FLUSH_TIMEOUT_MS = 3000


async def useSingleFileAuthState(fileName):
    cache = Cache(JSObject({'max': 20000, 'ttl': SIGNAL_STORE_TTL_MS}))
    mutex = makeKeyedMutex()
    state = JSObject({'fileData': JSObject({}), 'isLoaded': False, 'flushTimeout': None})

    async def loadKey():
        async def task():
            if state.get('isLoaded'):
                return None
            try:
                data = bufferJsonParse(readTextFile(fileName))
                state['fileData'] = data if data is not None else JSObject({})
                for keyName in list(dict.keys(state.get('fileData'))):
                    cache.set(keyName, state.get('fileData')[keyName])
            except Exception:
                state['fileData'] = JSObject({})
            state['isLoaded'] = True
            return None
        return await mutex.get('mutex')('__file__', task)

    def flushKey():
        if state.get('flushTimeout') is not None:
            return

        def onTimer():
            state['flushTimeout'] = None

            async def task():
                try:
                    tempFile = fileName + '.temp'
                    writeTextFile(tempFile, bufferJsonStringify(state.get('fileData')))
                    os.replace(tempFile, fileName)
                except Exception:
                    pass
                return None

            asyncio.ensure_future(mutex.get('mutex')('__file__', task))
        state['flushTimeout'] = setTimeout(onTimer, FLUSH_TIMEOUT_MS)

    def writeKey(keyName, value):
        cache.set(keyName, value)
        state.get('fileData')[keyName] = value
        flushKey()

    def removeKey(keyName):
        cache.delete(keyName)
        state.get('fileData').pop(keyName, None)
        flushKey()

    try:
        fileInfo = os.stat(fileName)
    except Exception:
        fileInfo = None
    if fileInfo is None:
        writeTextFile(fileName, '{}')
    elif not os.path.isfile(fileName):
        raise JSError(f'found something that is not a file at {fileName}, either delete it or specify a different location')

    await loadKey()
    creds = state.get('fileData').get('creds')
    if creds is None:
        creds = initAuthCreds()

    def keysGet(type, ids):
        data = JSObject({})
        for id in ids:
            keyName = f'{type}{id}'
            value = cache.get(keyName)
            if value is None and state.get('fileData')[keyName] is not None:
                value = state.get('fileData')[keyName]
                cache.set(keyName, value)
            if type == 'app-state-sync-key' and value is not None:
                value = proto.Message.AppStateSyncKeyData.fromObject(value)
            data[id] = value
        return CompletedValue(data)

    def keysSet(data):
        for category in list(dict.keys(data)):
            for id in list(dict.keys(data[category])):
                keyName = f'{category}{id}'
                value = data[category][id]
                if value is not None:
                    writeKey(keyName, value)
                else:
                    removeKey(keyName)
        return CompletedValue(None)

    def saveCreds():
        writeKey('creds', creds)
        return CompletedValue(None)

    return JSObject({
        'state': JSObject({
            'creds': creds,
            'keys': JSObject({
                'get': keysGet,
                'set': keysSet
            })
        }),
        'saveCreds': saveCreds
    })


CREDS_ROW_KEY = '__creds__'
CREATE_SCHEMA_SQL = '\n'.join([
    '',
    'CREATE TABLE IF NOT EXISTS creds (',
    '  key TEXT PRIMARY KEY,',
    '  value TEXT NOT NULL',
    ');',
    'CREATE TABLE IF NOT EXISTS signal_keys (',
    '  type TEXT NOT NULL,',
    '  id TEXT NOT NULL,',
    '  value TEXT NOT NULL,',
    '  PRIMARY KEY (type, id)',
    ');',
    'CREATE INDEX IF NOT EXISTS signal_keys_type_idx ON signal_keys(type);',
    '',
])

SQL_STATEMENTS = JSObject({
    'credsSelect': 'SELECT value FROM creds WHERE key = ?',
    'credsUpsert': 'INSERT INTO creds (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
    'keySelect': 'SELECT value FROM signal_keys WHERE type = ? AND id = ?',
    'keyUpsert': 'INSERT INTO signal_keys (type, id, value) VALUES (?, ?, ?) ON CONFLICT(type, id) DO UPDATE SET value = excluded.value',
    'keyDelete': 'DELETE FROM signal_keys WHERE type = ? AND id = ?',
    'keyListIds': 'SELECT id FROM signal_keys WHERE type = ?',
    'keyList': 'SELECT id, value FROM signal_keys WHERE type = ?',
    'clearKeys': 'DELETE FROM signal_keys'
})


def adaptDb(db):
    pragmaFn = getattr(db, 'pragma', None)
    if callable(pragmaFn):
        def pragma(stmt):
            return pragmaFn(stmt)
    else:
        def pragma(stmt):
            return db.execute(f'PRAGMA {stmt}')

    transactionFn = getattr(db, 'transaction', None)
    if callable(transactionFn):
        def transaction(fn):
            return transactionFn(fn)
    else:
        def transaction(fn):
            def run(*args):
                db.execute('BEGIN')
                try:
                    result = fn(*args)
                    db.execute('COMMIT')
                    return result
                except BaseException:
                    db.execute('ROLLBACK')
                    raise
            return run
    return JSObject({'db': db, 'pragma': pragma, 'transaction': transaction})


async def useSqliteAuthState(opts):
    rawDb = opts.get('database')
    if rawDb is None:
        rawDb = sqlite3.connect(opts.get('dbPath'), isolation_level=None)
    adapted = adaptDb(rawDb)
    db = adapted.get('db')
    pragma = adapted.get('pragma')
    transaction = adapted.get('transaction')

    pragma('journal_mode = WAL')
    pragma('synchronous = NORMAL')
    db.executescript(CREATE_SCHEMA_SQL)

    def loadCreds():
        row = db.execute(SQL_STATEMENTS['credsSelect'], (CREDS_ROW_KEY,)).fetchone()
        if row is None:
            return initAuthCreds()
        return bufferJsonParse(row[0])

    def persistCreds(creds):
        db.execute(SQL_STATEMENTS['credsUpsert'], (CREDS_ROW_KEY, bufferJsonStringify(creds)))

    creds = loadCreds()

    async def keysGet(type, ids):
        data = JSObject({})
        for id in ids:
            row = db.execute(SQL_STATEMENTS['keySelect'], (type, id)).fetchone()
            if row is not None:
                value = bufferJsonParse(row[0])
                if type == 'app-state-sync-key' and value is not None:
                    value = proto.Message.AppStateSyncKeyData.fromObject(value)
                data[id] = value
        return data

    async def keysSet(data):
        def writeTx():
            for category in list(dict.keys(data)):
                for id in list(dict.keys(data[category])):
                    value = data[category][id]
                    if value is not None:
                        db.execute(SQL_STATEMENTS['keyUpsert'], (category, id, bufferJsonStringify(value)))
                    else:
                        db.execute(SQL_STATEMENTS['keyDelete'], (category, id))
        transaction(writeTx)()

    async def saveCreds():
        return persistCreds(creds)

    return JSObject({
        'state': JSObject({
            'creds': creds,
            'keys': JSObject({
                'get': keysGet,
                'set': keysSet
            })
        }),
        'saveCreds': saveCreds
    })
