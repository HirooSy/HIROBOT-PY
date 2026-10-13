import asyncio
import os
import re
import shutil
import sys

from lib.utils import helper


ROOT = helper.ROOT
IGNORE_RULES = ['.cache/', '.gitignore', 'node_modules/', '.env', 'data/', '*.log', '*.zip', '*.bin', '*.pid', '*.bak', '.git', '*.tar.gz',
                'package-lock.json', 'yarn.lock', 'pnpm-lock.json', '.npmrc', '.npm', 'replit.nix', '.replit', '.github/*',
                '!.github/workflows', '.github/workflows/*', '!.github/workflows/uptimer.yml', 'run.js', '__pycache__/', '*.pyc']
UNTRACK_TARGETS = ['.cache', 'node_modules', '.github', '.gitignore', '.env', 'data', 'run.js', '__pycache__']


async def sh(cmd, env=None):
    proc = await asyncio.create_subprocess_shell(cmd, cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        detail = err.decode('utf-8', 'replace')
        raise RuntimeError(f'Command failed: {cmd}\n{detail}')
    return out.decode('utf-8', 'replace')


async def try_sh(cmd, env=None):
    try:
        return await sh(cmd, env)
    except Exception:
        return ''


def write_exclude():
    path = os.path.join(ROOT, '.git', 'info', 'exclude')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = ''
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as fh:
            content = fh.read()
    for rule in IGNORE_RULES:
        lines = [line.strip() for line in content.split('\n')]
        if rule not in lines:
            content += '\n' + rule
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(content)


async def push(conn, m, args):
    message = ' '.join(args) if args else '✦'
    await m.react('⬆️')
    try:
        token, user, email, repo = (os.environ.get(k) for k in ('GIT_TOKEN', 'GIT_USER', 'GIT_EMAIL', 'GIT_REPO'))
        if not (token and user and email and repo):
            raise RuntimeError('GIT_TOKEN, GIT_USER, GIT_EMAIL, or GIT_REPO is not set')
        cache = os.path.join(ROOT, '.cache')
        if os.path.exists(cache):
            shutil.rmtree(cache, ignore_errors=True)
        await try_sh('git init')
        write_exclude()
        remote = f'https://{user}:{token}@github.com/{user}/{repo}.git'
        await sh(f'git config user.email "{email}"')
        await sh(f'git config user.name "{user}"')
        env = {**os.environ, 'GIT_AUTHOR_NAME': user, 'GIT_AUTHOR_EMAIL': email}
        await try_sh('git remote remove origin')
        await sh(f'git remote add origin "{remote}"')
        for target in UNTRACK_TARGETS:
            await try_sh(f'git rm -r --cached --ignore-unmatch "{target}"')
        await sh('git add .')
        uptimer = os.path.join(ROOT, '.github', 'workflows', 'uptimer.yml')
        if os.path.exists(uptimer):
            await try_sh('git add -f .github/workflows/uptimer.yml')
        status = await sh('git status --porcelain')
        if not status.strip():
            await conn.reply(m.chat, '✅ Nothing to upload, no changes detected.', m)
            await m.react('✅')
            return
        await try_sh('git commit -m "' + message.replace('"', '\\"') + '"', env)
        await sh('git branch -M main')
        await sh('git push -u origin main --force')
        await conn.reply(m.chat, f'✅ Successfully uploaded to GitHub!\n\n- *Repo:* {repo}\n- *Commit:* {message}\n- *Branch:* main', m)
        await m.react('✅')
    except Exception as error:
        print(error, file=sys.stderr)
        await conn.reply(m.chat, f'❌ Upload failed:\n\n{error}', m)
        await m.react('❌')


async def status(conn, m):
    await m.react('🔍')
    try:
        branch = await sh('git rev-parse --abbrev-ref HEAD')
        short = await sh('git status --short')
        branch_line = await sh('git status -sb')
        ahead = 'ahead' in branch_line
        text = f'*GitHub Repository Status*\n\n- *Branch:* `{branch.strip()}`\n'
        if not short:
            text += '- *Status:* Working directory clean. (No changes)\n'
        else:
            text += f'- *Status:* There are uncommitted changes:\n```\n{short.strip()}\n```\n'
        if ahead:
            text += '- *Sync:* There are commits not yet pushed to remote.\nUse `.gitpush` to upload.'
        else:
            text += '- *Sync:* Up-to-date with remote.'
        await conn.reply(m.chat, text, m)
        await m.react('✅')
    except Exception as error:
        print('[gitstats] Error:', error, file=sys.stderr)
        await conn.reply(m.chat, f'❌ Failed to check GitHub status: {error}', m)
        await m.react('❌')


async def handler(m, *, conn, command, args):
    if command == 'gitpush':
        await push(conn, m, args)
    else:
        await status(conn, m)


help = ['gitpush <commit message>', 'gitstats']
tags = ['owner']
command = re.compile(r'^(gitpush|gitstats)$', re.I)
rowner = True
ai = {'risk': 'low', 'summarize': True, 'description': 'Push project to github repository'}
