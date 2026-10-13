import base64
import os
import re
import sys

from lib.package.ai import mcp


def _warn(*parts):
    print(*parts, file=sys.stderr)


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _g(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def _msg(err, fallback=''):
    text = str(err) if err is not None else ''
    return text or fallback


def _with_scheme(url):
    target = str(url).strip()
    if not target.startswith('http://') and not target.startswith('https://'):
        target = f'https://{target}'
    return target


def _parts_text(res):
    if not isinstance(res, dict):
        return ''
    try:
        parts = res['candidates'][0]['content']['parts']
    except (KeyError, IndexError, TypeError):
        return ''
    if not isinstance(parts, list):
        return ''
    return '\n'.join(p['text'] for p in parts if isinstance(p, dict) and p.get('text')).strip()


def _is_url(value):
    return bool(re.match(r'^https?://', str(value)))


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_file(path):
    with open(path, 'rb') as f:
        return f.read()


async def _exec_view_website(args=None, **kw):
    a = _args(args, kw)
    url = a.get('url')
    focus = a.get('focus')
    target_url = _with_scheme(url)

    img_buffer = None
    screenshot_err = None
    try:
        img_buffer = await mcp.capture_website_screenshot(target_url)
    except Exception as err:
        screenshot_err = err
        _warn(f'[view_website] Screenshot gagal untuk "{target_url}", fallback ke HTML mentah:', _msg(err))

    api_key = mcp.get_next_key(None)
    if not api_key:
        return 'No Gemini API key available for analysis.'
    ai = mcp.create_gemini_client(api_key=api_key)

    used_fallback = False
    personality = mcp.get_personality()

    if img_buffer is not None:
        base64_data = base64.b64encode(img_buffer).decode('ascii')
        if focus:
            prompt = (
                f'{personality}\n\nYou MUST use the speaking style above for this reply — do not answer in a report format/formal headings (no markdown ### headings, no excessive structured bullet points), just natural flowing text like chatting.\n\nThis is a full-page screenshot of the website: {target_url}\n\nPlease analyze this image and answer: {focus}\n\nGive information as complete as possible based on what is visible in the screenshot, but still in the natural style above.'
            )
        else:
            prompt = (
                f'{personality}\n\nYou MUST use the speaking style above for this reply — do not answer in a report format/formal headings (no markdown ### headings, no excessive structured bullet points), just natural flowing text like chatting.\n\nThis is a full-page screenshot of the website: {target_url}\n\nPlease describe and summarize the content of this website: title, main content, menu/navigation, visible important information, etc — but deliver it naturally, not in a report format.'
            )
        try:
            vision_res = await ai.models.generate_content(
                model=mcp.MODELS['default'],
                contents=[{
                    'role': 'user',
                    'parts': [
                        {'inlineData': {'mimeType': 'image/jpeg', 'data': base64_data}},
                        {'text': prompt},
                    ],
                }],
            )
        except Exception as err:
            return f'The screenshot was taken, but Gemini failed to analyze it: {_msg(err)}'
    else:
        used_fallback = True
        try:
            html = await mcp.fetch_website_html_fallback(target_url)
        except Exception as html_err:
            return f"Failed to get the content of \"{target_url}\" — the screenshot failed ({_msg(screenshot_err, 'unknown')}) AND the fallback of fetching the raw HTML also failed ({_msg(html_err)}). The site is probably down/blocking automated access."
        if focus:
            prompt = (
                f'{personality}\n\nYou MUST use the speaking style above — no report format/formal headings, just natural flowing text.\n\nThe visual screenshot of the website {target_url} failed to be taken, but here is the raw HTML of the page (script/style tags removed). Please read it and answer: {focus}\n\nHTML:\n{html}'
            )
        else:
            prompt = (
                f'{personality}\n\nYou MUST use the speaking style above — no report format/formal headings, just natural flowing text.\n\nThe visual screenshot of the website {target_url} failed to be taken, but here is the raw HTML of the page (script/style tags removed). Please describe and summarize the content of this website: title, main content, important information in its text/markup — deliver it naturally, not in a report format.\n\nHTML:\n{html}'
            )
        try:
            vision_res = await ai.models.generate_content(
                model=mcp.MODELS['default'],
                contents=[{'role': 'user', 'parts': [{'text': prompt}]}],
            )
        except Exception as err:
            return f'The screenshot failed ({_msg(screenshot_err, "unknown")}), and Gemini also failed to analyze its fallback HTML: {_msg(err)}'

    analysis_text = _parts_text(vision_res) or 'Could not analyze this page.'
    note = '\n\n_(note: the visual screenshot failed to be taken, this analysis is based on the raw HTML of the page, not the visual appearance)_' if used_fallback else ''
    return f'*Analisa website: {target_url}*\n\n{analysis_text}{note}'


async def _exec_fetch_html_raw(args=None, **kw):
    a = _args(args, kw)
    url = a.get('url')
    focus = a.get('focus')
    target_url = _with_scheme(url)
    try:
        html = await mcp.fetch_website_html_fallback(target_url)
    except Exception as err:
        return f'Failed to fetch the HTML from "{target_url}": {_msg(err)}'
    api_key = mcp.get_next_key(None)
    if not api_key:
        return 'No Gemini API key available for analysis.'
    ai = mcp.create_gemini_client(api_key=api_key)
    personality = mcp.get_personality()
    if focus:
        prompt = (
            f'{personality}\n\nYou MUST use the speaking style above — no report format/formal headings (no ### or excessive structured bullets), just natural flowing text like chatting.\n\nThis is the raw HTML of the page {target_url} (script/style tags removed). Please answer: {focus}\n\nHTML:\n{html}'
        )
    else:
        prompt = (
            f'{personality}\n\nYou MUST use the speaking style above — no report format/formal headings (no ### or excessive structured bullets), just natural flowing text like chatting.\n\nThis is the raw HTML of the page {target_url} (script/style tags removed). Please summarize the content of this page: title, main content, important structure/elements — deliver it naturally, not in a report format.\n\nHTML:\n{html}'
        )
    try:
        vision_res = await ai.models.generate_content(
            model=mcp.MODELS['default'],
            contents=[{'role': 'user', 'parts': [{'text': prompt}]}],
        )
    except Exception as err:
        return f'The HTML was fetched, but Gemini failed to analyze it: {_msg(err)}'
    analysis_text = _parts_text(vision_res) or 'Could not analyze this HTML.'
    return f'*Raw HTML from: {target_url}*\n\n{analysis_text}'


async def _fetch_video_item(vid_url, label):
    max_video_bytes = 15 * 1024 * 1024
    try:
        pv = await mcp.peek_fetch_video_buffer(vid_url, max_video_bytes)
        if not pv['tooLarge'] and len(pv['buffer']) > 0:
            ctype = pv['contentType'] if 'mp4' in str(pv['contentType']) else 'video/mp4'
            return {'buffer': pv['buffer'], 'contentType': ctype}
    except Exception as err:
        _warn(f'[view_link_post] Gagal download video {label} utuh, fallback ke thumbnail:', _msg(err))
    return None


async def _exec_view_link_post(args=None, **kw):
    a = _args(args, kw)
    url = a.get('url')
    context = a.get('context') or ''
    platform = mcp.detect_platform(url)
    media_items = []

    try:
        if platform == 'tiktok':
            from lib.scrapers.src import tiktok as tiktok_mod
            data = await tiktok_mod.tiktok(url)
            images = _g(data, 'images')
            if images:
                pf = await mcp.peek_fetch_buffer(images[0])
                media_items.append({'buffer': pf['buffer'], 'contentType': pf['contentType']})
            elif _g(data, 'play'):
                media_items.append({
                    'buffer': b'',
                    'contentType': 'video/mp4',
                    'thumbnailUrl': _g(data, 'cover') or _g(data, 'origin_cover') or None,
                })
        elif platform == 'instagram':
            from lib.scrapers.src import ig as ig_mod
            result = await ig_mod.instagram(url)
            if _g(result, 'status') and _g(result, 'result'):
                res = result['result']
                metadata = _g(res, 'metadata', {})
                media = _g(res, 'media', {})
                mtype = _g(metadata, 'type')
                thumb = _g(media, 'thumbnail')
                if mtype == 'single_image':
                    images = _g(media, 'images', [])
                    img_url = _g(images[0], 'url') if images else None
                    if img_url:
                        pf = await mcp.peek_fetch_buffer(img_url)
                        media_items.append({'buffer': pf['buffer'], 'contentType': pf['contentType']})
                elif mtype in ('video', 'reels'):
                    vids = _g(media, 'videos', [])
                    vid_url = _g(vids[0], 'url') if vids else None
                    sent_video = False
                    if vid_url:
                        item = await _fetch_video_item(vid_url, 'IG')
                        if item is not None:
                            media_items.append(item)
                            sent_video = True
                    if not sent_video and thumb:
                        try:
                            media_items.append({'buffer': _read_file(thumb), 'contentType': 'image/jpeg'})
                        except Exception:
                            pass
                    if thumb:
                        _unlink(thumb)
                elif mtype == 'carousel':
                    items = _g(media, 'items', [])
                    first = items[0] if items else None
                    sent_video = False
                    if _g(first, 'type') == 'video':
                        vids = _g(first, 'videos', [])
                        vid_url = _g(vids[0], 'url') if vids else None
                        if vid_url:
                            item = await _fetch_video_item(vid_url, 'carousel')
                            if item is not None:
                                media_items.append(item)
                                sent_video = True
                    if not sent_video and thumb:
                        try:
                            if _is_url(thumb):
                                pf = await mcp.peek_fetch_buffer(thumb)
                                media_items.append({'buffer': pf['buffer'], 'contentType': pf['contentType']})
                            else:
                                media_items.append({'buffer': _read_file(thumb), 'contentType': 'image/jpeg'})
                        except Exception:
                            pass
                    if thumb and not _is_url(thumb):
                        _unlink(thumb)
                    if len(items) > 1:
                        context = ' — '.join(x for x in [f'(Carousel with {len(items)} slides, this is only the first slide)', context] if x)
                elif thumb:
                    try:
                        if _is_url(thumb):
                            pf = await mcp.peek_fetch_buffer(thumb)
                            media_items.append({'buffer': pf['buffer'], 'contentType': pf['contentType']})
                        else:
                            media_items.append({'buffer': _read_file(thumb), 'contentType': 'image/jpeg'})
                            _unlink(thumb)
                    except Exception:
                        pass
        elif platform == 'youtube':
            m = re.search(r'(?:youtu\.be/|youtube\.com/(?:embed/|live/|shorts/)|[?&]v=)([a-zA-Z0-9\-_]{11})', url)
            video_id = m.group(1) if m else None
            if video_id:
                thumb_url = f'https://img.youtube.com/vi/{video_id}/hqdefault.jpg'
                pf = await mcp.peek_fetch_buffer(thumb_url)
                media_items.append({'buffer': pf['buffer'], 'contentType': pf['contentType']})
        elif platform == 'twitter':
            from lib.scrapers.src import x as x_mod
            data = await x_mod.twitter(url)
            thumb = _g(data, 'thumbnail')
            if thumb:
                try:
                    pf = await mcp.peek_fetch_buffer(thumb)
                    media_items.append({'buffer': pf['buffer'], 'contentType': pf['contentType']})
                except Exception:
                    pass
            description = _g(data, 'description')
            if description:
                context = ' — '.join(x for x in [description, context] if x)
        else:
            return 'Unknown platform for peek. Try using view_website to see the content of this URL.'
    except Exception as err:
        _warn(f'[view_link_post] Gagal ambil media dari {platform}:', _msg(err))
        msg = _msg(err)
        is_module_err = 'does not provide' in msg or 'Cannot find module' in msg or 'No module named' in msg or 'cannot import name' in msg
        if is_module_err:
            return f'[view_link_post INTERNAL ERROR — scraper module not found: {msg}. This is a code bug, not an exhausted quota. Do not tell the user the quota ran out — say the peek feature is having a technical problem, and offer a regular download as an alternative.]'
        return f'[view_link_post FAILED — {msg}. Offer the user an alternative such as a regular download, do not say "quota exhausted".]'

    return await mcp.peek_analyze_with_vision(media_items, platform, url, context)


async def _exec_search_web(args=None, **kw):
    a = _args(args, kw)
    query = a.get('query')
    try:
        result = await mcp.search_web_grounded(query)
        if not result or not result.get('answer'):
            return 'Search returned no answer for this query. Answer from your own knowledge and note that the info may not be up to date.'
        sources = '\n'.join(f"• {_g(s, 'title', '')}: {_g(s, 'url', '')}" for s in (result.get('sources') or []))
        reminder = '\n\n[MANDATORY INSTRUCTION: do NOT answer the user directly with plain text. Call the send_rich_reply tool now — body = the summary above in natural language WITHOUT any links, citations = the list of {url, title} from the relevant sources above (they will appear as link buttons under the message).]'
        return result['answer'] + (f'\n\nSources list (to be attached via send_rich_reply, do NOT paste it raw):\n{sources}' if sources else '') + reminder
    except Exception as e:
        _warn(f'[search_web] Error: {_msg(e)}')
        return f'Search failed: {_msg(e)}. Answer from your own knowledge and note that the info may not be up to date.'


default = [
    {
        'name': 'view_website',
        'description': (
            'Take a full-page desktop screenshot of a GENERAL website (not TikTok/Instagram/YouTube/Twitter-X) and '
            'analyze its content visually using Gemini Vision. Use this tool when the user asks to check the content '
            'of a website/link post OUTSIDE those four social platforms (e.g. e621, artstation, a blog, an online '
            'shop, github, etc) — to see how a page looks or to let the AI know what is at a URL. The screenshot is '
            'taken from screenshotmachine.com (full-page, desktop mode). Result: the AI will describe/analyze the '
            'visual content of that page. Do NOT use this tool for TikTok/Instagram/YouTube/Twitter URLs — use '
            'view_link_post for those (its visuals are more accurate because it fetches the original media from the '
            'platform scraper, not a generic browser screenshot).'
        ),
        'parameters': {
            'url': {
                'type': 'string',
                'description': 'The website URL to screenshot and analyze. Must start with http:// or https://',
                'required': True,
            },
            'focus': {
                'type': 'string',
                'description': 'What do you want to know about this website? (optional, e.g. "check the product price", "see the main content", "read the text on it")',
                'required': False,
            },
        },
        'execute': _exec_view_website,
    },
    {
        'name': 'fetch_html_raw',
        'description': (
            'Fetch the raw HTML from a URL directly (not a screenshot/visual) and summarize its content through Gemini '
            'as text. Use this tool SPECIFICALLY when the user explicitly asks for "html", "the page source code", '
            '"check its raw content", or wants to know the text/markup content of a page without needing its visual '
            'appearance. Different from view_website, which focuses on the visual appearance — this tool purely reads '
            'text/HTML.'
        ),
        'parameters': {
            'url': {
                'type': 'string',
                'description': 'The URL whose HTML should be fetched. Must start with http:// or https://',
                'required': True,
            },
            'focus': {
                'type': 'string',
                'description': 'What do you want to know from this HTML? (optional)',
                'required': False,
            },
        },
        'execute': _exec_fetch_html_raw,
    },
    {
        'name': 'view_link_post',
        'description': (
            "See the VISUAL content of a SPECIFIC TikTok / Instagram / YouTube / Twitter-X link — fetch the original "
            "media (photo/thumbnail/cover) directly from each platform's scraper (NOT a browser screenshot), then the AI "
            'reacts/comments on its content. YOU MUST USE THIS TOOL (not view_website) for these four platforms, because '
            'its visuals are far more accurate (original media, not a screenshot of the page). Use it when the user '
            'shares a link from one of those 4 platforms and does NOT ask to download, but wants the AI to know about/comment '
            'on that post. Example triggers: "check this", "take a look", "what do you think", "react to this", or the '
            'user sends a link without a download instruction.'
        ),
        'parameters': {
            'url': {
                'type': 'string',
                'description': 'URL post (TikTok, Instagram, YouTube, Twitter/X)',
                'required': True,
            },
            'context': {
                'type': 'string',
                'description': "The user's specific context or question about this content (optional)",
                'required': False,
            },
        },
        'execute': _exec_view_link_post,
    },
    {
        'name': 'search_web',
        'description': (
            'Search for the latest information on the internet (Gemini native grounding via Google Search — model '
            'gemini-3.1-flash-lite, falling back to gemini-2.5-flash if it fails/hits a limit). Use it for news, prices, '
            'real-time data, or things that may have changed since training. IMPORTANT: after getting a result from this '
            'tool, your final reply to the user MUST go through the send_rich_reply tool (see rule 13) — NEVER answer '
            'directly with plain text that pastes the raw links from the "Sources:" section of this tool result.'
        ),
        'parameters': {
            'query': {
                'type': 'string',
                'description': 'The keywords or question to search for',
                'required': True,
            },
        },
        'execute': _exec_search_web,
    },
]
