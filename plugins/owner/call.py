import os
import re
import sys
import tempfile
import time

from baileys_lite.voip import Voip
from lib.utils.helper import spawn
from lib.utils.handler import PluginError


_instances = {}
_active = {}
VIDEO_EXT_RE = re.compile(r'\.(mp4|mov|webm|mkv|avi|m4v|3gp)(\?|#|$)', re.I)


def get_voip(conn):
    key = id(conn)
    if key not in _instances:
        _instances[key] = (conn, Voip(conn, {'voipLogLevel': 'info'}))
    return _instances[key][1]


async def download_quoted_media(quoted):
    mime = quoted.mimetype or ''
    kind = 'video' if re.match(r'^video', mime) else 'audio' if re.match(r'^audio', mime) else None
    if not kind:
        return None
    buf = await quoted.download()
    if not buf:
        raise PluginError(f'Failed to download the replied {kind}.')
    path = os.path.join(tempfile.gettempdir(), f"voip_{kind}_{int(time.time() * 1000)}{'.mp4' if kind == 'video' else '.audio'}")
    with open(path, 'wb') as fh:
        fh.write(buf)
    return path


def require_call(m):
    entry = _active.get(m.chat)
    if not entry:
        raise PluginError('No call is currently in progress in this chat.')
    return entry


async def handler(m, *, conn, args, usedPrefix, command):
    args, p, command = args, usedPrefix, command
    voip = get_voip(conn)
    if command == 'voipend':
        force = bool(args) and args[0] == 'force'
        await voip.end(force)
        _active.pop(m.chat, None)
        await m.reply('✦ VOIP state force-reset.' if force else '✦ Hangup requested...')
        return
    if command == 'voipreact':
        entry = require_call(m)
        emoji = args[0] if args else '👍'
        sent = entry['call'].react(emoji)
        await m.reply(f'✦ Reaction {emoji} sent.' if sent else '✦ Failed to send reaction (call is not active yet).')
        return
    if command == 'voipsilent':
        entry = require_call(m)
        silenced = await entry['call'].silent()
        await m.reply('✦ Muted mic and paused video.' if silenced else '✦ Resumed.')
        return
    if not args:
        raise PluginError(f'Usage: {p + command} <phone_number> [media_url_or_path ...] [resolution] [auto] [loop] (reply to audio/video, or provide one or more URLs — mixing video and audio URLs plays them as a playlist; add "auto" to hang up automatically once the playlist finishes, "loop" to replay it from the start instead)\nDuring a call: .voipsilent .voipreact <emoji> .voipend')
    if m.chat in _active:
        raise PluginError('A call is already in progress in this chat, wait for it to finish (or `.voipend`).')
    phone = re.sub(r'\D', '', args[0])
    if not phone:
        raise PluginError('Invalid phone number.')
    rest = args[1:]
    resolution = next((a.lower() for a in rest if re.match(r'^(240p|360p|480p|720p|1080p)$', a, re.I)), None)
    auto_end = any(re.match(r'^auto$', a, re.I) for a in rest)
    loop = any(re.match(r'^loop$', a, re.I) for a in rest)
    media = [a for a in rest if re.match(r'^https?://', a, re.I)]
    temp = []
    qpath = await download_quoted_media(m.quoted) if m.quoted else None
    if qpath:
        temp.append(qpath)
        media.insert(0, qpath)
    will_video = any(src != 'silence' and VIDEO_EXT_RE.search(src) for src in media)
    media = 'silence' if not media else media[0] if len(media) == 1 else media
    sent = await m.reply(f"✦ Calling {phone}...{' (video)' if will_video else ''} (Use .voipend to end call, .voipsilent to mute/pause)")
    key = sent['key']

    def cleanup():
        for f in temp:
            try:
                os.unlink(f)
            except OSError:
                continue
    try:
        call = await voip.call(phone, media, resolution, {'autoEndCall': auto_end, 'loop': loop})
        _active[m.chat] = {'call': call, 'key': key, 'phoneNumber': phone}
        call.on('ringing', lambda *a: spawn(conn.sendMessage(m.chat, {'text': f'✦ Ringing {phone}...', 'edit': key})))
        call.on('connected', lambda *a: spawn(conn.sendMessage(m.chat, {'text': '✦ Call connected! Use .voipend to end.', 'edit': key})))

        def on_item(info):
            if info['index'] == 0:
                return
            spawn(conn.sendMessage(m.chat, {'text': f"✦ Now playing item {info['index'] + 1} ({info['kind']})."}))
        call.on('item', on_item)
        call.on('downgraded', lambda *a: spawn(conn.sendMessage(m.chat, {'text': '✦ All cameras are off, downgraded to audio.'})))

        def on_ended(reason=None):
            _active.pop(m.chat, None)
            text = f'✦ Call to {phone} was declined.' if reason == 'declined' else f'✦ Call ended for {phone}: {reason}'
            spawn(conn.sendMessage(m.chat, {'text': text, 'edit': key}))
            cleanup()
        call.on('ended', on_ended)

        def on_error(err=None):
            if call not in voip.calls:
                _active.pop(m.chat, None)
                cleanup()
            spawn(conn.reply(m.chat, f'Call error: {err}', m))
        call.on('error', on_error)
    except Exception as e:
        print('[ VOIP ] voip.call() threw:', e, file=sys.stderr)
        _active.pop(m.chat, None)
        cleanup()
        raise PluginError(f'Failed to place call: {e}')


help = ['voipcall <number>', 'voipend', 'voipsilent', 'voipreact <emoji>']
tags = ['owner']
command = re.compile(r'^(voipcall|voipend|voipsilent|voipreact)$', re.I)
rowner = True
