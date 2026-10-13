import re

from lib.utils.handler import G, PluginError


async def handler(m, *, text, usedPrefix, command):
    text, p, command = text, usedPrefix, command
    if not text:
        raise PluginError(f"- *Example:*\n- {p + command} `Name` | `Author`\n- {p + command} {G.settings['botname']} | Owner\n\n"
                          "> *Note:* You can skip name using [ .swm  | author ], also You can skip author using [ .swm name ], and You can use `Space`")
    user = G.db.data['users'][m.sender]
    name, *auth = text.split('|')
    user['stickerwm'] = [name.strip(), '|'.join(auth).strip()]
    await m.react('✅')


help = ['setwm']
tags = ['sticker']
command = re.compile(r'^setwm$', re.I)
level = 2
