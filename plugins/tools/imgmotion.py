import asyncio
import re
import sys

from baileys_lite import generateWAMessageFromContent, prepareWAMessageMedia
from lib.compat import http
from lib.utils.helper import oc


def _mime(msg):
    return str(oc(msg, 'msg', 'mimetype') or '')


def is_image(msg):
    if msg is None:
        return False
    return oc(msg, 'mtype') == 'imageMessage' or oc(msg, 'message', 'imageMessage') is not None or 'image' in _mime(msg)


def is_video(msg):
    if msg is None:
        return False
    return oc(msg, 'mtype') == 'videoMessage' or oc(msg, 'message', 'videoMessage') is not None or 'video' in _mime(msg)


def _state(conn):
    st = getattr(conn, 'imgmotion', None)
    if st is None:
        st = {}
        setattr(conn, 'imgmotion', st)
    return st


def _expire(conn, sender):
    _state(conn).pop(sender, None)


async def send_motion(conn, m, img, video):
    try:
        image_media = await prepareWAMessageMedia({'image': img}, {'upload': conn.waUploadToServer})
        video_media = await prepareWAMessageMedia({'video': video}, {'upload': conn.waUploadToServer})
        msg = generateWAMessageFromContent(m.chat, {'imageMessage': {**image_media['imageMessage'], 'caption': '', 'contextInfo': {'pairedMediaType': 5, 'statusSourceType': 0}}}, {})
        await conn.relayMessage(m.chat, msg['message'], {'messageId': msg['key']['id']})
        await conn.relayMessage(m.chat, {'videoMessage': {**video_media['videoMessage'], 'caption': '', 'contextInfo': {'pairedMediaType': 6, 'statusSourceType': 0}},
                                         'messageContextInfo': {'messageAssociation': {'associationType': 12, 'parentMessageKey': msg['key']}}}, {})
        await m.react('✅')
    except Exception as e:
        print(e, file=sys.stderr)
        m.error = e
        await m.react('❌')
        await conn.reply(m.chat, f'> *(;ŏ﹏ŏ) Ops! Something went wrong.*\n\n```{e}```', m)


async def handler(m, *, conn, text, usedPrefix, command):
    text, p, command = text, usedPrefix, command
    args = (text or '').strip().split()
    if len(args) >= 2 and re.search(r'https?://\S+', args[0], re.I):
        await m.react('⏳')
        try:
            img = (await http.get(args[0], timeout=30, raise_for_status=False)).content
            video = (await http.get(args[1], timeout=30, raise_for_status=False)).content
        except Exception as e:
            return await m.reply(f'> Failed to download from URL.\n```{e}```')
        return await send_motion(conn, m, img, video)
    img = video = None
    if is_image(m):
        img = await m.download()
    elif m.quoted and is_image(m.quoted):
        img = await m.quoted.download()
    if is_video(m):
        video = await m.download()
    elif m.quoted and is_video(m.quoted):
        video = await m.quoted.download()
    if img and video:
        await m.react('⏳')
        return await send_motion(conn, m, img, video)
    if not img and not video:
        return await m.reply(f'> *Usage:*\n1. Send/reply *image* with caption `{p + command}`\n2. Then send *video* with caption `2`\n3. Or reply *image + video* together\n4. Or: `{p + command} <imgUrl> <videoUrl>`')
    store = _state(conn)
    await m.react('⏳')
    if img:
        store[m.sender] = {'img': img, 'chat': m.chat, 'timeout': asyncio.get_running_loop().call_later(120, _expire, conn, m.sender)}
        return await conn.reply(m.chat, '✅ Image received!\n\nNow send a *video* with caption `2` to create Motion Image.', m)
    store[m.sender] = {'video': video, 'chat': m.chat, 'timeout': asyncio.get_running_loop().call_later(120, _expire, conn, m.sender)}
    return await conn.reply(m.chat, '✅ Video received!\n\nNow send an *image* with caption `2` to create Motion Image.', m)


async def before(m, *, conn):
    store = _state(conn)
    if m.sender not in store:
        return
    state = store[m.sender]
    img, video = state.get('img') or None, state.get('video') or None
    inp = (m.text or '').strip()
    if not img and is_image(m):
        img = await m.download()
    if not video and is_video(m):
        video = await m.download()
    if not img and m.quoted and is_image(m.quoted):
        img = await m.quoted.download()
    if not video and m.quoted and is_video(m.quoted):
        video = await m.quoted.download()
    if img and not video:
        state['img'] = img
        if inp == '2':
            await conn.reply(m.chat, '> Please also send a *video* with caption `2`.', m)
        return
    if not img and video:
        state['video'] = video
        if inp == '2':
            await conn.reply(m.chat, '> Please also send an *image* with caption `2`.', m)
        return
    if not img and not video:
        return
    try:
        state['timeout'].cancel()
    except Exception:
        pass
    store.pop(m.sender, None)
    await m.react('⏳')
    await send_motion(conn, m, img, video)


help = ['imgmotion']
tags = ['tools']
command = re.compile(r'^imgmotion$', re.I)
limit = True
