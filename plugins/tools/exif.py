import json
import re
import struct

from lib.utils.handler import PluginError
from plugins.tools.fetch import node_format


def read_webp_exif_chunk(buf):
    if buf[0:4] != b'RIFF' or buf[8:12] != b'WEBP':
        raise PluginError('Not a valid WebP file')
    offset = 12
    while offset < len(buf):
        if offset + 8 > len(buf):
            raise PluginError('Attempt to access memory outside buffer bounds')
        four = buf[offset:offset + 4]
        size = struct.unpack_from('<I', buf, offset + 4)[0]
        data_start = offset + 8
        if four == b'EXIF':
            return buf[data_start:data_start + size]
        offset = data_start + size + (size % 2)
    return None


def find_exif_json(exif):
    parsed = None
    search_from = 0
    while True:
        start = exif.find(b'{', search_from)
        if start == -1:
            break
        try:
            parsed = json.loads(exif[start:].decode('utf-8', errors='replace'))
            break
        except Exception:
            search_from = start + 1
    return parsed


async def handler(m):
    if not m.quoted:
        return await m.reply('Tag stikernya!')
    if re.search('sticker', m.quoted.mtype or ''):
        buffer = await m.quoted.download()
        exif = read_webp_exif_chunk(buffer)
        if exif is None:
            return await m.reply('Sticker ini tidak memiliki EXIF data.')
        parsed = find_exif_json(exif)
        if parsed is None:
            return await m.reply('EXIF data ditemukan, tapi tidak berisi JSON yang valid.')
        await m.reply(node_format(parsed))


tags = ['sticker']
help = command = ['getexif']
