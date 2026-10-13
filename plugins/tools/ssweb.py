import re
from http.cookiejar import CookieJar

from lib.compat import http


async def ssweb(url, device='desktop'):
    base = 'https://www.screenshotmachine.com'
    jar = CookieJar()
    res = await http.post(f'{base}/capture.php', form={'url': url, 'device': device, 'cacheLimit': 0, 'full': 'true'},
                          headers={'content-type': 'application/x-www-form-urlencoded; charset=UTF-8'}, cookies=jar)
    try:
        data = res.json()
    except Exception:
        data = None
    if isinstance(data, dict) and data.get('status') == 'success':
        img = await http.get(f"{base}/{data['link']}", cookies=jar)
        return {'status': 200, 'result': img.content}
    return {'status': 404, 'result': 'failed'}


async def handler(m, *, conn, text, usedPrefix, command):
    if not text:
        return await m.reply(f"- Input url.\n{usedPrefix + command} <url> -<desktop/phone/tablet>")
    kind = 'desktop'
    if re.search('phone', text):
        kind = 'phone'
    if re.search('tablet', text):
        kind = 'tablet'
    await m.react('🔎')
    result = await ssweb(text.split(' ')[0], kind)
    try:
        await conn.sendFile(m.chat, result['result'], '', None, m)
        await m.react('✅')
    except Exception as e:
        m.error = e
        raise


help = ['ssweb <url>']
tags = ['tools']
command = re.compile(r'^(ssweb)$', re.I)
registered = True
