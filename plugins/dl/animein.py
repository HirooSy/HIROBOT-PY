import asyncio
import os
import re
import sys
import time

from lib.utils.handler import G


TMP_DIR = os.environ.get('TMP') if os.path.isabs(os.environ.get('TMP') or '') else os.path.join(os.getcwd(), os.environ.get('TMP') or 'data/tmp')
FAVICON = 'https://animeinweb.com/favicon.ico'
QUALITY_ORDER = ['1080p', '720p', '480p', '360p']


def js_str(v):
    if v is None:
        return 'undefined'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return str(v)


def js_parse_int(value):
    mt = re.match(r'\s*[+-]?\d+', str(value or ''))
    return int(mt.group(0)) if mt else None


def quality_rank(q):
    return QUALITY_ORDER.index(q) if q in QUALITY_ORDER else -1


def free_space(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return None


def send_list(conn, m, caption, rows, button_text='Select'):
    return conn.sendButton(m.chat, {'document': {'url': FAVICON}, 'mimetype': 'image/png', 'fileName': 'ANIMEIN', 'caption': caption,
                                    'fileLength': '665666646645000', 'optionText': button_text, 'optionTitle': button_text,
                                    'nativeFlow': [{'text': button_text, 'sections': [{'rows': rows}]}]}, m)


async def handler(m, *, conn, text, command):
    an = G.scraper.animein
    if not text:
        return await m.reply(f'*ANIMEIN*\n\nUsage: .{command} <title>\n\nExample:\n.{command} pokemon')
    parts = text.strip().split(' ')
    sub, args = parts[0], parts[1:]
    if sub == 'info':
        anime_id = args[0] if args else None
        ep_page = js_parse_int(args[1] if len(args) > 1 else None) or 0
        limit_ep = 200
        if not anime_id:
            return
        await m.react('🔍')
        detail, all_eps = await asyncio.gather(an.getDetail(anime_id), an.getEpisodes(anime_id))
        if not detail or not detail.get('movie'):
            return await m.reply('Anime not found.')
        mv = detail['movie']
        synopsis = mv.get('synopsis')
        synopsis = js_str(synopsis[:400] if isinstance(synopsis, str) else None)
        caption = (f"*{js_str(mv.get('title'))}*\n━━━━━━━━━━━━━━\n- Type: {js_str(mv.get('type'))}\n- Year: {js_str(mv.get('year'))}\n- Status: {js_str(mv.get('status'))}\n"
                   f"- Studio: {mv.get('studio') or '-'}\n- Aired: {js_str(mv.get('aired_start'))} ~ {js_str(mv.get('aired_end') or '?')}\n- Genre: {js_str(mv.get('genre'))}\n"
                   f"- Views: {js_str(mv.get('views'))} | Favorites: {js_str(mv.get('favorites'))}\n━━━━━━━━━━━━━━\n{synopsis}...\n\n{len(all_eps)} episode(s) available")
        if not all_eps:
            return await m.reply(caption)
        start = ep_page * limit_ep
        eps = all_eps[start:start + limit_ep]
        has_next = len(all_eps) > start + limit_ep
        if not eps:
            return await m.reply('No more episodes.')
        first = await an.getStream(eps[0]['id'])
        if not first:
            return await m.reply('Failed to get stream data.')
        direct = [s for s in (first.get('server') or []) if s.get('type') == 'direct']
        qualities = [q for q in QUALITY_ORDER if any(s.get('quality') == q for s in direct)]
        sizes = {}
        for s in direct:
            if not sizes.get(s.get('quality')):
                sizes[s.get('quality')] = await an.getFileSize(s.get('link'))
                await asyncio.sleep(0.5)
        page_caption = caption + (f'\n\nPage {ep_page + 1} (showing {start + 1}-{start + len(eps)})' if has_next else '')
        if not qualities:
            rows = [{'header': js_str(ep.get('title')), 'title': js_str(ep.get('key_time')), 'description': f"Views: {js_str(ep.get('views'))}", 'id': f".{command} dl {ep['id']}"} for ep in eps]
            flow = [{'text': 'Select Episode', 'sections': [{'rows': rows}]}]
        else:
            flow = [{'text': q, 'sections': [{'rows': [{'header': js_str(ep.get('title')), 'title': js_str(ep.get('key_time')),
                                                         'description': f"Views: {js_str(ep.get('views'))} • Size: {an.formatSize(sizes.get(q))}",
                                                         'id': f".{command} dl {ep['id']} {q}"} for ep in eps]}]} for q in qualities]
        if has_next:
            flow.append({'text': '➡️ Next', 'id': f'.{command} info {anime_id} {ep_page + 1}'})
        return await conn.sendButton(m.chat, {'document': {'url': FAVICON}, 'mimetype': 'image/png', 'fileName': 'ANIMEIN', 'caption': page_caption,
                                              'fileLength': '665666646645000', 'optionText': 'Select', 'optionTitle': 'Select', 'nativeFlow': flow}, m)
    if sub == 'dl':
        episode_id = args[0] if args else None
        quality = args[1] if len(args) > 1 else None
        if not episode_id:
            return
        await m.react('⬇️')
        stream = await an.getStream(episode_id)
        if not stream or not stream.get('server'):
            return await m.reply('Server not found.')
        direct = [s for s in stream['server'] if s.get('type') == 'direct']
        if not direct:
            return await m.reply('No direct servers available.')
        direct.sort(key=lambda s: quality_rank(s.get('quality')))
        target = next((s for s in direct if s.get('quality') == quality), None) if quality else None
        target = target or direct[0]
        ep = stream['episode']
        os.makedirs(TMP_DIR, exist_ok=True)
        expected = await an.getFileSize(target['link'])
        available = free_space(TMP_DIR)
        if expected and available is not None and expected + 100 * 1024 * 1024 > available:
            await m.react('❌')
            return await m.reply(f'Ruang penyimpanan server tidak cukup.\nDibutuhkan: {an.formatSize(expected)}\nTersedia: {an.formatSize(available)}\n\nHubungi admin server untuk membersihkan storage atau menambah kuota.')
        tmp = os.path.join(TMP_DIR, f'animein_{episode_id}_{int(time.time() * 1000)}.mp4')
        try:
            total = await an.downloadEpisode(target['link'], tmp)
            await m.react('✅')
            await conn.sendMessage(m.chat, {'document': {'url': tmp}, 'mimetype': 'video/mp4', 'fileName': f"{ep['title']} - {target['quality']}.mp4",
                                            'caption': f"*{ep['title']}* | {target['quality']} | {js_str(target.get('name'))} | {an.formatSize(total)}"}, {'quoted': m})
        except Exception as error:
            print('Download failed:', error, file=sys.stderr)
            full = getattr(error, 'errno', None) == 28 or getattr(error, 'code', None) == 'ENOSPC'
            await m.reply('Download failed: server storage is full. Contact owner to clean up storage.' if full else f'Download failed: {str(error) or "Unknown error"}')
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return
    await m.react('🔍')
    keyword = text.strip()
    results = await an.searchAnime(keyword)
    if not results:
        return await m.reply('No results found.')
    rows = [{'header': f"{i + 1}. {js_str(a.get('title'))}", 'title': f"{js_str(a.get('type'))} | {js_str(a.get('status'))} | {js_str(a.get('year'))}",
             'description': f"Views: {js_str(a.get('views'))} | Favorites: {js_str(a.get('favorites'))} | {js_str(a.get('genre'))}", 'id': f".{command} info {a['id']}"}
            for i, a in enumerate(results[:1000])]
    return await send_list(conn, m, f'*{keyword}* - {len(results)} result(s)', rows, 'Select Anime')


help = command = ['animein']
tags = ['downloader', 'internet']
limit = 0
ai = {'risk': 'low', 'description': 'search/download anime'}
