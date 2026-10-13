import re

from lib.utils.handler import G


async def handler(m, *, usedPrefix, command, text):
    p, command, text = usedPrefix, command, text
    chat = G.db.data['chats'][m.chat]
    if re.search('setwelcome', command, re.I):
        if not text:
            return await m.reply(f'\n> Contoh Penggunaan: {p + command} <text & options>\n> *Options :*\n  - @user\n  - @subject\n  - @decription\n\n> *Contoh Text :*\n   Welcome To @subject, @user!')
        await m.reply('successfully applied as welcome')
        chat['sWelcome'] = text
    if re.search('setbye', command, re.I):
        if not text:
            return await m.reply(f'\n> Contoh Penggunaan: {p + command} <text & options>\n> *Options :*\n  - @user\n  - @subject\n  - @decription\n\n> *Contoh Text :*\n   Bye @user!')
        await m.reply('successfully applied as bye')
        chat['sBye'] = text


help = ['setbye <text>', 'setwelcome <text>']
tags = ['owner']
command = re.compile(r'^set(welcome|bye)$', re.I)
group = True
admin = True
