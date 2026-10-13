import asyncio
import re
from http.cookiejar import CookieJar
from urllib.parse import quote

from lib.compat import http


HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'id,en;q=0.9',
    'origin': 'https://nanobanana.im',
}
REQ_TIMEOUT = 15
MAIL_POLL_INTERVAL = 3
MAIL_POLL_MAX_TRIES = 40
TASK_POLL_INTERVAL = 4
TASK_POLL_MAX_TRIES = 45
DEFAULT_OPTS = {
    'dimension': 'auto',
    'aspect_ratio': 'auto',
    'num_images': '1',
    'size': '2K',
    'resolution': '2K',
    'output_format': 'png',
    'turnstileToken': '',
}


def _obj(value):
    return value if isinstance(value, dict) else {}


def _enc(text):
    return quote(text, safe="-_.!~*'()")


async def getMagicLink(email):
    for _ in range(MAIL_POLL_MAX_TRIES):
        try:
            res = await http.get(f'https://api.tempmail.ing/api/emails/{_enc(email)}', headers=HEADERS, timeout=REQ_TIMEOUT)
            data = _obj(res.json())
            emails = data.get('emails')
            if data.get('success') and emails:
                first = _obj(emails[0])
                text = first.get('text') or first.get('html') or ''
                mt = re.search(r'https://nanobanana\.im/api/auth/magic-link/verify\?token=[^\s"\']+', text)
                if mt:
                    return mt.group(0)
        except Exception:
            pass
        await asyncio.sleep(MAIL_POLL_INTERVAL)
    raise RuntimeError('Timeout menunggu magic link masuk.')


async def createAuthedSession():
    jar = CookieJar()
    mail = _obj((await http.post('https://api.tempmail.ing/api/generate', json={}, headers=HEADERS, timeout=REQ_TIMEOUT)).json())
    if not mail.get('success'):
        raise RuntimeError('Gagal membuat tempmail.')
    email = mail['email']['address']

    await http.get('https://nanobanana.im/', headers=HEADERS, timeout=REQ_TIMEOUT, cookies=jar)

    magic = await http.post('https://nanobanana.im/api/auth/sign-in/magic-link',
                            json={'email': email, 'callbackURL': '/'},
                            headers=HEADERS, timeout=REQ_TIMEOUT, cookies=jar)
    if not _obj(magic.json()).get('status'):
        raise RuntimeError('Gagal mengirim magic link.')

    link = await getMagicLink(email)
    await http.get(link, headers=HEADERS, timeout=REQ_TIMEOUT, cookies=jar, max_redirects=0,
                   validate_status=lambda s: 200 <= s < 400)

    await http.get('https://nanobanana.im/', headers=HEADERS, timeout=REQ_TIMEOUT, cookies=jar)
    return jar


async def createAndPollTask(jar, payload):
    taskRes = await http.post('https://nanobanana.im/api/img/nano-banana5', json=payload,
                              headers=HEADERS, timeout=REQ_TIMEOUT, cookies=jar)
    taskId = _obj(taskRes.json()).get('taskId')
    if not taskId:
        raise RuntimeError('Gagal membuat task gambar. Cek turnstileToken atau session.')

    for _ in range(TASK_POLL_MAX_TRIES):
        checkRes = await http.post('https://nanobanana.im/api/img/nano-banana5/taskResult', json={'taskId': taskId},
                                   headers=HEADERS, timeout=REQ_TIMEOUT, cookies=jar)
        d = _obj(checkRes.json())
        if d.get('status') == 1:
            raw = d.get('imgAfterSrcs') or d.get('images') or d.get('imgAfterSrc') or d.get('results')
            urls = raw if isinstance(raw, list) else [raw]
            return [u for u in urls if u]
        await asyncio.sleep(TASK_POLL_INTERVAL)

    raise RuntimeError('Timeout menunggu hasil gambar.')


def _payload(prompt, imageUrls, opts):
    o = {**DEFAULT_OPTS, **(opts or {})}
    return {
        'prompt': prompt,
        'dimension': o['dimension'],
        'aspect_ratio': o['aspect_ratio'],
        'image_urls': imageUrls,
        'num_images': o['num_images'],
        'batchSize': 1,
        'turnstileToken': o['turnstileToken'],
        'skipVerification': False,
        'image_path': 'hero',
        'size': o['size'],
        'resolution': o['resolution'],
        'output_format': o['output_format'],
    }


async def nanobanana(prompt, opts=None):
    jar = await createAuthedSession()
    return await createAndPollTask(jar, _payload(prompt, [], opts))


async def nanoEditImage(imageUrl, prompt, opts=None):
    jar = await createAuthedSession()
    return await createAndPollTask(jar, _payload(prompt, [imageUrl], opts))
