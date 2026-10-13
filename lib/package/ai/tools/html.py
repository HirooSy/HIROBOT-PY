import os
import re
import sys
import time

from lib.package.ai import mcp


ROOT = os.getcwd()
MAX_HTML_CHARS = 200000
OFFER_COOLDOWN_MS = 20 * 60 * 1000
STORE_TTL_MS = 6 * 60 * 60 * 1000
STORE_MAX_ENTRIES = 50

last_html_by_chat = {}
last_offer_by_chat = {}


def _now_ms():
    return int(time.time() * 1000)


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _msg(err):
    return str(err)


def _clean_html(raw):
    s = str(raw or '').strip()
    fence = re.fullmatch(r'```(?:html)?\s*\n([\s\S]*?)\n?```', s, re.I)
    if fence:
        s = fence.group(1).strip()
    return s


def _ensure_document(html):
    if re.search(r'<html[\s>]', html, re.I):
        return html
    return (
        '<!DOCTYPE html>\n'
        '<html>\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<style>\n'
        '  :root { color-scheme: light dark; }\n'
        '  * { box-sizing: border-box; }\n'
        '  body { margin: 0; padding: 12px; font-family: system-ui, sans-serif; background: transparent; }\n'
        '</style>\n'
        '</head>\n'
        '<body>\n'
        f'{html}\n'
        '</body>\n'
        '</html>'
    )


def _remember(jid, html, title):
    now = _now_ms()
    for key in list(last_html_by_chat.keys()):
        if now - last_html_by_chat[key]['at'] > STORE_TTL_MS:
            del last_html_by_chat[key]
    last_html_by_chat.pop(jid, None)
    last_html_by_chat[jid] = {'html': html, 'title': title or '', 'at': now}
    while len(last_html_by_chat) > STORE_MAX_ENTRIES:
        del last_html_by_chat[next(iter(last_html_by_chat))]


def _should_offer_file(jid):
    last = last_offer_by_chat.get(jid, 0)
    if _now_ms() - last < OFFER_COOLDOWN_MS:
        return False
    last_offer_by_chat[jid] = _now_ms()
    return True


def _safe_file_name(name, title):
    raw = str(name or title or 'design').strip()
    raw = re.sub(r'\.html?$', '', raw, flags=re.I)
    base = re.sub(r'[^a-zA-Z0-9._-]+', '-', raw)
    base = re.sub(r'^-+|-+$', '', base)[:60]
    return f"{base or 'design'}.html"


async def _render_in_chat(html, title):
    c = mcp.ctx()
    if c['conn'] is None or not c['currentJid']:
        return {'error': 'WA connection not ready'}
    body = _clean_html(html)
    if not body:
        return {'error': 'html is empty'}
    if len(body) > MAX_HTML_CHARS:
        return {'error': f'HTML is too large ({len(body)} chars, limit {MAX_HTML_CHARS}). Make it more compact (less repeated markup, no embedded images) and call the tool again.'}
    doc = _ensure_document(body)
    try:
        rich = c['conn'].aiRich()
        if title:
            rich.setTitle(str(title)[:80])
        rich.addHtml(doc)
        await rich.send(c['currentJid'], {'quoted': c['currentM']})
    except Exception as e:
        print('[html] aiRich send failed:', e, file=sys.stderr)
        return {'error': f'Failed to render the HTML in chat: {_msg(e)}'}
    _remember(c['currentJid'], doc, title)
    return {'doc': doc, 'jid': c['currentJid']}


def _delivered_message(kind, jid):
    offer = 'yes' if _should_offer_file(jid) else 'no'
    return f'[ALREADY SENT] {kind} rendered in chat. offer_file={offer}'


async def _exec_html_design(args=None, **kw):
    a = _args(args, kw)
    res = await _render_in_chat(a.get('html'), a.get('title'))
    if res.get('error'):
        return f"Failed: {res['error']}"
    return _delivered_message('HTML design', res['jid'])


async def _exec_html_preview(args=None, **kw):
    a = _args(args, kw)
    html = a.get('html')
    file_path = a.get('file_path')
    title = a.get('title')
    source = html
    label = title
    if not str(source or '').strip() and file_path:
        c = mcp.ctx()
        if not c['isOwner']:
            return 'file_path can only be used by the bot owner. Ask the user to paste the HTML code instead.'
        abs_path = os.path.abspath(os.path.join(ROOT, file_path))
        if not os.path.exists(abs_path):
            return f'File not found: {file_path}'
        if os.path.isdir(abs_path):
            return f'{file_path} is a directory, not a file.'
        with open(abs_path, 'r', encoding='utf-8') as f:
            source = f.read()
        label = label or os.path.basename(abs_path)
    if not str(source or '').strip():
        return 'Nothing to preview: pass the HTML in "html" or an existing file in "file_path".'
    res = await _render_in_chat(source, label)
    if res.get('error'):
        return f"Failed: {res['error']}"
    return _delivered_message('HTML preview', res['jid'])


async def _exec_send_html_file(args=None, **kw):
    a = _args(args, kw)
    file_name = a.get('file_name')
    c = mcp.ctx()
    if c['conn'] is None or not c['currentJid']:
        return 'WA connection not ready'
    entry = last_html_by_chat.get(c['currentJid'])
    if not entry or _now_ms() - entry['at'] > STORE_TTL_MS:
        return 'There is no recent HTML for this chat. Create one first with html_design or html_preview.'
    fname = _safe_file_name(file_name, entry['title'])
    try:
        await c['conn'].sendMessage(c['currentJid'], {
            'document': entry['html'].encode('utf-8'),
            'mimetype': 'text/html',
            'fileName': fname,
        }, {'quoted': c['currentM']})
    except Exception as e:
        print('[send_html_file] Error:', e, file=sys.stderr)
        return f'Failed to send the file: {_msg(e)}'
    last_offer_by_chat[c['currentJid']] = _now_ms()
    return f'[ALREADY SENT] HTML file {fname} sent.'


default = [
    {
        'name': 'html_design',
        'description': (
            'Design a visual page, UI or graphic and render it live inside the WhatsApp chat (AI Rich HTML). Use it when '
            'the user asks you to design or build something visual: landing page, profile or business card, poster, '
            'invitation, certificate, menu, dashboard, infographic, resume, pricing table, form mockup, small interactive '
            'widget or mini game, etc. YOU write the complete HTML and pass it in "html" — the tool only renders it, so do '
            'not paste the code into your reply. The tool result tells you whether to ask the user about getting the .html file.\n\n'
            'DESIGN RULES (follow all of them):\n'
            '1. Deliver ONE self-contained document. Put all CSS in a <style> block and any JavaScript inline in a <script> block. '
            'No external CSS, JS, fonts, images or CDN links (they are blocked or unreliable in the chat renderer). Use inline SVG, '
            'CSS gradients, emoji and data: URIs for visuals.\n'
            '2. Mobile-first. Include <meta name="viewport" content="width=device-width, initial-scale=1">. Design for a 360-420px '
            'wide screen, use fluid widths, max-width: 100%, flexbox or grid, and never cause horizontal scrolling.\n'
            '3. Support light and dark mode: add :root { color-scheme: light dark; }, define colors as CSS variables, override them '
            'in @media (prefers-color-scheme: dark), and keep the body background transparent or a deliberate surface color.\n'
            '4. Visual quality: clear hierarchy, one accent color plus neutrals, a consistent spacing scale, 16px base text with '
            '1.5 line height, rounded corners, enough contrast, short copy instead of walls of text, and a system font stack.\n'
            '5. Keep it compact: aim for under ~20KB unless the user asks for something complex. Hard limit is 200KB.\n'
            '6. Interactivity only when it helps and only with vanilla JS. No network requests and no forms that submit anywhere.\n'
            '7. Write the visible text of the design in the language the user is using, unless they ask otherwise.\n'
            '8. If the user asked for changes to a previous design, apply them to the previous HTML and render the full updated page again.'
        ),
        'parameters': {
            'html': {
                'type': 'string',
                'description': 'The complete HTML document (or just the body markup plus <style>) for the design. Raw HTML only, no markdown fences.',
                'required': True,
            },
            'title': {
                'type': 'string',
                'description': 'Short title shown above the rendered design (optional, e.g. "Coffee Shop Landing Page").',
                'required': False,
            },
        },
        'execute': _exec_html_design,
    },
    {
        'name': 'html_preview',
        'description': (
            'Render EXISTING HTML inside the WhatsApp chat so the user can see how it looks (AI Rich HTML). Use it for requests '
            'like "preview this html", "show me how this renders", "render this code", "open this html file". Pass the HTML exactly '
            'as the user gave it: do not redesign, restyle or "fix" it unless the user asks for that (if they ask for a redesign, use '
            'html_design instead). Provide either "html" (code pasted by the user or taken from a replied message) or "file_path" (an '
            'HTML file on the server, bot owner only). Do not paste the HTML back into your reply. The tool result tells you whether '
            'to ask the user about getting the .html file.'
        ),
        'parameters': {
            'html': {
                'type': 'string',
                'description': 'The HTML to preview, exactly as provided by the user. Raw HTML only, no markdown fences. Leave empty when using file_path.',
                'required': False,
            },
            'file_path': {
                'type': 'string',
                'description': 'Path of an HTML file on the server, relative to the bot root or absolute. Bot owner only. Leave empty when using html.',
                'required': False,
            },
            'title': {
                'type': 'string',
                'description': 'Short title shown above the preview (optional, defaults to the file name when file_path is used).',
                'required': False,
            },
        },
        'execute': _exec_html_preview,
    },
    {
        'name': 'send_html_file',
        'description': (
            'Send the HTML that was most recently rendered in this chat (by html_design or html_preview) as a downloadable .html '
            'document attachment. Call it when the user says yes to your offer of the html file, or explicitly asks for the HTML '
            'file/source of the design or preview you just showed. The HTML is read from memory by the tool, so never retype it. '
            'Do not call it on your own initiative without the user asking or agreeing.'
        ),
        'parameters': {
            'file_name': {
                'type': 'string',
                'description': 'File name for the attachment (optional, e.g. "landing-page.html"). Defaults to a name based on the title.',
                'required': False,
            },
        },
        'execute': _exec_send_html_file,
    },
]
