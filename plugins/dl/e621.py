import asyncio
import base64
import html as htmllib
import os
import re
import shutil
import sys
import tempfile
import time

from lib.compat import ffmpeg as ff, http
from lib.utils import converter
from lib.utils.handler import G


FFMPEG_PATH = ff.FFMPEG
PAGE_SIZE = 30
STATIC_IMAGE_EXTS = ['jpg', 'jpeg', 'png', 'webp']
MAX_DLALL_POSTS = 200
RATING = {'s': 'Safe', 'q': 'Questionable', 'e': 'Explicit'}
RATING_COLORS = {'s': '#34c759', 'q': '#ffcc00', 'e': '#ff3b30'}
IMAGE_EXTS = ['jpg', 'jpeg', 'png', 'gif', 'webp']
VIDEO_EXTS = ['webm', 'mp4']
LABELED_EXTS = ['webm', 'mp4', 'gif']
PREVIEW_CONCURRENCY = 25
GRID_COLUMNS = 3
SEARCH_STATE_TTL = 15 * 60 * 1000
PREVIEW_UA = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
SHRINK_ABOVE_BYTES = 8 * 1024
_state = {}


def e621():
    return G.scraper.e621


def base36(n):
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    if n == 0:
        return '0'
    out = ''
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def map_post(post):
    if not post:
        return None
    f = post.get('file') or {}
    preview = post.get('preview') or {}
    sample = post.get('sample') or {}
    return {
        'id': post.get('id'),
        'url': f.get('url') or None,
        'pageUrl': f"https://e621.net/posts/{post.get('id')}",
        'previewUrl': preview.get('url') or sample.get('url') or None,
        'ext': f.get('ext') or 'unknown',
        'size': f.get('size') or 0,
        'rating': post.get('rating') or '?',
        'favCount': post.get('fav_count') or 0,
        'tags': post.get('tags') or {},
    }


def get_tmp_dir():
    tmp = os.path.join(os.getcwd(), 'data', 'tmp')
    try:
        os.makedirs(tmp, exist_ok=True)
        if not os.access(tmp, os.W_OK):
            raise PermissionError(tmp)
        return tmp
    except OSError as e:
        print('[e621] Gagal buat folder tmp:', e, file=sys.stderr)
        return './tmp'


def cleanup_tmp(tmp_dir):
    try:
        for f in os.listdir(tmp_dir):
            if f.startswith('tmp_'):
                try:
                    os.unlink(os.path.join(tmp_dir, f))
                except OSError:
                    pass
    except OSError:
        pass


def cleanup_state():
    now = time.time() * 1000
    for k in [k for k, v in _state.items() if now - v['timestamp'] > SEARCH_STATE_TTL]:
        del _state[k]


async def search_tags(keywords, page):
    try:
        res = await http.get('https://e621.net/posts.json', params={'tags': keywords, 'limit': PAGE_SIZE, 'page': page}, headers=e621().getHeaders(), timeout=15)
        raw = res.json().get('posts') or []
        if not raw:
            return [], False
        return [map_post(p) for p in raw], len(raw) >= PAGE_SIZE
    except Exception as e:
        print('[e621] tagsSearch error:', e, file=sys.stderr)
        return [], False


async def fetch_all_pool_posts(pool_id):
    info = await e621().getPoolInfo(pool_id)
    if not info or not info['postIds']:
        return None
    posts = []
    ids = info['postIds']
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            res = await http.get('https://e621.net/posts.json', params={'tags': 'id:' + ','.join(map(str, chunk)), 'limit': len(chunk)}, headers=e621().getHeaders(), timeout=20)
            by_id = {p.get('id'): map_post(p) for p in res.json().get('posts') or []}
            posts.extend(by_id[x] for x in chunk if by_id.get(x))
        except Exception as e:
            print('[e621] fetchAllPoolPosts chunk error:', e, file=sys.stderr)
    return {'info': info, 'posts': posts}


async def build_pool_zip(posts):
    zf = converter.ZipFile()
    for post in posts:
        if not post.get('url'):
            continue
        try:
            res = await http.get(post['url'], headers=e621().getHeaders(), timeout=30)
            zf.file(f"{post['id']}.{post.get('ext') or 'bin'}", res.content)
        except Exception as e:
            print('[e621] zip fetch error for post', post['id'], e, file=sys.stderr)
    return await zf.generateAsync(type='nodebuffer', compression='DEFLATE')


def build_caption(post):
    t = post['tags']
    character = t.get('character') or []
    char = f"\n- *Character:* {', '.join(str(v) for v in character)}" if len(character) >= 1 else ''
    return (f"*#{post['id']}*\n{post['favCount']} Favorites • {RATING.get(post['rating'], post['rating'])}{char}\n"
            f"- *Species:* {', '.join(str(v) for v in (t.get('species') or []))}\n"
            f"- *Artist:* {', '.join(str(v) for v in (t.get('artist') or []))}\n"
            f"- *Tags:* {G.readmore or ' '}\n"
            f"> {', '.join(str(v) for v in (t.get('general') or []))}")


async def convert_to_mp4(url, ext):
    tmp_dir = get_tmp_dir()
    cleanup_tmp(tmp_dir)
    ts = int(time.time() * 1000)
    src = os.path.join(tmp_dir, f'tmp_in_{ts}.{ext}')
    out = os.path.join(tmp_dir, f'tmp_out_{ts}.mp4')
    res = await http.get(url, headers=e621().getHeaders(), timeout=60)
    with open(src, 'wb') as fh:
        fh.write(res.content)
    if os.path.getsize(src) == 0:
        raise RuntimeError('Download failed, input file is empty')
    try:
        await ff.run(FFMPEG_PATH, ['-y', '-i', src, '-movflags', 'faststart', '-pix_fmt', 'yuv420p', '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
                                   '-c:v', 'libx264', '-c:a', 'aac', '-f', 'mp4', out], timeout=None)
    except Exception as err:
        raise RuntimeError(f'ffmpeg error: {err}')
    if os.path.exists(src):
        os.unlink(src)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError('Conversion failed, output file is empty or not found')
    return out


async def send_post(conn, chat, post, quoted):
    opt = {'quoted': quoted} if quoted else {}
    caption = build_caption(post)
    if not post.get('url'):
        return await conn.sendMessage(chat, {'text': f"Media file for this post isn't available (e621 didn't expose a direct file URL - it may be restricted or deleted).\n\n{caption}\n{post.get('pageUrl') or ''}"}, opt)
    ext = post.get('ext')
    if ext == 'gif':
        path = await convert_to_mp4(post['url'], 'gif')
        try:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path, 'rb') as fh:
                    await conn.sendMessage(chat, {'video': fh.read(), 'gifPlayback': True, 'caption': caption}, opt)
            else:
                raise RuntimeError('Processed file is invalid.')
        finally:
            if os.path.exists(path):
                os.unlink(path)
        return
    if ext == 'webm':
        return await conn.sendMessage(chat, {'document': {'url': post['url']}, 'mimetype': 'video/webm', 'fileName': f"e621_{post['id']}.webm", 'caption': caption}, opt)
    if ext == 'mp4':
        return await conn.sendMessage(chat, {'video': {'url': post['url']}, 'caption': caption}, opt)
    return await conn.sendMessage(chat, {'image': {'url': post['url']}, 'caption': caption}, opt)


async def fetch_to_file(url, file):
    res = await http.get(url, headers={'User-Agent': PREVIEW_UA, 'Accept': 'image/*,*/*', 'Referer': 'https://e621.net/'}, timeout=15)
    if not res.content:
        raise RuntimeError('empty response')
    with open(file, 'wb') as fh:
        fh.write(res.content)
    mime = (res.headers.get('content-type') or '').split(';')[0].strip()
    return mime if re.match(r'^image/', mime, re.I) else 'image/jpeg'


async def first_frame(inp, out):
    args = ['-y']
    if re.match(r'^https?://', inp, re.I):
        args += ['-user_agent', e621().getHeaders()['User-Agent'], '-headers', 'Referer: https://e621.net/\r\n']
    args += ['-i', inp, '-frames:v', '1', '-vf', 'scale=320:-2', '-q:v', '5', out]
    await ff.run(FFMPEG_PATH, args, timeout=30)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError('empty frame')


async def save_preview(post, d, idx):
    ext = (post.get('ext') or '').lower()
    base = os.path.join(d, f'e621_{idx}')
    if post.get('previewUrl'):
        f = base + '.img'
        return {'file': f, 'mime': await fetch_to_file(post['previewUrl'], f)}
    out = base + '.jpg'
    if (ext in VIDEO_EXTS or ext == 'gif') and post.get('url'):
        await first_frame(post['url'], out)
        return {'file': out, 'mime': 'image/jpeg'}
    if ext in IMAGE_EXTS and post.get('url'):
        raw = base + '_raw'
        try:
            await fetch_to_file(post['url'], raw)
            await first_frame(raw, out)
        finally:
            if os.path.exists(raw):
                os.unlink(raw)
        return {'file': out, 'mime': 'image/jpeg'}
    raise RuntimeError('no preview source')


async def shrink_if_large(result, d, idx):
    if not result or os.path.getsize(result['file']) <= SHRINK_ABOVE_BYTES:
        return result
    out = os.path.join(d, f'e621_{idx}_s.jpg')
    try:
        await ff.run(FFMPEG_PATH, ['-y', '-i', result['file'], '-frames:v', '1', '-vf', 'scale=150:150:force_original_aspect_ratio=increase,crop=150:150', '-q:v', '12', out], timeout=20)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            os.unlink(result['file'])
            return {'file': out, 'mime': 'image/jpeg'}
    except Exception:
        pass
    return result


async def download_previews(posts, d):
    results = [None] * len(posts)
    failures = []
    sem = asyncio.Semaphore(PREVIEW_CONCURRENCY)

    async def one(i):
        async with sem:
            try:
                results[i] = await shrink_if_large(await save_preview(posts[i], d, i), d, i)
            except Exception as e:
                status = getattr(getattr(e, 'response', None), 'status', None)
                failures.append(f'#{i + 1}: {"HTTP " + str(status) if status else (str(e) or e.__class__.__name__)}')
    await asyncio.gather(*[one(i) for i in range(len(posts))])
    return results, failures


def fmt_fav(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n >= 1000:
        s = f'{n / 1000:.1f}'
        if s.endswith('.0'):
            s = s[:-2]
        return s + 'k'
    return str(int(n)) if n == int(n) else str(n)


def build_preview_html(posts, previews):
    cells = ''
    for i, post in enumerate(posts):
        ext = (post.get('ext') or '').lower()
        pv = previews[i]
        rating = str(post.get('rating') or '?').lower()
        color = RATING_COLORS.get(rating, '#8e8e93')
        tag = f'<span class="tag">{ext.upper()}</span>' if ext in LABELED_EXTS else ''
        if pv:
            with open(pv['file'], 'rb') as fh:
                media = f'<img src="data:{pv["mime"]};base64,{base64.b64encode(fh.read()).decode()}" alt="{i + 1}">'
        else:
            media = f'<div class="empty">{htmllib.escape((post.get("ext") or "?").upper())}</div>'
        cells += (f'\n    <div class="cell">\n      <div class="thumb">\n        {media}\n        <span class="num">{i + 1}</span>\n        {tag}\n      </div>\n'
                  f'      <div class="status"><span class="heart">&#9829;&#xFE0E;</span> <span class="count">{fmt_fav(post.get("favCount"))}</span> '
                  f'<span class="count">-</span> <span style="color:{color}">{htmllib.escape(rating.upper())}</span></div>\n    </div>')
    return ('<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<style>\n'
            '  :root { color-scheme: light dark; }\n  * { box-sizing: border-box; margin: 0; padding: 0; }\n'
            '  body { padding: 8px; font-family: sans-serif; background: transparent; }\n'
            f'  .grid {{ display: grid; grid-template-columns: repeat({GRID_COLUMNS}, 1fr); gap: 8px; }}\n'
            '  .cell { border-radius: 10px; overflow: hidden; background: #1c1c1e; }\n'
            '  .thumb { position: relative; aspect-ratio: 1 / 1; background: rgba(128,128,128,.2); }\n'
            '  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }\n'
            '  .empty { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 400; color: #fff; opacity: .6; }\n'
            '  .num { position: absolute; top: 5px; left: 5px; min-width: 20px; padding: 1px 6px; border-radius: 10px; background: rgba(0,0,0,.6); color: #fff; font-size: 10px; font-weight: 400; text-align: center; }\n'
            '  .tag { position: absolute; top: 5px; right: 5px; padding: 1px 5px; border-radius: 5px; background: rgba(0,0,0,.6); color: #fff; font-size: 9px; font-weight: 400; letter-spacing: .3px; }\n'
            '  .status { padding: 4px 4px; text-align: center; font-size: 10px; font-weight: 400; white-space: nowrap; }\n'
            '  .heart { color: #ff3b30; }\n  .count { color: #fff; }\n</style>\n</head>\n<body>\n  <div class="grid">'
            + cells + '\n  </div>\n</body>\n</html>')


async def send_gallery(conn, m, type_, keywords, pool_id, label, posts, page, has_next, command):
    cleanup_state()
    token = f"{(m.sender or '').split('@')[0]}_{base36(int(time.time() * 1000))}"
    _state[token] = {'type': type_, 'keywords': keywords, 'poolId': pool_id, 'posts': posts, 'page': page, 'timestamp': time.time() * 1000}
    rows = [{'header': f"{i + 1}. #{p['id']}", 'title': f"{RATING.get(p['rating'], p['rating'])} · {(p.get('ext') or '?').upper()} · {p['favCount']} favs",
             'description': '', 'id': f'.{command} get {token} {i}'} for i, p in enumerate(posts)]
    nav = []
    if type_ == 'pool':
        nav.append({'text': 'Download All', 'id': f'.{command} dlall {token}'})
    if page > 1:
        nav.append({'text': '◀︎Prev', 'id': f'.{command} page {token} {page - 1}'})
    if has_next:
        nav.append({'text': '▶︎Next', 'id': f'.{command} page {token} {page + 1}'})
    caption = f'- {label}\n- *Page:* {page}\n- *Showing:* {len(posts)}\n\nChoose the number you want to download.'
    d = tempfile.mkdtemp(prefix='e621_')
    try:
        previews, failures = await download_previews(posts, d)
        ok = len([p for p in previews if p])
        try:
            html = build_preview_html(posts, previews)
            title = re.sub(r'[*_]', '', str(keywords or label))
            await conn.aiRich().setTitle(f'e621 — {title}').addHtml(html).send(m.chat, {'quoted': m})
        except Exception as err:
            await m.reply(f'Preview gagal dikirim: {err}')
        if ok == 0:
            await m.reply(f"Preview gambar gagal di-download (0/{len(posts)}). {failures[0] if failures else ''}".strip())
        await conn.sendButton(m.chat, {'text': caption, 'footer': 'e621', 'nativeFlow': [{'text': '\u0000', 'sections': [{'rows': rows}]}, *nav]}, m)
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def handler(m, *, conn, text, command):
    sc = e621()
    if not text:
        return await m.reply('How to use:\n.e621 <keywords>\n.e621 <post url>\n.e621 <pool url>')
    parts = text.strip().split(' ')
    sub, rest = parts[0], parts[1:]
    try:
        if sub == 'get':
            state = _state.get(rest[0] if rest else None)
            if not state:
                return await m.reply('This search result has expired, please search again.')
            idx = int(rest[1]) if len(rest) > 1 and re.match(r'^\d+$', rest[1]) else -1
            if idx < 0 or idx >= len(state['posts']):
                return await m.reply('Post not found.')
            await m.react('⬇️')
            try:
                await send_post(conn, m.chat, state['posts'][idx], m)
                await m.react('✅')
            except Exception as err:
                await m.react('❌')
                await m.reply(f'Failed to download: {err}')
            return
        if sub == 'page':
            state = _state.get(rest[0] if rest else None)
            target = max(1, int(rest[1]) if len(rest) > 1 and re.match(r'^\d+$', rest[1]) else 1)
            if not state:
                return await m.reply('This search result has expired, please search again.')
            if state['type'] == 'pool':
                pp = await sc.getPoolPage(state['poolId'], target)
                if not pp or not pp['posts']:
                    return await m.reply(f'No posts on page {target}.')
                posts, has_next, label = pp['posts'], pp['totalPages'] > target, f"*Pool:* {pp['name']}"
            else:
                posts, has_next = await search_tags(state['keywords'], target)
                if not posts:
                    return await m.reply(f'No results on page {target}.')
                label = f"*Query:* {state['keywords']}"
            return await send_gallery(conn, m, state['type'], state['keywords'], state['poolId'], label, posts, target, has_next, command)
        if sub == 'dlall':
            state = _state.get(rest[0] if rest else None)
            if not state or state['type'] != 'pool':
                return await m.reply('Download All is only available for pool galleries, or this session has expired.')
            await m.react('📦')
            try:
                allp = await fetch_all_pool_posts(state['poolId'])
                if not allp or not allp['posts']:
                    raise RuntimeError('Failed to fetch pool contents.')
                posts = allp['posts']
                truncated = len(posts) > MAX_DLALL_POSTS
                posts = posts[:MAX_DLALL_POSTS]
                note = f' (limited to first {MAX_DLALL_POSTS})' if truncated else ''
                if all((p.get('ext') or '').lower() in STATIC_IMAGE_EXTS for p in posts):
                    pdf = await converter.img2pdf([p['url'] for p in posts if p.get('url')])
                    await conn.sendMessage(m.chat, {'document': pdf, 'mimetype': 'application/pdf', 'fileName': f"e621-pool_{state['poolId']}.pdf",
                                                    'caption': f"Pool #{state['poolId']} — {len(posts)} images{note} (PDF)"}, {'quoted': m})
                else:
                    z = await build_pool_zip(posts)
                    await conn.sendMessage(m.chat, {'document': z, 'mimetype': 'application/zip', 'fileName': f"e621-pool_{state['poolId']}.zip",
                                                    'caption': f"Pool #{state['poolId']} — {len(posts)} files{note}"}, {'quoted': m})
                await m.react('✅')
            except Exception as err:
                await m.react('❌')
                await m.reply(f'Failed to download all: {err}')
            return
        pm = re.match(r'^https?://e621\.net/pools/(\d+)', text, re.I)
        if pm:
            pool_id = pm.group(1)
            pp = await sc.getPoolPage(pool_id, 1)
            if not pp:
                return await m.reply('Pool not found or invalid URL.')
            if not pp['posts']:
                return await m.reply('This pool has no posts.')
            return await send_gallery(conn, m, 'pool', None, pool_id, f"*Pool:* {pp['name']}", pp['posts'], 1, pp['totalPages'] > 1, command)
        if re.match(r'^(https?://[^\s]+)$', text, re.I):
            post = await sc.getPost(text)
            if not post:
                return await m.reply('Post not found or invalid URL.')
            if post['size'] > 300 * 1024 * 1024:
                return await m.reply(f"File is too large ({post['size'] / 1024 / 1024:.1f} MB), maximum 300MB.\n{post.get('pageUrl') or text}")
            return await send_post(conn, m.chat, post, m)
        keywords, page = text, 1
        pg = re.match(r'^(.+?)\s*\|\s*page\s*(\d+)$', text, re.I)
        if pg:
            keywords, page = pg.group(1).strip(), max(1, int(pg.group(2)))
        posts, has_next = await search_tags(keywords, page)
        if not posts:
            return await m.reply(f"No results found{f' on page {page}' if page > 1 else ''}.")
        await send_gallery(conn, m, 'search', keywords, None, f'*Query:* {keywords}', posts, page, has_next, command)
    except Exception as e:
        m.error = e
        print('[e621] Handler error:', e, file=sys.stderr)
        await m.reply(f'Error: {e}')


help = command = ['e621']
tags = ['downloader', 'adult']
limit = 1
ai = {'risk': 'low', 'description': 'search e621 posts using keywords, download post using post id'}
