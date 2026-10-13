import asyncio
import math
import re
import time

from lib.compat import http
from lib.utils.handler import G


UA = 'Mozilla/5.0 (speedtest-cli/2.1.4)'
UPLOAD_SIZES = [32768, 65536, 131072, 262144, 524288, 1048576, 7340032]
DOWNLOAD_SIZES = [350, 500, 750, 1000, 1500, 2000, 2500, 3000, 3500, 4000]
NO_PING = 3600000


def js_round(x):
    return math.floor(x + 0.5)


def js_str(v):
    if v is None:
        return 'undefined'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return str(v)


def now_ms():
    return int(time.time() * 1000)


def parse_attrs(s):
    return {m.group(1): m.group(2) for m in re.finditer(r'([a-zA-Z_:][\w:.-]*)\s*=\s*"([^"]*)"', s)}


def find_tag(xml, name):
    m = re.search(rf'<{name}\b([^>]*?)/?>', xml, re.I)
    return parse_attrs(m.group(1)) if m else {}


def find_all(xml, name):
    return [parse_attrs(m.group(1)) for m in re.finditer(rf'<{name}\b([^>]*?)/?>', xml, re.I)]


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float('nan')


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def distance(lat1, lon1, lat2, lon2):
    r = 6371
    d_lat, d_lon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bust(url):
    return f"{url}{'&' if '?' in url else '?'}x={now_ms()}.0"


async def fetch_xml(url):
    res = await http.get(bust(url), headers={'User-Agent': UA, 'Cache-Control': 'no-cache'}, timeout=15, raise_for_status=False)
    if not res.ok:
        raise RuntimeError(f'HTTP {res.status} for {url}')
    return res.text()


async def get_config():
    xml = await fetch_xml('https://www.speedtest.net/speedtest-config.php')
    client, dl, ul, srv = (find_tag(xml, t) for t in ('client', 'download', 'upload', 'server-config'))
    ratio, up_max = to_int(ul.get('ratio')), to_int(ul.get('maxchunkcount'))
    up_sizes = UPLOAD_SIZES[ratio - 1:] if ratio is not None else list(UPLOAD_SIZES)
    upload_count = math.ceil(up_max / len(up_sizes))
    ignore = [to_int(x) for x in (srv.get('ignoreids') or '').split(',') if x]
    return {
        'client': {'ip': client.get('ip'), 'isp': client.get('isp'), 'lat': to_float(client.get('lat')), 'lon': to_float(client.get('lon'))},
        'ignore': ignore,
        'sizes': {'download': DOWNLOAD_SIZES, 'upload': up_sizes},
        'counts': {'download': to_int(dl.get('threadsperurl')), 'upload': upload_count},
        'threads': {'download': to_int(srv.get('threadcount')) * 2, 'upload': to_int(ul.get('threads'))},
        'length': {'download': to_int(dl.get('testlength')), 'upload': to_int(ul.get('testlength'))},
        'uploadMax': upload_count * len(up_sizes),
    }


async def get_servers(cfg):
    for base in ('https://www.speedtest.net/speedtest-servers-static.php', 'http://c.speedtest.net/speedtest-servers-static.php',
                 'https://www.speedtest.net/speedtest-servers.php', 'http://c.speedtest.net/speedtest-servers.php'):
        try:
            xml = await fetch_xml(f"{base}?threads={cfg['threads']['download']}")
            elements = find_all(xml, 'server')
            if not elements:
                continue
            servers = []
            for s in elements:
                if to_int(s.get('id')) in cfg['ignore']:
                    continue
                d = distance(cfg['client']['lat'], cfg['client']['lon'], to_float(s.get('lat')), to_float(s.get('lon')))
                servers.append({**s, 'd': d})
            servers.sort(key=lambda s: s['d'])
            return servers
        except Exception:
            continue
    raise RuntimeError('Cannot retrieve server list')


async def ping_server(server):
    base = server['url'][:server['url'].rfind('/')]
    times = []
    for i in range(3):
        t0 = now_ms()
        try:
            res = await http.get(f'{base}/latency.txt?x={now_ms()}.{i}', headers={'User-Agent': UA, 'Cache-Control': 'no-cache'}, timeout=5, raise_for_status=False)
            times.append(now_ms() - t0 if res.ok and res.text().strip() == 'test=test' else NO_PING)
        except Exception:
            times.append(NO_PING)
    return js_round(sum(times) / 6 * 10) / 10


async def best_server(servers):
    best, best_lat = None, float('inf')
    for s in servers[:5]:
        lat = await ping_server(s)
        if lat < best_lat:
            best_lat, best = lat, {**s, 'latency': lat}
    if not best:
        raise RuntimeError('Unable to determine best server')
    return best


async def test_download(best, cfg):
    base = best['url'][:best['url'].rfind('/')]
    urls = [f'{base}/random{s}x{s}.jpg' for s in cfg['sizes']['download'] for _ in range(cfg['counts']['download'])]
    timeout = cfg['length']['download']
    start = time.time()
    received = [0]

    async def worker():
        for url in urls:
            if time.time() - start >= timeout:
                break
            try:
                res = await http.get(bust(url), headers={'User-Agent': UA}, timeout=timeout, raise_for_status=False)
                received[0] += len(res.content)
            except Exception:
                continue
    await asyncio.gather(*[worker() for _ in range(cfg['threads']['download'])])
    return received[0] / max(time.time() - start, 0.001) * 8


def make_upload(size):
    chars = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    return 'content1=' + (chars * js_round(size / 36))[:size - 9]


async def test_upload(best, cfg):
    sizes = [s for s in cfg['sizes']['upload'] for _ in range(cfg['counts']['upload'])][:cfg['uploadMax']]
    timeout = cfg['length']['upload']
    start = time.time()
    sent = [0]

    async def worker():
        for size in sizes:
            if time.time() - start >= timeout:
                break
            try:
                await http.post(bust(best['url']), data=make_upload(size).encode(),
                                headers={'User-Agent': UA, 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': str(size)},
                                timeout=timeout, raise_for_status=False)
                sent[0] += size
            except Exception:
                continue
    await asyncio.gather(*[worker() for _ in range(cfg['threads']['upload'])])
    return sent[0] / max(time.time() - start, 0.001) * 8


async def run_speedtest():
    cfg = await get_config()
    servers = await get_servers(cfg)
    best = await best_server(servers)
    download = await test_download(best, cfg)
    upload = await test_upload(best, cfg)
    return {'isp': cfg['client']['isp'], 'ip': cfg['client']['ip'],
            'server': {'sponsor': best.get('sponsor'), 'name': best.get('name'), 'country': best.get('country'), 'd': best['d']},
            'ping': best['latency'], 'download': download, 'upload': upload}


async def handler(m, *, conn):
    await m.react('🔴')
    try:
        r = await run_speedtest()
        text = (f"> ```Testing From {js_str(r['isp'])}```\n\n────────────────\n- *`Hosted :`* {js_str(r['server']['sponsor'])}\n"
                f"- *`Location :`* {js_str(r['server']['name'])}, {js_str(r['server']['country'])} [{r['server']['d']:.2f} km]\n"
                f"- *`Ping :`* {r['ping']:.2f} ms\n- ▾ *`Inbound :`* {r['download'] / 1e6:.2f} Mb/s\n"
                f"- ▵ *`Outbound :`* {r['upload'] / 1e6:.2f} Mb/s\n\n────────────────")
        order = {'orderId': '780642630945098', 'thumbnail': await conn.resize(G.img['profile']['bot'], 150, 150), 'itemCount': 666, 'status': 1, 'surface': 1,
                 'message': '𝗦𝗣𝗘𝗘𝗗𝗧𝗘𝗦𝗧 . Ookla', 'orderTitle': 'Channel.', 'sellerJid': '6283143393763@s.whatsapp.net',
                 'token': 'AR6pyJ/fz5vRFxggGxURL7EA/vCtjKrhcJSNhHqX1iJh8A==', 'totalAmount1000': '0', 'totalCurrencyCode': 'IDR'}
        await conn.reply(m.chat, text, {'key': {'remoteJid': '0@s.whatsapp.net'}, 'message': {'orderMessage': order}})
        await m.react('🟢')
    except Exception as e:
        await m.reply(f'❌ Speedtest gagal:\n{e}')
        await m.react('🔴')


help = ['speedtest']
tags = ['info']
command = re.compile(r'^(speedtest)$', re.I)
ai = {'risk': 'low', 'summarize': True, 'description': 'Check speed response bot'}
