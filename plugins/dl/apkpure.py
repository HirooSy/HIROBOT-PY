import os
import re
import tempfile
import time

from lib.utils.handler import G, PluginError


MAX_SIZE = 1.9 * 1024 ** 3


async def handler(m, *, conn, text, usedPrefix, command):
    text, p, command = text, usedPrefix, command
    ap = G.scraper.apkpure
    if not text:
        raise PluginError(f'- *Example:*\n> {p + command} whatsapp\n> {p + command} com.whatsapp\n> {p + command} https://apkpure.com/whatsapp-android/com.whatsapp')
    await m.react('🔎')
    tmp = None
    try:
        is_target = bool(re.search(r'apkpure\.(com|net)/', text, re.I)) or bool(re.match(r'^[a-z0-9_]+(\.[a-z0-9_]+)+$', text.strip(), re.I))
        if not is_target:
            lst = await ap.apkSearch(text, 10)
            if not lst:
                raise PluginError('> App not found')
            rows = [{'header': f"{i + 1}. {a['title']}"[:60], 'title': a['pkg'],
                     'description': ' · '.join(x for x in [a.get('developer'), f"⭐ {a['rating']}" if a.get('rating') else None] if x) or 'Tap to download',
                     'id': f"{p}apkpure {a['pkg']}"} for i, a in enumerate(lst)]
            await conn.sendButton(m.chat, {'text': f'- *APKPure Search*\n- *Query:* {text}\n- *Results:* {len(lst)}\n\nSelect the app you want to download.',
                                           'footer': 'APKPure', 'nativeFlow': [{'text': 'Select App', 'sections': [{'title': 'Result', 'rows': rows}]}]}, m)
            return await m.react('✅')
        try:
            detail = await ap.apkDetail(text.strip())
        except Exception:
            detail = None
        dl = await ap.apkDownload(text.strip(), 'XAPK' if re.search('xapk', command, re.I) else 'auto')
        if dl.get('size') and dl['size'] > MAX_SIZE:
            raise PluginError(f"> File too large ({ap.formatSize(dl['size'])})")
        await m.react('⬇️')
        tmp = os.path.join(tempfile.gettempdir(), f"apk_{int(time.time() * 1000)}_{re.sub(r'[^A-Za-z0-9_.-]', '_', dl['fileName'])}")
        size = await ap.apkFetchFile(dl, tmp, MAX_SIZE)
        version = f"\n- `Version:` {detail['version']}" if detail and detail.get('version') else ''
        caption = f"- `Name:` {(detail or {}).get('title') or dl['pkg']}\n- `Package:` {dl['pkg']}{version}\n- `Size:` {ap.formatSize(size)}\n- `Type:` {dl['type']}"
        await conn.sendMessage(m.chat, {'document': {'url': tmp}, 'fileName': dl['fileName'],
                                        'mimetype': 'application/vnd.android.package-archive' if dl['type'] == 'APK' else 'application/octet-stream',
                                        'caption': caption}, {'quoted': m})
        await m.react('✅')
    except Exception as e:
        m.error = e
        raise
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


help = ['apkpure <query>']
tags = ['downloader', 'internet']
command = re.compile(r'^(apkpure|apk|apkpurexapk)$', re.I)
limit = True
