import asyncio
import base64
import inspect
import json
from baileys.foundation.node_runtime import jsonDefault
import math
import os
import re
import traceback
import urllib.error
import urllib.request
from datetime import datetime

from baileys.binary.wa_binary import isJidGroup, isJidNewsletter, isJidStatusBroadcast, jidNormalizedUser
from baileys.constants import AssociationType, ButtonHeaderType, ButtonType, CarouselCardType, CodeHighlightType, ListType, RichSubMessageType, WAMessageStatus
from baileys.foundation.ai_rich import AIRichBuilder, aiRichBuilderFromSpec
from baileys.foundation.boom import Boom
from baileys.foundation.long import jsNumber, jsParseInt
from baileys.foundation.node_crypto import randomBytes, randomUUID
from baileys.foundation.node_runtime import JSError, JSObject, now
from baileys.foundation.zip import zip as zipFiles
from baileys.utils.media import (
    cropRgba,
    decodeRaster,
    downloadContentFromMessage,
    encodeJpeg,
    encryptedStream,
    generateThumbnail,
    getAudioDuration,
    getAudioWaveform,
    getRawMediaUploadData,
    getStream,
    resizeRgba,
    toBuffer
)
from baileys.utils.wa_protocol_core import generateMessageIDV2, getKeyAuthor, sha256, shouldIncludeReportingToken, unixTimestampSeconds
from baileys.WAProto import proto


LIBRARY_NAME = 'Baileys'
WA_DEFAULT_EPHEMERAL = 7 * 24 * 60 * 60
CALL_AUDIO_PREFIX = 'https://call.whatsapp.com/voice/'
CALL_VIDEO_PREFIX = 'https://call.whatsapp.com/video/'
MEDIA_KEYS = ['image', 'video', 'audio', 'sticker', 'document']
URL_REGEX = re.compile(r'https://(?![^:@/\s]+:[^:@/\s]+@)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(:\d+)?(/[^\s]*)?')
LEXER_REGEX = re.compile(
    r'(//.*|/\*[\s\S]*?\*/|#.*)|("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[\s\S]*?`)|(\b[a-zA-Z_]\w*\b)(?=\s*\()|(\b[a-zA-Z_]\w*\b)|(\b\d+(?:\.\d+)?\b)|(\s+|[^\w\s]+)',
    re.ASCII
)
CONCURRENCY_LIMIT = 15
MIMETYPE_MAP = {
    'image': 'image/jpeg',
    'video': 'video/mp4',
    'document': 'application/pdf',
    'audio': 'audio/ogg; codecs=opus',
    'sticker': 'image/webp',
    'product-catalog-image': 'image/jpeg'
}
MESSAGE_TYPE_PROTO = {
    'image': proto.Message.ImageMessage,
    'video': proto.Message.VideoMessage,
    'audio': proto.Message.AudioMessage,
    'sticker': proto.Message.StickerMessage,
    'document': proto.Message.DocumentMessage
}

CPP_KEYWORDS = {
    'alignas', 'alignof', 'and', 'and_eq', 'asm', 'auto', 'bitand', 'bitor', 'bool', 'break', 'case',
    'catch', 'char', 'class', 'compl', 'concept', 'const', 'consteval', 'constexpr', 'constinit',
    'const_cast', 'continue', 'co_await', 'co_return', 'co_yield', 'decltype', 'default', 'delete',
    'do', 'double', 'dynamic_cast', 'else', 'enum', 'explicit', 'export', 'extern', 'false', 'float',
    'for', 'friend', 'goto', 'if', 'inline', 'int', 'long', 'mutable', 'namespace', 'new', 'noexcept',
    'not', 'not_eq', 'nullptr', 'operator', 'or', 'or_eq', 'private', 'protected', 'public', 'register',
    'reinterpret_cast', 'requires', 'return', 'short', 'signed', 'sizeof', 'static', 'static_assert',
    'static_cast', 'struct', 'switch', 'template', 'this', 'thread_local', 'throw', 'true', 'try',
    'typedef', 'typeid', 'typename', 'union', 'unsigned', 'using', 'virtual', 'void', 'volatile',
    'wchar_t', 'while', 'xor', 'xor_eq'
}
CSS_KEYWORDS = {
    'import', 'media', 'font-face', 'keyframes', 'supports', 'charset',
    'important', 'root', 'hover', 'active', 'focus', 'visited', 'before', 'after',
    'not', 'nth-child', 'first-child', 'last-child', 'only-child',
    'none', 'inherit', 'initial', 'unset', 'auto', 'transparent', 'currentcolor'
}
GO_KEYWORDS = {
    'break', 'default', 'func', 'interface', 'select', 'case', 'defer', 'go', 'map', 'struct',
    'chan', 'else', 'goto', 'package', 'switch', 'const', 'fallthrough', 'if', 'range', 'type',
    'continue', 'for', 'import', 'return', 'var', 'true', 'false', 'nil'
}
HTML_KEYWORDS = {
    'html', 'head', 'body', 'title', 'meta', 'link', 'script', 'style',
    'header', 'footer', 'main', 'section', 'article', 'aside', 'nav',
    'div', 'span', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'a', 'img',
    'ul', 'ol', 'li', 'table', 'tr', 'td', 'th', 'thead', 'tbody',
    'form', 'input', 'button', 'select', 'textarea', 'label', 'option',
    'canvas', 'svg', 'iframe', 'video', 'audio', 'source'
}
JS_KEYWORDS = {
    'import', 'export', 'from', 'default', 'as',
    'const', 'let', 'var', 'function', 'class', 'extends', 'new',
    'return', 'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'break', 'continue',
    'try', 'catch', 'finally', 'throw',
    'async', 'await', 'yield',
    'typeof', 'instanceof', 'in', 'of', 'delete', 'void',
    'true', 'false', 'null', 'undefined', 'NaN', 'Infinity',
    'this', 'super', 'static', 'get', 'set',
    'debugger', 'with'
}
PYTHON_KEYWORDS = {
    'import', 'from', 'as', 'def', 'class', 'return', 'if', 'elif', 'else',
    'for', 'while', 'break', 'continue', 'try', 'except', 'finally', 'raise',
    'with', 'yield', 'lambda', 'pass', 'del', 'global', 'nonlocal', 'assert',
    'True', 'False', 'None', 'and', 'or', 'not', 'in', 'is', 'async', 'await',
    'self', 'print'
}
RUST_KEYWORDS = {
    'as', 'break', 'const', 'continue', 'crate', 'else', 'enum', 'extern',
    'false', 'fn', 'for', 'if', 'impl', 'in', 'let', 'loop', 'match',
    'mod', 'move', 'mut', 'pub', 'ref', 'return', 'self', 'Self',
    'static', 'struct', 'super', 'trait', 'true', 'type', 'unsafe',
    'use', 'where', 'while', 'async', 'await', 'dyn',
    'abstract', 'become', 'box', 'do', 'final', 'macro',
    'override', 'priv', 'typeof', 'unsized', 'virtual', 'yield',
    'try'
}
C_KEYWORDS = {
    'auto', 'break', 'case', 'char', 'const', 'continue',
    'default', 'do', 'double', 'else', 'enum', 'extern',
    'float', 'for', 'goto', 'if', 'inline', 'int',
    'long', 'register', 'restrict', 'return', 'short',
    'signed', 'sizeof', 'static', 'struct', 'switch',
    'typedef', 'union', 'unsigned', 'void', 'volatile',
    'while',
    '_Alignas', '_Alignof', '_Atomic', '_Bool',
    '_Complex', '_Generic', '_Imaginary',
    '_Noreturn', '_Static_assert', '_Thread_local'
}
CSHARP_KEYWORDS = {
    'abstract', 'as', 'base', 'bool', 'break', 'byte',
    'case', 'catch', 'char', 'checked', 'class',
    'const', 'continue', 'decimal', 'default',
    'delegate', 'do', 'double', 'else', 'enum',
    'event', 'explicit', 'extern', 'false', 'finally',
    'fixed', 'float', 'for', 'foreach', 'goto',
    'if', 'implicit', 'in', 'int', 'interface',
    'internal', 'is', 'lock', 'long', 'namespace',
    'new', 'null', 'object', 'operator', 'out',
    'override', 'params', 'private', 'protected',
    'public', 'readonly', 'ref', 'return', 'sbyte',
    'sealed', 'short', 'sizeof', 'stackalloc',
    'static', 'string', 'struct', 'switch',
    'this', 'throw', 'true', 'try', 'typeof',
    'uint', 'ulong', 'unchecked', 'unsafe',
    'ushort', 'using', 'virtual', 'void',
    'volatile', 'while',
    'async', 'await', 'record', 'init',
    'required', 'file', 'global', 'nameof',
    'var', 'dynamic', 'partial', 'yield',
    'from', 'where', 'select', 'group',
    'orderby', 'join', 'let', 'into',
    'equals', 'by', 'ascending', 'descending'
}
BASH_KEYWORDS = {
    'if', 'then', 'else', 'elif', 'fi',
    'case', 'esac', 'for', 'while',
    'until', 'do', 'done', 'in',
    'function', 'select', 'time',
    'coproc',
    'echo', 'printf', 'read', 'cd',
    'pwd', 'exit', 'export', 'unset',
    'alias', 'unalias', 'source',
    'exec', 'eval', 'test', 'shift',
    'trap', 'wait', 'jobs', 'kill',
    'bg', 'fg', 'history', 'type',
    'ulimit', 'umask', 'set',
    'true', 'false'
}
CMD_KEYWORDS = {
    'echo', 'set', 'if', 'else',
    'for', 'in', 'do', 'goto',
    'call', 'exit', 'shift',
    'pause', 'start', 'title',
    'cls', 'rem',
    'dir', 'copy', 'move', 'del',
    'mkdir', 'rmdir', 'type',
    'ren', 'tasklist', 'taskkill',
    'ping', 'ipconfig', 'netstat',
    'shutdown'
}
POWERSHELL_KEYWORDS = {
    'function', 'filter', 'param',
    'begin', 'process', 'end',
    'if', 'else', 'elseif',
    'switch', 'foreach', 'for',
    'while', 'do', 'until',
    'break', 'continue', 'return',
    'throw', 'trap', 'try',
    'catch', 'finally',
    '$true', '$false', '$null',
    'Write-Host', 'Write-Output',
    'Get-Item', 'Set-Item',
    'Get-ChildItem', 'Remove-Item',
    'Copy-Item', 'Move-Item',
    'Test-Path', 'Invoke-Command'
}
LANGUAGE_KEYWORDS = {
    'css': CSS_KEYWORDS,
    'html': HTML_KEYWORDS,
    'javascript': JS_KEYWORDS,
    'typescript': JS_KEYWORDS,
    'js': JS_KEYWORDS,
    'ts': JS_KEYWORDS,
    'python': PYTHON_KEYWORDS,
    'py': PYTHON_KEYWORDS,
    'go': GO_KEYWORDS,
    'golang': GO_KEYWORDS,
    'cpp': CPP_KEYWORDS,
    'c++': CPP_KEYWORDS,
    'rust': RUST_KEYWORDS,
    'rs': RUST_KEYWORDS,
    'c': C_KEYWORDS,
    'h': C_KEYWORDS,
    'csharp': CSHARP_KEYWORDS,
    'cs': CSHARP_KEYWORDS,
    'bash': BASH_KEYWORDS,
    'sh': BASH_KEYWORDS,
    'zsh': BASH_KEYWORDS,
    'cmd': CMD_KEYWORDS,
    'bat': CMD_KEYWORDS,
    'powershell': POWERSHELL_KEYWORDS,
    'ps1': POWERSHELL_KEYWORDS
}
NOOP_KEYWORDS = set()
CODE_HIGHLIGHT_NAME = ['DEFAULT', 'KEYWORD', 'METHOD', 'STR', 'NUMBER', 'COMMENT']
REUPLOAD_REQUIRED_STATUS = [410, 404]
WIDGET_ICON_ASCII = {
    'accountCircle': '[user]', 'add': '[+]', 'arrowBack': '[<]', 'arrowForward': '[>]', 'attachFile': '[attach]',
    'calendarToday': '[date]', 'call': '[call]', 'camera': '[camera]', 'check': '[ok]', 'close': '[x]', 'delete': '[delete]',
    'download': '[download]', 'edit': '[edit]', 'event': '[event]', 'error': '[!]', 'fastForward': '[>>]', 'favorite': '[fav]',
    'favoriteOff': '[fav]', 'folder': '[folder]', 'help': '[?]', 'home': '[home]', 'info': '[i]', 'locationOn': '[location]',
    'lock': '[lock]', 'lockOpen': '[unlock]', 'mail': '[mail]', 'menu': '[menu]', 'moreVert': '[more]', 'moreHoriz': '[more]',
    'notificationsOff': '[muted]', 'notifications': '[alert]', 'pause': '[pause]', 'payment': '[pay]', 'person': '[user]',
    'phone': '[phone]', 'photo': '[photo]', 'play': '[play]', 'print': '[print]', 'refresh': '[refresh]', 'rewind': '[<<]',
    'search': '[search]', 'send': '[send]', 'settings': '[settings]', 'share': '[share]', 'shoppingCart': '[cart]',
    'skipNext': '[next]', 'skipPrevious': '[prev]', 'star': '[star]', 'starHalf': '[star]', 'starOff': '[star]',
    'stop': '[stop]', 'upload': '[upload]', 'visibility': '[show]', 'visibilityOff': '[hide]', 'volumeDown': '[vol-]',
    'volumeMute': '[mute]', 'volumeOff': '[mute]', 'volumeUp': '[vol+]', 'warning': '[!]'
}
WIDGET_ICON_SYMBOL = {
    'accountCircle': '\u263a', 'add': '+', 'arrowBack': '<', 'arrowForward': '>', 'attachFile': '@',
    'calendarToday': '#', 'call': '\u260e', 'camera': '\u25c9', 'check': '\u2713', 'close': '\u2715', 'delete': '\u2715',
    'download': '\u2193', 'edit': '\u270e', 'event': '#', 'error': '!', 'fastForward': '\u00bb', 'favorite': '\u2665',
    'favoriteOff': '\u2661', 'folder': '\u25a4', 'help': '?', 'home': '\u2302', 'info': 'i', 'locationOn': '\u2691',
    'lock': '\u26bf', 'lockOpen': '\u26bf', 'mail': '\u2709', 'menu': '\u2261', 'moreVert': '\u22ee', 'moreHoriz': '\u2026',
    'notificationsOff': '\u2307', 'notifications': '\u260f', 'pause': '\u2016', 'payment': '$', 'person': '\u263a',
    'phone': '\u260e', 'photo': '\u25a8', 'play': '\u25b6', 'print': '\u2399', 'refresh': '\u21bb', 'rewind': '\u00ab',
    'search': '\u26b2', 'send': '\u27a4', 'settings': '\u2699', 'share': '\u2934', 'shoppingCart': '\u26c1',
    'skipNext': '\u00bb', 'skipPrevious': '\u00ab', 'star': '\u2605', 'starHalf': '\u2605', 'starOff': '\u2606',
    'stop': '\u25a0', 'upload': '\u2191', 'visibility': '\u25c9', 'visibilityOff': '\u25ce', 'volumeDown': '\u25d1',
    'volumeMute': '\u25cc', 'volumeOff': '\u25cc', 'volumeUp': '\u25d5', 'warning': '!'
}
DEVICE_IOS_RE = re.compile(r'3A.{18}')
DEVICE_WEB_RE = re.compile(r'3E.{20}')
DEVICE_ANDROID_RE = re.compile(r'.{21}|.{32}')
DEVICE_DESKTOP_PREFIX_RE = re.compile(r'3F')
DEVICE_DESKTOP_RE = re.compile(r'.{18}')


def truthy(value):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0 and value == value
    if isinstance(value, str):
        return value != ''
    return True


def jsOr(*values):
    result = None
    for value in values:
        if truthy(value):
            return value
        result = value
    return result


def nullish(value, fallback):
    return value if value is not None else fallback


def getp(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return obj[key]
    except Exception:
        pass
    try:
        return getattr(obj, key, None)
    except Exception:
        return None


def isBuffer(value):
    return isinstance(value, (bytes, bytearray))


def bufToStr(value):
    if isBuffer(value):
        return bytes(value).decode('utf-8', 'replace')
    return jsString(value)


def jsString(value):
    if value is None:
        return 'undefined'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isBuffer(value):
        return bytes(value).decode('utf-8', 'replace')
    if isinstance(value, (list, tuple)):
        return ','.join('' if item is None else jsString(item) for item in value)
    return '[object Object]'


def jsList(value):
    if isinstance(value, list):
        return value
    raise TypeError('value is not an array')


def jsonPrepare(value):
    if isinstance(value, dict):
        return {key: jsonPrepare(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [jsonPrepare(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def jsonStringify(value):
    return json.dumps(jsonPrepare(value), separators=(',', ':'), ensure_ascii=False, default=jsonDefault)


def errorStack(error):
    return ''.join(traceback.format_exception(type(error), error, error.__traceback__))


def httpBoom(message, statusCode=400, data=None):
    props = JSObject({'statusCode': statusCode})
    if data is not None:
        props['data'] = data
    return Boom(message, props)


def logCall(logger, name, *args):
    if logger is None:
        return
    method = getattr(logger, name, None)
    if method is not None:
        method(*args)


async def maybeAwait(value):
    if inspect.isawaitable(value):
        return await value
    return value


def dateToMs(value):
    if isinstance(value, datetime):
        return round(value.timestamp() * 1000)
    getTime = getattr(value, 'getTime', None)
    if callable(getTime):
        return getTime()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    raise TypeError('getTime is not a function')


def keyAuthor(key, meId=None):
    if meId is None:
        return getKeyAuthor(key)
    return getKeyAuthor(key, meId)


def codeHighlightName(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(CODE_HIGHLIGHT_NAME):
        return CODE_HIGHLIGHT_NAME[value]
    return 'DEFAULT'


def tokenizeCode(code, language='javascript'):
    keywords = LANGUAGE_KEYWORDS.get(language) or NOOP_KEYWORDS
    blocks = []
    for match in LEXER_REGEX.finditer(jsString(code)):
        if match.group(1):
            blocks.append(JSObject({'highlightType': CodeHighlightType['COMMENT'], 'codeContent': match.group(1)}))
        elif match.group(2):
            blocks.append(JSObject({'highlightType': CodeHighlightType['STRING'], 'codeContent': match.group(2)}))
        elif match.group(3):
            highlight = CodeHighlightType['KEYWORD'] if match.group(3) in keywords else CodeHighlightType['METHOD']
            blocks.append(JSObject({'highlightType': highlight, 'codeContent': match.group(3)}))
        elif match.group(4):
            highlight = CodeHighlightType['KEYWORD'] if match.group(4) in keywords else CodeHighlightType['DEFAULT']
            blocks.append(JSObject({'highlightType': highlight, 'codeContent': match.group(4)}))
        elif match.group(5):
            blocks.append(JSObject({'highlightType': CodeHighlightType['NUMBER'], 'codeContent': match.group(5)}))
        else:
            blocks.append(JSObject({'highlightType': CodeHighlightType['DEFAULT'], 'codeContent': match.group(6)}))
    return blocks


def viewModel(primitive):
    return JSObject({
        'view_model': JSObject({
            'primitive': primitive,
            '__typename': 'GenAISingleLayoutViewModel'
        })
    })


def unifiedSection(submessage):
    messageType = getp(submessage, 'messageType')
    if messageType == RichSubMessageType['CODE']:
        codeMetadata = getp(submessage, 'codeMetadata')
        blocks = [
            JSObject({'content': getp(block, 'codeContent'), 'type': codeHighlightName(getp(block, 'highlightType'))})
            for block in jsList(getp(codeMetadata, 'codeBlocks'))
        ]
        return viewModel(JSObject({
            'language': getp(codeMetadata, 'codeLanguage'),
            'code_blocks': blocks,
            '__typename': 'GenAICodeUXPrimitive'
        }))
    if messageType in (RichSubMessageType['CONTENT_ITEMS'], RichSubMessageType['INLINE_IMAGE'], RichSubMessageType['LATEX']):
        return JSObject()
    if messageType == RichSubMessageType['TABLE']:
        tableMetadata = getp(submessage, 'tableMetadata')
        rows = []
        for row in jsList(getp(tableMetadata, 'rows')):
            items = getp(row, 'items')
            rows.append(JSObject({
                'is_header': getp(row, 'isHeading'),
                'cells': items,
                'markdown_cells': [JSObject({'text': item}) for item in jsList(items)]
            }))
        return viewModel(JSObject({
            'title': getp(tableMetadata, 'title'),
            'rows': rows,
            '__typename': 'GenATableUXPrimitive'
        }))
    if messageType == RichSubMessageType['TEXT']:
        return viewModel(JSObject({
            'text': getp(submessage, 'messageText'),
            'inline_entities': jsOr(getp(submessage, 'inlineEntities'), []),
            '__typename': 'GenAIMarkdownTextUXPrimitive'
        }))
    return submessage


def toUnified(submessages, uuid=None):
    sections = [unifiedSection(submessage) for submessage in jsList(submessages)]
    return JSObject({'response_id': jsOr(uuid, randomUUID()), 'sections': sections})


def botMetadataSignature():
    return randomBytes(64)


def botMetadataCertificate(length=685):
    certificate = bytearray(length)
    certificate[0] = 48
    certificate[1] = 130
    certificate[2:] = randomBytes(length - 2)
    return bytes(certificate)


def wrapToBotForwardedMessage(richResponseMessage):
    return JSObject({
        'messageContextInfo': JSObject({
            'botMetadata': JSObject({
                'verificationMetadata': JSObject({
                    'proofs': [JSObject({
                        'certificateChain': [botMetadataCertificate(), botMetadataCertificate(892)],
                        'version': 1,
                        'useCase': 1,
                        'signature': botMetadataSignature()
                    })]
                })
            })
        }),
        'botForwardedMessage': JSObject({'message': JSObject({'richResponseMessage': richResponseMessage})})
    })


def mapRichSubmessage(submessage):
    if truthy(getp(submessage, 'text')):
        return JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': getp(submessage, 'text'), 'inlineEntities': getp(submessage, 'inlineEntities')})
    if truthy(getp(submessage, 'code')):
        return JSObject({'messageType': RichSubMessageType['CODE'], 'codeMetadata': JSObject({'codeLanguage': getp(submessage, 'language'), 'codeBlocks': getp(submessage, 'code')})})
    if truthy(getp(submessage, 'items')):
        return JSObject({
            'messageType': RichSubMessageType['CONTENT_ITEMS'],
            'contentItemsMetadata': JSObject({'itemsMetadata': getp(submessage, 'items'), 'contentType': proto.AIRichResponseContentItemsMetadata.ContentType.CAROUSEL})
        })
    if truthy(getp(submessage, 'inlineImage')):
        return JSObject({
            'messageType': RichSubMessageType['INLINE_IMAGE'],
            'imageMetadata': JSObject({
                'imageUrl': getp(submessage, 'inlineImage'),
                'imageText': getp(submessage, 'imageText'),
                'alignment': getp(submessage, 'alignment'),
                'tapLinkUrl': getp(submessage, 'tapLinkUrl')
            })
        })
    if truthy(getp(submessage, 'inlineVideo')):
        return JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': 'INLINE_VIDEO'})
    if truthy(getp(submessage, 'latex')):
        return JSObject({'messageType': RichSubMessageType['LATEX'], 'latexMetadata': JSObject({'text': getp(submessage, 'text'), 'expressions': getp(submessage, 'latex')})})
    if truthy(getp(submessage, 'posts')):
        return JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': 'POSTS'})
    if truthy(getp(submessage, 'products')):
        return JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': 'PRODUCTS'})
    if truthy(getp(submessage, 'suggested')):
        return JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': 'SUGGESTED_PROMPT'})
    if truthy(getp(submessage, 'table')):
        return JSObject({'messageType': RichSubMessageType['TABLE'], 'tableMetadata': JSObject({'title': getp(submessage, 'title'), 'rows': getp(submessage, 'table')})})
    return submessage


def linkSubmessages(links):
    submessages = []
    for index, linkField in enumerate(jsList(links)):
        prefix = 'SS_' + str(index)
        url = getp(linkField, 'url')
        sourceList = getp(linkField, 'sources')
        sources = None
        if sourceList is not None:
            sources = [
                JSObject({
                    'source_type': 'THIRD_PARTY',
                    'source_display_name': getp(sourceField, 'displayName'),
                    'source_subtitle': getp(sourceField, 'subtitle'),
                    'source_url': jsOr(getp(sourceField, 'url'), url)
                })
                for sourceField in jsList(sourceList)
            ]
        submessages.append(JSObject({
            'messageType': RichSubMessageType['TEXT'],
            'messageText': jsString(getp(linkField, 'text')) + ' {{' + prefix + '}}\u00b9{{/' + prefix + '}} ',
            'inlineEntities': [
                JSObject({
                    'key': prefix,
                    'metadata': JSObject({
                        'reference_id': index + 1,
                        'reference_url': url,
                        'reference_title': getp(linkField, 'title'),
                        'reference_display_name': getp(linkField, 'displayName'),
                        'sources': sources if sources is not None else [],
                        '__typename': 'GenAISearchCitationItem'
                    })
                })
            ]
        }))
    return submessages


def prepareRichResponseMessage(content):
    content = JSObject(content)
    submessages = []
    richResponse = content.get('richResponse')
    if isinstance(richResponse, list):
        submessages = [mapRichSubmessage(submessage) for submessage in richResponse]
    else:
        if truthy(content.get('headerText')):
            submessages.append(JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': content.get('headerText')}))
        if truthy(content.get('contentText')):
            submessages.append(JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': content.get('contentText')}))
        if truthy(content.get('code')):
            lang = jsOr(content.get('language'), 'javascript')
            submessages.append(JSObject({
                'messageType': RichSubMessageType['CODE'],
                'codeMetadata': JSObject({'codeLanguage': lang, 'codeBlocks': tokenizeCode(content.get('code'), lang)})
            }))
        if truthy(content.get('items')):
            submessages.append(JSObject({
                'messageType': RichSubMessageType['CONTENT_ITEMS'],
                'contentItemsMetadata': JSObject({'itemsMetadata': content.get('items'), 'contentType': proto.AIRichResponseContentItemsMetadata.ContentType.CAROUSEL})
            }))
        if truthy(content.get('inlineImage')):
            submessages.append(JSObject({
                'messageType': RichSubMessageType['INLINE_IMAGE'],
                'imageMetadata': JSObject({
                    'imageUrl': content.get('inlineImage'),
                    'imageText': content.get('imageText'),
                    'alignment': content.get('alignment'),
                    'tapLinkUrl': content.get('tapLinkUrl')
                })
            }))
        if truthy(content.get('inlineVideo')):
            submessages.append(JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': 'INLINE_VIDEO'}))
        if truthy(content.get('latex')):
            submessages.append(JSObject({
                'messageType': RichSubMessageType['LATEX'],
                'latexMetadata': JSObject({'text': content.get('text'), 'expressions': content.get('latex')})
            }))
        if truthy(content.get('links')):
            submessages.extend(linkSubmessages(content.get('links')))
        if truthy(content.get('posts')):
            submessages.append(JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': 'POSTS'}))
        if truthy(content.get('products')):
            submessages.append(JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': 'PRODUCTS'}))
        if truthy(content.get('suggested')):
            submessages.append(JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': 'SUGGESTED_PROMPT'}))
        if truthy(content.get('table')):
            noHeading = content.get('noHeading')
            rows = [
                JSObject({'isHeading': (not truthy(noHeading)) and index == 0, 'items': itemsRow})
                for index, itemsRow in enumerate(jsList(content.get('table')))
            ]
            submessages.append(JSObject({
                'messageType': RichSubMessageType['TABLE'],
                'tableMetadata': JSObject({'title': content.get('title'), 'rows': rows})
            }))
        if truthy(content.get('footerText')):
            submessages.append(JSObject({'messageType': RichSubMessageType['TEXT'], 'messageText': content.get('footerText')}))

    uuid = randomUUID()
    unified = toUnified(submessages, uuid)
    richResponseMessage = proto.AIRichResponseMessage.create(JSObject({
        'submessages': submessages,
        'messageType': proto.AIRichResponseMessageType.AI_RICH_RESPONSE_TYPE_STANDARD,
        'unifiedResponse': JSObject({'data': jsonStringify(unified).encode('utf-8')}),
        'contextInfo': JSObject({
            'isForwarded': True,
            'forwardingScore': 1,
            'forwardedAiBotMessageInfo': JSObject({'botJid': '867051314767696@bot'}),
            'forwardOrigin': 4
        })
    }))
    message = wrapToBotForwardedMessage(richResponseMessage)
    botMetadata = message.get('messageContextInfo').get('botMetadata')
    if truthy(content.get('disclaimerText')):
        botMetadata['messageDisclaimerText'] = content.get('disclaimerText')
    botMetadata['botResponseId'] = uuid
    return message


def extractUrlFromText(text):
    if not isinstance(text, str):
        raise TypeError('text.match is not a function')
    match = URL_REGEX.search(text)
    if match is None:
        return None
    return match.group(0)


async def generateLinkPreviewIfRequired(text, getUrlInfo, logger):
    url = extractUrlFromText(text)
    if getUrlInfo is not None and truthy(url):
        try:
            return await maybeAwait(getUrlInfo(url))
        except Exception as error:
            logCall(logger, 'warn', JSObject({'trace': errorStack(error)}), 'url generation failed')
    return None


async def assertColor(color):
    if isinstance(color, (int, float)) and not isinstance(color, bool):
        if color > 0:
            return color
        return 0xffffffff + jsNumber(color) + 1
    hexText = str(color).strip().replace('#', '', 1)
    if len(hexText) <= 6:
        hexText = 'FF' + hexText.rjust(6, '0')
    return jsParseInt(hexText, 16)


def toBase64(value):
    return base64.b64encode(bytes(value)).decode('ascii')


def upperOrNone(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value.upper()
    raise TypeError('toUpperCase is not a function')


def buildWidgetItem(item, itemId, components):
    item = JSObject() if item is None else item
    if truthy(getp(item, 'image')):
        entry = JSObject({
            'id': itemId,
            'component': 'Image',
            'url': getp(item, 'image'),
            'variant': jsOr(getp(item, 'variant'), 'header'),
            'fit': jsOr(getp(item, 'fit'), 'cover')
        })
        if truthy(getp(item, 'description')):
            entry['description'] = jsString(getp(item, 'description'))
        components.append(entry)
    elif truthy(getp(item, 'text')):
        components.append(JSObject({'id': itemId, 'component': 'Text', 'text': jsString(getp(item, 'text')), 'variant': jsOr(getp(item, 'variant'), 'body')}))
    elif truthy(getp(item, 'icon')) or truthy(getp(item, 'name')):
        iconName = jsOr(getp(item, 'icon'), getp(item, 'name'))
        if truthy(getp(item, 'native')):
            components.append(JSObject({'id': itemId, 'component': 'Icon', 'name': iconName}))
        else:
            style = getp(item, 'style')
            mapping = WIDGET_ICON_SYMBOL if style == 'symbol' else WIDGET_ICON_ASCII
            text = mapping.get(iconName) if isinstance(iconName, str) else None
            if not truthy(text):
                text = '•' if style == 'symbol' else '[' + jsString(iconName) + ']'
            components.append(JSObject({'id': itemId, 'component': 'Text', 'text': text, 'variant': jsOr(getp(item, 'variant'), 'body')}))
    elif truthy(getp(item, 'video')):
        components.append(JSObject({'id': itemId, 'component': 'Video', 'url': getp(item, 'video')}))
    elif truthy(getp(item, 'audio')):
        entry = JSObject({'id': itemId, 'component': 'AudioPlayer', 'url': getp(item, 'audio')})
        if truthy(getp(item, 'description')):
            entry['description'] = jsString(getp(item, 'description'))
        components.append(entry)
    elif truthy(getp(item, 'divider')):
        divider = getp(item, 'divider')
        axis = divider if isinstance(divider, str) else 'horizontal'
        components.append(JSObject({'id': itemId, 'component': 'Divider', 'axis': axis}))
    elif truthy(getp(item, 'button')):
        textId = itemId + '_text'
        components.append(JSObject({
            'id': itemId,
            'component': 'Button',
            'child': textId,
            'variant': jsOr(getp(item, 'variant'), 'primary'),
            'action': JSObject({'call': 'openUrl', 'args': JSObject({'url': jsString(jsOr(getp(item, 'url'), ''))})})
        }))
        components.append(JSObject({'id': textId, 'component': 'Text', 'text': jsString(getp(item, 'button')), 'variant': 'body'}))
    elif truthy(getp(item, 'input')):
        entry = JSObject({
            'id': itemId,
            'component': 'TextField',
            'label': jsString(jsOr(getp(item, 'label'), getp(item, 'input')))
        })
        if getp(item, 'value') is not None:
            entry['value'] = jsString(getp(item, 'value'))
        entry['variant'] = jsOr(getp(item, 'variant'), 'shortText')
        if truthy(getp(item, 'validationRegexp')):
            entry['validationRegexp'] = getp(item, 'validationRegexp')
        components.append(entry)
    elif truthy(getp(item, 'checkbox')):
        components.append(JSObject({'id': itemId, 'component': 'CheckBox', 'label': jsString(getp(item, 'checkbox')), 'value': truthy(getp(item, 'value'))}))
    elif truthy(getp(item, 'choice')):
        entry = JSObject({'id': itemId, 'component': 'ChoicePicker'})
        if truthy(getp(item, 'label')):
            entry['label'] = jsString(getp(item, 'label'))
        options = []
        for option in jsList(getp(item, 'choice')):
            if isinstance(option, str):
                options.append(JSObject({'label': option, 'value': option}))
            else:
                options.append(JSObject({'label': jsString(getp(option, 'label')), 'value': jsString(getp(option, 'value'))}))
        entry['options'] = options
        value = getp(item, 'value')
        if truthy(value):
            entry['value'] = [jsString(v) for v in value] if isinstance(value, list) else [jsString(value)]
        else:
            entry['value'] = []
        entry['variant'] = jsOr(getp(item, 'variant'), 'mutuallyExclusive')
        if truthy(getp(item, 'displayStyle')):
            entry['displayStyle'] = getp(item, 'displayStyle')
        if getp(item, 'filterable') is not None:
            entry['filterable'] = truthy(getp(item, 'filterable'))
        components.append(entry)
    elif getp(item, 'slider') is not None:
        entry = JSObject({'id': itemId, 'component': 'Slider'})
        if truthy(getp(item, 'label')):
            entry['label'] = jsString(getp(item, 'label'))
        entry['min'] = nullish(getp(item, 'min'), 0)
        entry['max'] = getp(item, 'slider')
        entry['value'] = nullish(getp(item, 'value'), nullish(getp(item, 'min'), 0))
        components.append(entry)
    elif truthy(getp(item, 'datetime')):
        entry = JSObject({
            'id': itemId,
            'component': 'DateTimeInput',
            'value': jsString(getp(item, 'value')) if getp(item, 'value') is not None else '',
            'enableDate': truthy(getp(item, 'enableDate')) if getp(item, 'enableDate') is not None else True,
            'enableTime': truthy(getp(item, 'enableTime'))
        })
        if truthy(getp(item, 'min')):
            entry['min'] = jsString(getp(item, 'min'))
        if truthy(getp(item, 'max')):
            entry['max'] = jsString(getp(item, 'max'))
        if truthy(getp(item, 'label')):
            entry['label'] = jsString(getp(item, 'label'))
        components.append(entry)
    elif truthy(getp(item, 'row')):
        children = [c for c in (buildWidgetItem(sub, itemId + '_' + str(j), components) for j, sub in enumerate(jsList(getp(item, 'row')))) if truthy(c)]
        components.append(JSObject({'id': itemId, 'component': 'Row', 'children': children, 'justify': jsOr(getp(item, 'justify'), 'start'), 'align': jsOr(getp(item, 'align'), 'stretch')}))
    elif truthy(getp(item, 'column')):
        children = [c for c in (buildWidgetItem(sub, itemId + '_' + str(j), components) for j, sub in enumerate(jsList(getp(item, 'column')))) if truthy(c)]
        components.append(JSObject({'id': itemId, 'component': 'Column', 'children': children, 'justify': jsOr(getp(item, 'justify'), 'start'), 'align': jsOr(getp(item, 'align'), 'stretch')}))
    elif truthy(getp(item, 'list')):
        children = [c for c in (buildWidgetItem(sub, itemId + '_' + str(j), components) for j, sub in enumerate(jsList(getp(item, 'list')))) if truthy(c)]
        components.append(JSObject({'id': itemId, 'component': 'List', 'children': children, 'direction': jsOr(getp(item, 'direction'), 'vertical'), 'align': jsOr(getp(item, 'align'), 'stretch')}))
    else:
        return None
    return itemId


def widgetItemFallback(item):
    item = JSObject() if item is None else item
    if truthy(getp(item, 'image')):
        return '[image]'
    if truthy(getp(item, 'text')):
        return jsString(getp(item, 'text'))
    if truthy(getp(item, 'icon')) or truthy(getp(item, 'name')):
        iconName = jsOr(getp(item, 'icon'), getp(item, 'name'))
        mapped = WIDGET_ICON_ASCII.get(iconName) if isinstance(iconName, str) else None
        return mapped if truthy(mapped) else '[' + jsString(iconName) + ']'
    if truthy(getp(item, 'video')):
        return '[video]'
    if truthy(getp(item, 'audio')):
        return '[audio]'
    if truthy(getp(item, 'button')):
        return jsString(getp(item, 'button')) + ': ' + jsString(jsOr(getp(item, 'url'), ''))
    if truthy(getp(item, 'input')):
        return jsString(jsOr(getp(item, 'label'), getp(item, 'input'))) + ': ' + jsString(nullish(getp(item, 'value'), ''))
    if truthy(getp(item, 'checkbox')):
        return '[ ] ' + jsString(getp(item, 'checkbox'))
    if truthy(getp(item, 'choice')):
        value = getp(item, 'value')
        values = value if isinstance(value, list) else [value]
        joined = ', '.join(jsString(v) for v in values if truthy(v))
        return (jsString(jsOr(getp(item, 'label'), '')) + ' ' + joined).strip()
    if getp(item, 'slider') is not None:
        shown = nullish(getp(item, 'value'), nullish(getp(item, 'min'), 0))
        return jsString(jsOr(getp(item, 'label'), 'slider')) + ': ' + jsString(shown) + '/' + jsString(getp(item, 'slider'))
    if truthy(getp(item, 'datetime')):
        return jsString(jsOr(getp(item, 'label'), 'date')) + ': ' + jsString(jsOr(getp(item, 'value'), ''))
    if truthy(getp(item, 'row')):
        return ' | '.join(text for text in (widgetItemFallback(sub) for sub in jsList(getp(item, 'row'))) if truthy(text))
    if truthy(jsOr(getp(item, 'column'), getp(item, 'list'))):
        source = jsOr(getp(item, 'column'), getp(item, 'list'))
        return '\n'.join(text for text in (widgetItemFallback(sub) for sub in jsList(source)) if truthy(text))
    return ''


def prepareBloksWidget(widget):
    widget = JSObject() if widget is None else widget
    align = nullish(getp(widget, 'align'), 'center')
    items = nullish(getp(widget, 'items'), [])
    fallback = getp(widget, 'fallback')
    uuid = randomUUID()
    components = []
    children = [c for c in (buildWidgetItem(item, 'item_' + str(i), components) for i, item in enumerate(jsList(items))) if truthy(c)]
    components.insert(0, JSObject({'id': 'root', 'component': 'Column', 'justify': 'center', 'align': align, 'children': children}))
    data = jsonStringify(JSObject({
        'version': 'v0.9',
        'createSurface': JSObject({
            'surfaceId': uuid,
            'catalogId': 'https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json',
            'components': components
        })
    }))
    fallbackText = '\n'.join(text for text in (widgetItemFallback(item) for item in jsList(items)) if truthy(text))
    return JSObject({
        'uuid': uuid,
        'data': data,
        'type': 'im_a2ui',
        'fallback': jsOr(fallback, fallbackText, 'Cannot load widget')
    })


def nativeFlowButton(button):
    buttonText = jsOr(getp(button, 'text'), getp(button, 'buttonText'))
    buttonIcon = upperOrNone(getp(button, 'icon'))
    if truthy(getp(button, 'id')):
        return JSObject({
            'name': 'quick_reply',
            'buttonParamsJson': jsonStringify(JSObject({'display_text': jsOr(buttonText, '\U0001f449\U0001f3fb Click'), 'id': getp(button, 'id'), 'icon': buttonIcon}))
        })
    if truthy(getp(button, 'copy')):
        return JSObject({
            'name': 'cta_copy',
            'buttonParamsJson': jsonStringify(JSObject({'display_text': jsOr(buttonText, '\U0001f4cb Copy'), 'copy_code': getp(button, 'copy'), 'icon': buttonIcon}))
        })
    if truthy(getp(button, 'url')):
        return JSObject({
            'name': 'cta_url',
            'buttonParamsJson': jsonStringify(JSObject({
                'display_text': jsOr(buttonText, '\U0001f310 Visit'),
                'url': getp(button, 'url'),
                'merchant_url': getp(button, 'url'),
                'webview_interaction': getp(button, 'useWebview'),
                'icon': buttonIcon
            }))
        })
    if truthy(getp(button, 'call')):
        return JSObject({
            'name': 'cta_call',
            'buttonParamsJson': jsonStringify(JSObject({'display_text': jsOr(buttonText, '\U0001f4de Call'), 'phone_number': getp(button, 'call'), 'icon': buttonIcon}))
        })
    if truthy(getp(button, 'sections')):
        return JSObject({
            'name': 'single_select',
            'buttonParamsJson': jsonStringify(JSObject({'title': jsOr(buttonText, '\U0001f4cb Select'), 'sections': getp(button, 'sections'), 'icon': buttonIcon}))
        })
    return button


def prepareNativeFlowButtons(message):
    buttons = getp(message, 'nativeFlow')
    correctedField = buttons if isinstance(buttons, list) else getp(buttons, 'buttons')
    messageParamsJson = JSObject()
    if truthy(getp(message, 'offerText')):
        limitedTimeOffer = JSObject({'text': jsOr(getp(message, 'offerText'), LIBRARY_NAME)})
        if truthy(getp(message, 'offerUrl')):
            limitedTimeOffer['url'] = getp(message, 'offerUrl')
        limitedTimeOffer['copy_code'] = getp(message, 'offerCode')
        limitedTimeOffer['expiration_time'] = getp(message, 'offerExpiration')
        messageParamsJson['limited_time_offer'] = limitedTimeOffer
    if truthy(getp(message, 'optionText')):
        bottomSheet = JSObject({'in_thread_buttons_limit': 1})
        bottomSheet['divider_indices'] = list(range(len(jsList(correctedField))))
        bottomSheet['list_title'] = jsOr(getp(message, 'optionTitle'), '\U0001f4c4 Select Options')
        bottomSheet['button_title'] = getp(message, 'optionText')
        messageParamsJson['bottom_sheet'] = bottomSheet
    buttonList = [nativeFlowButton(button) for button in jsList(correctedField)]
    return JSObject({'buttons': buttonList, 'messageParamsJson': jsonStringify(messageParamsJson)})


def prepareDisappearingMessageSettingContent(ephemeralExpiration):
    ephemeralExpiration = jsOr(ephemeralExpiration, 0)
    return proto.Message.fromObject(JSObject({
        'ephemeralMessage': JSObject({
            'message': JSObject({
                'protocolMessage': JSObject({
                    'type': proto.Message.ProtocolMessage.Type.EPHEMERAL_SETTING,
                    'ephemeralExpiration': ephemeralExpiration
                })
            })
        })
    }))


def isWebPBuffer(buffer):
    return len(buffer) >= 12 and bytes(buffer[0:4]) == b'RIFF' and bytes(buffer[8:12]) == b'WEBP'


def isAnimatedWebP(buffer):
    if not isWebPBuffer(buffer):
        return False
    offset = 12
    while offset < len(buffer) - 8:
        chunkFourCC = bytes(buffer[offset:offset + 4]).decode('latin-1')
        chunkSize = int.from_bytes(bytes(buffer[offset + 4:offset + 8]), 'little')
        if chunkFourCC == 'VP8X':
            flagsOffset = offset + 8
            if flagsOffset < len(buffer) and buffer[flagsOffset] & 0x02:
                return True
        elif chunkFourCC == 'ANIM' or chunkFourCC == 'ANMF':
            return True
        offset += 8 + chunkSize + (chunkSize % 2)
    return False


def convertToWebP(buffer):
    if isWebPBuffer(buffer):
        return bytes(buffer)
    raise JSError('Converting non-WebP images to WebP needs a native image library (sharp or @napi-rs/image), which is not available in pure Python')


def coverResizeJpeg(buffer, size, quality):
    width, height, data = decodeRaster(buffer)
    scale = max(size / width, size / height)
    scaledW = max(size, int(round(width * scale)))
    scaledH = max(size, int(round(height * scale)))
    scaled = resizeRgba(data, width, height, scaledW, scaledH)
    left = (scaledW - size) // 2
    top = (scaledH - size) // 2
    cropped = cropRgba(scaled, scaledW, left, top, size, size)
    return encodeJpeg(cropped, size, size, quality)


def zipToBuffer(files):
    outcome = {}

    def callback(error=None, data=None):
        outcome['error'] = error
        outcome['data'] = data
    zipFiles(files, callback)
    if outcome.get('error') is not None:
        raise outcome.get('error')
    return bytes(outcome.get('data'))


def tryUnlink(path, logger):
    try:
        os.remove(path)
    except Exception:
        logCall(logger, 'warn', 'failed to remove tmp file')


def fetchPlainBytesSync(url):
    try:
        with urllib.request.urlopen(str(url), timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError:
        return None


async def fetchPlainBytes(url):
    return await asyncio.to_thread(fetchPlainBytesSync, url)


async def prepareProductMessage(message, options):
    if not truthy(getp(message, 'businessOwnerJid')):
        raise httpBoom('"businessOwnerJid" is missing from the content')
    product = getp(message, 'product')
    if truthy(getp(message, 'image')):
        imageSource = getp(message, 'image')
    else:
        if product is None:
            raise TypeError("Cannot read properties of undefined (reading 'productImage')")
        imageSource = getp(product, 'productImage')
    prepared = await prepareWAMessageMedia(JSObject({'image': imageSource}), options)
    imageMessage = prepared.get('imageMessage')
    content = JSObject({key: value for key, value in message.items() if key != 'image'})
    productValue = JSObject({'currencyCode': 'IDR', 'priceAmount1000': 1000, 'title': LIBRARY_NAME})
    if isinstance(product, dict):
        productValue.update(product)
    productValue['productImage'] = imageMessage
    content['product'] = productValue
    return content


def stickerFileHash(webpBuffer):
    return toBase64(sha256(webpBuffer)).replace('/', '-')


async def prepareStickerPackMessage(message, options):
    cover = getp(message, 'cover')
    stickers = nullish(getp(message, 'stickers'), [])
    name = nullish(getp(message, 'name'), '\U0001f4e6 Sticker Pack')
    publisher = nullish(getp(message, 'publisher'), '')
    description = nullish(getp(message, 'description'), '')
    if len(stickers) > 60:
        raise httpBoom('Sticker pack exceeds the maximum limit of 60 stickers')
    if len(stickers) == 0:
        raise httpBoom('Sticker pack must contain at least one sticker')
    if not truthy(cover):
        raise httpBoom('Sticker pack must contain a cover')

    logger = getp(options, 'logger')
    mediaCache = getp(options, 'mediaCache')
    cacheableKey = False
    if isinstance(stickers, list) and len(stickers) and truthy(mediaCache):
        urls = []
        for sticker in stickers:
            data = getp(sticker, 'data')
            if isinstance(data, dict) and truthy(data.get('url')):
                urls.append(data.get('url'))
        if len(urls) > 0:
            cacheableKey = 'sticker:' + '@'.join(jsString(url) for url in urls)
    if truthy(cacheableKey):
        mediaBuff = await maybeAwait(mediaCache.get(cacheableKey))
        if mediaBuff is not None:
            logCall(logger, 'debug', JSObject({'cacheableKey': cacheableKey}), 'got media cache hit')
            return proto.Message.StickerPackMessage.decode(mediaBuff)

    stickerPackIdValue = jsString(generateMessageIDV2(None))
    stickerData = {}
    stickerMetadata = [None] * len(stickers)

    async def processSticker(index):
        sticker = stickers[index]
        streamInfo = await getStream(getp(sticker, 'data'))
        buffer = await toBuffer(streamInfo.get('stream'))
        if isWebPBuffer(buffer):
            webpBuffer = bytes(buffer)
            isAnimated = isAnimatedWebP(buffer)
        else:
            webpBuffer = convertToWebP(buffer)
            isAnimated = False
        if len(webpBuffer) > 1024 * 1024:
            raise httpBoom('Sticker at index ' + str(index) + ' exceeds the 1MB size limit')
        fileName = stickerFileHash(webpBuffer) + '.webp'
        stickerData[fileName] = [webpBuffer, JSObject({'level': 0})]
        stickerMetadata[index] = JSObject({
            'fileName': fileName,
            'mimetype': 'image/webp',
            'isAnimated': isAnimated,
            'emojis': jsOr(getp(sticker, 'emojis'), ['✨']),
            'accessibilityLabel': jsOr(getp(sticker, 'accessibilityLabel'), '‎')
        })

    for start in range(0, len(stickers), CONCURRENCY_LIMIT):
        chunkEnd = min(start + CONCURRENCY_LIMIT, len(stickers))
        await asyncio.gather(*[processSticker(index) for index in range(start, chunkEnd)])

    trayIconFileName = stickerPackIdValue + '.webp'
    coverStream = await getStream(cover)
    coverBuffer = await toBuffer(coverStream.get('stream'))
    coverWebpBuffer = bytes(coverBuffer) if isWebPBuffer(coverBuffer) else convertToWebP(coverBuffer)
    stickerData[trayIconFileName] = [coverWebpBuffer, JSObject({'level': 0})]
    zipBuffer = zipToBuffer(stickerData)

    stickerPackUpload = await encryptedStream(zipBuffer, 'sticker-pack', JSObject({'logger': logger, 'opts': getp(options, 'options')}))
    try:
        stickerPackUploadResult = await maybeAwait(options.get('upload')(stickerPackUpload.get('encFilePath'), JSObject({
            'fileEncSha256B64': toBase64(stickerPackUpload.get('fileEncSha256')),
            'mediaType': 'sticker-pack',
            'timeoutMs': getp(options, 'mediaUploadTimeoutMs')
        })))
    finally:
        tryUnlink(stickerPackUpload.get('encFilePath'), logger)

    obj = JSObject({
        'name': name,
        'publisher': publisher,
        'stickerPackId': stickerPackIdValue,
        'packDescription': description,
        'stickerPackOrigin': proto.Message.StickerPackMessage.StickerPackOrigin.USER_CREATED,
        'stickerPackSize': len(zipBuffer),
        'stickers': stickerMetadata,
        'fileSha256': stickerPackUpload.get('fileSha256'),
        'fileEncSha256': stickerPackUpload.get('fileEncSha256'),
        'mediaKey': stickerPackUpload.get('mediaKey'),
        'directPath': getp(stickerPackUploadResult, 'directPath'),
        'fileLength': stickerPackUpload.get('fileLength'),
        'mediaKeyTimestamp': unixTimestampSeconds(None),
        'trayIconFileName': trayIconFileName
    })

    try:
        thumbnailBuffer = coverResizeJpeg(coverBuffer, 252, 80)
        if not isBuffer(thumbnailBuffer) or len(thumbnailBuffer) == 0:
            raise JSError('Failed to generate thumbnail buffer')
        thumbUpload = await encryptedStream(thumbnailBuffer, 'thumbnail-sticker-pack', JSObject({
            'logger': logger,
            'opts': getp(options, 'options'),
            'mediaKey': stickerPackUpload.get('mediaKey')
        }))
        try:
            thumbUploadResult = await maybeAwait(options.get('upload')(thumbUpload.get('encFilePath'), JSObject({
                'fileEncSha256B64': toBase64(thumbUpload.get('fileEncSha256')),
                'mediaType': 'thumbnail-sticker-pack',
                'timeoutMs': getp(options, 'mediaUploadTimeoutMs')
            })))
        finally:
            tryUnlink(thumbUpload.get('encFilePath'), logger)
        obj.update(JSObject({
            'thumbnailDirectPath': getp(thumbUploadResult, 'directPath'),
            'thumbnailSha256': thumbUpload.get('fileSha256'),
            'thumbnailEncSha256': thumbUpload.get('fileEncSha256'),
            'thumbnailHeight': 252,
            'thumbnailWidth': 252,
            'imageDataHash': toBase64(sha256(thumbnailBuffer))
        }))
    except Exception as error:
        logCall(logger, 'warn', 'Thumbnail generation failed: ' + str(error))

    if truthy(cacheableKey):
        logCall(logger, 'debug', JSObject({'cacheableKey': cacheableKey}), 'set cache (background)')
        pending = mediaCache.set(cacheableKey, proto.Message.StickerPackMessage.encode(obj).finish())
        if inspect.isawaitable(pending):
            asyncio.ensure_future(pending)
    return proto.Message.StickerPackMessage.fromObject(obj)


async def prepareWAMessageMedia(message, options):
    logger = getp(options, 'logger')
    mediaType = None
    for key in MEDIA_KEYS:
        if key in message:
            mediaType = key
    if mediaType is None:
        raise httpBoom('Invalid media type')

    uploadData = JSObject(message)
    uploadData['media'] = message[mediaType]
    uploadData.pop(mediaType, None)

    mediaCache = getp(options, 'mediaCache')
    media = uploadData.get('media')
    cacheableKey = False
    if isinstance(media, dict) and 'url' in media and truthy(media.get('url')) and truthy(mediaCache):
        cacheableKey = mediaType + ':' + jsString(media.get('url'))

    if mediaType == 'document' and not truthy(uploadData.get('fileName')):
        uploadData['fileName'] = 'file'
    if not truthy(uploadData.get('mimetype')):
        uploadData['mimetype'] = MIMETYPE_MAP[mediaType]

    if truthy(cacheableKey):
        mediaBuff = await maybeAwait(mediaCache.get(cacheableKey))
        if mediaBuff is not None:
            logCall(logger, 'debug', JSObject({'cacheableKey': cacheableKey}), 'got media cache hit')
            obj = proto.Message.decode(mediaBuff)
            key = mediaType + 'Message'
            cached = JSObject(uploadData)
            cached['media'] = None
            obj[key].update(cached)
            return obj

    isNewsletter = truthy(getp(options, 'jid')) and isJidNewsletter(getp(options, 'jid'))
    if isNewsletter:
        logCall(logger, 'info', JSObject({'key': cacheableKey}), 'Preparing raw media for newsletter')
        rawData = await getRawMediaUploadData(uploadData.get('media'), jsOr(getp(options, 'mediaTypeOverride'), mediaType), logger)
        filePath = rawData.get('filePath')
        fileSha256 = rawData.get('fileSha256')
        fileLength = rawData.get('fileLength')
        uploadResult = await maybeAwait(options.get('upload')(filePath, JSObject({
            'fileEncSha256B64': toBase64(fileSha256),
            'mediaType': mediaType,
            'timeoutMs': getp(options, 'mediaUploadTimeoutMs'),
            'newsletter': isNewsletter
        })))
        os.remove(filePath)
        fields = JSObject({
            'url': getp(uploadResult, 'mediaUrl'),
            'directPath': getp(uploadResult, 'directPath'),
            'fileSha256': fileSha256,
            'fileLength': fileLength,
            'thumbnailDirectPath': getp(uploadResult, 'thumbnailDirectPath'),
            'thumbnailSha256': getp(uploadResult, 'thumbnailSha256')
        })
        fields.update(uploadData)
        fields['media'] = None
        obj = proto.Message.fromObject(JSObject({mediaType + 'Message': MESSAGE_TYPE_PROTO[mediaType].fromObject(fields)}))
        if truthy(uploadData.get('ptv')):
            obj['ptvMessage'] = obj.get('videoMessage')
            obj.pop('videoMessage', None)
        if truthy(obj.get('stickerMessage')):
            obj.get('stickerMessage')['stickerSentTs'] = now()
        if truthy(cacheableKey):
            logCall(logger, 'debug', JSObject({'cacheableKey': cacheableKey}), 'set cache')
            await maybeAwait(mediaCache.set(cacheableKey, proto.Message.encode(obj).finish()))
        return obj

    requiresDurationComputation = mediaType == 'audio' and uploadData.get('seconds') is None
    requiresThumbnailComputation = mediaType in ('image', 'video') and uploadData.get('jpegThumbnail') is None
    requiresWaveformProcessing = mediaType == 'audio' and uploadData.get('ptt') is True and uploadData.get('waveform') is None
    requiresAudioBackground = truthy(getp(options, 'backgroundColor')) and mediaType == 'audio' and uploadData.get('ptt') is True
    requiresOriginalForSomeProcessing = requiresDurationComputation or requiresThumbnailComputation

    streamInfo = await encryptedStream(
        uploadData.get('media'),
        jsOr(getp(options, 'mediaTypeOverride'), mediaType),
        JSObject({'logger': logger, 'saveOriginalFileIfRequired': requiresOriginalForSomeProcessing, 'opts': getp(options, 'options')})
    )
    mediaKey = streamInfo.get('mediaKey')
    encFilePath = streamInfo.get('encFilePath')
    originalFilePath = streamInfo.get('originalFilePath')
    fileEncSha256 = streamInfo.get('fileEncSha256')
    fileSha256 = streamInfo.get('fileSha256')
    fileLength = streamInfo.get('fileLength')
    fileEncSha256B64 = toBase64(fileEncSha256)

    async def uploadMediaTask():
        result = await maybeAwait(options.get('upload')(encFilePath, JSObject({
            'fileEncSha256B64': fileEncSha256B64,
            'mediaType': mediaType,
            'timeoutMs': getp(options, 'mediaUploadTimeoutMs')
        })))
        logCall(logger, 'debug', JSObject({'mediaType': mediaType, 'cacheableKey': cacheableKey}), 'uploaded media')
        return result

    async def extraInfoTask():
        try:
            if requiresThumbnailComputation:
                thumbInfo = await generateThumbnail(originalFilePath, mediaType, options)
                uploadData['jpegThumbnail'] = thumbInfo.get('thumbnail')
                dimensions = thumbInfo.get('originalImageDimensions')
                if not truthy(uploadData.get('width')) and truthy(dimensions):
                    uploadData['width'] = dimensions.get('width')
                    uploadData['height'] = dimensions.get('height')
                    logCall(logger, 'debug', 'set dimensions')
                logCall(logger, 'debug', 'generated thumbnail')
            if requiresDurationComputation:
                uploadData['seconds'] = await getAudioDuration(originalFilePath)
                logCall(logger, 'debug', 'computed audio duration')
            if requiresWaveformProcessing:
                uploadData['waveform'] = await getAudioWaveform(originalFilePath, logger)
                logCall(logger, 'debug', 'processed waveform')
            if requiresAudioBackground:
                uploadData['backgroundArgb'] = await assertColor(getp(options, 'backgroundColor'))
                logCall(logger, 'debug', 'computed backgroundColor audio status')
        except Exception as error:
            logCall(logger, 'warn', JSObject({'trace': errorStack(error)}), 'failed to obtain extra info')

    try:
        uploadResult, _ = await asyncio.gather(uploadMediaTask(), extraInfoTask())
    finally:
        try:
            os.remove(encFilePath)
            if truthy(originalFilePath):
                os.remove(originalFilePath)
            logCall(logger, 'debug', 'removed tmp files')
        except Exception:
            logCall(logger, 'warn', 'failed to remove tmp file')

    fields = JSObject({
        'url': getp(uploadResult, 'mediaUrl'),
        'directPath': getp(uploadResult, 'directPath'),
        'mediaKey': mediaKey,
        'fileEncSha256': fileEncSha256,
        'fileSha256': fileSha256,
        'fileLength': fileLength,
        'mediaKeyTimestamp': unixTimestampSeconds(None)
    })
    fields.update(uploadData)
    fields['media'] = None
    obj = proto.Message.fromObject(JSObject({mediaType + 'Message': MESSAGE_TYPE_PROTO[mediaType].fromObject(fields)}))
    if truthy(uploadData.get('ptv')):
        obj['ptvMessage'] = obj.get('videoMessage')
        obj.pop('videoMessage', None)
    if truthy(cacheableKey):
        logCall(logger, 'debug', JSObject({'cacheableKey': cacheableKey}), 'set cache')
        await maybeAwait(mediaCache.set(cacheableKey, proto.Message.encode(obj).finish()))
    return obj


FUTURE_PROOF_KEYS = [
    'associatedChildMessage', 'botForwardedMessage', 'botInvokeMessage', 'botTaskMessage', 'documentWithCaptionMessage',
    'editedMessage', 'ephemeralMessage', 'eventCoverImage', 'groupMentionedMessage', 'groupStatusMentionMessage',
    'groupStatusMessage', 'groupStatusMessageV2', 'limitSharingMessage', 'lottieStickerMessage', 'newsletterAdminProfileMessage',
    'newsletterAdminProfileMessageV2', 'newsletterAdminProfileStatusMessage', 'pollCreationMessageV4',
    'pollCreationOptionImageMessage', 'questionMessage', 'questionReplyMessage', 'spoilerMessage', 'statusAddYours',
    'statusMentionMessage', 'viewOnceMessage', 'viewOnceMessageV2', 'viewOnceMessageV2Extension'
]


def hasNonNullishProperty(message, key):
    return isinstance(message, dict) and message.get(key) is not None


def hasOptionalProperty(obj, key):
    return isinstance(obj, dict) and obj.get(key) is not None


def hasValidAlbumMedia(message):
    return truthy(getp(message, 'imageMessage')) or truthy(getp(message, 'videoMessage'))


def hasValidInteractiveHeader(message):
    return any(truthy(getp(message, key)) for key in ('imageMessage', 'videoMessage', 'documentMessage', 'productMessage', 'locationMessage'))


def hasValidCarouselHeader(message):
    return any(truthy(getp(message, key)) for key in ('imageMessage', 'videoMessage', 'productMessage'))


def firstKeyName(obj):
    name = next(iter(obj), None) if isinstance(obj, dict) else None
    if name is None:
        raise TypeError("Cannot read properties of undefined (reading 'replace')")
    return name


def mediaHeaderName(obj):
    return firstKeyName(obj).replace('Message', '', 1).upper()


def mapMessageButton(button):
    buttonText = jsOr(getp(button, 'text'), getp(button, 'buttonText'))
    if hasOptionalProperty(button, 'sections'):
        return JSObject({
            'nativeFlowInfo': JSObject({
                'name': 'single_select',
                'paramsJson': jsonStringify(JSObject({'title': buttonText, 'sections': button.get('sections')}))
            }),
            'type': ButtonType['NATIVE_FLOW']
        })
    if hasOptionalProperty(button, 'name'):
        return JSObject({
            'nativeFlowInfo': JSObject({'name': button.get('name'), 'paramsJson': getp(button, 'paramsJson')}),
            'type': ButtonType['NATIVE_FLOW']
        })
    return JSObject({
        'buttonId': jsOr(getp(button, 'id'), getp(button, 'buttonId')),
        'buttonText': JSObject({'displayText': buttonText}) if isinstance(buttonText, str) else buttonText,
        'type': jsOr(getp(button, 'type'), ButtonType['RESPONSE'])
    })


def mapTemplateButton(button, index):
    buttonText = jsOr(getp(button, 'text'), getp(button, 'buttonText'))
    if hasOptionalProperty(button, 'id'):
        return JSObject({'index': index, 'quickReplyButton': JSObject({'displayText': jsOr(buttonText, '\U0001f449\U0001f3fb Click'), 'id': button.get('id')})})
    if hasOptionalProperty(button, 'url'):
        return JSObject({'index': index, 'urlButton': JSObject({'displayText': jsOr(buttonText, '\U0001f310 Visit'), 'url': button.get('url')})})
    if hasOptionalProperty(button, 'call'):
        return JSObject({'index': index, 'callButton': JSObject({'displayText': jsOr(buttonText, '\U0001f4de Call'), 'phoneNumber': button.get('call')})})
    button['index'] = jsOr(getp(button, 'index'), index)
    return button


async def buildCarouselCard(card, options):
    carouselHeader = JSObject()
    if hasNonNullishProperty(card, 'product'):
        carouselHeader['productMessage'] = await prepareProductMessage(card, options)
    else:
        try:
            carouselHeader = await prepareWAMessageMedia(card, options)
        except Exception:
            if truthy(getp(card, 'image')) or truthy(getp(card, 'video')):
                raise
            carouselHeader = JSObject()
    if not hasValidCarouselHeader(carouselHeader):
        raise httpBoom('Invalid media type for carousel card')
    nativeSource = card if truthy(getp(card, 'nativeFlow')) else JSObject({'nativeFlow': []})
    carouselCard = JSObject({'nativeFlowMessage': prepareNativeFlowButtons(nativeSource)})
    carouselCard['header'] = JSObject({
        'title': jsOr(getp(card, 'title'), ''),
        'subtitle': jsOr(getp(card, 'subtitle'), ''),
        'hasMediaAttachment': True
    })
    carouselCard.get('header').update(carouselHeader)
    if hasOptionalProperty(card, 'text'):
        carouselCard['body'] = JSObject({'text': card.get('text')})
    else:
        if hasOptionalProperty(card, 'caption'):
            carouselCard['body'] = JSObject({'text': card.get('caption')})
        if truthy(getp(card, 'thumbnail')):
            carouselCard['jpegThumbnail'] = card.get('thumbnail')
    if hasOptionalProperty(card, 'audioFooter'):
        prepared = await prepareWAMessageMedia(JSObject({'audio': card.get('audioFooter')}), options)
        carouselCard['footer'] = JSObject({'audioMessage': prepared.get('audioMessage'), 'hasMediaAttachment': True})
    elif hasOptionalProperty(card, 'footer'):
        carouselCard['footer'] = JSObject({'text': card.get('footer')})
    return carouselCard


def generateForwardMessageContent(message, forceForward):
    content = getp(message, 'message')
    if not truthy(content):
        raise httpBoom('no content in message')
    content = normalizeMessageContent(content)
    content = proto.Message.decode(proto.Message.encode(content).finish())
    key = next(iter(content), None)
    inner = content.get(key) if key is not None else None
    score = jsOr(getp(getp(inner, 'contextInfo'), 'forwardingScore'), 0)
    fromMe = truthy(getp(getp(message, 'key'), 'fromMe'))
    score += 0 if (fromMe and not truthy(forceForward)) else 1
    if key == 'conversation':
        content['extendedTextMessage'] = JSObject({'text': content.get('conversation')})
        content.pop('conversation', None)
        key = 'extendedTextMessage'
    target = content.get(key)
    if target is None:
        raise TypeError("Cannot set properties of undefined (setting 'contextInfo')")
    target['contextInfo'] = JSObject({'forwardingScore': score, 'isForwarded': True}) if score > 0 else JSObject()
    return content


def pickContentKey(obj):
    if not isinstance(obj, dict):
        return None
    for name in obj.keys():
        if name != 'messageContextInfo' and name != 'senderKeyDistributionMessage':
            return name
    return None


def getContextInfoHolder(m):
    typeKey = pickContentKey(m)
    if typeKey is None:
        return None
    holder = m[typeKey]
    if typeKey == 'botForwardedMessage' and truthy(getp(holder, 'message')):
        inner = holder.get('message')
        innerType = pickContentKey(inner)
        if innerType is not None and truthy(getp(inner, innerType)):
            holder = inner[innerType]
    return holder


async def generateWAMessageContent(message, options):
    m = JSObject()
    if hasNonNullishProperty(message, 'raw'):
        message.pop('raw', None)
        return message
    elif hasNonNullishProperty(message, 'aiRich'):
        aiRich = message.get('aiRich')
        builder = aiRich if isinstance(aiRich, AIRichBuilder) else aiRichBuilderFromSpec(aiRich)
        kwargs = {}
        for name in ('forwarded', 'quoted', 'quotedParticipant', 'botJid'):
            value = getp(message, name)
            if value is not None:
                kwargs[name] = value
        m = builder.build(**kwargs)
    elif (
        hasNonNullishProperty(message, 'code')
        or hasNonNullishProperty(message, 'links')
        or hasNonNullishProperty(message, 'table')
        or hasNonNullishProperty(message, 'richResponse')
    ):
        m = prepareRichResponseMessage(message)
    elif hasNonNullishProperty(message, 'text'):
        extContent = JSObject({'text': message.get('text')})
        urlInfo = getp(message, 'linkPreview')
        if urlInfo is None:
            urlInfo = await generateLinkPreviewIfRequired(message.get('text'), getp(options, 'getUrlInfo'), getp(options, 'logger'))
        if truthy(urlInfo):
            extContent['matchedText'] = getp(urlInfo, 'matched-text')
            extContent['jpegThumbnail'] = getp(urlInfo, 'jpegThumbnail')
            extContent['description'] = getp(urlInfo, 'description')
            extContent['title'] = getp(urlInfo, 'title')
            extContent['previewType'] = nullish(getp(urlInfo, 'previewType'), 0)
            extContent['linkPreviewMetadata'] = getp(urlInfo, 'linkPreviewMetadata')
            img = getp(urlInfo, 'highQualityThumbnail')
            if truthy(img):
                extContent['thumbnailDirectPath'] = getp(img, 'directPath')
                extContent['mediaKey'] = getp(img, 'mediaKey')
                extContent['mediaKeyTimestamp'] = getp(img, 'mediaKeyTimestamp')
                extContent['thumbnailWidth'] = getp(img, 'width')
                extContent['thumbnailHeight'] = getp(img, 'height')
                extContent['thumbnailSha256'] = getp(img, 'fileSha256')
                extContent['thumbnailEncSha256'] = getp(img, 'fileEncSha256')
        faviconData = getp(message, 'favicon')
        if truthy(faviconData) and callable(getp(options, 'upload')):
            prepared = await prepareWAMessageMedia(JSObject({'image': faviconData}), options)
            imageMessage = prepared.get('imageMessage')
            extContent['faviconMMSMetadata'] = JSObject({
                'thumbnailDirectPath': imageMessage.get('directPath'),
                'mediaKey': imageMessage.get('mediaKey'),
                'mediaKeyTimestamp': imageMessage.get('mediaKeyTimestamp'),
                'thumbnailWidth': 32,
                'thumbnailHeight': 32,
                'thumbnailSha256': imageMessage.get('fileSha256'),
                'thumbnailEncSha256': imageMessage.get('fileEncSha256')
            })
        if truthy(getp(options, 'backgroundColor')):
            extContent['backgroundArgb'] = await assertColor(options.get('backgroundColor'))
        if truthy(getp(options, 'font')):
            extContent['font'] = options.get('font')
        m['extendedTextMessage'] = extContent
    elif hasNonNullishProperty(message, 'contacts'):
        contacts = message.get('contacts')
        contactList = jsList(getp(contacts, 'contacts'))
        contactLen = len(contactList)
        if not contactLen:
            raise httpBoom('require atleast 1 contact')
        if contactLen == 1:
            m['contactMessage'] = proto.Message.ContactMessage.create(contactList[0])
        else:
            m['contactsArrayMessage'] = proto.Message.ContactsArrayMessage.create(contacts)
    elif hasNonNullishProperty(message, 'location'):
        m['locationMessage'] = proto.Message.LocationMessage.create(message.get('location'))
    elif hasNonNullishProperty(message, 'react'):
        react = message.get('react')
        if not truthy(getp(react, 'senderTimestampMs')):
            react['senderTimestampMs'] = now()
        m['reactionMessage'] = proto.Message.ReactionMessage.create(react)
    elif hasNonNullishProperty(message, 'delete'):
        m['protocolMessage'] = JSObject({'key': message.get('delete'), 'type': proto.Message.ProtocolMessage.Type.REVOKE})
    elif hasNonNullishProperty(message, 'forward'):
        m = generateForwardMessageContent(message.get('forward'), getp(message, 'force'))
    elif hasNonNullishProperty(message, 'disappearingMessagesInChat'):
        value = message.get('disappearingMessagesInChat')
        if isinstance(value, bool):
            exp = WA_DEFAULT_EPHEMERAL if value else 0
        else:
            exp = value
        m = prepareDisappearingMessageSettingContent(exp)
    elif hasNonNullishProperty(message, 'groupInvite'):
        invite = message.get('groupInvite')
        groupInvite = JSObject()
        m['groupInviteMessage'] = groupInvite
        groupInvite['inviteCode'] = getp(invite, 'inviteCode')
        groupInvite['inviteExpiration'] = getp(invite, 'inviteExpiration')
        groupInvite['caption'] = getp(invite, 'text')
        groupInvite['groupJid'] = getp(invite, 'jid')
        groupInvite['groupName'] = getp(invite, 'subject')
        getProfilePicUrl = getp(options, 'getProfilePicUrl')
        if getProfilePicUrl is not None:
            pfpUrl = await maybeAwait(getProfilePicUrl(getp(invite, 'jid'), 'preview'))
            if truthy(pfpUrl):
                body = await fetchPlainBytes(pfpUrl)
                if body is not None:
                    groupInvite['jpegThumbnail'] = body
    elif hasNonNullishProperty(message, 'stickers'):
        m['stickerPackMessage'] = await prepareStickerPackMessage(message, options)
    elif hasNonNullishProperty(message, 'pin'):
        m['pinInChatMessage'] = JSObject({'key': message.get('pin'), 'type': message.get('type'), 'senderTimestampMs': now()})
        m['messageContextInfo'] = JSObject({
            'messageAddOnDurationInSecs': jsOr(getp(message, 'time'), 86400) if getp(message, 'type') == 1 else 0
        })
    elif hasNonNullishProperty(message, 'keep'):
        m['keepInChatMessage'] = JSObject({'key': message.get('keep'), 'keepType': message.get('type'), 'timestampMs': now()})
    elif hasNonNullishProperty(message, 'flowReply'):
        flow = message.get('flowReply')
        m['interactiveResponseMessage'] = JSObject({
            'body': JSObject({
                'format': jsOr(getp(flow, 'format'), proto.Message.InteractiveResponseMessage.Body.Format.DEFAULT),
                'text': getp(flow, 'text')
            }),
            'nativeFlowResponseMessage': JSObject({
                'name': getp(flow, 'name'),
                'paramsJson': jsOr(getp(flow, 'paramsJson'), '{}'),
                'version': jsOr(getp(flow, 'version'), 1)
            })
        })
    elif hasNonNullishProperty(message, 'buttonReply'):
        reply = message.get('buttonReply')
        if getp(message, 'type') == 'template':
            m['templateButtonReplyMessage'] = JSObject({
                'selectedDisplayText': getp(reply, 'displayText'),
                'selectedId': getp(reply, 'id'),
                'selectedIndex': getp(reply, 'index')
            })
        elif getp(message, 'type') == 'plain':
            m['buttonsResponseMessage'] = JSObject({
                'selectedButtonId': getp(reply, 'id'),
                'selectedDisplayText': getp(reply, 'displayText'),
                'type': proto.Message.ButtonsResponseMessage.Type.DISPLAY_TEXT
            })
    elif hasNonNullishProperty(message, 'listReply'):
        reply = message.get('listReply')
        m['listResponseMessage'] = JSObject({
            'description': getp(reply, 'description'),
            'listType': proto.Message.ListResponseMessage.ListType.SINGLE_SELECT,
            'singleSelectReply': JSObject({'selectedRowId': getp(reply, 'id')}),
            'title': getp(reply, 'title')
        })
    elif truthy(getp(message, 'ptv')):
        prepared = await prepareWAMessageMedia(JSObject({'video': getp(message, 'video')}), options)
        m['ptvMessage'] = prepared.get('videoMessage')
    elif hasNonNullishProperty(message, 'product'):
        m['productMessage'] = await prepareProductMessage(message, options)
    elif hasNonNullishProperty(message, 'event'):
        event = message.get('event')
        eventMessage = JSObject()
        m['eventMessage'] = eventMessage
        startTime = math.floor(dateToMs(getp(event, 'startDate')) / 1000)
        if truthy(getp(event, 'call')) and getp(options, 'getCallLink') is not None:
            token = await maybeAwait(options.get('getCallLink')(event.get('call'), JSObject({'startTime': startTime})))
            prefix = CALL_AUDIO_PREFIX if event.get('call') == 'audio' else CALL_VIDEO_PREFIX
            eventMessage['joinLink'] = prefix + jsString(token)
        m['messageContextInfo'] = JSObject({'messageSecret': jsOr(getp(event, 'messageSecret'), randomBytes(32))})
        eventMessage['name'] = getp(event, 'name')
        eventMessage['description'] = getp(event, 'description')
        eventMessage['startTime'] = startTime
        eventMessage['endTime'] = jsNumber(dateToMs(event.get('endDate')) / 1000) if truthy(getp(event, 'endDate')) else None
        eventMessage['isCanceled'] = nullish(getp(event, 'isCancelled'), False)
        eventMessage['extraGuestsAllowed'] = getp(event, 'extraGuestsAllowed')
        eventMessage['isScheduleCall'] = nullish(getp(event, 'isScheduleCall'), False)
        eventMessage['location'] = getp(event, 'location')
    elif hasNonNullishProperty(message, 'poll'):
        poll = message.get('poll')
        if not truthy(poll.get('selectableCount')):
            poll['selectableCount'] = 0
        if not truthy(poll.get('toAnnouncementGroup')):
            poll['toAnnouncementGroup'] = False
        values = poll.get('values')
        if not isinstance(values, list):
            raise httpBoom('Invalid poll values')
        selectable = poll.get('selectableCount')
        if selectable < 0 or selectable > len(values):
            raise httpBoom('poll.selectableCount in poll should be >= 0 and <= ' + str(len(values)))
        pollCreationMessage = JSObject({
            'name': poll.get('name'),
            'selectableOptionsCount': selectable,
            'options': [JSObject({'optionName': optionName}) for optionName in values],
            'endTime': dateToMs(poll.get('endDate')) if truthy(poll.get('endDate')) else None,
            'hideParticipantName': nullish(poll.get('hideVoter'), False),
            'allowAddOption': nullish(poll.get('canAddOption'), False)
        })
        if truthy(poll.get('toAnnouncementGroup')):
            m['pollCreationMessageV2'] = pollCreationMessage
        elif poll.get('pollType') == 1:
            if not truthy(poll.get('correctAnswer')):
                raise httpBoom('No "correctAnswer" provided for quiz')
            quizMessage = JSObject(pollCreationMessage)
            quizMessage['correctAnswer'] = JSObject({'optionName': jsString(poll.get('correctAnswer'))})
            quizMessage['pollType'] = 1
            quizMessage['selectableOptionsCount'] = 1
            m['pollCreationMessageV5'] = quizMessage
        elif truthy(poll.get('hideVoter')):
            m['pollCreationMessageV6'] = pollCreationMessage
        elif selectable == 1:
            m['pollCreationMessageV3'] = pollCreationMessage
        else:
            m['pollCreationMessage'] = pollCreationMessage
        m['messageContextInfo'] = JSObject({'messageSecret': jsOr(poll.get('messageSecret'), randomBytes(32))})
    elif hasNonNullishProperty(message, 'pollResult'):
        result = message.get('pollResult')
        snapshot = JSObject({
            'name': getp(result, 'name'),
            'pollVotes': [
                JSObject({'optionName': getp(vote, 'name'), 'optionVoteCount': jsParseInt(jsString(getp(vote, 'voteCount')), 10)})
                for vote in jsList(getp(result, 'votes'))
            ]
        })
        if getp(result, 'pollType') == 1:
            snapshot['pollType'] = proto.Message.PollType.QUIZ
            m['pollResultSnapshotMessageV3'] = snapshot
        else:
            snapshot['pollType'] = proto.Message.PollType.POLL
            m['pollResultSnapshotMessage'] = snapshot
    elif hasNonNullishProperty(message, 'pollUpdate'):
        update = message.get('pollUpdate')
        if not truthy(getp(update, 'key')):
            raise httpBoom('Message key is required')
        if not truthy(getp(update, 'vote')):
            raise httpBoom('Encrypted vote payload is required')
        m['pollUpdateMessage'] = JSObject({
            'metadata': getp(update, 'metadata'),
            'pollCreationMessageKey': update.get('key'),
            'senderTimestampMs': now(),
            'vote': update.get('vote')
        })
    elif hasNonNullishProperty(message, 'paymentInviteServiceType'):
        m['paymentInviteMessage'] = JSObject({'expiryTimestamp': now(), 'serviceType': message.get('paymentInviteServiceType')})
    elif hasNonNullishProperty(message, 'orderText'):
        if not isBuffer(getp(message, 'thumbnail')):
            raise httpBoom('Must provide thumbnail buffer in order message')
        orderMessage = JSObject({
            'itemCount': 1,
            'messageVersion': 1,
            'orderTitle': LIBRARY_NAME,
            'status': proto.Message.OrderMessage.OrderStatus.INQUIRY,
            'surface': proto.Message.OrderMessage.OrderSurface.CATALOG,
            'token': generateMessageIDV2(None),
            'totalAmount1000': 1000,
            'totalCurrencyCode': 'IDR'
        })
        orderMessage.update(message)
        orderMessage['message'] = message.get('orderText')
        orderMessage.pop('orderText', None)
        m['orderMessage'] = orderMessage
    elif hasNonNullishProperty(message, 'album'):
        album = message.get('album')
        if not isinstance(album, list):
            raise httpBoom('Invalid album type. Expected an array.')
        videoCount = sum(1 for item in album if truthy(getp(item, 'video')))
        imageCount = sum(1 for item in album if truthy(getp(item, 'image')))
        if videoCount + imageCount < 2:
            raise httpBoom('Minimum provide 2 media to upload album message')
        m['albumMessage'] = JSObject({'expectedImageCount': imageCount, 'expectedVideoCount': videoCount})
    elif hasNonNullishProperty(message, 'sharePhoneNumber'):
        m['protocolMessage'] = JSObject({'type': proto.Message.ProtocolMessage.Type.SHARE_PHONE_NUMBER})
    elif hasNonNullishProperty(message, 'requestPhoneNumber'):
        m['requestPhoneNumberMessage'] = JSObject()
    elif hasNonNullishProperty(message, 'limitSharing'):
        m['protocolMessage'] = JSObject({
            'type': proto.Message.ProtocolMessage.Type.LIMIT_SHARING,
            'limitSharing': JSObject({
                'sharingLimited': message.get('limitSharing') is True,
                'trigger': 1,
                'limitSharingSettingTimestamp': now(),
                'initiatedByMe': True
            })
        })
    else:
        m = await prepareWAMessageMedia(message, options)

    if hasNonNullishProperty(message, 'buttons'):
        buttonsMessage = JSObject({'buttons': [mapMessageButton(button) for button in jsList(message.get('buttons'))]})
        if hasOptionalProperty(message, 'text'):
            buttonsMessage['contentText'] = message.get('text')
            buttonsMessage['headerType'] = ButtonHeaderType['EMPTY']
        else:
            if hasOptionalProperty(message, 'caption'):
                buttonsMessage['contentText'] = message.get('caption')
            buttonsMessage['headerType'] = ButtonHeaderType[mediaHeaderName(m)]
            buttonsMessage.update(m)
        if hasOptionalProperty(message, 'footer'):
            buttonsMessage['footerText'] = message.get('footer')
        m = JSObject({'buttonsMessage': buttonsMessage})
    elif hasNonNullishProperty(message, 'sections'):
        m = JSObject({'listMessage': JSObject({
            'sections': message.get('sections'),
            'buttonText': getp(message, 'buttonText'),
            'title': getp(message, 'title'),
            'footerText': getp(message, 'footer'),
            'description': getp(message, 'text'),
            'listType': ListType['SINGLE_SELECT']
        })})
    elif hasNonNullishProperty(message, 'templateButtons'):
        hydrated = JSObject({
            'hydratedButtons': [mapTemplateButton(button, index) for index, button in enumerate(jsList(message.get('templateButtons')))]
        })
        if hasOptionalProperty(message, 'text'):
            hydrated['hydratedContentText'] = message.get('text')
        else:
            if hasOptionalProperty(message, 'caption'):
                if not hasValidInteractiveHeader(m) and truthy(getp(message, 'title')):
                    hydrated['hydratedTitleText'] = message.get('title')
                hydrated['hydratedContentText'] = message.get('caption')
            hydrated.update(m)
        if hasOptionalProperty(message, 'footer'):
            hydrated['hydratedFooterText'] = message.get('footer')
        hydrated['templateId'] = jsOr(getp(message, 'id'), 'template-' + str(now()))
        m = JSObject({'templateMessage': JSObject({'hydratedFourRowTemplate': hydrated, 'hydratedTemplate': hydrated})})
    elif hasNonNullishProperty(message, 'nativeFlow'):
        interactiveMessage = JSObject({'nativeFlowMessage': prepareNativeFlowButtons(message)})
        if hasOptionalProperty(message, 'bizJid'):
            interactiveMessage['collectionMessage'] = JSObject({'bizJid': message.get('bizJid'), 'id': getp(message, 'id'), 'messageVersion': 1})
        elif hasOptionalProperty(message, 'shopSurface'):
            interactiveMessage['shopStorefrontMessage'] = JSObject({'surface': message.get('shopSurface'), 'id': getp(message, 'id'), 'messageVersion': 1})
        if hasOptionalProperty(message, 'text'):
            interactiveMessage['body'] = JSObject({'text': message.get('text')})
        else:
            if not hasValidInteractiveHeader(m):
                if hasOptionalProperty(message, 'caption'):
                    raise httpBoom('Invalid media type for interactive message header')
                raise httpBoom('nativeFlow needs `text`, or a media (image/video/document/location/product) with an optional `caption`')
            interactiveMessage['header'] = JSObject({
                'title': jsOr(getp(message, 'title'), ''),
                'subtitle': jsOr(getp(message, 'subtitle'), ''),
                'hasMediaAttachment': True
            })
            interactiveMessage.get('header').update(m)
            interactiveMessage['body'] = JSObject({'text': message.get('caption') if hasOptionalProperty(message, 'caption') else ''})
            if truthy(getp(message, 'thumbnail')):
                interactiveMessage['jpegThumbnail'] = message.get('thumbnail')
        if hasOptionalProperty(message, 'audioFooter'):
            prepared = await prepareWAMessageMedia(JSObject({'audio': message.get('audioFooter')}), options)
            interactiveMessage['footer'] = JSObject({'audioMessage': prepared.get('audioMessage'), 'hasMediaAttachment': True})
        elif hasOptionalProperty(message, 'footer'):
            interactiveMessage['footer'] = JSObject({'text': message.get('footer')})
        if hasNonNullishProperty(message, 'widget'):
            interactiveMessage['bloksWidget'] = prepareBloksWidget(message.get('widget'))
        m = JSObject({'interactiveMessage': interactiveMessage})
    elif hasNonNullishProperty(message, 'cards'):
        cards = await asyncio.gather(*[buildCarouselCard(card, options) for card in jsList(message.get('cards'))])
        interactiveMessage = JSObject({
            'carouselMessage': JSObject({
                'cards': list(cards),
                'carouselCardType': CarouselCardType['UNKNOWN'],
                'messageVersion': 1
            })
        })
        if hasOptionalProperty(message, 'text'):
            interactiveMessage['body'] = JSObject({'text': message.get('text')})
        if hasOptionalProperty(message, 'footer'):
            interactiveMessage['footer'] = JSObject({'text': message.get('footer')})
        m = JSObject({'interactiveMessage': interactiveMessage})
    elif hasNonNullishProperty(message, 'requestPaymentFrom'):
        requestPaymentMessage = JSObject({
            'amount': JSObject({'currencyCode': 'IDR', 'offset': 1000, 'value': 1000}),
            'amount1000': 1000,
            'currencyCodeIso4217': 'IDR',
            'expiryTimestamp': now(),
            'noteMessage': m,
            'requestFrom': message.get('requestPaymentFrom')
        })
        requestPaymentMessage.update(message)
        requestPaymentMessage.pop('requestPaymentFrom', None)
        if hasNonNullishProperty(m, 'extendedTextMessage') or hasNonNullishProperty(m, 'stickerMessage'):
            noteMessage = requestPaymentMessage.get('noteMessage')
            if noteMessage is None:
                raise TypeError('Cannot convert undefined or null to object')
            noteMessage.update(m)
        else:
            raise httpBoom('Invalid message type for request payment note message')
        m = JSObject({'requestPaymentMessage': requestPaymentMessage})
    elif hasNonNullishProperty(message, 'invoiceNote'):
        attachment = jsOr(getp(m, 'imageMessage'), getp(m, 'documentMessage'))
        typeName = mediaHeaderName(m)
        invoiceMessage = JSObject({
            'attachmentType': proto.Message.InvoiceMessage.AttachmentType['PDF' if typeName == 'DOCUMENT' else 'IMAGE'],
            'note': message.get('invoiceNote')
        })
        if truthy(attachment):
            invoiceMessage.update(JSObject({
                'attachmentDirectPath': getp(attachment, 'directPath'),
                'attachmentFileEncSha256': getp(attachment, 'fileEncSha256'),
                'attachmentFileSha256': getp(attachment, 'fileSha256'),
                'attachmentJpegThumbnail': getp(attachment, 'jpegThumbnail'),
                'attachmentMediaKey': getp(attachment, 'mediaKey'),
                'attachmentMediaKeyTimestamp': getp(attachment, 'mediaKeyTimestamp'),
                'attachmentMimetype': getp(attachment, 'mimetype'),
                'token': generateMessageIDV2(None)
            }))
        else:
            raise httpBoom('Invalid media type for invoice message')
        m = JSObject({'invoiceMessage': invoiceMessage})

    if hasOptionalProperty(message, 'externalAdReply') and truthy(message.get('externalAdReply')):
        key = getContextInfoHolder(m)
        content = message.get('externalAdReply')
        if 'thumbnail' in content and not isBuffer(content.get('thumbnail')):
            raise httpBoom('Thumbnail must in buffer type')
        if content.get('url') is not None and not isinstance(content.get('url'), str):
            raise httpBoom('externalAdReply.url must be a string')
        externalAdReply = JSObject(content)
        externalAdReply.update(JSObject({
            'body': content.get('body'),
            'mediaType': jsOr(content.get('mediaType'), 1),
            'mediaUrl': content.get('url'),
            'renderLargerThumbnail': content.get('largeThumbnail'),
            'sourceUrl': content.get('url'),
            'thumbnail': content.get('thumbnail'),
            'thumbnailUrl': jsString(content.get('url')) + '?update=' + str(now()) if truthy(content.get('url')) else None,
            'title': jsOr(content.get('title'), LIBRARY_NAME)
        }))
        externalAdReply.pop('subTitle', None)
        externalAdReply.pop('largeThumbnail', None)
        externalAdReply.pop('url', None)
        if key is not None and 'contextInfo' in key and truthy(key.get('contextInfo')):
            previous = getp(key.get('contextInfo'), 'externalAdReply')
            merged = JSObject(previous if isinstance(previous, dict) else {})
            merged.update(externalAdReply)
            key.get('contextInfo')['externalAdReply'] = merged
        elif key is not None:
            key['contextInfo'] = JSObject({'externalAdReply': externalAdReply})

    mentionList = getp(message, 'mentions')
    mentionCount = len(mentionList) if isinstance(mentionList, (list, str)) else 0
    if mentionCount > 0 or truthy(getp(message, 'mentionAll')):
        key = getContextInfoHolder(m)
        if key is not None and 'contextInfo' in key:
            key['contextInfo'] = jsOr(key.get('contextInfo'), JSObject())
            if mentionCount > 0:
                key.get('contextInfo')['mentionedJid'] = mentionList
            if truthy(getp(message, 'mentionAll')):
                key.get('contextInfo')['nonJidMentions'] = 1
        elif key is not None:
            key['contextInfo'] = JSObject({
                'mentionedJid': mentionList,
                'nonJidMentions': 1 if truthy(getp(message, 'mentionAll')) else 0
            })

    if hasOptionalProperty(message, 'contextInfo') and truthy(message.get('contextInfo')):
        key = getContextInfoHolder(m)
        if key is not None and 'contextInfo' in key and truthy(key.get('contextInfo')):
            key['contextInfo'] = JSObject({**key.get('contextInfo'), **message.get('contextInfo')})
        elif key is not None:
            key['contextInfo'] = message.get('contextInfo')

    if hasOptionalProperty(message, 'groupStatus') and truthy(message.get('groupStatus')):
        key = getContextInfoHolder(m)
        if key is not None and 'contextInfo' in key and truthy(key.get('contextInfo')):
            key.get('contextInfo')['isGroupStatus'] = message.get('groupStatus')
        elif key is not None:
            key['contextInfo'] = JSObject({'isGroupStatus': message.get('groupStatus')})
        m = JSObject({'groupStatusMessageV2': JSObject({'message': m})})
        message.pop('groupStatus', None)

    if hasOptionalProperty(message, 'spoiler') and truthy(message.get('spoiler')):
        key = getContextInfoHolder(m)
        if key is not None and 'contextInfo' in key and truthy(key.get('contextInfo')):
            key.get('contextInfo')['isSpoiler'] = message.get('spoiler')
        elif key is not None:
            key['contextInfo'] = JSObject({'isSpoiler': message.get('spoiler')})
        m = JSObject({'spoilerMessage': JSObject({'message': m})})
        message.pop('spoiler', None)
    elif hasOptionalProperty(message, 'interactiveAsTemplate') and truthy(message.get('interactiveAsTemplate')):
        if not truthy(getp(m, 'interactiveMessage')):
            raise httpBoom('Invalid message type for template')
        m = JSObject({'templateMessage': JSObject({
            'interactiveMessageTemplate': m.get('interactiveMessage'),
            'templateId': jsOr(getp(message, 'id'), 'template-' + str(now()))
        })})
        message.pop('interactiveAsTemplate', None)

    if hasOptionalProperty(message, 'ephemeral') and truthy(message.get('ephemeral')):
        m = JSObject({'ephemeralMessage': JSObject({'message': m})})
        message.pop('ephemeral', None)

    if hasOptionalProperty(message, 'isLottie') and truthy(message.get('isLottie')):
        m = JSObject({'lottieStickerMessage': JSObject({'message': m})})
    elif hasOptionalProperty(message, 'viewOnce') and truthy(message.get('viewOnce')):
        m = JSObject({'viewOnceMessage': JSObject({'message': m})})
    elif hasOptionalProperty(message, 'viewOnceV2') and truthy(message.get('viewOnceV2')):
        m = JSObject({'viewOnceMessageV2': JSObject({'message': m})})
        message.pop('viewOnceV2', None)
    elif hasOptionalProperty(message, 'viewOnceV2Extension') and truthy(message.get('viewOnceV2Extension')):
        m = JSObject({'viewOnceMessageV2Extension': JSObject({'message': m})})
        message.pop('viewOnceV2Extension', None)

    if hasOptionalProperty(message, 'edit'):
        m = JSObject({'protocolMessage': JSObject({
            'key': message.get('edit'),
            'editedMessage': m,
            'timestampMs': now(),
            'type': proto.Message.ProtocolMessage.Type.MESSAGE_EDIT
        })})

    if not isinstance(m, JSObject):
        m = JSObject(m)
    if shouldIncludeReportingToken(m):
        m['messageContextInfo'] = jsOr(m.get('messageContextInfo'), JSObject())
        if not truthy(m.get('messageContextInfo').get('messageSecret')):
            m.get('messageContextInfo')['messageSecret'] = randomBytes(32)
    return proto.Message.create(m)


def generateWAMessageFromContent(jid, message, options):
    if not truthy(getp(options, 'timestamp')):
        options['timestamp'] = now()
    else:
        options['timestamp'] = dateToMs(options.get('timestamp'))
    innerMessage = normalizeMessageContent(message)
    key = getContentType(innerMessage)
    timestamp = unixTimestampSeconds(options.get('timestamp'))
    isNewsletter = isJidNewsletter(jid)
    quoted = getp(options, 'quoted')
    userJid = getp(options, 'userJid')

    if truthy(quoted):
        quotedKey = getp(quoted, 'key')
        if truthy(getp(quotedKey, 'fromMe')):
            participant = userJid
        else:
            participant = jsOr(getp(quoted, 'participant'), getp(quotedKey, 'participant'), getp(quotedKey, 'remoteJid'))
        quotedMsg = normalizeMessageContent(getp(quoted, 'message'))
        msgType = getContentType(quotedMsg)
        quotedMsg = proto.Message.create(JSObject({msgType: quotedMsg[msgType]}))
        quotedContent = quotedMsg[msgType]
        if isinstance(quotedContent, dict) and 'contextInfo' in quotedContent:
            del quotedContent['contextInfo']
        existing = innerMessage[key]
        if existing is None:
            raise TypeError("Cannot use 'in' operator to search for 'contextInfo'")
        contextInfo = jsOr(existing.get('contextInfo') if 'contextInfo' in existing else None, JSObject())
        contextInfo['participant'] = jidNormalizedUser(participant)
        contextInfo['stanzaId'] = getp(quotedKey, 'id')
        contextInfo['quotedMessage'] = quotedMsg
        if not isNewsletter and jid != getp(quotedKey, 'remoteJid'):
            contextInfo['remoteJid'] = getp(quotedKey, 'remoteJid')
        if truthy(contextInfo) and truthy(existing):
            existing['contextInfo'] = contextInfo

    if truthy(getp(options, 'ephemeralExpiration')) and key not in ('protocolMessage', 'ephemeralMessage') and not isNewsletter:
        target = innerMessage[key]
        target['contextInfo'] = JSObject({
            **jsOr(target.get('contextInfo'), {}),
            'expiration': jsOr(getp(options, 'ephemeralExpiration'), WA_DEFAULT_EPHEMERAL)
        })

    message = proto.Message.create(message)
    messageJSON = JSObject({
        'key': JSObject({
            'remoteJid': jid,
            'fromMe': True,
            'id': jsOr(getp(options, 'messageId'), generateMessageIDV2(None))
        }),
        'message': message,
        'messageTimestamp': timestamp,
        'messageStubParameters': [],
        'participant': userJid if (isJidGroup(jid) or isJidStatusBroadcast(jid)) else None,
        'status': WAMessageStatus['PENDING']
    })
    return proto.WebMessageInfo.fromObject(messageJSON)


async def generateWAMessage(jid, content, options):
    logger = getp(options, 'logger')
    if logger is not None and callable(getattr(logger, 'child', None)):
        options['logger'] = logger.child(JSObject({'msgId': getp(options, 'messageId')}))
    contentOptions = JSObject(options)
    contentOptions['jid'] = jid
    return generateWAMessageFromContent(jid, await generateWAMessageContent(content, contentOptions), options)


def getContentType(content):
    if truthy(content):
        for name in content.keys():
            if (name == 'conversation' or 'Message' in name) and name != 'senderKeyDistributionMessage':
                return name
    return None


def getFutureProofMessage(message):
    for name in FUTURE_PROOF_KEYS:
        value = getp(message, name)
        if truthy(value):
            return value
    return None


def normalizeMessageContent(content):
    if not truthy(content):
        return None
    for _ in range(5):
        inner = getFutureProofMessage(content)
        if not truthy(inner):
            break
        content = getp(inner, 'message')
    return content


def extractMessageContent(content):
    def extractFromTemplateMessage(msg):
        if truthy(getp(msg, 'imageMessage')):
            return JSObject({'imageMessage': msg.get('imageMessage')})
        if truthy(getp(msg, 'documentMessage')):
            return JSObject({'documentMessage': msg.get('documentMessage')})
        if truthy(getp(msg, 'videoMessage')):
            return JSObject({'videoMessage': msg.get('videoMessage')})
        if truthy(getp(msg, 'locationMessage')):
            return JSObject({'locationMessage': msg.get('locationMessage')})
        if 'contentText' in msg:
            conversation = msg.get('contentText')
        elif 'hydratedContentText' in msg:
            conversation = msg.get('hydratedContentText')
        else:
            conversation = ''
        return JSObject({'conversation': conversation})

    content = normalizeMessageContent(content)
    buttonsMessage = getp(content, 'buttonsMessage')
    if truthy(buttonsMessage):
        return extractFromTemplateMessage(buttonsMessage)
    template = getp(content, 'templateMessage')
    for field in ('hydratedFourRowTemplate', 'hydratedTemplate', 'fourRowTemplate'):
        inner = getp(template, field)
        if truthy(inner):
            return extractFromTemplateMessage(inner)
    return content


def getDevice(identifier):
    text = jsString(identifier)
    if DEVICE_IOS_RE.fullmatch(text):
        return 'ios'
    if DEVICE_WEB_RE.fullmatch(text):
        return 'web'
    if DEVICE_ANDROID_RE.fullmatch(text):
        return 'android'
    if DEVICE_DESKTOP_PREFIX_RE.match(text) or DEVICE_DESKTOP_RE.fullmatch(text):
        return 'desktop'
    return 'unknown'


def updateMessageWithReceipt(msg, receipt):
    if msg.get('userReceipt') is None:
        msg['userReceipt'] = []
    receipts = msg.get('userReceipt')
    existing = None
    for entry in receipts:
        if getp(entry, 'userJid') == getp(receipt, 'userJid'):
            existing = entry
            break
    if existing is not None:
        existing.update(receipt)
    else:
        receipts.append(receipt)


def updateMessageWithReaction(msg, reaction):
    authorID = keyAuthor(getp(reaction, 'key'))
    reactions = [r for r in nullish(msg.get('reactions'), []) if keyAuthor(getp(r, 'key')) != authorID]
    reaction['text'] = jsOr(getp(reaction, 'text'), '')
    reactions.append(reaction)
    msg['reactions'] = reactions


def updateMessageWithPollUpdate(msg, update):
    authorID = keyAuthor(getp(update, 'pollUpdateMessageKey'))
    reactions = [r for r in nullish(msg.get('pollUpdates'), []) if keyAuthor(getp(r, 'pollUpdateMessageKey')) != authorID]
    selected = getp(getp(update, 'vote'), 'selectedOptions')
    if isinstance(selected, (list, str)) and len(selected) > 0:
        reactions.append(update)
    msg['pollUpdates'] = reactions


def updateMessageWithEventResponse(msg, update):
    authorID = keyAuthor(getp(update, 'eventResponseMessageKey'))
    responses = [r for r in nullish(msg.get('eventResponses'), []) if keyAuthor(getp(r, 'eventResponseMessageKey')) != authorID]
    responses.append(update)
    msg['eventResponses'] = responses


def getAggregateVotesInPollMessage(data, meId=None):
    message = getp(data, 'message')
    pollUpdates = getp(data, 'pollUpdates')
    opts = jsOr(
        getp(getp(message, 'pollCreationMessage'), 'options'),
        getp(getp(message, 'pollCreationMessageV2'), 'options'),
        getp(getp(message, 'pollCreationMessageV3'), 'options'),
        getp(getp(message, 'pollCreationMessageV5'), 'options'),
        getp(getp(message, 'pollCreationMessageV6'), 'options'),
        []
    )
    voteHashMap = {}
    for opt in jsList(opts):
        optionName = jsOr(getp(opt, 'optionName'), '')
        digest = bufToStr(sha256(jsString(optionName).encode('utf-8')))
        voteHashMap[digest] = JSObject({'name': optionName, 'voters': []})
    for update in nullish(pollUpdates, []):
        vote = getp(update, 'vote')
        if not truthy(vote):
            continue
        for option in nullish(getp(vote, 'selectedOptions'), []):
            digest = bufToStr(option)
            data_ = voteHashMap.get(digest)
            if data_ is None:
                voteHashMap[digest] = JSObject({'name': 'Unknown', 'voters': []})
                data_ = voteHashMap[digest]
            data_.get('voters').append(keyAuthor(getp(update, 'pollUpdateMessageKey'), meId))
    return list(voteHashMap.values())


def getAggregateResponsesInEventMessage(data, meId=None):
    responseMap = JSObject()
    for responseType in ('GOING', 'NOT_GOING', 'MAYBE'):
        responseMap[responseType] = JSObject({'response': responseType, 'responders': []})
    for update in nullish(getp(data, 'eventResponses'), []):
        responseType = jsOr(getp(update, 'eventResponse'), 'UNKNOWN')
        if responseType != 'UNKNOWN' and responseType in responseMap:
            responseMap[responseType].get('responders').append(keyAuthor(getp(update, 'eventResponseMessageKey'), meId))
    return list(responseMap.values())


def aggregateMessageKeysNotFromMe(keys):
    keyMap = {}
    for key in jsList(keys):
        remoteJid = getp(key, 'remoteJid')
        participant = getp(key, 'participant')
        if not truthy(getp(key, 'fromMe')):
            uqKey = jsString(remoteJid) + ':' + jsString(jsOr(participant, ''))
            if uqKey not in keyMap:
                keyMap[uqKey] = JSObject({'jid': remoteJid, 'participant': participant, 'messageIds': []})
            keyMap[uqKey].get('messageIds').append(getp(key, 'id'))
    return list(keyMap.values())


async def downloadMediaMessage(message, type, options, ctx):
    async def downloadMsg():
        mContent = extractMessageContent(getp(message, 'message'))
        if not truthy(mContent):
            raise httpBoom('No message present', 400, message)
        contentType = getContentType(mContent)
        mediaType = contentType.replace('Message', '', 1) if contentType is not None else None
        media = getp(mContent, contentType)
        if not isinstance(media, dict) or ('url' not in media and 'thumbnailDirectPath' not in media):
            raise Boom('"' + jsString(contentType) + '" message is not a media message')
        if 'thumbnailDirectPath' in media and 'url' not in media:
            download = JSObject({'directPath': media.get('thumbnailDirectPath'), 'mediaKey': media.get('mediaKey')})
            mediaType = 'thumbnail-link'
        else:
            download = media
        stream = await downloadContentFromMessage(download, mediaType, options)
        if type == 'buffer':
            return await toBuffer(stream)
        return stream

    try:
        return await downloadMsg()
    except Exception as error:
        status = getattr(error, 'status', None)
        if truthy(ctx) and isinstance(status, int) and not isinstance(status, bool) and status in REUPLOAD_REQUIRED_STATUS:
            logCall(getp(ctx, 'logger'), 'info', JSObject({'key': getp(message, 'key')}), 'sending reupload media request...')
            message = await maybeAwait(getp(ctx, 'reuploadRequest')(message))
            return await downloadMsg()
        raise


def assertMediaContent(content):
    content = extractMessageContent(content)
    mediaContent = jsOr(
        getp(content, 'documentMessage'),
        getp(content, 'imageMessage'),
        getp(content, 'videoMessage'),
        getp(content, 'audioMessage'),
        getp(content, 'stickerMessage')
    )
    if not truthy(mediaContent):
        raise httpBoom('given message is not a media message', 400, content)
    return mediaContent


def shouldIncludeBizBinaryNode(message):
    interactive = getp(message, 'interactiveMessage')
    return bool(
        truthy(getp(message, 'buttonsMessage'))
        or truthy(getp(message, 'listMessage'))
        or truthy(getp(message, 'templateMessage'))
        or (truthy(interactive) and (truthy(getp(interactive, 'nativeFlowMessage')) or truthy(getp(interactive, 'carouselMessage'))))
    )
