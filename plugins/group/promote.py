import re


def clean(number):
    return re.sub(r'[@+-]', '', re.sub(r'\s', '', number))


def js_is_nan(s):
    t = str(s).strip()
    if t == '':
        return False
    if re.fullmatch(r'[+-]?Infinity', t):
        return False
    if re.fullmatch(r'[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?', t):
        return False
    if re.fullmatch(r'0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+', t):
        return False
    return True


async def handler(m, *, conn, usedPrefix, text):
    p = usedPrefix
    text = clean(text or '')
    usage = f'*❏ PROMOTE NUMBER*\n\n• ```Tag user:``` *{p}promote @Tag*\n• ```Type Number:``` *{p}promote 6289654360447*'
    if not text and not m.quoted:
        return await conn.reply(m.chat, usage, m)
    number = None if js_is_nan(text) else text
    if number is None or js_is_nan(number) or len(number) > 15:
        return await conn.reply(m.chat, usage, m)
    if text:
        user = number + '@s.whatsapp.net'
    elif m.quoted and m.quoted.sender:
        user = m.quoted.sender
    elif m.mentionedJid:
        user = number + '@s.whatsapp.net'
    else:
        user = None
    await conn.groupParticipantsUpdate(m.chat, [user], 'promote')
    await conn.reply(m.chat, f'Berhasil menjadi sebagai admin group @{number}', None)


help = ['promote <@user>']
tags = ['group', 'admin']
command = re.compile(r'^promote$', re.I)
group = True
admin = True
botAdmin = True
