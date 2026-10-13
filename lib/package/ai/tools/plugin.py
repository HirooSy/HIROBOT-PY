import os
import re
import sys

from lib.package.ai import mcp


ROOT = os.getcwd()


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _msg(err):
    return str(err)


def _attr(obj, key, default=None):
    return mcp._pget(obj, key, default)


def _is_regex(x):
    return hasattr(x, 'pattern') and hasattr(x, 'search')


def _cmd_match(cmd, text):
    if _is_regex(cmd):
        return cmd.search(text) is not None
    if isinstance(cmd, (list, tuple)):
        return any(c == text or (_is_regex(c) and c.search(text) is not None) for c in cmd)
    return cmd == text


def _as_list(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    if value:
        return [value]
    return []


def _plugin_map():
    from lib.utils import plugins as plugins_mod
    return plugins_mod.plugins or {}


def _find_by_command(text):
    for name, plugin in _plugin_map().items():
        if plugin is None or not _attr(plugin, 'command'):
            continue
        if _cmd_match(_attr(plugin, 'command'), text):
            return name, plugin
    return None, None


def _dig_text(obj, depth=0):
    if not obj or depth > 4 or not isinstance(obj, (dict, list)):
        return []
    items = obj.items() if isinstance(obj, dict) else ((str(i), v) for i, v in enumerate(obj))
    out = []
    for key, val in items:
        if isinstance(val, str) and val.strip() and re.search(r'text|caption', str(key), re.I):
            out.append(val)
        elif val and isinstance(val, (dict, list)):
            out.extend(_dig_text(val, depth + 1))
    return out


async def _exec_run_eval(args=None, **kw):
    a = _args(args, kw)
    code = a.get('code')
    silent = bool(a.get('silent') or False)
    try:
        await mcp.exec_eval(code, silent=silent)
        return f"[ALREADY SENT to the user (eval code was executed{', silent mode' if silent else ''}). If the execution produced a reply/output to the chat, it was already sent directly by the plugin -- DO NOT rewrite that output. DO NOT send any reply text after this unless a short confirmation is really needed, in your own words (e.g. \"Done running it ✅\"). If there is nothing to add, just answer with an empty string.]"
    except Exception as e:
        return f'Failed to run eval: {_msg(e)}'


async def _exec_list_plugins(args=None, **kw):
    a = _args(args, kw)
    category = a.get('category')
    try:
        entries = []
        for name, plugin in _plugin_map().items():
            if not plugin or _attr(plugin, 'disabled') or not _attr(plugin, 'help') or not isinstance(_attr(plugin, 'ai'), dict):
                continue
            help_list = _as_list(_attr(plugin, 'help'))
            tags = _as_list(_attr(plugin, 'tags'))
            cmds = []
            for h in help_list:
                c = str(h).split(' ')[0]
                if c and c not in cmds:
                    cmds.append(c)
            risk = mcp.classify_plugin_risk(name, plugin)
            reqs = mcp.plugin_requirements(plugin)
            if cmds:
                entries.append({'tags': tags, 'cmds': cmds, 'reqs': reqs, 'risk': risk['level']})

        if category:
            filtered = [e for e in entries if any(str(t).lower() == str(category).lower() for t in e['tags'])]
        else:
            filtered = entries

        if not filtered:
            suffix = f' for category "{category}"' if category else ''
            return f'No commands found{suffix}.\nAvailable categories: main, group, sticker, ai, internet, adult, tools, downloader, owner, info'

        grouped = {}
        for e in filtered:
            tag = e['tags'][0] if e['tags'] else 'other'
            grouped.setdefault(tag, []).append(e)

        total_cmds = sum(len(e['cmds']) for e in filtered)
        out = f'*Bot commands ({total_cmds}, same source as .menu):*\n_Flags: [L] limit  [P] premium  [G] group-only  [D] DM-only  [A] group admin  [B] bot-admin_\n\n'
        for tag, lst in grouped.items():
            out += f'*{tag}*\n'
            for e in lst:
                r = e['reqs']
                flags = ''.join([
                    '[L]' if r.get('limit') else '',
                    '[P]' if r.get('premium') else '',
                    '[G]' if r.get('group') else '',
                    '[D]' if r.get('private') else '',
                    '[A]' if r.get('admin') else '',
                    '[B]' if r.get('botAdmin') else '',
                ])
                out += f"  • {', '.join(e['cmds'])}{f' {flags}' if flags else ''}\n"
            out += '\n'
        return out.strip()[:4000]
    except Exception as e:
        return f'Failed to read plugin list: {_msg(e)}'


async def _exec_run_plugin(args=None, **kw):
    a = _args(args, kw)
    command = a.get('command')
    args_str = a.get('args') or ''
    confirmed = bool(a.get('confirmed') or False)
    normalized = str(command).strip().lower()
    c = mcp.ctx()

    if normalized in ('twitter', 'x'):
        try:
            summary = await mcp.download_twitter_direct(args_str)
            return f'{summary}\nDO NOT rewrite these details, just give a short confirmation if needed.'
        except Exception as e:
            print('[run_plugin] Gagal download twitter (hard redirect):', _msg(e), file=sys.stderr)
            return f'Failed to download Twitter/X: {_msg(e)}'

    menu_like_unverified = ['help', 'allmenu', 'list']
    if normalized in menu_like_unverified and not c['isOwner']:
        is_same_as_menu = False
        try:
            menu_name, _ = _find_by_command('menu')
            this_name, _ = _find_by_command(normalized)
            is_same_as_menu = bool(menu_name) and menu_name == this_name
        except Exception:
            pass
        if not is_same_as_menu:
            return mcp.ai_only(f'Command "{command}" is not run automatically through the AI for non-owners — this plugin may display the list of owner commands. Just explain the bot features in your own words to the user, or ask the user to type ".{command}" directly.')

    try:
        pre = await mcp.resolve_plugin(command)
        pre_plugin = pre.get('plugin')
        should_summarize = _attr(_attr(pre_plugin, 'ai'), 'summarize') is True

        res = await mcp.exec_plugin_command(command, args_str, confirmed=confirmed, capture_output=should_summarize)
        plugin_name = res.get('pluginName')
        exec_risk = res.get('risk')
        if exec_risk:
            risk = exec_risk
        elif pre_plugin:
            risk = mcp.classify_plugin_risk(plugin_name, pre_plugin)
        else:
            risk = {'level': 'none', 'reason': 'Risk unknown (fallback).'}
        suffix = f" {args_str}" if args_str else ''

        if not should_summarize:
            tags = _as_list(_attr(pre_plugin, 'tags'))
            if 'downloader' in tags:
                return f'[STOP -- DO NOT CALL run_plugin AGAIN FOR THIS COMMAND/LINK]\nThe command ".{command}{suffix}" (downloader) was already run ONCE and the plugin already sent its own reply to the user (usually a numbered list of choices when there are several formats/qualities, which CANNOT be completed through run_plugin). Your job here is DONE. DO NOT rewrite the plugin\'s reply, DO NOT call run_plugin again with the same command/link. If this platform is supported in download_media, use that tool to complete the download directly. If not, just remind the user ONCE to reply with the number of their choice directly in the chat -- then stay quiet, do not retry anything.'
            return f'Command ".{command}{suffix}" finished running (internal risk: {risk["level"]}, do NOT mention this term to the user). The plugin already sent its reply directly to the user -- DO NOT rewrite/add any other reply about this, just move on to something else if there is anything, or stay silent if there is nothing more to say.'

        conn = c['conn']
        captured = res.get('captured') or []
        text_parts = []
        for msg in captured:
            content = msg.get('content') or {}
            if isinstance(content.get('text'), str):
                text_parts.append(content['text'])
            elif isinstance(content.get('conversation'), str):
                text_parts.append(content['conversation'])
            elif content.get('caption'):
                text_parts.append(content['caption'])

            if msg.get('opts'):
                for extra in _dig_text(msg['opts']):
                    if extra not in text_parts:
                        text_parts.append(extra)

            is_media_only = any(content.get(k) for k in ('image', 'video', 'document', 'audio', 'sticker'))
            if is_media_only and conn is not None:
                try:
                    await conn.sendMessage(msg.get('jid'), content, msg.get('opts'))
                except Exception as e:
                    print(f'[run_plugin] Gagal kirim ulang media captured dari "{command}": {_msg(e)}', file=sys.stderr)

        combined = '\n\n'.join(p for p in text_parts if p)
        return mcp.ai_only(
            f'Command ".{command}{suffix}" finished running (internal risk: {risk["level"]}, do NOT mention this term to the user).\n\n'
            f'RAW OUTPUT of the plugin (DO NOT forward/copy-paste it raw to the user, this is only DATA for you to read):\n'
            f'{mcp.PLUGIN_OUT_START}\n{combined or "(the plugin did not send any text message)"}\n{mcp.PLUGIN_OUT_END}\n\n'
            'Reply to the user in an ordinary natural conversational style matching your persona, AS SHORT AS POSSIBLE according to what the user actually asked/requested -- NOT a re-listing of all the fields above. Example: if the user only said "try ping", a natural answer is just something like "Pong! Response ~100ms." -- the RAM/CPU/disk/etc details above are only a reference for you, do NOT show them unless the user really asks about them (or you briefly offer "want to see the server details too?" without dumping everything right away).'
        )
    except Exception as e:
        return _msg(e)


async def _exec_check_plugin_risk(args=None, **kw):
    a = _args(args, kw)
    command = a.get('command')
    try:
        found_name, found = _find_by_command(command)
        if found is None:
            return f'Command "{command}" was not found. Check with list_plugins first for the correct command name.'
        risk = mcp.classify_plugin_risk(found_name, found)
        reqs = mcp.plugin_requirements(found)
        level = risk['level']
        owner_note = ' Level HIGH: only the owner may run this through the AI Agent, AND you must still ask for explicit confirmation first before run_plugin (confirmed: true) even if the requester is the owner.' if level == 'high' else ''
        banned_note = ' This is NOT a restriction on the user -- the user can still run this command manually by typing it directly in the chat. What is forbidden is the AI Agent running it through run_plugin.' if level == 'blocked' else ''
        none_note = f' Plugin file: {found_name}. If the requester is the owner, read its source (read_file), then decide the fitting risk and write it back to this file (write_file) before running it -- see rule 6c.' if level == 'none' else ''
        req_notes = [
            'can only be used in a group' if reqs.get('group') else '',
            'can only be used in a DM/private chat' if reqs.get('private') else '',
            'needs premium status' if reqs.get('premium') else '',
            'needs the sender to be a group admin' if reqs.get('admin') else '',
            'needs the bot to be a group admin' if reqs.get('botAdmin') else '',
            'uses a usage limit' if reqs.get('limit') else '',
        ]
        req_notes = [x for x in req_notes if x]
        req_line = f"\nAdditional requirements: {', '.join(req_notes)}." if req_notes else ''
        level_label = 'BANNED' if level == 'blocked' else level.upper()
        return mcp.ai_only(
            f'Command "{command}" -> internal risk {level_label} (source: {risk.get("source") or "floor"}). {risk.get("reason", "")}{banned_note}{owner_note}{none_note}{req_line}\n'
            f'NOTE: "{level_label}" is an internal term for you (the AI Agent) only -- NEVER mention this word or the word "risk" to the user, reply naturally (see rule 6c).'
        )
    except Exception as e:
        return f'Failed to check the risk of command "{command}": {_msg(e)}'


PLUGIN_GUIDE_FALLBACK = [
    "*This Bot's Plugin Guide* (built-in, there is no external PLUGIN_GUIDE.md)",
    '',
    '```python',
    'import re',
    '',
    'async def handler(m, *, conn, text, usedPrefix, command):',
    "    if not text:",
    "        raise Exception(f\"Example: {usedPrefix + command} <input>\")",
    "    # ...the plugin's main logic here...",
    "    await conn.sendMessage(m.chat, {'text': 'result'}, {'quoted': m})",
    '',
    "help = ['commandname <arg>']",
    "tags = ['category']  # main, group, sticker, ai, internet, downloader, owner, etc",
    '',
    '# command can be a regex OR a list of strings — both are valid:',
    "command = re.compile(r'^(name|alias)$', re.I)",
    "# OR: command = ['name', 'alias']",
    '',
    'limit = 1        # a NUMBER (limit cost per use), NOT boolean True/False',
    'owner = False    # True = only regular owners can use it',
    'rowner = False   # True = only the ROwner (root owner) can use it',
    'group = False    # True = can only be used in a group',
    'private = False  # True = can only be used in a private chat',
    'admin = False    # True = only group admins can use it',
    'registered = False  # True = the user MUST be registered first',
    'level = 0        # minimum user level (from db.data.users[jid].level)',
    '```',
    '',
    '*Advanced patterns:*',
    '- `async def before(m, *, conn, isOwner)` — runs BEFORE all other plugins are checked, used for staged/stateful flows (e.g. a download plugin that waits for the user to pick a quality number after the link is sent — keep the state in a module-level dict keyed by `m.sender` with timeout cleanup).',
    '- `async def after(m, *, conn, command)` — runs AFTER the main handler finishes (success or error), used for cleanup.',
    '- A plugin that raises a plain message (`raise Exception("error message")`) is valid — the dispatcher catches it and shows it to the user as a message.',
    '- Declare only the context values a function reads, as keyword-only parameters (conn, command, args, text, usedPrefix, noPrefix, match, user, bot, participants, groupMetadata, isOwner, isROwner, isAdmin, isRAdmin, isBotAdmin, isPrems). The framework passes only those.',
    '- Use `import` statements of the Python standard library (no pip packages unless they are already installed), like the whole codebase.',
]


async def _exec_read_plugin_guide(args=None, **kw):
    guides = ['PLUGIN_GUIDE.md', 'PLUGIN_SHORTHAND.md', 'docs/plugin-guide.md', 'README.md']
    for g in guides:
        abs_path = os.path.join(ROOT, g)
        if os.path.exists(abs_path):
            with open(abs_path, 'r', encoding='utf-8') as f:
                content = f.read()[:6000]
            return f'*{g}*\n\n{content}'
    return '\n'.join(PLUGIN_GUIDE_FALLBACK)


RUN_EVAL_DESCRIPTION = (
    "Run custom Python code through the bot's eval plugin (equivalent to manually typing \"<< code\" in the chat). "
    'Can ONLY be used by the REAL OWNER of the bot (rowner) -- non-owners are always rejected. There is NO extra '
    'confirmation before execution (unlike other medium/none risk commands) -- once called, the code is EXECUTED '
    'IMMEDIATELY with full access to the bot runtime (conn, db, m, etc). Use it ONLY when the owner explicitly asks to '
    'run specific code/eval/script — NEVER call this on your own initiative without an explicit instruction from the '
    'owner in that same turn.'
)

RUN_PLUGIN_DESCRIPTION = '\n'.join([
    "Run one of the bot's existing FEATURES. This is equivalent to the user typing \".feature_name\" in the chat.",
    '',
    'IMPORTANT: the risk level system below is a restriction for the AI AGENT (you), NOT a restriction for human users -- users may always still run any command manually by typing it directly in the chat (".command"), regardless of its risk level. What is limited here is purely which commands YOU may run automatically on behalf of the user through this tool.',
    '',
    "Plugins WITHOUT handler.ai can never be run through this tool (they are treated as system/internal-only). For plugins that have handler.ai, the risk level is TRUSTED DIRECTLY from the plugin's own handler.ai.risk + handler.ai.description declaration (check first with check_plugin_risk if in doubt, or look at the badge in list_plugins) — except for the most sensitive system commands (rowner-only, exec/session/secret) which stay hard BANNED whatever the plugin declares. NOTE: handler.ai.summarize decides WHETHER the plugin result is held and you recompose it into a natural answer (true), or the plugin sends its reply directly to the user itself and you just stay quiet (false/default) — see the instruction inside the tool result after run_plugin is called, and follow it:",
    '  banned -> system/dangerous/rowner-only (hard block, the plugin cannot change it), OR the plugin simply has no handler.ai. The AI Agent is STRICTLY FORBIDDEN from running this through this tool, whoever the requester is including the owner -- this is different from forbidding the user, the user may still type it manually. This tool will REFUSE by itself, do not force it.',
    '  high -> declared by the plugin as an owner-only/mass/destructive action. Two gates at once: (1) it only runs if the sender is the owner, (2) AFTER that this tool STILL ASKS FOR CONFIRMATION first (returns the error "CONFIRM_REQUIRED") -- ask the owner explicitly first, then call again with confirmed: true if the owner agrees. Different from banned: high CAN STILL run as long as it is the owner + confirmed, banned can NOT run at all under any condition.',
    '  medium -> declared by the plugin as a small/reversible state change, any user may use it. This tool will ASK FOR CONFIRMATION first (returns the error "CONFIRM_REQUIRED") — when that happens, ASK the user whether they are sure, and ONLY if the user has explicitly agreed, call run_plugin again with confirmed: true.',
    '  low -> declared by the plugin as safe & idempotent (sticker, ping, downloader, etc) — examples: "sticker"/"s" ONLY converts the replied/attached image/video into a sticker, "tiktok"/"ig" ONLY download media from public URLs. Run it immediately without hesitation, without asking anything.',
    "  unclassified (none) -> the plugin has handler.ai but has NOT declared a valid risk. DO NOT just ask the user whether it is allowed like medium. This tool will return the error \"UNCLASSIFIED\" containing the plugin's file path -- if the current requester is the owner, read that plugin's source code (read_file), decide for yourself the most fitting risk level (banned/high/medium/low) from how the code works, then write it back to that file (write_file) filling in the risk+description fields in handler.ai (do not change other parts), and only then call run_plugin again with the same command. If the requester is not the owner, do NOT edit any file -- tell the user this command has not been verified and the owner needs to configure it first.",
    '',
    "Besides risk, there are separate context requirements from the plugin's other flags (handler.group, handler.private, handler.premium, handler.admin, handler.botAdmin) which are ALSO checked automatically and can make this tool refuse even if the risk is low: group-only commands are rejected when called from a DM (and the reverse for DM-only), premium-only commands are rejected if the sender is not premium/owner, admin-only commands are rejected if the sender is not an admin of this group, commands that need the bot to be admin are rejected if the bot is not an admin in that group.",
    '',
    'The "menu" command is confirmed safe for ALL users — run it immediately without asking anything first, following the MENU rule in the system prompt. Similar commands that are not verified (e.g. "help", "allmenu", "list") might show owner-only commands to regular users depending on the plugin implementation, so this tool specifically holds those commands back for non-owners.',
    '',
    'IMPORTANT ABOUT MULTI-STEP PLUGINS (e.g. the "twitter"/"x" downloader): some plugins (usually downloaders with many quality/format options) work in 2 stages -- run_plugin only runs the FIRST STAGE (sending the link, the plugin replies with a numbered list of choices). The SECOND STAGE (the user typing the chosen number) is HANDLED THROUGH A SEPARATE MECHANISM (handler.before) which CANNOT be triggered again through run_plugin -- run_plugin ONLY knows how to run a command from the beginning again, not how to "continue" an existing state.',
    '',
    'FOR THIS TYPE OF DOWNLOADER PLUGIN, DO NOT USE run_plugin AT ALL -- use the download_media tool instead (the "twitter" platform in download_media already runs the scraper directly, without the numbered flow). If the platform the user asked for is unavailable in download_media either, do NOT try to work around it with run_plugin for those multi-stage downloader plugins (it will only get stuck at the first stage) -- tell the user that platform cannot be auto-downloaded through the AI yet.',
])

RUN_PLUGIN_COMMAND_DESCRIPTION = (
    'The command/plugin name EXACTLY as registered (check list_plugins/check_plugin_risk if in doubt), without prefix, '
    "and do NOT translate it from the user's natural-language intent. Example: the user asks for \"ping\" → command: \"ping\". "
    'IMPORTANT for plugins whose format is "plugin_name <argument>" (see handler.help in list_plugins, e.g. "simulate <event> [@mention]"): '
    'the command STAYS the plugin name ("simulate"), the arguments after it ("bye", "promote", etc) go into the args parameter, '
    'NOT made into a command of their own. Wrong example: the user says "try simulate bye" and command:"bye" is called — this is WRONG '
    'because "bye" is not a plugin name, it is the event argument for the "simulate" plugin. Correct example: command:"simulate", args:"bye".'
)


default = [
    {
        'name': 'run_eval',
        'description': RUN_EVAL_DESCRIPTION,
        'parameters': {
            'code': {
                'type': 'string',
                'description': 'The Python code to execute, EXACTLY as the owner meant it (do not change/"fix" it yourself unless asked). Example: \'await m.reply("hello world")\'',
                'required': True,
            },
            'silent': {
                'type': 'boolean',
                'description': 'true = silent execution (equivalent to the prefix "< ", no return value reply is shown). false/default = show the return value (equivalent to the prefix "<< ").',
                'required': False,
            },
        },
        'execute': _exec_run_eval,
    },
    {
        'name': 'list_plugins',
        'description': (
            'The ONLY source of truth about which commands/plugins really exist in this bot AND are exposed to the AI (plugins '
            'without handler.ai will not appear here at all, because they are treated as system/internal-only) — its data source '
            "is exactly the same as the bot's built-in \".menu\" command (plugin.help + plugin.tags), not a guess/memory from file "
            'names or another bot. MUST be called every time the user asks which commands/features/plugins are available — NEVER '
            'answer from memory/guesses because this bot does NOT HAVE generic commands like get_random_x or an AI image generation '
            'feature unless they really appear in this tool result. Also use it before run_plugin to learn the correct command name. '
            'Each command is marked with a risk badge for the AI Agent (banned, high, medium, low, unclassified — see the full '
            'explanation in the run_plugin description; this limits the AI Agent, not the user) so you immediately know which ones '
            'may be run freely and which need the owner/confirmation first. Existing categories: main, group, sticker, ai, internet, '
            'adult, tools, downloader, owner, info.'
        ),
        'parameters': {
            'category': {
                'type': 'string',
                'description': 'Category/tag filter (optional). Example: "main", "group", "downloader", "owner"',
                'required': False,
            },
        },
        'execute': _exec_list_plugins,
    },
    {
        'name': 'run_plugin',
        'description': RUN_PLUGIN_DESCRIPTION,
        'parameters': {
            'command': {
                'type': 'string',
                'description': RUN_PLUGIN_COMMAND_DESCRIPTION,
                'required': True,
            },
            'args': {
                'type': 'string',
                'description': 'Extra arguments for the command (optional)',
                'required': False,
            },
            'confirmed': {
                'type': 'boolean',
                'description': "Set true ONLY after the user has explicitly agreed to run a command that previously asked for confirmation (CONFIRM_REQUIRED). Never set true beforehand without the user's approval.",
                'required': False,
            },
        },
        'execute': _exec_run_plugin,
    },
    {
        'name': 'check_plugin_risk',
        'description': (
            'Check the risk level for the AI Agent (banned / high / medium / low / unclassified) of a command BEFORE running it '
            'through run_plugin — use this if you are unsure whether a command is safe to run automatically or needs '
            'confirmation/the owner first. This level limits the AI Agent, NOT the user (the user can still run any command manually '
            'in the chat). It does not run anything, it only checks.'
        ),
        'parameters': {
            'command': {
                'type': 'string',
                'description': 'The feature/command name to check, without prefix. Example: "broadcast", "ban", "sticker"',
                'required': True,
            },
        },
        'execute': _exec_check_plugin_risk,
    },
    {
        'name': 'read_plugin_guide',
        'description': 'Read the internal guide for creating a new plugin in this bot. Read this first before writing a new plugin.',
        'parameters': {},
        'execute': _exec_read_plugin_guide,
    },
]
