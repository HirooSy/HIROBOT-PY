import asyncio
import json
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

from lib.compat import http
from lib.compat.html import load


MAX_RETRY_ATTEMPTS = 3
RETRY_INTERVAL = 333
DEFAULT_USER_AGENT = 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html) (yt-search; https://www.npmjs.com/package/yt-search)'
TEMPLATES = {'YT': 'https://youtube.com', 'SEARCH_DESKTOP': 'https://www.youtube.com/results'}
DAY_IN_MS = 1000 * 60 * 60 * 24
NAN = float('nan')
_state = {'userAgent': DEFAULT_USER_AGENT}
_JP_RE = re.compile(r'\.\.([a-zA-Z0-9_$]+|\*)|\.(\*)|\.([a-zA-Z0-9_$]+)|\[(\d+)\]|\.\[\?\([^)]*\)\]')


class _Duration(dict):
    def __init__(self, seconds, timestamp, text):
        super().__init__(seconds=seconds, timestamp=timestamp)
        self.text = text

    def __str__(self):
        return self.text


class _Results(list):
    _ctoken = None
    _sp = None


class _NoQueryError(RuntimeError):
    pass


def _truthy(v):
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == v and v != 0
    if isinstance(v, str):
        return len(v) > 0
    return True


def _or(*vals):
    for v in vals[:-1]:
        if _truthy(v):
            return v
    return vals[-1] if vals else None


def _strMethod(v):
    if not isinstance(v, str):
        raise TypeError('method is not a function on non-string value')
    return v


def _splitWs(v):
    return re.split(r'\s+', _strMethod(v))


def _toJs(v):
    if v is None:
        return 'undefined'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v:
            return 'NaN'
        if v in (float('inf'), float('-inf')):
            return 'Infinity' if v > 0 else '-Infinity'
        if v == int(v):
            return str(int(v))
        return repr(v)
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return ','.join(_joinItem(x) for x in v)
    return '[object Object]'


def _joinItem(v):
    return '' if v is None else _toJs(v)


def _joinAll(values):
    return ''.join(_joinItem(x) for x in values)


def _toStrJs(val):
    if val is None:
        return ''
    if isinstance(val, str):
        return val
    if isinstance(val, (bool, int, float)):
        return _toJs(val)
    if isinstance(val, list):
        return ''.join(_toStrJs(x) for x in val)
    if isinstance(val, dict):
        if isinstance(val.get('text'), str):
            return val['text']
        if isinstance(val.get('simpleText'), str):
            return val['simpleText']
        if isinstance(val.get('content'), str):
            return val['content']
        if isinstance(val.get('runs'), list):
            out = ''
            for r in val['runs']:
                if r is None:
                    raise TypeError("Cannot read properties of null (reading 'text')")
                out += _toStrJs(r.get('text')) if isinstance(r, dict) else ''
            return out
    return ''


def _jsNumber(v):
    if v is None:
        return NAN
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == '':
            return 0.0
        if re.fullmatch(r'[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?', s):
            return float(s)
        if re.fullmatch(r'0[xX][0-9a-fA-F]+', s):
            return float(int(s, 16))
        if s in ('Infinity', '+Infinity'):
            return float('inf')
        if s == '-Infinity':
            return float('-inf')
    return NAN


def _numOut(x):
    if isinstance(x, float) and math.isfinite(x) and x == int(x):
        return int(x)
    return x


def _deepFindAll(obj, key, out):
    if isinstance(obj, list):
        for el in obj:
            _deepFindAll(el, key, out)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            _deepFindAll(v, key, out)
    return out


def _allChildren(obj, out):
    if isinstance(obj, list):
        out.extend(obj)
    elif isinstance(obj, dict):
        out.extend(obj.values())
    return out


def _parseJpPath(path):
    p = path.strip()
    if p.startswith('$'):
        p = p[1:]
    steps = []
    for m in _JP_RE.finditer(p):
        if m.group(1) is not None:
            steps.append(('deep', m.group(1)))
        elif m.group(2) is not None:
            steps.append(('children', None))
        elif m.group(3) is not None:
            steps.append(('child', m.group(3)))
        elif m.group(4) is not None:
            steps.append(('index', int(m.group(4))))
        else:
            steps.append(('filter', None))
    return steps


def _jpQuery(data, path):
    current = [data]
    for kind, arg in _parseJpPath(path):
        nxt = []
        if kind == 'deep':
            for node in current:
                if arg == '*':
                    _allChildren(node, nxt)
                else:
                    _deepFindAll(node, arg, nxt)
        elif kind == 'children':
            for node in current:
                _allChildren(node, nxt)
        elif kind == 'child':
            for node in current:
                if isinstance(node, dict) and arg in node:
                    nxt.append(node[arg])
        elif kind == 'index':
            for node in current:
                if isinstance(node, list) and arg < len(node):
                    nxt.append(node[arg])
        else:
            nxt.extend(current)
        current = nxt
        if not current:
            break
    return current


def _jpValue(data, path):
    r = _jpQuery(data, path)
    return r[0] if r else None


def _hasPlaylistMetadataRow(item):
    for parts in _deepFindAll(item, 'metadataParts', []):
        part1 = parts[1] if isinstance(parts, list) and len(parts) > 1 else None
        if not _truthy(part1):
            continue
        for t in _deepFindAll(part1, 'text', []):
            if isinstance(t, dict) and t.get('content') == 'Playlist':
                return True
        if isinstance(part1, dict) and part1.get('content') == 'Playlist':
            return True
    return False


def _normalizeThumbnail(thumbnails):
    if not _truthy(thumbnails):
        return None
    if isinstance(thumbnails, str):
        t = thumbnails
    elif isinstance(thumbnails, list) and len(thumbnails) > 0:
        return _normalizeThumbnail(thumbnails[0])
    else:
        return None
    t = t.split('?')[0]
    t = t.replace('/default.jpg', '/hqdefault.jpg').replace('/default.jpeg', '/hqdefault.jpeg')
    if t.startswith('//'):
        return 'https://' + t[2:]
    return t.replace('http://', 'https://')


def _thumbFrom(item, *paths):
    return _or(*[_normalizeThumbnail(_jpValue(item, p)) for p in paths], None)


_DATE_FORMATS = ['%b %d, %Y', '%B %d, %Y', '%b %d %Y', '%B %d %Y', '%d %b %Y', '%d %B %Y',
                 '%b %Y', '%B %Y', '%m/%d/%Y', '%Y/%m/%d']


def _parseDateString(s):
    s = s.strip()
    if not s:
        return None
    if re.fullmatch(r'[0-9]{4}', s):
        return datetime(int(s), 1, 1, tzinfo=timezone.utc).timestamp() * 1000
    if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', s):
        return datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() * 1000
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).timestamp() * 1000
        except ValueError:
            continue
    return None


def _toMs(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return None if f != f else f
    if isinstance(value, str):
        try:
            return _parseDateString(value)
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _toInternalDateString(value):
    ms = _toMs(value)
    if ms is None:
        return 'NaN-NaN-NaN'
    try:
        dt = datetime.fromtimestamp(ms / 1000)
    except (ValueError, OverflowError, OSError):
        return 'NaN-NaN-NaN'
    return f'{dt.year}-{dt.month}-{dt.day}'


def _humanTime(ms):
    if ms is None:
        return 'just now'
    seconds = math.floor((time.time() * 1000 - ms) / 1000)
    if seconds < 0:
        return 'in the future'
    for label, secs in (('year', 31536000), ('month', 2592000), ('week', 604800), ('day', 86400),
                        ('hour', 3600), ('minute', 60), ('second', 1)):
        count = seconds // secs
        if count >= 1:
            return f"{count} {label}{'s' if count > 1 else ''} ago"
    return 'just now'


def _parseDuration(timestampText):
    a = _splitWs(timestampText)
    lastword = a[-1]
    timestamp = re.sub(r'[^:.0-9]', '', lastword)
    if not timestamp:
        return _Duration(0, 0, a[0])
    while timestamp and not timestamp[-1].isdigit():
        timestamp = timestamp[:-1]
    timestamp = timestamp.replace('.', ':')
    t = re.split(r'[:.]', timestamp)
    seconds = 0
    exp = 0
    for part in reversed(t):
        if len(part) <= 0:
            continue
        number = re.sub(r'[^0-9]', '', part)
        seconds += int(number) * (60 ** exp if exp > 0 else 1)
        exp += 1
        if exp > 2:
            break
    return _Duration(seconds, timestamp, f'{seconds} seconds ({timestamp})')


def _parseNumbers(label):
    if not _truthy(label):
        return []
    words = [w.lower() for w in _splitWs(label) if re.search(r'[0-9]', w)]
    results = []
    for n in words:
        m = re.search(r'-?[0-9]+(\.[0-9]+)?', n)
        if not m:
            continue
        num = float(m.group(0))
        if 'm' in n:
            num = 1000000 * num
        if 'k' in n:
            num = 1000 * num
        results.append(_numOut(num))
    return results


def _numFromLabel(s):
    first = _splitWs(s)[0]
    return _numOut(_jsNumber(first.replace(',', '').replace('.', '').strip()))


def _parseSubCountLabel(label):
    if not _truthy(label):
        return None
    lab = _strMethod([w for w in _splitWs(label) if re.search(r'[0-9]', w)][0]).lower()
    m = re.search(r'[0-9]+(\.[0-9]+)?', lab)
    if not m:
        return None
    num = float(m.group(0))
    if 'm' in lab:
        return _numOut(1000000 * num)
    if 'k' in lab:
        return _numOut(1000 * num)
    return _numOut(num)


def _msToTimestamp(ms):
    def floor(x):
        return NAN if x != x else float(math.floor(x))

    def mod(a, b):
        return NAN if a != a else math.fmod(a, b)

    t = ''
    h = floor(ms / 3600000)
    m = mod(floor(ms / 60000), 60)
    s = mod(floor(ms / 1000), 60)
    if _truthy(h):
        t += _toJs(h) + ':'
    if _truthy(h) and len(_toJs(m)) < 2:
        t += '0'
    t += _toJs(m) + ':'
    if len(_toJs(s)) < 2:
        t += '0'
    return t + _toJs(s)


def _getScripts(text):
    c = load(text)
    buf = ''
    for el in c('script'):
        child = el.children[0] if el.children else None
        data = getattr(child, 'data', None) if child is not None else None
        if data:
            buf += data + '\n'
    return buf


def _findLine(pattern, text):
    rx = re.compile(pattern)
    for line in text.split('\n'):
        if rx.search(line):
            return line
    return ''


def _between(text, start, end):
    i = text.find(start)
    j = text.rfind(end)
    if i < 0 or j < 0:
        return ''
    return text[i:j + 1]


def _enc(text):
    return quote(_toJs(text), safe="-_.!~*'()")


async def _httpGet(uri, headers):
    res = await http.get(uri, headers=headers, raise_for_status=False, timeout=60)
    return res.status, res.text()


def _filterType(arr, typeName, keyName, typeList=None):
    out = []
    for i, item in enumerate(arr):
        if typeList is not None:
            if item.get('type') not in typeList:
                continue
        elif item.get('type') != typeName:
            continue
        key = item.get(keyName)
        first = next(j for j, el in enumerate(arr) if el.get(keyName) == key)
        if first == i:
            out.append(item)
    return out


def _parseSearchResultInitialData(responseText):
    c = load(responseText)
    initialHtml = c('div#initial-data').html() or ''
    mt = re.search(r'\{.*\}', initialHtml)
    initialData = mt
    if initialData is None:
        for script in c('script'):
            src = c(script).html() or ''
            for line in src.split('\n'):
                while True:
                    i = line.find('ytInitialData')
                    if i < 0:
                        break
                    line = line[i + len('ytInitialData'):]
                    match = re.search(r'\{.*\}', line)
                    if match and initialData is None:
                        initialData = match
    if initialData is None:
        raise RuntimeError('could not find inital data in the html document')

    results = _Results()
    errors = []
    js = json.loads(initialData.group(0))
    items = _jpQuery(js, '$..itemSectionRenderer..contents.*')
    items.extend(_jpQuery(js, '$..primaryContents..contents.*'))

    for item in items:
        kind = 'unknown'
        hasList = _or(_jpValue(item, '$..compactPlaylistRenderer'), _jpValue(item, '$..playlistRenderer'),
                      _hasPlaylistMetadataRow(item))
        hasChannel = _or(_jpValue(item, '$..compactChannelRenderer'), _jpValue(item, '$..channelRenderer'))
        hasVideo = _or(_jpValue(item, '$..compactVideoRenderer'), _jpValue(item, '$..videoRenderer'))

        listId = _jpValue(item, '$..watchEndpoint..playlistId') if _truthy(hasList) else None
        channelId = _jpValue(item, '$..channelId') if _truthy(hasChannel) else None
        videoId = _jpValue(item, '$..videoId') if _truthy(hasVideo) else None

        watchingLabel = _joinAll(_jpQuery(item, '$..viewCountText..text'))
        isUpcoming = _joinAll(_jpQuery(item, '$..thumbnailOverlayTimeStatusRenderer..style')).upper().strip() == 'UPCOMING'
        isLive = ('watching' in watchingLabel
                  or _joinAll(_jpQuery(item, '$..badges..label')).upper().strip() == 'LIVE NOW'
                  or _joinAll(_jpQuery(item, '$..thumbnailOverlayTimeStatusRenderer..text')).upper().strip() == 'LIVE'
                  or isUpcoming)

        if _truthy(videoId):
            kind = 'video'
        if _truthy(channelId):
            kind = 'channel'
        if _truthy(listId):
            kind = 'list'
        if isLive:
            kind = 'live'

        result = None
        try:
            if kind == 'video':
                result = _parseVideoItem(item, videoId)
            elif kind == 'list':
                result = _parseListItem(item, listId)
            elif kind == 'channel':
                result = _parseChannelItem(item)
            elif kind == 'live':
                result = _parseLiveItem(item, videoId)
        except Exception as err:
            errors.append(err)
        if result:
            results.append(result)

    results._ctoken = _jpValue(js, '$..continuation')
    if errors:
        raise errors[-1]
    return results


def _descriptionOf(item):
    return _or(_joinAll(_jpQuery(item, '$..detailedMetadataSnippets..snippetText..text')),
               _joinAll(_jpQuery(item, '$..description..text')),
               _joinAll(_jpQuery(item, '$..descriptionSnippet..text')))


def _parseVideoItem(item, videoId):
    thumbnail = _thumbFrom(item, '$..thumbnail..url', '$..thumbnails..url', '$..thumbnails')
    title = _or(_jpValue(item, '$..title..text'), _jpValue(item, '$..title..simpleText'))
    authorName = _or(_jpValue(item, '$..shortBylineText..text'), _jpValue(item, '$..longBylineText..text'))
    authorUrl = _or(_jpValue(item, '$..shortBylineText..url'), _jpValue(item, '$..longBylineText..url'))
    agoText = _or(_jpValue(item, '$..publishedTimeText..text'), _jpValue(item, '$..publishedTimeText..simpleText'))
    viewCountText = _or(_jpValue(item, '$..viewCountText..text'), _jpValue(item, '$..viewCountText..simpleText'), '0')
    views = _numFromLabel(viewCountText)
    lengthText = _or(_jpValue(item, '$..lengthText..text'), _jpValue(item, '$..lengthText..simpleText'))
    duration = _parseDuration(_or(lengthText, '0:00'))
    return {
        'type': 'video',
        'videoId': videoId,
        'url': TEMPLATES['YT'] + '/watch?v=' + _toJs(videoId),
        'title': _toStrJs(title).strip(),
        'description': _descriptionOf(item),
        'image': thumbnail,
        'thumbnail': thumbnail,
        'seconds': _numOut(_jsNumber(duration['seconds'])),
        'timestamp': duration['timestamp'],
        'duration': duration,
        'ago': agoText,
        'views': views,
        'author': {'name': authorName, 'url': TEMPLATES['YT'] + _toJs(authorUrl)},
    }


def _parseListItem(item, listId):
    thumbnail = _thumbFrom(item, '$..primaryThumbnail..url', '$..thumbnail..url', '$..thumbnails..url', '$..thumbnails')
    title = _or(_jpValue(item, '$..metadata..title..content'), _jpValue(item, '$..title..text'), _jpValue(item, '$..title..simpleText'))
    authorName = _or(_jpValue(item, '$..metadataParts[0]..text..content'), _jpValue(item, '$..shortBylineText..text'),
                     _jpValue(item, '$..longBylineText..text'), _jpValue(item, '$..shortBylineText..simpleText'),
                     _jpValue(item, '$..longBylineText..simpleTextn'), 'YouTube')
    authorUrl = _or(_jpValue(item, '$..metadataParts[0]..url'), _jpValue(item, '$..shortBylineText..url'),
                    _jpValue(item, '$..longBylineText..url'), '')
    countLabel = _jpValue(item, '$..overlays..thumbnailBadges..text')
    videoCount = _or(_jpValue(item, '$..videoCountShortText..text'), _jpValue(item, '$..videoCountText..text'),
                     _jpValue(item, '$..videoCountShortText..simpleText'), _jpValue(item, '$..videoCountText..simpleText'),
                     _jpValue(item, '$..thumbnailText..text'), _jpValue(item, '$..thumbnailText..simpleText'))
    parsed = _parseNumbers(countLabel)
    count = _numOut(_jsNumber(parsed[0])) if parsed else NAN
    return {
        'type': 'list',
        'listId': listId,
        'url': TEMPLATES['YT'] + '/playlist?list=' + _toJs(listId),
        'title': _toStrJs(title).strip(),
        'image': thumbnail,
        'thumbnail': thumbnail,
        'videoCount': count if _truthy(count) else videoCount,
        'author': {'name': authorName, 'url': TEMPLATES['YT'] + _toJs(authorUrl)},
    }


def _parseChannelItem(item):
    thumbnail = _thumbFrom(item, '$..thumbnail..url', '$..thumbnails..url', '$..thumbnails')
    title = _or(_jpValue(item, '$..title..text'), _jpValue(item, '$..title..simpleText'), _jpValue(item, '$..displayName..text'))
    channelId = _or(_jpValue(item, '$..channelRenderer..channelId'), '')
    authorName = _or(_jpValue(item, '$..shortBylineText..text'), _jpValue(item, '$..longBylineText..text'),
                     _jpValue(item, '$..displayName..text'), _jpValue(item, '$..displayName..simpleText'))
    about = _joinAll(_jpQuery(item, '$..channelRenderer..descriptionSnippet..text')) or ''
    vlabel = _or(_jpValue(item, '$..videoCountText..simpleText'), _jpValue(item, '$..videoCountText..label'),
                 _jpValue(item, '$..videoCountText..text'), '0')
    verifiedLabel = _or(_jpValue(item, '$..channelRenderer..ownerBadges..style'),
                        _jpValue(item, '$..channelRenderer..ownerBadges..tooltip'),
                        _jpValue(item, '$..channelRenderer..ownerBadges..label'), '')
    verified = re.search(r'[\s_]?verified', _strMethod(verifiedLabel).lower().strip()) is not None
    slabel = _or(_jpValue(item, '$..subscriberCountText..simpleText'), _jpValue(item, '$..subscriberCountText..text'), '0')
    if isinstance(slabel, str):
        if _strMethod(slabel).find('subscribe') < 1:
            if _strMethod(vlabel).find('subscribe') > 0:
                slabel, vlabel = vlabel, '-1'
        words = [w for w in _splitWs(slabel) if re.search(r'[0-9]', w)]
        slabel = words[0] if words else None
    base = _or(_jpValue(item, '$..navigationEndpoint..url'), _jpValue(item, '$..browseEndpoint..canonicalBaseUrl'),
               _jpValue(item, '$..browseEndpoint..url'), '/user/' + _toJs(title))
    parsed = _parseNumbers(vlabel)
    return {
        'type': 'channel',
        'name': authorName,
        'url': TEMPLATES['YT'] + _toJs(base),
        'baseUrl': base,
        'id': channelId,
        'title': _toStrJs(title).strip(),
        'about': about,
        'image': thumbnail,
        'thumbnail': thumbnail,
        'videoCount': _numOut(_jsNumber(parsed[0])) if parsed else NAN,
        'videoCountLabel': vlabel,
        'verified': verified,
        'subCount': _parseSubCountLabel(slabel),
        'subCountLabel': slabel,
    }


def _parseLiveItem(item, videoId):
    thumbnail = _thumbFrom(item, '$..thumbnail..url', '$..thumbnails..url', '$..thumbnails')
    title = _or(_jpValue(item, '$..title..text'), _jpValue(item, '$..title..simpleText'))
    authorName = _or(_jpValue(item, '$..shortBylineText..text'), _jpValue(item, '$..longBylineText..text'))
    authorUrl = _or(_jpValue(item, '$..shortBylineText..url'), _jpValue(item, '$..longBylineText..url'))
    watchingLabel = _or(_joinAll(_jpQuery(item, '$..viewCountText..text')),
                        _joinAll(_jpQuery(item, '$..viewCountText..simpleText')), '0')
    watchCount = _numFromLabel(watchingLabel)
    sched = _jpValue(item, '$..upcomingEventData..startTime')
    if sched is None:
        scheduledTime = None
    else:
        n = _jsNumber(sched)
        scheduledTime = _numOut(n * 1000) if time.time() * 1000 > n else sched
    result = {
        'type': 'live',
        'videoId': videoId,
        'url': TEMPLATES['YT'] + '/watch?v=' + _toJs(videoId),
        'title': _toStrJs(title).strip(),
        'description': _descriptionOf(item),
        'image': thumbnail,
        'thumbnail': thumbnail,
        'watching': watchCount,
        'author': {'name': authorName, 'url': TEMPLATES['YT'] + _toJs(authorUrl)},
    }
    if _truthy(scheduledTime):
        result['startTime'] = scheduledTime
        result['startDate'] = _toInternalDateString(scheduledTime)
        result['status'] = 'UPCOMING'
    else:
        result['status'] = 'LIVE'
    return result


async def getSearchResults(opts):
    q = _enc(opts.get('search'))
    hl = _or(opts.get('hl'), 'en')
    gl = _or(opts.get('gl'), 'US')
    category = _or(opts.get('category'), '')

    pageStart = _or(_numOut(_jsNumber(opts.get('pageStart'))), 1)
    pageEnd = _or(_numOut(_jsNumber(opts.get('pageEnd'))), _numOut(_jsNumber(opts.get('pages'))), 1)

    if pageStart <= 0:
        pageStart = 1
        if pageEnd >= 1:
            pageEnd += 1

    if pageEnd != pageEnd:
        raise RuntimeError('error: pageEnd must be a number')

    opts['pageStart'] = pageStart
    opts['pageEnd'] = pageEnd
    opts['currentPage'] = _or(opts.get('currentPage'), pageStart)

    qs = '?search_query=' + q
    if '&hl=' not in qs:
        qs += '&hl=' + _toJs(hl)
    if '&gl=' not in qs:
        qs += '&gl=' + _toJs(gl)
    if _truthy(category):
        qs += '&category=' + _toJs(category)
    if _truthy(opts.get('sp')):
        qs += '&sp=' + _toJs(opts['sp'])

    uri = TEMPLATES['SEARCH_DESKTOP'] + qs
    headers = {
        'user-agent': _state['userAgent'],
        'accept': 'text/html',
        'accept-encoding': 'gzip',
        'accept-language': 'en-US',
    }
    status, body = await _httpGet(uri, headers)
    if status != 200:
        raise RuntimeError('http status: ' + str(status))

    results = _parseSearchResultInitialData(body)
    videos = _filterType(results, 'video', 'videoId')
    playlists = _filterType(results, 'list', 'listId')
    channels = _filterType(results, 'channel', 'url')
    live = _filterType(results, 'live', 'videoId')
    allItems = _filterType(results, None, 'url', ['video', 'list', 'channel', 'live'])

    data = _or(opts.get('_data'), {})
    opts['_data'] = data
    for k in ('videos', 'playlists', 'channels', 'live', 'all'):
        data[k] = _or(data.get(k), [])
    data['videos'].extend(videos)
    data['playlists'].extend(playlists)
    data['channels'].extend(channels)
    data['live'].extend(live)
    data['all'].extend(allItems)

    opts['currentPage'] = opts['currentPage'] + 1
    getMoreResults = opts['currentPage'] <= opts['pageEnd']

    if getMoreResults and _truthy(results._sp):
        opts['sp'] = results._sp
        await asyncio.sleep(2.5)
        return await getSearchResults(opts)

    fVideos = _filterType(data['videos'], 'video', 'videoId')
    fPlaylists = _filterType(data['playlists'], 'list', 'listId')
    fChannels = _filterType(data['channels'], 'channel', 'url')
    fLive = _filterType(data['live'], 'live', 'videoId')
    fAll = _filterType(data['all'], None, 'url', ['video', 'list', 'channel', 'live'])
    return {
        'all': fAll,
        'videos': fVideos,
        'live': fLive,
        'playlists': fPlaylists,
        'lists': fPlaylists,
        'accounts': fChannels,
        'channels': fChannels,
    }


async def getVideoMetaData(opts):
    videoId = opts.get('videoId')
    hl = opts.get('hl', 'en')
    gl = opts.get('gl', 'US')
    uri = f'https://www.youtube.com/watch?hl={_toJs(hl)}&gl={_toJs(gl)}&v={_toJs(videoId)}'
    headers = {
        'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.1.1 Safari/605.1.15',
        'accept': 'text/html',
        'accept-encoding': 'gzip',
        'accept-language': f'{_toJs(hl)}-{_toJs(gl)}',
    }
    status, body = await _httpGet(uri, headers)
    if status != 200:
        raise RuntimeError('http status: ' + str(status))
    return await _parseVideoInitialData(body)


def _parseVideoMetaDataTitle(idata):
    t = _or(_joinAll(_jpQuery(idata, '$..videoPrimaryInfoRenderer.title..text')),
            _joinAll(_jpQuery(idata, '$..videoPrimaryInfoRenderer.title..simpleText')),
            _joinAll(_jpQuery(idata, '$..videoPrimaryRenderer.title..text')),
            _joinAll(_jpQuery(idata, '$..videoPrimaryRenderer.title..simpleText')),
            _jpValue(idata, '$..title..text'),
            _jpValue(idata, '$..title..simpleText'))
    return re.sub(r'[\x00-\x1f\x7f-\x9f​]', '', _strMethod(t))


async def _parseVideoInitialData(responseText):
    text = _getScripts(responseText)
    initialData = _between(_findLine(r'ytInitialData.*=\s*\{', text), '{', '}')
    if not initialData:
        raise RuntimeError('could not find inital data in the html document')
    initialPlayerData = _between(_findLine(r'ytInitialPlayerResponse.*=\s*\{', text), '{', '}')
    if not initialPlayerData:
        raise RuntimeError('could not find inital player data in the html document')

    idata = json.loads(initialData)
    ipdata = json.loads(initialPlayerData)

    videoId = _jpValue(idata, '$..currentVideoEndpoint..videoId')
    if not _truthy(videoId):
        raise RuntimeError('video unavailable')
    if _jpValue(ipdata, '$..status') == 'ERROR' or _jpValue(ipdata, '$..reason') == 'Video unavailable':
        raise RuntimeError('video unavailable')

    title = _parseVideoMetaDataTitle(idata)
    description = _or(_joinAll(_jpQuery(idata, '$..detailedMetadataSnippets..snippetText..text')),
                      _joinAll(_jpQuery(idata, '$..description..text')),
                      _joinAll(_jpQuery(ipdata, '$..description..simpleText')),
                      _joinAll(_jpQuery(ipdata, '$..microformat..description..simpleText')),
                      _joinAll(_jpQuery(ipdata, '$..videoDetails..shortDescription')))
    authorName = _or(_jpValue(idata, '$..owner..title..text'), _jpValue(idata, '$..owner..title..simpleText'))
    authorUrl = _or(_jpValue(idata, '$..owner..navigationEndpoint..url'), _jpValue(idata, '$..owner..title..url'))
    thumbnailUrl = 'https://i.ytimg.com/vi/' + _toJs(videoId) + '/hqdefault.jpg'
    seconds = _jsNumber(_jpValue(ipdata, '$..videoDetails..lengthSeconds'))
    timestamp = _msToTimestamp(seconds * 1000)
    duration = _parseDuration(timestamp)
    uploadDate = _or(_jpValue(idata, '$..uploadDate'), _jpValue(idata, '$..dateText..simpleText'))
    agoText = _humanTime(_toMs(uploadDate)) if _truthy(uploadDate) else ''
    genre = _strMethod(_or(_jpValue(ipdata, '$..category'), '')).lower()

    video = {
        'title': title,
        'description': description,
        'url': TEMPLATES['YT'] + '/watch?v=' + _toJs(videoId),
        'videoId': videoId,
        'seconds': _numOut(_jsNumber(duration['seconds'])),
        'timestamp': duration['timestamp'],
        'duration': duration,
        'views': _numOut(_jsNumber(_jpValue(ipdata, '$..videoDetails..viewCount'))),
        'genre': genre,
        'uploadDate': _toInternalDateString(uploadDate),
        'ago': agoText,
        'image': thumbnailUrl,
        'thumbnail': thumbnailUrl,
        'author': {'name': authorName, 'url': TEMPLATES['YT'] + _toJs(authorUrl)},
    }

    if not _truthy(video['description']) or not _truthy(video['timestamp']) or not _truthy(video['seconds']) or not _truthy(video['views']):
        q = _toJs(video['title'])
        while q and q[0] == '-':
            q = q[1:]
        await asyncio.sleep(1.5)
        r = await _searchInner({'query': q, 'options': {'RETRY_INTERVAL': 1000}})
        if r.get('videos') is None:
            return video
        for v in r['videos']:
            if not _truthy(v):
                continue
            if video['videoId'] is not None and video['videoId'] == v.get('videoId'):
                for key in list(video.keys()):
                    video[key] = _or(v.get(key), video[key])
                break
        return video
    return video


async def getPlaylistMetaData(opts):
    listId = _or(opts.get('listId'), opts.get('playlistId'))
    hl = opts.get('hl', 'en')
    gl = opts.get('gl', 'US')
    uri = f'https://www.youtube.com/playlist?hl={_toJs(hl)}&gl={_toJs(gl)}&list={_toJs(listId)}'
    headers = {
        'user-agent': _state['userAgent'],
        'accept': 'text/html',
        'accept-encoding': 'gzip',
        'accept-language': f'{_toJs(hl)}-{_toJs(gl)}',
    }
    status, body = await _httpGet(uri, headers)
    if status != 200:
        raise RuntimeError('http status: ' + str(status))
    return _parsePlaylistInitialData(body, listId)


def _parsePlaylistLastUpdateTime(lastUpdateLabel):
    try:
        words = re.split(r'[\s.-]+', _strMethod(lastUpdateLabel).lower().strip())
        if len(words) > 0:
            lastWord = words[-1].lower()
            if lastWord == 'yesterday':
                ms = time.time() * 1000 - DAY_IN_MS
                return _toInternalDateString(ms)
        if len(words) >= 2:
            if words[0] == 'updated' and words[2][:3] == 'day':
                ms = time.time() * 1000 - DAY_IN_MS * _jsNumber(words[1])
                if _toMs(ms) is not None:
                    return _toInternalDateString(ms)
        for i in range(len(words)):
            sl = words[i:]
            a = _toMs(' '.join(sl))
            if a is not None:
                return _toInternalDateString(a)
            b = _toMs(' '.join(reversed(sl)))
            if b is not None:
                return _toInternalDateString(b)
        return ''
    except Exception:
        return ''


def _parsePlaylistInitialData(responseText, listIdArg):
    text = _getScripts(responseText)
    mt = re.search(r'ytInitialData.*=\s*(\{.*\});', text)
    jsonString = mt.group(1) if mt else None
    if not _truthy(jsonString):
        raise RuntimeError('failed to parse ytInitialData json data')
    js = json.loads(jsonString)

    plerr = _jpValue(js, '$..alerts..alertRenderer')
    if isinstance(plerr, dict) and isinstance(plerr.get('type'), str) and plerr['type'].lower() == 'error':
        plerrtext = 'playlist error, not found?'
        t = plerr.get('text')
        if isinstance(t, (dict, list)):
            plerrtext = _joinAll(_jpQuery(t, '$..text'))
        elif t is None and 'text' in plerr:
            plerrtext = ''
        if isinstance(t, str):
            plerrtext = t
        raise RuntimeError('playlist error: ' + plerrtext)

    alertInfo = ''
    for val in _jpQuery(js, '$..alerts..text'):
        if isinstance(val, str):
            alertInfo += val
        if isinstance(val, (dict, list)):
            simpleText = _jpValue(val, '$..simpleText')
            if _truthy(simpleText):
                alertInfo += _toJs(simpleText)

    canon = _or(_jpValue(js, '$..microformat..urlCanonical'), '')
    parts = _strMethod(canon).split('=')
    listId = parts[1] if len(parts) > 1 else None

    viewCount = 0
    try:
        viewCountLabel = _jpValue(js, '$..sidebar.playlistSidebarRenderer.items[0]..stats[1].simpleText')
        if _strMethod(viewCountLabel).lower() == 'no views':
            viewCount = 0
        else:
            found = re.findall(r'[0-9]+', _strMethod(viewCountLabel))
            if not found:
                raise TypeError("Cannot read properties of null (reading 'join')")
            viewCount = ''.join(found)
    except Exception:
        pass

    sizeVal = _or(_jpValue(js, '$..sidebar.playlistSidebarRenderer.items[0]..stats[0].simpleText'),
                  _joinAll(_jpQuery(js, '$..sidebar.playlistSidebarRenderer.items[0]..stats[0]..text')))
    sizeMatches = re.findall(r'[0-9]+', _strMethod(sizeVal))
    if not sizeMatches:
        raise TypeError("Cannot read properties of null (reading 'join')")
    size = ''.join(sizeMatches)

    lists = _jpQuery(js, '$..playlistVideoListRenderer..contents')
    lst = lists[0] if lists else None
    if not isinstance(lst, list):
        raise TypeError('list.forEach is not a function')

    videos = []
    for item in lst:
        if item is None:
            raise TypeError("Cannot read properties of null (reading 'playlistVideoRenderer')")
        if not isinstance(item, dict) or not _truthy(item.get('playlistVideoRenderer')):
            continue
        durationText = _or(_jpValue(item, '$..lengthText..simpleText'),
                           _jpValue(item, '$..thumbnailOverlayTimeStatusRenderer..simpleText'),
                           _joinAll(_jpQuery(item, '$..lengthText..text')),
                           _joinAll(_jpQuery(item, '$..thumbnailOverlayTimeStatusRenderer..text')))
        duration = _parseDuration(durationText)
        videos.append({
            'title': _or(_jpValue(item, '$..title..simpleText'), _jpValue(item, '$..title..text'),
                         _joinAll(_jpQuery(item, '$..title..text'))),
            'videoId': _jpValue(item, '$..videoId'),
            'listId': listId,
            'thumbnail': _thumbFrom(item, '$..thumbnail..url', '$..thumbnails..url', '$..thumbnails'),
            'duration': duration,
            'author': {
                'name': _jpValue(item, '$..shortBylineText..runs[0]..text'),
                'url': 'https://youtube.com' + _toJs(_jpValue(item, '$..shortBylineText..runs[0]..url')),
            },
        })

    plthumbnail = _thumbFrom(js, '$..microformat..thumbnail..url', '$..microformat..thumbnails..url', '$..microformat..thumbnails')
    firstThumb = videos[0]['thumbnail'] if videos else None
    dateLabel = _or(_jpValue(js, '$..sidebar.playlistSidebarRenderer.items[0]..stats[2]..simpleText'),
                    _joinAll(_jpQuery(js, '$..sidebar.playlistSidebarRenderer.items[0]..stats[2]..text')), '')

    return {
        'title': _jpValue(js, '$..microformat..title'),
        'listId': listId,
        'url': 'https://youtube.com/playlist?list=' + _toJs(listId),
        'size': _numOut(_jsNumber(size)),
        'views': _numOut(_jsNumber(viewCount)),
        'date': _parsePlaylistLastUpdateTime(dateLabel),
        'image': _or(plthumbnail, firstThumb),
        'thumbnail': _or(plthumbnail, firstThumb),
        'videos': videos,
        'alertInfo': alertInfo,
        'author': {
            'name': _jpValue(js, '$..videoOwner..title..runs[0]..text'),
            'url': 'https://youtube.com' + _toJs(_jpValue(js, '$..videoOwner..navigationEndpoint..url')),
        },
    }


async def _searchInner(query):
    opts = {'query': query} if isinstance(query, str) else query
    opts['_attempts'] = (opts.get('_attempts') or 0) + 1
    retryOptions = dict(opts)

    if _truthy(opts.get('userAgent')):
        _state['userAgent'] = opts['userAgent']

    opts['search'] = _or(opts.get('query'), opts.get('search'))
    opts['original_search'] = opts['search']

    if not (_truthy(opts.get('videoId')) or _truthy(opts.get('listId'))) and not _truthy(opts['search']):
        raise _NoQueryError('yt-search: no query given')

    try:
        if _truthy(opts.get('videoId')):
            return await getVideoMetaData(opts)
        if _truthy(opts.get('listId')):
            return await getPlaylistMetaData(opts)
        return await getSearchResults(opts)
    except _NoQueryError:
        raise
    except Exception:
        maxAttempts = _or(opts.get('MAX_RETRY_ATTEMPTS'), MAX_RETRY_ATTEMPTS)
        if opts['_attempts'] > maxAttempts:
            raise
        n = opts['_attempts']
        waitMs = (2 ** (n - 1)) * _or(opts.get('RETRY_INTERVAL'), RETRY_INTERVAL)
        await asyncio.sleep(waitMs / 1000)
        return await _searchInner(retryOptions)


async def search(query, callback=None):
    if callback is None:
        return await _searchInner(query)
    try:
        data = await _searchInner(query)
    except Exception as err:
        callback(err, None)
        return None
    callback(None, data)
    return None


default = search
