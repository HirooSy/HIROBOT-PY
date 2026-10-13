import base64
import inspect
import sys

from baileys.WAProto import proto
from baileys.binary.wa_binary import (
    getBinaryNodeChild,
    getBinaryNodeChildren,
    getBinaryNodeChildString,
    isTruthy,
    jidEncode,
    jidNormalizedUser,
    jsNumber,
    S_WHATSAPP_NET
)
from baileys.constants import WAMessageStubType
from baileys.foundation.logger import createLogger

logger = createLogger(None, 'silent')
from baileys.foundation.node_runtime import JSObject
from baileys.socket.messages_recv import makeMessagesRecvSocket
from baileys.socket.socket_core import asyncListener, getp, orValue, spreadSocket, toBytes
from baileys.utils.media import getRawMediaUploadData
from baileys.utils.message_compose import jsonStringify
from baileys.utils.message_processing import (
    parseCatalogNode,
    parseCollectionsNode,
    parseOrderDetailsNode,
    parseProductNode,
    toProductNode,
    uploadingNecessaryImagesOfProduct
)
from baileys.utils.wa_protocol_core import generateMessageID, generateMessageIDV2, unixTimestampSeconds


async def maybeAwaitValue(value):
    if inspect.isawaitable(value):
        return await value
    return value


def makeBusinessSocket(config):
    sock = makeMessagesRecvSocket(config)
    authState = sock.get('authState')
    query = sock.get('query')
    waUploadToServer = sock.get('waUploadToServer')

    async def updateBussinesProfile(args):
        node = []
        simpleFields = ['address', 'email', 'description']
        for key in simpleFields:
            if args[key] is not None:
                node.append(JSObject({'tag': key, 'attrs': JSObject({}), 'content': args[key]}))
        if args.get('websites') is not None:
            for website in args.get('websites'):
                node.append(JSObject({'tag': 'website', 'attrs': JSObject({}), 'content': website}))
        if args.get('hours') is not None:
            hours = args.get('hours')
            content = []
            for dayConfig in hours.get('days'):
                base = JSObject({'tag': 'business_hours_config', 'attrs': JSObject({'day_of_week': dayConfig.get('day'), 'mode': dayConfig.get('mode')})})
                if dayConfig.get('mode') == 'specific_hours':
                    content.append(JSObject({
                        'tag': base.get('tag'),
                        'attrs': JSObject({**base.get('attrs'), 'open_time': dayConfig.get('openTimeInMinutes'), 'close_time': dayConfig.get('closeTimeInMinutes')})
                    }))
                else:
                    content.append(base)
            node.append(JSObject({'tag': 'business_hours', 'attrs': JSObject({'timezone': hours.get('timezone')}), 'content': content}))
        return await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'w:biz'}),
            'content': [JSObject({
                'tag': 'business_profile',
                'attrs': JSObject({'v': '3', 'mutation_type': 'delta'}),
                'content': node
            })]
        }))

    async def updateCoverPhoto(photo):
        raw = await getRawMediaUploadData(photo, 'biz-cover-photo', None)
        fileSha256 = raw.get('fileSha256')
        filePath = raw.get('filePath')
        fileSha256B64 = base64.b64encode(bytes(fileSha256)).decode()
        uploaded = await maybeAwaitValue(waUploadToServer(filePath, JSObject({'fileEncSha256B64': fileSha256B64, 'mediaType': 'biz-cover-photo'})))
        fbid = uploaded.get('fbid')
        await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'w:biz'}),
            'content': [JSObject({
                'tag': 'business_profile',
                'attrs': JSObject({'v': '3', 'mutation_type': 'delta'}),
                'content': [JSObject({
                    'tag': 'cover_photo',
                    'attrs': JSObject({'id': str(fbid), 'op': 'update', 'token': uploaded.get('meta_hmac'), 'ts': str(uploaded.get('ts'))})
                })]
            })]
        }))
        return fbid

    async def removeCoverPhoto(id_):
        return await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'w:biz'}),
            'content': [JSObject({
                'tag': 'business_profile',
                'attrs': JSObject({'v': '3', 'mutation_type': 'delta'}),
                'content': [JSObject({'tag': 'cover_photo', 'attrs': JSObject({'op': 'delete', 'id': id_})})]
            })]
        }))

    async def getCatalog(args):
        jid = args.get('jid')
        limit = args.get('limit')
        cursor = args.get('cursor')
        jid = jid if isTruthy(jid) else getp(authState.get('creds').get('me'), 'id')
        jid = jidNormalizedUser(jid)
        queryParamNodes = [
            JSObject({'tag': 'limit', 'attrs': JSObject({}), 'content': str(orValue(limit, 10)).encode('utf-8')}),
            JSObject({'tag': 'width', 'attrs': JSObject({}), 'content': b'100'}),
            JSObject({'tag': 'height', 'attrs': JSObject({}), 'content': b'100'})
        ]
        if isTruthy(cursor):
            queryParamNodes.append(JSObject({'tag': 'after', 'attrs': JSObject({}), 'content': cursor}))
        result = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'get', 'xmlns': 'w:biz:catalog'}),
            'content': [JSObject({
                'tag': 'product_catalog',
                'attrs': JSObject({'jid': jid, 'allow_shop_source': 'true'}),
                'content': queryParamNodes
            })]
        }))
        return parseCatalogNode(result)

    async def getCollections(jid, limit=51):
        jid = jid if isTruthy(jid) else getp(authState.get('creds').get('me'), 'id')
        jid = jidNormalizedUser(jid)
        result = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'get', 'xmlns': 'w:biz:catalog', 'smax_id': '35'}),
            'content': [JSObject({
                'tag': 'collections',
                'attrs': JSObject({'biz_jid': jid}),
                'content': [
                    JSObject({'tag': 'collection_limit', 'attrs': JSObject({}), 'content': str(limit).encode('utf-8')}),
                    JSObject({'tag': 'item_limit', 'attrs': JSObject({}), 'content': str(limit).encode('utf-8')}),
                    JSObject({'tag': 'width', 'attrs': JSObject({}), 'content': b'100'}),
                    JSObject({'tag': 'height', 'attrs': JSObject({}), 'content': b'100'})
                ]
            })]
        }))
        return parseCollectionsNode(result)

    async def getOrderDetails(orderId, tokenBase64):
        result = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'get', 'xmlns': 'fb:thrift_iq', 'smax_id': '5'}),
            'content': [JSObject({
                'tag': 'order',
                'attrs': JSObject({'op': 'get', 'id': orderId}),
                'content': [
                    JSObject({
                        'tag': 'image_dimensions',
                        'attrs': JSObject({}),
                        'content': [
                            JSObject({'tag': 'width', 'attrs': JSObject({}), 'content': b'100'}),
                            JSObject({'tag': 'height', 'attrs': JSObject({}), 'content': b'100'})
                        ]
                    }),
                    JSObject({'tag': 'token', 'attrs': JSObject({}), 'content': toBytes(tokenBase64)})
                ]
            })]
        }))
        return parseOrderDetailsNode(result)

    async def productUpdate(productId, update):
        update = await uploadingNecessaryImagesOfProduct(update, waUploadToServer)
        editNode = toProductNode(productId, update)
        result = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'w:biz:catalog'}),
            'content': [JSObject({
                'tag': 'product_catalog_edit',
                'attrs': JSObject({'v': '1'}),
                'content': [
                    editNode,
                    JSObject({'tag': 'width', 'attrs': JSObject({}), 'content': '100'}),
                    JSObject({'tag': 'height', 'attrs': JSObject({}), 'content': '100'})
                ]
            })]
        }))
        productCatalogEditNode = getBinaryNodeChild(result, 'product_catalog_edit')
        productNode = getBinaryNodeChild(productCatalogEditNode, 'product')
        return parseProductNode(productNode)

    async def productCreate(create):
        create['isHidden'] = isTruthy(create.get('isHidden'))
        create = await uploadingNecessaryImagesOfProduct(create, waUploadToServer)
        createNode = toProductNode(None, create)
        result = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'w:biz:catalog'}),
            'content': [JSObject({
                'tag': 'product_catalog_add',
                'attrs': JSObject({'v': '1'}),
                'content': [
                    createNode,
                    JSObject({'tag': 'width', 'attrs': JSObject({}), 'content': '100'}),
                    JSObject({'tag': 'height', 'attrs': JSObject({}), 'content': '100'})
                ]
            })]
        }))
        productCatalogAddNode = getBinaryNodeChild(result, 'product_catalog_add')
        productNode = getBinaryNodeChild(productCatalogAddNode, 'product')
        return parseProductNode(productNode)

    async def productDelete(productIds):
        result = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': S_WHATSAPP_NET, 'type': 'set', 'xmlns': 'w:biz:catalog'}),
            'content': [JSObject({
                'tag': 'product_catalog_delete',
                'attrs': JSObject({'v': '1'}),
                'content': [JSObject({
                    'tag': 'product',
                    'attrs': JSObject({}),
                    'content': [JSObject({'tag': 'id', 'attrs': JSObject({}), 'content': toBytes(id_)})]
                }) for id_ in productIds]
            })]
        }))
        productCatalogDelNode = getBinaryNodeChild(result, 'product_catalog_delete')
        return JSObject({'deleted': jsNumber(orValue(getp(getp(productCatalogDelNode, 'attrs'), 'deleted_count'), 0))})

    return spreadSocket(sock, JSObject({
        'logger': config.get('logger'),
        'getOrderDetails': getOrderDetails,
        'getCatalog': getCatalog,
        'getCollections': getCollections,
        'productCreate': productCreate,
        'productDelete': productDelete,
        'productUpdate': productUpdate,
        'updateBussinesProfile': updateBussinesProfile,
        'updateCoverPhoto': updateCoverPhoto,
        'removeCoverPhoto': removeCoverPhoto
    }))


def makeCommunitiesSocket(config):
    sock = makeBusinessSocket(config)
    authState = sock.get('authState')
    ev = sock.get('ev')
    query = sock.get('query')
    upsertMessage = sock.get('upsertMessage')

    async def communityQuery(jid, type_, content):
        return await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'type': type_, 'xmlns': 'w:g2', 'to': jid}),
            'content': content
        }))

    async def communityMetadata(jid):
        result = await communityQuery(jid, 'get', [JSObject({'tag': 'query', 'attrs': JSObject({'request': 'interactive'})})])
        return extractCommunityMetadata(result)

    async def communityFetchAllParticipating():
        result = await query(JSObject({
            'tag': 'iq',
            'attrs': JSObject({'to': '@g.us', 'xmlns': 'w:g2', 'type': 'get'}),
            'content': [JSObject({
                'tag': 'participating',
                'attrs': JSObject({}),
                'content': [
                    JSObject({'tag': 'participants', 'attrs': JSObject({})}),
                    JSObject({'tag': 'description', 'attrs': JSObject({})})
                ]
            })]
        }))
        data = JSObject({})
        communitiesChild = getBinaryNodeChild(result, 'communities')
        if communitiesChild is not None:
            for communityNode in getBinaryNodeChildren(communitiesChild, 'community'):
                meta = extractCommunityMetadata(JSObject({'tag': 'result', 'attrs': JSObject({}), 'content': [communityNode]}))
                data[meta.get('id')] = meta
        ev.get('emit')('groups.update', list(dict.values(data)))
        return data

    async def parseGroupResult(node):
        logger.info(JSObject({'node': node}), 'parseGroupResult')
        groupNode = getBinaryNodeChild(node, 'group')
        if groupNode is not None:
            try:
                logger.info(JSObject({'groupNode': groupNode}), 'groupNode')
                metadata = await sock.get('groupMetadata')(f"{groupNode.get('attrs').get('id')}@g.us")
                return metadata if isTruthy(metadata) else None
            except Exception as error:
                print('Error parsing group metadata:', error, file=sys.stderr)
                return None
        return None

    async def onCommunityDirty(node):
        attrs = getBinaryNodeChild(node, 'dirty')['attrs']
        if attrs.get('type') != 'communities':
            return
        await communityFetchAllParticipating()
        await sock.get('cleanDirtyBits')('groups')
    sock.get('ws').on('CB:ib,,dirty', asyncListener(onCommunityDirty))

    async def communityCreate(subject, body):
        descriptionId = generateMessageID()[:12]
        result = await communityQuery('@g.us', 'set', [
            JSObject({
                'tag': 'create',
                'attrs': JSObject({'subject': subject}),
                'content': [
                    JSObject({
                        'tag': 'description',
                        'attrs': JSObject({'id': descriptionId}),
                        'content': [JSObject({'tag': 'body', 'attrs': JSObject({}), 'content': toBytes(orValue(body, ''))})]
                    }),
                    JSObject({'tag': 'parent', 'attrs': JSObject({'default_membership_approval_mode': 'request_required'})}),
                    JSObject({'tag': 'allow_non_admin_sub_group_creation', 'attrs': JSObject({})}),
                    JSObject({'tag': 'create_general_chat', 'attrs': JSObject({})})
                ]
            })
        ])
        return await parseGroupResult(result)

    async def communityCreateGroup(subject, participants, parentCommunityJid):
        key = generateMessageIDV2(None)
        result = await communityQuery('@g.us', 'set', [
            JSObject({
                'tag': 'create',
                'attrs': JSObject({'subject': subject, 'key': key}),
                'content': [
                    *[JSObject({'tag': 'participant', 'attrs': JSObject({'jid': jid})}) for jid in participants],
                    JSObject({'tag': 'linked_parent', 'attrs': JSObject({'jid': parentCommunityJid})})
                ]
            })
        ])
        return await parseGroupResult(result)

    async def communityLeave(id_):
        await communityQuery('@g.us', 'set', [
            JSObject({'tag': 'leave', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'community', 'attrs': JSObject({'id': id_})})]})
        ])

    async def communityUpdateSubject(jid, subject):
        await communityQuery(jid, 'set', [JSObject({'tag': 'subject', 'attrs': JSObject({}), 'content': subject.encode('utf-8')})])

    async def communityLinkGroup(groupJid, parentCommunityJid):
        await communityQuery(parentCommunityJid, 'set', [
            JSObject({
                'tag': 'links',
                'attrs': JSObject({}),
                'content': [JSObject({
                    'tag': 'link',
                    'attrs': JSObject({'link_type': 'sub_group'}),
                    'content': [JSObject({'tag': 'group', 'attrs': JSObject({'jid': groupJid})})]
                })]
            })
        ])

    async def communityUnlinkGroup(groupJid, parentCommunityJid):
        await communityQuery(parentCommunityJid, 'set', [
            JSObject({
                'tag': 'unlink',
                'attrs': JSObject({'unlink_type': 'sub_group'}),
                'content': [JSObject({'tag': 'group', 'attrs': JSObject({'jid': groupJid})})]
            })
        ])

    async def communityFetchLinkedGroups(jid):
        communityJid = jid
        isCommunity = False
        metadata = await sock.get('groupMetadata')(jid)
        if isTruthy(metadata.get('linkedParent')):
            communityJid = metadata.get('linkedParent')
        else:
            isCommunity = True
        result = await communityQuery(communityJid, 'get', [JSObject({'tag': 'sub_groups', 'attrs': JSObject({})})])
        linkedGroupsData = []
        subGroupsNode = getBinaryNodeChild(result, 'sub_groups')
        if subGroupsNode is not None:
            for groupNode in getBinaryNodeChildren(subGroupsNode, 'group'):
                attrs = groupNode.get('attrs')
                linkedGroupsData.append(JSObject({
                    'id': jidEncode(attrs.get('id'), 'g.us') if isTruthy(attrs.get('id')) else None,
                    'subject': orValue(attrs.get('subject'), ''),
                    'creation': jsNumber(attrs.get('creation')) if isTruthy(attrs.get('creation')) else None,
                    'owner': jidNormalizedUser(attrs.get('creator')) if isTruthy(attrs.get('creator')) else None,
                    'size': jsNumber(attrs.get('size')) if isTruthy(attrs.get('size')) else None
                }))
        return JSObject({'communityJid': communityJid, 'isCommunity': isCommunity, 'linkedGroups': linkedGroupsData})

    async def communityRequestParticipantsList(jid):
        result = await communityQuery(jid, 'get', [JSObject({'tag': 'membership_approval_requests', 'attrs': JSObject({})})])
        node = getBinaryNodeChild(result, 'membership_approval_requests')
        participants = getBinaryNodeChildren(node, 'membership_approval_request')
        return [v.get('attrs') for v in participants]

    async def communityRequestParticipantsUpdate(jid, participants, action):
        result = await communityQuery(jid, 'set', [
            JSObject({
                'tag': 'membership_requests_action',
                'attrs': JSObject({}),
                'content': [JSObject({
                    'tag': action,
                    'attrs': JSObject({}),
                    'content': [JSObject({'tag': 'participant', 'attrs': JSObject({'jid': p})}) for p in participants]
                })]
            })
        ])
        node = getBinaryNodeChild(result, 'membership_requests_action')
        nodeAction = getBinaryNodeChild(node, action)
        participantsAffected = getBinaryNodeChildren(nodeAction, 'participant')
        return [JSObject({'status': orValue(p.get('attrs').get('error'), '200'), 'jid': p.get('attrs').get('jid')}) for p in participantsAffected]

    async def communityParticipantsUpdate(jid, participants, action):
        result = await communityQuery(jid, 'set', [
            JSObject({
                'tag': action,
                'attrs': JSObject({'linked_groups': 'true'}) if action == 'remove' else JSObject({}),
                'content': [JSObject({'tag': 'participant', 'attrs': JSObject({'jid': p})}) for p in participants]
            })
        ])
        node = getBinaryNodeChild(result, action)
        participantsAffected = getBinaryNodeChildren(node, 'participant')
        return [JSObject({'status': orValue(p.get('attrs').get('error'), '200'), 'jid': p.get('attrs').get('jid'), 'content': p}) for p in participantsAffected]

    async def communityUpdateDescription(jid, description):
        metadata = await communityMetadata(jid)
        prev = metadata.get('descId')
        attrs = JSObject({'id': generateMessageID()} if isTruthy(description) else {'delete': 'true'})
        if isTruthy(prev):
            attrs['prev'] = prev
        await communityQuery(jid, 'set', [
            JSObject({
                'tag': 'description',
                'attrs': attrs,
                'content': [JSObject({'tag': 'body', 'attrs': JSObject({}), 'content': description.encode('utf-8')})] if isTruthy(description) else None
            })
        ])

    async def communityInviteCode(jid):
        result = await communityQuery(jid, 'get', [JSObject({'tag': 'invite', 'attrs': JSObject({})})])
        inviteNode = getBinaryNodeChild(result, 'invite')
        return getp(getp(inviteNode, 'attrs'), 'code')

    async def communityRevokeInvite(jid):
        result = await communityQuery(jid, 'set', [JSObject({'tag': 'invite', 'attrs': JSObject({})})])
        inviteNode = getBinaryNodeChild(result, 'invite')
        return getp(getp(inviteNode, 'attrs'), 'code')

    async def communityAcceptInvite(code):
        results = await communityQuery('@g.us', 'set', [JSObject({'tag': 'invite', 'attrs': JSObject({'code': code})})])
        result = getBinaryNodeChild(results, 'community')
        return getp(getp(result, 'attrs'), 'jid')

    async def communityRevokeInviteV4(communityJid, invitedJid):
        result = await communityQuery(communityJid, 'set', [
            JSObject({'tag': 'revoke', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'participant', 'attrs': JSObject({'jid': invitedJid})})]})
        ])
        return isTruthy(result)

    async def acceptInviteV4Work(key, inviteMessage):
        if isinstance(key, str):
            key = JSObject({'remoteJid': key})
        results = await communityQuery(inviteMessage.get('groupJid'), 'set', [
            JSObject({
                'tag': 'accept',
                'attrs': JSObject({'code': inviteMessage.get('inviteCode'), 'expiration': str(inviteMessage.get('inviteExpiration')), 'admin': key.get('remoteJid')})
            })
        ])
        if isTruthy(key.get('id')):
            inviteMessage = proto.Message.GroupInviteMessage.fromObject(inviteMessage)
            inviteMessage['inviteExpiration'] = 0
            inviteMessage['inviteCode'] = ''
            ev.get('emit')('messages.update', [JSObject({
                'key': key,
                'update': JSObject({'message': JSObject({'groupInviteMessage': inviteMessage})})
            })])
        await upsertMessage(JSObject({
            'key': JSObject({
                'remoteJid': inviteMessage.get('groupJid'),
                'id': generateMessageIDV2(getp(sock.get('user'), 'id')),
                'fromMe': False,
                'participant': key.get('remoteJid')
            }),
            'messageStubType': WAMessageStubType['GROUP_PARTICIPANT_ADD'],
            'messageStubParameters': [jsonStringify(authState.get('creds').get('me'))],
            'participant': key.get('remoteJid'),
            'messageTimestamp': unixTimestampSeconds()
        }), 'notify')
        return results.get('attrs').get('from')
    communityAcceptInviteV4 = ev.get('createBufferedFunction')(acceptInviteV4Work)

    async def communityGetInviteInfo(code):
        results = await communityQuery('@g.us', 'get', [JSObject({'tag': 'invite', 'attrs': JSObject({'code': code})})])
        return extractCommunityMetadata(results)

    async def communityToggleEphemeral(jid, ephemeralExpiration):
        if isTruthy(ephemeralExpiration):
            content = JSObject({'tag': 'ephemeral', 'attrs': JSObject({'expiration': str(ephemeralExpiration)})})
        else:
            content = JSObject({'tag': 'not_ephemeral', 'attrs': JSObject({})})
        await communityQuery(jid, 'set', [content])

    async def communitySettingUpdate(jid, setting):
        await communityQuery(jid, 'set', [JSObject({'tag': setting, 'attrs': JSObject({})})])

    async def communityMemberAddMode(jid, mode):
        await communityQuery(jid, 'set', [JSObject({'tag': 'member_add_mode', 'attrs': JSObject({}), 'content': mode})])

    async def communityJoinApprovalMode(jid, mode):
        await communityQuery(jid, 'set', [
            JSObject({'tag': 'membership_approval_mode', 'attrs': JSObject({}), 'content': [JSObject({'tag': 'community_join', 'attrs': JSObject({'state': mode})})]})
        ])

    return spreadSocket(sock, JSObject({
        'communityMetadata': communityMetadata,
        'communityCreate': communityCreate,
        'communityCreateGroup': communityCreateGroup,
        'communityLeave': communityLeave,
        'communityUpdateSubject': communityUpdateSubject,
        'communityLinkGroup': communityLinkGroup,
        'communityUnlinkGroup': communityUnlinkGroup,
        'communityFetchLinkedGroups': communityFetchLinkedGroups,
        'communityRequestParticipantsList': communityRequestParticipantsList,
        'communityRequestParticipantsUpdate': communityRequestParticipantsUpdate,
        'communityParticipantsUpdate': communityParticipantsUpdate,
        'communityUpdateDescription': communityUpdateDescription,
        'communityInviteCode': communityInviteCode,
        'communityRevokeInvite': communityRevokeInvite,
        'communityAcceptInvite': communityAcceptInvite,
        'communityRevokeInviteV4': communityRevokeInviteV4,
        'communityAcceptInviteV4': communityAcceptInviteV4,
        'communityGetInviteInfo': communityGetInviteInfo,
        'communityToggleEphemeral': communityToggleEphemeral,
        'communitySettingUpdate': communitySettingUpdate,
        'communityMemberAddMode': communityMemberAddMode,
        'communityJoinApprovalMode': communityJoinApprovalMode,
        'communityFetchAllParticipating': communityFetchAllParticipating
    }))


def extractCommunityMetadata(result):
    community = getBinaryNodeChild(result, 'community')
    descChild = getBinaryNodeChild(community, 'description')
    desc = None
    descId = None
    if descChild is not None:
        desc = getBinaryNodeChildString(descChild, 'body')
        descId = descChild.get('attrs').get('id')
    rawId = community.get('attrs').get('id')
    communityId = rawId if rawId is not None and '@' in rawId else jidEncode(orValue(rawId, ''), 'g.us')
    eph = getp(getBinaryNodeChild(community, 'ephemeral'), 'attrs')
    ephExpiration = getp(eph, 'expiration')
    memberAddMode = getBinaryNodeChildString(community, 'member_add_mode') == 'all_member_add'
    creator = community.get('attrs').get('creator')
    linkedParent = getp(getBinaryNodeChild(community, 'linked_parent'), 'attrs')
    linkedParentJid = getp(linkedParent, 'jid')
    return JSObject({
        'id': communityId,
        'subject': orValue(community.get('attrs').get('subject'), ''),
        'subjectOwner': community.get('attrs').get('s_o'),
        'subjectTime': jsNumber(orValue(community.get('attrs').get('s_t'), 0)),
        'size': len(getBinaryNodeChildren(community, 'participant')),
        'creation': jsNumber(orValue(community.get('attrs').get('creation'), 0)),
        'owner': jidNormalizedUser(creator) if isTruthy(creator) else None,
        'desc': desc,
        'descId': descId,
        'linkedParent': orValue(linkedParentJid, None),
        'restrict': getBinaryNodeChild(community, 'locked') is not None,
        'announce': getBinaryNodeChild(community, 'announcement') is not None,
        'isCommunity': getBinaryNodeChild(community, 'parent') is not None,
        'isCommunityAnnounce': getBinaryNodeChild(community, 'default_sub_community') is not None,
        'joinApprovalMode': getBinaryNodeChild(community, 'membership_approval_mode') is not None,
        'memberAddMode': memberAddMode,
        'participants': [JSObject({'id': participant.get('attrs').get('jid'), 'admin': orValue(participant.get('attrs').get('type'), None)}) for participant in getBinaryNodeChildren(community, 'participant')],
        'ephemeralDuration': jsNumber(ephExpiration) if isTruthy(ephExpiration) else None,
        'addressingMode': getBinaryNodeChildString(community, 'addressing_mode')
    })
