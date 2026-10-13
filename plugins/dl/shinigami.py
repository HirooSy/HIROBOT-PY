import re

from lib.compat import http
from lib.utils import converter
from lib.utils.handler import G


API_BASE = 'https://api.shngm.io/v1'
COMMENT_BASE = 'https://commento.shngm.io/api'
HEADERS = {'Accept': 'application/json', 'Content-Type': 'application/json', 'Origin': 'https://g.shinigami.asia',
           'Referer': 'https://g.shinigami.asia/', 'User-Agent': 'Mozilla/5.0'}


def js_parse_int(value):
    mt = re.match(r'\s*[+-]?\d+', str(value or ''))
    return int(mt.group(0)) if mt else None


def fmt_int(n):
    if n is None:
        return '0'
    if isinstance(n, float) and n == int(n):
        n = int(n)
    if isinstance(n, (int, float)):
        return f'{n:,}' if n else '0'
    return str(n)


async def api(path, params=None):
    return (await http.get(API_BASE + path, params=params, headers=HEADERS, timeout=20)).json()


def send_list(conn, m, caption, rows, button_text='Select', location=None):
    content = {'text': caption, 'footer': '', 'optionText': button_text, 'optionTitle': button_text,
               'nativeFlow': [{'text': button_text, 'sections': [{'rows': rows}]}]}
    if location:
        content['location'] = location
    return conn.sendButton(m.chat, content, m)


def names(tax, key):
    items = (tax or {}).get(key)
    return [v['name'] for v in items if v and v.get('name')] if isinstance(items, list) else []


def strip_html(h):
    if not h:
        return ''
    h = re.sub(r'<details[\s\S]*?</details>', '[spoiler]', h, flags=re.I)
    h = re.sub(r'<[^>]+>', '', h)
    h = h.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")
    return re.sub(r'\n{3,}', '\n\n', h).strip()


def flatten_comments(items):
    result = []
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get('nick') and item.get('orig') is not None:
            result.append(item)
        if isinstance(item.get('children'), list):
            result.extend(c for c in item['children'] if isinstance(c, dict) and c.get('nick'))
    return result


async def fetch_comments(manga_id, page_size=10):
    all_comments = []
    total = 0
    try:
        params = {'path': f'series/{manga_id}', 'page': 1, 'pageSize': page_size, 'lang': 'en', 'sortBy': 'like_desc'}
        first = (await http.get(f'{COMMENT_BASE}/comment', params=params, timeout=30)).json()
        cd = (first or {}).get('data') or {}
        total_page = cd.get('totalPages') or 1
        total = cd.get('count') or 0
        all_comments = flatten_comments(cd.get('data'))
        for page in range(2, min(total_page, 3) + 1):
            params = {'path': f'series/{manga_id}', 'page': page, 'pageSize': page_size, 'lang': 'en', 'sortBy': 'like_desc'}
            res = (await http.get(f'{COMMENT_BASE}/comment', params=params, timeout=30)).json()
            all_comments.extend(flatten_comments(((res or {}).get('data') or {}).get('data')))
    except Exception:
        pass
    return total, all_comments


async def get_details(manga_id, page_size=50):
    detail = await api(f'/manga/detail/{manga_id}')
    manga = detail.get('data')
    if not manga:
        raise RuntimeError('Manga detail not found')
    first = await api(f'/chapter/{manga_id}/list', {'page': 1, 'page_size': page_size, 'sort_by': 'chapter_number', 'sort_order': 'desc'})
    total_page = (first.get('meta') or {}).get('total_page') or 1
    chapters = list(first.get('data') or []) if isinstance(first.get('data'), list) else []
    for page in range(2, total_page + 1):
        res = await api(f'/chapter/{manga_id}/list', {'page': page, 'page_size': page_size, 'sort_by': 'chapter_number', 'sort_order': 'desc'})
        if isinstance(res.get('data'), list):
            chapters.extend(res['data'])
    chapters.sort(key=lambda c: c.get('chapter_number') or 0)
    total_comments, comments = await fetch_comments(manga_id)
    comments.sort(key=lambda c: c.get('like') or 0, reverse=True)
    tax = manga.get('taxonomy') or {}
    return {'manga_id': manga.get('manga_id'), 'title': manga.get('title'), 'alternative_title': manga.get('alternative_title'),
            'description': manga.get('description'), 'status': manga.get('status'), 'cover': manga.get('cover_image_url'),
            'views': manga.get('view_count'), 'bookmark': manga.get('bookmark_count'), 'rank': manga.get('rank'),
            'taxonomy': {'author': names(tax, 'Author'), 'artist': names(tax, 'Artist'), 'genre': names(tax, 'Genre'), 'format': names(tax, 'Format')},
            'chapter_info': {'first': chapters[0] if chapters else None, 'latest': chapters[-1] if chapters else None},
            'total_chapter': len(chapters), 'chapters': chapters,
            'comment_info': {'total': total_comments}, 'comments': comments[:5]}


async def chapter_images(chapter_id):
    data = (await api(f'/chapter/detail/{chapter_id}')).get('data')
    if not data:
        raise RuntimeError('Chapter detail not found')
    base = data.get('base_url') or data.get('base_url_low') or ''
    path = (data.get('chapter') or {}).get('path') or ''
    files = (data.get('chapter') or {}).get('data') or []
    images = [f'{base}{path}{f}' for f in files]
    if not images and isinstance(data.get('chapter_image'), list):
        images = [i['image_url'] for i in data['chapter_image'] if i.get('image_url')]
    return {'chapter_id': data.get('chapter_id'), 'chapter_number': data.get('chapter_number'), 'images': images, 'total_image': len(images)}


def detail_caption(d):
    c = '━━━━━━━━━━━━━━\n'
    if d['alternative_title']:
        c += f"- *Alt:* {d['alternative_title']}\n"
    c += f"- *Status:* {d['status']}\n- *Views:* {fmt_int(d['views'])} | *Bookmarks:* {fmt_int(d['bookmark'])}\n"
    if d['rank']:
        c += f"- *Rank:* {d['rank']}\n"
    for label, key in (('Author', 'author'), ('Artist', 'artist'), ('Genre', 'genre')):
        if d['taxonomy'][key]:
            c += f"- *{label}:* {', '.join(d['taxonomy'][key])}\n"
    c += f"- *Chapters:* {d['total_chapter']}\n━━━━━━━━━━━━━━\n"
    if d['description']:
        c += strip_html(d['description'])[:400] + '...\n'
    return c.strip()


async def handler(m, *, conn, text, usedPrefix, command):
    text, p, command = text, usedPrefix, command
    if not text:
        return await m.reply(f'Usage:\n- {p + command} <title>\n- {p + command} hot')
    parts = text.strip().split(' ')
    sub, args = parts[0], parts[1:]
    if sub in ('update', 'hot'):
        page = js_parse_int(args[0] if args else None) or 1
        await m.react('🔍')
        data = await api('/manga/list', {'type': 'project', 'page': page, 'page_size': 24, 'is_update': 'true', 'sort': 'latest', 'sort_order': 'desc'})
        if not data.get('data'):
            return await m.reply('No update data found.')
        rows = [{'header': f"{i + 1}. {mg['title']}", 'title': f"Status: {mg.get('status')} | Ch. {mg.get('latest_chapter_number') or '?'}",
                 'description': f"Views: {fmt_int(mg.get('view_count'))} | Bookmarks: {fmt_int(mg.get('bookmark_count'))}", 'id': f".{command} info {mg['manga_id']}"}
                for i, mg in enumerate(data['data'])]
        await m.react('✅')
        total = data.get('total')
        pages = 'NaN' if total is None else -(-int(total) // 24)
        return await send_list(conn, m, f'*LATEST UPDATES* (Page {page}/{pages})', rows, 'Select Manga')
    if sub == 'info':
        manga_id = args[0] if args else None
        if not manga_id:
            return
        await m.react('🔍')
        d = await get_details(manga_id)
        if not d['chapters']:
            await m.react('❌')
            return await m.reply('No chapters available for this manga.')
        rows = [{'header': f"Chapter {ch['chapter_number']}{' - ' + ch['title'] if ch.get('title') else ''}", 'title': f"Views: {fmt_int(ch.get('views'))}",
                 'description': '', 'id': f".{command} dl {manga_id} {ch['chapter_number']}"} for ch in reversed(d['chapters'])]
        await m.react('✅')
        loc = {'name': d['title'], 'address': '', 'image': d['cover']}
        try:
            return await send_list(conn, m, detail_caption(d), rows, 'Select Chapter', loc)
        except Exception:
            loc.pop('image', None)
            return await send_list(conn, m, detail_caption(d), rows, 'Select Chapter', loc)
    if sub == 'dl':
        if len(args) < 2 or not args[0] or not args[1]:
            return
        manga_id, num = args[0], args[1]
        await m.react('⬇️')
        d = await get_details(manga_id)
        found = next((c for c in d['chapters'] if str(c['chapter_number']) == num), None)
        if not found:
            await m.react('❌')
            return await m.reply('Chapter not found.')
        cd = await chapter_images(found['chapter_id'])
        if not cd['images']:
            await m.react('❌')
            return await m.reply('No images found for this chapter.')
        buffers = []
        for url in cd['images']:
            buffers.append((await http.get(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://g.shinigami.asia/'}, timeout=30)).content)
        pdf = await converter.img2pdf(buffers)
        await m.react('✅')
        return await conn.sendMessage(m.chat, {'document': pdf, 'mimetype': 'application/pdf', 'fileName': f"{d['title']} - Chapter {num}.pdf",
                                               'caption': f"*{d['title']}*\nChapter {num} | {cd['total_image']} Pages"}, {'quoted': m})
    await m.react('🔍')
    keyword = text.strip()
    res = await api('/manga/list', {'page': 1, 'page_size': 24, 'genre_include_mode': 'or', 'genre_exclude_mode': 'or', 'sort': 'latest', 'sort_order': 'desc', 'q': keyword})
    if not res.get('data'):
        await m.react('❌')
        return await m.reply('No results found.')
    rows = [{'header': f"{i + 1}. {mg['title']}", 'title': f"Status: {mg.get('status')}", 'description': f"Views: {fmt_int(mg.get('view_count'))}",
             'id': f".{command} info {mg['manga_id']}"} for i, mg in enumerate(res['data'])]
    await m.react('✅')
    return await send_list(conn, m, f"*{keyword}* - {len(res['data'])} result(s)", rows, 'Select Manga')


help = command = ['shinigami']
tags = ['downloader']
ai = {'risk': 'low', 'description': 'search/download manga'}
