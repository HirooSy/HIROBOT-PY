import json
import math
import os
import re
import shutil
import sys
from datetime import datetime, timezone

from lib.package.ai import mcp


ROOT = os.getcwd()


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _msg(err):
    return str(err)


def _js_len(text):
    return len(str(text).encode('utf-16-le')) // 2


def _js_round(x):
    return int(math.floor(x + 0.5))


def _ctx():
    return mcp.ctx()


def _basename(file_path):
    return file_path.split('/')[-1]


def _ext(file_path):
    return (file_path.split('.')[-1] or '').lower()


def _resolve(file_path):
    return os.path.abspath(os.path.join(ROOT, file_path))


def _fmt_size(b):
    if b < 1048576:
        return f'{b / 1024:.1f}KB'
    return f'{b / 1048576:.1f}MB'


def _iso_stamp():
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S') + f'.{now.microsecond // 1000:03d}Z'


async def _exec_read_file(args=None, **kw):
    a = _args(args, kw)
    file_path = a.get('file_path')
    offset = a.get('offset')
    if offset is None:
        offset = 0
    abs_path = _resolve(file_path)
    if not os.path.exists(abs_path):
        wanted_name = _basename(file_path).lower()
        matches = []
        skip_dirs = {'node_modules', '.git', 'sessions', 'tmp'}

        def walk(directory, depth=0):
            if depth > 6 or len(matches) >= 8:
                return
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name)
            except OSError:
                return
            for ent in entries:
                if len(matches) >= 8:
                    return
                if ent.is_dir(follow_symlinks=False):
                    if ent.name in skip_dirs:
                        continue
                    walk(os.path.join(directory, ent.name), depth + 1)
                elif ent.name.lower() == wanted_name:
                    matches.append(os.path.relpath(os.path.join(directory, ent.name), ROOT))

        walk(ROOT)
        if len(matches) == 1:
            return mcp.read_file_tool_core(matches[0], offset)
        if len(matches) > 1:
            btn_list = [
                {
                    'type': 'reply',
                    'label': ('…' + m[-23:]) if len(m) > 24 else m,
                    'value': f'read file {m}',
                }
                for m in matches[:8]
            ]
            payload = json.dumps({
                '__type': 'buttons',
                'body': f'File "{file_path}" was not found exactly. Which one is it?',
                'buttons': btn_list,
            }, ensure_ascii=False, separators=(',', ':'))
            return (
                f'File "{file_path}" was not found exactly. Found {len(matches)} candidates with the same name: {", ".join(matches[:8])}.\n\n'
                'Offer these choices to the user through buttons — reply with this JSON __type:"buttons" AS THE ONLY CONTENT OF YOUR RESPONSE (no other text outside the JSON):\n'
                + payload
            )
        return f'File not found: {file_path} (also searched the whole project, no file named "{wanted_name}")'
    return mcp.read_file_tool_core(file_path, offset)


async def _exec_write_file(args=None, **kw):
    a = _args(args, kw)
    file_path = a.get('file_path')
    content = a.get('content')
    abs_path = _resolve(file_path)
    is_py_module = bool(re.search(r'\.py$', file_path, re.I))
    if is_py_module and isinstance(content, str):
        try:
            compile(content, file_path, 'exec')
        except SyntaxError as syn_err:
            line_num = syn_err.lineno or 1
            return (
                f'REJECTED: {file_path} has a Python syntax error at line {line_num}: {syn_err.msg}. '
                'This project is Python 3.10+ (see CODE_CONVENTIONS), so the file must compile (py_compile) before it is written. '
                'Fix the syntax first (no JavaScript idioms such as let/const, ===, require()), then call write_file again. '
                'write_file writes NOTHING to disk as long as the .py file does not compile.'
            )
        except ValueError as val_err:
            return (
                f'REJECTED: {file_path} cannot be compiled: {val_err}. '
                'The content must be plain Python source text without null bytes. Fix it, then call write_file again. '
                'write_file writes NOTHING to disk as long as the .py file does not compile.'
            )
    is_esm_module = bool(re.search(r'\.(m?js)$', file_path, re.I)) and not file_path.endswith('.cjs')
    if is_esm_module:
        require_match = re.search(r"\brequire\s*\(\s*['\"`]", content)
        if require_match:
            line_num = content[:require_match.start()].count('\n') + 1
            return (
                f'REJECTED: {file_path} still uses require() at line ~{line_num} — this project is pure ESM '
                '(see CODE_CONVENTIONS), every import MUST use "import x from \'y\'" at the top of the file, '
                "or \"const x = (await import('y')).default\" if you need a dynamic import (e.g. inside a function, "
                'or when the import is conditional). Fix it into an ESM import first, then call write_file again. '
                'write_file writes NOTHING to disk as long as a .js/.mjs file still contains require().'
            )
    existed = os.path.exists(abs_path)
    old_content = ''
    if existed:
        with open(abs_path, 'r', encoding='utf-8') as f:
            old_content = f.read()
    c = _ctx()
    old_len = _js_len(old_content)
    new_len = _js_len(content)
    if existed and old_len > 200:
        shrink_ratio = 1 - (new_len / old_len)
        if shrink_ratio > 0.2:
            diff = mcp.build_simple_diff(old_content, content)
            if c['autoHealActive'] and c['autoHealNotifyJid'] and c['conn'] is not None:
                await mcp.append_auto_heal_status(
                    f'*Auto-Heal CANCELLED* for {file_path}\n\n'
                    f'The change would shrink the file from {old_len} → {new_len} chars '
                    f'({_js_round(shrink_ratio * 100)}% shorter). This is the same pattern as an earlier incident '
                    '(important logic silently removed), so it was rejected automatically and needs manual review.\n\n'
                    f'Diff (summary):\n{diff[:1500]}'
                )
            return (
                f'REJECTED: change would shrink {file_path} by {_js_round(shrink_ratio * 100)}% '
                f'({old_len} → {new_len} chars). This is usually a sign that content/logic was removed '
                'unintentionally (e.g. the AI rewrote it from its own memory instead of copying the original content), not a '
                'normal edit. If the file SHOULD really become shorter (e.g. a deliberate refactor/removal of dead code), '
                'explain the reason explicitly to the user and ask for confirmation first before '
                'calling write_file again — a file this large will not be overwritten automatically.'
            )
    if existed:
        backup_dir = mcp.AI_BACKUP_DIR
        os.makedirs(backup_dir, exist_ok=True)
        stamp = re.sub(r'[:.]', '-', _iso_stamp())
        backup_name = re.sub(r'[/\\]', '__', file_path) + '.' + stamp + '.bak'
        shutil.copyfile(abs_path, os.path.join(backup_dir, backup_name))
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, 'w', encoding='utf-8') as f:
        f.write(content)
    if c['autoHealActive'] and c['autoHealNotifyJid'] and c['conn'] is not None and existed:
        diff = mcp.build_simple_diff(old_content, content)
        await mcp.append_auto_heal_status(f'Wrote {file_path} ({old_len} -> {new_len} chars)\n\nDiff:\n{diff[:1500]}')
    return f'Written: {file_path} ({new_len} chars)'


async def _exec_list_files(args=None, **kw):
    a = _args(args, kw)
    dir_path = a.get('dir_path')
    if dir_path is None:
        dir_path = '.'
    abs_path = _resolve(dir_path)
    if not os.path.exists(abs_path):
        return f'Folder not found: {dir_path}'
    entries = sorted(os.scandir(abs_path), key=lambda e: e.name)
    listing = '\n'.join(f'{e.name}/' if e.is_dir(follow_symlinks=False) else e.name for e in entries)
    return f'{dir_path}:\n{listing}'


async def _exec_delete_file(args=None, **kw):
    a = _args(args, kw)
    file_path = a.get('file_path')
    abs_path = _resolve(file_path)
    if not os.path.exists(abs_path):
        return f'File not found: {file_path}'
    if os.path.isdir(abs_path):
        return f'{file_path} is a directory'
    os.unlink(abs_path)
    return f'File dihapus: {file_path}'


async def _exec_move_file(args=None, **kw):
    a = _args(args, kw)
    src_rel = a.get('from')
    dst_rel = a.get('to')
    src = _resolve(src_rel)
    dst = _resolve(dst_rel)
    if not os.path.exists(src):
        return f'Not found: {src_rel}'
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)
    return f'{src_rel} -> {dst_rel}'


async def _exec_search_files(args=None, **kw):
    a = _args(args, kw)
    query = a.get('query')
    folder = a.get('folder')
    if folder is None:
        folder = '.'
    target = _resolve(folder)
    results = []
    needle = query.lower()

    def walk(directory, depth=0):
        if depth > 6:
            return
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return
        for item in names:
            if item in ('node_modules', '.git'):
                continue
            full = os.path.join(directory, item)
            rel = os.path.relpath(full, ROOT) if full.startswith(ROOT) else full
            if needle in item.lower():
                try:
                    st = os.stat(full)
                    results.append(f'{rel} ({_fmt_size(st.st_size)})' if os.path.isfile(full) else rel)
                except OSError:
                    pass
            try:
                if os.path.isdir(full):
                    walk(full, depth + 1)
            except OSError:
                pass

    walk(target)
    if not results:
        return f'No file matches "{query}"'
    return f'"{query}" — {len(results)} results:\n\n' + '\n'.join(results[:40])


MIME_BY_EXT = {
    'js': 'text/javascript', 'mjs': 'text/javascript', 'ts': 'text/typescript',
    'json': 'application/json', 'html': 'text/html', 'css': 'text/css',
    'py': 'text/x-python', 'md': 'text/markdown', 'txt': 'text/plain',
    'env': 'text/plain', 'yml': 'text/yaml', 'yaml': 'text/yaml',
    'sh': 'text/x-sh', 'sql': 'text/plain', 'xml': 'text/xml',
}

LANG_BY_EXT = {
    'js': 'javascript', 'mjs': 'javascript', 'ts': 'typescript', 'json': 'json',
    'html': 'html', 'css': 'css', 'py': 'python', 'md': 'markdown', 'sh': 'bash',
    'sql': 'sql', 'yml': 'yaml', 'yaml': 'yaml', 'env': 'text', 'txt': 'text', 'xml': 'xml',
}


async def _exec_send_as_file(args=None, **kw):
    a = _args(args, kw)
    file_path = a.get('file_path')
    content = a.get('content')
    caption = a.get('caption')
    c = _ctx()
    if c['autoHealActive']:
        return 'This tool is disabled during auto-heal — the change diff is already shown to the owner automatically through write_file, do not send the file again here.'
    if c['conn'] is None or not c['currentJid']:
        return 'WA connection not ready'
    if not file_path:
        return 'file_path is required'
    final_content = content
    if not final_content:
        if not c['isOwner']:
            return 'Empty content (read-from-disk mode) is only allowed for the owner. Fill the content parameter manually with the content you want to send.'
        abs_path = _resolve(file_path)
        if not os.path.exists(abs_path):
            return f'Content is empty and the file was not found on disk: {file_path}. Fill the content parameter manually, or make sure file_path is correct (an absolute path is allowed).'
        if os.path.isdir(abs_path):
            return f'{file_path} is a folder, not a file. Give the path to the file.'
        with open(abs_path, 'r', encoding='utf-8') as f:
            final_content = f.read()
    if not final_content:
        return 'Content cannot be empty'
    mimetype = MIME_BY_EXT.get(_ext(file_path), 'text/plain')
    file_name = _basename(file_path)
    content_len = _js_len(final_content)
    content_obj = {
        'document': final_content.encode('utf-8'),
        'mimetype': mimetype,
        'fileName': file_name,
    }
    if caption:
        content_obj['caption'] = caption
    try:
        await c['conn'].sendMessage(c['currentJid'], content_obj, {'quoted': c['currentM']})
        return f'File "{file_name}" ({content_len} chars) sent as attachment.'
    except Exception as e:
        print('[send_as_file] Error:', e, file=sys.stderr)
        return f'Failed to send file: {_msg(e)}. Try send_codeblock instead.'


async def _exec_send_codeblock(args=None, **kw):
    a = _args(args, kw)
    file_path = a.get('file_path')
    title = a.get('title')
    description = a.get('description')
    c = _ctx()
    if c['autoHealActive']:
        return 'This tool is disabled during auto-heal — the change diff is already shown to the owner automatically through write_file, do not send the codeblock again here.'
    if c['conn'] is None or not c['currentJid']:
        return 'WA connection not ready'
    if not file_path:
        return 'file_path is required'
    abs_path = _resolve(file_path)
    if not os.path.exists(abs_path):
        return f'File not found: {file_path}'
    if os.path.isdir(abs_path):
        return f'{file_path} is a folder, not a file.'
    with open(abs_path, 'r', encoding='utf-8') as f:
        content = f.read()
    content_len = _js_len(content)
    if content_len > 4000:
        return f'File "{file_path}" is too large for a codeblock ({content_len} chars, limit ~4000 so it is not heavy in the chat). Use the send_as_file tool with the same file_path (leave content empty) instead — no need to re-read it manually.'
    language = LANG_BY_EXT.get(_ext(file_path), 'text')
    file_name = _basename(file_path)
    try:
        rich = c['conn'].aiRich()
        rich.setTitle(title or file_name)
        if description:
            rich.addText(description + '\n', {'hyperlink': True})
        rich.addCode(language, content)
        await rich.send(c['currentJid'], {'quoted': c['currentM']})
        return f'Codeblock "{file_name}" ({content_len} chars) sent.'
    except Exception:
        try:
            msg = (f'*{title}*\n\n' if title else '') + (f'{description}\n\n' if description else '') + '```' + language + '\n' + content + '\n```'
            await c['conn'].sendMessage(c['currentJid'], {'text': msg}, {'quoted': c['currentM']})
            return f'Codeblock "{file_name}" sent (plain text fallback).'
        except Exception as e2:
            print('[send_codeblock] Error:', e2, file=sys.stderr)
            return f'Failed to send codeblock: {_msg(e2)}'


default = [
    {
        'name': 'read_file',
        'description': (
            'Read the content of a file on the server. Can read config, plugins, .env, etc. For JSON files (package.json, '
            'config.json, etc), it parses and displays them in a tidier format. For VERY LARGE files (more than ~100k characters), '
            'the content is cut into parts — use the offset parameter to get the next part (see the [FILE TRUNCATED] note in the '
            'response when this happens). If the given path is not found EXACTLY, this tool automatically searches for files with a '
            'similar name across the whole project and offers the choices through buttons — you do NOT need to try other paths '
            'yourself by trial and error, just call this tool and let it do the searching.'
        ),
        'parameters': {
            'file_path': {
                'type': 'string',
                'description': (
                    'File path. May be RELATIVE to the bot root (example: "plugins/ai.js", "package.json") OR ABSOLUTE starting '
                    'with "/" (example: "/etc/hosts", "/var/log/syslog") to read a file ANYWHERE on the server, not limited to the '
                    'project folder. May also be just a file name without a folder (e.g. "profile.js") — this tool will find its '
                    'location inside the project by itself if it is not found at the root.'
                ),
                'required': True,
            },
            'offset': {
                'type': 'number',
                'description': 'Character position to start reading from (default 0). Used to get the next part of a large file that was cut off — fill it with the number mentioned in the [FILE TRUNCATED] note from the previous read_file call.',
                'required': False,
            },
        },
        'execute': _exec_read_file,
    },
    {
        'name': 'write_file',
        'description': (
            'Write/overwrite the content of a file on the server. It automatically makes a backup before overwriting. NEVER use '
            'this tool for requests like "send/show/let me see the content of file X" — those MUST use read_file followed by '
            'send_codeblock/send_as_file (this tool does NOT send anything to the chat, it only writes to the server disk). '
            'write_file is ONLY for when the user explicitly asks to CHANGE/EDIT the content of a file (e.g. "change the version '
            'in package.json to 2.0", "add function X to file Y"). Misusing this tool just to "display" a file has once really '
            "overwritten the user's original file with a wrong/shorter version — ALWAYS make sure the content you write is EXACTLY "
            'what the user wants, do NOT rewrite from your own memory/assumptions. THIS PROJECT IS PYTHON 3.10+ with asyncio, '
            'standard library only (see CODE_CONVENTIONS): for .py files, the content must compile, and imports go at the top of the file '
            '(no third-party packages, no JavaScript syntax such as let/const, ===, require() or module.exports). '
            'This tool will REFUSE to write a .py file that has a syntax error and reports the line number. '
            'Node-style .js/.mjs files must not contain require() either; this tool refuses those too.'
        ),
        'parameters': {
            'file_path': {
                'type': 'string',
                'description': 'File path. May be RELATIVE to the bot root OR ABSOLUTE starting with "/" to write to any file on the server (not limited to the project folder) — be careful when writing outside the project, make sure it is really what the user asked for.',
                'required': True,
            },
            'content': {
                'type': 'string',
                'description': 'The file content to write',
                'required': True,
            },
        },
        'execute': _exec_write_file,
    },
    {
        'name': 'list_files',
        'description': 'List the content of a folder on the server. Can be used for any folder across the whole system, not limited to the bot project folder.',
        'parameters': {
            'dir_path': {
                'type': 'string',
                'description': 'Folder path. RELATIVE to the bot root (default: "." = project root) OR ABSOLUTE starting with "/" (example: "/", "/etc", "/home") to list any directory on the server.',
                'required': False,
            },
        },
        'execute': _exec_list_files,
    },
    {
        'name': 'delete_file',
        'description': 'Delete a file from the server.',
        'parameters': {
            'file_path': {
                'type': 'string',
                'description': 'Path of the file to delete. RELATIVE to the bot root OR ABSOLUTE starting with "/" to delete any file on the server. BE CAREFUL outside the project — make sure the user really asked for it.',
                'required': True,
            },
        },
        'execute': _exec_delete_file,
    },
    {
        'name': 'move_file',
        'description': 'Move or rename a file/folder on the server.',
        'parameters': {
            'from': {
                'type': 'string',
                'description': 'Source path. RELATIVE to the bot root OR ABSOLUTE starting with "/" for any file/folder on the server.',
                'required': True,
            },
            'to': {
                'type': 'string',
                'description': 'Destination path. RELATIVE to the bot root OR ABSOLUTE starting with "/".',
                'required': True,
            },
        },
        'execute': _exec_move_file,
    },
    {
        'name': 'search_files',
        'description': 'Search for files by name on the server. Can search the whole system through an absolute folder parameter, not limited to the project folder.',
        'parameters': {
            'query': {
                'type': 'string',
                'description': 'The file name or part of it',
                'required': True,
            },
            'folder': {
                'type': 'string',
                'description': 'Search folder. RELATIVE to the bot root (default: ".") OR ABSOLUTE starting with "/" to search any directory on the server.',
                'required': False,
            },
        },
        'execute': _exec_search_files,
    },
    {
        'name': 'send_as_file',
        'description': (
            "Send content as a FILE ATTACHMENT (document) on WhatsApp, not as a text message/card. USE THIS TOOL (not send_codeblock) "
            'when the file/code to display is LARGE ENOUGH that send_codeblock would need to be called many times (more than ~1-2 '
            "parts) — sending many send_codeblock cards in a row makes the chat lag and is heavy on the user's phone. A document "
            'attachment is much lighter for large files: the user just opens/saves the file, no need to scroll through many messages. '
            'For SMALL files that fit in one send_codeblock, still use send_codeblock (it has syntax highlighting, easier to read '
            'directly in the chat).\n\n'
            'IMPORTANT about the "content" parameter: if file_path points to a file THAT ALREADY EXISTS on the server (reproducing an '
            'existing file, not creating a new one), LEAVE the content parameter EMPTY — this tool will read that file DIRECTLY FROM '
            'DISK itself, guaranteed exact with no risk of being cut off or mistyped. Fill the content parameter manually ONLY if it '
            'is content you composed/assembled yourself from scratch (not a reproduction of an existing file).'
        ),
        'parameters': {
            'file_path': {
                'type': 'string',
                'description': 'File path. If this file ALREADY EXISTS on the server and content is left empty, this path is also used to read its content directly from disk (may be relative to the root OR absolute starting with "/"). If it is only a display name for new content you composed yourself, it can be any name.',
                'required': True,
            },
            'content': {
                'type': 'string',
                'description': 'The complete content of the file to send as an attachment. OPTIONAL — leave empty if file_path points to a file that already exists on the server (so it is read directly from disk, which is safer). Fill manually ONLY for content you composed/generated yourself from scratch.',
                'required': False,
            },
            'caption': {
                'type': 'string',
                'description': 'Short text that accompanies the file (optional) — e.g. "Here is mcp.js, ~146k characters"',
                'required': False,
            },
        },
        'execute': _exec_send_as_file,
    },
    {
        'name': 'send_codeblock',
        'description': (
            'Send the content of a file THAT ALREADY EXISTS on the server as a code block with syntax highlighting in the WhatsApp '
            'chat. YOU MUST USE THIS TOOL every time the user asks to see the content of a file that ALREADY EXISTS (e.g. "show the '
            'content of package.json", "let me see the ai.js code") and it is small (~under 4000 characters) — JUST give file_path, do '
            'NOT retype the content yourself into any parameter. This tool reads the file DIRECTLY FROM DISK, guaranteed exact '
            'character-for-character, it cannot be cut off or mistyped like when you reproduce it manually. If the file turns out to '
            'be too large, this tool tells you to use send_as_file(file_path) instead (also without retyping the content). For code YOU '
            'WRITE YOURSELF from scratch (not a reproduction of an existing file), still use the manual JSON format '
            '{"__type":"codeblock",...} in the final answer as usual — this tool is ONLY for files that already exist on disk.'
        ),
        'parameters': {
            'file_path': {
                'type': 'string',
                'description': 'Path of the file whose content should be displayed. May be relative to the bot root OR absolute starting with "/". The content is read directly from disk by this tool — do not copy it manually into any other parameter.',
                'required': True,
            },
            'title': {
                'type': 'string',
                'description': 'Title shown above the code (optional, defaults to the file name)',
                'required': False,
            },
            'description': {
                'type': 'string',
                'description': 'Optional short explanation above the code',
                'required': False,
            },
        },
        'execute': _exec_send_codeblock,
    },
]
