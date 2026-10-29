"""极简 HTML DOM 构建与查询。仅依赖标准库，避免每日定时任务受第三方包影响。"""
from html.parser import HTMLParser
import re

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
SKIP_TAGS = {"script", "style", "noscript"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent

    # ---- 属性 ----
    @property
    def cls(self):
        return self.attrs.get("class", "") or ""

    def get(self, key, default=""):
        return self.attrs.get(key, default)

    # ---- 遍历 ----
    def walk(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.walk()

    def find_all(self, tag=None, cls=None, id_=None):
        out = []
        for n in self.walk():
            if n is self:
                continue
            if tag and n.tag != tag:
                continue
            if cls and cls not in n.cls.split():
                continue
            if id_ and n.attrs.get("id") != id_:
                continue
            out.append(n)
        return out

    def find(self, tag=None, cls=None, id_=None):
        r = self.find_all(tag, cls, id_)
        return r[0] if r else None

    # ---- 文本 ----
    def text(self, sep="", strip=True):
        parts = []

        def rec(n):
            for c in n.children:
                if isinstance(c, str):
                    parts.append(c)
                elif c.tag == "br":
                    parts.append("\n")
                elif c.tag in SKIP_TAGS:
                    continue
                else:
                    rec(c)

        rec(self)
        s = sep.join(p.strip() for p in parts if p.strip()) if sep else "".join(parts)
        return re.sub(r"[ \t\u3000]+", " ", s).strip() if strip else s

    def __repr__(self):
        return f"<Node {self.tag} class={self.cls!r} children={len(self.children)}>"


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def parse(html):
    b = _Builder()
    try:
        b.feed(html)
        b.close()
    except Exception:
        pass
    return b.root
