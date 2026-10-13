import asyncio
import base64
import copy
import datetime
import errno as errnoModule
import hashlib
import hmac
import inspect
import ipaddress
import json
import math
import os
import platform
import random
import re
import secrets
import socket
import sqlite3
import ssl
import struct
import sys
import threading
import time
import unicodedata
import urllib.parse
from datetime import timedelta

from lib.compat import http
from lib.utils import helper
from lib.utils.color import chalk
from lib.utils.handler import G
from lib.utils.helper import maybe_await, spawn


CLIENT_LONG_PASSWORD = 0x00000001


CLIENT_FOUND_ROWS = 0x00000002


CLIENT_LONG_FLAG = 0x00000004


CLIENT_CONNECT_WITH_DB = 0x00000008


CLIENT_PROTOCOL_41 = 0x00000200


CLIENT_SSL = 0x00000800


CLIENT_TRANSACTIONS = 0x00002000


CLIENT_SECURE_CONNECTION = 0x00008000


CLIENT_MULTI_RESULTS = 0x00020000


CLIENT_PLUGIN_AUTH = 0x00080000


CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA = 0x00200000


COM_QUIT = 0x01


COM_QUERY = 0x03


CHARSET_UTF8MB4 = 45


MAX_PACKET = 0x01000000


T_DECIMAL = 0


T_TINY = 1


T_SHORT = 2


T_LONG = 3


T_FLOAT = 4


T_DOUBLE = 5


T_NULL = 6


T_TIMESTAMP = 7


T_LONGLONG = 8


T_INT24 = 9


T_DATE = 10


T_TIME = 11


T_DATETIME = 12


T_YEAR = 13


T_NEWDATE = 14


T_VARCHAR = 15


T_BIT = 16


T_JSON = 245


T_NEWDECIMAL = 246


T_ENUM = 247


T_SET = 248


T_TINY_BLOB = 249


T_MEDIUM_BLOB = 250


T_LONG_BLOB = 251


T_BLOB = 252


T_VAR_STRING = 253


T_STRING = 254


T_GEOMETRY = 255


ERROR_CODES = {
    1005: 'ER_CANT_CREATE_TABLE', 1007: 'ER_DB_CREATE_EXISTS', 1008: 'ER_DB_DROP_EXISTS',
    1030: 'ER_GET_ERRNO', 1040: 'ER_CON_COUNT_ERROR', 1044: 'ER_DBACCESS_DENIED_ERROR',
    1045: 'ER_ACCESS_DENIED_ERROR', 1048: 'ER_BAD_NULL_ERROR', 1049: 'ER_BAD_DB_ERROR',
    1050: 'ER_TABLE_EXISTS_ERROR', 1051: 'ER_BAD_TABLE_ERROR', 1054: 'ER_BAD_FIELD_ERROR',
    1062: 'ER_DUP_ENTRY', 1064: 'ER_PARSE_ERROR', 1071: 'ER_TOO_LONG_KEY',
    1130: 'ER_HOST_NOT_PRIVILEGED', 1136: 'ER_WRONG_VALUE_COUNT_ON_ROW', 1142: 'ER_TABLEACCESS_DENIED_ERROR',
    1146: 'ER_NO_SUCH_TABLE', 1205: 'ER_LOCK_WAIT_TIMEOUT', 1213: 'ER_LOCK_DEADLOCK',
    1227: 'ER_SPECIFIC_ACCESS_DENIED_ERROR', 1264: 'ER_WARN_DATA_OUT_OF_RANGE', 1290: 'ER_OPTION_PREVENTS_STATEMENT',
    1292: 'ER_TRUNCATED_WRONG_VALUE', 1406: 'ER_DATA_TOO_LONG', 1451: 'ER_ROW_IS_REFERENCED_2',
    1452: 'ER_NO_REFERENCED_ROW_2', 2006: 'CR_SERVER_GONE_ERROR', 2013: 'CR_SERVER_LOST',
}


class MySqlError(Exception):
    def __init__(self, message, code=None, errno=None, sqlState=None, sqlMessage=None, fatal=False):
        super().__init__(message)
        self.name = 'MySqlError'
        self.code = code
        self.errno = errno
        self.sqlState = sqlState
        self.sqlMessage = sqlMessage
        self.fatal = fatal


class OkPacket:
    def __init__(self, affectedRows=0, insertId=0, serverStatus=0, warningCount=0, message='', changedRows=0):
        self.fieldCount = 0
        self.affectedRows = affectedRows
        self.insertId = insertId
        self.serverStatus = serverStatus
        self.warningCount = warningCount
        self.message = message
        self.changedRows = changedRows


class FieldPacket:
    def __init__(self, catalog, db, table, orgTable, name, orgName, charsetNr, length, type, flags, decimals):
        self.catalog = catalog
        self.db = db
        self.table = table
        self.orgTable = orgTable
        self.name = name
        self.orgName = orgName
        self.charsetNr = charsetNr
        self.length = length
        self.type = type
        self.flags = flags
        self.decimals = decimals


def jsNumberToString(v):
    if isinstance(v, float):
        if math.isnan(v):
            return 'NaN'
        if math.isinf(v):
            return 'Infinity' if v > 0 else '-Infinity'
        if v.is_integer() and abs(v) < 1e21:
            return str(int(v))
        s = repr(v)
        if 'e' in s:
            mant, exp = s.split('e')
            sign = '-' if exp.startswith('-') else '+'
            digits = exp.lstrip('+-').lstrip('0') or '0'
            s = f'{mant}e{"-" if sign == "-" else "+"}{digits}'
        return s
    return str(v)


_ESCAPE_MAP = {'\x00': '\\0', '\b': '\\b', '\t': '\\t', '\n': '\\n', '\r': '\\r',
               '\x1a': '\\Z', '"': '\\"', "'": "\\'", '\\': '\\\\'}


def escapeString(val):
    return "'" + re.sub(r'[\x00\b\t\n\r\x1a"\'\\]', lambda m: _ESCAPE_MAP[m.group()], val) + "'"


def dateToString(dt, timeZone=None):
    if isinstance(dt, datetime.datetime):
        local = dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt
        ms = local.microsecond // 1000
        return (f"'{local.year:04d}-{local.month:02d}-{local.day:02d} "
                f"{local.hour:02d}:{local.minute:02d}:{local.second:02d}.{ms:03d}'")
    return f"'{dt.year:04d}-{dt.month:02d}-{dt.day:02d} 00:00:00.000'"


def arrayToList(arr, timeZone=None):
    parts = []
    for v in arr:
        if isinstance(v, (list, tuple)):
            parts.append('(' + arrayToList(v, timeZone) + ')')
        else:
            parts.append(escape(v, True, timeZone))
    return ', '.join(parts)


def objectToValues(obj, timeZone=None):
    parts = []
    for key, v in obj.items():
        if callable(v):
            continue
        parts.append(escapeId(key) + ' = ' + escape(v, True, timeZone))
    return ', '.join(parts)


def escape(val, stringifyObjects=False, timeZone=None):
    if val is None:
        return 'NULL'
    if isinstance(val, bool):
        return 'true' if val else 'false'
    if isinstance(val, (int, float)):
        return jsNumberToString(val)
    if isinstance(val, str):
        return escapeString(val)
    if isinstance(val, (datetime.datetime, datetime.date)):
        return dateToString(val, timeZone or 'local')
    if isinstance(val, (list, tuple)):
        return arrayToList(val, timeZone)
    if isinstance(val, (bytes, bytearray)):
        return "X'" + bytes(val).hex() + "'"
    if isinstance(val, dict):
        if stringifyObjects:
            return escapeString('[object Object]')
        return objectToValues(val, timeZone)
    return escapeString(str(val))


def escapeId(val, forbidQualified=False):
    if isinstance(val, (list, tuple)):
        return ', '.join(escapeId(v, forbidQualified) for v in val)
    s = str(val)
    if forbidQualified:
        return '`' + s.replace('`', '``') + '`'
    return '`' + s.replace('`', '``').replace('.', '`.`') + '`'


def format(sql, values=None, stringifyObjects=False, timeZone=None):
    if values is None:
        return sql
    if not isinstance(values, (list, tuple)):
        values = [values]
    pos = 0
    vi = 0
    out = []
    for m in re.finditer(r'\?+', sql):
        if vi >= len(values):
            break
        ln = len(m.group())
        if ln > 2:
            continue
        v = values[vi]
        value = escapeId(v) if ln == 2 else escape(v, stringifyObjects, timeZone)
        out.append(sql[pos:m.start()] + value)
        pos = m.end()
        vi += 1
    if pos == 0:
        return sql
    return ''.join(out) + sql[pos:]


def encodeLenencInt(n):
    if n < 251:
        return bytes([n])
    if n < (1 << 16):
        return b'\xfc' + struct.pack('<H', n)
    if n < (1 << 24):
        return b'\xfd' + n.to_bytes(3, 'little')
    return b'\xfe' + struct.pack('<Q', n)


def readLenencInt(buf, pos):
    first = buf[pos]
    if first < 0xfb:
        return first, pos + 1
    if first == 0xfb:
        return None, pos + 1
    if first == 0xfc:
        return struct.unpack_from('<H', buf, pos + 1)[0], pos + 3
    if first == 0xfd:
        return int.from_bytes(buf[pos + 1:pos + 4], 'little'), pos + 4
    return struct.unpack_from('<Q', buf, pos + 1)[0], pos + 9


def readLenencBytes(buf, pos):
    n, pos = readLenencInt(buf, pos)
    if n is None:
        return None, pos
    return bytes(buf[pos:pos + n]), pos + n


def cstr(s):
    return s.encode('utf-8') + b'\x00'


def xorCyclic(data, key):
    return bytes(d ^ key[i % len(key)] for i, d in enumerate(data))


def nativePasswordToken(password, scramble):
    if not password:
        return b''
    pw = password.encode('utf-8')
    s1 = hashlib.sha1(pw).digest()
    s2 = hashlib.sha1(s1).digest()
    s3 = hashlib.sha1(scramble + s2).digest()
    return bytes(a ^ b for a, b in zip(s1, s3))


def cachingSha2Token(password, scramble):
    if not password:
        return b''
    pw = password.encode('utf-8')
    s1 = hashlib.sha256(pw).digest()
    s2 = hashlib.sha256(s1).digest()
    s3 = hashlib.sha256(s2 + scramble).digest()
    return bytes(a ^ b for a, b in zip(s1, s3))


def authToken(plugin, password, scramble):
    if plugin == 'mysql_native_password':
        return nativePasswordToken(password, scramble)
    if plugin == 'caching_sha2_password':
        return cachingSha2Token(password, scramble)
    raise MySqlError(f'Unknown auth plugin: {plugin}', code='UNKNOWN_AUTH_PLUGIN', fatal=True)


def derRead(buf, pos):
    tag = buf[pos]
    ln = buf[pos + 1]
    pos += 2
    if ln & 0x80:
        n = ln & 0x7f
        ln = int.from_bytes(buf[pos:pos + n], 'big')
        pos += n
    return tag, buf[pos:pos + ln], pos + ln


def rsaPublicFromPem(pem):
    text = pem.decode('ascii', 'replace')
    m = re.search(r'-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----', text, re.S)
    if not m:
        raise MySqlError('Invalid RSA public key from server', code='INVALID_PUBLIC_KEY', fatal=True)
    der = base64.b64decode(''.join(m.group(2).split()))
    _, body, _ = derRead(der, 0)
    if m.group(1) == 'PUBLIC KEY':
        _, _, nxt = derRead(body, 0)
        _, bits, _ = derRead(body, nxt)
        _, inner, _ = derRead(bits[1:], 0)
    else:
        inner = body
    _, nBytes, nxt = derRead(inner, 0)
    _, eBytes, _ = derRead(inner, nxt)
    return int.from_bytes(nBytes, 'big'), int.from_bytes(eBytes, 'big')


def mgf1(seed, length):
    out = b''
    counter = 0
    while len(out) < length:
        out += hashlib.sha1(seed + counter.to_bytes(4, 'big')).digest()
        counter += 1
    return out[:length]


def rsaOaepEncrypt(pem, message):
    n, e = rsaPublicFromPem(pem)
    k = (n.bit_length() + 7) // 8
    hLen = 20
    if len(message) > k - 2 * hLen - 2:
        raise MySqlError('Password is too long for RSA encryption', code='PASSWORD_TOO_LONG', fatal=True)
    lHash = hashlib.sha1(b'').digest()
    ps = b'\x00' * (k - len(message) - 2 * hLen - 2)
    db = lHash + ps + b'\x01' + message
    seed = secrets.token_bytes(hLen)
    dbMask = mgf1(seed, k - hLen - 1)
    maskedDB = bytes(a ^ b for a, b in zip(db, dbMask))
    seedMask = mgf1(maskedDB, hLen)
    maskedSeed = bytes(a ^ b for a, b in zip(seed, seedMask))
    em = b'\x00' + maskedSeed + maskedDB
    c = pow(int.from_bytes(em, 'big'), e, n)
    return c.to_bytes(k, 'big')


def errorFromPacket(pkt):
    errno = struct.unpack_from('<H', pkt, 1)[0]
    pos = 3
    sqlState = None
    if pkt[pos:pos + 1] == b'#':
        sqlState = pkt[pos + 1:pos + 6].decode('ascii', 'replace')
        pos += 6
    msg = pkt[pos:].decode('utf-8', 'replace')
    code = ERROR_CODES.get(errno, 'ER_UNKNOWN_ERROR')
    return MySqlError(f'{code}: {msg}', code=code, errno=errno, sqlState=sqlState, sqlMessage=msg)


def parseOk(pkt):
    pos = 1
    affected, pos = readLenencInt(pkt, pos)
    insertId, pos = readLenencInt(pkt, pos)
    status = 0
    warnings = 0
    if len(pkt) >= pos + 4:
        status, warnings = struct.unpack_from('<HH', pkt, pos)
        pos += 4
    message = pkt[pos:].decode('utf-8', 'replace')
    changed = 0
    m = re.search(r'Changed: ([0-9]+)', message)
    if m:
        changed = int(m.group(1))
    return OkPacket(affected or 0, insertId or 0, status, warnings, message, changed)


def parseField(pkt):
    pos = 0
    strs = []
    for _ in range(6):
        s, pos = readLenencBytes(pkt, pos)
        strs.append(s.decode('utf-8', 'replace'))
    pos += 1
    charsetNr, length, typ, flags, decimals = struct.unpack_from('<HIBHB', pkt, pos)
    return FieldPacket(strs[0], strs[1], strs[2], strs[3], strs[4], strs[5], charsetNr, length, typ, flags, decimals)


def parseDateText(text):
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return text


def parseDateTimeText(text):
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return text


def castValue(raw, field):
    t = field.type
    if t in (T_TINY, T_SHORT, T_LONG, T_INT24, T_LONGLONG, T_YEAR):
        return int(raw.decode('ascii'))
    if t in (T_FLOAT, T_DOUBLE):
        return float(raw.decode('ascii'))
    if t in (T_DECIMAL, T_NEWDECIMAL, T_TIME):
        return raw.decode('utf-8', 'replace')
    if t in (T_DATE, T_NEWDATE):
        return parseDateText(raw.decode('utf-8', 'replace'))
    if t in (T_DATETIME, T_TIMESTAMP):
        return parseDateTimeText(raw.decode('utf-8', 'replace'))
    if t == T_JSON:
        text = raw.decode('utf-8', 'replace')
        try:
            return json.loads(text)
        except ValueError:
            return text
    if t in (T_BIT, T_GEOMETRY):
        return bytes(raw)
    if t in (T_TINY_BLOB, T_MEDIUM_BLOB, T_LONG_BLOB, T_BLOB, T_VAR_STRING, T_STRING, T_VARCHAR, T_ENUM, T_SET):
        if field.charsetNr == 63:
            return bytes(raw)
        return raw.decode('utf-8', 'replace')
    return raw.decode('utf-8', 'replace')


def parseRow(pkt, fields):
    pos = 0
    row = {}
    for f in fields:
        if pkt[pos] == 0xfb:
            pos += 1
            row[f.name] = None
        else:
            raw, pos = readLenencBytes(pkt, pos)
            row[f.name] = castValue(raw, f)
    return row


class MySqlConnection:
    def __init__(self, config):
        self.config = dict(config)
        self.host = config.get('host') or 'localhost'
        self.port = int(config.get('port') or 3306)
        self.user = config.get('user')
        self.password = config.get('password') or ''
        self.database = config.get('database')
        ct = config.get('connectTimeout')
        self.connectTimeout = ct if ct is not None else 10000
        self.sock = None
        self.closed = False
        self.threadId = None
        self.serverVersion = None
        self.serverCaps = 0
        self._seq = 0
        self._pool = None
        self._lock = asyncio.Lock()

    def close(self):
        self.closed = True
        sk = self.sock
        if sk is not None:
            try:
                sk.close()
            except OSError:
                pass

    def _lost(self):
        self.close()
        return MySqlError('Connection lost: The server closed the connection.', code='PROTOCOL_CONNECTION_LOST', fatal=True)

    def _recvExact(self, n):
        if self.sock is None:
            raise self._lost()
        chunks = []
        remaining = n
        while remaining > 0:
            try:
                chunk = self.sock.recv(min(remaining, 262144))
            except OSError:
                raise self._lost()
            if not chunk:
                raise self._lost()
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    def _readPacket(self):
        payload = bytearray()
        while True:
            header = self._recvExact(4)
            length = int.from_bytes(header[:3], 'little')
            self._seq = (header[3] + 1) & 0xff
            payload += self._recvExact(length)
            if length < 0xffffff:
                break
        return bytes(payload)

    def _writePacket(self, payload):
        data = bytearray()
        pos = 0
        while True:
            chunk = payload[pos:pos + 0xffffff]
            data += len(chunk).to_bytes(3, 'little') + bytes([self._seq]) + chunk
            self._seq = (self._seq + 1) & 0xff
            pos += len(chunk)
            if len(chunk) < 0xffffff:
                break
        if self.sock is None:
            raise self._lost()
        try:
            self.sock.sendall(bytes(data))
        except OSError:
            raise self._lost()

    def _tlsContext(self):
        opts = self.config.get('ssl')
        ctx = ssl.create_default_context()
        if isinstance(opts, dict):
            if opts.get('ca'):
                ctx.load_verify_locations(cadata=opts['ca'])
            if opts.get('rejectUnauthorized') is False:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _connectSync(self):
        timeout = self.connectTimeout / 1000 if self.connectTimeout else None
        try:
            raw = socket.create_connection((self.host, self.port), timeout=timeout)
        except socket.timeout:
            raise MySqlError('connect ETIMEDOUT', code='ETIMEDOUT', fatal=True)
        except OSError as err:
            code = 'ENOTFOUND' if isinstance(err, socket.gaierror) else errnoModule.errorcode.get(err.errno or 0, 'ECONNREFUSED')
            raise MySqlError(f'connect {code} {self.host}:{self.port}', code=code, fatal=True)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.sock = raw
        self.closed = False
        try:
            self._handshakeSync()
        except BaseException:
            self.close()
            raise
        self.sock.settimeout(None)

    def _handshakeSync(self):
        greeting = self._readPacket()
        if greeting[:1] == b'\xff':
            raise errorFromPacket(greeting)
        if greeting[:1] != b'\x0a':
            raise MySqlError('Protocol version 10 expected', code='PROTOCOL_VERSION_NOT_SUPPORTED', fatal=True)
        pos = 1
        nul = greeting.index(b'\x00', pos)
        self.serverVersion = greeting[pos:nul].decode('utf-8', 'replace')
        pos = nul + 1
        self.threadId = struct.unpack_from('<I', greeting, pos)[0]
        pos += 4
        scramble1 = greeting[pos:pos + 8]
        pos += 9
        capLo = struct.unpack_from('<H', greeting, pos)[0]
        pos += 2 + 1 + 2
        capHi = struct.unpack_from('<H', greeting, pos)[0]
        pos += 2
        serverCaps = capLo | (capHi << 16)
        authLen = greeting[pos] if serverCaps & CLIENT_PLUGIN_AUTH else 0
        pos += 1 + 10
        scramble = scramble1
        if serverCaps & CLIENT_SECURE_CONNECTION:
            part2Len = max(13, authLen - 8)
            part2 = greeting[pos:pos + part2Len]
            pos += part2Len
            if part2.endswith(b'\x00'):
                part2 = part2[:-1]
            scramble = (scramble1 + part2)[:20]
        plugin = 'mysql_native_password'
        if serverCaps & CLIENT_PLUGIN_AUTH:
            end = greeting.find(b'\x00', pos)
            if end == -1:
                end = len(greeting)
            plugin = greeting[pos:end].decode('ascii', 'replace') or 'mysql_native_password'
        self.serverCaps = serverCaps
        if not (serverCaps & CLIENT_PROTOCOL_41):
            raise MySqlError('Server does not support protocol 4.1', code='PROTOCOL_NOT_SUPPORTED', fatal=True)
        useSsl = bool(self.config.get('ssl'))
        if useSsl and not (serverCaps & CLIENT_SSL):
            raise MySqlError('Server does not support SSL connections', code='HANDSHAKE_NO_SSL_SUPPORT', fatal=True)
        caps = (CLIENT_LONG_PASSWORD | CLIENT_FOUND_ROWS | CLIENT_LONG_FLAG | CLIENT_PROTOCOL_41
                | CLIENT_TRANSACTIONS | CLIENT_SECURE_CONNECTION | CLIENT_MULTI_RESULTS
                | CLIENT_PLUGIN_AUTH | CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA)
        if self.database:
            caps |= CLIENT_CONNECT_WITH_DB
        if useSsl:
            caps |= CLIENT_SSL
            self._writePacket(struct.pack('<IIB', caps, MAX_PACKET, CHARSET_UTF8MB4) + b'\x00' * 23)
            try:
                self.sock = self._tlsContext().wrap_socket(self.sock, server_hostname=self.host)
            except ssl.SSLError as err:
                raise MySqlError(f'SSL handshake failed: {err}', code='HANDSHAKE_SSL_ERROR', fatal=True)
        password = self.password or ''
        token = authToken(plugin, password, scramble)
        resp = struct.pack('<IIB', caps, MAX_PACKET, CHARSET_UTF8MB4) + b'\x00' * 23
        resp += cstr(self.user or '')
        resp += encodeLenencInt(len(token)) + token
        if self.database:
            resp += cstr(self.database)
        resp += cstr(plugin)
        self._writePacket(resp)
        self._authLoop(plugin, password, scramble)

    def _authLoop(self, plugin, password, scramble):
        pkt = self._readPacket()
        while True:
            head = pkt[:1]
            if head == b'\x00':
                return
            if head == b'\xff':
                raise errorFromPacket(pkt)
            if head == b'\xfe':
                end = pkt.find(b'\x00', 1)
                plugin = pkt[1:end].decode('ascii', 'replace')
                data = pkt[end + 1:]
                if data.endswith(b'\x00'):
                    data = data[:-1]
                self._writePacket(authToken(plugin, password, data))
                pkt = self._readPacket()
                continue
            if head == b'\x01' and plugin == 'caching_sha2_password':
                more = pkt[1:2]
                if more == b'\x03':
                    pkt = self._readPacket()
                    continue
                if more == b'\x04':
                    if isinstance(self.sock, ssl.SSLSocket):
                        self._writePacket(password.encode('utf-8') + b'\x00')
                    else:
                        self._writePacket(b'\x02')
                        keyPkt = self._readPacket()
                        if keyPkt[:1] != b'\x01':
                            raise errorFromPacket(keyPkt) if keyPkt[:1] == b'\xff' else MySqlError(
                                'Unexpected packet while requesting public key', code='PROTOCOL_ERROR', fatal=True)
                        enc = rsaOaepEncrypt(keyPkt[1:], xorCyclic(password.encode('utf-8') + b'\x00', scramble))
                        self._writePacket(enc)
                    pkt = self._readPacket()
                    continue
                raise MySqlError(f'Unexpected caching_sha2_password response: {more!r}', code='PROTOCOL_ERROR', fatal=True)
            raise MySqlError(f'Unexpected packet 0x{head.hex()} during authentication', code='PROTOCOL_ERROR', fatal=True)

    def _queryRaw(self, sql):
        if self.closed:
            raise MySqlError("Can't add new command when connection is in closed state", code='PROTOCOL_CONNECTION_LOST', fatal=True)
        self._seq = 0
        self._writePacket(bytes([COM_QUERY]) + sql.encode('utf-8'))
        pkt = self._readPacket()
        head = pkt[:1]
        if head == b'\xff':
            raise errorFromPacket(pkt)
        if head == b'\x00':
            return parseOk(pkt), []
        if head == b'\xfb':
            raise MySqlError('LOCAL INFILE requests are refused', code='LOCAL_INFILE_NOT_SUPPORTED')
        count, _ = readLenencInt(pkt, 0)
        fields = [parseField(self._readPacket()) for _ in range(count)]
        eof = self._readPacket()
        if eof[:1] == b'\xff':
            raise errorFromPacket(eof)
        rows = []
        while True:
            p = self._readPacket()
            if p[:1] == b'\xfe' and len(p) < 9:
                break
            if p[:1] == b'\xff':
                raise errorFromPacket(p)
            rows.append(parseRow(p, fields))
        return rows, fields

    def _quitSync(self):
        try:
            self._seq = 0
            self._writePacket(bytes([COM_QUIT]))
        except MySqlError:
            pass
        self.close()

    async def connect(self):
        await asyncio.to_thread(self._connectSync)
        return self

    async def query(self, sql, values=None):
        text = format(sql, values) if values is not None else sql
        async with self._lock:
            return await asyncio.to_thread(self._queryRaw, text)

    async def execute(self, sql, values=None):
        return await self.query(sql, values)

    async def end(self):
        async with self._lock:
            if self.closed:
                return
            await asyncio.to_thread(self._quitSync)

    async def release(self):
        if self._pool is not None:
            await self._pool.release(self)
        else:
            await self.end()

    def destroy(self):
        self.close()


async def createConnection(config):
    conn = MySqlConnection(config)
    await conn.connect()
    return conn


class Pool:
    def __init__(self, config):
        self.config = dict(config)
        limit = config.get('connectionLimit')
        self.connectionLimit = int(limit) if limit else 10
        self.waitForConnections = config.get('waitForConnections', True) is not False
        self._idle = []
        self._semaphore = asyncio.Semaphore(self.connectionLimit)
        self._ended = False

    async def getConnection(self):
        if self._ended:
            raise MySqlError('Pool is closed.', code='POOL_CLOSED')
        if not self.waitForConnections and self._semaphore.locked():
            raise MySqlError('No connections available.', code='POOL_CONNLIMIT')
        await self._semaphore.acquire()
        try:
            conn = None
            while self._idle and conn is None:
                c = self._idle.pop()
                if not c.closed:
                    conn = c
            if conn is None:
                conn = MySqlConnection(self.config)
                conn._pool = self
                await conn.connect()
            return conn
        except BaseException:
            self._semaphore.release()
            raise

    async def release(self, conn):
        if conn.closed or self._ended:
            conn.close()
        else:
            self._idle.append(conn)
        self._semaphore.release()

    async def query(self, sql, values=None):
        conn = await self.getConnection()
        try:
            return await conn.query(sql, values)
        finally:
            await self.release(conn)

    async def execute(self, sql, values=None):
        conn = await self.getConnection()
        try:
            return await conn.execute(sql, values)
        finally:
            await self.release(conn)

    async def end(self):
        self._ended = True
        idle = self._idle
        self._idle = []
        for c in idle:
            try:
                await c.end()
            except MySqlError:
                pass


def createPool(config):
    return Pool(config)


CREATE_BOT_DATA_SQL = '\n      CREATE TABLE IF NOT EXISTS bot_data (\n        id INT PRIMARY KEY DEFAULT 1,\n        data LONGTEXT\n      )\n    '


def jsSanitize(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        if obj.is_integer() and abs(obj) < 1e21:
            return int(obj)
    if isinstance(obj, dict):
        return {k: jsSanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsSanitize(v) for v in obj]
    return obj


def jsonDefault(value):
    if isinstance(value, (bytes, bytearray)):
        return {'type': 'Buffer', 'data': list(value)}
    if hasattr(value, 'toNumber'):
        return value.toNumber()
    return None


def jsonStringify(obj):
    return json.dumps(jsSanitize(obj), separators=(',', ':'), ensure_ascii=False, default=jsonDefault)


class MySqlAdapter:
    def __init__(self, jdbcUrl):
        match = re.search(r'jdbc:mysql://([^:]+):([^@]+)@([^:]+):([0-9]+)/(.+)', jdbcUrl)
        if not match:
            raise Exception('Invalid JDBC format! Should be: jdbc:mysql://user:pass@host:port/database')
        user, password, host, port, database = match.groups()
        self.config = {
            'host': host, 'user': user, 'password': password, 'database': database,
            'port': int(port), 'waitForConnections': True, 'connectionLimit': 10,
        }
        self.pool = None
        self._inited = False

    async def getPool(self):
        if self.pool:
            return self.pool
        self.pool = createPool(self.config)
        return self.pool

    async def init(self):
        if self._inited:
            return
        pool = await self.getPool()
        await pool.query(CREATE_BOT_DATA_SQL)
        self._inited = True

    async def read(self):
        await self.init()
        pool = await self.getPool()
        rows, _ = await pool.query('SELECT data FROM bot_data WHERE id = 1')
        return json.loads(rows[0]['data'] or '{}') if rows else {}

    async def write(self, obj):
        await self.init()
        pool = await self.getPool()
        data = jsonStringify(obj)
        await pool.query(
            'INSERT INTO bot_data (id, data) VALUES (1, ?) ON DUPLICATE KEY UPDATE data = ?',
            [data, data],
        )

    async def close(self):
        if self.pool:
            await self.pool.end()


EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


INT32_MIN = -0x80000000


INT32_MAX = 0x7fffffff


INT64_MIN = -(1 << 63)


INT64_MAX = (1 << 63) - 1


def _int32(v):
    return ((int(v) + (1 << 31)) % (1 << 32)) - (1 << 31)


def _int64(v):
    return ((int(v) + (1 << 63)) % (1 << 64)) - (1 << 63)


class ObjectId:
    _counter = int.from_bytes(secrets.token_bytes(3), 'big')
    _process = secrets.token_bytes(5)
    _lock = threading.Lock()

    def __init__(self, id=None):
        if id is None:
            with ObjectId._lock:
                ObjectId._counter = (ObjectId._counter + 1) & 0xffffff
                counter = ObjectId._counter
            self.buffer = (
                struct.pack('>I', int(time.time()) & 0xffffffff)
                + ObjectId._process
                + counter.to_bytes(3, 'big')
            )
        elif isinstance(id, ObjectId):
            self.buffer = bytes(id.buffer)
        elif isinstance(id, str) and re.fullmatch(r'[0-9a-fA-F]{24}', id):
            self.buffer = bytes.fromhex(id)
        elif isinstance(id, (bytes, bytearray)) and len(id) == 12:
            self.buffer = bytes(id)
        else:
            raise TypeError('ObjectId must be a 24-char hex string or 12-byte Buffer')

    def toHexString(self):
        return self.buffer.hex()

    def __str__(self):
        return self.toHexString()

    def __repr__(self):
        return f"ObjectId('{self.toHexString()}')"

    def toJSON(self):
        return self.toHexString()

    def equals(self, other):
        if isinstance(other, ObjectId):
            return self.buffer == other.buffer
        if isinstance(other, str):
            return self.toHexString() == other.lower()
        return False

    def __eq__(self, other):
        return self.equals(other)

    def __hash__(self):
        return hash(self.buffer)

    def getTimestamp(self):
        return EPOCH + timedelta(seconds=struct.unpack('>I', self.buffer[:4])[0])


class BsonInt32:
    def __init__(self, v):
        self.value = _int32(v)


class BsonDouble:
    def __init__(self, v):
        self.value = float(v)


class BsonLong:
    def __init__(self, v):
        self.value = int(v)

    def toNumber(self):
        return float(self.value)

    def __str__(self):
        return str(self.value)


class BsonTimestamp:
    def __init__(self, t, i):
        self.t = int(t) & 0xffffffff
        self.i = int(i) & 0xffffffff


class BsonBinary:
    def __init__(self, buffer, subType=0):
        self.buffer = bytes(buffer)
        self.subType = subType


class BsonDecimal128:
    def __init__(self, bytes_):
        self.bytes = bytes(bytes_)


class BsonMinKey:
    pass


class BsonMaxKey:
    pass


def bsonCString(s):
    if '\x00' in s:
        raise Exception('BSON key/bsonCString may not contain NUL')
    return s.encode('utf-8') + b'\x00'


def bsonEncodeString(s):
    body = s.encode('utf-8')
    return struct.pack('<i', len(body) + 1) + body + b'\x00'


def bsonWrapDocument(parts):
    body = b''.join(parts)
    return struct.pack('<i', len(body) + 5) + body + b'\x00'


def bsonEncodeValue(value, ignoreUndefined=True):
    if value is None:
        return (0x0a, b'')
    if isinstance(value, bool):
        return (0x08, b'\x01' if value else b'\x00')
    if isinstance(value, int):
        if INT32_MIN <= value <= INT32_MAX:
            return (0x10, struct.pack('<i', value))
        if INT64_MIN <= value <= INT64_MAX:
            return (0x12, struct.pack('<q', value))
        return (0x01, struct.pack('<d', float(value)))
    if isinstance(value, float):
        if (value.is_integer() and INT32_MIN <= value <= INT32_MAX
                and not (value == 0 and math.copysign(1.0, value) < 0)):
            return (0x10, struct.pack('<i', int(value)))
        return (0x01, struct.pack('<d', value))
    if isinstance(value, str):
        return (0x02, bsonEncodeString(value))
    if isinstance(value, datetime.datetime):
        aware = value if value.tzinfo is not None else value.astimezone()
        ms = (aware - EPOCH) // timedelta(milliseconds=1)
        return (0x09, struct.pack('<q', ms))
    if isinstance(value, ObjectId):
        return (0x07, value.buffer)
    if isinstance(value, BsonInt32):
        return (0x10, struct.pack('<i', value.value))
    if isinstance(value, BsonDouble):
        return (0x01, struct.pack('<d', value.value))
    if isinstance(value, BsonLong):
        return (0x12, struct.pack('<q', _int64(value.value)))
    if isinstance(value, BsonTimestamp):
        return (0x11, struct.pack('<II', value.i, value.t))
    if isinstance(value, BsonDecimal128):
        return (0x13, value.bytes)
    if isinstance(value, BsonMinKey):
        return (0xff, b'')
    if isinstance(value, BsonMaxKey):
        return (0x7f, b'')
    if isinstance(value, re.Pattern):
        flags = ''
        if value.flags & re.IGNORECASE:
            flags += 'i'
        if value.flags & re.MULTILINE:
            flags += 'm'
        if value.flags & re.DOTALL:
            flags += 's'
        return (0x0b, bsonCString(value.pattern) + bsonCString(flags))
    if isinstance(value, BsonBinary):
        return (0x05, struct.pack('<i', len(value.buffer)) + bytes([value.subType]) + value.buffer)
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        return (0x05, struct.pack('<i', len(data)) + b'\x00' + data)
    if isinstance(value, (list, tuple)):
        return (0x04, bsonSerializeArray(value, ignoreUndefined))
    if callable(getattr(value, 'toBSON', None)):
        return bsonEncodeValue(value.toBSON(), ignoreUndefined)
    if callable(value):
        return None
    if isinstance(value, dict):
        return (0x03, bsonSerialize(value, ignoreUndefined))
    if hasattr(value, '__dict__'):
        return (0x03, bsonSerialize(vars(value), ignoreUndefined))
    raise TypeError(f'BSON: cannot encode value of type {type(value).__name__}')


def bsonSerializeArray(arr, ignoreUndefined=True):
    parts = []
    for i, v in enumerate(arr):
        enc = bsonEncodeValue(v, ignoreUndefined) or (0x0a, b'')
        parts.append(bytes([enc[0]]) + bsonCString(str(i)) + enc[1])
    return bsonWrapDocument(parts)


def bsonSerialize(doc, ignoreUndefined=True):
    if isinstance(doc, (list, tuple)):
        return bsonSerializeArray(doc, ignoreUndefined)
    if not isinstance(doc, dict):
        raise TypeError('BSON bsonSerialize expects an object')
    parts = []
    for key, val in doc.items():
        enc = bsonEncodeValue(val, ignoreUndefined)
        if enc is None:
            continue
        parts.append(bytes([enc[0]]) + bsonCString(key) + enc[1])
    return bsonWrapDocument(parts)


def bsonReadCString(buf, offset):
    end = buf.find(b'\x00', offset)
    if end == -1:
        raise Exception('BSON: unterminated bsonCString')
    return buf[offset:end].decode('utf-8', 'replace'), end + 1


def bsonDecodeValue(t, buf, offset, opts):
    if t == 0x01:
        return struct.unpack_from('<d', buf, offset)[0], offset + 8
    if t in (0x02, 0x0d, 0x0e):
        n = struct.unpack_from('<i', buf, offset)[0]
        return buf[offset + 4:offset + 4 + n - 1].decode('utf-8', 'replace'), offset + 4 + n
    if t == 0x03:
        size = struct.unpack_from('<i', buf, offset)[0]
        return bsonDeserialize(buf, opts, offset), offset + size
    if t == 0x04:
        size = struct.unpack_from('<i', buf, offset)[0]
        obj = bsonDeserialize(buf, opts, offset)
        keys = sorted(obj, key=lambda k: (0, int(k)) if k.isdigit() else (1, 0))
        return [obj[k] for k in keys], offset + size
    if t == 0x05:
        n = struct.unpack_from('<i', buf, offset)[0]
        sub = buf[offset + 4]
        data = bytes(buf[offset + 5:offset + 5 + n])
        if sub == 2:
            data = data[4:]
        if sub == 0 and not opts.get('keepBinary'):
            return data, offset + 5 + n
        return BsonBinary(data, sub), offset + 5 + n
    if t == 0x06:
        return None, offset
    if t == 0x07:
        return ObjectId(bytes(buf[offset:offset + 12])), offset + 12
    if t == 0x08:
        return buf[offset] == 1, offset + 1
    if t == 0x09:
        ms = struct.unpack_from('<q', buf, offset)[0]
        return EPOCH + timedelta(milliseconds=ms), offset + 8
    if t == 0x0a:
        return None, offset
    if t == 0x0b:
        source, o1 = bsonReadCString(buf, offset)
        flags, o2 = bsonReadCString(buf, o1)
        pyFlags = 0
        for f in flags:
            if f == 'i':
                pyFlags |= re.IGNORECASE
            elif f == 'm':
                pyFlags |= re.MULTILINE
            elif f == 's':
                pyFlags |= re.DOTALL
        return re.compile(source, pyFlags), o2
    if t == 0x10:
        return struct.unpack_from('<i', buf, offset)[0], offset + 4
    if t == 0x11:
        low, high = struct.unpack_from('<II', buf, offset)
        return BsonTimestamp(high, low), offset + 8
    if t == 0x12:
        return struct.unpack_from('<q', buf, offset)[0], offset + 8
    if t == 0x13:
        return BsonDecimal128(bytes(buf[offset:offset + 16])), offset + 16
    if t == 0xff:
        return BsonMinKey(), offset
    if t == 0x7f:
        return BsonMaxKey(), offset
    raise Exception(f'BSON: unsupported element type 0x{t:x}')


def bsonDeserialize(buf, opts=None, start=0):
    opts = opts or {}
    size = struct.unpack_from('<i', buf, start)[0]
    if size < 5 or start + size > len(buf):
        raise Exception('BSON: invalid document size')
    end = start + size - 1
    if buf[end] != 0:
        raise Exception('BSON: missing document terminator')
    out = {}
    offset = start + 4
    while offset < end:
        t = buf[offset]
        offset += 1
        if t == 0:
            break
        key, offset = bsonReadCString(buf, offset)
        value, offset = bsonDecodeValue(t, buf, offset, opts)
        out[key] = value
    return out


def bsonDeserializeStream(buf, opts=None):
    docs = []
    offset = 0
    while offset < len(buf):
        size = struct.unpack_from('<i', buf, offset)[0]
        if size < 5:
            raise Exception('BSON: invalid document size')
        docs.append(bsonDeserialize(buf, opts, offset))
        offset += size
    return docs


SCRAM_MECHANISMS = {
    'SCRAM-SHA-256': {'hash': 'sha256', 'keyLen': 32},
    'SCRAM-SHA-1': {'hash': 'sha1', 'keyLen': 20},
}


def scramHmac(alg, key, data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hmac.new(key, data, alg).digest()


def scramDigest(alg, data):
    return hashlib.new(alg, data).digest()


def scramXor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def saslName(s):
    return s.replace('=', '=3D').replace(',', '=2C')


def saslPrep(text):
    if re.fullmatch(r'[\x20-\x7e]*', text):
        return text
    s = unicodedata.normalize('NFKC', text)
    s = re.sub(r'[\u00a0\u1680\u2000-\u200b\u202f\u205f\u3000]', ' ', s)
    s = re.sub(r'[\u00ad\u034f\u1806\u180b-\u180d\u200c\u200d\u2060\ufe00-\ufe0f\ufeff]', '', s)
    if re.search(r'[\u0000-\u001f\u007f-\u009f\u2028\u2029]', s):
        raise Exception('SASLprep: prohibited character in password')
    return s


def mongoSha1Password(user, password):
    return hashlib.md5(f'{user}:mongo:{password}'.encode('utf-8')).hexdigest()


def parseScramMessage(msg):
    out = {}
    for part in msg.split(','):
        i = part.find('=')
        if i > 0:
            out[part[:i]] = part[i + 1:]
    return out


def jsBase64Decode(text):
    t = re.sub(r'[^A-Za-z0-9+/_-]', '', text).replace('-', '+').replace('_', '/')
    if len(t) % 4 == 1:
        t = t[:-1]
    t += '=' * (-len(t) % 4)
    return base64.b64decode(t)


def parseIntJS(s):
    m = re.match(r'\s*[+-]?[0-9]+', s)
    return int(m.group()) if m else None


class ScramClient:
    def __init__(self, mechanism='SCRAM-SHA-256', username=None, password=None, nonce=None):
        m = SCRAM_MECHANISMS.get(mechanism)
        if not m:
            raise Exception(f'Unsupported SCRAM mechanism: {mechanism}')
        self.mechanism = mechanism
        self.alg = m['hash']
        self.keyLen = m['keyLen']
        self.username = username
        self.password = password
        self.nonce = nonce or base64.b64encode(secrets.token_bytes(24)).decode('ascii')
        self.clientFirstBare = f'n={saslName(username)},r={self.nonce}'
        self.serverSignature = None

    def clientFirst(self):
        return f'n,,{self.clientFirstBare}'.encode('utf-8')

    def clientFinal(self, serverFirstBuf):
        serverFirst = bytes(serverFirstBuf).decode('utf-8', 'replace')
        p = parseScramMessage(serverFirst)
        if not p.get('r') or not p.get('s') or not p.get('i'):
            raise Exception('SCRAM: malformed server-first message')
        if not p['r'].startswith(self.nonce):
            raise Exception('SCRAM: server nonce does not extend client nonce')
        iterations = parseIntJS(p['i'])
        if iterations is None or iterations < 4096:
            raise Exception(f"SCRAM: iteration count {p['i']} is too low")
        salt = jsBase64Decode(p['s'])
        if self.mechanism == 'SCRAM-SHA-256':
            pw = saslPrep(self.password)
        else:
            pw = mongoSha1Password(self.username, self.password)
        salted = hashlib.pbkdf2_hmac(self.alg, pw.encode('utf-8'), salt, iterations, dklen=self.keyLen)
        clientKey = scramHmac(self.alg, salted, 'Client Key')
        storedKey = scramDigest(self.alg, clientKey)
        clientFinalNoProof = f"c=biws,r={p['r']}"
        authMessage = f'{self.clientFirstBare},{serverFirst},{clientFinalNoProof}'
        clientSignature = scramHmac(self.alg, storedKey, authMessage)
        proof = scramXor(clientKey, clientSignature)
        serverKey = scramHmac(self.alg, salted, 'Server Key')
        self.serverSignature = scramHmac(self.alg, serverKey, authMessage)
        return f"{clientFinalNoProof},p={base64.b64encode(proof).decode('ascii')}".encode('utf-8')

    def verifyServerFinal(self, serverFinalBuf):
        p = parseScramMessage(bytes(serverFinalBuf).decode('utf-8', 'replace'))
        if p.get('e'):
            raise Exception(f"SCRAM: server error: {p['e']}")
        if not p.get('v'):
            raise Exception('SCRAM: server-final missing verifier')
        got = jsBase64Decode(p['v'])
        if len(got) != len(self.serverSignature) or not hmac.compare_digest(got, self.serverSignature):
            raise Exception('SCRAM: server signature mismatch (server could not prove it knows the password)')
        return True


def uriBool(v):
    s = str(v).lower()
    if s == 'true':
        return True
    if s == 'false':
        return False
    raise Exception(f'Invalid boolean value: {v}')


def jsNumber(value):
    s = str(value).strip()
    if s == '':
        return 0.0
    if re.fullmatch(r'[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', s):
        return float(s)
    if re.fullmatch(r'0[xX][0-9a-fA-F]+', s):
        return float(int(s[2:], 16))
    if re.fullmatch(r'0[oO][0-7]+', s):
        return float(int(s[2:], 8))
    if re.fullmatch(r'0[bB][01]+', s):
        return float(int(s[2:], 2))
    if s in ('Infinity', '+Infinity'):
        return float('inf')
    if s == '-Infinity':
        return float('-inf')
    return float('nan')


def uriInt(value):
    n = jsNumber(value)
    if not math.isfinite(n) or n != math.floor(n) or n < 0:
        raise Exception(f'Invalid integer value: {value}')
    return int(n)


def uriW(v):
    return int(v) if re.fullmatch(r'[0-9]+', v) else str(v)


URI_OPTIONS = {
    'replicaset': ('replicaSet', str),
    'tls': ('tls', uriBool),
    'ssl': ('tls', uriBool),
    'tlsallowinvalidcertificates': ('tlsAllowInvalidCertificates', uriBool),
    'tlsallowinvalidhostnames': ('tlsAllowInvalidHostnames', uriBool),
    'tlscafile': ('tlsCAFile', str),
    'tlscertificatekeyfile': ('tlsCertificateKeyFile', str),
    'authsource': ('authSource', str),
    'authmechanism': ('authMechanism', str),
    'appname': ('appName', str),
    'directconnection': ('directConnection', uriBool),
    'connecttimeoutms': ('connectTimeoutMS', uriInt),
    'sockettimeoutms': ('socketTimeoutMS', uriInt),
    'serverselectiontimeoutms': ('serverSelectionTimeoutMS', uriInt),
    'maxpoolsize': ('maxPoolSize', uriInt),
    'minpoolsize': ('minPoolSize', uriInt),
    'retrywrites': ('retryWrites', uriBool),
    'retryreads': ('retryReads', uriBool),
    'w': ('w', uriW),
    'wtimeoutms': ('wtimeoutMS', uriInt),
    'journal': ('journal', uriBool),
    'readpreference': ('readPreference', str),
    'compressors': ('compressors', str),
    'loadbalanced': ('loadBalanced', uriBool),
}


def uriDecode(s, what):
    if re.search(r'%(?![0-9a-fA-F]{2})', s):
        raise Exception(f'Invalid percent-encoding in {what}')
    try:
        return urllib.parse.unquote(s, errors='strict')
    except UnicodeDecodeError:
        raise Exception(f'Invalid percent-encoding in {what}')


def uriParseHostList(hostList, isSrv):
    hosts = []
    for raw in hostList.split(','):
        if not raw:
            raise Exception('Invalid connection string: empty host')
        port = None
        if raw.startswith('['):
            end = raw.find(']')
            if end == -1:
                raise Exception(f'Invalid IPv6 host: {raw}')
            host = raw[1:end]
            rest = raw[end + 1:]
            if rest:
                if not rest.startswith(':'):
                    raise Exception(f'Invalid host: {raw}')
                port = rest[1:]
        elif raw.endswith('.sock'):
            raise Exception('Unix domain sockets are not supported')
        else:
            i = raw.rfind(':')
            if i == -1:
                host = raw
            else:
                host = raw[:i]
                port = raw[i + 1:]
        if not host:
            raise Exception(f'Invalid host: {raw}')
        if port is not None:
            if isSrv:
                raise Exception('mongodb+srv:// URIs must not specify a port')
            if not re.fullmatch(r'[0-9]+', port) or int(port) < 1 or int(port) > 65535:
                raise Exception(f'Invalid port: {port}')
            port = int(port)
        hosts.append({'host': host.lower(), 'port': port if port is not None else 27017})
    return hosts


def uriParseQuery(query, into=None):
    if into is None:
        into = {}
    if not query:
        return into
    for pair in query.split('&'):
        if not pair:
            continue
        i = pair.find('=')
        rawKey = uriDecode(pair if i == -1 else pair[:i], 'option name')
        rawVal = '' if i == -1 else uriDecode(pair[i + 1:], f'option {rawKey}')
        spec = URI_OPTIONS.get(rawKey.lower())
        if spec:
            into[spec[0]] = spec[1](rawVal)
        else:
            into.setdefault('extra', {})[rawKey] = rawVal
    return into


def parseUri(uri):
    if not isinstance(uri, str):
        raise TypeError('Connection string must be a string')
    m = re.fullmatch(r'(mongodb(?:\+srv)?)://(.*)', uri, re.IGNORECASE | re.DOTALL)
    if not m:
        raise Exception('Invalid scheme, expected connection string to start with "mongodb://" or "mongodb+srv://"')
    srv = m.group(1).lower() == 'mongodb+srv'
    rest = m.group(2)
    query = ''
    q = rest.find('?')
    if q != -1:
        query = rest[q + 1:]
        rest = rest[:q]
    dbName = ''
    slash = rest.find('/')
    authority = rest
    if slash != -1:
        authority = rest[:slash]
        dbName = uriDecode(rest[slash + 1:], 'database name')
    elif query and not rest:
        raise Exception('Invalid connection string: missing hosts')
    username = None
    password = None
    at = authority.rfind('@')
    hostPart = authority
    if at != -1:
        cred = authority[:at]
        hostPart = authority[at + 1:]
        colon = cred.find(':')
        tail = cred if colon == -1 else cred[colon + 1:]
        if re.search(r'[@/]', tail) and '@' in cred:
            raise Exception('Username and password must be percent-encoded ("@", ":" and "/" are reserved)')
        username = uriDecode(cred if colon == -1 else cred[:colon], 'username')
        password = None if colon == -1 else uriDecode(cred[colon + 1:], 'password')
        if not username:
            raise Exception('Invalid connection string: empty username')
    if not hostPart:
        raise Exception('Invalid connection string: missing hosts')
    hosts = uriParseHostList(hostPart, srv)
    if srv and len(hosts) != 1:
        raise Exception('mongodb+srv:// URIs must contain exactly one host')
    if srv and len(hosts[0]['host'].split('.')) < 3:
        raise Exception('mongodb+srv:// hostname must have at least three parts (e.g. cluster0.abcde.mongodb.net)')
    options = uriParseQuery(query)
    if srv and options.get('tls') is None:
        options['tls'] = True
    return {
        'srv': srv,
        'srvHost': hosts[0]['host'] if srv else None,
        'hosts': [] if srv else hosts,
        'username': username,
        'password': password,
        'dbName': dbName or 'test',
        'hasDbName': bool(dbName),
        'options': options,
    }


class DnsError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


DNS_RCODES = {1: 'EFORMERR', 2: 'ESERVFAIL', 3: 'ENOTFOUND', 4: 'ENOTIMP', 5: 'EREFUSED'}


def dnsNameservers():
    servers = []
    try:
        with open('/etc/resolv.conf', 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == 'nameserver':
                    servers.append(parts[1])
    except OSError:
        pass
    return servers or ['127.0.0.1']


def dnsEncodeName(name):
    out = bytearray()
    for label in name.rstrip('.').split('.'):
        raw = label.encode('ascii') if label.isascii() else label.encode('idna')
        if not 0 < len(raw) <= 63:
            raise DnsError('EINVAL', f'queryS EINVAL {name}')
        out.append(len(raw))
        out += raw
    out.append(0)
    return bytes(out)


def dnsReadName(data, offset):
    labels = []
    end = None
    hops = 0
    while True:
        if offset >= len(data) or hops > 32:
            raise DnsError('EFORMERR', 'queryS EFORMERR malformed name')
        ln = data[offset]
        if ln == 0:
            if end is None:
                end = offset + 1
            break
        if (ln & 0xc0) == 0xc0:
            if end is None:
                end = offset + 2
            offset = ((ln & 0x3f) << 8) | data[offset + 1]
            hops += 1
            continue
        labels.append(data[offset + 1:offset + 1 + ln].decode('ascii', 'replace'))
        offset += 1 + ln
    return '.'.join(labels), end


def dnsParse(data, qtype, name):
    if len(data) < 12:
        raise DnsError('EFORMERR', f'queryS EFORMERR {name}')
    flags, qd, an = struct.unpack_from('>HHH', data, 2)
    rcode = flags & 0xf
    if rcode:
        code = DNS_RCODES.get(rcode, 'EFORMERR')
        raise DnsError(code, f'queryS {code} {name}')
    off = 12
    for _ in range(qd):
        _, off = dnsReadName(data, off)
        off += 4
    records = []
    for _ in range(an):
        _, off = dnsReadName(data, off)
        rtype, _, _, rdlen = struct.unpack_from('>HHIH', data, off)
        off += 10
        rdoff = off
        off += rdlen
        if rtype != qtype:
            continue
        if qtype == 33:
            prio, weight, port = struct.unpack_from('>HHH', data, rdoff)
            target, _ = dnsReadName(data, rdoff + 6)
            records.append({'name': target, 'port': port, 'priority': prio, 'weight': weight})
        elif qtype == 16:
            chunks = []
            p = rdoff
            end = rdoff + rdlen
            while p < end:
                ln = data[p]
                chunks.append(data[p + 1:p + 1 + ln].decode('utf-8', 'replace'))
                p += 1 + ln
            records.append(chunks)
    if not records:
        raise DnsError('ENODATA', f'queryS ENODATA {name}')
    return records


def dnsRecvExact(sk, n):
    chunks = []
    got = 0
    while got < n:
        chunk = sk.recv(n - got)
        if not chunk:
            raise OSError('dns connection closed')
        chunks.append(chunk)
        got += len(chunk)
    return b''.join(chunks)


def dnsUdpExchange(ns, packet, qid):
    family = socket.AF_INET6 if ':' in ns else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sk:
        sk.settimeout(3)
        sk.sendto(packet, (ns, 53))
        while True:
            data, _ = sk.recvfrom(65535)
            if len(data) >= 12 and struct.unpack_from('>H', data, 0)[0] == qid:
                return data


def dnsTcpExchange(ns, packet, qid):
    with socket.create_connection((ns, 53), timeout=3) as sk:
        sk.sendall(struct.pack('>H', len(packet)) + packet)
        ln = struct.unpack('>H', dnsRecvExact(sk, 2))[0]
        data = dnsRecvExact(sk, ln)
    if len(data) < 12 or struct.unpack_from('>H', data, 0)[0] != qid:
        raise OSError('dns id mismatch')
    return data


def dnsLookup(name, qtype):
    servers = dnsNameservers()
    qid = secrets.randbits(16)
    packet = struct.pack('>HHHHHH', qid, 0x0100, 1, 0, 0, 0) + dnsEncodeName(name) + struct.pack('>HH', qtype, 1)
    for _ in range(2):
        for ns in servers:
            try:
                data = dnsUdpExchange(ns, packet, qid)
                if data[2] & 0x02:
                    data = dnsTcpExchange(ns, packet, qid)
            except OSError:
                continue
            return dnsParse(data, qtype, name)
    raise DnsError('ETIMEOUT', f'queryS ETIMEOUT {name}')


async def dnsResolveSrv(name):
    return await asyncio.to_thread(dnsLookup, name, 33)


async def dnsResolveTxt(name):
    return await asyncio.to_thread(dnsLookup, name, 16)


async def resolveSrvUri(parsed, resolver=None):
    if not parsed['srv']:
        return parsed
    if resolver is None:
        resolver = {'resolveSrv': dnsResolveSrv, 'resolveTxt': dnsResolveTxt}
    name = parsed['srvHost']
    records = await resolver['resolveSrv'](f'_mongodb._tcp.{name}')
    if not records:
        raise Exception(f'No SRV records found for _mongodb._tcp.{name}')
    parent = '.'.join(name.split('.')[1:])
    hosts = []
    for r in records:
        target = re.sub(r'\.$', '', r['name']).lower()
        if not (target == parent or target.endswith('.' + parent)):
            raise Exception(f'SRV target "{target}" is not within parent domain "{parent}"')
        hosts.append({'host': target, 'port': r['port']})
    txtOpts = {}
    try:
        txt = await resolver['resolveTxt'](name)
        if len(txt) > 1:
            raise Exception('Multiple TXT records found; only one is allowed')
        if len(txt) == 1:
            raw = uriParseQuery(''.join(txt[0]))
            for k in raw:
                if k not in ('authSource', 'replicaSet', 'loadBalanced'):
                    raise Exception(f'TXT record may only set authSource, replicaSet, loadBalanced (got {k})')
            txtOpts = raw
    except Exception as err:
        if getattr(err, 'code', None) not in ('ENODATA', 'ENOTFOUND'):
            raise
    return dict(parsed, hosts=hosts, options={**txtOpts, **parsed['options']})


async def resolveUri(uri, opts=None):
    return await resolveSrvUri(parseUri(uri), **(opts or {}))


WIRE_OP_MSG = 2013


WIRE_OP_COMPRESSED = 2012


WIRE_HEADER_LEN = 16


WIRE_CHECKSUM_PRESENT = 1 << 0


WIRE_MORE_TO_COME = 1 << 1


WIRE_MAX_MESSAGE_SIZE = 48_000_000


MAX_BATCH_DOCS = 100_000


MAX_BATCH_BYTES = 16 * 1024 * 1024


MONGO_RETRYABLE_CODES = {6, 7, 89, 91, 189, 262, 9001, 10107, 11600, 11602, 13435, 13436}


class MongoError(Exception):
    def __init__(self, message='', **extra):
        super().__init__(message)
        self.name = 'MongoError'
        for k, v in extra.items():
            setattr(self, k, v)


class MongoNetworkError(MongoError):
    def __init__(self, message='', **extra):
        super().__init__(message, **extra)
        self.name = 'MongoNetworkError'


class MongoServerError(MongoError):
    def __init__(self, doc):
        super().__init__(doc.get('errmsg') or doc.get('$err') or 'Unknown server error')
        self.name = 'MongoServerError'
        self.code = doc.get('code')
        self.codeName = doc.get('codeName')
        self.errorLabels = doc.get('errorLabels') or []
        self.result = doc


class MongoReply(dict):
    conn = None


_wireLock = threading.Lock()


_wireCounter = 0


def wireStripChecksum(body, flags):
    return body[:-4] if flags & WIRE_CHECKSUM_PRESENT else body


def wireNextRequestId():
    global _wireCounter
    with _wireLock:
        _wireCounter = (_wireCounter + 1) & 0x7fffffff
        return _wireCounter or 1


def encodeOpMsg(command, requestId, responseTo=0, flags=0, documentSequences=None):
    body = bsonSerialize(command)
    parts = [struct.pack('<I', flags), b'\x00', body]
    for seq in documentSequences or []:
        docs = b''.join(bsonSerialize(d) for d in seq['documents'])
        ident = (seq['identifier'] + '\x00').encode('utf-8')
        parts.append(b'\x01' + struct.pack('<i', 4 + len(ident) + len(docs)) + ident + docs)
    payload = b''.join(parts)
    header = struct.pack('<iiii', WIRE_HEADER_LEN + len(payload), requestId, responseTo, WIRE_OP_MSG)
    return header + payload


def decodeOpMsg(payload, bsonOpts=None):
    flags = struct.unpack_from('<I', payload, 0)[0]
    body = wireStripChecksum(payload[4:], flags)
    offset = 0
    doc = None
    sequences = {}
    while offset < len(body):
        kind = body[offset]
        offset += 1
        if kind == 0:
            size = struct.unpack_from('<i', body, offset)[0]
            doc = bsonDeserialize(body, bsonOpts, offset)
            offset += size
        elif kind == 1:
            size = struct.unpack_from('<i', body, offset)[0]
            end = offset + size
            p = offset + 4
            idEnd = body.find(b'\x00', p)
            if idEnd == -1:
                raise MongoError('WIRE_OP_MSG malformed sequence identifier')
            ident = body[p:idEnd].decode('utf-8', 'replace')
            sequences[ident] = bsonDeserializeStream(body[idEnd + 1:end], bsonOpts)
            offset = end
        else:
            raise MongoError(f'Unknown WIRE_OP_MSG section kind {kind}')
    if doc is None:
        raise MongoError('WIRE_OP_MSG reply had no body section')
    return {'doc': doc, 'sequences': sequences, 'flags': flags}


def isIPAddress(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def mongoIsOperatorDoc(doc):
    return isinstance(doc, dict) and len(doc) > 0 and all(isinstance(k, str) and k.startswith('$') for k in doc)


def mongoCmdHasNonIdempotentOps(cmd, sequences):
    if not cmd.get('update'):
        return False
    updates = []
    for s in sequences or []:
        if s.get('identifier') == 'updates':
            updates.extend(s.get('documents') or [])
    for u in updates:
        uu = u.get('u')
        if isinstance(uu, dict) and any(uu.get(k) for k in ('$inc', '$push', '$pull', '$addToSet', '$mul', '$pop', '$rename')):
            return True
    return False


def toPortJS(text):
    try:
        n = int(text)
    except (TypeError, ValueError):
        n = 0
    return n or 27017


class Connection:
    def __init__(self, host, port, tls=False, tlsOptions=None, connectTimeoutMS=30000, socketTimeoutMS=0, appName=None, bsonOptions=None):
        self.host = host
        self.port = port
        self.useTls = tls
        self.tlsOptions = tlsOptions or {}
        self.connectTimeoutMS = connectTimeoutMS
        self.socketTimeoutMS = socketTimeoutMS
        self.appName = appName
        self.bsonOptions = bsonOptions or {}
        self.sock = None
        self.closed = False
        self.hello = None
        self.address = f'{host}:{port}'

    def close(self):
        self.closed = True
        sk = self.sock
        if sk is not None:
            try:
                sk.close()
            except OSError:
                pass

    def _wrapTls(self, raw):
        opts = self.tlsOptions
        ctx = ssl.create_default_context(cafile=opts.get('tlsCAFile') or None)
        if opts.get('tlsAllowInvalidHostnames') or opts.get('tlsAllowInvalidCertificates'):
            ctx.check_hostname = False
        if opts.get('tlsAllowInvalidCertificates'):
            ctx.verify_mode = ssl.CERT_NONE
        if opts.get('tlsCertificateKeyFile'):
            ctx.load_cert_chain(certfile=opts['tlsCertificateKeyFile'])
        return ctx.wrap_socket(raw, server_hostname=self.host)

    def _connectSync(self):
        timeout = self.connectTimeoutMS / 1000 if self.connectTimeoutMS else None
        try:
            raw = socket.create_connection((self.host, self.port), timeout=timeout)
        except socket.timeout:
            raise MongoNetworkError(f'connection to {self.address} timed out after {self.connectTimeoutMS}ms')
        except OSError as err:
            raise MongoNetworkError(f'connect to {self.address} failed: {err}', cause=err)
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, 'TCP_KEEPIDLE'):
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
            if self.useTls:
                raw = self._wrapTls(raw)
        except socket.timeout:
            raw.close()
            raise MongoNetworkError(f'connection to {self.address} timed out after {self.connectTimeoutMS}ms')
        except OSError as err:
            raw.close()
            raise MongoNetworkError(f'connect to {self.address} failed: {err}', cause=err)
        raw.settimeout(self.socketTimeoutMS / 1000 if self.socketTimeoutMS else None)
        self.sock = raw
        self.closed = False

    def _recvExact(self, n, deadline):
        chunks = []
        remaining = n
        while remaining > 0:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise socket.timeout('command deadline')
                self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(min(remaining, 1 << 20))
            except socket.timeout:
                raise
            except OSError as err:
                raise MongoNetworkError(f'socket error on {self.address}: {err}', cause=err)
            if not chunk:
                raise MongoNetworkError(f'connection to {self.address} closed')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    def _commandSync(self, db, cmd, documentSequences=None, timeoutMS=None):
        if self.closed:
            raise MongoNetworkError(f'connection to {self.address} is closed')
        requestId = wireNextRequestId()
        msgCmd = dict(cmd)
        msgCmd['$db'] = db
        msg = encodeOpMsg(msgCmd, requestId, documentSequences=documentSequences)
        if len(msg) > WIRE_MAX_MESSAGE_SIZE:
            raise MongoError(f'command exceeds max message size ({len(msg)} bytes)')
        deadline = time.monotonic() + timeoutMS / 1000 if timeoutMS else None
        try:
            if deadline is not None:
                self.sock.settimeout(max(deadline - time.monotonic(), 0.001))
            self.sock.sendall(msg)
        except socket.timeout:
            self.close()
            if timeoutMS:
                raise MongoNetworkError(f'command timed out after {timeoutMS}ms')
            raise MongoNetworkError(f'write failed: timed out')
        except OSError as err:
            self.close()
            raise MongoNetworkError(f'write failed: {err}', cause=err)
        try:
            while True:
                header = self._recvExact(4, deadline)
                length = struct.unpack('<i', header)[0]
                if length < WIRE_HEADER_LEN + 5 or length > WIRE_MAX_MESSAGE_SIZE:
                    raise MongoNetworkError(f'invalid message length {length} from {self.address}')
                frame = header + self._recvExact(length - 4, deadline)
                responseTo = struct.unpack_from('<i', frame, 8)[0]
                opCode = struct.unpack_from('<i', frame, 12)[0]
                if opCode == WIRE_OP_COMPRESSED:
                    raise MongoNetworkError(f'protocol error from {self.address}: WIRE_OP_COMPRESSED replies are not supported (do not negotiate compressors)')
                if opCode != WIRE_OP_MSG:
                    raise MongoNetworkError(f'protocol error from {self.address}: unexpected opcode {opCode}')
                if responseTo == requestId:
                    break
        except socket.timeout:
            self.close()
            if timeoutMS:
                raise MongoNetworkError(f'command timed out after {timeoutMS}ms')
            raise MongoNetworkError(f'socket timeout on {self.address}')
        except MongoNetworkError:
            self.close()
            raise
        finally:
            if not self.closed:
                self.sock.settimeout(self.socketTimeoutMS / 1000 if self.socketTimeoutMS else None)
        reply = decodeOpMsg(frame[WIRE_HEADER_LEN:], self.bsonOptions)
        doc = reply['doc']
        if doc.get('ok') != 1:
            raise MongoServerError(doc)
        return doc

    def _handshakeSync(self, username=None, password=None, authSource=None, authMechanism=None, appName=None, dbName=None):
        machine = platform.machine()
        arch = {'x86_64': 'x64', 'aarch64': 'arm64'}.get(machine, machine)
        client = {}
        if appName:
            client['application'] = {'name': appName[:128]}
        client['driver'] = {'name': 'native-python', 'version': '1.0.0'}
        client['os'] = {'type': platform.system(), 'name': sys.platform, 'architecture': arch, 'version': platform.release()}
        client['platform'] = f'Python {platform.python_version()}'
        cmd = {'hello': 1, 'helloOk': True, 'client': client}
        authDb = authSource or ('admin' if username else None)
        mech = authMechanism if authMechanism and authMechanism != 'DEFAULT' else None
        if username and not mech:
            cmd['saslSupportedMechs'] = f'{authDb}.{username}'
        hello = self._commandSync('admin', cmd, None, self.connectTimeoutMS)
        self.hello = hello
        mwv = hello.get('maxWireVersion')
        if mwv is not None and mwv < 8:
            raise MongoError(f'Server wire version {mwv} is too old; need MongoDB 4.2+ (wire version 8+)')
        if username:
            chosen = mech
            if not chosen:
                supported = hello.get('saslSupportedMechs') or []
                chosen = 'SCRAM-SHA-256' if 'SCRAM-SHA-256' in supported else 'SCRAM-SHA-1'
            if chosen not in ('SCRAM-SHA-256', 'SCRAM-SHA-1'):
                raise MongoError(f'Unsupported authMechanism "{chosen}" (supported: SCRAM-SHA-256, SCRAM-SHA-1)')
            self._authenticateScramSync(chosen, authDb, username, password if password is not None else '')
        return hello

    def _authenticateScramSync(self, mechanism, authDb, username, password):
        scram = ScramClient(mechanism=mechanism, username=username, password=password)

        def payloadOf(res):
            p = res.get('payload')
            if isinstance(p, (bytes, bytearray)):
                return bytes(p)
            if isinstance(p, BsonBinary):
                return p.buffer
            raise MongoError('SCRAM: server reply is missing a binary payload')

        r = self._commandSync(authDb, {'saslStart': 1, 'mechanism': mechanism, 'payload': scram.clientFirst(), 'autoAuthorize': 1, 'options': {'skipEmptyExchange': True}})
        conversationId = r.get('conversationId')
        r = self._commandSync(authDb, {'saslContinue': 1, 'conversationId': conversationId, 'payload': scram.clientFinal(payloadOf(r))})
        scram.verifyServerFinal(payloadOf(r))
        while not r.get('done'):
            r = self._commandSync(authDb, {'saslContinue': 1, 'conversationId': conversationId, 'payload': b''})

    async def connect(self):
        await asyncio.to_thread(self._connectSync)
        return self

    async def command(self, db, cmd, documentSequences=None, timeoutMS=None):
        return await asyncio.to_thread(self._commandSync, db, cmd, documentSequences, timeoutMS)

    async def handshake(self, username=None, password=None, authSource=None, authMechanism=None, appName=None, dbName=None):
        return await asyncio.to_thread(self._handshakeSync, username, password, authSource, authMechanism, appName, dbName)

    async def authenticateScram(self, mechanism, authDb, username, password):
        return await asyncio.to_thread(self._authenticateScramSync, mechanism, authDb, username, password)


class MongoClient:
    def __init__(self, uri, options=None):
        self.uri = uri
        self.options = options if options is not None else {}
        self._parsed = parseUri(uri)
        self._resolved = None
        self._pool = []
        self._all = set()
        self._opening = 0
        self._waiters = []
        self._connected = False
        self._closing = False
        self._maxPool = 5
        self._opts = {}
        self._dbName = None

    async def connect(self):
        if self._connected:
            return self
        parsed = await resolveSrvUri(self._parsed)
        self._resolved = parsed
        o = dict(parsed['options'])
        o.update(self.options)
        self._opts = o
        mp = o.get('maxPoolSize')
        self._maxPool = max(1, mp if mp is not None else 5)
        self._dbName = parsed['dbName']
        conn = await self._openConnection()
        self._pool.append(conn)
        self._connected = True
        self._closing = False
        return self

    def db(self, name=None):
        if not self._resolved:
            raise MongoError('MongoClient must be connected before calling db()')
        return Db(self, name or self._dbName)

    async def _openConnection(self):
        p = self._resolved
        o = self._opts
        sst = o.get('serverSelectionTimeoutMS')
        deadline = time.monotonic() + (sst if sst is not None else 30000) / 1000
        useTls = bool(o.get('tls'))
        tlsOptions = {
            'tlsAllowInvalidCertificates': o.get('tlsAllowInvalidCertificates'),
            'tlsAllowInvalidHostnames': o.get('tlsAllowInvalidHostnames'),
            'tlsCAFile': o.get('tlsCAFile'),
            'tlsCertificateKeyFile': o.get('tlsCertificateKeyFile'),
        }
        cto = o.get('connectTimeoutMS')
        sto = o.get('socketTimeoutMS')
        shuffled = list(p['hosts'])
        random.shuffle(shuffled)
        lastErr = None
        while time.monotonic() < deadline:
            for h in shuffled:
                conn = Connection(h['host'], h['port'], tls=useTls, tlsOptions=tlsOptions,
                                  connectTimeoutMS=cto if cto is not None else 10000,
                                  socketTimeoutMS=sto if sto is not None else 0,
                                  appName=o.get('appName'))
                try:
                    await conn.connect()
                    authSource = o.get('authSource') or (p['dbName'] if p['hasDbName'] and p['username'] else None) or 'admin'
                    hello = await conn.handshake(username=p['username'], password=p['password'],
                                                 authSource=authSource, authMechanism=o.get('authMechanism'),
                                                 appName=o.get('appName'))
                    usable = (bool(o.get('directConnection')) or bool(hello.get('isWritablePrimary'))
                              or bool(hello.get('ismaster')) or hello.get('msg') == 'isdbgrid')
                    if not usable:
                        conn.close()
                        primary = hello.get('primary')
                        if primary and not any(f"{x['host']}:{x['port']}" == primary.lower() for x in shuffled):
                            ph, _, pp = primary.partition(':')
                            shuffled.append({'host': ph.lower(), 'port': toPortJS(pp)})
                        lastErr = MongoNetworkError(f"{h['host']}:{h['port']} is not a writable primary")
                        continue
                    self._all.add(conn)
                    return conn
                except Exception as err:
                    conn.close()
                    if isinstance(err, MongoServerError):
                        raise
                    if str(err).startswith('SCRAM:'):
                        raise
                    lastErr = err
            await asyncio.sleep(0.25)
        msg = str(lastErr) if lastErr is not None else 'no servers available'
        raise MongoNetworkError(f'Server selection timed out after {sst if sst is not None else 30000}ms: {msg}', cause=lastErr)

    async def _acquire(self):
        if self._closing:
            raise MongoError('MongoClient is closed')
        while self._pool:
            c = self._pool.pop()
            if not c.closed:
                return c
        self._all = {c for c in self._all if not c.closed}
        self._pool = [c for c in self._pool if not c.closed]
        if len(self._all) + self._opening < self._maxPool:
            return await self._openReserved()
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        return await fut

    async def _openReserved(self):
        self._opening += 1
        try:
            return await self._openConnection()
        finally:
            self._opening -= 1

    def _popWaiter(self):
        while self._waiters:
            w = self._waiters.pop(0)
            if not w.done():
                return w
        return None

    async def _replaceWaiter(self, w):
        try:
            conn = await self._openReserved()
        except Exception as err:
            if not w.done():
                w.set_exception(err)
            return
        if w.done():
            self._release(conn)
        else:
            w.set_result(conn)

    def _release(self, conn):
        if conn.closed:
            w = self._popWaiter()
            if w is not None:
                asyncio.ensure_future(self._replaceWaiter(w))
            return
        w = self._popWaiter()
        if w is not None:
            w.set_result(conn)
        else:
            self._pool.append(conn)

    async def _execute(self, dbName, cmd, opts=None):
        o = opts or {}
        pinned = o.get('conn')
        sequences = o.get('sequences')
        read = bool(o.get('read'))
        write = bool(o.get('write'))
        if not self._connected:
            raise MongoError('MongoClient is not connected')
        retryable = (read and self._opts.get('retryReads') is not False) or (write and self._opts.get('retryWrites') is not False)
        attempts = 1 if pinned else (2 if retryable else 1)
        lastErr = None
        for attempt in range(1, attempts + 1):
            conn = pinned or await self._acquire()
            try:
                res = await conn.command(dbName, cmd, documentSequences=sequences,
                                         timeoutMS=self._opts.get('socketTimeoutMS') or 60000)
                reply = MongoReply(res)
                reply.conn = conn
                if not pinned:
                    self._release(conn)
                return reply
            except Exception as err:
                if not pinned:
                    self._release(conn)
                lastErr = err
                transient = isinstance(err, MongoNetworkError) or (
                    isinstance(err, MongoServerError) and err.code in MONGO_RETRYABLE_CODES)
                safe = read or (write and not mongoCmdHasNonIdempotentOps(cmd, sequences))
                if not transient or attempt == attempts or not safe:
                    raise
                await asyncio.sleep(0.1 * attempt)
        raise lastErr

    async def close(self):
        self._closing = True
        self._connected = False
        waiters = self._waiters
        self._waiters = []
        for w in waiters:
            if not w.done():
                w.set_exception(MongoError('MongoClient closed while waiting for a connection'))
        for c in list(self._all):
            c.close()
        self._all.clear()
        self._pool = []


class _ListResult:
    def __init__(self, items):
        self._items = items

    async def toArray(self):
        return list(self._items)


class Db:
    def __init__(self, client, name):
        self.client = client
        self.databaseName = name

    def collection(self, name):
        return Collection(self, name)

    async def listCollections(self):
        r = await self._run({'listCollections': 1}, {'read': True})
        return _ListResult(r['cursor']['firstBatch'])

    async def command(self, cmd):
        return await self._run(cmd, {})

    async def _run(self, cmd, opts=None):
        return await self.client._execute(self.databaseName, cmd, opts or {})


class Collection:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    async def _run(self, cmd, opts=None):
        return await self.db._run(cmd, opts)

    async def _writeCmd(self, cmd, seqName, docs, ordered=True):
        chunks = []
        cur = []
        nbytes = 0
        for d in docs:
            approx = len(bsonSerialize(d))
            if cur and (len(cur) >= MAX_BATCH_DOCS or nbytes + approx > MAX_BATCH_BYTES):
                chunks.append(cur)
                cur = []
                nbytes = 0
            cur.append(d)
            nbytes += approx
        if cur:
            chunks.append(cur)
        merged = {'n': 0, 'nModified': 0, 'upserted': [], 'writeErrors': []}
        offset = 0
        for chunk in chunks:
            res = await self._run({**cmd, 'ordered': ordered},
                                  {'sequences': [{'identifier': seqName, 'documents': chunk}], 'write': True})
            merged['n'] += res.get('n') or 0
            merged['nModified'] += res.get('nModified') or 0
            for u in res.get('upserted') or []:
                merged['upserted'].append({**u, 'index': u['index'] + offset})
            for e in res.get('writeErrors') or []:
                merged['writeErrors'].append({**e, 'index': e['index'] + offset})
            if res.get('writeConcernError'):
                merged['writeConcernError'] = res['writeConcernError']
            offset += len(chunk)
            if ordered and merged['writeErrors']:
                break
        if merged['writeErrors']:
            first = merged['writeErrors'][0]
            raise MongoServerError({'errmsg': first.get('errmsg'), 'code': first.get('code'), 'ok': 0,
                                    'writeErrors': merged['writeErrors'], 'result': merged})
        if merged.get('writeConcernError'):
            raise MongoServerError({**merged['writeConcernError'], 'ok': 0})
        return merged

    def find(self, filter=None, options=None):
        return FindCursor(self, {} if filter is None else filter, options or {})

    async def findOne(self, filter=None, options=None):
        opts = dict(options or {})
        opts['limit'] = 1
        opts['batchSize'] = 1
        cur = self.find(filter, opts)
        try:
            return await cur.next()
        finally:
            await cur.close()

    async def insertOne(self, doc):
        d = dict(doc)
        if '_id' not in d:
            d['_id'] = ObjectId()
        await self._writeCmd({'insert': self.name}, 'documents', [d])
        return {'acknowledged': True, 'insertedId': d['_id']}

    async def insertMany(self, docs, ordered=True):
        prepared = [x if '_id' in x else {**x, '_id': ObjectId()} for x in docs]
        await self._writeCmd({'insert': self.name}, 'documents', prepared, ordered=ordered)
        return {'acknowledged': True, 'insertedCount': len(prepared),
                'insertedIds': {i: d['_id'] for i, d in enumerate(prepared)}}

    async def _update(self, filter, update, upsert=False, multi=False):
        if isinstance(update, dict) and not mongoIsOperatorDoc(update) and multi:
            raise TypeError('Update document requires atomic operators ($set, $inc, ...)')
        r = await self._writeCmd({'update': self.name}, 'updates',
                                 [{'q': filter, 'u': update, 'multi': multi, 'upsert': upsert}])
        upserted = r['upserted']
        return {
            'acknowledged': True,
            'matchedCount': r['n'] - len(upserted),
            'modifiedCount': r['nModified'],
            'upsertedCount': len(upserted),
            'upsertedId': upserted[0].get('_id') if upserted else None,
        }

    async def updateOne(self, filter, update, opts=None):
        o = opts or {}
        return await self._update(filter, update, upsert=bool(o.get('upsert')), multi=False)

    async def updateMany(self, filter, update, opts=None):
        o = opts or {}
        return await self._update(filter, update, upsert=bool(o.get('upsert')), multi=True)

    async def replaceOne(self, filter, doc, opts=None):
        if mongoIsOperatorDoc(doc):
            raise TypeError('Replacement document must not contain atomic operators')
        o = opts or {}
        return await self._update(filter, doc, upsert=bool(o.get('upsert')), multi=False)

    async def deleteOne(self, filter=None):
        r = await self._writeCmd({'delete': self.name}, 'deletes', [{'q': {} if filter is None else filter, 'limit': 1}])
        return {'acknowledged': True, 'deletedCount': r['n']}

    async def deleteMany(self, filter=None):
        r = await self._writeCmd({'delete': self.name}, 'deletes', [{'q': {} if filter is None else filter, 'limit': 0}])
        return {'acknowledged': True, 'deletedCount': r['n']}

    async def countDocuments(self, filter=None):
        r = await self._run({'count': self.name, 'query': {} if filter is None else filter}, {'read': True})
        return r['n']

    async def bulkWrite(self, operations, ordered=True):
        if not operations:
            return {'acknowledged': True, 'insertedCount': 0, 'matchedCount': 0, 'modifiedCount': 0,
                    'deletedCount': 0, 'upsertedCount': 0, 'upsertedIds': {}}
        result = {'acknowledged': True, 'insertedCount': 0, 'matchedCount': 0, 'modifiedCount': 0,
                  'deletedCount': 0, 'upsertedCount': 0, 'upsertedIds': {}}
        groups = []
        for index, op in enumerate(operations):
            kind = next(iter(op), None)
            last = groups[-1] if groups else None
            if last and last['kind'] == kind:
                last['items'].append({'op': op[kind], 'index': index})
            else:
                groups.append({'kind': kind, 'items': [{'op': op[kind], 'index': index}]})
        for g in groups:
            kind = g['kind']
            if kind in ('replaceOne', 'updateOne', 'updateMany'):
                updates = []
                for item in g['items']:
                    op = item['op']
                    u = op.get('replacement') if kind == 'replaceOne' else op.get('update')
                    if kind == 'replaceOne' and mongoIsOperatorDoc(u):
                        raise TypeError('Replacement document must not contain atomic operators')
                    updates.append({'q': op.get('filter'), 'u': u, 'multi': kind == 'updateMany', 'upsert': bool(op.get('upsert'))})
                r = await self._writeCmd({'update': self.name}, 'updates', updates, ordered=ordered)
                result['matchedCount'] += r['n'] - len(r['upserted'])
                result['modifiedCount'] += r['nModified']
                result['upsertedCount'] += len(r['upserted'])
                for up in r['upserted']:
                    result['upsertedIds'][g['items'][up['index']]['index']] = up.get('_id')
            elif kind in ('deleteOne', 'deleteMany'):
                deletes = [{'q': item['op'].get('filter'), 'limit': 1 if kind == 'deleteOne' else 0} for item in g['items']]
                r = await self._writeCmd({'delete': self.name}, 'deletes', deletes, ordered=ordered)
                result['deletedCount'] += r['n']
            elif kind == 'insertOne':
                docs = []
                for item in g['items']:
                    doc = item['op']['document']
                    docs.append(doc if '_id' in doc else {**doc, '_id': ObjectId()})
                await self._writeCmd({'insert': self.name}, 'documents', docs, ordered=ordered)
                result['insertedCount'] += len(docs)
            else:
                raise TypeError(f'Unsupported bulkWrite operation: {kind}')
        return result

    async def drop(self):
        try:
            await self._run({'drop': self.name}, {'write': True})
        except Exception as e:
            if getattr(e, 'code', None) != 26:
                raise
        return True


class FindCursor:
    def __init__(self, collection, filter=None, options=None):
        self._col = collection
        self._filter = {} if filter is None else filter
        self._opts = dict(options or {})
        self._buffer = None
        self._id = 0
        self._closed = False
        self._conn = None

    def sort(self, spec):
        self._opts['sort'] = spec
        return self

    def limit(self, n):
        self._opts['limit'] = n
        return self

    def skip(self, n):
        self._opts['skip'] = n
        return self

    def project(self, p):
        self._opts['projection'] = p
        return self

    def batchSize(self, n):
        self._opts['batchSize'] = n
        return self

    async def _init(self):
        if self._buffer is not None:
            return
        cmd = {'find': self._col.name, 'filter': self._filter}
        for k in ('sort', 'skip', 'limit', 'projection', 'batchSize', 'hint', 'maxTimeMS'):
            if self._opts.get(k) is not None:
                cmd[k] = self._opts[k]
        if cmd.get('limit') == 0:
            del cmd['limit']
        res = await self._col._run(cmd, {'read': True})
        self._buffer = list(res['cursor']['firstBatch'])
        self._id = int(res['cursor'].get('id') or 0)
        self._conn = res.conn

    async def next(self):
        await self._init()
        while not self._buffer and self._id != 0 and not self._closed:
            cmd = {'getMore': BsonLong(self._id), 'collection': self._col.name}
            if self._opts.get('batchSize'):
                cmd['batchSize'] = self._opts['batchSize']
            res = await self._col._run(cmd, {'conn': self._conn})
            self._buffer = list(res['cursor']['nextBatch'])
            self._id = int(res['cursor'].get('id') or 0)
        return self._buffer.pop(0) if self._buffer else None

    async def toArray(self):
        out = []
        d = await self.next()
        while d is not None:
            out.append(d)
            d = await self.next()
        return out

    async def forEach(self, fn):
        d = await self.next()
        while d is not None:
            r = fn(d)
            if inspect.isawaitable(r):
                await r
            d = await self.next()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._id != 0:
            try:
                await self._col._run({'killCursors': self._col.name, 'cursors': [BsonLong(self._id)]}, {'conn': self._conn})
            except Exception:
                pass
        self._id = 0

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        try:
            while True:
                d = await self.next()
                if d is None:
                    break
                yield d
        finally:
            await self.close()


def objectEntries(v):
    if v is None:
        raise TypeError('Cannot convert undefined or null to object')
    if isinstance(v, dict):
        return list(v.items())
    if isinstance(v, (list, tuple)):
        return [(str(i), x) for i, x in enumerate(v)]
    return []


class MongoDB:
    def __init__(self, url, options=None):
        self.url = url
        self.options = options if options is not None else {}
        self.data = {}
        self.db = None
        self.client = None
        self.lists = None
        self._writeMutex = asyncio.Lock()
        self._initPromise = None

    async def _initInner(self):
        client = MongoClient(self.url, self.options)
        try:
            await client.connect()
        except Exception as err:
            self._initPromise = None
            try:
                await client.close()
            except Exception:
                pass
            raise err
        self.client = client
        self.db = client.db()
        return self.db

    async def initDB(self):
        if self.db is not None:
            return self.db
        if self._initPromise is None:
            self._initPromise = asyncio.ensure_future(self._initInner())
        return await self._initPromise

    async def close(self):
        c = self.client
        self.client = None
        self.db = None
        self._initPromise = None
        if c is not None:
            await c.close()

    async def read(self):
        await self.initDB()
        listCol = self.db.collection('lists')
        self.lists = await listCol.findOne({})
        if self.lists is None or self.lists.get('data') is None:
            await listCol.insertOne({'data': []})
            self.lists = await listCol.findOne({})
        garbage = []
        self.data = {}

        async def load(item):
            name = item['name']
            try:
                collection = self.db.collection(name)
                docs = await collection.find({}).toArray()
                self.data[name] = {pair[0]: pair[1] for pair in (v['data'] for v in docs)}
            except Exception as e:
                garbage.append(name)
                print(e, file=sys.stderr)

        await asyncio.gather(*(load(item) for item in self.lists['data']))
        if garbage:
            try:
                await listCol.updateOne(
                    {'_id': self.lists['_id']},
                    {'$set': {'data': [v for v in self.lists['data'] if v.get('name') not in garbage]}},
                )
            except Exception as e:
                print(e, file=sys.stderr)
        return self.data

    async def write(self, data):
        async with self._writeMutex:
            return await self._writeUnlocked(data)

    async def _writeUnlocked(self, data):
        if self.lists is None or data is None:
            raise Exception('Write called before read(), or no data provided')
        keys = list(data.keys())

        async def writeKey(key):
            entries = objectEntries(data[key])
            collection = self.db.collection(key)
            if entries:
                ops = [
                    {'replaceOne': {'filter': {'_key': k}, 'replacement': {'_key': k, 'data': [k, v]}, 'upsert': True}}
                    for k, v in entries
                ]
                await collection.bulkWrite(ops)
                validKeys = [k for k, _ in entries]
                await collection.deleteMany({'_key': {'$nin': validKeys}})
            else:
                await collection.deleteMany({})

        await asyncio.gather(*(writeKey(k) for k in keys))
        listDoc = [{'name': name} for name in keys]
        listCol = self.db.collection('lists')
        doc = await listCol.findOne({'_id': self.lists['_id']})
        if doc is None:
            await self.read()
            return await self._writeUnlocked(data)
        await listCol.updateOne({'_id': self.lists['_id']}, {'$set': {'data': listDoc}})
        self.data = data
        return True

DEFAULT = {
    'user': {
        'exp': 0, 'limit': 10, 'registered': False, 'afk': -1, 'afkReason': '', 'banned': False,
        'warn': 0, 'level': 0, 'password': '', 'premium': False, 'premiumTime': 0, 'autoreconnect': False,
    },
    'userUnregistered': {'name': '', 'email': '', 'age': -1, 'regTime': -1},
    'chat': {
        'isBanned': False, 'welcome': False, 'detect': False, 'sWelcome': '', 'sBye': '', 'sPromote': '',
        'sDemote': '', 'delete': False, 'useDocument': False, 'viewonce': False, 'aiChat': False,
        'aiSessionChat': [], 'expired': 0, 'antiLink': False, 'antispam': False, 'antinsfw': False,
    },
    'settings': {
        'self': False, 'restrict': False, 'status': 0, 'anticall': True, 'autoread': True,
        'autorestart': False, 'clearlag': True, 'timeclearlag': 0, 'restartDB': 0, 'resetlimit': 0,
    },
}


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and not (isinstance(x, float) and math.isnan(x))


def _numeric_keys(spec):
    return [key for key in spec if isinstance(spec[key], (int, float)) and not isinstance(spec[key], bool)]


DEFAULT_USER_NUMERIC_KEYS = _numeric_keys(DEFAULT['user'])
DEFAULT_USER_UNREGISTERED_NUMERIC_KEYS = _numeric_keys(DEFAULT['userUnregistered'])
DEFAULT_CHAT_NUMERIC_KEYS = _numeric_keys(DEFAULT['chat'])
DEFAULT_SETTINGS_NUMERIC_KEYS = _numeric_keys(DEFAULT['settings'])


def _bytes_default(value):
    if isinstance(value, (bytes, bytearray)):
        return {'type': 'Buffer', 'data': list(value)}
    if hasattr(value, 'toNumber'):
        return value.toNumber()
    return str(value)


def _buffer_hook(obj):
    if obj.get('type') == 'Buffer' and isinstance(obj.get('data'), list):
        return bytes(obj['data'])
    return obj


def stringify(obj):
    return json.dumps(obj, indent=2, default=_bytes_default, ensure_ascii=False)


def parse(text):
    return json.loads(text, object_hook=_buffer_hook)


def _err_message(err):
    return getattr(err, 'message', None) or err


class SQLiteAdapter:
    def __init__(self, filename):
        folder = os.path.dirname(os.path.abspath(filename))
        os.makedirs(folder, exist_ok=True)
        self.db = sqlite3.connect(filename, check_same_thread=False, isolation_level=None)
        self.db.execute(
            'CREATE TABLE IF NOT EXISTS bot_data (\n        id INTEGER PRIMARY KEY CHECK (id = 1),\n        data TEXT\n      )'
        )

    async def read(self):
        row = self.db.execute('SELECT data FROM bot_data WHERE id = 1').fetchone()
        if row is None or row[0] is None:
            return None
        return parse(row[0])

    async def write(self, obj):
        data = stringify(obj)
        self.db.execute(
            'INSERT INTO bot_data (id, data) VALUES (1, ?)\n      ON CONFLICT(id) DO UPDATE SET data = excluded.data',
            (data,),
        )

    def close(self):
        self.db.close()


class CloudDBAdapter:
    def __init__(self, url, serialize=None, deserialize=None, fetch_options=None):
        self.url = url
        self.serialize = serialize or stringify
        self.deserialize = deserialize or parse
        self.fetch_options = dict(fetch_options or {})

    async def read(self):
        try:
            options = dict(self.fetch_options)
            headers = {'Accept': 'application/json;q=0.9,text/plain', **(options.pop('headers', None) or {})}
            res = await http.get(self.url, headers=headers, raise_for_status=False, timeout=60, **options)
            if not res.ok:
                raise RuntimeError(res.statusText)
            return self.deserialize(res.text())
        except Exception:
            return None

    async def write(self, obj):
        options = dict(self.fetch_options)
        headers = {'Content-Type': 'application/json', **(options.pop('headers', None) or {})}
        res = await http.post(
            self.url, headers=headers, data=self.serialize(obj).encode('utf-8'),
            raise_for_status=False, timeout=60, **options,
        )
        if not res.ok:
            raise RuntimeError(res.statusText)
        return res.text()


class Low:
    def __init__(self, adapter, default_data=None):
        self.adapter = adapter
        self.data = default_data
        self.chain = None
        self._loading = None

    async def read(self):
        self.data = await self.adapter.read()
        return self.data

    async def write(self):
        if self.data is None:
            raise RuntimeError('Cannot write, data is null or undefined. Call read() first or set data manually.')
        await self.adapter.write(self.data)


def local_sqlite_adapter():
    os.makedirs('./data', exist_ok=True)
    first = (helper.opts.get('_') or [None])[0]
    prefix = f'{first}_' if first else ''
    return SQLiteAdapter(f'./data/{prefix}database.db')


class FallbackAdapter:
    def __init__(self, primary, label):
        self.primary = primary
        self.label = label
        self.fallback = None

    def _get_fallback(self):
        if self.fallback is None:
            print(chalk.red('[ DB ]') + chalk.gray(f' {self.label} unavailable, falling back to local SQLite so data keeps persisting.'), file=sys.stderr)
            self.fallback = local_sqlite_adapter()
        return self.fallback

    async def read(self):
        if self.fallback is not None:
            return await self.fallback.read()
        try:
            return await self.primary.read()
        except Exception as err:
            print(chalk.red('[ DB ]') + chalk.gray(f' {self.label} read() failed: {_err_message(err)}'), file=sys.stderr)
            return await self._get_fallback().read()

    async def write(self, obj):
        if self.fallback is not None:
            return await self.fallback.write(obj)
        try:
            return await self.primary.write(obj)
        except Exception as err:
            print(chalk.red('[ DB ]') + chalk.gray(f' {self.label} write() failed: {_err_message(err)}'), file=sys.stderr)
            return await self._get_fallback().write(obj)

    async def close(self):
        for target in (self.fallback, self.primary):
            fn = getattr(target, 'close', None) if target is not None else None
            if fn:
                return await maybe_await(fn())


def create_database_adapter(database_url):
    if database_url and re.search(r'mongodb(\+srv)?://', database_url, re.IGNORECASE):
        return FallbackAdapter(MongoDB(database_url, {}), 'MongoDB')
    if database_url and re.search(r'mysql://', database_url, re.IGNORECASE):
        return FallbackAdapter(MySqlAdapter(database_url), 'MySQL')
    if database_url and re.search(r'https?://', database_url):
        return FallbackAdapter(CloudDBAdapter(database_url), 'CloudDB')
    return local_sqlite_adapter()


database = G.db if G.db is not None and type(G.db).__name__ == 'Low' else Low(create_database_adapter(os.environ.get('DATABASE', '')))

DB_AUTOSAVE_DEBOUNCE_MS = 15000
DB_AUTOSAVE_MAX_WAIT_MS = 60000
_autosave = {'timer': None, 'first': None, 'inflight': False}


async def _autosave_write():
    try:
        await database.write()
    except Exception as err:
        print('[DB AUTOSAVE] write failed:', err, file=sys.stderr)
    finally:
        _autosave['inflight'] = False


def _autosave_fire():
    _autosave['timer'] = None
    _autosave['first'] = None
    if _autosave['inflight']:
        return
    _autosave['inflight'] = True
    spawn(_autosave_write())


def schedule_autosave():
    if database.data is None:
        return
    loop = asyncio.get_event_loop()
    now = time.time() * 1000
    if _autosave['first'] is None:
        _autosave['first'] = now
    if _autosave['timer'] is not None:
        _autosave['timer'].cancel()
    elapsed = now - _autosave['first']
    delay = min(DB_AUTOSAVE_DEBOUNCE_MS, max(0, DB_AUTOSAVE_MAX_WAIT_MS - elapsed))
    _autosave['timer'] = loop.call_later(delay / 1000, _autosave_fire)


class Chain:
    def __init__(self, data):
        self.data = data

    def get(self, key):
        return self.data.get(key) if isinstance(self.data, dict) else None

    def set(self, key, value):
        self.data[key] = value
        return self

    def has(self, key):
        return key in self.data

    def clone(self):
        return Chain(copy.deepcopy(self.data))

    def value(self):
        return self.data

    def map(self, fn):
        if isinstance(self.data, list):
            self.data = [fn(x) for x in self.data]
        return self

    def filter(self, fn):
        if isinstance(self.data, list):
            self.data = [x for x in self.data if fn(x)]
        return self

    def reduce(self, fn, initial=None):
        if isinstance(self.data, list):
            acc = initial
            for x in self.data:
                acc = fn(acc, x)
            self.data = acc
        return self

    def keys(self):
        return list(self.data.keys()) if isinstance(self.data, dict) else []

    def values(self):
        return list(self.data.values()) if isinstance(self.data, dict) else []

    def entries(self):
        return [list(pair) for pair in self.data.items()] if isinstance(self.data, dict) else []

    def assign(self, obj):
        if isinstance(self.data, dict):
            self.data = {**self.data, **obj}
        return self

    def omit(self, keys):
        if isinstance(self.data, dict):
            self.data = {k: v for k, v in self.data.items() if k not in keys}
        return self

    def pick(self, keys):
        if isinstance(self.data, dict):
            self.data = {k: self.data[k] for k in keys if k in self.data}
        return self


async def _load_database_task():
    try:
        await database.read()
    except Exception as err:
        print(err, file=sys.stderr)
    database.data = {'users': {}, 'chats': {}, 'stats': {}, 'msgs': {}, 'settings': {}, 'agents': {}, **(database.data or {})}
    database.chain = Chain(database.data)
    return database.data


async def load_database():
    if database.data is not None:
        return database.data
    if database._loading is None:
        database._loading = asyncio.ensure_future(_load_database_task())
    return await database._loading


def _ensure_defaults(obj, defaults, numeric_keys):
    for key in defaults:
        is_missing = (not _is_number(obj.get(key))) if key in numeric_keys else (key not in obj)
        if is_missing:
            obj[key] = copy.deepcopy(defaults[key])
    return obj


def ensure_user_defaults(jid, extra=None):
    if database.data is None:
        raise RuntimeError('ensureUserDefaults called before loadDatabase() resolved')
    users = database.data['users']
    if not isinstance(users.get(jid), dict):
        users[jid] = {}
    user = users[jid]
    _ensure_defaults(user, DEFAULT['user'], DEFAULT_USER_NUMERIC_KEYS)
    if not user.get('registered'):
        _ensure_defaults(user, DEFAULT['userUnregistered'], DEFAULT_USER_UNREGISTERED_NUMERIC_KEYS)
    if isinstance(extra, dict):
        for key, value in extra.items():
            if value is not None and user.get(key) is None:
                user[key] = value
    return user


def ensure_chat_defaults(jid):
    if database.data is None:
        raise RuntimeError('ensureChatDefaults called before loadDatabase() resolved')
    chats = database.data['chats']
    if not isinstance(chats.get(jid), dict):
        chats[jid] = {}
    return _ensure_defaults(chats[jid], DEFAULT['chat'], DEFAULT_CHAT_NUMERIC_KEYS)


def ensure_settings_defaults(jid):
    if database.data is None:
        raise RuntimeError('ensureSettingsDefaults called before loadDatabase() resolved')
    settings = database.data['settings']
    if not isinstance(settings.get(jid), dict):
        settings[jid] = {}
    return _ensure_defaults(settings[jid], DEFAULT['settings'], DEFAULT_SETTINGS_NUMERIC_KEYS)


def get_user_auto_reconnect(jid, fallback=True):
    users = (database.data or {}).get('users') or {}
    user = users.get(jid) if isinstance(users, dict) else None
    if not isinstance(user, dict) or 'autoreconnect' not in user:
        return fallback
    return user['autoreconnect']


async def set_user_auto_reconnect(jid, value):
    ensure_user_defaults(jid)
    database.data['users'][jid]['autoreconnect'] = value
    try:
        await database.write()
    except Exception as err:
        print('[DB WRITE] setUserAutoReconnect failed to persist:', err, file=sys.stderr)


def get_agent_session(jid):
    if database.data is None:
        return None
    return (database.data.get('agents') or {}).get(jid) or None


async def set_agent_session(jid, session):
    if database.data is None:
        return
    if not database.data.get('agents'):
        database.data['agents'] = {}
    if session is None:
        database.data['agents'].pop(jid, None)
    else:
        database.data['agents'][jid] = {**(database.data['agents'].get(jid) or {}), **session}
    schedule_autosave()


async def delete_agent_session(jid):
    return await set_agent_session(jid, None)


db = database
G.db = database
