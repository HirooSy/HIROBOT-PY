import asyncio
import re

from baileys_lite import downloadMediaMessage, S_WHATSAPP_NET
from lib.utils.canvas import jimp
from lib.utils.handler import G, PluginError
from lib.utils.helper import spawn


async def generate_profile_picture(media):
    return await jimp.resize(media, 720, 720, {'fit': 'inside', 'out': 'jpeg'})


async def handler(m, *, conn):
    q = m.quoted or m
    mime = q.mimetype or q.mediaType or ''

    async def set_profile_picture(image_buffer):
        query = asyncio.ensure_future(conn.query({
            'tag': 'iq',
            'attrs': {'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'w:profile:picture'},
            'content': [{'tag': 'picture', 'attrs': {'type': 'image'}, 'content': image_buffer}],
        }))
        spawn(m.reply('Success'))
        return await query

    if not re.search('image', mime):
        raise PluginError("Where's the media?")
    try:
        raw = await q.download()
    except Exception:
        raw = await downloadMediaMessage({'key': m.key, 'message': m.message}, 'buffer', {}, {'reuploadRequest': conn.updateMediaMessage})
    url = await G.scraper.ezgif.webp2png(raw) if re.search('webp', mime) else raw
    image = await generate_profile_picture(url)
    await set_profile_picture(image)


help = ['setbotpp']
command = re.compile(r'^setbotpp$', re.I)
owner = True
ai = {'risk': 'low', 'description': "change bot's profile picture"}
