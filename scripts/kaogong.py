#!/usr/bin/env python3
"""考公素材库 skill 的唯一入口。用户和助手都直接跑这个脚本。

设计取向：按需抓取，不囤数据。
库里只留用户这次要看的那几天，抓完即用、用完即弃，不建站、不起常驻服务。

    kaogong.py show --days 1              看今天（缺数据自动补抓）
    kaogong.py show --days 3 --topic 基层治理
    kaogong.py search 乡村振兴 --days 7
    kaogong.py fetch --days 5             显式抓 5 天
    kaogong.py memo --days 2              可直接背的段落
    kaogong.py deep --days 1              每日精读速记卡
    kaogong.py stats                      库里现在有什么
    kaogong.py clean                      清空缓存
    kaogong.py doctor                     环境自检

抓取复用主仓库的 scraper 包（五源列表解析 / 正文清洗 / 规则打分），
数据落到用户缓存目录，不碰仓库里的 data/kaogong.db。
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

VERSION = "2.0"
# 记住「哪些日期已经抓过」，避免反复抓同一个空日期（党媒周末版面少）
COVERED = "covered.json"


# ---------------------------------------------------------------- 位置

def data_dir():
    """缓存目录：用户自己的临时区，不落在仓库里。"""
    env = (os.environ.get("KAOGONG_DATA") or "").strip()
    if env:
        return os.path.abspath(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "kaogong-skill")
    return os.path.join(os.path.expanduser("~"), ".cache", "kaogong-skill")


def db_path():
    return os.path.join(data_dir(), "kaogong.db")


def find_root():
    """定位 scraper 包所在目录。

    顺序：KAOGONG_HOME → skill 目录旁的 config.json → skill 上一级
    （引擎与 skill 同一仓库的发布形态）→ 常见默认路径。
    """
    cands = []
    env = (os.environ.get("KAOGONG_HOME") or "").strip()
    if env:
        cands.append(env)
    here = os.path.dirname(os.path.abspath(__file__))
    for cfg in (os.path.join(os.path.dirname(here), "config.json"),
                os.path.join(here, "config.json")):
        try:
            with open(cfg, encoding="utf-8") as f:
                p = (json.load(f).get("home") or "").strip()
            if p:
                cands.append(p)
        except Exception:
            pass
    # 引擎与 skill 同仓：发布版在上一级，主仓库在 ../../（skill 嵌在仓库里）
    cands.append(os.path.dirname(here))
    cands.append(os.path.dirname(os.path.dirname(here)))
    cands.append(os.path.expanduser("~/gong/platform"))
    for c in cands:
        if os.path.isfile(os.path.join(c, "scraper", "store.py")):
            return os.path.abspath(c)
    return None


def load_engine():
    root = find_root()
    if not root:
        sys.exit(
            "找不到 scraper 包（抓取引擎）。\n"
            "修复方式二选一：\n"
            "  1. 设环境变量 KAOGONG_HOME 指向包含 scraper/ 的目录\n"
            "  2. 在 skill 目录下建 config.json：{\"home\": \"<那个目录>\"}\n"
            "仓库自带 scraper/ 时，在仓库根目录直接运行 scripts/kaogong.py 即可。"
        )
    if root not in sys.path:
        sys.path.insert(0, root)
    from scraper import classify, sources, store  # noqa: E402
    from scraper import run as runner  # noqa: E402
    return root, classify, sources, store, runner


# ---------------------------------------------------------------- 已抓日期

def _covered_file():
    return os.path.join(data_dir(), COVERED)


def load_covered():
    try:
        with open(_covered_file(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def mark_covered(day, n):
    d = load_covered()
    d[day] = {"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "n": n}
    try:
        os.makedirs(data_dir(), exist_ok=True)
        with open(_covered_file(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def clear_covered():
    try:
        os.remove(_covered_file())
    except Exception:
        pass


# ---------------------------------------------------------------- 抓取

def do_fetch(days, limit=20, ai=False, ai_top=40, memo=False, memo_min=80,
             memo_top=30, keep=None, refresh=False, quiet=False):
    """抓最近 days 天。返回 (新增篇数, 实际抓的天数)。"""
    _root, classify, _sources, store, runner = load_engine()
    con = store.connect(db_path())
    covered = load_covered()

    todo = []
    for i in range(days):
        d = (date.today() - timedelta(days=i)).isoformat()
        if refresh or d not in covered:
            todo.append(d)

    if not todo and not (ai or memo):
        if not quiet:
            print(f"最近 {days} 天的数据都已在本地，无需重抓。"
                  f"（要重抓加 --refresh；要 AI 研判加 --ai）")
        return 0, 0

    if not quiet:
        if todo:
            print(f"抓取 {len(todo)} 天：{'、'.join(todo)}")
        else:
            print("文章已在本地，只补跑后续环节。")

    total_new = 0
    t0 = time.time()
    for i, day_s in enumerate(todo):
        day = datetime.strptime(day_s, "%Y-%m-%d").date()
        # 频道页类源只反映「当前最新」，只有第一天需要抓
        is_today = (day == date.today())
        if not quiet:
            print(f"  [{day_s}] 抓取中…", flush=True)
        try:
            arts = runner.crawl_day(day, limit, skip_live=not is_today)
        except Exception as e:
            print(f"  ! {day_s} 抓取失败：{type(e).__name__}: {e}")
            continue
        new = 0
        for a in arts:
            try:
                if store.save(con, a, classify.classify(a)):
                    new += 1
            except Exception:
                pass
        total_new += new
        mark_covered(day_s, len(arts))
        if not quiet:
            print(f"  → 有效 {len(arts)} 篇，新增 {new} 篇")

    if ai:
        if not quiet:
            print(f"AI 研判（最多 {ai_top} 篇，会计费）…")
        try:
            runner.run_ai(con, limit=ai_top)
        except Exception as e:
            print(f"  ! AI 研判失败：{type(e).__name__}: {e}")

    if memo:
        if not quiet:
            print(f"背诵点提取（≥{memo_min} 分，最多 {memo_top} 篇）…")
        try:
            runner.run_memo(con, limit=memo_top, min_score=memo_min)
        except Exception as e:
            print(f"  ! 背诵提取失败：{type(e).__name__}: {e}")

    # 只留用户要的天数，不囤积
    keep_n = keep if keep else days
    gone = store.prune(con, keep_n)

    if not quiet:
        s = store.stats(con)
        print(f"完成：新增 {total_new} 篇｜库内 {s['total']} 篇"
              f"（{s['first']} ~ {s['last']}）｜清理 {gone} 篇"
              f"｜{time.time() - t0:.0f}s")
    con.close()
    return total_new, len(todo)


# ---------------------------------------------------------------- 查询

def _loads(v, default):
    if v is None or v == "":
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def _rows(days, topic=None, track=None, source=None, min_score=0, limit=50):
    _root, _c, _s, store, _r = load_engine()
    con = store.connect(db_path())
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    sql = "SELECT * FROM articles WHERE date >= ?"
    args = [since]
    if source:
        sql += " AND source = ?"
        args.append(source)
    if min_score:
        sql += " AND score >= ?"
        args.append(min_score)
    sql += " ORDER BY date DESC, score DESC"
    out = []
    for r in con.execute(sql, args):
        d = dict(r)
        d["topics"] = _loads(d.get("topics"), [])
        d["tracks"] = _loads(d.get("tracks"), [])
        d["angles"] = _loads(d.get("angles"), [])
        if topic and topic not in d["topics"]:
            continue
        if track and track not in d["tracks"]:
            continue
        out.append(d)
    con.close()
    return out[:limit]


def _card(a, idx, body=None):
    tags = "、".join((a.get("tracks") or []) + (a.get("topics") or []))
    wc = len(body or "") or len(a.get("body") or "")
    lines = [f"## {idx}. {a['title']}", "",
             f"{a['source']} {a.get('channel') or ''}｜{a.get('date')}｜"
             f"{wc} 字｜命题价值 {a.get('score') or 0}"]
    if tags:
        lines.append(f"标签：{tags}")
    if a.get("summary"):
        lines += ["", f"**核心观点**：{a['summary']}"]
    elif a.get("reason"):
        lines += ["", f"**判定依据**：{a['reason']}"]
    if a.get("angles"):
        lines += ["", "**可命题角度**"] + [f"- {x}" for x in a["angles"]]
    lines += ["", f"原文：{a['url']}", "", "---", ""]
    return "\n".join(lines)


def cmd_show(args):
    if not args.no_fetch:
        do_fetch(args.days, limit=args.limit, keep=args.keep,
                 refresh=args.refresh, quiet=args.quiet)
    rows = _rows(args.days, topic=args.topic, track=args.track,
                 source=args.source, min_score=args.min_score, limit=args.limit)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    span = f"最近 {args.days} 天" if args.days > 1 else "今天"
    if args.days == 1:
        span = f"今天（{date.today().isoformat()}）"
    cond = [span]
    if args.topic:
        cond.append(f"议题={args.topic}")
    if args.track:
        cond.append(f"用途={args.track}")
    if args.source:
        cond.append(f"来源={args.source}")
    if args.min_score:
        cond.append(f"≥{args.min_score} 分")

    if not rows:
        print(f"# 考公素材 · {'｜'.join(cond)}\n")
        print("没有符合条件的文章。可能是当天党媒没有新内容，"
              "或者筛选太窄——放宽 `--days`、去掉 `--topic`/`--track` 再试。")
        return 0

    out = [f"# 考公素材 · {'｜'.join(cond)}", "",
           f"共 {len(rows)} 篇，按日期与命题价值排序", "", "---", ""]
    for i, a in enumerate(rows, 1):
        out.append(_card(a, i))
    print("\n".join(out))
    return 0


def cmd_search(args):
    if not args.no_fetch:
        do_fetch(args.days, limit=args.limit, keep=args.keep, quiet=args.quiet)
    _root, _c, _s, store, _r = load_engine()
    con = store.connect(db_path())
    since = (date.today() - timedelta(days=args.days - 1)).isoformat()
    kw = args.keyword.lower()
    hits = []
    for r in con.execute(
            "SELECT * FROM articles WHERE date >= ? ORDER BY date DESC, score DESC",
            (since,)):
        d = dict(r)
        blob = " ".join(str(d.get(k) or "") for k in
                        ("title", "summary", "reason", "body")).lower()
        if kw in blob:
            d["topics"] = _loads(d.get("topics"), [])
            d["tracks"] = _loads(d.get("tracks"), [])
            d["angles"] = _loads(d.get("angles"), [])
            hits.append(d)
    con.close()
    hits = hits[:args.limit]

    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return 0
    if not hits:
        print(f"# 搜索「{args.keyword}」\n\n最近 {args.days} 天没有匹配的文章。"
              f"换个词，或加大 `--days`。")
        return 0
    out = [f"# 搜索「{args.keyword}」", "", f"最近 {args.days} 天命中 {len(hits)} 篇", "", "---", ""]
    for i, a in enumerate(hits, 1):
        out.append(_card(a, i))
    print("\n".join(out))
    return 0


def cmd_memo(args):
    if not args.no_fetch:
        do_fetch(args.days, limit=args.limit, keep=args.keep, quiet=args.quiet)
    _root, _c, _s, store, _r = load_engine()
    con = store.connect(db_path())
    since = (date.today() - timedelta(days=args.days - 1)).isoformat()
    rows = []
    for r in con.execute(
            "SELECT * FROM articles WHERE date >= ? AND memorable IS NOT NULL "
            "AND memorable != '' ORDER BY date DESC, score DESC", (since,)):
        d = dict(r)
        items = _loads(d.get("memorable"), [])
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            if args.kind and it.get("kind") != args.kind:
                continue
            rows.append((d, it))
    con.close()
    rows = rows[:args.limit]

    if not rows:
        print(f"# 申论背诵本\n\n最近 {args.days} 天没有背诵点。\n\n"
              f"背诵提取要单独跑（会计费）：\n"
              f"  `kaogong.py fetch --days {args.days} --memo`\n"
              f"只提取 ≥80 分的文章，低分文章里的段落背诵价值有限。")
        return 0

    out = ["# 申论背诵本", "", f"最近 {args.days} 天，共 {len(rows)} 段"
           + (f"（{args.kind}）" if args.kind else ""), ""]
    for d, it in rows:
        out += [f"## [{it.get('kind')}]", "", "<第三方原文>",
                str(it.get("text", "")), "</第三方原文>", ""]
        if it.get("usage"):
            out.append(f"- 怎么用：{it['usage']}")
        out.append(f"- 议题：{'、'.join(it.get('topics') or []) or '—'}"
                   f"｜来源：{d['source']} {d['date']}｜命题价值 {d.get('score')}")
        out.append(f"- 出处：{d['title']}")
        out.append(f"- 原文：{d['url']}")
        out.append("")
    print("\n".join(out))
    return 0


def cmd_deep(args):
    if not args.no_fetch:
        do_fetch(args.days, limit=args.limit, keep=args.keep, quiet=args.quiet)
    _root, _c, _s, store, runner = load_engine()
    con = store.connect(db_path())
    for i in range(args.days):
        d = (date.today() - timedelta(days=i)).isoformat()
        print(f"精读 {d}…")
        try:
            runner.run_deep(con, day=d, n=args.n, force=args.force)
        except Exception as e:
            print(f"  ! {type(e).__name__}: {e}")
    rows = store.load_deep(con, days=args.days)
    con.close()

    if not rows:
        print("\n没有精读卡片。精读要挑高分文章，先确保有 AI 研判过的文章："
              "`kaogong.py fetch --days 2 --ai`")
        return 0

    out = ["# 每日精读 · 中心理解速记卡", ""]
    last = None
    for r in rows:
        d = dict(r)
        if d["date"] != last:
            out += [f"## {d['date']}", ""]
            last = d["date"]
        c = _loads(d.get("card"), {})
        core = c.get("core") or {}
        out += [f"### {d['rank']}. {d['title']}", "",
                f"{d['source']} {d.get('channel') or ''}｜{d['date']}｜"
                f"{len(d.get('body') or '')} 字｜命题价值 {d.get('score')}"]
        if d.get("reason"):
            out.append(f"- 选它的理由：{d['reason']}")
        out += ["", f"**中心论点**：{c.get('thesis') or '—'}"]
        if c.get("plain"):
            out.append(f"- 白话解读：{c['plain']}")
        if c.get("structure"):
            out += ["", f"**文段结构**：{c['structure']}"]
        if core.get("text"):
            out += ["", f"**中心句**（第 {core.get('para')} 段{core.get('position') or ''}）：", "",
                    "<第三方原文>", core["text"], "</第三方原文>"]
            if core.get("basis"):
                out.append(f"- 判定依据：{core['basis']}")
        for e in c.get("exam") or []:
            out += ["", f"**命题演练·{e.get('type')}**", "", f"{e.get('stem')}", ""]
            for i, o in enumerate(e.get("options") or []):
                out.append(f"{chr(65 + i)}. {o}")
            out += ["", f"- 答案：{e.get('answer')}"]
            if e.get("why"):
                out.append(f"- 解析：{e['why']}")
        sl = c.get("shenlun") or {}
        if sl.get("measures") or sl.get("phrases"):
            out += ["", "**申论迁移**", ""]
            out += [f"- 对策：{x}" for x in sl.get("measures") or []]
            out += [f"- 表述：{x}" for x in sl.get("phrases") or []]
        if c.get("mnemonic"):
            out += ["", f"**速记**：{c['mnemonic']}"]
        out += ["", f"原文：{d.get('url')}", "", "---", ""]
    print("\n".join(out))
    return 0


def cmd_stats(args):
    _root, _c, _s, store, _r = load_engine()
    if not os.path.exists(db_path()):
        print("本地还没有数据。先跑 `kaogong.py fetch --days 3`。")
        return 0
    con = store.connect(db_path())
    s = store.stats(con)
    by_src = {}
    by_day = {}
    ai_n = memo_n = 0
    for r in con.execute("SELECT source, date, ai_at, memorable FROM articles"):
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
        by_day[r["date"]] = by_day.get(r["date"], 0) + 1
        if r["ai_at"]:
            ai_n += 1
        if r["memorable"]:
            memo_n += 1
    con.close()

    print("# 库内现状\n")
    print(f"- 缓存位置：{db_path()}")
    print(f"- 总篇数：{s['total']}")
    print(f"- 时间范围：{s['first'] or '—'} ~ {s['last'] or '—'}")
    print(f"- 平均分：{s['avg']}")
    print(f"- AI 研判：{ai_n} 篇｜背诵点：{memo_n} 篇")
    if by_day:
        print("\n## 按日期\n")
        for d in sorted(by_day, reverse=True):
            print(f"- {d}：{by_day[d]} 篇")
    if by_src:
        print("\n## 按来源\n")
        for k, v in sorted(by_src.items(), key=lambda x: -x[1]):
            print(f"- {k}：{v} 篇")
    print(f"\n占用磁盘：{_size(data_dir()):.1f} MB")
    return 0


def _size(p):
    t = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                t += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return t / 1024 / 1024


def cmd_clean(args):
    n = 0
    for f in ("kaogong.db", "kaogong.db-wal", "kaogong.db-shm", COVERED):
        p = os.path.join(data_dir(), f)
        if os.path.exists(p):
            try:
                os.remove(p)
                n += 1
            except Exception as e:
                print(f"删不掉 {f}：{e}")
    print(f"已清空缓存（{n} 个文件）：{data_dir()}" if n else "缓存本来就是空的。")
    return 0


def cmd_doctor(args):
    root = find_root()
    print(f"skill 版本：{VERSION}")
    print(f"Python：{sys.version.split()[0]}  {sys.executable}")
    print(f"缓存目录：{data_dir()}")
    print(f"  存在：{os.path.isdir(data_dir())}")
    print(f"主仓库：{root or '未找到'}")
    if not root:
        return 1
    try:
        _r, _c, sources, _s, _r2 = load_engine()
        print(f"  已接入源：{'、'.join(sources.LISTERS)}")
    except Exception as e:
        print(f"  载入失败：{type(e).__name__}: {e}")
        return 1
    if os.path.exists(db_path()):
        _root, _c2, _s2, store, _r3 = load_engine()
        con = store.connect(db_path())
        s = store.stats(con)
        con.close()
        print(f"本地数据：{s['total']} 篇（{s['first'] or '—'} ~ {s['last'] or '—'}）"
              f"｜{_size(data_dir()):.1f} MB")
    else:
        print("本地数据：无（首次运行会自动抓取）")
    print(f"已抓日期：{len(load_covered())} 天")
    return 0


# ---------------------------------------------------------------- CLI

def build_parser():
    ap = argparse.ArgumentParser(
        prog="kaogong", description="考公素材库：按需抓取党媒评论，用完即弃")
    ap.add_argument("--version", action="version", version=f"kaogong {VERSION}")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--days", type=int, default=1,
                       help="看最近几天，1—30（默认 1）")
        p.add_argument("--limit", type=int, default=20,
                       help="每源每天抓取上限 / 返回条数上限（默认 20）")
        p.add_argument("--keep", type=int, default=None,
                       help="本地只保留几天，默认等于 --days")
        p.add_argument("--no-fetch", action="store_true",
                       help="只查本地已有数据，不联网抓取")
        p.add_argument("--refresh", action="store_true",
                       help="忽略已抓记录，强制重抓")
        p.add_argument("--quiet", action="store_true", help="不打印抓取进度")

    p = sub.add_parser("show", help="看当天或最近几天")
    common(p)
    p.add_argument("--topic", help="议题，如 基层治理、乡村振兴、科技创新")
    p.add_argument("--track", help="用途：申论素材/言语语料/常识时政/面试热点")
    p.add_argument("--source", help="来源：人民日报/光明网/半月谈/中国青年报/经济日报")
    p.add_argument("--min-score", type=int, default=0, help="最低命题价值")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("fetch", help="显式抓取（可带 AI 研判）")
    common(p)
    p.add_argument("--ai", action="store_true", help="启用 AI 研判（会计费）")
    p.add_argument("--ai-top", type=int, default=40, help="AI 处理篇数上限")
    p.add_argument("--memo", action="store_true", help="同时提取背诵点（会计费）")
    p.add_argument("--memo-min", type=int, default=80, help="背诵提取最低分")
    p.add_argument("--memo-top", type=int, default=30, help="背诵提取篇数上限")

    def _fetch(a):
        do_fetch(a.days, limit=a.limit, ai=a.ai, ai_top=a.ai_top,
                 memo=a.memo, memo_min=a.memo_min, memo_top=a.memo_top,
                 keep=a.keep, refresh=a.refresh, quiet=a.quiet)
        return 0
    p.set_defaults(func=_fetch)

    p = sub.add_parser("search", help="关键词搜索")
    common(p)
    p.add_argument("keyword", help="关键词")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("memo", help="可直接背的段落")
    common(p)
    p.add_argument("--kind", help="论点段/案例段/规范段/金句段")
    p.set_defaults(func=cmd_memo)

    p = sub.add_parser("deep", help="每日精读速记卡（会计费）")
    common(p)
    p.add_argument("--n", type=int, default=5, help="每天几篇（默认 5）")
    p.add_argument("--force", action="store_true", help="已有也重做")
    p.set_defaults(func=cmd_deep)

    p = sub.add_parser("stats", help="库里现在有什么")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("clean", help="清空本地缓存")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("doctor", help="环境自检")
    p.set_defaults(func=cmd_doctor)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    if hasattr(args, "days") and not (1 <= args.days <= 30):
        sys.exit("--days 要在 1—30 之间")
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
