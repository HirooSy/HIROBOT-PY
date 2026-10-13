import asyncio
import math
import os
import platform
import re
import shutil
import sys
import time


START = time.time()


def fmt(n):
    if n == 0:
        return '0 B'
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    i = min(int(math.floor(math.log(n) / math.log(1000))), len(units) - 1)
    v = n / (1000 ** i)
    s = f'{v:.0f}' if i == 0 else re.sub(r'\.?0+$', '', f'{v:.2f}')
    return f'{s} {units[i]}'


def clock_string(ms):
    d = int(ms // 86400000)
    h = int(ms // 3600000) % 24
    mi = int(ms // 60000) % 60
    s = int(ms // 1000) % 60
    parts = []
    if d > 0:
        parts.append(f'{d:02d} ᴅ')
    if h > 0:
        parts.append(f'{h:02d} ʜ')
    if mi > 0:
        parts.append(f'{mi:02d} ᴍ')
    parts.append(f'{s:02d} s')
    return ', '.join(parts)


def cpu_model():
    try:
        with open('/proc/cpuinfo', 'r', encoding='utf-8') as fh:
            for line in fh:
                if line.lower().startswith('model name'):
                    name = line.split(':', 1)[1]
                    return re.sub(r'\s+', ' ', re.sub(r'\(R\)|\(TM\)|CPU', '', name)).strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def mem_info():
    for lim, use in (('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory.current'),
                     ('/sys/fs/cgroup/memory/memory.limit_in_bytes', '/sys/fs/cgroup/memory/memory.usage_in_bytes')):
        try:
            total = int(open(lim).read().strip())
            used = int(open(use).read().strip())
            if total < 1 << 50:
                return total, used
        except (OSError, ValueError):
            pass
    try:
        info = {}
        for line in open('/proc/meminfo'):
            k, v = line.split(':', 1)
            info[k] = int(v.split()[0]) * 1024
        return info['MemTotal'], info['MemTotal'] - info.get('MemAvailable', info.get('MemFree', 0))
    except Exception:
        return 0, 0


def rss():
    try:
        with open('/proc/self/status', 'r', encoding='utf-8') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    except Exception:
        return 0


def heap_info():
    return sys.getallocatedblocks() * 64, 0


def storage():
    try:
        usage = shutil.disk_usage(os.getcwd())
        total = usage.total
        if os.environ.get('SERVER_DISK', '').isdigit():
            total = int(os.environ['SERVER_DISK']) * 1024 * 1024
        used = 0
        for dp, _, fns in os.walk(os.getcwd()):
            for fn in fns:
                try:
                    used += os.path.getsize(os.path.join(dp, fn))
                except OSError:
                    pass
        return used, total
    except Exception:
        return None


async def handler(m, *, conn):
    uptime = clock_string((time.time() - START) * 1000)
    total, used = mem_info()
    st = await asyncio.to_thread(storage)
    drive = fmt(st[0]) if st else 'Not Detect'
    t0 = time.perf_counter()
    await m.react('⚙️')
    latency = (time.perf_counter() - t0) * 1000

    def row(label, value):
        return f'{label.ljust(8)} : {value}'
    info = '\n'.join([
        row('Runtime', uptime), row('CPU', cpu_model()), row('Cores', str(os.cpu_count())),
        row('RAM', f'{fmt(used)} / {fmt(total)}'), row('Disk', drive),
        row('System', f'{platform.system().lower()} {platform.machine()} (Python {platform.python_version()})'),
    ])
    heap_used = heap_info()[0]
    rss_now = rss()
    mem = '\n'.join([
        row('RSS', fmt(rss_now)),
        row('Heap', f'{fmt(heap_used)} / {fmt(rss_now)}'),
        row('External', fmt(0)),
        row('Buffers', fmt(0)),
    ])
    await conn.reply(m.chat, f'```{latency:.4f} ms\n──────────────────\n{info}\n──────────────────\n{mem}```', m)
    await m.react('✅')


help = ['ping', 'speed']
tags = ['info', 'tools']
command = re.compile(r'^(ping|speed)$', re.I)
ai = {'risk': 'low', 'summarize': True, 'description': 'Check speed response bot'}
