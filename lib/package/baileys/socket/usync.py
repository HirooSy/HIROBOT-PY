import math
from datetime import datetime, timezone

from baileys.binary.wa_binary import assertNodeErrorFree, getBinaryNodeChild, getBinaryNodeChildren, getBinaryNodeChildString, isTruthy, jsNumber
from baileys.foundation.node_runtime import JSObject


def _dateFromMs(ms):
    if ms is None or ms != ms or math.isinf(ms):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _textOf(content):
    if isinstance(content, (bytes, bytearray)):
        return bytes(content).decode('utf-8', 'replace')
    if isinstance(content, str):
        return content
    return None


def _orDefault(value, fallback):
    return value if isTruthy(value) else fallback


class USyncUser:
    def __init__(self):
        self.id = None
        self.lid = None
        self.phone = None
        self.username = None
        self.usernameKey = None
        self.type = None
        self.personaId = None

    def withId(self, id_):
        self.id = id_
        return self

    def withLid(self, lid):
        self.lid = lid
        return self

    def withPhone(self, phone):
        self.phone = phone
        return self

    def withUsername(self, username):
        self.username = username
        return self

    def withUsernameKey(self, usernameKey):
        self.usernameKey = usernameKey
        return self

    def withType(self, type_):
        self.type = type_
        return self

    def withPersonaId(self, personaId):
        self.personaId = personaId
        return self


class USyncContactProtocol:
    def __init__(self):
        self.name = 'contact'

    def getQueryElement(self):
        return JSObject({'tag': 'contact', 'attrs': JSObject({})})

    def getUserElement(self, user):
        if isTruthy(user.phone):
            return JSObject({'tag': 'contact', 'attrs': JSObject({}), 'content': user.phone})
        if isTruthy(user.username):
            attrs = JSObject({'username': user.username})
            if isTruthy(user.usernameKey):
                attrs['pin'] = user.usernameKey
            if isTruthy(user.lid):
                attrs['lid'] = user.lid
            return JSObject({'tag': 'contact', 'attrs': attrs})
        if isTruthy(user.type):
            return JSObject({'tag': 'contact', 'attrs': JSObject({'type': user.type})})
        return JSObject({'tag': 'contact', 'attrs': JSObject({})})

    def parser(self, node):
        if node.get('tag') == 'contact':
            assertNodeErrorFree(node)
            return node.get('attrs').get('type') == 'in'
        return False


class USyncDeviceProtocol:
    def __init__(self):
        self.name = 'devices'

    def getQueryElement(self):
        return JSObject({'tag': 'devices', 'attrs': JSObject({'version': '2'})})

    def getUserElement(self, user=None):
        return None

    def parser(self, node):
        deviceList = []
        keyIndex = None
        if node.get('tag') == 'devices':
            assertNodeErrorFree(node)
            deviceListNode = getBinaryNodeChild(node, 'device-list')
            keyIndexNode = getBinaryNodeChild(node, 'key-index-list')
            if deviceListNode is not None and isinstance(deviceListNode.get('content'), list):
                for child in deviceListNode.get('content'):
                    tag = child.get('tag')
                    attrs = child.get('attrs')
                    id_ = jsNumber(attrs.get('id'))
                    ki = jsNumber(attrs.get('key-index'))
                    if tag == 'device':
                        deviceList.append(JSObject({'id': id_, 'keyIndex': ki, 'isHosted': bool(attrs.get('is_hosted') and attrs.get('is_hosted') == 'true')}))
            if keyIndexNode is not None and keyIndexNode.get('tag') == 'key-index-list':
                keyIndex = JSObject({
                    'timestamp': jsNumber(keyIndexNode.get('attrs').get('ts')),
                    'signedKeyIndex': keyIndexNode.get('content'),
                    'expectedTimestamp': jsNumber(keyIndexNode.get('attrs').get('expected_ts')) if isTruthy(keyIndexNode.get('attrs').get('expected_ts')) else None
                })
        return JSObject({'deviceList': deviceList, 'keyIndex': keyIndex})


class USyncDisappearingModeProtocol:
    def __init__(self):
        self.name = 'disappearing_mode'

    def getQueryElement(self):
        return JSObject({'tag': 'disappearing_mode', 'attrs': JSObject({})})

    def getUserElement(self, user=None):
        return None

    def parser(self, node):
        if node.get('tag') == 'disappearing_mode':
            assertNodeErrorFree(node)
            duration = jsNumber(node.get('attrs').get('duration'))
            setAt = _dateFromMs(jsNumber(_orDefault(node.get('attrs').get('t'), 0)) * 1000)
            return JSObject({'duration': duration, 'setAt': setAt})
        return None


class USyncStatusProtocol:
    def __init__(self):
        self.name = 'status'

    def getQueryElement(self):
        return JSObject({'tag': 'status', 'attrs': JSObject({})})

    def getUserElement(self, user=None):
        return None

    def parser(self, node):
        if node.get('tag') == 'status':
            assertNodeErrorFree(node)
            status = _textOf(node.get('content'))
            setAt = _dateFromMs(jsNumber(_orDefault(node.get('attrs').get('t'), 0)) * 1000)
            attrs = node.get('attrs')
            if not isTruthy(status):
                status = '' if (isTruthy(attrs.get('code')) and jsNumber(attrs.get('code')) == 401) else None
            elif isinstance(status, str) and len(status) == 0:
                status = None
            return JSObject({'status': status, 'setAt': setAt})
        return None


class USyncUsernameProtocol:
    def __init__(self):
        self.name = 'username'

    def getQueryElement(self):
        return JSObject({'tag': 'username', 'attrs': JSObject({})})

    def getUserElement(self, user=None):
        return None

    def parser(self, node):
        if node.get('tag') == 'username':
            assertNodeErrorFree(node)
            return node.get('content') if isinstance(node.get('content'), str) else None
        return None


class USyncBotProfileProtocol:
    def __init__(self):
        self.name = 'bot'

    def getQueryElement(self):
        return JSObject({'tag': 'bot', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'profile', 'attrs': JSObject({'v': '1'})})]})

    def getUserElement(self, user):
        return JSObject({'tag': 'bot', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'profile', 'attrs': JSObject({'persona_id': user.personaId})})]})

    def parser(self, node):
        botNode = getBinaryNodeChild(node, 'bot')
        profile = getBinaryNodeChild(botNode, 'profile')
        commandsNode = getBinaryNodeChild(profile, 'commands')
        promptsNode = getBinaryNodeChild(profile, 'prompts')
        commands = []
        prompts = []
        for command in getBinaryNodeChildren(commandsNode, 'command'):
            commands.append(JSObject({'name': getBinaryNodeChildString(command, 'name'), 'description': getBinaryNodeChildString(command, 'description')}))
        for prompt in getBinaryNodeChildren(promptsNode, 'prompt'):
            prompts.append(f"{getBinaryNodeChildString(prompt, 'emoji')} {getBinaryNodeChildString(prompt, 'text')}")
        return JSObject({
            'isDefault': bool(getBinaryNodeChild(profile, 'default')),
            'jid': node.get('attrs').get('jid'),
            'name': getBinaryNodeChildString(profile, 'name'),
            'attributes': getBinaryNodeChildString(profile, 'attributes'),
            'description': getBinaryNodeChildString(profile, 'description'),
            'category': getBinaryNodeChildString(profile, 'category'),
            'personaId': profile.get('attrs').get('persona_id'),
            'commandsDescription': getBinaryNodeChildString(commandsNode, 'description'),
            'commands': commands,
            'prompts': prompts
        })


class USyncLIDProtocol:
    def __init__(self):
        self.name = 'lid'

    def getQueryElement(self):
        return JSObject({'tag': 'lid', 'attrs': JSObject({})})

    def getUserElement(self, user):
        return JSObject({'tag': 'lid', 'attrs': JSObject({'jid': user.lid})}) if isTruthy(user.lid) else None

    def parser(self, node):
        return node.get('attrs').get('val') if node.get('tag') == 'lid' else None


class USyncQuery:
    def __init__(self):
        self.protocols = []
        self.users = []
        self.context = 'interactive'
        self.mode = 'query'

    def withMode(self, mode):
        self.mode = mode
        return self

    def withContext(self, context):
        self.context = context
        return self

    def withUser(self, user):
        self.users.append(user)
        return self

    def parseUSyncQueryResult(self, result):
        if result.get('attrs').get('type') != 'result':
            return None
        protocolMap = {}
        for protocol in self.protocols:
            protocolMap[protocol.name] = protocol.parser
        queryResult = JSObject({'list': [], 'sideList': []})
        usyncNode = getBinaryNodeChild(result, 'usync')
        listNode = getBinaryNodeChild(usyncNode, 'list') if usyncNode is not None else None
        if listNode is not None and isinstance(listNode.get('content'), list):
            entries = []
            for node in listNode.get('content'):
                id_ = node.get('attrs').get('jid')
                if isTruthy(id_):
                    data = JSObject({})
                    if isinstance(node.get('content'), list):
                        for content in node.get('content'):
                            protocol = content.get('tag')
                            parser = protocolMap.get(protocol)
                            if parser is not None:
                                data[protocol] = parser(content)
                    entries.append(JSObject({**data, 'id': id_}))
            queryResult['list'] = entries
        return queryResult

    def withDeviceProtocol(self):
        self.protocols.append(USyncDeviceProtocol())
        return self

    def withContactProtocol(self):
        self.protocols.append(USyncContactProtocol())
        return self

    def withStatusProtocol(self):
        self.protocols.append(USyncStatusProtocol())
        return self

    def withDisappearingModeProtocol(self):
        self.protocols.append(USyncDisappearingModeProtocol())
        return self

    def withBotProfileProtocol(self):
        self.protocols.append(USyncBotProfileProtocol())
        return self

    def withLIDProtocol(self):
        self.protocols.append(USyncLIDProtocol())
        return self

    def withUsernameProtocol(self):
        self.protocols.append(USyncUsernameProtocol())
        return self
