"""SQLite 持久层。

数据模型（第一性原理）
======================
一篇文章 = 固有属性 + 两份独立评估 + 一份物化展示：

- 固有属性  source/channel/title/url/date/author/body
- 规则评估  rule_score / rule_topics / rule_tracks / rule_reason / rule_signals
            —— classify 的输出，本地重算、零成本，可随时覆盖
- AI 评估   summary / angles / ai_score / ai_topics / ai_tracks / ai_why / ai_note / ai_at
            —— LLM 研判输出，计费、幂等：ai_at 非空即已处理（含敏感词拦截，
               拦截原因记 ai_note），绝不重复请求
- 背诵点    memorable —— 独立于两套评估的第三个花钱环节，非空即已提取
- 展示层    score / topics / tracks / reason —— 物化列，恒等于「有 AI 用 AI，没有用规则」

两条铁律：
1. 两个评估引擎互不覆盖：重算规则分永远不会抹掉 AI 结果，反之亦然。
   （旧模型里 score/reason 被两个引擎轮流覆盖，是本次重构要修掉的核心缺陷。）
2. 每个计费环节一个幂等标记：ai_at / memorable 非空即跳过，不重复计费。

物化边界：展示列只物化 score/topics/tracks/reason 四列，到此为止——
再物化更多列会让 update_display 的隐式合同（5 个写路径都要记得调用）继续膨胀。
"""
import hashlib
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kaogong.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE,
    source      TEXT NOT NULL,
    channel     TEXT,
    title       TEXT NOT NULL,
    url         TEXT UNIQUE NOT NULL,
    date        TEXT,
    author      TEXT,
    body        TEXT,
    topics      TEXT,
    tracks      TEXT,
    score       INTEGER DEFAULT 0,
    reason      TEXT,
    fetched_at  TEXT,
    summary     TEXT,
    angles      TEXT,
    ai_score    INTEGER,
    ai_at       TEXT,
    memorable   TEXT,
    rule_score    INTEGER,
    rule_topics   TEXT,
    rule_tracks   TEXT,
    rule_reason   TEXT,
    rule_signals  TEXT,
    ai_topics     TEXT,
    ai_tracks     TEXT,
    ai_why        TEXT,
    ai_note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_date  ON articles(date);
CREATE INDEX IF NOT EXISTS idx_score ON articles(score);
CREATE INDEX IF NOT EXISTS idx_src   ON articles(source);

-- 每日精读：一天 5 篇，card 是「中心理解速记卡」的 JSON
CREATE TABLE IF NOT EXISTS deep_reads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    article_id  INTEGER NOT NULL,
    rank        INTEGER,
    reason      TEXT,
    card        TEXT,
    created_at  TEXT,
    UNIQUE(date, article_id)
);
CREATE INDEX IF NOT EXISTS idx_deep_date ON deep_reads(date);

-- 每天一条主线：5 篇共同的时政背景
CREATE TABLE IF NOT EXISTS deep_meta (
    date  TEXT PRIMARY KEY,
    theme TEXT
);
"""

# 旧库自动补齐列，不用重建（含 v1 的 5 个 AI 列 + v2 的 9 个分列存储列）
MIGRATIONS = [
    ("summary", "TEXT"),      # AI 一句话摘要
    ("angles", "TEXT"),       # AI 命题角度（JSON 数组）
    ("ai_score", "INTEGER"),  # AI 判定的命题价值
    ("ai_at", "TEXT"),        # AI 研判时间，非空即已处理
    ("memorable", "TEXT"),    # 背诵点（JSON 数组），非空即已提取
    ("rule_score", "INTEGER"),
    ("rule_topics", "TEXT"),
    ("rule_tracks", "TEXT"),
    ("rule_reason", "TEXT"),
    ("rule_signals", "TEXT"),
    ("ai_topics", "TEXT"),
    ("ai_tracks", "TEXT"),
    ("ai_why", "TEXT"),
    ("ai_note", "TEXT"),
]


def _j(v):
    return json.dumps(v, ensure_ascii=False)


def fingerprint(title):
    t = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", title or "")
    return hashlib.md5(t[:40].encode("utf-8")).hexdigest()


def _backfill(con):
    """旧库一次性迁移：把混存在展示列里的规则值 / AI 值拆进各自的列。

    旧模型里 score/topics/tracks/reason 被两个引擎轮流覆盖：
    - ai_at 为空的行，四个列是规则值
    - ai_at 非空且 ai_score 非空的行，是 AI 值（规则值已丢失）
    - ai_at 非空且 ai_score 为空的行（敏感词拦截），reason 是拦截提示

    拆分策略：先把现值按 ai_at 归位到 ai_* 列，再对全库重算规则字段
    （classify 是纯本地函数，秒级），最后 update_display 重建展示列。
    先 fetchall 快照再写，避免边读边写把拷贝源改掉。
    """
    from . import classify  # 延迟导入：classify 不依赖 store，无循环

    rows = con.execute(
        "SELECT id,title,body,source,channel,date,ai_at,ai_score,topics,tracks,reason FROM articles"
    ).fetchall()
    for r in rows:
        if r["ai_at"] is not None:
            if r["ai_score"] is not None:
                con.execute(
                    "UPDATE articles SET ai_topics=?,ai_tracks=?,ai_why=? WHERE id=?",
                    (r["topics"], r["tracks"], r["reason"], r["id"]),
                )
            else:  # 敏感词拦截：reason 里存的是拦截提示
                con.execute(
                    "UPDATE articles SET ai_note=? WHERE id=?", (r["reason"], r["id"])
                )
        cls = classify.classify({
            "title": r["title"] or "", "body": r["body"] or "",
            "source": r["source"] or "", "channel": r["channel"] or "",
            "date": r["date"] or "",
        })
        con.execute(
            """UPDATE articles SET rule_score=?,rule_topics=?,rule_tracks=?,rule_reason=?,rule_signals=?
               WHERE id=?""",
            (cls["score"], _j(cls["topics"]), _j(cls["tracks"]),
             cls["reason"], _j(cls["signals"]), r["id"]),
        )
    update_display(con, *[r["id"] for r in rows])


def connect(db_path=None):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(articles)")}
    for name, typ in MIGRATIONS:
        if name not in cols:
            con.execute(f"ALTER TABLE articles ADD COLUMN {name} {typ}")
    con.commit()
    # rule_score 全空说明是旧库或新库首轮写入前——只有存在旧行时才需要回填
    if con.execute("SELECT 1 FROM articles WHERE rule_score IS NULL LIMIT 1").fetchone():
        _backfill(con)
        con.commit()
    return con


def update_display(con, *ids):
    """重建展示列：有 AI 用 AI，没有用规则。纯 SQL，可批量。"""
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    con.execute(
        f"""UPDATE articles SET
              score  = COALESCE(ai_score, rule_score),
              topics = COALESCE(ai_topics, rule_topics),
              tracks = COALESCE(ai_tracks, rule_tracks),
              reason = CASE WHEN ai_score IS NOT NULL AND ai_why IS NOT NULL
                            THEN ai_why ELSE rule_reason END
            WHERE id IN ({marks})""",
        ids,
    )


def exists(con, url, fp):
    cur = con.execute("SELECT 1 FROM articles WHERE url=? OR fingerprint=?", (url, fp))
    return cur.fetchone() is not None


def save(con, art, cls):
    """新文章入库：写固有属性 + 规则评估，展示列初始化为规则值。"""
    fp = fingerprint(art.title)
    if exists(con, art.url, fp):
        return False
    cur = con.execute(
        """INSERT OR IGNORE INTO articles
           (fingerprint,source,channel,title,url,date,author,body,
            rule_score,rule_topics,rule_tracks,rule_reason,rule_signals,
            score,topics,tracks,reason,fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            fp, art.source, art.channel, art.title, art.url, art.date, art.author, art.body,
            cls["score"], _j(cls["topics"]), _j(cls["tracks"]), cls["reason"], _j(cls["signals"]),
            cls["score"], _j(cls["topics"]), _j(cls["tracks"]), cls["reason"],
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    con.commit()
    return cur.rowcount > 0  # exists 预检失效（IGNORE 兜底触发）时如实返回未新增


def load_all(con, days=None, min_score=0):
    sql = "SELECT * FROM articles WHERE score>=?"
    args = [min_score]
    if days:
        # 本地时区计算下界——date('now') 是 UTC，UTC+8 下凌晨会少算一天
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        sql += " AND date>=?"
        args.append(cutoff)
    sql += " ORDER BY score DESC, date DESC"
    rows = [dict(r) for r in con.execute(sql, args)]
    for r in rows:
        for k in ("topics", "tracks", "rule_signals", "angles", "memorable"):
            try:
                r[k] = json.loads(r.get(k) or ("{}" if k == "rule_signals" else "[]"))
            except Exception:
                r[k] = {} if k == "rule_signals" else []
    return rows


def pending_ai(con, limit=40, min_score=0, max_age_days=None):
    """还没被 AI 研判过的候选，命题价值（展示分）高的优先。

    score 是展示列：未处理的文章恒为规则分，口径统一。
    只过滤正文过短的（短讯没有研判价值）。
    max_age_days：超龄文章不进候选——研判要计费，临期被 prune 的文章
    花了钱也只有几天寿命，不值得。
    """
    sql = """SELECT id,title,source,channel,body FROM articles
             WHERE ai_at IS NULL AND score>=? AND length(body)>=180"""
    args = [min_score]
    if max_age_days:
        sql += " AND date>=?"
        args.append((date.today() - timedelta(days=max_age_days)).isoformat())
    sql += " ORDER BY score DESC LIMIT ?"
    args.append(limit)
    return [tuple(r) for r in con.execute(sql, args)]


def save_ai(con, aid, res):
    now = datetime.now().isoformat(timespec="seconds")
    if res.get("blocked"):
        # 敏感词拦截：只记 ai_note + ai_at，规则评估与展示列原样保留
        con.execute(
            "UPDATE articles SET ai_note=?, ai_at=? WHERE id=?",
            (res.get("why") or "接口输入敏感词拦截，未做 AI 研判", now, aid),
        )
        con.commit()
        return
    con.execute(
        """UPDATE articles SET summary=?,angles=?,ai_score=?,
               ai_topics=?,ai_tracks=?,ai_why=?,ai_at=? WHERE id=?""",
        (
            res.get("summary", ""),
            _j(res.get("angles", [])),
            res.get("score"),
            _j(res.get("topics", [])),
            _j(res.get("tracks", [])),
            res.get("why", ""),
            now, aid,
        ),
    )
    update_display(con, aid)
    con.commit()


def pending_memo(con, limit=200, min_score=80, max_age_days=None):
    """还没提取过背诵点的候选，命题价值（展示分）高的优先。

    与命题研判各自独立：memorable 非空即已提取，不会重复调用。
    min_score 默认 80——低分文章里的段落背诵价值有限，省 token。
    """
    sql = """SELECT id,title,source,channel,body FROM articles
             WHERE memorable IS NULL AND score>=? AND length(body)>=180"""
    args = [min_score]
    if max_age_days:
        sql += " AND date>=?"
        args.append((date.today() - timedelta(days=max_age_days)).isoformat())
    sql += " ORDER BY score DESC LIMIT ?"
    args.append(limit)
    return [tuple(r) for r in con.execute(sql, args)]


def save_memo(con, aid, items):
    con.execute(
        "UPDATE articles SET memorable=? WHERE id=?",
        (_j(items), aid),
    )
    con.commit()


def save_memo_blocked(con, aid, why):
    """背诵提取被敏感词拦截：memorable 置空列表（幂等，不再重试），
    拦截原因记入 ai_note（若空）——不然后人无从知道这篇为什么没有背诵点。"""
    con.execute("UPDATE articles SET memorable='[]' WHERE id=?", (aid,))
    con.execute(
        "UPDATE articles SET ai_note=? WHERE id=? AND (ai_note IS NULL OR ai_note='')",
        (f"背诵提取被敏感词拦截：{why}", aid),
    )
    con.commit()


def prune(con, keep_days=30):
    """只保留最近 N 天，超期删除。返回删除条数。"""
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    cur = con.execute("DELETE FROM articles WHERE date < ?", (cutoff,))
    con.commit()
    return cur.rowcount


def stats(con):
    cur = con.execute("SELECT COUNT(*) c, MIN(date) a, MAX(date) b, AVG(score) s FROM articles")
    r = cur.fetchone()
    return {"total": r["c"] or 0, "first": r["a"], "last": r["b"], "avg": round(r["s"] or 0, 1)}


def load_deep(con, days=14):
    """取最近 N 天的精读卡片，按日期倒序、当日 rank 升序。

    正文一并取出：精读页要同时展示原文和拆解，分开取会让页面多一轮请求。
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT d.date, d.rank, d.reason, d.card,
                  a.id, a.title, a.source, a.channel, a.url, a.date AS adate,
                  a.author, a.body, a.score, a.topics, a.tracks
           FROM deep_reads d JOIN articles a ON a.id = d.article_id
           WHERE d.date >= ?
           ORDER BY d.date DESC, d.rank ASC""",
        (cutoff,),
    ).fetchall()
    themes = {r["date"]: r["theme"] for r in con.execute("SELECT date,theme FROM deep_meta")}

    out = []
    for r in rows:
        d = dict(r)
        try:
            d["card"] = json.loads(d["card"] or "{}")
        except Exception:
            d["card"] = {}
        for k in ("topics", "tracks"):
            try:
                d[k] = json.loads(d[k] or "[]")
            except Exception:
                d[k] = []
        d["theme"] = themes.get(d["date"], "")
        out.append(d)
    return out

