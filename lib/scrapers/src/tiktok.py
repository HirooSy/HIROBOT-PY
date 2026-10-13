import os
import re

from lib.compat import ffmpeg as ff, http


UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


def isLink(text):
    return re.findall(r'https?://\S+', text or '', re.I) or None


async def tiktok(url):
    link = isLink(url)
    if not link:
        raise ValueError('URL tidak valid')
    res = await http.post('https://www.tikwm.com/api/', json={'url': link[0], 'count': 12, 'cursor': 0, 'web': 1, 'hd': 1},
                          headers={'Content-Type': 'application/json', 'User-Agent': UA})
    try:
        data = res.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not data.get('data'):
        raise RuntimeError('Gagal mengambil data TikTok')
    return data['data']


def normalize_video_url(url):
    if not url:
        raise ValueError('URL video kosong/tidak tersedia dari tikwm')
    if re.match(r'^https?://', url, re.I):
        return url
    return 'https://www.tikwm.com' + ('' if url.startswith('/') else '/') + url


async def tiktokBoostVolume(video_url):
    fixed = normalize_video_url(video_url)
    video = (await http.get(fixed, headers={'User-Agent': UA, 'Referer': 'https://www.tikwm.com/'})).content
    return await ff.convert_bytes(video, 'mp4', 'mp4', ['-filter:a', 'volume=4.0', '-c:v', 'copy'])
