import base64

from baileys_lite.WAProto import proto
from baileys_lite.binary.wa_binary import (
    getBinaryNodeChild,
    getBinaryNodeChildren,
    getBinaryNodeChildString,
    isLidUser,
    isPnUser,
    isTruthy,
    jidEncode,
    jidNormalizedUser,
    jsNumber,
    S_WHATSAPP_NET
)
from baileys_lite.constants import QueryIds, WAMessageAddressingMode, WAMessageStubType, XWAPaths
from baileys_lite.foundation.boom import Boom
from baileys_lite.foundation.long import jsParseInt
from baileys_lite.foundation.node_runtime import JSObject
from baileys_lite.socket.chats import makeChatsSocket
from baileys_lite.socket.socket_core import asyncListener, executeWMexQuery as genericExecuteWMexQuery, getp, orValue, spreadSocket
from baileys_lite.utils.media import generateProfilePicture
from baileys_lite.utils.message_compose import jsonStringify
from baileys_lite.utils.wa_protocol_core import generateMessageIDV2, unixTimestampSeconds


def childAttr(node, tag, key):
    child = getBinaryNodeChild(node, tag)
    return getp(getp(child, 'attrs'), key)


def extractGroupMetadata(result):
    group = getBinaryNodeChild(result, 'group')
    if group is None:
        errorNode = getBinaryNodeChild(result, 'error')
        if errorNode is not None:
            code = jsNumber(errorNode.get('attrs').get('code')) if isTruthy(errorNode.get('attrs').get('code')) else 500
            text = orValue(errorNode.get('attrs').get('text'), 'group metadata query failed')
            raise Boom(text, JSObject({'statusCode': code, 'data': errorNode}))
        raise Boom('Invalid group metadata response: missing <group> node', JSObject({'data': result}))
    if not isTruthy(group.get('attrs').get('id')):
        raise Boom('Invalid group metadata response: missing group id', JSObject({'data': group}))
    descChild = getBinaryNodeChild(group, 'description')
    desc = None
    descId = None
    descOwner = None
    descOwnerPn = None
    descOwnerUsername = None
    descTime = None
    if descChild is not None:
        desc = getBinaryNodeChildString(descChild, 'body')
        descOwner = jidNormalizedUser(descChild.get('attrs').get('participant')) if isTruthy(descChild.get('attrs').get('participant')) else None
        descOwnerPn = jidNormalizedUser(descChild.get('attrs').get('participant_pn')) if isTruthy(descChild.get('attrs').get('participant_pn')) else None
        descOwnerUsername = orValue(descChild.get('attrs').get('participant_username'), None)
        descTime = jsNumber(descChild.get('attrs').get('t'))
        descId = descChild.get('attrs').get('id')
    groupAttrs = group.get('attrs')
    groupId = groupAttrs.get('id') if '@' in groupAttrs.get('id') else jidEncode(groupAttrs.get('id'), 'g.us')
    eph = childAttr(group, 'ephemeral', 'expiration')
    memberAddMode = getBinaryNodeChildString(group, 'member_add_mode') == 'all_member_add'
    sizeAttr = groupAttrs.get('size')
    creator = groupAttrs.get('creator')
    linkedParent = childAttr(group, 'linked_parent', 'jid')
    return JSObject({
        'id': groupId,
        'notify': groupAttrs.get('notify'),
        'addressingMode': WAMessageAddressingMode['LID'] if groupAttrs.get('addressing_mode') == 'lid' else WAMessageAddressingMode['PN'],
        'subject': groupAttrs.get('subject'),
        'subjectOwner': groupAttrs.get('s_o'),
        'subjectOwnerPn': groupAttrs.get('s_o_pn'),
        'subjectOwnerUsername': groupAttrs.get('s_o_username'),
        'subjectTime': jsNumber(groupAttrs.get('s_t')),
        'size': jsNumber(sizeAttr) if isTruthy(sizeAttr) else len(getBinaryNodeChildren(group, 'participant')),
        'creation': jsNumber(groupAttrs.get('creation')),
        'owner': jidNormalizedUser(creator) if isTruthy(creator) else None,
        'ownerPn': jidNormalizedUser(groupAttrs.get('creator_pn')) if isTruthy(groupAttrs.get('creator_pn')) else None,
        'ownerUsername': orValue(groupAttrs.get('creator_username'), None),
        'owner_country_code': groupAttrs.get('creator_country_code'),
        'desc': desc,
        'descId': descId,
        'descOwner': descOwner,
        'descOwnerPn': descOwnerPn,
        'descOwnerUsername': descOwnerUsername,
        'descTime': descTime,
        'linkedParent': orValue(linkedParent, None),
        'restrict': getBinaryNodeChild(group, 'locked') is not None,
        'announce': getBinaryNodeChild(group, 'announcement') is not None,
        'isCommunity': getBinaryNodeChild(group, 'parent') is not None,
        'isCommunityAnnounce': getBinaryNodeChild(group, 'default_sub_group') is not None,
        'joinApprovalMode': getBinaryNodeChild(group, 'membership_approval_mode') is not None,
        'memberAddMode': memberAddMode,
        'participants': [JSObject({
            'id': participant.get('attrs').get('jid'),
            'phoneNumber': participant.get('attrs').get('phone_number') if isLidUser(participant.get('attrs').get('jid')) and isPnUser(participant.get('attrs').get('phone_number')) else None,
            'lid': participant.get('attrs').get('lid') if isPnUser(participant.get('attrs').get('jid')) and isLidUser(participant.get('attrs').get('lid')) else None,
            'username': orValue(participant.get('attrs').get('participant_username'), orValue(participant.get('attrs').get('username'), None)),
            'admin': orValue(participant.get('attrs').get('type'), None)
        }) for participant in getBinaryNodeChildren(group, 'participant')],
        'ephemeralDuration': jsNumber(eph) if isTruthy(eph) else None
    })


def makeGroupsSocket(config):
    sock = makeChatsSocket(config)
    authState = sock.get('authState')
    ev = sock.get('ev')
    query = sock.get('query')
    upsertMessage = sock.get('upsertMessage')
    ws = sock.get('ws')

    async def groupQuery(jid, type_, content):
        return await query(JSObject({'tag': 'iq', 'attrs': JSObject({'type': type_, 'xmlns': 'w:g2', 'to': jid}), 'content': content}))

    async def groupMetadata(jid):
        return extractGroupMetadata(await groupQuery(jid, 'get', [JSObject({'tag': 'query', 'attrs': JSObject({'request': 'interactive'})})]))

    async def groupFetchAllParticipating():
        result = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': '@g.us', 'xmlns': 'w:g2', 'type': 'get'}),
            'content': [JSObject({'tag': 'participating', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'participants', 'attrs': JSObject({})}), JSObject({'tag': 'description', 'attrs': JSObject({})})]})]
        }))
        data = JSObject({})
        groupsChild = getBinaryNodeChild(result, 'groups')
        if groupsChild is not None:
            for groupNode in getBinaryNodeChildren(groupsChild, 'group'):
                meta = extractGroupMetadata(JSObject({'tag': 'result', 'attrs': JSObject({}), 'content': [groupNode]}))
                data[meta.get('id')] = meta
        ev.get('emit')('groups.update', list(dict.values(data)))
        return data

    async def onGroupDirty(node):
        attrs = getBinaryNodeChild(node, 'dirty')['attrs']
        if attrs.get('type') != 'groups':
            return
        await groupFetchAllParticipating()
        await sock.get('cleanDirtyBits')('groups')
    ws.on('CB:ib,,dirty', asyncListener(onGroupDirty))

    async def groupCreate(subject, participants):
        key = generateMessageIDV2(None)
        result = await groupQuery('@g.us', 'set', [
            JSObject({'tag': 'create', 'attrs': JSObject({'subject': subject, 'key': key}), 'content': [JSObject({'tag': 'participant', 'attrs': JSObject({'jid': jid})}) for jid in participants]})
        ])
        return extractGroupMetadata(result)

    async def groupLeave(id_):
        await groupQuery('@g.us', 'set', [JSObject({'tag': 'leave', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'group', 'attrs': JSObject({'id': id_})})]})])

    async def groupUpdateSubject(jid, subject):
        await groupQuery(jid, 'set', [JSObject({'tag': 'subject', 'attrs': JSObject({}), 'content': subject.encode('utf-8')})])

    async def groupRequestParticipantsList(jid):
        result = await groupQuery(jid, 'get', [JSObject({'tag': 'membership_approval_requests', 'attrs': JSObject({})})])
        node = getBinaryNodeChild(result, 'membership_approval_requests')
        return [v.get('attrs') for v in getBinaryNodeChildren(node, 'membership_approval_request')]

    async def groupRequestParticipantsUpdate(jid, participants, action):
        result = await groupQuery(jid, 'set', [
            JSObject({'tag': 'membership_requests_action', 'attrs': JSObject({}), 'content': [JSObject({'tag': action, 'attrs': JSObject({}), 'content': [JSObject({'tag': 'participant', 'attrs': JSObject({'jid': p})}) for p in participants]})]})
        ])
        node = getBinaryNodeChild(result, 'membership_requests_action')
        nodeAction = getBinaryNodeChild(node, action)
        return [JSObject({'status': orValue(p.get('attrs').get('error'), '200'), 'jid': p.get('attrs').get('jid')}) for p in getBinaryNodeChildren(nodeAction, 'participant')]

    async def groupParticipantsUpdate(jid, participants, action):
        result = await groupQuery(jid, 'set', [JSObject({'tag': action, 'attrs': JSObject({}), 'content': [JSObject({'tag': 'participant', 'attrs': JSObject({'jid': p})}) for p in participants]})])
        node = getBinaryNodeChild(result, action)
        return [JSObject({'status': orValue(p.get('attrs').get('error'), '200'), 'jid': p.get('attrs').get('jid'), 'content': p}) for p in getBinaryNodeChildren(node, 'participant')]

    async def groupUpdateDescription(jid, description):
        metadata = await groupMetadata(jid)
        prev = metadata.get('descId')
        attrs = JSObject({'id': generateMessageIDV2(None)} if isTruthy(description) else {'delete': 'true'})
        if isTruthy(prev):
            attrs['prev'] = prev
        await groupQuery(jid, 'set', [
            JSObject({
                'tag': 'description',
                'attrs': attrs,
                'content': [JSObject({'tag': 'body', 'attrs': JSObject({}), 'content': description.encode('utf-8')})] if isTruthy(description) else None
            })
        ])

    async def groupInviteCode(jid):
        result = await groupQuery(jid, 'get', [JSObject({'tag': 'invite', 'attrs': JSObject({})})])
        return childAttr(result, 'invite', 'code')

    async def groupRevokeInvite(jid):
        result = await groupQuery(jid, 'set', [JSObject({'tag': 'invite', 'attrs': JSObject({})})])
        return childAttr(result, 'invite', 'code')

    async def groupAcceptInvite(code):
        results = await groupQuery('@g.us', 'set', [JSObject({'tag': 'invite', 'attrs': JSObject({'code': code})})])
        return childAttr(results, 'group', 'jid')

    async def groupRevokeInviteV4(groupJid, invitedJid):
        result = await groupQuery(groupJid, 'set', [JSObject({'tag': 'revoke', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'participant', 'attrs': JSObject({'jid': invitedJid})})]})])
        return isTruthy(result)

    async def acceptInviteV4Work(key, inviteMessage):
        if isinstance(key, str):
            key = JSObject({'remoteJid': key})
        results = await groupQuery(inviteMessage.get('groupJid'), 'set', [
            JSObject({'tag': 'accept', 'attrs': JSObject({'code': inviteMessage.get('inviteCode'), 'expiration': str(inviteMessage.get('inviteExpiration')), 'admin': key.get('remoteJid')})})
        ])
        if isTruthy(key.get('id')):
            inviteMessage = proto.Message.GroupInviteMessage.fromObject(inviteMessage)
            inviteMessage['inviteExpiration'] = 0
            inviteMessage['inviteCode'] = ''
            ev.get('emit')('messages.update', [JSObject({'key': key, 'update': JSObject({'message': JSObject({'groupInviteMessage': inviteMessage})})})])
        await upsertMessage(JSObject({
            'key': JSObject({'remoteJid': inviteMessage.get('groupJid'), 'id': generateMessageIDV2(getp(sock.get('user'), 'id')), 'fromMe': False, 'participant': key.get('remoteJid')}),
            'messageStubType': WAMessageStubType['GROUP_PARTICIPANT_ADD'],
            'messageStubParameters': [jsonStringify(authState.get('creds').get('me'))],
            'participant': key.get('remoteJid'),
            'messageTimestamp': unixTimestampSeconds()
        }), 'notify')
        return results.get('attrs').get('from')
    groupAcceptInviteV4 = ev.get('createBufferedFunction')(acceptInviteV4Work)

    async def groupGetInviteInfo(code):
        return extractGroupMetadata(await groupQuery('@g.us', 'get', [JSObject({'tag': 'invite', 'attrs': JSObject({'code': code})})]))

    async def groupToggleEphemeral(jid, ephemeralExpiration):
        if isTruthy(ephemeralExpiration):
            content = JSObject({'tag': 'ephemeral', 'attrs': JSObject({'expiration': str(ephemeralExpiration)})})
        else:
            content = JSObject({'tag': 'not_ephemeral', 'attrs': JSObject({})})
        await groupQuery(jid, 'set', [content])

    async def groupSettingUpdate(jid, setting):
        await groupQuery(jid, 'set', [JSObject({'tag': setting, 'attrs': JSObject({})})])

    async def groupMemberAddMode(jid, mode):
        await groupQuery(jid, 'set', [JSObject({'tag': 'member_add_mode', 'attrs': JSObject({}), 'content': mode})])

    async def groupJoinApprovalMode(jid, mode):
        await groupQuery(jid, 'set', [JSObject({'tag': 'membership_approval_mode', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'group_join', 'attrs': JSObject({'state': mode})})]})])

    return spreadSocket(sock, JSObject({
        'groupQuery': groupQuery,
        'groupMetadata': groupMetadata,
        'groupCreate': groupCreate,
        'groupLeave': groupLeave,
        'groupUpdateSubject': groupUpdateSubject,
        'groupRequestParticipantsList': groupRequestParticipantsList,
        'groupRequestParticipantsUpdate': groupRequestParticipantsUpdate,
        'groupParticipantsUpdate': groupParticipantsUpdate,
        'groupUpdateDescription': groupUpdateDescription,
        'groupInviteCode': groupInviteCode,
        'groupRevokeInvite': groupRevokeInvite,
        'groupAcceptInvite': groupAcceptInvite,
        'groupRevokeInviteV4': groupRevokeInviteV4,
        'groupAcceptInviteV4': groupAcceptInviteV4,
        'groupGetInviteInfo': groupGetInviteInfo,
        'groupToggleEphemeral': groupToggleEphemeral,
        'groupSettingUpdate': groupSettingUpdate,
        'groupMemberAddMode': groupMemberAddMode,
        'groupJoinApprovalMode': groupJoinApprovalMode,
        'groupFetchAllParticipating': groupFetchAllParticipating
    }))


def parseNewsletterCreateResponse(response):
    thread = response.get('thread_metadata')
    viewer = response.get('viewer_metadata')
    picture = thread.get('picture')
    return JSObject({
        'id': response.get('id'),
        'owner': None,
        'name': thread.get('name').get('text'),
        'creation_time': jsParseInt(str(thread.get('creation_time')), 10),
        'description': thread.get('description').get('text'),
        'invite': thread.get('invite'),
        'subscribers': jsParseInt(str(thread.get('subscribers_count')), 10),
        'verification': thread.get('verification'),
        'picture': JSObject({'id': getp(picture, 'id'), 'directPath': getp(picture, 'direct_path')}),
        'mute_state': viewer.get('mute')
    })


def parseNewsletterMetadata(result):
    if not isinstance(result, dict):
        return None
    if 'id' in result and isinstance(result.get('id'), str):
        return result
    if 'result' in result and isinstance(result.get('result'), dict) and 'id' in result.get('result'):
        return result.get('result')
    return None


def makeNewsletterSocket(config):
    sock = makeGroupsSocket(config)
    query = sock.get('query')
    generateMessageTag = sock.get('generateMessageTag')
    logger = sock.get('logger')

    async def executeWMexQuery(variables, queryId, dataPath):
        return await genericExecuteWMexQuery(variables, queryId, dataPath, query, generateMessageTag)

    async def newsletterUpdate(jid, updates):
        return await executeWMexQuery(JSObject({'newsletter_id': jid, 'updates': JSObject({**updates, 'settings': None})}), QueryIds['UPDATE_METADATA'], 'xwa2_newsletter_update')

    async def newsletterCreate(name, description=None):
        rawResponse = await executeWMexQuery(JSObject({'input': JSObject({'name': name, 'description': description})}), QueryIds['CREATE'], XWAPaths['xwa2_newsletter_create'])
        return parseNewsletterCreateResponse(rawResponse)

    async def newsletterFollowers(jid, count=100):
        return await executeWMexQuery(JSObject({'input': JSObject({'newsletter_id': jid, 'count': count})}), QueryIds['FOLLOWERS'], XWAPaths['xwa2_newsletter_followers'])

    async def newsletterSubscribers(jid, count=100):
        response = await executeWMexQuery(JSObject({'input': JSObject({'newsletter_id': jid, 'count': count})}), QueryIds['FOLLOWERS'], XWAPaths['xwa2_newsletter_followers'])
        followers = getp(response, 'followers')
        edges = getp(followers, 'edges')
        if edges is None:
            edges = []
        result = []
        for edge in edges:
            node = getp(edge, 'node')
            result.append(JSObject({
                'id': getp(node, 'id'),
                'phoneNumber': getp(node, 'pn'),
                'displayName': getp(node, 'display_name'),
                'username': getp(getp(node, 'username_info'), 'username'),
                'role': getp(edge, 'role'),
                'followTime': getp(edge, 'follow_time')
            }))
        return result

    async def newsletterInsights(jid, metrics=None):
        if metrics is None:
            metrics = ['NET_FOLLOWS', 'UNFOLLOWS']
        return await executeWMexQuery(JSObject({'input': JSObject({'newsletter_id': jid, 'metrics': metrics})}), QueryIds['INSIGHTS'], XWAPaths['xwa2_newsletter_admin_insights'])

    async def newsletterAdminInfo(jid):
        return await executeWMexQuery(JSObject({'newsletter_id': jid}), QueryIds['ADMIN_INFO'], XWAPaths['xwa2_newsletter_admin_count'])

    async def newsletterPendingAdminInvites(jid):
        response = await executeWMexQuery(JSObject({'newsletter_id': jid}), QueryIds['PENDING_ADMIN_INVITES'], XWAPaths['xwa2_newsletter_admin_count'])
        invites = getp(response, 'pending_admin_invites')
        if invites is None:
            invites = []
        return [JSObject({'id': getp(getp(invite, 'user'), 'id'), 'phoneNumber': getp(getp(invite, 'user'), 'pn')}) for invite in invites]

    async def newsletterSubscribed():
        return await executeWMexQuery(JSObject({}), QueryIds['SUBSCRIBED'], XWAPaths['xwa2_newsletter_subscribed'])

    async def newsletterMetadata(type_, key):
        variables = JSObject({'fetch_creation_time': True, 'fetch_full_image': True, 'fetch_viewer_metadata': True, 'input': JSObject({'key': key, 'type': type_.upper()})})
        result = await executeWMexQuery(variables, QueryIds['METADATA'], XWAPaths['xwa2_newsletter_metadata'])
        return parseNewsletterMetadata(result)

    async def newsletterFollow(jid):
        return await executeWMexQuery(JSObject({'newsletter_id': jid}), QueryIds['FOLLOW'], XWAPaths['xwa2_newsletter_join_v2'])

    async def newsletterUnfollow(jid):
        return await executeWMexQuery(JSObject({'newsletter_id': jid}), QueryIds['UNFOLLOW'], XWAPaths['xwa2_newsletter_leave_v2'])

    async def newsletterMute(jid):
        return await executeWMexQuery(JSObject({'newsletter_id': jid}), QueryIds['MUTE'], XWAPaths['xwa2_newsletter_mute_v2'])

    async def newsletterUnmute(jid):
        return await executeWMexQuery(JSObject({'newsletter_id': jid}), QueryIds['UNMUTE'], XWAPaths['xwa2_newsletter_unmute_v2'])

    async def newsletterUpdateName(jid, name):
        return await newsletterUpdate(jid, JSObject({'name': name}))

    async def newsletterUpdateDescription(jid, description):
        return await newsletterUpdate(jid, JSObject({'description': description}))

    async def newsletterUpdatePicture(jid, content):
        generated = await generateProfilePicture(content, None)
        return await newsletterUpdate(jid, JSObject({'picture': base64.b64encode(bytes(generated.get('img'))).decode()}))

    async def newsletterRemovePicture(jid):
        return await newsletterUpdate(jid, JSObject({'picture': ''}))

    async def newsletterReactMessage(jid, serverId, reaction):
        attrs = JSObject({'to': jid})
        if not isTruthy(reaction):
            attrs['edit'] = '7'
        attrs['type'] = 'reaction'
        attrs['server_id'] = serverId
        attrs['id'] = generateMessageTag()
        await query(JSObject({
            'tag': 'message',
            'attrs': attrs,
            'content': [JSObject({'tag': 'reaction', 'attrs': JSObject({'code': reaction}) if isTruthy(reaction) else JSObject({})})]
        }))

    async def newsletterFetchMessages(type_, key, count, after=None, before=None):
        messagesAttrs = JSObject({'count': str(count), 'type': type_})
        messagesAttrs['jid' if type_ == 'jid' else 'key'] = key
        if isTruthy(after):
            messagesAttrs['after'] = str(after)
        if isTruthy(before):
            messagesAttrs['before'] = str(before)
        result = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'id': generateMessageTag(), 'type': 'get', 'xmlns': 'newsletter', 'to': S_WHATSAPP_NET}),
            'content': [JSObject({'tag': 'messages', 'attrs': messagesAttrs})]
        }))
        messagesNode = getBinaryNodeChild(result, 'messages')
        if messagesNode is None:
            return []
        newsletterJid = orValue(messagesNode.get('attrs').get('jid'), key if type_ == 'jid' else None)
        messages = []
        for child in getBinaryNodeChildren(messagesNode, 'message'):
            plaintextNode = getBinaryNodeChild(child, 'plaintext')
            if plaintextNode is None or not isTruthy(plaintextNode.get('content')):
                continue
            try:
                content = plaintextNode.get('content')
                contentBuf = content.encode('latin-1') if isinstance(content, str) else bytes(content)
                messageProto = proto.Message.decode(contentBuf).toJSON()
                timestamp = child.get('attrs').get('t')
                fullMessage = proto.WebMessageInfo.fromObject(JSObject({
                    'key': JSObject({
                        'remoteJid': newsletterJid,
                        'id': orValue(child.get('attrs').get('id'), child.get('attrs').get('server_id')),
                        'server_id': child.get('attrs').get('server_id'),
                        'fromMe': False
                    }),
                    'message': messageProto,
                    'messageTimestamp': jsNumber(timestamp) if isTruthy(timestamp) else None
                })).toJSON()
                messages.append(fullMessage)
            except Exception as error:
                logger.error(JSObject({'error': error}), 'Failed to decode newsletter message')
        return messages

    async def subscribeNewsletterUpdates(jid):
        result = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'id': generateMessageTag(), 'type': 'set', 'xmlns': 'newsletter', 'to': jid}),
            'content': [JSObject({'tag': 'live_updates', 'attrs': JSObject({}), 'content': []})]
        }))
        liveUpdatesNode = getBinaryNodeChild(result, 'live_updates')
        duration = getp(getp(liveUpdatesNode, 'attrs'), 'duration')
        return JSObject({'duration': duration}) if isTruthy(duration) else None

    async def newsletterAdminCount(jid):
        response = await executeWMexQuery(JSObject({'newsletter_id': jid}), QueryIds['ADMIN_COUNT'], XWAPaths['xwa2_newsletter_admin_count'])
        return response.get('admin_count')

    async def newsletterChangeOwner(jid, newOwnerJid):
        await executeWMexQuery(JSObject({'newsletter_id': jid, 'user_id': newOwnerJid}), QueryIds['CHANGE_OWNER'], XWAPaths['xwa2_newsletter_change_owner'])

    async def newsletterDemote(jid, userJid):
        await executeWMexQuery(JSObject({'newsletter_id': jid, 'user_id': userJid}), QueryIds['DEMOTE'], XWAPaths['xwa2_newsletter_demote'])

    async def newsletterDelete(jid):
        await executeWMexQuery(JSObject({'newsletter_id': jid}), QueryIds['DELETE'], XWAPaths['xwa2_newsletter_delete_v2'])

    return spreadSocket(sock, JSObject({
        'executeWMexQuery': executeWMexQuery,
        'newsletterCreate': newsletterCreate,
        'newsletterUpdate': newsletterUpdate,
        'newsletterFollowers': newsletterFollowers,
        'newsletterSubscribers': newsletterSubscribers,
        'newsletterInsights': newsletterInsights,
        'newsletterAdminInfo': newsletterAdminInfo,
        'newsletterPendingAdminInvites': newsletterPendingAdminInvites,
        'newsletterSubscribed': newsletterSubscribed,
        'newsletterMetadata': newsletterMetadata,
        'newsletterFollow': newsletterFollow,
        'newsletterUnfollow': newsletterUnfollow,
        'newsletterMute': newsletterMute,
        'newsletterUnmute': newsletterUnmute,
        'newsletterUpdateName': newsletterUpdateName,
        'newsletterUpdateDescription': newsletterUpdateDescription,
        'newsletterUpdatePicture': newsletterUpdatePicture,
        'newsletterRemovePicture': newsletterRemovePicture,
        'newsletterReactMessage': newsletterReactMessage,
        'newsletterFetchMessages': newsletterFetchMessages,
        'subscribeNewsletterUpdates': subscribeNewsletterUpdates,
        'newsletterAdminCount': newsletterAdminCount,
        'newsletterChangeOwner': newsletterChangeOwner,
        'newsletterDemote': newsletterDemote,
        'newsletterDelete': newsletterDelete
    }))
