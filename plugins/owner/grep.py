import asyncio
import re

from lib.utils.handler import PluginError


async def handler(m, *, text):
    if not text:
        raise PluginError('Keyword?')
    proc = await asyncio.create_subprocess_exec(
        'grep', '-rn', text, '--include=*.py', '--exclude-dir=__pycache__', '--exclude-dir=sessions', '--exclude-dir=baileys', './',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    out, err = out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')
    if err.strip():
        return await m.reply(f'Error: {err}')
    if not out.strip():
        return await m.reply(f'Not found: "{text}"')
    results = []
    number = 1
    for line in out.strip().split('\n'):
        mt = re.match(r'^(.+?):(\d+):(.*)$', line)
        if mt:
            path, num, code = mt.groups()
            code = code.strip()
            if len(code) > 150:
                code = code[:150] + '...'
            results.append(f'{number}. *{path}* (line {num})\n> {code}')
            number += 1
    await m.reply(f'🔍 *Search results: "{text}"* ({len(results)} found)\n\n' + '\n\n'.join(results))


help = ['grep <keyword>']
tags = ['owner']
command = re.compile(r'^(grep)$', re.I)
rowner = True
ai = {'risk': 'low', 'summarize': True, 'description': 'search exact keywords using grep -r'}
