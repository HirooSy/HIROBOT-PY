import asyncio
import json
import os
import re
import sys
import tempfile
import time

from lib.compat import ffmpeg as ff, http
from lib.compat.html import load


UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'
PH_HEADERS = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
              'Accept-Language': 'en-US,en;q=0.5', 'Referer': 'https://www.pornhub.com'}


def is_link(text):
    return re.findall(r'https?://\S+', text, re.I) if text else None


def ensure_tmp():
    tmp = os.path.join(os.getcwd(), 'data', 'tmp')
    try:
        os.makedirs(tmp, exist_ok=True)
        if not os.access(tmp, os.W_OK):
            raise PermissionError(tmp)
        return tmp
    except OSError:
        return tempfile.gettempdir()


async def hls_to_mp4(m3u8):
    tmp_dir = ensure_tmp()
    try:
        for f in os.listdir(tmp_dir):
            if f.startswith('ph_tmp_'):
                try:
                    os.unlink(os.path.join(tmp_dir, f))
                except OSError:
                    pass
    except OSError:
        pass
    out = os.path.join(tmp_dir, f'ph_tmp_{int(time.time() * 1000)}.mp4')
    try:
        await ff.run(ff.FFMPEG, ['-y', '-allowed_extensions', 'ALL', '-protocol_whitelist', 'file,http,https,tcp,tls,crypto', '-headers',
                                 f'User-Agent: {UA}\r\nReferer: https://www.pornhub.com\r\n', '-i', m3u8, '-c', 'copy', '-movflags', 'faststart',
                                 '-bsf:a', 'aac_adtstoasc', '-f', 'mp4', out])
    except Exception as err:
        raise RuntimeError(f'ffmpeg: {err}')
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError('Remux failed — output empty.')
    return out


def extract_viewkey(url):
    m = re.search(r'[?&]viewkey=([a-zA-Z0-9]+)', url) or re.search(r'/video/([a-zA-Z0-9]+)', url)
    return m.group(1) if m else None


async def scrape(url):
    vk = extract_viewkey(url)
    if not vk:
        raise RuntimeError('Could not extract viewkey from URL.')
    res = await http.get(f'https://www.pornhub.com/view_video.php?viewkey={vk}', headers=PH_HEADERS, timeout=20)
    c = load(res.text())
    title = re.sub(r'\s*[-–]\s*Pornhub\.com\s*$', '', c('title').text(), flags=re.I).strip() or (c('meta[property="og:title"]').attr('content') or '').strip() or 'Unknown Title'
    thumbnail = c('meta[property="og:image"]').attr('content')
    uploader = c('.usernameBadgesWrapper a').first().text().strip() or c('a.bolded').first().text().strip() or 'Unknown'
    flashvars = None
    for el in c('script'):
        raw = el.inner_html()
        if 'mediaDefinitions' not in raw:
            continue
        mt = re.search(r'var\s+flashvars_\d+\s*=\s*(\{[\s\S]*?\})\s*;', raw)
        if not mt:
            continue
        try:
            flashvars = json.loads(mt.group(1))
            break
        except Exception:
            try:
                start = raw.index('{', raw.index('flashvars_'))
                end = raw.rfind('}', 0, raw.index(';\n', start) + 1) + 1
                flashvars = json.loads(raw[start:end])
                break
            except Exception:
                continue
    if not flashvars:
        raise RuntimeError('Could not locate flashvars. Video may be age-gated or geo-blocked.')
    urls = []
    for d in flashvars.get('mediaDefinitions') or []:
        if not d.get('videoUrl'):
            continue
        q = f"{d['quality']}p" if d.get('quality') else None
        if d.get('format') == 'mp4':
            urls.append({'type': 'MP4', 'quality': q or 'Default', 'link': [d['videoUrl']]})
        elif d.get('format') == 'hls':
            urls.append({'type': 'HLS', 'quality': q or 'Auto', 'link': [d['videoUrl']]})

    def qnum(x):
        mm = re.match(r'\d+', x['quality'])
        return int(mm.group(0)) if mm else 0
    urls.sort(key=lambda x: (x['type'] == 'HLS', -qnum(x)))
    if not urls:
        raise RuntimeError('No downloadable media found — video may be premium-only.')
    if thumbnail:
        urls.append({'type': 'JPG', 'quality': 'Thumbnail', 'link': [thumbnail]})
    return {'title': title, 'uploader': uploader, 'thumbnail': thumbnail, 'videoUrls': urls}


async def handler(m, *, conn, text, usedPrefix, command):
    links = is_link(text)
    if not text or not links:
        return await conn.reply(m.chat, f"Please send a Pornhub video link.\n> Example: {usedPrefix + command} https://www.pornhub.com/view_video.php?viewkey=XXXX", m)
    conn.pornhub = conn.pornhub or {}
    try:
        await m.react('⏳')
        data = await scrape(links[0])
    except Exception as e:
        return await conn.reply(m.chat, f'⚠️ Failed: {e}', m)
    menu = '\n'.join(f"*_{i + 1}. {it['type']} — {it['quality']}_*" for i, it in enumerate(data['videoUrls']))
    await conn.reply(m.chat, f"*{data['title']}*\nUploader: {data['uploader']}\n──────────────\n\nSelect a format:\n{menu}", m)
    loop = asyncio.get_event_loop()
    conn.pornhub[m.sender] = {'title': data['title'], 'allLinks': [v['link'] for v in data['videoUrls']], 'types': [v['type'] for v in data['videoUrls']],
                              'timeout': loop.call_later(160, lambda: conn.pornhub.pop(m.sender, None))}


async def before(m, *, conn):
    conn.pornhub = conn.pornhub or {}
    if m.sender not in conn.pornhub:
        return
    session = conn.pornhub[m.sender]
    nums = re.findall(r'\d+', m.text or '')
    if not nums:
        return
    index = int(nums[0]) - 1
    if index < 0 or index >= len(session['allLinks']):
        return await conn.reply(m.chat, 'Please pick a valid number from the list.', m, {'ephemeralExpiration': 86400})
    try:
        await m.react('⬇️')
        cap = f"*{session['title']}*"
        for url in session['allLinks'][index]:
            kind = session['types'][index]
            if kind == 'JPG':
                await conn.sendFile(m.chat, url, 'thumbnail.jpg', cap, m)
            elif kind == 'HLS':
                path = None
                try:
                    path = await hls_to_mp4(url)
                    await conn.sendFile(m.chat, path, 'video.mp4', cap, m)
                except Exception as e:
                    print('[ph HLS send error]', e, file=sys.stderr)
                    await conn.reply(m.chat, f'⚠️ Send failed: {e}', m)
                    m.error = e
                finally:
                    if path:
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
            else:
                await conn.sendFile(m.chat, url, 'video.mp4', cap, m)
    except Exception as e:
        print(e, file=sys.stderr)
        await conn.reply(m.chat, f'⚠️ Error: {e}', m)
        m.error = e
    finally:
        try:
            session['timeout'].cancel()
        except Exception:
            pass
        conn.pornhub.pop(m.sender, None)


help = [v + ' <url>' for v in ('ph', 'pornhub')]
tags = ['adult']
command = ['ph', 'pornhub']
limit = 1
level = 3
ai = {'risk': 'low', 'description': 'download p#rnhub post using url'}
