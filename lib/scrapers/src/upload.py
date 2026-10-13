import hashlib
import os
import re
import sys

from lib.compat import http


ONLYFILES_UPLOAD_URL = 'https://api.onlyfiles.com/v1/upload'
ONLYFILES_MAX_SIZE = 100 * 1024 * 1024
LOCAL_MAX_SIZE = 200 * 1024 * 1024
LOCAL_PORT = os.environ.get('PORT') or os.environ.get('SERVER_PORT') or 8080
LOCAL_UPLOAD_URL = f'http://127.0.0.1:{LOCAL_PORT}/api/upload'
_cache = {}


async def upload_to_onlyfiles(buffer, filename):
    res = await http.post(ONLYFILES_UPLOAD_URL, multipart={'file': {'content': buffer, 'filename': filename}, 'expire': '0'},
                          timeout=30, raise_for_status=False)
    body = res.text()
    try:
        data = res.json()
    except Exception:
        data = None
    if res.status != 200:
        raise RuntimeError(f'onlyfiles.com HTTP {res.status}: {body[:300]}')
    file_data = ((data or {}).get('data') or {}).get('file')
    if (data or {}).get('status') is not True or not file_data:
        raise RuntimeError((data or {}).get('message') or 'onlyfiles.com returned no file data')
    url = file_data.get('url') or {}
    share = url.get('full') or url.get('short')
    if not share:
        raise RuntimeError('onlyfiles.com returned no share URL')
    try:
        page = await http.get(share, timeout=15)
        mt = re.search(r'<a class="download" href="([^"]+)"', page.text())
        if mt:
            return mt.group(1)
    except Exception as e:
        print('[UPLOAD] onlyfiles.com:', e, file=sys.stderr)
    return share


async def upload_to_local(buffer, filename):
    res = await http.post(LOCAL_UPLOAD_URL, multipart={'file': {'content': buffer, 'filename': filename}}, raise_for_status=False)
    try:
        data = res.json()
    except Exception:
        data = None
    if not data or not data.get('success') or not data.get('url'):
        raise RuntimeError((data or {}).get('message') or 'Local upload server returned no URL')
    return data['url']


async def upload(buffer, filename='file.bin'):
    buffer = bytes(buffer)
    digest = hashlib.sha256(buffer).hexdigest()
    if digest in _cache:
        return _cache[digest]
    if len(buffer) > LOCAL_MAX_SIZE:
        raise RuntimeError(f'File too large: {len(buffer) / 1048576:.2f}MB exceeds max limit of {LOCAL_MAX_SIZE // 1048576}MB')
    url = None
    if len(buffer) <= ONLYFILES_MAX_SIZE:
        try:
            url = await upload_to_onlyfiles(buffer, filename)
        except Exception as err:
            print('[UPLOAD] onlyfiles.com failed, falling back to local server:', err, file=sys.stderr)
    if not url:
        try:
            url = await upload_to_local(buffer, filename)
        except Exception as err2:
            raise RuntimeError('Upload failed on all providers: ' + str(err2))
    _cache[digest] = url
    return url


default = upload
