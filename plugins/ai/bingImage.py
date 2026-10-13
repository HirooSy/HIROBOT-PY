import re
import sys

from lib.utils.handler import G, PluginError


async def handler(m, *, conn, text, usedPrefix, command):
    text, p, command = text, usedPrefix, command
    ai_image = G.scraper['ai-image']
    inp = (text or (m.quoted.text if m.quoted else '') or '').strip()
    ratio = '1:1'
    mt = re.search(r'(?:^|\s)(?:--)?(1:1|3:2|2:3)(?=\s|$)', inp)
    if mt:
        ratio = mt.group(1)
        inp = re.sub(r'\s+', ' ', inp.replace(mt.group(0), ' ', 1)).strip()
    if not inp:
        raise PluginError(f"> Where The Prompt?\n\nExample: {p + command} an astronaut cat riding a skateboard\nRatio (optional): {', '.join(ai_image.ASPECT_RATIOS)}\nExample: {p + command} 3:2 mountain sunset")
    await m.react('⏳')
    try:
        urls = await ai_image.generateImage(inp, {'aspectRatio': ratio})
        if not urls:
            raise RuntimeError('No image was returned by Bing.')
        try:
            await conn.aiRich().addText(inp).addImage(urls, {'resolveUrl': True}).send(m.chat, {'quoted': m})
        except Exception as rich_err:
            print('[bingImage] aiRich failed, fallback sendFile:', rich_err, file=sys.stderr)
            for url in urls:
                await conn.sendFile(m.chat, url, 'bing.png', inp, m)
        await m.react('✅')
    except Exception as e:
        m.error = e
        raise


help = ['bingimage [1:1|3:2|2:3] <prompt>']
tags = ['ai']
command = re.compile(r'^(bingimage|bingimg|bing)$', re.I)
limit = False
