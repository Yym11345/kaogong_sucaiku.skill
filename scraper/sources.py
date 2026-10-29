"""来源适配器：把不同党媒站点的列表页/正文页归一化为 Article。

设计原则
--------
1. 只依赖 HTML 结构与 URL 规律，不依赖任何第三方 API。
2. 电子报（epaper）走「版面枚举」，因为 URL 含日期，可回溯任意历史日期。
3. 频道页（光明网时评等）走「首页链接抽取」，只拿最新一屏。
"""
import re
import urllib.parse as up
from datetime import date, timedelta

from . import dom, net

# ---------------------------------------------------------------- 数据模型


class Article:
    __slots__ = ("source", "channel", "title", "url", "date", "author", "body")

    def __init__(self, source, channel, title, url, date_, body="", author=""):
        self.source = source
        self.channel = channel
        self.title = title
        self.url = url
        self.date = date_
        self.body = body
        self.author = author

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


# ---------------------------------------------------------------- 清洗工具

NOISE = re.compile(
    r"^(返回目录|放大|缩小|全文复制|上一篇|下一篇|扫一扫|分享到|责任编辑|"
    r"《人民日报》|《光明日报》|《经济日报》|《中国青年报》|第\s*\d+\s*版|"
    r"版次|版面|阅读全文|【?纠错】?|字号|打印|关闭|广告|图片报道|本版责编.*)$"
)
# 报头：「《人民日报》（2026年10月20日 第05版）」
MASTHEAD = re.compile(r"^《[^》]{2,12}》\s*[（(]?\s*\d{4}\s*年.*版.*$")
# 副标题：「——正确认识中国经济的阶段特征与长远趋势⑤」
SUBTITLE = re.compile(r"^[—–\-]{1,2}\s*\S.{0,48}$")
# 署名/页脚行：「来源：中国青年报」「作者：李四」「责任编辑：张三」
META_LINE = re.compile(r"^(作者[:：]|编辑[:：]|责任编辑[:：]|来源[:：]|发布时间[:：]|记者\s)")


def clean_paras(paras, title=""):
    """逐行清洗：一个 <p> 里可能被 <br> 拆成多行（作者、报头常挤在一起）。"""
    out = []
    for block in paras:
        for raw in re.split(r"[\r\n]+", block or ""):
            p = re.sub(r"[\u3000\xa0]+", " ", raw).strip()
            if not p or len(p) < 2:
                continue
            if NOISE.match(p) or MASTHEAD.match(p) or META_LINE.match(p):
                continue
            if title and (p == title or p.strip("（）() ") == title.strip("（）() ")):
                continue
            if SUBTITLE.match(p):
                continue
            if re.fullmatch(r"[（(]?\d{4}年\d{1,2}月\d{1,2}日[）)]?", p):
                continue
            if len(p) <= 5 and re.fullmatch(r"[\u4e00-\u9fa5]{1,4}(\s[\u4e00-\u9fa5]{1,4})?", p):
                continue  # 署名
            out.append(p)
    return out


def extract_body(root, selectors, title=""):
    """按优先级尝试选择器，返回 (body, paras)。"""
    for cls in selectors:
        node = root.find(cls=cls)
        if not node:
            continue
        paras = clean_paras([p.text() for p in node.find_all("p")], title)
        text = "\n".join(paras)
        if len(text) >= 180:
            return text, paras
    # 兜底：全页最长连续段落块
    best = []
    cur = []
    for p in root.find_all("p"):
        t = p.text()
        if len(t) >= 20:
            cur.append(t)
        else:
            if sum(map(len, cur)) > sum(map(len, best)):
                best = cur
            cur = []
    if sum(map(len, cur)) > sum(map(len, best)):
        best = cur
    paras = clean_paras(best, title)
    return "\n".join(paras), paras


def guess_author(paras):
    for p in paras[:4]:
        m = re.match(r"^(?:作者[:：]\s*)?([\u4e00-\u9fa5]{2,4}(?:\s+[\u4e00-\u9fa5]{2,4})?)$", p.strip())
        if m:
            return m.group(1)
    return ""


TITLE_NOISE = re.compile(r"[_\-—|]\s*(光明网|人民网|新华网|求是网|半月谈网?|中国经济网|中青在线|中国青年报)\s*$")
SKIP_TITLE = re.compile(r"^(图片报道|本版责编|广告|导读|目录|公告|启事)")


def norm_title(t):
    t = re.sub(r"\s+", " ", (t or "").strip())
    t = TITLE_NOISE.sub("", t)
    return t.strip()


PREFIX_KEEP = {
    "光明时评", "光明观察", "光明论坛", "光明网评论员", "人民时评", "人民论坛",
    "评论员观察", "今日谈", "编者按", "记者", "新华社", "半月谈", "半月评论",
    "人民日报", "光明日报", "经济日报", "中国青年报", "央视快评", "新华时评",
}


def split_author_prefix(title):
    """把「赵志疆：村庄被8家化工厂包围」拆成 (标题, 作者)。"""
    m = re.match(r"^([\u4e00-\u9fa5]{2,4})[：:]\s*(.{6,})$", title)
    if m and m.group(1) not in PREFIX_KEEP:
        return m.group(2).strip(), m.group(1)
    return title, ""


# ---------------------------------------------------------------- 电子报通用（新版 CMS）


def _host(url):
    try:
        return (up.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _safe(url, hosts):
    """链接白名单：scheme 必须 http(s)、host 在给定集合内、不带 userinfo 和端口。

    抓取的 href 是不可信输入——电子报页面若被篡改（明文 HTTP 源 + 中间人），
    javascript: 伪协议或任意域链接会一路走到前端的「阅读原文」href 里。
    端口与 userinfo 也一并拒绝：党媒电子报都用标准端口，带这些的一律不是正常内容链接。
    """
    try:
        u = up.urlsplit(url)
    except ValueError:
        return ""
    if u.scheme not in ("http", "https"):
        return ""
    if (u.hostname or "").lower() not in hosts:
        return ""
    if u.username or u.password or u.port:
        return ""
    return url


def _epaper_pages(layout_url):
    """返回 {node_no: 版面名}，从 01 版导航解析。"""
    html = net.fetch(layout_url)
    if not html:
        return {}
    root = dom.parse(html)
    pages = {}
    for a in root.find_all("a"):
        href = a.get("href") or ""
        m = re.search(r"node_(\d+)\.html", href)
        if not m:
            continue
        txt = a.text()
        mm = re.search(r"(\d{1,2})\s*版[：:]\s*(.+)", txt)
        if mm:
            pages.setdefault(m.group(1).zfill(2), mm.group(2).strip())
    return pages


def _epaper_articles(layout_url, page_no):
    """抓某版面下所有文章链接。"""
    base = re.sub(r"node_\d+\.html.*$", f"node_{page_no}.html", layout_url)
    html = net.fetch(base, referer=layout_url)
    if not html:
        return []
    root = dom.parse(html)
    base_host = {_host(base)}
    items, seen = [], set()
    for a in root.find_all("a"):
        href = a.get("href") or ""
        if "content_" not in href:
            continue
        url = _safe(up.urljoin(base, href), base_host)
        if not url or url in seen:
            continue
        title = norm_title(a.text())
        if not title or len(title) < 5 or SKIP_TITLE.match(title):
            continue
        seen.add(url)
        items.append((title, url))
    return items


def crawl_epaper(source, host_prefix, date_, page_keywords):
    """通用电子报抓取。host_prefix 例：http://paper.people.com.cn/rmrb/pc"""
    ym, dd = date_.strftime("%Y%m"), date_.strftime("%d")
    layout = f"{host_prefix}/layout/{ym}/{dd}/node_01.html"
    pages = _epaper_pages(layout)
    if not pages:
        return []
    out = []
    date_str = date_.strftime("%Y-%m-%d")
    for no, name in sorted(pages.items()):
        if not any(k in name for k in page_keywords):
            continue
        for title, url in _epaper_articles(layout, no):
            out.append(Article(source, f"{no}版·{name}", title, url, date_str))
    return out


# ---------------------------------------------------------------- 具体源


def list_rmrb(date_):
    return crawl_epaper(
        "人民日报", "http://paper.people.com.cn/rmrb/pc", date_,
        ["评论", "理论", "要闻", "经济", "文化", "生态", "政治", "社会", "党建", "法治", "民生", "观察"],
    )


def list_zqb(date_):
    return crawl_epaper(
        "中国青年报", "http://zqb.cyol.com/pc", date_,
        ["要闻", "中青视线", "经济", "评论", "青年", "思想", "文化", "调查", "教育"],
    )


def list_ce(date_):
    return crawl_epaper(
        "经济日报", "http://paper.ce.cn/pc", date_,
        ["要闻", "评论", "理论", "经济", "观察", "调查", "产经", "时评"],
    )


def list_gmw(date_):
    """光明网时评频道（guancha.gmw.cn）。首页 SSR 约 60 条，含相对链接。"""
    html = net.fetch("https://guancha.gmw.cn/")
    if not html:
        return []
    root = dom.parse(html)
    out, seen = [], set()
    pat = re.compile(r"/(\d{4})-(\d{2})/(\d{2})/content_\d+\.htm")
    for a in root.find_all("a"):
        href = a.get("href") or ""
        if "content_" not in href:
            continue
        url = _safe(up.urljoin("https://guancha.gmw.cn/", href), {"guancha.gmw.cn"})
        if not url:
            continue
        m = pat.search(url)
        if not m or url in seen:
            continue
        title, author = split_author_prefix(norm_title(a.text()))
        if len(title) < 6 or SKIP_TITLE.match(title):
            continue
        seen.add(url)
        art = Article("光明网", "时评频道", title, url,
                      f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
        art.author = author
        out.append(art)
    return out


def list_banyuetan(date_):
    """半月谈网（考公第一刊，申论材料大户）。"""
    html = net.fetch("http://www.banyuetan.org/")
    if not html:
        return []
    root = dom.parse(html)
    out, seen = [], set()
    pat = re.compile(r"banyuetan\.org/(\w+)/detail/(\d{8})/")
    for a in root.find_all("a"):
        href = a.get("href") or ""
        m = pat.search(href)
        if not m:
            continue
        # 正则允许 banyuetan.org 出现在 href 任意位置，必须再校验 host 归属
        if not _safe(href, {"www.banyuetan.org", "banyuetan.org"}):
            continue
        if href in seen:
            continue
        title = norm_title(a.text())
        if len(title) < 6 or SKIP_TITLE.match(title):
            continue
        seen.add(href)
        d = m.group(2)
        out.append(Article("半月谈", "半月谈网", title, href,
                           f"{d[:4]}-{d[4:6]}-{d[6:]}"))
    return out


LISTERS = {
    "人民日报": list_rmrb,
    "光明网": list_gmw,
    "半月谈": list_banyuetan,
    "中国青年报": list_zqb,
    "经济日报": list_ce,
}

# 频道页类源只反映「当前最新」，不受 date 参数影响，回溯时无需重复抓
LIVE_ONLY = {"光明网", "半月谈"}

BODY_SELECTORS = {
    "人民日报": ["article", "article-box"],
    "中国青年报": ["article", "article-box"],
    "经济日报": ["article", "article-box"],
    "光明网": ["u-mainText", "con-text"],
    "半月谈": ["content", "article", "TRS_Editor", "detail-content"],
}


def fetch_body(article):
    html = net.fetch(article.url)
    if not html:
        return article
    root = dom.parse(html)
    t = root.find("title")
    if t:
        ft = norm_title(t.text())
        if ft and len(ft) > len(article.title) * 0.5:
            # 频道页标题常被截断，用详情页标题校正
            if len(ft) > len(article.title) + 2 or article.title.endswith(("…", "...")):
                article.title = ft
    body, paras = extract_body(root, BODY_SELECTORS.get(article.source, ["article"]), article.title)
    article.body = body
    if not article.author:
        article.author = guess_author(paras)
    return article
