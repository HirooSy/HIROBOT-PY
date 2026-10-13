import asyncio
import os
import re
import sys
import time

from lib.utils import converter


GIT_TIMEOUT = 180
EXCLUDED = ['node_modules', 'package-lock.json', 'yarn.lock', 'data/store.json', 'data/ai/backups', 'data/reminder.json', 'data/tunnel',
            'data/tmp', '.cache', '.npm', '.agents', '.config', 'data/sessions/store.db', 'data/python', '.local']


def dir_size(d):
    total = 0
    for item in os.listdir(d):
        full = os.path.join(d, item)
        total += dir_size(full) if os.path.isdir(full) else os.path.getsize(full)
    return total


def fmt(b):
    units = ['B', 'KB', 'MB', 'GB']
    i = 0
    b = float(b)
    while b >= 1024 and i < len(units) - 1:
        b /= 1024
        i += 1
    return f'{b:.2f} {units[i]}' if i else f'{int(b)} B'


async def git(cwd, args):
    proc = await asyncio.create_subprocess_exec('git', *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f'git {" ".join(args)} timed out')
    if proc.returncode != 0:
        raise RuntimeError(err.decode('utf-8', 'replace').strip() or f'git {" ".join(args)} exited with {proc.returncode}')


async def cleanup_git(cwd):
    git_dir = os.path.join(cwd, '.git')
    if not os.path.isdir(git_dir):
        return None
    before = dir_size(git_dir)
    try:
        await git(cwd, ['reflog', 'expire', '--expire-unreachable=now', '--all'])
        await git(cwd, ['gc', '--aggressive', '--prune=now'])
    except Exception as e:
        print('[backup] git cleanup failed:', e, file=sys.stderr)
    hooks = os.path.join(git_dir, 'hooks')
    if os.path.isdir(hooks):
        for f in os.listdir(hooks):
            if f.endswith('.sample'):
                os.remove(os.path.join(hooks, f))
    return {'before': before, 'after': dir_size(git_dir)}


def add_folder(zf, folder, cwd, excluded):
    for item in os.listdir(folder):
        full = os.path.join(folder, item)
        if full in excluded or item == '__pycache__':
            continue
        if os.path.isdir(full):
            add_folder(zf, full, cwd, excluded)
        else:
            with open(full, 'rb') as fh:
                zf.file(os.path.relpath(full, cwd), fh.read())


async def handler(m, *, conn):
    await m.react('⏳')
    cwd = os.getcwd()
    git_info = await cleanup_git(cwd)
    zf = converter.ZipFile()
    excluded = [os.path.join(cwd, p) for p in EXCLUDED]
    await asyncio.to_thread(add_folder, zf, cwd, cwd, excluded)
    data = await zf.generateAsync(type='nodebuffer', compression='DEFLATE', compressionOptions={'level': 9})
    caption = f"- .git: {fmt(git_info['before'])} → {fmt(git_info['after'])}\n- Zip: {fmt(len(data))}" if git_info else f'- Zip: {fmt(len(data))}'
    await conn.sendMessage(m.chat, {'document': data, 'mimetype': 'application/zip', 'fileName': f'backup_{int(time.time() * 1000)}.zip', 'caption': caption}, {'quoted': m})
    await m.react('✅')


command = re.compile(r'^(backup)$', re.I)
tags = ['owner']
help = ['backup']
rowner = True
private = True
ai = {'risk': 'low', 'description': 'backup project'}