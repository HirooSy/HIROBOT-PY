import sys

from lib.package.ai import mcp


DOWNLOAD_PLATFORM_KEYS = ['tiktok', 'instagram', 'youtube', 'youtube_audio', 'twitter', 'facebook']


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _msg(err):
    return str(err)


def _warn(*parts):
    print(*parts, file=sys.stderr)


async def _exec_download_media(args=None, **kw):
    a = _args(args, kw)
    platform = a.get('platform')
    query = a.get('query')
    target = mcp.DOWNLOAD_PLATFORM_MAP.get(platform)
    if not target:
        return f'Platform "{platform}" is not recognized. Valid options: {", ".join(DOWNLOAD_PLATFORM_KEYS)}.'

    if platform == 'twitter':
        try:
            return await mcp.download_twitter_direct(query)
        except Exception as e:
            print('[download_media] Gagal download twitter:', e, file=sys.stderr)
            return f'Failed to download Twitter/X: {_msg(e)}'

    try:
        await mcp.exec_plugin_command(target['command'], query)
        return f"{target['label']} was processed through the .{target['command']} plugin, the result was sent straight to this chat."
    except Exception as e:
        return f"Failed to download {target['label']}: {_msg(e)}"


async def _exec_generate_image(args=None, **kw):
    a = _args(args, kw)
    prompt = a.get('prompt')
    aspect_ratio = a.get('aspect_ratio')
    c = mcp.ctx()
    if c['conn'] is None or not c['currentJid']:
        return 'WA connection not ready'
    try:
        from lib.scrapers.src import ai_image as ai_image_mod
        img_urls = await ai_image_mod.generateImage(prompt, {'aspectRatio': aspect_ratio})
        if not img_urls:
            return 'Failed to generate the image: no result from the server.'

        try:
            rich = c['conn'].aiRich()
            rich.addText(prompt)
            rich.addImage(img_urls)
            await rich.send(c['currentJid'], {'quoted': c['currentM']})
        except Exception as rich_err:
            _warn('[generate_image] aiRich failed, fallback sendMessage:', _msg(rich_err))
            await c['conn'].sendMessage(c['currentJid'],
                                        {'image': {'url': img_urls[0]}, 'caption': prompt},
                                        {'quoted': c['currentM']})

        return f'[ALREADY SENT] {len(img_urls)} images for "{prompt}" were generated successfully and have been sent.'
    except Exception as e:
        _warn('[generate_image] Failed to generate:', _msg(e))
        return f'Failed to generate the image: {_msg(e)}'


async def _exec_ai_edit_image(args=None, **kw):
    a = _args(args, kw)
    instruction = a.get('instruction')
    c = mcp.ctx()
    if c['conn'] is None or not c['currentJid']:
        return 'WA connection not ready'
    if c['currentM'] is None:
        return 'There is no message context to fetch the source image from.'
    try:
        image_url = await mcp.download_user_image_as_url(c['currentM'])
        if not image_url:
            return 'No image was detected — make sure the user attached an image directly or replied to a message containing an image/sticker.'

        from lib.scrapers.src import nano as nano_mod
        result_urls = await nano_mod.nanoEditImage(image_url, instruction)
        if not result_urls:
            return 'The edit finished but no result URL could be found in the response.'

        try:
            await c['conn'].sendFile(c['currentJid'], result_urls[0], 'nano.png', instruction, c['currentM'])
        except Exception as send_err:
            _warn('[ai_edit_image] sendFile gagal, fallback aiRich:', _msg(send_err))
            try:
                rich = c['conn'].aiRich()
                rich.addText(instruction)
                rich.addImage(result_urls)
                await rich.send(c['currentJid'], {'quoted': c['currentM']})
            except Exception as rich_err:
                _warn('[ai_edit_image] aiRich juga gagal, fallback sendMessage:', _msg(rich_err))
                await c['conn'].sendMessage(c['currentJid'], {'image': {'url': result_urls[0]}, 'caption': instruction}, {'quoted': c['currentM']})

        return f'The image was edited successfully according to the instruction "{instruction}" and has been sent to this chat.'
    except Exception as e:
        _warn('[ai_edit_image] Gagal edit:', _msg(e))
        return f'Failed to edit the image: {_msg(e)}'


default = [
    {
        'name': 'download_media',
        'description': (
            'Download media (video/photo/audio) from supported social platforms and send it straight to the user. Choose '
            '"platform" according to the source: "tiktok" for tiktok.com/vt.tiktok.com URLs, "instagram" for instagram.com '
            'URLs (Reels/Post), "youtube" for youtube.com/youtu.be URLs when the user wants a VIDEO file, "youtube_audio" '
            'when the user asks to play a song/search for a song/download an MP3 from YouTube (a song title is enough, a URL '
            'is not required), "twitter" for twitter.com/x.com URLs, "facebook" for facebook.com URLs'
        ),
        'parameters': {
            'platform': {
                'type': 'string',
                'description': 'Media source platform: "tiktok", "facebook", "instagram", "youtube", "youtube_audio", or "twitter".',
                'enum': DOWNLOAD_PLATFORM_KEYS,
                'required': True,
            },
            'query': {
                'type': 'string',
                'description': 'URL of the media to download. For the "youtube_audio" platform a song title may be given when there is no URL.',
                'required': True,
            },
        },
        'execute': _exec_download_media,
    },
    {
        'name': 'generate_image',
        'description': (
            'Generate images from a text description (text-to-image) using Bing Image Creator, then send them straight to '
            'the user. Each request produces 4 images that are sent together in one rich message. Use it when the user asks '
            'you to create/draw something, e.g. "draw an astronaut cat", "make a picture of a mountain landscape", "generate '
            'image of...". The process usually takes around 10-30 seconds, so you MUST tell the user first that this takes a '
            'few seconds before calling this tool.'
        ),
        'parameters': {
            'prompt': {
                'type': 'string',
                'description': 'The image description/prompt to generate, in English for the best result (translate it first if the user asked in another language)',
                'required': True,
            },
            'aspect_ratio': {
                'type': 'string',
                'description': (
                    'Image aspect ratio: "1:1" (square), "3:2" (landscape), or "2:3" (portrait). Infer it from the request '
                    'if there is a clear hint, e.g. "landscape"/"wide scenery"/"wallpaper" -> "3:2", "poster"/"vertical"/'
                    '"phone wallpaper"/"portrait" -> "2:3". If the user did not mention anything about orientation/ratio, do '
                    'NOT guess, use the default "1:1".'
                ),
                'enum': ['1:1', '3:2', '2:3'],
                'required': False,
            },
        },
        'execute': _exec_generate_image,
    },
    {
        'name': 'ai_edit_image',
        'description': (
            'Edit an image the user sent/replied to using AI (image-to-image) based on a text instruction — for example "add '
            'glasses", "change it to anime style", "change the background to a beach", etc. An image MUST be attached to this '
            'message OR this message must reply to a message containing an image/sticker. The process can take a while, so '
            'tell the user first that this takes a bit before calling this tool.'
        ),
        'parameters': {
            'instruction': {
                'type': 'string',
                'description': 'The edit instruction in English for the best result (translate it first if the user asked in another language), as detailed as possible about what should change',
                'required': True,
            },
        },
        'execute': _exec_ai_edit_image,
    },
]
