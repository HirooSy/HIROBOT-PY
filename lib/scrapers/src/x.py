import asyncio
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

from lib.compat import ffmpeg as ff, http
from lib.compat.html import load


UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'
TMP_DIR = os.path.join(os.getcwd(), 'data', 'tmp')
_SSL_CTX = ssl.create_default_context()


def _ensure_tmp():
    os.makedirs(TMP_DIR, exist_ok=True)


_ensure_tmp()


def isLink(text):
    return re.findall(r'https?://\S+', text or '', re.I) or None


def _nowIso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _jsStr(value):
    return 'undefined' if value is None else str(value)


def _substring(s, start, end):
    start = max(0, min(start, len(s)))
    end = max(0, min(end, len(s)))
    if start > end:
        start, end = end, start
    return s[start:end]


def _stamp():
    return f'{int(time.time() * 1000)}_{os.urandom(3).hex()}'


def _downloadStream(fileUrl, outputPath):
    req = urllib.request.Request(fileUrl, headers={'User-Agent': UA}, method='GET')
    try:
        resp = urllib.request.urlopen(req, timeout=120, context=_SSL_CTX)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Request failed with status code {error.code}') from None
    with resp, open(outputPath, 'wb') as fh:
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            fh.write(chunk)


async def _downloadInput(fileUrl, tmpInput):
    await asyncio.to_thread(_downloadStream, fileUrl, tmpInput)
    if not os.path.exists(tmpInput) or os.path.getsize(tmpInput) == 0:
        raise RuntimeError('Download gagal, file input kosong')


async def _runFfmpeg(args, tmpInput, tmpOutput, removeInputOnError):
    try:
        await ff.ffmpeg(args)
    except Exception as err:
        if removeInputOnError and os.path.exists(tmpInput):
            os.unlink(tmpInput)
        raise RuntimeError(f'ffmpeg error: {err}\n')
    if os.path.exists(tmpInput):
        os.unlink(tmpInput)
    if not os.path.exists(tmpOutput) or os.path.getsize(tmpOutput) == 0:
        raise RuntimeError('Konversi gagal, file output kosong atau tidak ditemukan')
    return tmpOutput


async def gifToMp4(fileUrl):
    try:
        for f in os.listdir(TMP_DIR):
            if f.startswith('tmp_'):
                try:
                    os.unlink(os.path.join(TMP_DIR, f))
                except OSError:
                    pass
    except OSError:
        pass

    stamp = _stamp()
    tmpInput = os.path.join(TMP_DIR, f'tmp_in_{stamp}.mp4')
    tmpOutput = os.path.join(TMP_DIR, f'tmp_out_{stamp}.mp4')

    await _downloadInput(fileUrl, tmpInput)

    nowIso = _nowIso()
    args = [
        '-i', tmpInput,
        '-map_metadata', '-1',
        '-metadata', f'creation_time={nowIso}',
        '-metadata:s:v:0', f'creation_time={nowIso}',
        '-metadata:s:a:0', f'creation_time={nowIso}',
        '-movflags', 'faststart',
        '-pix_fmt', 'yuv420p',
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
        '-f', 'mp4',
        tmpOutput,
    ]
    return await _runFfmpeg(args, tmpInput, tmpOutput, False)


async def fixVideoMetadata(fileUrl):
    stamp = _stamp()
    tmpInput = os.path.join(TMP_DIR, f'tmp_in_{stamp}.mp4')
    tmpOutput = os.path.join(TMP_DIR, f'tmp_out_{stamp}.mp4')

    await _downloadInput(fileUrl, tmpInput)

    nowIso = _nowIso()
    args = [
        '-i', tmpInput,
        '-map_metadata', '-1',
        '-metadata', f'creation_time={nowIso}',
        '-metadata:s:v:0', f'creation_time={nowIso}',
        '-metadata:s:a:0', f'creation_time={nowIso}',
        '-c', 'copy',
        '-movflags', 'faststart',
        '-f', 'mp4',
        tmpOutput,
    ]
    return await _runFfmpeg(args, tmpInput, tmpOutput, True)


def _scriptValue(c, key):
    found = None
    for el in c('script'):
        node = c(el)
        if key in node.html():
            found = node.html()
            break
    if found is None:
        raise TypeError("Cannot read properties of undefined (reading 'match')")
    mt = re.search(key + r'\s*=\s*"([^"]*)"', found)
    if not mt:
        raise TypeError("Cannot read properties of null (reading '1')")
    return mt.group(1)


async def twitter(url):
    if not url or not isinstance(url, str):
        raise ValueError('Invalid URL provided.')
    urls = re.findall(r'(https?://[^\s]+)', url)
    if not urls:
        raise ValueError('No URL found.')

    res = await http.post('https://savetwitter.net/api/ajaxSearch',
                          data=urlencode({'q': urls[0], 'lang': 'id'}),
                          headers={
                              'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                              'Accept': '*/*',
                              'X-Requested-With': 'XMLHttpRequest',
                          })

    c = load(res.json()['data'])
    twitterId = c('#TwitterId').attr('value')
    mp3Link = c('a[data-audiourl]').attr('data-audiourl')

    usernameIndex = url.find('.com/') + 5
    usernameEndIndex = url.find('/status/')
    nickname = _substring(url, usernameIndex, usernameEndIndex)

    kexp = _scriptValue(c, 'k_exp')
    ktoken = _scriptValue(c, 'k_token')

    thumbnail = c('.thumbnail .image-tw img').attr('src')

    gifUrl = c('.dl-action a').filter(lambda i, el: 'Unduh MP4 (gif)' in c(el).text()).attr('href')
    imageUrl = c('.dl-action a').filter(lambda i, el: 'unduh gambar' in c(el).text().lower()).attr('href')

    if gifUrl:
        return {
            'username': nickname,
            'description': c('h3').text().strip(),
            'type': 'gif',
            'thumbnail': thumbnail,
            'gif': gifUrl,
            'image': imageUrl or None,
            'videoUrls': [],
        }

    links = []
    for el in c('a'):
        node = c(el)
        text = node.text()
        href = node.attr('href')
        if 'Unduh MP4' in text:
            mt = re.search(r'\((\d+p)\)', text)
            links.append({'type': 'MP4', 'quality': mt.group(1) if mt else 'Unknown', 'link': [href]})

    imgLinks = []
    for el in c('img'):
        src = c(el).attr('src')
        if src:
            imgLinks.append(src)
    if imgLinks:
        links.append({'type': 'JPG', 'quality': 'Image', 'link': imgLinks})

    if mp3Link:
        mp3Res = await http.post('https://s1.twcdn.net/api/json/convert',
                                 data=urlencode({
                                     'ftype': 'mp3',
                                     'v_id': _jsStr(twitterId),
                                     'audioUrl': mp3Link,
                                     'audioType': 'audio/mp4',
                                     'fquality': '320',
                                     'fname': 'SaveTwitter.Net',
                                     'exp': kexp,
                                     'token': ktoken,
                                 }),
                                 headers={
                                     'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                     'Accept': '*/*',
                                 })
        try:
            mp3Data = mp3Res.json()
        except ValueError:
            mp3Data = None
        result = mp3Data.get('result') if isinstance(mp3Data, dict) else None
        if result != 'Converting':
            links.append({'type': 'MP3', 'quality': '320kbps', 'link': [result]})

    return {
        'username': nickname,
        'description': c('h3').text().strip(),
        'thumbnail': c('a[onclick="showAd()"]').attr('href'),
        'videoUrls': links,
    }
