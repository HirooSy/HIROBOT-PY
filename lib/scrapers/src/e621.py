import math
import random
import re
import sys

from lib.compat import http


AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1.1 Safari/605.1.15',
]
POOL_PAGE_SIZE = 30


def getRandomIP():
    return '.'.join(str(random.randrange(256)) for _ in range(4))


def getRandomUserAgent():
    return random.choice(AGENTS)


def getHeaders():
    return {
        'User-Agent': getRandomUserAgent(),
        'Accept': '*/*',
        'Referer': 'https://e621.net/',
        'X-Forwarded-For': getRandomIP(),
        'X-Real-IP': getRandomIP(),
    }


def mapPostData(post):
    if not post:
        return None
    f = post.get('file') or {}
    return {
        'id': post.get('id'),
        'url': f.get('url') or None,
        'pageUrl': f"https://e621.net/posts/{post.get('id')}",
        'previewUrl': (post.get('preview') or {}).get('url') or (post.get('sample') or {}).get('url') or None,
        'ext': f.get('ext') or 'unknown',
        'size': f.get('size') or 0,
        'rating': post.get('rating') or '?',
        'favCount': post.get('fav_count') or 0,
        'tags': post.get('tags') or {},
    }


map_post = mapPostData


def _obj(value):
    return value if isinstance(value, dict) else {}


async def tagsSearch(keywords, page=1):
    try:
        res = await http.get('https://e621.net/posts.json', params={'tags': keywords, 'limit': 30, 'page': page},
                             headers=getHeaders(), timeout=15)
        posts = _obj(res.json()).get('posts')
        if not posts:
            return None
        return [mapPostData(p) for p in posts]
    except Exception as error:
        print('[E621 Scraper] Search error:', error, file=sys.stderr)
        return None


async def getPost(url):
    match = re.search(r'/posts/(\d+)', url or '')
    if not match:
        return None
    try:
        res = await http.get(f'https://e621.net/posts/{match.group(1)}.json', headers=getHeaders(), timeout=15)
        return mapPostData(_obj(res.json()).get('post'))
    except Exception as error:
        print('[E621 Scraper] GetPost error:', error, file=sys.stderr)
        return None


async def getPoolInfo(poolId):
    try:
        res = await http.get(f'https://e621.net/pools/{poolId}.json', headers=getHeaders(), timeout=15)
        pool = res.json()
        if not pool or not isinstance(pool.get('post_ids'), list):
            return None
        return {
            'id': pool.get('id'),
            'name': (pool.get('name') or f"Pool {pool.get('id')}").replace('_', ' '),
            'description': pool.get('description') or '',
            'category': pool.get('category') or 'series',
            'isActive': pool.get('is_active') is not False,
            'postIds': pool['post_ids'],
            'postCount': len(pool['post_ids']),
        }
    except Exception as error:
        if getattr(error, 'status', None) != 404:
            print('[E621 Scraper] GetPoolInfo error:', error, file=sys.stderr)
        return None


async def getPoolPage(poolId, page=1):
    info = await getPoolInfo(poolId)
    if not info:
        return None
    start = (page - 1) * POOL_PAGE_SIZE
    idsSlice = info['postIds'][start:start + POOL_PAGE_SIZE]
    totalPages = max(1, math.ceil(info['postCount'] / POOL_PAGE_SIZE))
    if not idsSlice:
        return {**info, 'posts': [], 'page': page, 'totalPages': totalPages}
    try:
        res = await http.get('https://e621.net/posts.json',
                             params={'tags': 'id:' + ','.join(str(i) for i in idsSlice), 'limit': len(idsSlice)},
                             headers=getHeaders(), timeout=15)
        raw = _obj(res.json()).get('posts') or []
        byId = {p.get('id'): mapPostData(p) for p in raw}
        posts = [byId[i] for i in idsSlice if byId.get(i)]
        return {**info, 'posts': posts, 'page': page, 'totalPages': totalPages}
    except Exception as error:
        print('[E621 Scraper] GetPoolPage error:', error, file=sys.stderr)
        return None
