from lib.utils.handler import G


async def handler(m, *, conn):
    url = G.getServerUrl() if callable(G.getServerUrl) else 'https://not-loaded.yet'
    await conn.sendButton(m.chat, {
        'text': f"Hi {G.db.data['users'][m.sender]['name']}, Sorry register only available on website",
        'nativeFlow': [{'text': 'Click Here', 'url': url}],
    }, m)


help = command = ['daftar', 'register']
tags = ['main']
