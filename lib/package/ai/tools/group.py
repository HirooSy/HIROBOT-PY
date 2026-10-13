import inspect
import re

from lib.package.ai import mcp
from lib.utils.handler import G


async def _await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _g(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _jid(target):
    text = str(target or '')
    return text if '@' in text else re.sub(r'[^0-9]', '', text) + '@s.whatsapp.net'


def _jsnum(value):
    if value is None:
        return float('nan')
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == '':
        return 0.0
    try:
        return float(text)
    except ValueError:
        return float('nan')


def _fmt_num(n):
    return str(int(n)) if float(n).is_integer() else str(n)


def _to_pn(p):
    raw = _g(p, 'phoneNumber') or _g(p, 'id') or ''
    return re.sub(r'@.*', '', str(raw))


async def _format_group(meta, with_members=False):
    c = mcp.ctx()
    participants = _g(meta, 'participants') or []
    admins = [_to_pn(p) for p in participants if _g(p, 'admin')]
    if _g(meta, 'participants') is not None:
        member_count = len(_g(meta, 'participants'))
    elif _g(meta, 'size') is not None:
        member_count = _g(meta, 'size')
    else:
        member_count = '?'
    allow_link = _g(mcp.read_group_settings(_g(meta, 'id')), 'allowMemberLink')
    lines = [
        _g(meta, 'subject') or '(no name)',
        f"- Id: {_g(meta, 'id')}",
        f'- Member: {member_count}',
        f"- Admin: {', '.join(admins) if admins else '-'}",
        f"- Description: {_g(meta, 'desc') or '-'}",
        f"- Regular members may request the group link: {'yes' if allow_link else 'no (admin/owner only)'}",
    ]
    if with_members and _g(meta, 'participants'):
        lines += ['', 'Member list:']
        for p in participants:
            jid = _g(p, 'phoneNumber') or _g(p, 'id')
            identity = None
            try:
                identity = await _await(mcp.get_user_identity(jid, G.db, c['conn']))
            except Exception:
                identity = None
            admin = _g(p, 'admin')
            role = ' [group owner]' if admin == 'superadmin' else (' [admin]' if admin == 'admin' else '')
            id_name = _g(identity, 'name')
            name_label = f' ({id_name})' if id_name and id_name != _g(identity, 'number') else ''
            lines.append(f"- {_g(identity, 'number') or _to_pn(p)}{name_label}{role}")
    return '\n'.join(lines)


def _group_target(a, c):
    explicit = a.get('group_jid')
    if explicit:
        return explicit
    cur = c['currentJid']
    return cur if cur and str(cur).endswith('@g.us') else None


async def _exec_get_group_info(args=None, **kw):
    a = _args(args, kw)
    invite_link = a.get('invite_link')
    list_all = a.get('list_all')
    include_members = a.get('include_members')
    c = mcp.ctx()
    if c['conn'] is None:
        return 'WA connection not ready'
    try:
        if invite_link:
            code = str(invite_link).split('chat.whatsapp.com/')[-1].split('?')[0].strip()
            meta = await c['conn'].groupGetInviteInfo(code)
            return await _format_group(meta)

        target_jid = a.get('group_jid') or (
            c['currentJid'] if not list_all and str(c['currentJid'] or '').endswith('@g.us') else None
        )
        if target_jid:
            meta = await c['conn'].groupMetadata(target_jid)
            return await _format_group(meta, with_members=bool(include_members))

        if not list_all and c['currentJid'] and not str(c['currentJid']).endswith('@g.us'):
            return 'This chat is not a group, so there is no group info for "this group". Give the JID or invite link of the group you mean if you want to see another group.'

        from lib.utils import connection
        store = getattr(connection, 'store', None)
        if store is None:
            return 'Store is not available'
        chats_map = _g(store, 'chats') or {}
        chats = [jid for jid in chats_map.keys() if str(jid).endswith('@g.us')]
        if not chats:
            return 'No groups'
        return f'Bot groups ({len(chats)}):\n' + '\n'.join(f'- {j}' for j in chats[:30])
    except Exception as e:
        return f'Error: {e}'


async def _exec_group_member_action(args=None, **kw):
    a = _args(args, kw)
    action = a.get('action')
    targets = a.get('targets')
    c = mcp.ctx()
    if c['conn'] is None:
        return 'WA connection not ready'
    group_jid = _group_target(a, c)
    if not group_jid:
        return 'No group specified -- this is not a group chat and group_jid was not filled in.'
    if not isinstance(targets, list) or not targets:
        return 'targets is required, at least 1.'

    action_map = {'add': 'add', 'kick': 'remove', 'remove': 'remove', 'promote': 'promote', 'demote': 'demote'}
    wa_action = action_map.get(str(action).lower())
    if not wa_action:
        return f'Action "{action}" is not recognized. Use one of: add, kick, promote, demote.'

    perm = mcp.check_group_admin_or_owner(group_jid)
    perm = await _await(perm)
    if not _g(perm, 'allowed'):
        return f"REJECTED: {_g(perm, 'reason')}"

    jids = [_jid(t) for t in targets]
    try:
        result = await c['conn'].groupParticipantsUpdate(group_jid, jids, wa_action)
        summary = '\n'.join(
            f"{_g(r, 'jid')}: {'success' if _g(r, 'status') == '200' else 'failed (' + str(_g(r, 'status')) + ')'}"
            for r in (result or [])
        )
        return f'Action "{wa_action}" finished:\n{summary or "(no result from WA)"}'
    except Exception as e:
        return f'Failed: {e}'


async def _exec_group_settings(args=None, **kw):
    a = _args(args, kw)
    action = a.get('action')
    value = a.get('value')
    c = mcp.ctx()
    if c['conn'] is None:
        return 'WA connection not ready'
    group_jid = _group_target(a, c)
    if not group_jid:
        return 'No group specified -- this is not a group chat and group_jid was not filled in.'

    perm = await _await(mcp.check_group_admin_or_owner(group_jid))
    if not _g(perm, 'allowed'):
        return f"REJECTED: {_g(perm, 'reason')}"

    conn = c['conn']
    try:
        if action == 'set_name':
            if not value:
                return 'value (the new group name) is required.'
            await conn.groupUpdateSubject(group_jid, value)
            return f'Group name changed to "{value}".'
        if action == 'set_description':
            if value is None:
                return 'value (the new description) is required.'
            await conn.groupUpdateDescription(group_jid, value)
            return 'Group description updated.'
        if action == 'set_photo':
            if not value:
                return 'value (image URL) is required.'
            await conn.updateProfilePicture(group_jid, {'url': value})
            return 'Group photo updated.'
        if action == 'remove_photo':
            await conn.removeProfilePicture(group_jid)
            return 'Group photo removed.'
        if action == 'announcement_on':
            await conn.groupSettingUpdate(group_jid, 'announcement')
            return 'The group is now set so only admins can send messages.'
        if action == 'announcement_off':
            await conn.groupSettingUpdate(group_jid, 'not_announcement')
            return 'The group is now set so all members can send messages.'
        if action == 'lock_info':
            await conn.groupSettingUpdate(group_jid, 'locked')
            return 'Only admins can now edit the group info (name/description/photo).'
        if action == 'unlock_info':
            await conn.groupSettingUpdate(group_jid, 'unlocked')
            return 'All members can now edit the group info (name/description/photo).'
        if action == 'member_add_admin_only':
            await conn.groupMemberAddMode(group_jid, 'admin_add')
            return 'Only admins can now add new members.'
        if action == 'member_add_all':
            await conn.groupMemberAddMode(group_jid, 'all_member_add')
            return 'All members can now add new members.'
        if action == 'ephemeral':
            seconds = _jsnum(value)
            if seconds != seconds or seconds in (float('inf'), float('-inf')) or seconds < 0:
                return 'value (number of seconds) must be a number >= 0.'
            await conn.groupToggleEphemeral(group_jid, seconds)
            if seconds == 0:
                return 'Pesan sementara dimatikan.'
            return f'Pesan sementara diset {_fmt_num(seconds)} detik.'
        if action == 'join_approval_on':
            await conn.groupJoinApprovalMode(group_jid, 'on')
            return 'Join approval mode is on -- new members must be approved first.'
        if action == 'join_approval_off':
            await conn.groupJoinApprovalMode(group_jid, 'off')
            return 'Join approval mode is off -- people can join directly through the link.'
        if action == 'allow_member_link':
            brain = mcp.load_brain()
            mcp.ensure_brain_group_slot(brain, group_jid)['settings']['allowMemberLink'] = True
            mcp.save_brain(brain)
            return "Regular members may now request this group's invite link through the bot."
        if action == 'disallow_member_link':
            brain = mcp.load_brain()
            mcp.ensure_brain_group_slot(brain, group_jid)['settings']['allowMemberLink'] = False
            mcp.save_brain(brain)
            return "Only admins/owner may now request this group's invite link through the bot."
        return f'Action "{action}" is not recognized.'
    except Exception as e:
        return f'Failed: {e}'


async def _exec_group_link(args=None, **kw):
    a = _args(args, kw)
    action = a.get('action')
    c = mcp.ctx()
    if c['conn'] is None:
        return 'WA connection not ready'
    group_jid = _group_target(a, c)
    if not group_jid:
        return 'No group specified -- this is not a group chat and group_jid was not filled in.'

    allowed_for_member = _g(mcp.read_group_settings(group_jid), 'allowMemberLink') is True
    if not allowed_for_member:
        perm = await _await(mcp.check_group_admin_or_owner(group_jid))
        if not _g(perm, 'allowed'):
            return f"REJECTED: {_g(perm, 'reason')} (an admin has not allowed regular members to request this group link)"

    try:
        if action == 'revoke':
            perm = await _await(mcp.check_group_admin_or_owner(group_jid))
            if not _g(perm, 'allowed'):
                if not allowed_for_member:
                    return f"REJECTED: {_g(perm, 'reason')}"
                return 'REJECTED: revoking the link is for admins/owner only, even though regular members may view the link.'
            code = await c['conn'].groupRevokeInvite(group_jid)
            return f'Old link reset. New link: https://chat.whatsapp.com/{code}'
        code = await c['conn'].groupInviteCode(group_jid)
        return f'https://chat.whatsapp.com/{code}'
    except Exception as e:
        return f'Failed: {e}'


async def _exec_group_leave(args=None, **kw):
    a = _args(args, kw)
    c = mcp.ctx()
    if c['conn'] is None:
        return 'WA connection not ready'
    group_jid = _group_target(a, c)
    if not group_jid:
        return 'No group specified -- this is not a group chat and group_jid was not filled in.'

    perm = await _await(mcp.check_group_admin_or_owner(group_jid))
    if not _g(perm, 'allowed'):
        return f"REJECTED: {_g(perm, 'reason')}"

    try:
        if c['conn'] is not None and group_jid:
            await c['conn'].sendMessage(group_jid, {'text': 'Baik, bot keluar dari grup ini ya. Bye! 👋'})
        await c['conn'].groupLeave(group_jid)
        return f'Successfully left the group {group_jid}.'
    except Exception as e:
        return f'Failed: {e}'


async def _exec_group_join_requests(args=None, **kw):
    a = _args(args, kw)
    action = a.get('action')
    targets = a.get('targets')
    c = mcp.ctx()
    if c['conn'] is None:
        return 'WA connection not ready'
    group_jid = _group_target(a, c)
    if not group_jid:
        return 'No group specified -- this is not a group chat and group_jid was not filled in.'

    perm = await _await(mcp.check_group_admin_or_owner(group_jid))
    if not _g(perm, 'allowed'):
        return f"REJECTED: {_g(perm, 'reason')}"

    try:
        if action == 'list':
            requests = await c['conn'].groupRequestParticipantsList(group_jid)
            if not requests:
                return 'There are no pending join requests.'
            return '\n'.join(f"- {_g(r, 'jid')}" for r in requests)
        if action != 'approve' and action != 'reject':
            return f'Action "{action}" is not recognized. Use: list, approve, reject.'
        if not isinstance(targets, list) or not targets:
            return 'targets is required for approve/reject.'
        jids = [_jid(t) for t in targets]
        await c['conn'].groupRequestParticipantsUpdate(group_jid, jids, action)
        verb = 'approved' if action == 'approve' else 'rejected'
        return f'Successfully {verb} {len(jids)} join requests.'
    except Exception as e:
        return f'Failed: {e}'


default = [
    {
        'name': 'get_group_info',
        'description': (
            'Get group information: name, description, member count, admin list, and (optionally) the list '
            'of ALL members with names recognized from the bot database. If the user says "info about this '
            'group"/"this group" WITHOUT giving a specific JID or link, do NOT fill group_jid/invite_link — '
            'leave them empty, this tool automatically uses the currently active chat group. Fill '
            'invite_link if the user gives a group invite link (chat.whatsapp.com/...) for a group the bot '
            'has NOT joined yet. Set include_members=true if the user asks to see ALL group members (not '
            'just admins), or if you need to know who is in this group to answer another question.'
        ),
        'parameters': {
            'group_jid': {
                'type': 'string',
                'description': (
                    'Group JID (example: 120363...@g.us). Leave empty to use the currently active chat group. Also '
                    'leave it empty (with nothing filled in) to list all groups the bot is in.'
                ),
                'required': False,
            },
            'invite_link': {
                'type': 'string',
                'description': (
                    'Group invite link (e.g. "https://chat.whatsapp.com/ABC123..." or just the code "ABC123...") — '
                    'used to see info of a group the bot has not joined.'
                ),
                'required': False,
            },
            'list_all': {
                'type': 'boolean',
                'description': (
                    'Set true to explicitly ask for the list of ALL groups the bot is in, instead of the active chat '
                    'group.'
                ),
                'required': False,
            },
            'include_members': {
                'type': 'boolean',
                'description': (
                    'Set true to include the list of ALL group members (not just admins), with names from the bot '
                    'database if recognized. Does not apply to invite_link (a group not joined yet).'
                ),
                'required': False,
            },
        },
        'execute': _exec_get_group_info,
    },
    {
        'name': 'group_member_action',
        'description': (
            'Add, kick, promote (make admin), or demote (remove admin status from) group members. ONLY an '
            'ADMIN of this group or the bot OWNER may request this -- if the requester is not an '
            'admin/owner, this tool refuses automatically. The bot itself must also be an admin in that '
            "group for this action to be executed by WhatsApp (outside this tool's control)."
        ),
        'parameters': {
            'action': {
                'type': 'string',
                'description': 'One of: "add", "kick" (alias "remove"), "promote", "demote".',
                'required': True,
            },
            'targets': {
                'type': 'array',
                'items': {
                    'type': 'string',
                },
                'description': (
                    'List of phone numbers or JIDs targeted by the action. Example: ["628123456789", '
                    '"628987654321@s.whatsapp.net"].'
                ),
                'required': True,
            },
            'group_jid': {
                'type': 'string',
                'description': 'Group JID. Leave empty to use the currently active chat group.',
                'required': False,
            },
        },
        'execute': _exec_group_member_action,
    },
    {
        'name': 'group_settings',
        'description': (
            'Change group settings: name, description, photo, chat mode (announcement/everyone can chat), '
            'group info lock (admin only/everyone can edit info), member-add mode (admin only/everyone), '
            'ephemeral messages, join approval mode, and permission for regular members to request the '
            'invite link. ONLY an ADMIN of this group or the bot OWNER may request this.'
        ),
        'parameters': {
            'action': {
                'type': 'string',
                'description': (
                    'One of: "set_name", "set_description", "set_photo", "remove_photo", "announcement_on" (only '
                    'admins can chat), "announcement_off" (everyone can chat), "lock_info" (only admins edit group '
                    'info), "unlock_info" (everyone can edit group info), "member_add_admin_only", "member_add_all", '
                    '"ephemeral" (needs value = seconds, 0 to turn off), "join_approval_on", "join_approval_off", '
                    '"allow_member_link" (regular members may request the invite link), "disallow_member_link" (only '
                    'admin/owner may).'
                ),
                'required': True,
            },
            'value': {
                'type': 'string',
                'description': (
                    'The value for actions that need one (e.g. the new group name for set_name, description text for '
                    'set_description, image URL for set_photo, number of seconds for ephemeral -- 0 to turn off, '
                    '86400 = 24 hours).'
                ),
                'required': False,
            },
            'group_jid': {
                'type': 'string',
                'description': 'Group JID. Leave empty to use the currently active chat group.',
                'required': False,
            },
        },
        'execute': _exec_group_settings,
    },
    {
        'name': 'group_link',
        'description': (
            'Get or reset (revoke) the group invite link. Group admins/bot owner are ALWAYS allowed. Regular '
            'members are only allowed if an admin has permitted it through group_settings (action '
            'allow_member_link) -- if it has not been permitted and the requester is not an admin/owner, '
            'this tool refuses automatically.'
        ),
        'parameters': {
            'action': {
                'type': 'string',
                'description': '"get" to fetch the current link, "revoke" to reset the link (the old link becomes invalid).',
                'required': True,
            },
            'group_jid': {
                'type': 'string',
                'description': 'Group JID. Leave empty to use the currently active chat group.',
                'required': False,
            },
        },
        'execute': _exec_group_link,
    },
    {
        'name': 'group_leave',
        'description': 'The bot leaves the group. ONLY an ADMIN of this group or the bot OWNER may request this.',
        'parameters': {
            'group_jid': {
                'type': 'string',
                'description': 'Group JID. Leave empty to use the currently active chat group.',
                'required': False,
            },
        },
        'execute': _exec_group_leave,
    },
    {
        'name': 'group_join_requests',
        'description': (
            'View, approve, or reject the list of people requesting to join the group (when join approval '
            'mode is on). ONLY an ADMIN of this group or the bot OWNER may request this.'
        ),
        'parameters': {
            'action': {
                'type': 'string',
                'description': '"list" to see the pending list, "approve" or "reject" to process specific targets.',
                'required': True,
            },
            'targets': {
                'type': 'array',
                'items': {
                    'type': 'string',
                },
                'description': 'List of numbers/JIDs to approve/reject. Required when action is not "list".',
                'required': False,
            },
            'group_jid': {
                'type': 'string',
                'description': 'Group JID. Leave empty to use the currently active chat group.',
                'required': False,
            },
        },
        'execute': _exec_group_join_requests,
    },
]
