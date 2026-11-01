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
    r"《人民日报》|《光明日报》|《经济日报》|《中国青年报》|《求是》|第\s*\d+\s*版|"
    r"版次|版面|阅读全文|【?纠错】?|字号|打印|关闭|广告|图片报道|本版责编.*|"
    r"点击播报本文.*|打开|题[:：].*|分享让更多人看到|视频/海报制作.*|"
    r"责编[:：].*|统筹[:：].*|监制[:：].*)$"
)
# 报头：「《人民日报》（2026年10月20日 第05版）」
MASTHEAD = re.compile(r"^《[^》]{2,12}》\s*[（(]?\s*\d{4}\s*年.*版.*$")
# 副标题：「——正确认识中国经济的阶段特征与长远趋势⑤」
SUBTITLE = re.compile(r"^[—–\-]{1,2}\s*\S.{0,48}$")
# 署名/页脚行：「来源：中国青年报」「作者：李四」「责任编辑：张三」
META_LINE = re.compile(r"^(作者[:：]|编辑[:：]|责任编辑[:：]|来源[:：]|发布时间[:：]|记者\s)")
# 独立时间行：「2026-10-31 10:21:31」（求是网正文顶部会带）
TIMESTAMP = re.compile(r"^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?\s*\d{1,2}:\d{2}(:\d{2})?$")


def clean_paras(paras, title=""):
    """逐行清洗：一个 <p> 里可能被 <br> 拆成多行（作者、报头常挤在一起）。"""
    out = []
    for block in paras:
        for raw in re.split(r"[\r\n]+", block or ""):
            # \u3000 全角空格、\xa0 nbsp、\u2003 em space（新华网正文用它做首行缩进）
            p = re.sub(r"[\u3000\xa0\u2003\u2002]+", " ", raw).strip()
            if not p or len(p) < 2:
                continue
            if NOISE.match(p) or MASTHEAD.match(p) or META_LINE.match(p) or TIMESTAMP.match(p):
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


TITLE_NOISE = re.compile(
    r"(?:[_\-—|]\s*(?:光明网|人民网|新华网|求是网|半月谈网?|中国经济网|中青在线|中国青年报)\s*"
    r"|[-—]{1,2}\s*(?:观点|评论|时评|理论|国际|社会|法治|教育|科技|文化|经济|体育|军事)\s*-?)\s*$"
)
SKIP_TITLE = re.compile(r"^(图片报道|本版责编|广告|导读|目录|公告|启事)")


def norm_title(t):
    t = re.sub(r"\s+", " ", (t or "").strip())
    t = TITLE_NOISE.sub("", t)
    return t.strip()


PREFIX_KEEP = {
    "光明时评", "光明观察", "光明论坛", "光明网评论员", "人民时评", "人民论坛",
    "评论员观察", "今日谈", "编者按", "记者", "新华社", "半月谈", "半月评论",
    "人民日报", "光明日报", "经济日报", "中国青年报", "央视快评", "新华时评",
    # 新增源带来的栏目名：这些是「栏目」不是「作者」，拆出去会把栏目信息丢掉
    "人民锐评", "新华网评", "求是网评论员", "求是杂志", "钟声", "仲音", "任平",
    "宣言", "金社平", "人民观点", "光明网评",
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


# ---------------------------------------------------------------- 频道页通用

# 频道页源的 URL 模式。日期都带在路径里，比从正文猜可靠得多。
# 每个正则必须含 (年)(月)(日) 三个捕获组，_channel_links 靠它取日期。
# 求是网的栏目页用相对路径（20261031/{hex}/c.html），首页用绝对路径，
# 所以主机名部分要可选，两种都吃。
PAT_QSTHEORY = re.compile(
    r"(?:qstheory\.cn/)?(\d{4})(\d{2})(\d{2})/[0-9a-f]{32}/c\.html")
PAT_XINHUA = re.compile(r"/comments/(\d{4})(\d{2})(\d{2})/[0-9a-z]+/c\.html")
# 新华时评/新华社评论员栏目页的稿子不挂在 /comments/ 下，而是散在 /20261031/、
# /fortune/20261028/ 等各频道，所以栏目页要用这个宽松版，否则一条也抓不到。
PAT_XINHUA_ANY = re.compile(r"/(\d{4})(\d{2})(\d{2})/[0-9a-z]+/c\.html")
PAT_PEOPLE = re.compile(r"/n\d/(\d{4})/(\d{2})(\d{2})/c\d+-\d+\.html")
# 光明网的年月之间是横杠、月日之间是斜杠：/2026-10/29/content_39071825.htm
PAT_GMW = re.compile(r"/(\d{4})-(\d{2})/(\d{2})/content_\d+\.htm")


def _channel_links(home, host_set, pat, source, channel):
    """频道页通用抽取：首页链接 → 文章列表。

    pat 必须含 (年)(月)(日) 三个捕获组，日期直接从 URL 取——
    党媒频道页的 URL 一律带发布日期，比从正文里猜可靠。

    注意顺序：**先 urljoin 再匹配正则**。光明网有一批 href 是
    `2026-09/24/content_39019476.htm` 这种不带前导斜杠的相对路径，
    直接拿 href 去匹配会漏掉一大半（实测 18 条 vs 80 条）。
    """
    html = net.fetch(home)
    if not html:
        return []
    root = dom.parse(html)
    out, seen = [], set()
    for a in root.find_all("a"):
        href = a.get("href") or ""
        if not href:
            continue
        url = _safe(up.urljoin(home, href), host_set)
        if not url or url in seen:
            continue
        m = pat.search(url)
        if not m:
            continue
        title, author = split_author_prefix(norm_title(a.text()))
        if len(title) < 6 or SKIP_TITLE.match(title):
            continue
        seen.add(url)
        art = Article(source, channel, title, url,
                      f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
        art.author = author
        out.append(art)
    return out


def _merge(*groups):
    """多栏目合并去重。

    同一篇文章常同时出现在「最新」和「栏目」两个页面里，按 URL 去重即可；
    标题可能因版面不同被改写，所以不用标题做键。
    """
    out, seen = [], set()
    for arts in groups:
        for a in arts:
            if a.url in seen:
                continue
            seen.add(a.url)
            out.append(a)
    return out


def list_gmw(date_):
    """光明网：时评频道 + 理论频道。

    两个频道都是 SSR 静态页，链接形如 /2026-10/29/content_39071825.htm。
    理论频道（theory.gmw.cn）是《光明日报》理论版的稿子，属于申论最吃的那种
    「有论点有论证」的长文，值得单独抓；实测多出约 20 条候选。
    """
    hosts = {"guancha.gmw.cn", "news.gmw.cn", "theory.gmw.cn", "www.gmw.cn"}
    return _merge(
        _channel_links("https://guancha.gmw.cn/", hosts, PAT_GMW,
                       "光明网", "时评频道"),
        _channel_links("https://theory.gmw.cn/", hosts, PAT_GMW,
                       "光明网", "理论频道"),
    )


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


def list_qstheory(date_):
    """求是网：首页 + 求是网评栏目页。

    首页（《求是》杂志官网，习近平重要文章首发地之一）覆盖理论长文；
    /qswp.htm 是求是网评，日更一条，是申论最对口的那种短评。
    URL 形如 /20261031/{32位hex}/c.html，日期在路径里。
    """
    hosts = {"www.qstheory.cn", "qstheory.cn"}
    return _merge(
        _channel_links("https://www.qstheory.cn/",
                       hosts, PAT_QSTHEORY, "求是网", "求是首页"),
        _channel_links("https://www.qstheory.cn/qswp.htm",
                       hosts, PAT_QSTHEORY, "求是网", "求是网评"),
    )


def list_xinhua(date_):
    """新华网评论：四个页面合并。

    只抓 /comments/ 首页远远不够——那是个位次有限的推荐位，混着几个月前的
    旧稿，七天里只出一条当天的。真正的日更在栏目页：
      新华网评     /comments/wpyc/index.html
      新华时评     /depthobserve/xhsp.html
      新华社评论员  /depthobserve/xhsply.html
    注意栏目页的稿子散落在各频道路径下（/20261031/、/fortune/20261028/），
    必须用 PAT_XINHUA_ANY，用带 /comments/ 的 PAT_XINHUA 会一条都抓不到。
    """
    hosts = {"www.news.cn", "news.cn"}
    return _merge(
        _channel_links("http://www.news.cn/depthobserve/xhsp.html",
                       hosts, PAT_XINHUA_ANY, "新华网", "新华时评"),
        _channel_links("http://www.news.cn/depthobserve/xhsply.html",
                       hosts, PAT_XINHUA_ANY, "新华网", "新华社评论员"),
        _channel_links("http://www.news.cn/comments/wpyc/index.html",
                       hosts, PAT_XINHUA, "新华网", "新华网评"),
        _channel_links("http://www.news.cn/comments/",
                       hosts, PAT_XINHUA, "新华网", "评论频道"),
    )


def list_people(date_):
    """人民网观点频道。URL 形如 /n1/2026/1030/c461529-40824171.html。

    注意：人民网的 href 相对/绝对混用，且同一文章常有多个 <a>，
    必须用 urljoin + seen 去重，否则会重复计入。
    """
    return _channel_links(
        "http://opinion.people.com.cn/",
        {"opinion.people.com.cn", "www.people.com.cn", "people.com.cn"},
        PAT_PEOPLE, "人民网", "观点频道",
    )


LISTERS = {
    "人民日报": list_rmrb,
    "光明网": list_gmw,
    "半月谈": list_banyuetan,
    "中国青年报": list_zqb,
    "经济日报": list_ce,
    "求是网": list_qstheory,
    "新华网": list_xinhua,
    "人民网": list_people,
}

# 频道页类源只反映「当前最新」，不受 date 参数影响，回溯时无需重复抓
LIVE_ONLY = {"光明网", "半月谈", "求是网", "新华网", "人民网"}

BODY_SELECTORS = {
    "人民日报": ["article", "article-box"],
    "中国青年报": ["article", "article-box"],
    "经济日报": ["article", "article-box"],
    "光明网": ["u-mainText", "con-text"],
    "半月谈": ["content", "article", "TRS_Editor", "detail-content"],
    "求是网": ["text", "content"],
    "新华网": ["main-left", "main"],
    "人民网": ["voice-wrap", "rm_txt_con"],
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
