import re
from urllib.parse import quote

from lib.compat import http


async def handler(m, *, text, usedPrefix, command):
    if not text:
        return await m.reply(f"Example: {usedPrefix + command} https://example.com")
    try:
        encoded = quote(text, safe="-_.!~*'()")
        res = await http.get(f'https://tinyurl.com/api-create.php?url={encoded}', raise_for_status=False)
        if not res.ok:
            raise RuntimeError(f'HTTP {res.status}')
        await m.reply(res.content.decode('utf-8', errors='replace'))
    except Exception as e:
        await m.reply(f'Failed to shorten URL!\n{e}')


help = ['tinyurl <url>']
tags = ['tools']
command = re.compile(r'^(tinyurl)$', re.I)
