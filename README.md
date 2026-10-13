<div align=center>
<a href="#"><img src="https://files.catbox.moe/bisfzj.png"/></a> 

  <a href="https://github.com/whiskeysockets/baileys"><img height="22" src="https://img.shields.io/badge/Baileys-000000?style=for-the-badge&logo=whatsapp&logoColor=green"/></a><a href="#"><img height="22" src="https://img.shields.io/badge/Python-000000.svg?&style=for-the-badge&logo=python&logoColor=yellow"/></a><a href="https://gemini.google.com"><img height="22" src="https://img.shields.io/badge/Gemini-000000?style=for-the-badge&logo=googlegemini&logoColor=blue"/></a><a href="https://cloudflare.com"><img height="22" src="https://img.shields.io/badge/Cloudflare-000000?style=for-the-badge&logo=Cloudflare&logoColor=orange"/></a>
</div>

> [!IMPORTANT]
> Please read the [<code>LICENSE</code>](https://github.com/HirooSy/HIROBOT/blob/main/LICENSE) before using this project! You're free to recode it, but do not sell or claim this project as your own. <b>Do not change the License.</b> if you modify it, <b>please credit me in your License.</b>

> [!NOTE]
> High-performance WhatsApp bot with AI agent, VoIP calling capabilities, and a website feature. Built with a Python Baileys and Python 3.10+.
> 
> ---
> 
> <p align=center><b>Features:</b></p>
> 
> - [x] AI Agent Using Gemini.
> - [x] Audio & Video Call.
> - [x] Multi Sessions.
> - [x] Database SQLite / MongoDB / MySql.
> - [x] Support AI Rich and Button Message.
> - [x] Cloudflared Tunnel Website.
> - [x] Lightweight & High-performance.

---

<table align=center height=100>
  <td>
  <sub>
    
```env
         ---Project Structure---
HIROBOT
├── 📁lib
│   ├── 📁compat
│   ├── 📁package
│   │   ├── 📁ai
│   │   ├── 📁baileys_lite
│   │   └── 📁website
│   ├── 📁scrapers
│   ├── 📁utils
│   ├── 📄config.py          # bot's preference
│   ├── 📄main.py            # bot process
│   └── 📄start.py           # supervisor (run this)
├── 📁data
│   ├── 📁sessions
│   ├── 📁tunnel
│   └── 📁tmp
├── 📁plugins
├── 📄.env                   # your tokens
├── 📄.env.example
├── 📄LICENSE
└── 📄README.md
```
</sub>

  </td>
</table>


<details> 
  <summary align=left><b>About AI Agent</b></summary>
<p align=center>──────────────</p>

<h4 align=center >How AutoHeal Works?</h4>

> ```mermaid
> flowchart LR
>     A@{ shape: odd, label: "User Command" } --> Error
>     Error process@==> C@{ shape: diamond, label: "Gemini Server
> Gemma-4-31b-it"}
>     C --> D@{ shape: circle, label: "⏳" }
>     D ==> E[✅ Write and save]
>     D ==> F[❌ Stop autoheal]
>     E --> G[Done]
>     F --> H[Note it as failure]
>
> process@{ animate: true }
> style Error stroke:#f00
> ```

<table align=center>
  <tr>
    <td>default — gemini-3.1-flash-lite</td>
    <td>Daily Conversation</td>
  </tr>
  <tr>
    <td>flash — gemini-3.5-flash</td>
    <td>Daily conversation but more complex</td>
  </tr>
  <tr>
    <td>pro — gemini-2.5-pro</td>
    <td>Complex reasoning and analysis</td>
  </tr>
 <tr>
    <td>gemma — gemma-4-31b-it</td>
    <td>AutoHeal system and coding</td>
  </tr>
  <tr>
    <td>gemma-moe — gemma-4-26b-a4b-it</td>
    <td>AutoHeal system and coding</td>
  </tr>
</table>

<h4 align=center>How to add a new tool</h4>

<details>
  <summary align=center><sub>All MCP Helper</sub></summary>
  
<table>
  <thead>
    <tr>
      <th>Category</th>
      <th>Function</th>
      <th>Description</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="3"><strong>Session &amp; Chat History</strong></td>
      <td><code>get_session(jid)</code></td>
      <td>get chat history list for a chat</td>
    </tr>
    <tr>
      <td><code>reset_session(jid)</code></td>
      <td>clear chat history for a chat</td>
    </tr>
    <tr>
      <td><code>get_pinned_notes_readonly(jid)</code></td>
      <td>get notes pinned to a chat</td>
    </tr>
    <tr>
      <td rowspan="4"><strong>Talking to the AI / Agent Loop</strong></td>
      <td><code>run_agent(conn, m, text, opts)</code></td>
      <td>run a full AI turn, get a reply</td>
    </tr>
    <tr>
      <td><code>run_agent_confirmed(conn, m, opts)</code></td>
      <td>resume an agent turn awaiting confirmation</td>
    </tr>
    <tr>
      <td><code>call_tool(name, args)</code></td>
      <td>call another registered tool by name</td>
    </tr>
    <tr>
      <td><code>list_tools() / count_tools()</code></td>
      <td>list / count registered tools</td>
    </tr>
    <tr>
      <td rowspan="3"><strong>Identity &amp; Permissions</strong></td>
      <td><code>read_group_settings(group_jid)</code></td>
      <td>read group settings from brain storage</td>
    </tr>
    <tr>
      <td><code>read_owner_list()</code></td>
      <td>list registered bot owners</td>
    </tr>
    <tr>
      <td><code>get_dangerous_doc_reason(m)</code></td>
      <td>check if a message/doc looks risky</td>
    </tr>
    <tr>
      <td rowspan="2"><strong>Persistent Storage (&quot;brain&quot;)</strong></td>
      <td><code>load_brain() / save_brain(brain)</code></td>
      <td>read/write the brain file</td>
    </tr>
    <tr>
      <td><code>ctx()</code></td>
      <td>current turn context (see below)</td>
    </tr>
    <tr>
      <td rowspan="7"><strong>Web &amp; Media</strong></td>
      <td><code>search_web_grounded(query)</code></td>
      <td>grounded web search</td>
    </tr>
    <tr>
      <td><code>capture_website_screenshot(url)</code></td>
      <td>screenshot a webpage</td>
    </tr>
    <tr>
      <td><code>peek_fetch_buffer(url, headers)</code></td>
      <td>peek a file buffer from a URL</td>
    </tr>
    <tr>
      <td><code>peek_fetch_video_buffer(url, max_bytes, headers)</code></td>
      <td>peek a video buffer from a URL</td>
    </tr>
    <tr>
      <td><code>detect_platform(url)</code></td>
      <td>detect platform (YouTube/TikTok/etc)</td>
    </tr>
    <tr>
      <td><code>peek_analyze_with_vision(media_items, platform, url, context)</code></td>
      <td>analyze media with vision model</td>
    </tr>
    <tr>
      <td><code>build_media_part(m)</code></td>
      <td>extract image/video/audio from a message</td>
    </tr>
    <tr>
      <td><code>fetch_social_multi(url)</code></td>
      <td>download helper for social media</td>
    </tr>
    <tr>
      <td rowspan="2"><strong>Plugin Execution (Advanced/Internal)</strong></td>
      <td><code>exec_plugin_command(command, args_str, confirmed, capture_output)</code></td>
      <td>run an existing bot plugin/command</td>
    </tr>
    <tr>
      <td><code>exec_eval(code, silent)</code></td>
      <td>evaluate raw Python code (owner-only, dangerous)</td>
    </tr>
    <tr>
      <td rowspan="4"><strong>Error Handling &amp; Internals</strong></td>
      <td><code>get_api_keys() / get_next_key(scope) / rotate_key()</code></td>
      <td>API key pool management</td>
    </tr>
    <tr>
      <td><code>reset_rate_limit(jid)</code></td>
      <td>reset the rate limit for a chat</td>
    </tr>
    <tr>
      <td><code>get_personality()</code></td>
      <td>get bot's configured personality/system prompt</td>
    </tr>
    <tr>
      <td><code>MODELS</code></td>
      <td>map of available AI models</td>
    </tr>
  </tbody>
</table>

</details>

<sub>

```python
from lib.package.ai import mcp


async def _exec_check_weather(args=None, **kw):
    a = {**(args or {}), **kw}
    ctx = mcp.ctx()
    if not ctx.get('currentJid'):
        return 'Chat context not available'
    city = str(a.get('city'))
    return f'Weather in {city}: sunny, 30°C'


default = [
    {
        'name': 'check_weather',
        'description': 'Check the weather for a specific city. Use it when a user asks for the weather, e.g., "What\'s the weather like in Jakarta?"',
        'parameters': {
            'city': {'type': 'string', 'description': 'City name, e.g. "Jakarta"', 'required': True},
        },
        'execute': _exec_check_weather,
    },
]
```
</sub>

</details>

<details>
   <summary align=left><b>About Website</b></summary>
   <p align=center>──────────────</p>

<h4>.env Settings for Website</h4>

<table align="center">
  <tbody>
    <tr>
      <td rowspan="5"><b>SCENARIO 1</b><br><sub>No public address, using own domain</sub></td>
      <td colspan="2">
        
> [!NOTE]
> <sub>Your panel does <b>not</b> give a real public address/IP (e.g. Pterodactyl, where the address is just <code>name:port</code>, internal-only). The bot still runs <code>cloudflared</code> (binary auto-downloaded, no manual install) to open a quick tunnel — your custom domain sits in front of it via a Cloudflare Worker + KV Namespace, instead of a regular DNS record.</sub>

</td>
    </tr>
    <tr>
      <td><code>CF_KV_TOKEN</code></td>
      <td>Cloudflare API Token with <b>Account &gt; Workers KV Storage &gt; Edit</b> permission, from <a href="https://dash.cloudflare.com/profile/api-tokens">dash.cloudflare.com/profile/api-tokens</a></td>
    </tr>
    <tr>
      <td><code>CF_ACCOUNT_ID</code></td>
      <td>Found on the Workers &amp; Pages overview page</td>
    </tr>
    <tr>
      <td><code>CF_KV_NAMESPACE_ID</code></td>
      <td>Found on your KV Namespace's page</td>
    </tr>
    <tr>
      <td><code>CF_HOSTNAME</code></td>
      <td>Required for the domain that becomes your bot's main address, e.g. <code>bot.yourdomain.com</code></td>
    </tr>
    <tr>
      <td rowspan="4"><b>SCENARIO 2</b><br><sub>DNS-pointable server, using own domain</sub></td>
      <td colspan="3">
        
> [!NOTE]
> <sub>Your panel/server <b>can</b> be pointed to via a DNS record (a VPS with a fixed IP, or a panel supporting Cloudflare Named Tunnel / Zero Trust). Requires card verification on Cloudflare Zero Trust (free forever, no charge unless you exceed free-tier limits).</sub>
</td>
    </tr>
    <tr>
      <td><code>CF_TOKEN</code></td>
      <td>Named Tunnel token from Cloudflare Zero Trust</td>
    </tr>
    <tr>
      <td><code>CF_HOSTNAME</code></td>
      <td>Required for the domain that becomes your bot's main address, e.g. <code>bot.yourdomain.com</code></td>
    </tr>
    <tr>
      <td><code>HOSTNAME_PUBLIC</code></td>
      <td>Fill it using same value as CF_HOSTNAME <code>bot.yourdomain.com</code></td>
    </tr>
    <tr>
      <td><b>SCENARIO 3</b><br><sub>No own domain, free provider</sub></td>
      <td colspan="2">
        
> [!NOTE]
> <sub><code>HOSTNAME_PUBLIC</code> — you don't own a custom domain, but have a free public address from another provider (e.g. <code>my.zone.id</code>, <code>is.dev</code>) already pointed (A/CNAME) at your panel's real public address. Fill in <b>only</b> this one, leave everything above empty — the bot will not run cloudflared/any tunnel at all.</sub>
      </td>
    </tr>
    <tr>
      <td><b>SCENARIO 4</b><br><sub>Default, nothing configured</sub></td>
      <td colspan="2">
      
> [!NOTE]
> Nothing above is filled in — the bot automatically uses a free <code>trycloudflare.com</code> URL that changes every time it restarts. No setup required.</td>
    </tr>
  </tbody>
</table>

</details>

<details>
   <summary align=left><b>Message types</b></summary>
   <p align=center>──────────────</p>

<details> <summary>📖 Basic</summary>
  <sub>
    
```python
await conn.reply(m.chat, 'Hello world!', m)

# @Media
# URL — 'https://example.com/audio.mp3'
# Local — '/path/to/video.mp4'
#
# @Options
# send as document — options={'asDocument': True}
# send as voicenote — ptt=True (the sixth argument, right after m)
await conn.sendFile(m.chat, media, 'file.png', 'hello world!', m, options={'asDocument': True})
await conn.sendFile(m.chat, media, 'voice.mp3', '', m, True)

await conn.sendContact(m.chat, [
    ['6281234567890', 'HirooSy'],
    ['6289876543210', 'Hiro'],
], m)

# react is synchronous, do not await it
conn.react(m.chat, '👍', m.key)
```
</sub></details>

<details> <summary>📍 Location</summary>
  <sub>
  
```python
await conn.sendLocation(m.chat, 'https://example.com/thumb.jpg', 'Title', 'Address', m)
```

</sub></details>

<details> <summary>🧾 Product</summary>
  <sub>
 
```python
# @Media
# String: "https://example.com/img.png" or "./image.png"
# Array: [ "https://example.com/", "./img.png" ]
await conn.sendProduct(m.chat, media, 'Title', 'Description', m, {
    'businessOwnerJid': '0@s.whatsapp.net',
    'currencyCode': 'USD',
    'priceAmount1000': 20,
    'retailerId': 'Code Promo',
})
```

</sub></details>

<details> <summary>🖼️ Url Preview</summary>
  <sub>
    
```python
await conn.sendUrlPreview(
    m.chat,
    'https://example.com/thumb.jpg',
    'https://example.com Hello World!',
    'Url Preview Title',
    'Url Description',
    'IMAGE',   # True for highQuality, or ['IMAGE', True]
    m,
)
```
</sub></details>

<details> <summary>🛒 Carousel</summary>
  <sub>
    
```python
await conn.sendButton(m.chat, {
    'text': 'Interactive with Carousel!',
    'footer': 'HirooSy',
    'cards': [
        {
            'image': {'url': './path/to/image.jpg'},
            'caption': 'Image 1',
            'footer': 'Image 1',
            'nativeFlow': [{'text': 'Source', 'url': 'https://example.com', 'useWebview': True}],
        },
        {
            'image': {'url': 'https://example.com/image.png'},
            'caption': 'Image 2',
            'footer': 'Image 2',
            'ltoText': 'New Coupon!',
            'ltoCode': 'HiroBot',
            'ltoUrl': 'https://example.com',
            'nativeFlow': [{'text': 'Source', 'url': 'https://example.com'}],
        },
    ],
}, m)
```
</sub></details>

<details> <summary>🔖 NativeFlow Button</summary>
  <sub>

```python
await conn.sendButton(m.chat, {
    'image': {'url': './path/to/image.jpg'},
    'caption': 'Interactive!',
    'footer': 'My Bot',
    'optionText': 'Select Options',
    'optionTitle': 'Select Options',
    'ltoText': 'HirooSy',
    'ltoCode': 'Hiro bot',
    'ltoUrl': 'https://example.com',
    'nativeFlow': [
        {'text': '👋🏻 Greeting', 'id': '#Greeting'},
        {'text': '📞 Call', 'call': '628123456789'},
        {'text': '📋 Copy', 'copy': 'Hiro bot'},
        {'text': '🌐 Source', 'url': 'https://example.com', 'useWebview': True},
        {
            'text': '📋 Select',
            'sections': [
                {'title': '✨ Section 1', 'rows': [{'header': '', 'title': '🏷️ Coupon', 'description': '', 'id': '#CouponCode'}]},
                {'title': '✨ Section 2', 'highlight_label': '🔥 Popular', 'rows': [{'header': '', 'title': '💭 Secret Ingredient', 'description': '', 'id': '#SecretIngredient'}]},
            ],
        },
    ],
}, m)
```
</sub></details>

<details><summary>❇️ Widget</summary>

<sub>
  
```python
await conn.sendButton(m.chat, {
    'text': 'Full demo of all widget components',
    'footer': 'A2UI Showcase',
    'widget': {
        'align': 'center',
        'fallback': 'Widget cannot be loaded on this device',  # optional
        'items': [
            # --- TEXT ---
            {'text': 'Welcome to the Widget Demo', 'variant': 'title'},
            {'text': 'This is a longer description explaining what the widget contains.', 'variant': 'body'},
            {'text': 'Small caption', 'variant': 'caption'},

            # --- ICON ---
            {'icon': 'info'},                              # -> '[i]'
            {'icon': 'warning', 'style': 'symbol'},        # -> '!'
            {'icon': 'favorite', 'native': True},          # uses native Icon component

            # --- IMAGE ---
            {'image': 'https://example.com/banner.jpg', 'variant': 'header', 'fit': 'cover', 'description': 'Promo banner'},

            # --- VIDEO ---
            {'video': 'https://example.com/preview.mp4'},

            # --- AUDIO ---
            {'audio': 'https://example.com/audio.mp3', 'description': 'Listen to this audio'},

            # --- DIVIDER ---
            {'divider': 'horizontal'},
            {'divider': 'vertical'},

            # --- BUTTON ---
            {'button': 'Open Website', 'url': 'https://example.com', 'variant': 'primary'},

            # --- TEXT INPUT ---
            {'input': 'name', 'label': 'Enter your name', 'value': '', 'variant': 'shortText'},
            {'input': 'message', 'label': 'Long message', 'variant': 'longText', 'value': ''},
            {'input': 'email', 'label': 'Email', 'variant': 'shortText', 'validationRegexp': r'^[^@]+@[^@]+\.[^@]+$'},

            # --- CHECKBOX ---
            {'checkbox': 'I agree to the terms & conditions', 'value': False},

            # --- CHOICE PICKER ---
            {
                'choice': ['Red', 'Green', 'Blue'],
                'label': 'Pick your favorite color',
                'variant': 'mutuallyExclusive',
                'displayStyle': 'chips',
                'filterable': True,
            },
            {
                'choice': [
                    {'label': 'Option A', 'value': 'a'},
                    {'label': 'Option B', 'value': 'b'},
                    {'label': 'Option C', 'value': 'c'},
                ],
                'label': 'Pick multiple (multi-select)',
                'variant': 'multipleSelection',
                'value': ['a', 'b'],
            },

            # --- SLIDER ---
            {'slider': 100, 'min': 0, 'max': 100, 'value': 50, 'label': 'Volume'},

            # --- DATETIME ---
            {'datetime': True, 'label': 'Pick a date', 'value': '', 'enableDate': True, 'enableTime': False},

            # --- ROW (horizontal) ---
            {'row': [{'text': 'Left'}, {'text': 'Center'}, {'text': 'Right'}], 'justify': 'space-between', 'align': 'center'},

            # --- COLUMN (vertical) ---
            {'column': [{'text': 'Row 1'}, {'text': 'Row 2'}, {'text': 'Row 3'}], 'justify': 'start', 'align': 'stretch'},

            # --- LIST ---
            {'list': [{'text': '• First item'}, {'text': '• Second item'}, {'text': '• Third item'}], 'direction': 'vertical'},
        ],
    },
}, quoted)
```
</sub>
  
</details>

<details> <summary>🗓️ AI Rich</summary>
  <sub>

```python
await (
    conn.aiRich()
    .setTitle('Ai Rich Message')
    .addText('[HyperLink](https://example.com)\nCitation [](https://example.com)\n[x^2+y^2=r^2|100|100](https://example.com/latex.png)')
    .addImage('https://example.com/image.png')
    .addCode('python', "print('Hello World')")
    .addHtml(['<html>Hello world</html>', 'Tab 1'], ['<html>Hi twin</html>', 'Tab 2'])
    .addTable([
        ['Name', 'HirooSy'],
        ['Bio', 'Im developer'],
        ['Age', '67'],
    ])
    .addSource([['https://example.com/favicon.ico', 'https://example.com', 'Source']])
    .addTip('Tip Text')
    .addSuggest(['Continue', 'Cancel'])
    .send(m.chat, {'quoted': m})
)

# animated progress
await conn.aiRich().addProcess('Loading...').send(m.chat)
```
</sub></details>

<details> <summary>📦 Sticker</summary>
  <sub>

```python
# @Media
# URL — 'https://example.com/image.png'
# Local — '/path/to/image.png'

# Sticker
await conn.sendSticker(m.chat, media, {'packname': 'Hiro', 'author': 'Bot'}, m)

# StickerPack
await conn.sendStickerPack(m.chat, {
    'cover': {'url': media},
    'stickers': [
        {'data': {'url': media}},
        {'data': {'url': media}},
    ],
    'name': 'My Sticker Pack',
    'publisher': 'Publisher stickerpack',
    'description': 'Description pack',
})
```
</sub></details>

<details> <summary>📞 Call</summary>
  <sub>

```python
# @Media
# URL — 'https://example.com/audio.mp3'
# Local — '/path/to/video.mp4'
# Support Array — [ url, path ]
from baileys_lite.voip import Voip

voip = Voip(conn, {'voipLogLevel': 'info'})

call = await voip.call(m.sender, media, '720p')

# hang up
await call.hangup()

# mute
call.mute()

# silent (mute mic and pause video)
await call.silent()
```
</sub></details>
</details>

<details>
   <summary><b>Install and Run</b></summary><br>

<table align="center">
  <tbody>
    <tr>
      <td rowspan="3"><b>REQUIREMENT</b></td>
      <td><b>Server</b></td>
      <td colspan="2">100% CPU Limit, 700MB RAM, 1GB Storage<br> <code>-</code></td>
    </tr>
    <tr>
      <td><b>Python</b></td>
      <td colspan="2">Python 3.10+ (standard library only, no pip install needed) <br> <code>pkg install python</code></td>
    </tr>
    <tr>
      <td><b>FFMPEG</b></td>
      <td colspan="2">latest, <code>ffmpeg</code> and <code>ffprobe</code> must be on PATH (sticker, audio, video, brat) <br> <code>pkg install ffmpeg</code></td>
    </tr>
  </tbody>
</table>

<h4>Configuration environment (.env)</h4>

1. Copy `.env.example` to `.env`
2. Fill it with your values:

<table align=center height=100>
  <td>
<sub>
  
```env
OWNER=6281234567890,6281234567890
MODERATOR=6281234567890,6281234567890

# Support mongodb/MySql/Local file sql
# • mongodb+srv://...
# • mysql://...
# • dont fill it to use local file sql
DATABASE=

AI_PERSONALITY=

# Gemini API Key (Free, Get from aistudio.google.com)
# Accepts single string or Array for fallback
AI_KEYS=

# OPTIONAL ENV
#__________________________________________________

GIT_CLASSIC_KEY=
GIT_TOKEN=
GIT_USER=
GIT_EMAIL=
GIT_REPO=

# TUNNEL / CUSTOM DOMAIN (optional)
#__________________________________________________
# See README.md -> "About Website" for full explanation of each scenario.

CF_KV_TOKEN=
CF_ACCOUNT_ID=
CF_KV_NAMESPACE_ID=

CF_TOKEN=
CF_HOSTNAME=

HOSTNAME_PUBLIC=
```
</sub>
</td>
</table>

<h4>Run</h4>

<sub align=left>

```bash
$ git clone https://github.com/HirooSy/HIROBOT.git
$ cd HIROBOT
$ cp .env.example .env
$ python lib/start.py
```
</sub>

<div align=center>

  <a href="#"><img height="25" align=right src="https://img.shields.io/badge/Size-20_MB-black?style=for-the-badge"/> </a>
  
  <a href="https://wispbyte.com"><img height="25" align=left src="https://img.shields.io/badge/Deploy-black?style=for-the-badge&logo=4chan&logoColor=blue"/></a>

</div>

</details>