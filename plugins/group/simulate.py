import re

from lib.utils.helper import maybe_await


ACTIONS = {
    'add': 'add', 'invite': 'add', 'welcome': 'add',
    'bye': 'remove', 'kick': 'remove', 'leave': 'remove', 'remove': 'remove',
    'promote': 'promote', 'demote': 'demote',
}


async def handler(m, *, conn, args, text, usedPrefix, command):
    p = usedPrefix + command
    event = args[0] if args else None
    if not event:
        return await m.reply(f'Example:\n{p} welcome @user\n{p} bye @user\n{p} promote @user\n{p} demote @user')
    mentions = text.replace(event, '', 1).lstrip()
    who = conn.parseMention(mentions) if mentions else []
    part = who or [m.sender]
    await m.reply(f'Simulating *{event}...*')
    act = ACTIONS.get(event.lower())
    if not act:
        raise Exception(f'Event "{event}" tidak dikenali')
    return await maybe_await(conn.participantsUpdate({'id': m.chat, 'participants': part, 'action': act}))


help = ['simulate <event> [@mention]']
tags = ['owner']
command = re.compile(r'^simulate$', re.I)
ai = {'risk': 'low', 'isTool': False, 'description': 'dummy simulate group event'}
