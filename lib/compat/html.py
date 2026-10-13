import re
from html import escape as _escape
from html.parser import HTMLParser


_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
_RAW = {'script', 'style'}
_AUTOCLOSE = {
    'li': {'li'}, 'p': {'p'}, 'tr': {'tr', 'td', 'th'}, 'td': {'td', 'th'}, 'th': {'td', 'th'},
    'option': {'option'}, 'dt': {'dt', 'dd'}, 'dd': {'dt', 'dd'}, 'thead': {'tbody', 'tfoot'},
    'tbody': {'tbody', 'tfoot', 'thead'},
}


class Node:
    __slots__ = ('type', 'name', 'attrs', 'children', 'parent', 'data')

    def __init__(self, type_, name=None, attrs=None, data=None):
        self.type = type_
        self.name = name
        self.attrs = attrs if attrs is not None else {}
        self.children = []
        self.parent = None
        self.data = data

    def append(self, child):
        child.parent = self
        self.children.append(child)

    @property
    def elements(self):
        return [c for c in self.children if c.type == 'tag']

    def text_content(self):
        if self.type == 'text':
            return self.data
        if self.type == 'comment':
            return ''
        return ''.join(c.text_content() for c in self.children)

    def inner_html(self):
        return ''.join(_serialize(c) for c in self.children)

    def outer_html(self):
        return _serialize(self)

    def descendants(self):
        for c in self.children:
            if c.type == 'tag':
                yield c
                yield from c.descendants()

    def index_in_parent(self):
        if self.parent is None:
            return 0
        return self.parent.elements.index(self)


def _serialize(node):
    if node.type == 'text':
        parent = node.parent
        if parent is not None and parent.name in _RAW:
            return node.data
        return _escape(node.data, quote=False)
    if node.type == 'comment':
        return f'<!--{node.data}-->'
    if node.type == 'root':
        return ''.join(_serialize(c) for c in node.children)
    attrs = ''.join(
        f' {k}' if v is None else f' {k}="{_escape(v, quote=True)}"' for k, v in node.attrs.items()
    )
    if node.name in _VOID:
        return f'<{node.name}{attrs}>'
    return f'<{node.name}{attrs}>{"".join(_serialize(c) for c in node.children)}</{node.name}>'


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        closers = _AUTOCLOSE.get(tag)
        if closers:
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i].name in closers:
                    del self.stack[i:]
                    break
                if self.stack[i].name in ('table', 'ul', 'ol', 'div', 'body', 'select'):
                    break
        node = Node('tag', tag, {k.lower(): (v if v is not None else '') for k, v in attrs})
        self.stack[-1].append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        node = Node('tag', tag, {k.lower(): (v if v is not None else '') for k, v in attrs})
        self.stack[-1].append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].name == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].append(Node('text', data=data))

    def handle_comment(self, data):
        self.stack[-1].append(Node('comment', data=data))


def parse(html):
    b = _Builder()
    b.feed(html or '')
    b.close()
    return b.root


_TOKEN = re.compile(
    r'\s*(?P<comb>[>+~])\s*'
    r'|(?P<ws>\s+)'
    r'|(?P<tag>\*|[a-zA-Z_][\w-]*)'
    r'|\.(?P<cls>[\w-]+)'
    r'|\#(?P<id>[\w-]+)'
    r'|\[\s*(?P<attr>[\w:.-]+)\s*(?:(?P<op>[~|^$*!]?=)\s*(?:"(?P<dq>[^"]*)"|\'(?P<sq>[^\']*)\'|(?P<bare>[^\]\s]*))\s*(?P<flag>[iI])?)?\s*\]'
    r'|:(?P<pseudo>[\w-]+)(?:\((?P<parg>(?:[^()]|\([^()]*\))*)\))?',
    re.X)


def _split_top(selector, sep=','):
    parts, depth, cur, quote = [], 0, [], None
    for ch in selector:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in '"\'':
            quote = ch
            cur.append(ch)
        elif ch in '([':
            depth += 1
            cur.append(ch)
        elif ch in ')]':
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            parts.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append(''.join(cur))
    return [p.strip() for p in parts if p.strip()]


def _parse_compound_chain(sel):
    chain = []
    cur = {'tag': None, 'cls': [], 'id': None, 'attrs': [], 'pseudos': []}
    comb = ' '
    pos = 0
    sel = sel.strip()
    pending_start = True
    while pos < len(sel):
        m = _TOKEN.match(sel, pos)
        if not m or m.end() == pos:
            raise ValueError(f'Bad selector near: {sel[pos:]!r}')
        pos = m.end()
        if m.group('comb'):
            chain.append((comb, cur))
            comb = m.group('comb')
            cur = {'tag': None, 'cls': [], 'id': None, 'attrs': [], 'pseudos': []}
        elif m.group('ws'):
            chain.append((comb, cur))
            comb = ' '
            cur = {'tag': None, 'cls': [], 'id': None, 'attrs': [], 'pseudos': []}
        elif m.group('tag'):
            cur['tag'] = m.group('tag').lower()
        elif m.group('cls'):
            cur['cls'].append(m.group('cls'))
        elif m.group('id'):
            cur['id'] = m.group('id')
        elif m.group('attr'):
            val = m.group('dq') if m.group('dq') is not None else (m.group('sq') if m.group('sq') is not None else m.group('bare'))
            cur['attrs'].append((m.group('attr').lower(), m.group('op'), val, bool(m.group('flag'))))
        elif m.group('pseudo'):
            cur['pseudos'].append((m.group('pseudo').lower(), m.group('parg')))
    chain.append((comb, cur))
    return chain


def _nth_match(expr, index):
    expr = (expr or '').strip().lower().replace(' ', '')
    if expr == 'odd':
        return index % 2 == 1
    if expr == 'even':
        return index % 2 == 0
    m = re.fullmatch(r'([+-]?\d*)n([+-]\d+)?', expr)
    if m:
        a = m.group(1)
        a = -1 if a == '-' else (1 if a in ('', '+') else int(a))
        b = int(m.group(2) or 0)
        if a == 0:
            return index == b
        k = (index - b) / a
        return k >= 0 and k == int(k)
    try:
        return index == int(expr)
    except ValueError:
        return False


def _match_simple(node, comp, scope_root):
    if node.type != 'tag':
        return False
    if comp['tag'] and comp['tag'] != '*' and node.name != comp['tag']:
        return False
    if comp['id'] and node.attrs.get('id') != comp['id']:
        return False
    if comp['cls']:
        classes = (node.attrs.get('class') or '').split()
        if not all(c in classes for c in comp['cls']):
            return False
    for name, op, val, ci in comp['attrs']:
        actual = node.attrs.get(name)
        if op is None:
            if name not in node.attrs:
                return False
            continue
        if actual is None:
            if op == '!=':
                continue
            return False
        a, v = (actual.lower(), (val or '').lower()) if ci else (actual, val or '')
        if op == '=' and a != v:
            return False
        if op == '!=' and a == v:
            return False
        if op == '^=' and not (v and a.startswith(v)):
            return False
        if op == '$=' and not (v and a.endswith(v)):
            return False
        if op == '*=' and not (v and v in a):
            return False
        if op == '~=' and v not in a.split():
            return False
        if op == '|=' and not (a == v or a.startswith(v + '-')):
            return False
    for pname, parg in comp['pseudos']:
        sibs = node.parent.elements if node.parent is not None else [node]
        idx = sibs.index(node) + 1
        if pname == 'first-child' and idx != 1:
            return False
        if pname == 'last-child' and idx != len(sibs):
            return False
        if pname == 'only-child' and len(sibs) != 1:
            return False
        if pname == 'nth-child' and not _nth_match(parg, idx):
            return False
        if pname == 'nth-last-child' and not _nth_match(parg, len(sibs) - idx + 1):
            return False
        if pname in ('first-of-type', 'last-of-type', 'nth-of-type'):
            same = [s for s in sibs if s.name == node.name]
            ti = same.index(node) + 1
            if pname == 'first-of-type' and ti != 1:
                return False
            if pname == 'last-of-type' and ti != len(same):
                return False
            if pname == 'nth-of-type' and not _nth_match(parg, ti):
                return False
        if pname == 'not' and select_matches(node, parg, scope_root):
            return False
        if pname in ('is', 'where', 'matches') and not select_matches(node, parg, scope_root):
            return False
        if pname == 'has' and not any(True for _ in select(node, parg, include_self=False)):
            return False
        if pname == 'contains':
            needle = (parg or '').strip().strip('"\'')
            if needle not in node.text_content():
                return False
        if pname == 'empty' and node.children:
            return False
        if pname == 'root' and node.parent is not scope_root:
            return False
        if pname in ('checked', 'selected', 'disabled') and pname not in node.attrs:
            return False
    return True


def _match_chain(node, chain, idx, scope_root):
    comb, comp = chain[idx]
    if not _match_simple(node, comp, scope_root):
        return False
    if idx == 0:
        return True
    prev_comb = chain[idx][0]
    if prev_comb == '>':
        p = node.parent
        return p is not None and p.type == 'tag' and _match_chain(p, chain, idx - 1, scope_root)
    if prev_comb == ' ':
        p = node.parent
        while p is not None and p.type == 'tag':
            if _match_chain(p, chain, idx - 1, scope_root):
                return True
            p = p.parent
        return False
    if prev_comb == '+':
        if node.parent is None:
            return False
        sibs = node.parent.elements
        i = sibs.index(node)
        return i > 0 and _match_chain(sibs[i - 1], chain, idx - 1, scope_root)
    if prev_comb == '~':
        if node.parent is None:
            return False
        sibs = node.parent.elements
        i = sibs.index(node)
        return any(_match_chain(s, chain, idx - 1, scope_root) for s in sibs[:i])
    return False


def _fix_combinators(chain):
    return [(' ' if i == 0 else comb, comp) for i, (comb, comp) in enumerate(chain)]


def select_matches(node, selector, scope_root=None):
    for sel in _split_top(selector):
        chain = _fix_combinators(_parse_compound_chain(sel))
        if _match_chain(node, chain, len(chain) - 1, scope_root):
            return True
    return False


def select(root, selector, include_self=False):
    seen = set()
    found = []
    candidates = []
    if include_self and root.type == 'tag':
        candidates.append(root)
    candidates.extend(root.descendants())
    parsed = []
    for sel in _split_top(selector):
        chain = _parse_compound_chain(sel)
        parsed.append(_fix_combinators(chain))
    for node in candidates:
        for chain in parsed:
            if _match_chain(node, chain, len(chain) - 1, root):
                if id(node) not in seen:
                    seen.add(id(node))
                    found.append(node)
                break
    return found


def _is_node(x):
    return isinstance(x, Node)


class Selection:
    def __init__(self, nodes, root=None):
        self._nodes = list(nodes)
        self._root = root
        self._prev = None

    def __len__(self):
        return len(self._nodes)

    @property
    def length(self):
        return len(self._nodes)

    def __iter__(self):
        return iter(self._nodes)

    def __getitem__(self, i):
        return self._nodes[i]

    def __bool__(self):
        return bool(self._nodes)

    def _new(self, nodes):
        s = Selection(nodes, self._root)
        s._prev = self
        return s

    def get(self, i=None):
        if i is None:
            return list(self._nodes)
        try:
            return self._nodes[i]
        except IndexError:
            return None

    toArray = get

    def eq(self, i):
        try:
            return self._new([self._nodes[i]])
        except IndexError:
            return self._new([])

    def first(self):
        return self._new(self._nodes[:1])

    def last(self):
        return self._new(self._nodes[-1:])

    def slice(self, start=0, end=None):
        return self._new(self._nodes[start:end])

    def end(self):
        return self._prev if self._prev is not None else self

    def find(self, selector):
        out, seen = [], set()
        for n in self._nodes:
            for x in select(n, selector):
                if id(x) not in seen:
                    seen.add(id(x))
                    out.append(x)
        return self._new(out)

    def children(self, selector=None):
        out = []
        for n in self._nodes:
            out.extend(n.elements)
        if selector:
            out = [x for x in out if select_matches(x, selector)]
        return self._new(out)

    def contents(self):
        out = []
        for n in self._nodes:
            out.extend(n.children)
        return self._new(out)

    def parent(self, selector=None):
        out, seen = [], set()
        for n in self._nodes:
            p = n.parent
            if p is not None and p.type == 'tag' and id(p) not in seen:
                if not selector or select_matches(p, selector):
                    seen.add(id(p))
                    out.append(p)
        return self._new(out)

    def parents(self, selector=None):
        out, seen = [], set()
        for n in self._nodes:
            p = n.parent
            while p is not None and p.type == 'tag':
                if id(p) not in seen and (not selector or select_matches(p, selector)):
                    seen.add(id(p))
                    out.append(p)
                p = p.parent
        return self._new(out)

    def closest(self, selector):
        out = []
        for n in self._nodes:
            c = n
            while c is not None and c.type == 'tag':
                if select_matches(c, selector):
                    out.append(c)
                    break
                c = c.parent
        return self._new(out)

    def siblings(self, selector=None):
        out = []
        for n in self._nodes:
            if n.parent is not None:
                out.extend(s for s in n.parent.elements if s is not n)
        if selector:
            out = [x for x in out if select_matches(x, selector)]
        return self._new(out)

    def _adjacent(self, step, selector=None, until_end=False):
        out = []
        for n in self._nodes:
            if n.parent is None:
                continue
            sibs = n.parent.elements
            i = sibs.index(n)
            if step > 0:
                chosen = sibs[i + 1:] if until_end else sibs[i + 1:i + 2]
            else:
                chosen = list(reversed(sibs[:i])) if until_end else sibs[max(i - 1, 0):i]
            out.extend(chosen)
        if selector:
            out = [x for x in out if select_matches(x, selector)]
        return self._new(out)

    def next(self, selector=None):
        return self._adjacent(1, selector)

    def nextAll(self, selector=None):
        return self._adjacent(1, selector, True)

    def prev(self, selector=None):
        return self._adjacent(-1, selector)

    def prevAll(self, selector=None):
        return self._adjacent(-1, selector, True)

    def filter(self, arg):
        if callable(arg):
            return self._new([n for i, n in enumerate(self._nodes) if arg(i, n)])
        if isinstance(arg, Selection):
            ids = {id(x) for x in arg._nodes}
            return self._new([n for n in self._nodes if id(n) in ids])
        return self._new([n for n in self._nodes if select_matches(n, arg)])

    def not_(self, arg):
        if callable(arg):
            return self._new([n for i, n in enumerate(self._nodes) if not arg(i, n)])
        return self._new([n for n in self._nodes if not select_matches(n, arg)])

    def has(self, selector):
        return self._new([n for n in self._nodes if select(n, selector)])

    def is_(self, arg):
        if callable(arg):
            return any(arg(i, n) for i, n in enumerate(self._nodes))
        return any(select_matches(n, arg) for n in self._nodes)

    def add(self, other):
        extra = other._nodes if isinstance(other, Selection) else (select(self._root, other, False) if isinstance(other, str) else [other])
        merged = list(self._nodes)
        ids = {id(x) for x in merged}
        for x in extra:
            if id(x) not in ids:
                merged.append(x)
        return self._new(merged)

    def each(self, fn):
        for i, n in enumerate(list(self._nodes)):
            if fn(i, n) is False:
                break
        return self

    def map(self, fn):
        out = []
        for i, n in enumerate(self._nodes):
            r = fn(i, n)
            if r is not None:
                out.append(r)
        return _MapResult(out)

    def text(self, value=None):
        if value is None:
            return ''.join(n.text_content() for n in self._nodes)
        for n in self._nodes:
            n.children = []
            n.append(Node('text', data=str(value)))
        return self

    def html(self, value=None):
        if value is None:
            return self._nodes[0].inner_html() if self._nodes else None
        for n in self._nodes:
            n.children = []
            for c in parse(str(value)).children:
                n.append(c)
        return self

    def outerHtml(self):
        return self._nodes[0].outer_html() if self._nodes else None

    def attr(self, name, value=None):
        if value is None:
            if not self._nodes:
                return None
            return self._nodes[0].attrs.get(name.lower())
        for n in self._nodes:
            n.attrs[name.lower()] = str(value)
        return self

    def removeAttr(self, name):
        for n in self._nodes:
            n.attrs.pop(name.lower(), None)
        return self

    def prop(self, name):
        if not self._nodes:
            return None
        n = self._nodes[0]
        if name == 'tagName':
            return (n.name or '').upper()
        if name == 'outerHTML':
            return n.outer_html()
        if name == 'innerHTML':
            return n.inner_html()
        return n.attrs.get(name)

    def data(self, key=None):
        if not self._nodes:
            return None
        attrs = self._nodes[0].attrs
        if key is None:
            return {k[5:]: v for k, v in attrs.items() if k.startswith('data-')}
        return attrs.get('data-' + re.sub(r'([A-Z])', lambda m: '-' + m.group(1).lower(), key))

    def val(self):
        if not self._nodes:
            return None
        n = self._nodes[0]
        if n.name == 'textarea':
            return n.text_content()
        return n.attrs.get('value')

    def hasClass(self, name):
        return any(name in (n.attrs.get('class') or '').split() for n in self._nodes)

    def addClass(self, name):
        for n in self._nodes:
            cur = (n.attrs.get('class') or '').split()
            for c in name.split():
                if c not in cur:
                    cur.append(c)
            n.attrs['class'] = ' '.join(cur)
        return self

    def removeClass(self, name):
        for n in self._nodes:
            cur = [c for c in (n.attrs.get('class') or '').split() if c not in name.split()]
            n.attrs['class'] = ' '.join(cur)
        return self

    def remove(self, selector=None):
        targets = self._nodes if not selector else [n for n in self._nodes if select_matches(n, selector)]
        for n in targets:
            if n.parent is not None:
                n.parent.children = [c for c in n.parent.children if c is not n]
                n.parent = None
        return self

    def empty(self):
        for n in self._nodes:
            n.children = []
        return self

    def index(self):
        return self._nodes[0].index_in_parent() if self._nodes else -1

    def append(self, content):
        for n in self._nodes:
            for c in (parse(content).children if isinstance(content, str) else [content]):
                n.append(c)
        return self

    def prepend(self, content):
        for n in self._nodes:
            new = parse(content).children if isinstance(content, str) else [content]
            for c in new:
                c.parent = n
            n.children = new + n.children
        return self

    def clone(self):
        return self._new([parse(n.outer_html()).children[0] for n in self._nodes if n.type == 'tag'])

    def serialize(self):
        return self.outerHtml()

    def toString(self):
        return self.outerHtml() or ''


class _MapResult(list):
    def get(self, i=None):
        if i is None:
            return list(self)
        try:
            return self[i]
        except IndexError:
            return None

    toArray = get

    def join(self, sep=','):
        return sep.join(str(x) for x in self)

    def filter(self, fn):
        return _MapResult([x for x in self if fn(x)])

    def map(self, fn):
        return _MapResult([fn(x) for x in self])


class Cheerio:
    def __init__(self, root):
        self.root = root

    def __call__(self, arg=None, context=None):
        if arg is None:
            return Selection([], self.root)
        if isinstance(arg, Selection):
            return arg
        if _is_node(arg):
            return Selection([arg], self.root)
        if isinstance(arg, (list, tuple)):
            return Selection([x for x in arg if _is_node(x)], self.root)
        if isinstance(arg, str):
            if arg.lstrip().startswith('<'):
                return Selection([c for c in parse(arg).children if c.type == 'tag'], self.root)
            base = self.root
            if context is not None:
                if isinstance(context, Selection):
                    out, seen = [], set()
                    for n in context._nodes:
                        for x in select(n, arg):
                            if id(x) not in seen:
                                seen.add(id(x))
                                out.append(x)
                    return Selection(out, self.root)
                if _is_node(context):
                    base = context
            return Selection(select(base, arg), self.root)
        return Selection([], self.root)

    def html(self, arg=None):
        if arg is None:
            return self.root.inner_html()
        if isinstance(arg, Selection):
            return arg.outerHtml()
        if _is_node(arg):
            return arg.outer_html()
        return ''

    def text(self, arg=None):
        if arg is None:
            return self.root.text_content()
        if isinstance(arg, Selection):
            return arg.text()
        if _is_node(arg):
            return arg.text_content()
        return ''

    def xml(self, arg=None):
        return self.html(arg)

    def root_(self):
        return Selection([self.root], self.root)


def load(html):
    return Cheerio(parse(html))


def attr(node, name):
    return node.attrs.get(name)


def text(node):
    return node.text_content()
