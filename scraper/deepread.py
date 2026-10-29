"""每日精读：每天挑 5 篇值得逐段拆解的文章，产出「中心理解速记卡」。

为什么单独一条流水线
--------------------
命题研判回答「这篇能不能被改编成题」，背诵提取回答「哪几句值得背」，
精读回答的是第三个问题——「这篇文章的论证骨架长什么样」。

三者标准不同：
- 研判看分值，一天几十篇都要跑；
- 背诵看段落，只挑高分文章；
- 精读看结构，一天只挑 5 篇，但要拆到段落级。

精读是三个环节里最贵的（每篇一次完整长文分析），所以按天做、只做 5 篇、
结果落库后当天不再重算——`deep_reads` 表里有记录就跳过。

速记卡的设计依据
----------------
对应行测「言语理解与表达·中心理解题」的解题链条：
找主题词 → 判结构 → 定中心句 → 排干扰项。
卡片按这个顺序组织，让考生读一篇文章的同时把解题动作走一遍。
"""
import json
import os
import re
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import llm

# 候选窗口：太短没有论证结构，太长不像片段阅读材料
MIN_CHARS = 700
MAX_CHARS = 3600
BODY_WINDOW = 4200

# 详细卡片拆成两次调用：一次输出三千字以上的 JSON，单次请求会长时间无响应直至超时。
# 拆成「结构拆解」+「应试拆解」两半，各自更快，且可并行——总耗时取较大者而不是相加。
# 副作用是容错更好：应试那半失败时，结构卡仍然可用。
# 实测：这类长文拆解，模型光「思考」就要烧掉 13000+ token，正文输出另算 2000 左右。
# 预算低于 20000 会被 thinking 吃光、一个字都吐不出来（stop_reason=max_tokens）。
STRUCT_TOKENS = (20000, 26000, 32000)
EXAM_TOKENS = (18000, 24000, 30000)
CARD_TIMEOUT = 300


PICK_PROMPT = """你是公务员考试言语理解与表达教研员。下面是某天党媒文章的候选清单，
请挑出最适合做「中心理解」精读训练的 {n} 篇。

挑选标准（按重要性排序）：
1. 议论性文本优先——评论、时评、理论文章有明确中心论点；纯新闻通稿、会议报道、活动消息不要。
2. 结构完整——有提出观点、展开论证、收束升华的完整脉络，能拆出「总—分」「分—总」等结构。
3. 主题分散——几篇尽量覆盖不同议题，不要都是同一话题。
4. 命题价值高——分值高的优先。

候选清单（id | 分值 | 来源 | 议题 | 标题 | 摘要）：
{cands}

严格只输出一个 JSON 对象，不要解释文字，不要 markdown 代码块：
{{
  "theme": "今日主线，一句话概括这几篇共同的时政背景，不超过30字",
  "picks": [
    {{"id": 文章id数字, "reason": "为什么选它做精读，不超过35字，要点出结构特征"}}
  ]
}}
必须正好 {n} 篇，id 只能从清单里选。"""


STRUCT_PROMPT = """你是公务员考试言语理解与表达教研员，正在给考生做「中心理解」精读拆解。
拆解要细到：考生读完这张卡，能自己复述全文的论证链条，并说清每一段为什么放在那个位置。

标题：{title}
来源：{source} · {channel}
正文：
{body}

严格只输出一个 JSON 对象，不要解释文字，不要 markdown 代码块：
{{
  "thesis": "全文中心论点，一句话完整判断句，不超过50字",
  "plain": "用大白话把这个论点翻译一遍，不用政策套话，60到100字。让人一看就懂这篇到底在主张什么",
  "background": "这篇评论针对的现实问题与政策背景：什么现象、什么文件、什么时间节点，80到120字",
  "structure": "从这几个里选一个：总—分／分—总／总—分—总／并列／递进／转折／因果",
  "structure_note": "这个结构具体怎么展开的、各部分怎么衔接，60到100字",
  "core": {{
    "text": "中心句原文，逐字摘抄连续一句或两句，不得改写",
    "para": 段号数字,
    "position": "首句／段中／尾句",
    "basis": "判定它为中心句的依据，60字以内",
    "logic": "它为什么是中心：与上下文构成什么逻辑关系（转折后是重点／尾句总结前文／承上启下引出对策），60到100字",
    "signals": ["原文里的逻辑标志词，如 然而、因此、说到底、关键在于，2到3个"]
  }},
  "keywords": [
    {{"word": "原文出现的实词，不是议题标签", "why": "它统摄了文中哪些内容，30字内"}}
  ],
  "outline": [
    {{"para": 1,
      "gist": "该段大意，30字内",
      "role": "从这些里选：提出论点／解释概念／举例论证／分析原因／正反对比／提出对策／总结升华",
      "key": "该段最关键的一句原文，逐字摘抄，不超过60字",
      "link": "这一段对全文主旨起什么作用，30字内"}}
  ],
  "methods": [
    {{"name": "举例论证／对比论证／数据论证／引用论证／道理论证／比喻论证",
      "example": "文中具体在哪里用的，如 第3段举深圳无人车案例",
      "effect": "这样写的作用，25字内"}}
  ],
  "terms": [
    {{"term": "文中出现的政策术语、专业概念或生僻说法", "plain": "一句话解释，不超过40字，要让外行看懂"}}
  ],
  "mnemonic": "一句话速记口诀，不超过30字，帮考生记住这篇文章的论证骨架"
}}

要求：
- outline 必须覆盖正文每一个自然段，段号从 1 开始连续，一段都不能漏。
- outline 的 key 必须是该段真实存在的原句，逐字摘抄，不得改写。
- terms 挑真正会影响理解的词，常见词不要列。
- 所有摘抄必须逐字出自正文，不得改写或拼接。
- 不要写空话套话，每条都要具体到这篇的内容。"""


EXAM_PROMPT = """你是公务员考试命题人，正在就下面这篇党媒评论出「中心理解」练习题，并整理申论可迁移素材。

标题：{title}
来源：{source} · {channel}
中心论点（前一步已判定）：{thesis}
正文：
{body}

严格只输出一个 JSON 对象，不要解释文字，不要 markdown 代码块：
{{
  "exam": [
    {{"type": "主旨概括／意图判断／标题填入／细节判断，选1到2个最贴的",
      "stem": "按真题口吻写的题干",
      "options": ["选项内容", "选项内容", "选项内容", "选项内容"],
      "answer": "B",
      "why": "为什么选它，40字内",
      "traps": [
        {{"opt": "A", "why": "这一项错在哪，25字内"}},
        {{"opt": "C", "why": "这一项错在哪，25字内"}},
        {{"opt": "D", "why": "这一项错在哪，25字内"}}
      ]}}
  ],
  "shenlun": {{
    "measures": ["文中可直接迁移到申论的对策句，逐字摘抄，1到2条"],
    "phrases": ["值得背的规范表述，2到3条，每条不超过20字"],
    "paragraph": "示范：用本文材料写一个申论分论点段落，120到160字，要有论点句、论据、回扣"
  }},
  "pitfalls": ["读这类文章最容易漏掉的点，20到35字，2到3条"]
}}

要求：
- options 必须有 4 项，只写选项正文，不要带「A.」前缀。
- 正确项要是全文层面的概括；三个干扰项分别犯不同错误（以偏概全、偷换概念、无中生有、过度推断），不要凑数。
- traps 覆盖其余三个错误选项，逐项说明错在哪。
- measures 必须逐字出自正文，不得改写。
- 不要写空话套话，每条都要具体到这篇的内容。"""


# ---------------------------------------------------------------- 候选与挑选

def candidates(con, day, limit=18, exclude=None):
    """当天的精读候选：议论性、结构完整、长度适中。

    已被接口敏感词拦截过的文章（ai_note 有记录）直接排除——同一段正文
    再送一次仍然会被拒，白花一次调用。
    """
    rows = con.execute(
        """SELECT id,title,source,channel,score,topics,summary,length(body) AS n
           FROM articles
           WHERE date=? AND length(body)>=? AND length(body)<=?
             AND (ai_note IS NULL OR ai_note NOT LIKE '%敏感词%')
           ORDER BY score DESC LIMIT ?""",
        (day, MIN_CHARS, MAX_CHARS, limit + (len(exclude) if exclude else 0)),
    ).fetchall()
    out = [dict(r) for r in rows]
    if exclude:
        out = [c for c in out if c["id"] not in exclude]
    return out[:limit]


def _fmt_cands(cands):
    lines = []
    for c in cands:
        topics = "、".join(json.loads(c["topics"] or "[]")[:3])
        summary = (c["summary"] or "").replace("\n", " ")[:40]
        lines.append(f'{c["id"]} | {c["score"]} | {c["source"]} | {topics} | {c["title"][:36]} | {summary}')
    return "\n".join(lines)


def _rule_pick(cands, n=5):
    """兜底挑选：按分值排序，同一议题最多 2 篇，保证主题分散。"""
    picked, topic_count = [], {}
    for c in cands:  # cands 已按分值降序
        ts = json.loads(c["topics"] or "[]")
        main = ts[0] if ts else "综合"
        if topic_count.get(main, 0) >= 2:
            continue
        picked.append({"id": c["id"], "reason": f"命题价值 {c['score']} 分，{main}议题中的高结构完整度文本"})
        topic_count[main] = topic_count.get(main, 0) + 1
        if len(picked) >= n:
            break
    return picked


def pick(con, day, n=5, exclude=None):
    """让模型从候选里挑 n 篇；失败则按规则兜底。"""
    exclude = exclude or set()
    cands = candidates(con, day, limit=max(12, n * 3), exclude=exclude)
    if not cands:
        return "", []
    valid_ids = {c["id"] for c in cands}

    cfg = llm.config()
    if cfg.get("key"):
        prompt = PICK_PROMPT.format(n=n, cands=_fmt_cands(cands))
        payload = {
            "model": cfg["model"],
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }
        for attempt in range(2):
            try:
                d = llm._post(cfg, payload, 120)
                text = "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text")
                obj = llm._parse(text)
                picks = [p for p in (obj or {}).get("picks", [])
                         if isinstance(p, dict) and p.get("id") in valid_ids]
                if len(picks) >= n:
                    return (obj.get("theme") or "").strip(), picks[:n]
            except Exception:
                if attempt:
                    break
                time.sleep(2)

    return "", _rule_pick(cands, n)


# ---------------------------------------------------------------- 速记卡生成

def _clip(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n] + "…"


def _verbatim(s, flat):
    """摘抄必须能在正文里找到（去标点后前 40 字比对）。"""
    q = re.sub(r"[^\w一-鿿]", "", str(s or ""))
    return len(q) >= 8 and q[:40] in flat


def _norm_struct(obj, body):
    """清洗「结构拆解」那一半输出。

    两处硬校验（不做就会把模型编的东西当原文展示）：
    - core.text：中心句必须是正文里真实存在的连续片段；
    - outline[].key：每段「关键句」同理。
    校验不过就置空，页面退化为只显示结构判断，不显示伪造摘抄。
    """
    if not isinstance(obj, dict):
        return None
    core = obj.get("core") if isinstance(obj.get("core"), dict) else {}
    flat = re.sub(r"[^\w一-鿿]", "", body or "")

    text = str(core.get("text") or "").strip()
    if text and not _verbatim(text, flat):
        text = ""

    outline = []
    for i, it in enumerate(obj.get("outline") or []):
        if not isinstance(it, dict):
            continue
        try:
            para = int(it.get("para") or i + 1)
        except Exception:
            para = i + 1
        key = str(it.get("key") or "").strip()
        outline.append({
            "para": para,
            "gist": _clip(it.get("gist"), 34),
            "role": _clip(it.get("role"), 10),
            "key": _clip(key, 70) if _verbatim(key, flat) else "",
            "link": _clip(it.get("link"), 36),
        })

    keywords = []
    for k in (obj.get("keywords") or [])[:4]:
        if isinstance(k, dict):
            w = _clip(k.get("word"), 14)
            if w:
                keywords.append({"word": w, "why": _clip(k.get("why"), 34)})
        elif k:
            keywords.append({"word": _clip(k, 14), "why": ""})

    methods = []
    for m in (obj.get("methods") or [])[:3]:
        if isinstance(m, dict):
            n = _clip(m.get("name"), 10)
            if n:
                methods.append({"name": n, "example": _clip(m.get("example"), 46),
                                "effect": _clip(m.get("effect"), 30)})
        elif m:
            methods.append({"name": _clip(m, 10), "example": "", "effect": ""})

    terms = []
    for t in (obj.get("terms") or [])[:4]:
        if isinstance(t, dict):
            w = _clip(t.get("term"), 16)
            if w:
                terms.append({"term": w, "plain": _clip(t.get("plain"), 50)})
        elif t:
            terms.append({"term": _clip(t, 16), "plain": ""})

    card = {
        "thesis": _clip(obj.get("thesis"), 60),
        "plain": _clip(obj.get("plain"), 130),
        "background": _clip(obj.get("background"), 150),
        "structure": _clip(obj.get("structure"), 10),
        "structure_note": _clip(obj.get("structure_note"), 130),
        "core": {
            "text": _clip(text, 180),
            "para": core.get("para"),
            "position": _clip(core.get("position"), 6),
            "basis": _clip(core.get("basis"), 80),
            "logic": _clip(core.get("logic"), 130),
            "signals": [_clip(s, 8) for s in (core.get("signals") or [])[:4] if s],
        },
        "keywords": keywords,
        "outline": outline,
        "methods": methods,
        "terms": terms,
        "mnemonic": _clip(obj.get("mnemonic"), 40),
    }
    if not card["thesis"]:
        return None
    return card


def _norm_exam(obj):
    """清洗「应试拆解」那一半输出。"""
    if not isinstance(obj, dict):
        return None
    exam = []
    for it in (obj.get("exam") or [])[:2]:
        if not isinstance(it, dict):
            continue
        opts = [_clip(x, 70) for x in (it.get("options") or [])[:4] if x]
        traps = []
        for t in (it.get("traps") or [])[:3]:
            if isinstance(t, dict):
                traps.append({"opt": _clip(t.get("opt"), 2), "why": _clip(t.get("why"), 32)})
            elif t:
                traps.append({"opt": "", "why": _clip(t, 32)})
        if not traps and it.get("trap"):  # 兼容旧版单条 trap
            traps.append({"opt": "", "why": _clip(it.get("trap"), 32)})
        exam.append({
            "type": _clip(it.get("type"), 8),
            "stem": _clip(it.get("stem"), 60),
            "options": opts,
            "answer": _clip(it.get("answer"), 6),
            "why": _clip(it.get("why"), 50),
            "traps": traps,
        })

    sl = obj.get("shenlun") if isinstance(obj.get("shenlun"), dict) else {}
    return {
        "exam": exam,
        "shenlun": {
            "measures": [_clip(x, 100) for x in (sl.get("measures") or [])[:2] if x],
            "phrases": [_clip(x, 30) for x in (sl.get("phrases") or [])[:3] if x],
            "paragraph": _clip(sl.get("paragraph"), 220),
        },
        "pitfalls": [_clip(x, 45) for x in (obj.get("pitfalls") or [])[:3] if x],
    }


def _call(cfg, prompt, steps, timeout, parse, *args):
    """带额度加码的单次结构化调用。返回 (obj, err)。"""
    err = ""
    for i, budget in enumerate(steps):
        payload = {
            "model": cfg["model"],
            "max_tokens": budget,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            d = llm._post(cfg, payload, timeout)
            text = "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text")
            if not text.strip():
                err = f"思考占满 {budget} token，未输出内容"
            else:
                obj = parse(llm._parse(text), *args)
                if obj:
                    return obj, ""
                err = f"返回内容无法解析（stop={d.get('stop_reason')}）"
        except llm.Blocked as e:
            return None, f"敏感词拦截：{e}"
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        if i < len(steps) - 1:
            time.sleep(2 * (i + 1))
    return None, err


def make_card(row, timeout=CARD_TIMEOUT):
    """生成速记卡：结构拆解与应试拆解并行，合并成一张卡。

    两半各自独立失败：结构那半失败则整篇作废（没有主旨就没法用），
    应试那半失败只丢命题演练与申论迁移，卡片其余部分仍可用。
    """
    cfg = llm.config()
    if not cfg.get("key"):
        return None, "未配置 API Key"
    body = (row.get("body") or "")[:BODY_WINDOW]
    head = dict(title=row.get("title", ""), source=row.get("source", ""),
                channel=row.get("channel", ""))

    struct_prompt = STRUCT_PROMPT.format(body=body, **head)
    struct, err = _call(cfg, struct_prompt, STRUCT_TOKENS, timeout, _norm_struct, body)
    if not struct:
        return None, err

    exam_prompt = EXAM_PROMPT.format(body=body, thesis=struct["thesis"], **head)
    extra, err2 = _call(cfg, exam_prompt, EXAM_TOKENS, timeout, _norm_exam)
    if extra:
        struct.update(extra)
    else:
        # 应试那半失败不废整篇：结构卡仍然有精读价值，只是少了练习题
        struct.update({"exam": [], "shenlun": {"measures": [], "phrases": [], "paragraph": ""},
                       "pitfalls": [], "partial": True, "partial_reason": err2})
    return struct, ""


# ---------------------------------------------------------------- 主流程

def _renumber(con, day):
    """按分值重排名次。

    补漏生成时新篇拿不到正确位次（已有 4 篇时新篇只能是第 5），
    而 LLM 挑选顺序也不等于推荐强度——统一按命题价值排，口径稳定可解释。
    """
    rows = con.execute(
        """SELECT d.id FROM deep_reads d JOIN articles a ON a.id = d.article_id
           WHERE d.date=? ORDER BY a.score DESC, d.id""", (day,)).fetchall()
    for i, r in enumerate(rows, 1):
        con.execute("UPDATE deep_reads SET rank=? WHERE id=?", (i, r["id"]))
    con.commit()


def run(con, day=None, n=5, workers=3, force=False, log=print):
    """生成某天的精读。已有卡片不重做，只补缺的（除非 force）。"""
    from datetime import date as _date
    day = day or _date.today().isoformat()

    existing = [r["article_id"] for r in con.execute(
        "SELECT article_id FROM deep_reads WHERE date=? ORDER BY rank", (day,))]
    if not force and len(existing) >= n:
        log(f"精读：{day} 已有 {len(existing)} 篇，跳过")
        return len(existing)

    need = n if force else n - len(existing)
    theme, picks = pick(con, day, need, exclude=set(existing) if not force else None)
    if not picks:
        log(f"精读：{day} 没有合适候选（正文 {MIN_CHARS}~{MAX_CHARS} 字）")
        return len(existing)

    ids = [p["id"] for p in picks]
    marks = ",".join("?" * len(ids))
    rows = {r["id"]: dict(r) for r in con.execute(
        f"SELECT * FROM articles WHERE id IN ({marks})", ids)}

    log(f"精读：{day} 选中 {len(picks)} 篇，生成速记卡…")
    ok = 0
    base_rank = len(existing)

    def task(pair):
        rank, item = pair
        aid = item["id"]
        card, err = make_card(rows[aid])
        return rank, item, card, err

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(task, (base_rank + i + 1, p))
                for i, p in enumerate(picks) if p["id"] in rows]
        for f in as_completed(futs):
            rank, item, card, err = f.result()
            if not card:
                log(f"  ✗ {rows[item['id']]['title'][:24]}：{err}")
                if err.startswith("敏感词拦截"):
                    # 记进 ai_note：候选查询会把带敏感词记录的文章排除掉，不再重试
                    con.execute(
                        """UPDATE articles SET ai_note =
                             CASE WHEN ai_note IS NULL OR ai_note='' THEN '精读被接口敏感词拦截'
                                  WHEN ai_note LIKE '%精读被接口敏感词拦截%' THEN ai_note
                                  ELSE ai_note || '；精读被接口敏感词拦截' END
                           WHERE id=?""", (item["id"],))
                    con.commit()
                continue
            con.execute(
                """INSERT INTO deep_reads(date,article_id,rank,reason,card,created_at)
                   VALUES(?,?,?,?,?,datetime('now','localtime'))
                   ON CONFLICT(date,article_id) DO UPDATE SET
                     rank=excluded.rank, reason=excluded.reason, card=excluded.card""",
                (day, item["id"], rank, item.get("reason", ""),
                 json.dumps(card, ensure_ascii=False)),
            )
            con.commit()
            ok += 1
            log(f"  ✓ {rows[item['id']]['title'][:24]}")

    # 重做（force）时是重新挑文章，旧卡片对应的文章可能已不在本轮名单里。
    # 只按 article_id 做 upsert 会把新旧混在一起（一天变成 6 篇），必须清掉本轮之外的。
    if force and ok:
        keep = [p["id"] for p in picks if p["id"] in rows]
        if keep:
            con.execute(
                f"DELETE FROM deep_reads WHERE date=? AND article_id NOT IN ({','.join('?' * len(keep))})",
                [day] + keep)
            con.commit()

    # 补漏时只选了 1 篇，据此重写主线会跑偏——已有主线就保留
    old = con.execute("SELECT theme FROM deep_meta WHERE date=?", (day,)).fetchone()
    if theme and (force or not (old and old["theme"])):
        con.execute(
            "INSERT OR REPLACE INTO deep_meta(date,theme) VALUES(?,?)", (day, theme))
        con.commit()
    total = con.execute("SELECT COUNT(*) FROM deep_reads WHERE date=?", (day,)).fetchone()[0]
    if total:
        _renumber(con, day)
    log(f"精读：{day} 本轮新增 {ok}/{len(picks)} 篇，当天共 {total} 篇")
    return total
