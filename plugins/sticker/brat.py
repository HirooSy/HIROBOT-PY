import re

from lib.utils.handler import G, PluginError
from lib.utils.helper import sleep


async def handler(m, *, conn, text, usedPrefix, command):
    if not text:
        raise PluginError(f"*• Example :* {usedPrefix + command} [text]")
    brat = G.scraper.brat
    is_video = bool(re.match(r'^bratvid$', command, re.I))
    last = None
    try:
        for attempt in range(1, 4):
            try:
                res = await (brat.generateBratVideo(text) if is_video else brat.generateBratImage(text))
                await conn.sendSticker(m.chat, res['buffer'], {}, m)
                if res['failures']:
                    await m.reply(brat.buildFailureWarning(res['failures']))
                return
            except Exception as e:
                last = e
                if attempt < 3:
                    await sleep(1000 * attempt)
        raise last
    except Exception as e:
        await m.reply(f'❌ Failed: {e}')
        m.error = e


help = ['brat <text>', 'bratvid <text>']
tags = ['sticker']
command = re.compile(r'^brat(vid)?$', re.I)
limit = 1
ai = {'risk': 'low', 'description': 'create a sticker. "/brat <text>" for image, "/bratvid <text>" for video'}
