import math

from baileys import getDevice
from lib.compat import http
from lib.utils.canvas import Canvas, jimp, rounded_poly
from lib.utils.handler import G


SIZE = 714
CARD_H = math.floor(SIZE * 7 / 12 + 0.5)
CARD_Y = (SIZE - CARD_H) // 2
PANEL_X = 280
PAD = 28
CONTENT_X = PANEL_X + PAD
CONTENT_W = SIZE - PANEL_X - 2 * PAD
BG = '#0d0e14'
PANEL_STOPS = [(0.0, '#171923'), (0.7, '#0d0e14'), (1.0, '#0d0e14')]
LETTER_STOPS = ['#3a3f5c', '#1b1d2b']
LINE_STOPS = ['rgba(255,255,255,0)', 'rgba(255,255,255,.22)', 'rgba(255,255,255,0)']
DEV_COLOR = '#3ddc84'
ICON = 30
ICON_SCALE = ICON / 24
BADGE = 58
BADGE_GAP = 14
BADGE_RADIUS = 16
BADGE_BG = 'rgba(255,255,255,0.06)'
BADGE_BORDER = 'rgba(255,255,255,0.12)'


def js_str(v):
    if v is None:
        return 'undefined'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def to_simple(n):
    n = n or 0
    for lim, suffix in ((1e15, 'Q'), (1e12, 'T'), (1e9, 'B'), (1e6, 'M'), (1000, 'k')):
        if n >= lim:
            s = f'{n / lim:.1f}'
            return (s[:-2] if s.endswith('.0') else s) + suffix
    return js_str(n)


def at(seq, idx):
    if idx is None or not isinstance(seq, (list, tuple)):
        return None
    return seq[idx] if 0 <= idx < len(seq) else None


def text_width(cv, text, size, spacing=0.0):
    return cv.measure(text, size)['width'] + spacing * len(text)


def draw_text(cv, text, x, y, size, color, spacing=0.0, bold=False):
    offset = size * 0.03 if bold else 0.0
    if not spacing:
        cv.text(text, x, y, size, color)
        if bold:
            cv.text(text, x + offset, y, size, color)
        return
    for i, ch in enumerate(text):
        cx = x + (cv.measure(text[:i], size)['width'] if i else 0.0) + spacing * i
        cv.text(ch, cx, y, size, color)
        if bold:
            cv.text(ch, cx + offset, y, size, color)


def clip_text(cv, text, size, spacing, max_w):
    if text_width(cv, text, size, spacing) <= max_w:
        return text
    out = text
    while out and text_width(cv, out + '…', size, spacing) > max_w:
        out = out[:-1]
    return out + '…'


def draw_badge(cv, x, y, kind):
    cv.fill_polygons([rounded_poly(x, y, BADGE, BADGE, BADGE_RADIUS)], BADGE_BG)
    cv.fill_polygons([rounded_poly(x, y, BADGE, BADGE, BADGE_RADIUS), rounded_poly(x + 1, y + 1, BADGE - 2, BADGE - 2, BADGE_RADIUS - 1)],
                     BADGE_BORDER, 'evenodd')
    ox, oy = x + (BADGE - ICON) / 2, y + (BADGE - ICON) / 2

    def p(px, py):
        return (ox + px * ICON_SCALE, oy + py * ICON_SCALE)
    if kind == 'dev':
        width = 2.4 * ICON_SCALE
        cv.stroke([p(8, 6), p(2, 12), p(8, 18)], width, DEV_COLOR, False, 'round', 'round')
        cv.stroke([p(16, 6), p(22, 12), p(16, 18)], width, DEV_COLOR, False, 'round', 'round')
        cv.stroke([p(14, 4), p(10, 20)], width, DEV_COLOR, False, 'round', 'round')
    elif kind == 'premium':
        star = [p(12, 1), p(14.6, 9.4), p(23, 12), p(14.6, 14.6), p(12, 23), p(9.4, 14.6), p(1, 12), p(9.4, 9.4)]
        cv.fill_polygons([star], '#ffffff')


async def get_avatar(url):
    if not isinstance(url, str) or not url:
        return None
    try:
        res = await http.get(url, timeout=8, raise_for_status=False)
    except Exception:
        return None
    return res.content if res.ok else None


async def render_card(name, avatar, badges):
    cv = Canvas(SIZE, SIZE, BG)
    cv.rect(0, CARD_Y, PANEL_X, CARD_H, '#1b1d2b')
    if avatar:
        try:
            photo = await jimp.read(avatar)
        except Exception:
            photo = None
        if photo is not None:
            photo.resize({'w': PANEL_X, 'h': CARD_H, 'fit': 'cover'})
            cv.paste(photo, 0, CARD_Y)
    else:
        cv.gradient(0, CARD_Y, PANEL_X, CARD_H, LETTER_STOPS, 135)
        letter = (name or '?').strip()[:1].upper() or '?'
        lm = cv.measure(letter, 120)
        draw_text(cv, letter, (PANEL_X - lm['width']) / 2, CARD_Y + (CARD_H - lm['height']) / 2, 120, '#ffffff', bold=True)

    cv.gradient(PANEL_X, CARD_Y, SIZE - PANEL_X, CARD_H, PANEL_STOPS, 160)

    size = 24 if len(name) > 14 else 30 if len(name) > 10 else 36
    name_text = clip_text(cv, name.upper(), size, 2, CONTENT_W)
    name_lh = cv.measure('A', size)['height']
    label_lh = cv.measure('A', 13)['height']
    none_lh = cv.measure('A', 22)['height']
    row_h = BADGE if badges else 10 + none_lh
    content_h = name_lh + 22 + 1 + 24 + label_lh + 20 + row_h
    top = CARD_Y + PAD + (CARD_H - 2 * PAD - content_h) / 2

    name_x = CONTENT_X + (CONTENT_W - text_width(cv, name_text, size, 2)) / 2
    draw_text(cv, name_text, name_x, top, size, '#ffffff', spacing=2, bold=True)

    line_y = top + name_lh + 22
    cv.gradient(CONTENT_X, line_y, CONTENT_W, 1, LINE_STOPS, 90)

    label_y = line_y + 1 + 24
    label_w = text_width(cv, 'BADGE', 13, 7) + 7
    label_x = CONTENT_X + (CONTENT_W - label_w) / 2
    draw_text(cv, 'BADGE', label_x + 7, label_y, 13, '#8b90a3', spacing=7)

    row_y = label_y + label_lh + 20
    if badges:
        total = len(badges) * BADGE + (len(badges) - 1) * BADGE_GAP
        bx = CONTENT_X + (CONTENT_W - total) / 2
        for kind in badges:
            draw_badge(cv, bx, row_y, kind)
            bx += BADGE + BADGE_GAP
    else:
        dash_w = cv.measure('-', 22)['width']
        draw_text(cv, '-', CONTENT_X + (CONTENT_W - dash_w) / 2, row_y + 10, 22, '#555a6e')
    return cv.to_bytes('png')


async def handler(m, *, conn, isOwner, isPrems):
    is_owner, is_prems = isOwner, isPrems
    user = G.db.data['users'].get(m.sender)
    if not user:
        return await m.reply('Profil tidak ditemukan.')
    tier = G.settings['tier']
    level = user.get('level')
    exp = user.get('exp') or 0
    required = at(tier['exp_required'], level + 1 if isinstance(level, (int, float)) else None) or exp
    if isinstance(level, (int, float)) and level >= len(tier['name']) + 1:
        tier_up = 'MAX !'
    elif exp > required:
        tier_up = 'UPGRADE !'
    else:
        tier_up = f'{to_simple(exp)} / {to_simple(required)}'
    limit = ('♾ ' + js_str(user.get('limit'))) if (is_owner or is_prems) else js_str(user.get('limit'))
    badge = user.get('badge') or {}
    premium_badge = badge.get('premium') or {}
    badges = []
    if is_owner or badge.get('developer'):
        badges.append('dev')
    if is_prems or premium_badge.get('active'):
        badges.append('premium')
    name = user.get('name') or m.pushName or 'User'
    avatar = await get_avatar(G.img['profile']['sender'] if G.img else None)
    image = await render_card(name, avatar, badges)
    status = 'Owner' if is_owner else 'Premium User' if is_prems else 'User'
    url = G.getServerUrl()
    caption = (f"- `Name :` {name}\n- `Status :` {status}\n- `Registered :` {js_str(user.get('registered'))}\n"
               f"- `Device :` {getDevice(m.id)}\n- `Tier :` {js_str(at(tier['name'], level))}\n- `Exp :` {tier_up}\n"
               f"- `Gem :` {to_simple(user.get('gems') or 0)}\n- `Limit :` {limit}")
    await conn.sendButton(m.chat, {
        'location': {'buffer': image, 'name': '', 'address': '', 'url': url},
        'text': caption,
        'footer': '',
        'nativeFlow': [{'text': 'More Info', 'url': url, 'useWebview': False}],
    }, m)


help = command = ['profile']
tags = ['main']
ai = {'risk': 'low', 'summarize': False, 'description': 'Check profile user'}
