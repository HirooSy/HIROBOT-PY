import asyncio
import os
import shutil
import subprocess
import time


TMP_DIR = os.path.join(os.getcwd(), 'data', 'tmp')


def binary(name):
    return shutil.which(name) or f'/usr/bin/{name}'


FFMPEG = binary('ffmpeg')
FFPROBE = binary('ffprobe')


def tmp_path(ext='bin', prefix=''):
    os.makedirs(TMP_DIR, exist_ok=True)
    return os.path.join(TMP_DIR, f'{prefix}{int(time.time() * 1000)}_{os.urandom(3).hex()}.{ext}')


async def run(binary_path, args, input_bytes=None, timeout=None):
    proc = await asyncio.create_subprocess_exec(
        binary_path, *args,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input_bytes), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise RuntimeError(err.decode('utf-8', errors='replace')[-2000:] or f'{binary_path} exited with {proc.returncode}')
    return out, err


async def ffmpeg(args, input_bytes=None, timeout=None):
    return await run(FFMPEG, ['-y', '-hide_banner', '-loglevel', 'error', *args], input_bytes, timeout)


async def convert_bytes(data, in_ext, out_ext, args=None, timeout=None):
    src = tmp_path(in_ext)
    dst = tmp_path(out_ext)
    with open(src, 'wb') as fh:
        fh.write(data)
    try:
        await ffmpeg(['-i', src, *(args or []), dst], timeout=timeout)
        with open(dst, 'rb') as fh:
            return fh.read()
    finally:
        for p in (src, dst):
            try:
                os.unlink(p)
            except OSError:
                pass


async def probe_duration(path):
    try:
        out, _ = await run(FFPROBE, ['-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', path])
        return float(out.decode().strip() or 0)
    except Exception:
        return 0.0


async def probe_dimensions(data, ext='jpg'):
    src = tmp_path(ext)
    with open(src, 'wb') as fh:
        fh.write(data)
    try:
        out, _ = await run(FFPROBE, ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', src])
        w, h = out.decode().strip().split('x')[:2]
        return int(w), int(h)
    except Exception:
        return 0, 0
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


FFMPEG_TIMEOUT = 120


def _sync_ffmpeg(args, input_bytes=b'', timeout=None):
    proc = subprocess.run(
        [FFMPEG, '-y', '-hide_banner', '-loglevel', 'error', *args],
        input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout or FFMPEG_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode('utf-8', errors='replace')[-2000:] or f'ffmpeg exited with {proc.returncode}')
    return proc.stdout


def transcode_sync(data, in_args, out_args, timeout=None):
    return _sync_ffmpeg([*in_args, '-i', 'pipe:0', *out_args, 'pipe:1'], bytes(data), timeout)


def decode_rgba_sync(data, timeout=None):
    return transcode_sync(data, ['-noautorotate'], ['-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgba'], timeout)


def filter_raw_sync(data, width, height, vf, pix_fmt='rgba', timeout=None):
    return _sync_ffmpeg(
        ['-f', 'rawvideo', '-pix_fmt', pix_fmt, '-video_size', f'{width}x{height}', '-i', 'pipe:0',
         '-vf', vf, '-f', 'rawvideo', '-pix_fmt', pix_fmt, 'pipe:1'],
        bytes(data), timeout,
    )


def encode_raw_sync(data, width, height, out_args, pix_fmt='rgba', vf=None, timeout=None):
    vf_args = ['-vf', vf] if vf else []
    return _sync_ffmpeg(
        ['-f', 'rawvideo', '-pix_fmt', pix_fmt, '-video_size', f'{width}x{height}', '-i', 'pipe:0',
         *vf_args, *out_args, 'pipe:1'],
        bytes(data), timeout,
    )


async def transcode(data, in_args, out_args, timeout=None):
    return await asyncio.to_thread(transcode_sync, data, in_args, out_args, timeout)


async def decode_rgba(data, timeout=None):
    return await asyncio.to_thread(decode_rgba_sync, data, timeout)


async def filter_raw(data, width, height, vf, pix_fmt='rgba', timeout=None):
    return await asyncio.to_thread(filter_raw_sync, data, width, height, vf, pix_fmt, timeout)


async def encode_raw(data, width, height, out_args, pix_fmt='rgba', vf=None, timeout=None):
    return await asyncio.to_thread(encode_raw_sync, data, width, height, out_args, pix_fmt, vf, timeout)
