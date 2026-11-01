"""考公命题价值评估引擎。

第一性原理
----------
命题人不是随机选文章的。一篇文章被改编成考题，必须满足四个条件：

1. 来源可信     —— 党媒评论版/理论版，观点代表主流价值判断（申论参考答案的"政治正确"基准）
2. 文体可拆     —— 议论性、有中心论点、有对策句，才能被切分成片段阅读或概括题材料
3. 话题在库     —— 落在申论常考的治理议题谱系内（基层、乡村、创新、生态、民生……）
4. 语言可考     —— 规范书面语、有并列/转折结构、有数据，适合做逻辑填空与语句排序

因此评分 = 来源(32) + 文体(24) + 话题(18) + 语言(12) + 时效(10)，满分 96，
每一项都可由文本证据反推，不是黑箱。
"""
import re
from datetime import date

# ---------------------------------------------------------------- 话题谱系

TOPICS = {
    "基层治理": ["基层", "社区", "网格", "群众工作", "枫桥经验", "减负", "属地管理", "治理效能", "下沉一线"],
    "乡村振兴": ["乡村振兴", "三农", "粮食安全", "耕地", "农民增收", "农业", "农村", "县域", "共同富裕"],
    "科技创新": ["科技创新", "技术攻关", "卡脖子", "人工智能", "算力", "新质生产力", "研发投入", "自立自强", "成果转化"],
    "营商环境": ["营商环境", "民营经济", "市场主体", "放管服", "行政审批", "融资难", "中小企业", "产权保护", "公平竞争"],
    "生态文明": ["生态文明", "绿色发展", "双碳", "污染防治", "环保", "长江", "黄河", "低碳", "美丽中国", "绿水青山"],
    "文化自信": ["文化自信", "非遗", "传统文化", "文脉", "中华文明", "文旅融合", "文化传承", "精神标识"],
    "民生保障": ["民生", "就业", "养老", "社保", "医疗", "教育", "住房", "托育", "居民收入", "医保", "公共服务"],
    "数字经济": ["数字经济", "数据要素", "平台经济", "算法", "互联网", "智能化", "数字化转型", "大模型"],
    "法治建设": ["法治", "依法", "司法", "立法", "执法", "普法", "权利保障", "合法性审查"],
    "干部作风": ["作风建设", "形式主义", "官僚主义", "担当作为", "政绩观", "躺平", "调查研究", "实事求是"],
    "舆情应对": ["舆情", "回应关切", "通报", "舆论监督", "曝光", "热搜", "公众关切", "透明度"],
    "对外开放": ["对外开放", "外贸", "一带一路", "自贸", "外资", "全球治理", "制度型开放"],
    "人口人才": ["人口", "老龄化", "生育", "人才", "劳动力", "银发经济", "引才"],
    "安全底线": ["安全生产", "食品安全", "应急", "防汛", "隐患", "底线思维", "风险防控", "监管漏洞"],
    "消费内需": ["消费", "内需", "以旧换新", "服务业", "文旅消费", "需求侧", "提振消费"],
}

# ---------------------------------------------------------------- 来源权重

SOURCE_BASE = {
    "半月谈": 28,
    "求是网": 30,
    "人民日报": 26,
    "新华网": 25,
    "人民网": 25,
    "光明网": 24,
    "经济日报": 23,
    "中国青年报": 22,
}

CHANNEL_RULES = [
    (r"评论", 4), (r"时评", 4), (r"网评", 4), (r"理论", 3), (r"观点", 3),
    (r"观察", 2), (r"求是", 3),
    (r"深度|调查|视线|周刊", 2),
    (r"要闻", -2), (r"国际", -4), (r"体育|副刊|广告", -12),
]

# 标题即栏目名 —— 最可靠的文体信号
COLUMN_SIGNALS = {
    "人民时评": 3, "评论员观察": 3, "人民论坛": 2, "今日谈": 2, "微观": 1,
    "纵横": 1, "大家谈": 2, "光明时评": 3, "光明论坛": 2, "半月评论": 3,
    "一线视角": 2, "现场评论": 2, "来论": 1, "思想纵横": 2, "评论员": 2,
    "新华网评": 3, "人民锐评": 3, "求是网评论员": 3, "求是杂志": 3,
    "钟声": 2, "仲音": 2, "金社平": 2, "任平": 3, "宣言": 3,
}

# 领导人活动/会议通稿：常识会考，但极少做申论材料
PROTOCOL = re.compile(
    r"会见|致信|贺信|祝贺|出席|发表讲话|主持召[开]|签署|考察|调研|座谈|批示|"
    r"致电|慰问|题词|开幕|闭幕|集体学习|主持会[议]"
)
POLICY_DOC = re.compile(r"《[^》]{2,30}》|印发|出台|部署|规划|意见|条例|办法|方案")
DATA_PAT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|亿|万亿|万|个百分[点分])")
COUNTER = re.compile(r"然而|但是|其实|事实上|恰恰|反之|问题在于|难就难在|不容忽视|短板|痛点|堵点")
MEASURE = re.compile(r"要[^。；，]{0,12}[，、]|应当|必须|亟[须待]|需要|关键在于|重在|贵在|一方面|另一方面")
PARALLEL = re.compile(r"[\u4e00-\u9fa5]{4}[，、][\u4e00-\u9fa5]{4}[，、][\u4e00-\u9fa5]{4}")
IDIOM = re.compile(r"[\u4e00-\u9fa5]{4}(?=[，。；、])")


def _fields(article):
    """输入归一：Article 对象、SimpleNamespace、sqlite Row 转成的 dict 都接受。"""
    if isinstance(article, dict):
        g = article.get
    else:
        g = lambda k, _d=None: getattr(article, k, _d)
    return (g("title") or "", g("body") or "", g("source") or "",
            g("channel") or "", g("date") or "")


def detect_topics(text, title=""):
    """话题打分：命中词数 + 标题命中加权。标题里出现即视为主题明确。"""
    scored = []
    for name, kws in TOPICS.items():
        hits = [w for w in kws if w in text]
        if not hits:
            continue
        w = len(hits) + min(2, sum(text.count(k) for k in hits) // 3)
        if any(k in title for k in kws):
            w += 2
        scored.append((w, name))
    scored.sort(reverse=True)
    return [n for _, n in scored[:3]], scored


def classify(article):
    """返回 dict：topics / tracks / score / reason / signals（规则评估，纯本地零成本）"""
    title, body, source, channel, adate = _fields(article)

    text = body or title
    n = len(body)
    topics, tscore = detect_topics(body, title)
    if tscore and tscore[0][0] <= 1:
        topics = []  # 只擦到一次边的话题不算主题，避免「满屏安全底线」

    # ---- 1. 来源（0-32）
    s_score = SOURCE_BASE.get(source, 18)
    for pat, d in CHANNEL_RULES:
        if re.search(pat, channel):
            s_score += d
    for kw, d in COLUMN_SIGNALS.items():
        if kw in title:
            s_score += d
    s_score = max(0, min(32, s_score))

    # ---- 2. 文体（0-24）
    st = 11
    if PROTOCOL.search(title):
        st -= 7
    if POLICY_DOC.search(title) or POLICY_DOC.search(body[:400]):
        st += 3
    m_cnt = len(MEASURE.findall(body))
    density = m_cnt / max(1, n / 800)
    st += min(6, density * 1.8)
    if COUNTER.search(body):
        st += 2
    if n < 300:
        st -= 6
    elif 700 <= n <= 2600:
        st += 3
    elif n > 4000:
        st -= 4
    st = max(0, min(24, st))

    # ---- 3. 话题（0-18）
    t_score = min(18, sum(min(c, 3) for c, _ in tscore[:3]) * 2.2)
    if not topics:
        t_score = 2

    # ---- 4. 语言（0-12）
    data_n = len(DATA_PAT.findall(body))
    l_score = 4
    l_score += min(3, data_n * 0.6)
    l_score += min(3.5, len(PARALLEL.findall(body)) * 1.2)
    l_score += min(2, len(set(IDIOM.findall(body))) * 0.15)
    l_score = max(0, min(12, l_score))

    # ---- 5. 时效（0-10）
    f_score = 6
    try:
        d = date.fromisoformat(adate[:10])
        gap = (date.today() - d).days
        f_score = 10 if gap <= 1 else 8 if gap <= 3 else 6 if gap <= 7 else 3
    except Exception:
        pass

    score = round(s_score + st + t_score + l_score + f_score)

    # ---- 考公用途标签（按备考价值排序，常识时政保持稀缺性）
    tracks = []
    if topics and st >= 12:
        tracks.append("申论素材")
    if 300 <= n <= 1500 and (PARALLEL.search(body) or COUNTER.search(body)) and l_score >= 7:
        tracks.append("言语语料")
    if topics and (COUNTER.search(body) or re.search(r"为何|怎么|如何|难题|困局", title)):
        tracks.append("面试热点")
    # 只在标题层面命中才算时政考点：通稿动作或政策文件名
    if PROTOCOL.search(title) or re.search(r"《[^》]{2,30}》", title):
        tracks.append("常识时政")
    if not tracks:
        tracks.append("泛时政")

    reason = _reason(source, channel, topics, n, m_cnt, data_n, tracks)
    return {
        "topics": topics or ["综合"],
        "tracks": tracks,
        "score": score,
        "reason": reason,
        "signals": {
            "source": round(s_score, 1),
            "style": round(st, 1),
            "topic": round(t_score, 1),
            "language": round(l_score, 1),
            "fresh": round(f_score, 1),
            "measures": m_cnt,
            "data": data_n,
        },
    }


def _reason(source, channel, topics, n, m_cnt, data_n, tracks):
    bits = [f"{source}·{channel}"]
    if topics:
        bits.append("议题：" + "/".join(topics[:2]))
    if m_cnt:
        bits.append(f"{m_cnt}处对策句")
    if data_n:
        bits.append(f"{data_n}处数据")
    bits.append(f"{n}字")
    tail = "、".join(tracks)
    return "｜".join(bits) + f" → 可用于{tail}"
