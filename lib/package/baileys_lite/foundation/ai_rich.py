import base64
import decimal
import math
import re
import string

from baileys_lite.foundation.node_crypto import randomUUID
from baileys_lite.foundation.node_runtime import JSError, JSObject, now


class _JSNull:
    __slots__ = ()


JS_NULL = _JSNull()

MESSAGE_EDIT_TYPE = 14

_JS_WS = frozenset(
    '\t\n\x0b\x0c\r \xa0     　﻿'
    + ''.join(chr(c) for c in range(0x2000, 0x200B))
)
_JS_WS_STR = ''.join(sorted(_JS_WS))

_ASCII_LETTERS = frozenset(string.ascii_letters)
_ASCII_DIGITS = frozenset(string.digits)
_IDENT_CHARS = _ASCII_LETTERS | _ASCII_DIGITS | frozenset('_$')
_CSS_IDENT_CHARS = _IDENT_CHARS | frozenset('-')
_HTML_IDENT_CHARS = _IDENT_CHARS | frozenset(':-')
_IDENT_START = _ASCII_LETTERS | frozenset('_$')
_NUMBER_CONT = _ASCII_DIGITS | frozenset('._')

_OBJECT_PROTO_NAMES = frozenset([
    'constructor', '__defineGetter__', '__defineSetter__', 'hasOwnProperty', '__lookupGetter__',
    '__lookupSetter__', 'isPrototypeOf', 'propertyIsEnumerable', 'toString', 'valueOf', '__proto__',
    'toLocaleString',
])

_KEYWORD_SETS = JSObject({
    'javascript': frozenset(['break','case','catch','continue','debugger','delete','do','else','finally','for','function','if','in','instanceof','new','return','switch','this','throw','typeof','var','void','while','with','true','false','null','undefined','class','const','let','super','extends','export','import','yield','static','async','await','get','set']),
    'typescript': frozenset(['abstract','any','as','asserts','bigint','boolean','declare','enum','implements','infer','interface','is','keyof','module','namespace','never','readonly','require','number','object','override','private','protected','public','satisfies','string','symbol','type','unknown','using','from','break','case','catch','continue','do','else','finally','for','function','if','new','return','switch','this','throw','try','var','void','while','class','const','let','extends','import','export','async','await']),
    'python': frozenset(['False','None','True','and','as','assert','async','await','break','class','continue','def','del','elif','else','except','finally','for','from','global','if','import','in','is','lambda','nonlocal','not','or','pass','raise','return','try','while','with','yield']),
    'java': frozenset(['abstract','assert','boolean','break','byte','case','catch','char','class','const','continue','default','do','double','else','enum','extends','final','finally','float','for','goto','if','implements','import','instanceof','int','interface','long','native','new','package','private','protected','public','return','short','static','strictfp','super','switch','synchronized','this','throw','throws','transient','try','void','volatile','while']),
    'go': frozenset(['break','case','chan','const','continue','default','defer','else','fallthrough','for','func','go','goto','if','import','interface','map','package','range','return','select','struct','switch','type','var']),
    'golang': frozenset(['break','case','chan','const','continue','default','defer','else','fallthrough','for','func','go','goto','if','import','interface','map','package','range','return','select','struct','switch','type','var']),
    'c': frozenset(['auto','break','case','char','const','continue','default','do','double','else','enum','extern','float','for','goto','if','int','long','register','return','short','signed','sizeof','static','struct','switch','typedef','union','unsigned','void','volatile','while']),
    'cpp': frozenset(['alignas','alignof','and','auto','bool','break','case','catch','class','const','constexpr','continue','delete','do','double','else','enum','explicit','export','extern','false','float','for','friend','if','inline','int','long','mutable','namespace','new','noexcept','nullptr','operator','private','protected','public','return','short','signed','sizeof','static','struct','switch','template','this','throw','true','try','typedef','typename','union','unsigned','using','virtual','void','while']),
    'php': frozenset(['abstract','and','array','as','break','callable','case','catch','class','clone','const','continue','declare','default','do','echo','else','elseif','empty','enddeclare','endfor','endforeach','endif','endswitch','endwhile','extends','final','finally','fn','for','foreach','function','global','goto','if','implements','include','include_once','instanceof','interface','match','namespace','new','null','or','private','protected','public','require','require_once','return','static','switch','throw','trait','try','use','var','while','yield']),
    'rust': frozenset(['as','break','const','continue','crate','else','enum','extern','false','fn','for','if','impl','in','let','loop','match','mod','move','mut','pub','ref','return','self','Self','static','struct','super','trait','true','type','unsafe','use','where','while']),
    'html': frozenset(['html','head','body','div','span','p','a','img','video','audio','script','style','link','meta','form','input','button','table','tr','td','th','ul','ol','li','section','article','header','footer','nav','main']),
    'bash': frozenset(['if','then','else','elif','fi','for','while','do','done','case','esac','function','in','select','until','break','continue','return','export','readonly','local','declare']),
})

HL = JSObject({'DEFAULT': 0, 'KEYWORD': 1, 'METHOD': 2, 'STRING': 3, 'NUMBER': 4, 'COMMENT': 5})
HL_NAME = ['DEFAULT', 'KEYWORD', 'METHOD', 'STR', 'NUMBER', 'COMMENT']


def _typeError(message):
    return JSError(message, name='TypeError')


def _jsTruthy(value):
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0 and value == value
    if isinstance(value, str):
        return len(value) > 0
    return True


def _jsOr(*values):
    for value in values[:-1]:
        if _jsTruthy(value):
            return value
    return values[-1]


def _jsNullish(*values):
    for value in values[:-1]:
        if value is not None:
            return value
    return values[-1]


def _jsBool(value):
    return bool(_jsTruthy(value))


def _charAt(text, index):
    if 0 <= index < len(text):
        return text[index]
    return ''


def _isJsSpace(ch):
    return ch in _JS_WS


def _jsTrim(text):
    return text.strip(_JS_WS_STR)


def _get(obj, key):
    if obj is None:
        raise _typeError("Cannot read properties of undefined (reading '" + key + "')")
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


def _optGet(obj, key):
    if obj is None:
        return None
    return _get(obj, key)


def _clientGet(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _flag(options, key):
    value = _get(options, key) if options is not None else None
    return True if value is None else value


def _linkFlags(options):
    return _flag(options, 'hyperlink'), _flag(options, 'citation'), _flag(options, 'latex')


def _isStrOrBuffer(value):
    return isinstance(value, (str, bytes, bytearray))


def _isBuffer(value):
    return isinstance(value, (bytes, bytearray))


def _index0(value):
    if isinstance(value, (list, str)) and len(value) > 0:
        return value[0]
    return None


def _jsNumber(value):
    if value is None:
        return math.nan
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = _jsTrim(value)
        if s == '':
            return 0.0
        if re.fullmatch(r'[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?', s):
            return float(s)
        if re.fullmatch(r'[+-]?Infinity', s):
            return -math.inf if s.startswith('-') else math.inf
        if re.fullmatch(r'0[xX][0-9a-fA-F]+', s):
            return float(int(s[2:], 16))
        if re.fullmatch(r'0[oO][0-7]+', s):
            return float(int(s[2:], 8))
        if re.fullmatch(r'0[bB][01]+', s):
            return float(int(s[2:], 2))
    return math.nan


def _newLayout(name, data, extra=None):
    out = JSObject()
    if extra is not None:
        out.update(extra)
    key = 'primitives' if isinstance(data, list) else 'primitive'
    out['view_model'] = JSObject({key: data, '__typename': 'GenAI' + name + 'LayoutViewModel'})
    return out


def _mediaUrl(value):
    if _isBuffer(value):
        return 'data:application/octet-stream;base64,' + base64.b64encode(bytes(value)).decode('ascii')
    if isinstance(value, dict) and isinstance(value.get('url'), str):
        return value.get('url')
    if isinstance(value, str):
        return value
    return None


def _toEntity(kind, ie):
    if kind == 'hyperlink':
        return JSObject({
            'key': ie.get('key'),
            'metadata': JSObject({
                'display_name': ie.get('text'),
                'is_trusted': ie.get('is_trusted'),
                'url': ie.get('url'),
                '__typename': 'GenAIInlineLinkItem',
            }),
        })
    if kind == 'citation':
        return JSObject({
            'key': ie.get('key'),
            'metadata': JSObject({
                'reference_id': ie.get('reference_id'),
                'reference_url': ie.get('url'),
                'reference_title': ie.get('url'),
                'reference_display_name': ie.get('url'),
                'sources': [],
                '__typename': 'GenAISearchCitationItem',
            }),
        })
    if kind == 'latex':
        return JSObject({
            'key': ie.get('key'),
            'metadata': JSObject({
                'latex_expression': ie.get('text'),
                'latex_image': JSObject({
                    'url': ie.get('url'),
                    'width': _jsOr(_jsNumber(ie.get('width')), 100.0),
                    'height': _jsOr(_jsNumber(ie.get('height')), 100.0),
                }),
                'font_height': _jsOr(_jsNumber(ie.get('font_height')), 83.333333333333),
                'padding': _jsOr(_jsNumber(ie.get('padding')), 15.0),
                '__typename': 'GenAILatexItem',
            }),
        })
    return None


def _extractInlineEntities(text, hyperlink=True, citation=True, latex=True):
    inlineEntities = []
    result = ''
    last = 0
    citationIndex = 1
    hyperlinkIndex = 0
    latexIndex = 0
    stack = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == '[' and _charAt(text, i - 1) != '\\':
            stack.append(i)
            i += 1
            continue
        if ch == ']' and (_charAt(text, i + 1) == '(' or _charAt(text, i + 1) == '<'):
            start = stack.pop() if stack else None
            if start is None:
                i += 1
                continue
            opener = text[i + 1]
            closer = ')' if opener == '(' else '>'
            kind = 'link' if opener == '(' else 'latex'
            end = i + 2
            depth = 1
            while end < n and depth:
                if text[end] == opener and text[end - 1] != '\\':
                    depth += 1
                elif text[end] == closer and text[end - 1] != '\\':
                    depth -= 1
                end += 1
            if depth:
                i += 1
                continue

            raw = _jsTrim(text[start + 1:i])
            url = _jsTrim(text[i + 2:end - 1])

            if kind == 'latex':
                if not _jsTruthy(latex):
                    i += 1
                    continue
                pieces = raw.split('|')
                txt = pieces[0] if len(pieces) > 0 else ''
                width = pieces[1] if len(pieces) > 1 else None
                height = pieces[2] if len(pieces) > 2 else None
                fontHeight = pieces[3] if len(pieces) > 3 else None
                padding = pieces[4] if len(pieces) > 4 else None
                key = '_LATEX_' + str(latexIndex)
                latexIndex += 1
                tag = '{{' + key + '}}' + _jsOr(txt, 'image') + '{{/' + key + '}}'
                entityType = 'latex'
                ie = JSObject({'key': key, 'text': txt, 'url': url, 'width': width, 'height': height,
                               'font_height': fontHeight, 'padding': padding})
            elif raw:
                if not _jsTruthy(hyperlink):
                    i += 1
                    continue
                trusted = not url.startswith('!')
                key = '_HYPERLINK_' + str(hyperlinkIndex)
                hyperlinkIndex += 1
                shown = url if trusted else url[1:]
                tag = '{{' + key + '}}' + shown + '{{/' + key + '}}'
                entityType = 'hyperlink'
                ie = JSObject({'key': key, 'text': raw, 'url': shown, 'is_trusted': trusted})
            else:
                if not _jsTruthy(citation):
                    i += 1
                    continue
                key = '_CITATION_' + str(citationIndex - 1)
                tag = '{{' + key + '}}' + url + '{{/' + key + '}}'
                entityType = 'citation'
                ie = JSObject({'reference_id': citationIndex, 'key': key, 'text': '', 'url': url})
                citationIndex += 1

            result += text[last:start] + tag
            last = end
            entity = _toEntity(entityType, ie)
            if entity is not None:
                inlineEntities.append(entity)
            i = end
            continue
        i += 1
    result += text[last:]
    return result, inlineEntities


def _toTableMetadata(rows, hyperlink=True, citation=True, latex=True):
    if not isinstance(rows, list) or not all(
        isinstance(row, list) and all(isinstance(cell, str) for cell in row) for row in rows
    ):
        raise _typeError('Table must be a nested array of strings')
    if not rows:
        raise _typeError("Cannot read properties of undefined (reading 'length')")
    header = rows[0]
    body = rows[1:]
    maxLen = max([len(header)] + [len(r) for r in body])

    def normalize(r):
        return list(r) + [''] * (maxLen - len(r))

    unified = [JSObject({'is_header': True, 'cells': normalize(header)})]
    for r in body:
        unified.append(JSObject({'is_header': False, 'cells': normalize(r)}))

    finalRows = []
    for row in unified:
        markdownCells = []
        for cell in row.get('cells'):
            extractedText, entities = _extractInlineEntities(cell, hyperlink, citation, latex)
            markdownCell = JSObject({'text': extractedText})
            if entities:
                markdownCell['inline_entities'] = entities
            markdownCells.append(markdownCell)
        newRow = JSObject(row)
        if any(c.get('inline_entities') for c in markdownCells):
            newRow['markdown_cells'] = markdownCells
        finalRows.append(newRow)

    rowItems = []
    for r in finalRows:
        item = JSObject({'items': r.get('cells')})
        if r.get('is_header'):
            item['isHeading'] = True
        rowItems.append(item)
    return JSObject({'title': '', 'rows': rowItems, 'unifiedRows': finalRows})


def tokenizeAIRichCode(code, lang='javascript'):
    key = (lang if _jsTruthy(lang) else '').lower()
    if not key or key in ('txt', 'text', 'plaintext'):
        return [JSObject({'codeContent': code, 'highlightType': HL['DEFAULT']})]

    keywordSet = _KEYWORD_SETS.get(key)
    keywordsBroken = key in _OBJECT_PROTO_NAMES
    keywords = frozenset() if keywordSet is None else keywordSet
    if key == 'css':
        isIdentifier = _CSS_IDENT_CHARS.__contains__
    elif key == 'html':
        isIdentifier = _HTML_IDENT_CHARS.__contains__
    else:
        isIdentifier = _IDENT_CHARS.__contains__

    tokens = []

    def push(content, kind):
        if not content:
            return
        last = tokens[-1] if tokens else None
        if last is not None and last.get('highlightType') == kind:
            last['codeContent'] += content
        else:
            tokens.append(JSObject({'codeContent': content, 'highlightType': kind}))

    n = len(code)
    i = 0
    while i < n:
        c = code[i]
        if _isJsSpace(c):
            s = i
            while i < n and _isJsSpace(code[i]):
                i += 1
            push(code[s:i], HL['DEFAULT'])
            continue
        if (c == '/' and _charAt(code, i + 1) == '/') or (c == '#' and key in ('python', 'bash')):
            s = i
            while i < n and code[i] != '\n':
                i += 1
            push(code[s:i], HL['COMMENT'])
            continue
        if c == '"' or c == "'" or c == '`':
            s = i
            q = c
            i += 1
            while i < n:
                if code[i] == '\\' and i + 1 < n:
                    i += 2
                elif code[i] == q:
                    i += 1
                    break
                else:
                    i += 1
            push(code[s:i], HL['STRING'])
            continue
        if c in _ASCII_DIGITS:
            s = i
            while i < n and code[i] in _NUMBER_CONT:
                i += 1
            push(code[s:i], HL['NUMBER'])
            continue
        if c in _IDENT_START:
            s = i
            while i < n and isIdentifier(code[i]):
                i += 1
            word = code[s:i]
            if keywordsBroken:
                raise _typeError('keywords.has is not a function')
            kind = HL['DEFAULT']
            if word in keywords:
                kind = HL['KEYWORD']
            else:
                j = i
                while j < n and _isJsSpace(code[j]):
                    j += 1
                if _charAt(code, j) == '(':
                    kind = HL['METHOD']
            push(word, kind)
            continue
        push(c, HL['DEFAULT'])
        i += 1
    return tokens


_JSON_SPECIAL = {'"': '\\"', '\\': '\\\\', '\b': '\\b', '\f': '\\f', '\n': '\\n', '\r': '\\r', '\t': '\\t'}
_JSON_ESCAPE_RE = re.compile(r'["\\\x00-\x1f\ud800-\udfff]')


def _jsonEscapeChar(match):
    ch = match.group(0)
    if ch in _JSON_SPECIAL:
        return _JSON_SPECIAL[ch]
    return '\\u%04x' % ord(ch)


def _jsonString(value):
    return '"' + _JSON_ESCAPE_RE.sub(_jsonEscapeChar, value) + '"'


def _numberText(value):
    if value != value or value in (math.inf, -math.inf):
        return 'null'
    if value == 0:
        return '0'
    if isinstance(value, int):
        return str(value)
    sign = '-' if value < 0 else ''
    sign_, digits, exponent = decimal.Decimal(repr(abs(value))).as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    k = len(digits)
    n = exponent + k
    text = ''.join(str(d) for d in digits)
    if k <= n <= 21:
        body = text + '0' * (n - k)
    elif 0 < n <= 21:
        body = text[:n] + '.' + text[n:]
    elif -6 < n <= 0:
        body = '0.' + '0' * (-n) + text
    else:
        e = n - 1
        expo = ('+' if e >= 0 else '-') + str(abs(e))
        if k == 1:
            body = text + 'e' + expo
        else:
            body = text[0] + '.' + text[1:] + 'e' + expo
    return sign + body


def _arrayIndexKey(key):
    return isinstance(key, str) and re.fullmatch(r'[0-9]+', key) and (key == '0' or not key.startswith('0')) and int(key) < 4294967295


def _orderedKeys(value):
    indexKeys = sorted((k for k in value if _arrayIndexKey(k)), key=int)
    others = [k for k in value if not _arrayIndexKey(k)]
    return indexKeys + others


def _jsonValue(value):
    if value is None or value is JS_NULL:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return _jsonString(value)
    if isinstance(value, (int, float)):
        return _numberText(value)
    if isinstance(value, (bytes, bytearray)):
        return '{"type":"Buffer","data":[' + ','.join(str(b) for b in value) + ']}'
    if isinstance(value, list):
        return '[' + ','.join('null' if v is None else _jsonValue(v) for v in value) + ']'
    if isinstance(value, dict):
        parts = []
        for key in _orderedKeys(value):
            item = value[key]
            if item is None:
                continue
            parts.append(_jsonString(key) + ':' + _jsonValue(item))
        return '{' + ','.join(parts) + '}'
    raise _typeError('Unsupported value in JSON serialization')


def _jsonStringify(value):
    return _jsonValue(value)


class AIRichBuilder:

    def __init__(self, client=None):
        self._client = client
        self._title = ''
        self._footer = ''
        self._contextInfo = JSObject()
        self._submessages = []
        self._sections = []
        self._richResponseSources = []
        self._embeddedScreens = []

    def setTitle(self, title):
        if not isinstance(title, str):
            raise _typeError('Title must be a string')
        self._title = title
        return self

    def setFooter(self, footer):
        if not isinstance(footer, str):
            raise _typeError('Footer must be a string')
        self._footer = footer
        return self

    def setContextInfo(self, obj):
        if not isinstance(obj, dict):
            raise _typeError('ContextInfo must be a plain object')
        self._contextInfo = obj
        return self

    def addText(self, text, options=None):
        if not isinstance(text, str):
            raise _typeError('Text must be a string')
        hyperlink, citation, latex = _linkFlags(options)
        extractedText, inlineEntities = _extractInlineEntities(text, hyperlink, citation, latex)
        self._submessages.append(JSObject({'messageType': 2, 'messageText': extractedText}))
        primitive = JSObject({'text': extractedText})
        if inlineEntities:
            primitive['inline_entities'] = inlineEntities
        primitive['__typename'] = 'GenAIMarkdownTextUXPrimitive'
        self._sections.append(_newLayout('Single', primitive))
        return self

    def addCode(self, language, code):
        if not isinstance(language, str) or not isinstance(code, str):
            raise _typeError('Language and code must be a string')
        codeBlocks = tokenizeAIRichCode(code, language)
        self._submessages.append(JSObject({
            'messageType': 5,
            'codeMetadata': JSObject({'codeLanguage': language, 'codeBlocks': codeBlocks}),
        }))
        self._sections.append(_newLayout('Single', JSObject({
            'language': language,
            'code_blocks': [JSObject({'content': b.get('codeContent'), 'type': HL_NAME[b.get('highlightType')]}) for b in codeBlocks],
            '__typename': 'GenAICodeUXPrimitive',
        })))
        return self

    def addTable(self, table, options=None):
        if not isinstance(table, list):
            raise _typeError('Table must be an array')
        hyperlink, citation, latex = _linkFlags(options)
        meta = _toTableMetadata(table, hyperlink, citation, latex)
        self._submessages.append(JSObject({
            'messageType': 4,
            'tableMetadata': JSObject({'title': meta.get('title'), 'rows': meta.get('rows')}),
        }))
        self._sections.append(_newLayout('Single', JSObject({'rows': meta.get('unifiedRows'), '__typename': 'GenATableUXPrimitive'})))
        return self

    def addImage(self, image):
        valid = _isStrOrBuffer(image) or (isinstance(image, list) and all(_isStrOrBuffer(v) for v in image))
        if not valid:
            raise _typeError('image must be a string | buffer | array of string/buffer')
        items = image if isinstance(image, list) else [image]
        urls = [u for u in (_mediaUrl(v) for v in items) if _jsTruthy(u)]
        first = urls[0] if urls else None
        self._submessages.append(JSObject({
            'messageType': 1,
            'gridImageMetadata': JSObject({
                'gridImageUrl': JSObject({'imagePreviewUrl': first}),
                'imageUrls': [JSObject({'imagePreviewUrl': u, 'imageHighResUrl': u, 'sourceUrl': u}) for u in urls],
            }),
        }))
        for url in urls:
            self._sections.append(_newLayout('Single', JSObject({
                'media': JSObject({'url': url, 'mime_type': 'image/png'}),
                'imagine_type': 'IMAGE',
                'status': JSObject({'status': 'READY'}),
                '__typename': 'GenAIImaginePrimitive',
            })))
        return self

    def addVideo(self, video):
        items = video if isinstance(video, list) else [video]
        self._submessages.append(JSObject({'messageType': 2, 'messageText': '[ VIDEO ]'}))
        for item in items:
            isObj = isinstance(item, dict)
            url = _mediaUrl(_get(item, 'url') if isObj else item)
            media = JSObject({
                'url': url,
                'mime_type': _jsOr(_get(item, 'mimeType') if isObj else None, 'video/mp4'),
                'file_length': _jsOr(_get(item, 'fileLength') if isObj else None, 0),
                'duration': _jsOr(_get(item, 'duration') if isObj else None, 0),
            })
            self._sections.append(_newLayout('Single', JSObject({
                'media': media,
                'imagine_type': 'ANIMATE',
                'status': JSObject({'status': 'READY'}),
                'thumbnail': JSObject({'raw_media': _get(item, 'thumbnail') if isObj else None}),
                '__typename': 'GenAIImaginePrimitive',
            })))
        return self

    def addSource(self, sources=None):
        if sources is None:
            sources = []
        isFlat = isinstance(sources, list) and all(isinstance(item, str) for item in sources)
        items = [sources] if isFlat else sources
        if not (isinstance(items, list) and all(
            isinstance(item, list) and all(isinstance(v, str) for v in item) for item in items
        )):
            raise _typeError('Sources must be a string array or an array of string arrays')
        mapped = []
        for item in items:
            icon = item[0] if len(item) > 0 else None
            url = item[1] if len(item) > 1 else None
            text = item[2] if len(item) > 2 else None
            mapped.append(JSObject({
                'source_type': 'THIRD_PARTY',
                'source_display_name': _jsNullish(text, ''),
                'source_subtitle': 'AI',
                'source_url': _jsNullish(url, ''),
                'favicon': JSObject({'url': _jsNullish(icon, ''), 'mime_type': 'image/jpeg', 'width': 16, 'height': 16}),
            }))
        self._sections.append(_newLayout('Single', JSObject({'sources': mapped, '__typename': 'GenAISearchResultPrimitive'})))
        return self

    def addProduct(self, data=None):
        if data is None:
            data = JSObject()
        items = data if isinstance(data, list) else [data]
        self._submessages.append(JSObject({'messageType': 2, 'messageText': '[ PRODUCT ]'}))
        products = []
        for item in items:
            products.append(JSObject({
                'title': _get(item, 'title'),
                'brand': _get(item, 'brand'),
                'price': _get(item, 'price'),
                'sale_price': _get(item, 'sale_price'),
                'product_url': _jsNullish(_get(item, 'product_url'), _get(item, 'url')),
                'image': JSObject({'url': _mediaUrl(_jsNullish(_get(item, 'image_url'), _get(item, 'image')))}),
                'additional_images': [JSObject({'url': _mediaUrl(_jsNullish(_get(item, 'icon_url'), _get(item, 'icon')))})],
                '__typename': 'GenAIProductItemCardPrimitive',
            }))
        if isinstance(data, list):
            self._sections.append(_newLayout('HScroll', products))
        else:
            self._sections.append(_newLayout('Single', products[0]))
        return self

    def addPost(self, data=None):
        if data is None:
            data = JSObject()
        posts = data if isinstance(data, list) else [data]
        self._submessages.append(JSObject({'messageType': 2, 'messageText': '[ POST ]'}))
        primitives = []
        for p in posts:
            primitives.append(JSObject({
                'title': _jsNullish(_get(p, 'title'), ''),
                'subtitle': _jsNullish(_get(p, 'subtitle'), ''),
                'username': _jsNullish(_get(p, 'username'), ''),
                'profile_picture_url': _mediaUrl(_jsNullish(_get(p, 'profile_picture_url'), _get(p, 'profile_url'), _get(p, 'profile'))),
                'is_verified': _jsBool(_jsOr(_get(p, 'is_verified'), _get(p, 'verified'))),
                'thumbnail_url': _mediaUrl(_jsNullish(_get(p, 'thumbnail_url'), _get(p, 'thumbnail'))),
                'post_caption': _jsNullish(_get(p, 'post_caption'), _get(p, 'caption'), ''),
                'likes_count': _jsNullish(_get(p, 'likes_count'), _get(p, 'like'), 0),
                'comments_count': _jsNullish(_get(p, 'comments_count'), _get(p, 'comment'), 0),
                'shares_count': _jsNullish(_get(p, 'shares_count'), _get(p, 'share'), 0),
                'post_url': _jsNullish(_get(p, 'post_url'), _get(p, 'url'), ''),
                'post_deeplink': _jsNullish(_get(p, 'post_deeplink'), _get(p, 'deeplink'), ''),
                'source_app': _jsOr(_get(p, 'source_app'), _get(p, 'source'), 'INSTAGRAM'),
                'footer_label': _jsNullish(_get(p, 'footer_label'), _get(p, 'footer'), ''),
                'footer_icon': _mediaUrl(_jsNullish(_get(p, 'footer_icon'), _get(p, 'icon'))),
                'is_carousel': len(posts) > 1,
                'orientation': _jsNullish(_get(p, 'orientation'), 'LANDSCAPE'),
                'post_type': _jsNullish(_get(p, 'post_type'), 'VIDEO'),
                '__typename': 'GenAIPostPrimitive',
            }))
        self._sections.append(_newLayout('HScroll', primitives))
        return self

    def addReels(self, data=None):
        if data is None:
            data = []
        items = data if isinstance(data, list) else [data]
        reels = []
        for item in items:
            avatar = _mediaUrl(_jsNullish(_get(item, 'profileIconUrl'), _get(item, 'profile_url'), _get(item, 'profile')))
            thumbnail = _mediaUrl(_jsNullish(_get(item, 'thumbnailUrl'), _get(item, 'thumbnail')))
            reels.append((item, avatar, thumbnail))
        self._submessages.append(JSObject({
            'messageType': 9,
            'contentItemsMetadata': JSObject({
                'contentType': 1,
                'itemsMetadata': [
                    JSObject({'reelItem': JSObject({
                        'title': _jsNullish(_get(item, 'username'), ''),
                        'profileIconUrl': avatar,
                        'thumbnailUrl': thumbnail,
                        'videoUrl': _jsNullish(_get(item, 'videoUrl'), _get(item, 'url'), ''),
                    })})
                    for item, avatar, thumbnail in reels
                ],
            }),
        }))
        for idx, (item, avatar, thumbnail) in enumerate(reels):
            self._richResponseSources.append(JSObject({
                'thumbnailCdnUrl': thumbnail,
                'sourceProviderUrl': _jsNullish(_get(item, 'videoUrl'), _get(item, 'url'), ''),
                'sourceQuery': '',
                'faviconCdnUrl': avatar,
                'citationNumber': idx + 1,
                'sourceTitle': _jsNullish(_get(item, 'username'), ''),
            }))
        self._sections.append(_newLayout('HScroll', [
            JSObject({
                'reels_url': _jsNullish(_get(item, 'videoUrl'), _get(item, 'url'), ''),
                'thumbnail_url': thumbnail,
                'creator': _jsNullish(_get(item, 'username'), _get(item, 'title'), ''),
                'avatar_url': avatar,
                'reels_title': _jsNullish(_get(item, 'reels_title'), _get(item, 'title'), ''),
                'likes_count': _jsNullish(_get(item, 'likes_count'), _get(item, 'like'), 0),
                'shares_count': _jsNullish(_get(item, 'shares_count'), _get(item, 'share'), 0),
                'view_count': _jsNullish(_get(item, 'view_count'), _get(item, 'view'), 0),
                'reel_source': _jsNullish(_get(item, 'reel_source'), _get(item, 'source'), 'IG'),
                'is_verified': _jsBool(_jsOr(_get(item, 'is_verified'), _get(item, 'verified'))),
                '__typename': 'GenAIReelPrimitive',
            })
            for item, avatar, thumbnail in reels
        ]))
        return self

    def addTip(self, text):
        self._submessages.append(JSObject({'messageType': 2, 'messageText': text}))
        self._sections.append(_newLayout('Single', JSObject({'text': text, '__typename': 'GenAIMetadataTextPrimitive'})))
        return self

    def addSuggest(self, suggestion, options=None):
        scroll = _flag_default(options, 'scroll', True)
        layout = _get(options, 'layout') if options is not None else None
        if not (isinstance(suggestion, str) or (isinstance(suggestion, list) and all(isinstance(v, str) for v in suggestion))):
            raise _typeError('Suggestion must be a string or array of strings')
        texts = suggestion if isinstance(suggestion, list) else [suggestion]
        items = [JSObject({'prompt_text': text, 'prompt_type': 'SUGGESTED_PROMPT', '__typename': 'GenAIFollowUpSuggestionPillPrimitive'}) for text in texts]
        if layout is not None:
            kind = layout
        elif len(items) == 1:
            kind = 'Single'
        elif _jsTruthy(scroll):
            kind = 'HScroll'
        else:
            kind = 'ActionRow'
        data = (items[0] if items else None) if kind == 'Single' else items
        self._sections.append(_newLayout(kind, data, JSObject({'__typename': 'GenAIUnifiedResponseSection'})))
        return self

    def addProcess(self, title):
        if not isinstance(title, str):
            raise _typeError('Process title must be a string')
        self._submessages.append(JSObject({'messageType': 2, 'messageText': title}))
        self._sections.append(_newLayout('Single', JSObject({
            'icon': JS_NULL,
            'is_in_progress': True,
            'meta_search_apps': JS_NULL,
            'target_secondary_screen_id': JS_NULL,
            'target_secondary_screen_tab_id': JS_NULL,
            'title': title,
            '__typename': 'GenAIBotProgressStatusPrimitive',
        })))
        return self

    def addHtml(self, *args):
        args = list(args)
        options = JSObject()
        if args and isinstance(args[-1], dict):
            options = args.pop()
        url = _jsNullish(_get(options, 'url'), '')
        trustedSources = _jsNullish(_get(options, 'trustedSources'), [])

        if len(args) == 1 and isinstance(args[0], str):
            self._submessages.append(JSObject({'messageType': 2, 'messageText': '[ HTML ]'}))
            self._sections.append(_newLayout('Single', JSObject({
                'payload': args[0],
                'url': url,
                'trusted_sources': trustedSources,
                '__typename': 'GenAIaeacdsnwHtmlPrimitive',
            })))
            return self
        if not all(isinstance(item, list) and len(item) > 0 and isinstance(item[0], str) for item in args):
            raise _typeError('addHtml() expects a single HTML string, or one or more [html, title] pairs, with an optional trailing options object')
        if not self._embeddedScreens:
            self._embeddedScreens.append(JSObject({
                'title': 'Preview',
                'content': [JSObject({'__typename': 'FOAIDNixelButtonSheets', 'tabs': []})],
            }))
        tabs = self._embeddedScreens[0].get('content')[0].get('tabs')
        self._submessages.append(JSObject({'messageType': 2, 'messageText': '[ HTML ]'}))
        for item in args:
            payload = item[0]
            title = item[1] if len(item) > 1 else None
            tabId = 'tab_' + str(len(tabs))
            tabHeader = _jsNullish(title, 'Tab ' + str(len(tabs) + 1))
            section = _newLayout('Single', JSObject({
                'payload': payload,
                'url': url,
                'trusted_sources': trustedSources,
                '__typename': 'GenAIaeacdsnwHtmlPrimitive',
            }), JSObject({'__typename': 'GenAIUnifiedResponseSection'}))
            tabs.append(JSObject({
                'id': tabId,
                'tab_header': tabHeader,
                'sections': [section],
                'step_entries': [],
            }))
        return self

    def build(self, forwarded=True, quoted=None, quotedParticipant=None, botJid='0@bot'):
        if forwarded is None:
            forwarded = True
        if botJid is None:
            botJid = '0@bot'
        forward = JSObject({
            'forwardingScore': 1,
            'isForwarded': True,
            'forwardedAiBotMessageInfo': JSObject({'botJid': botJid}),
            'forwardOrigin': 4,
        }) if _jsTruthy(forwarded) else JSObject()
        if _jsTruthy(quoted):
            quotedKey = _optGet(quoted, 'key')
            qObj = JSObject({
                'stanzaId': _jsOr(_optGet(quotedKey, 'id'), _optGet(quoted, 'id')),
                'participant': _jsOr(quotedParticipant, _optGet(quotedKey, 'participant'), _optGet(quotedKey, 'remoteJid')),
                'quotedType': 0,
                'quotedMessage': _jsNullish(_optGet(quoted, 'message'), quoted),
            })
        else:
            qObj = JSObject()

        sections = list(self._sections)
        if _jsTruthy(self._footer):
            sections.append(_newLayout('Single', JSObject({'text': self._footer, '__typename': 'GenAIMetadataTextPrimitive'})))
        responseData = JSObject({'response_id': randomUUID(), 'sections': sections})
        if self._embeddedScreens:
            responseData['embedded_screens'] = self._embeddedScreens

        hasRenderableContent = len(sections) > 0 or len(self._embeddedScreens) > 0
        contextInfo = JSObject()
        contextInfo.update(forward)
        contextInfo.update(qObj)
        contextInfo.update(self._contextInfo)
        richResponseMessage = JSObject({
            'messageType': 1,
            'submessages': self._submessages,
            'contextInfo': contextInfo,
        })
        if hasRenderableContent:
            payload = _jsonStringify(responseData).encode('utf-8')
            richResponseMessage['unifiedResponse'] = JSObject({'data': base64.b64encode(payload).decode('ascii')})

        return JSObject({
            'messageContextInfo': JSObject({
                'deviceListMetadata': JSObject(),
                'deviceListMetadataVersion': 2,
                'botMetadata': JSObject({
                    'messageDisclaimerText': self._title,
                    'richResponseSourcesMetadata': JSObject({'sources': self._richResponseSources}),
                }),
            }),
            'botForwardedMessage': JSObject({'message': JSObject({'richResponseMessage': richResponseMessage})}),
        })

    def buildEdit(self, jid, targetKey, editedMessage):
        if not _jsTruthy(jid):
            raise JSError('A jid is required to edit an aiRich message')
        if not _jsTruthy(_optGet(targetKey, 'id')):
            raise JSError('A target message id is required to edit an aiRich message')
        if not _jsTruthy(editedMessage):
            raise JSError('editedMessage does not contain an aiRich response')
        return JSObject({
            'botForwardedMessage': JSObject({
                'message': JSObject({
                    'protocolMessage': JSObject({
                        'key': JSObject({'remoteJid': jid, 'fromMe': True, 'id': _get(targetKey, 'id')}),
                        'type': MESSAGE_EDIT_TYPE,
                        'editedMessage': editedMessage,
                        'timestampMs': now(),
                    }),
                }),
            }),
        })

    async def send(self, jid, options=None):
        if not _jsTruthy(self._client):
            raise JSError('AIRichBuilder.send() needs a client — use sock.aiRich() or new AIRichBuilder(sock)')
        forwarded = _get(options, 'forwarded') if options is not None else None
        quoted = _get(options, 'quoted') if options is not None else None
        quotedParticipant = _get(options, 'quotedParticipant') if options is not None else None
        botJid = _get(options, 'botJid') if options is not None else None
        sendOptions = JSObject()
        if isinstance(options, dict):
            excluded = ('forwarded', 'quoted', 'quotedParticipant', 'botJid')
            sendOptions.update((k, v) for k, v in options.items() if k not in excluded)
        sendMessage = _clientGet(self._client, 'sendMessage')
        sent = await sendMessage(jid, JSObject({
            'aiRich': self,
            'forwarded': forwarded,
            'quoted': quoted,
            'quotedParticipant': quotedParticipant,
            'botJid': botJid,
        }), sendOptions)
        richResponse = _optGet(_optGet(_optGet(sent, 'message'), 'botForwardedMessage'), 'message')
        richResponse = _optGet(richResponse, 'richResponseMessage')
        if not _jsTruthy(_optGet(_optGet(sent, 'key'), 'id')) or not _jsTruthy(_optGet(richResponse, 'unifiedResponse')):
            return sent
        try:
            from baileys_lite.utils.wa_protocol_core import generateMessageIDV2
            user = _clientGet(self._client, 'user')
            authState = _clientGet(self._client, 'authState')
            userId = _jsNullish(
                _optGet(user, 'id'),
                _optGet(_optGet(_optGet(authState, 'creds'), 'me'), 'id'),
            )
            edit = self.buildEdit(jid, _get(sent, 'key'), _get(sent, 'message'))
            relayMessage = _clientGet(self._client, 'relayMessage')
            await relayMessage(jid, edit, JSObject({
                'messageId': generateMessageIDV2(userId),
                'additionalAttributes': JSObject({'edit': '1'}),
                'additionalNodes': _get(sendOptions, 'additionalNodes'),
                'useCachedGroupMetadata': _get(sendOptions, 'useCachedGroupMetadata'),
                'statusJidList': _get(sendOptions, 'statusJidList'),
            }))
        except Exception as error:
            error.relayedKey = _get(sent, 'key')
            raise
        return sent


def _flag_default(options, key, default):
    value = _get(options, key) if options is not None else None
    return default if value is None else value


def aiRichBuilderFromSpec(spec):
    b = AIRichBuilder()
    if _jsTruthy(_get(spec, 'title')):
        b.setTitle(spec.get('title'))
    if _jsTruthy(_get(spec, 'footer')):
        b.setFooter(spec.get('footer'))
    if _jsTruthy(_get(spec, 'contextInfo')):
        b.setContextInfo(spec.get('contextInfo'))
    if _jsTruthy(_get(spec, 'text')):
        b.addText(spec.get('text'), _get(spec, 'textOptions'))
    if _jsTruthy(_get(spec, 'code')):
        b.addCode(_get(spec.get('code'), 'language'), _get(spec.get('code'), 'code'))
    if _jsTruthy(_get(spec, 'table')):
        b.addTable(spec.get('table'), _get(spec, 'tableOptions'))
    if _jsTruthy(_get(spec, 'image')):
        b.addImage(spec.get('image'))
    if _jsTruthy(_get(spec, 'video')):
        b.addVideo(spec.get('video'))
    if _jsTruthy(_get(spec, 'source')):
        b.addSource(spec.get('source'))
    if _jsTruthy(_get(spec, 'product')):
        b.addProduct(spec.get('product'))
    if _jsTruthy(_get(spec, 'post')):
        b.addPost(spec.get('post'))
    if _jsTruthy(_get(spec, 'reels')):
        b.addReels(spec.get('reels'))
    if _jsTruthy(_get(spec, 'tip')):
        b.addTip(spec.get('tip'))
    if _jsTruthy(_get(spec, 'suggest')):
        b.addSuggest(spec.get('suggest'), _get(spec, 'suggestOptions'))
    if _jsTruthy(_get(spec, 'process')):
        b.addProcess(spec.get('process'))
    if _jsTruthy(_get(spec, 'html')):
        htmlValue = spec.get('html')
        args = htmlValue if isinstance(_index0(htmlValue), list) else [htmlValue]
        b.addHtml(*args)
    return b
