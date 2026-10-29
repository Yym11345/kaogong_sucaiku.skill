"""把数据库渲染成「壳 + 元数据 + 正文分片」的静态站点。

为什么不再用单文件
------------------
单文件内嵌全文在 250 篇时是 1.2MB，可以接受；但存一个月会到 3000 篇、14MB，
手机根本加载不了。所以拆成三层：

    index.html            壳（模板 + 渲染逻辑）
    data/meta.json        全部文章元数据（无正文，约 200 字节/篇）
    data/bodies/日期.json  当天所有正文，点开某篇时才按日期加载该分片

首屏只需 meta.json；正文按天懒加载，一天一个分片（30 天 = 30 个文件）。

发布顺序（原子性约定）
---------------------
先写正文分片与背诵清单，meta.json 最后落盘：meta 是唯一的清单/版本标记，
读者（网页与 /api）以 meta.json 的 mtime 为缓存失效依据——它落盘时，
它引用的所有分片已经在位。保证的是「新 meta 引用的分片必在位」；
反向窗口（新分片 + 旧 meta）存在数秒：被本轮删除的文章在窗口内
正文取不到，属可接受的降级，不会出现半写文件。
"""
import html
import json
import os
import time
from datetime import date, datetime

from . import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _paths(root):
    return {
        "tpl": os.path.join(root, "scraper", "template.html"),
        "out": os.path.join(root, "web", "index.html"),
        "memo_tpl": os.path.join(root, "scraper", "memorize.html"),
        "memo_out": os.path.join(root, "web", "memorize.html"),
        "deep_tpl": os.path.join(root, "scraper", "deep.html"),
        "deep_out": os.path.join(root, "web", "deep.html"),
        "online_cfg": os.path.join(root, "config", "online.json"),
        "data": os.path.join(root, "web", "data"),
    }


def _data_base(p):
    """线上瘦客户端拉数据的基地址（公网隧道地址）。

    本地留空 = 同源相对路径，本地用法完全不变；
    线上填隧道地址，页面在运行时去隧道取数据，发一次版就永远是最新的。
    """
    try:
        with open(p, encoding="utf-8") as f:
            base = (json.load(f).get("data_base") or "").strip()
    except Exception:
        base = ""
    return base.rstrip("/") + "/" if base else ""


def _angles(r):
    """规则兜底：AI 没研判过时，用文本特征拼出命题角度。"""
    out = []
    tracks = r.get("tracks") or []
    sig = r.get("rule_signals") or {}
    body = r.get("body") or ""
    paras = [p for p in body.split("\n") if len(p) >= 60]

    if "申论素材" in tracks:
        out.append(f"归纳概括：全文 {len(body)} 字，可截取主体段落设「概括主要做法／成效／问题」")
        if sig.get("measures", 0) >= 4:
            out.append(f"提出对策：文中 {sig['measures']} 处对策表述，可设「就…提出建议」")
    if "言语语料" in tracks and paras:
        longest = max(paras, key=len)
        idx = body.split("\n").index(longest) + 1
        out.append(f"片段阅读：第 {idx} 段（{len(longest)} 字）论点完整，适合设主旨概括／意图判断")
    if "常识时政" in tracks:
        out.append("常识判断：留意文中政策文件名、时间节点与主管部门")
    if "面试热点" in tracks:
        out.append("面试综合分析：可作「谈谈你的看法」的背景材料，按表态—分析—对策组织")
    return out[:4] or ["可作为背景阅读，积累规范表述与政策语汇"]


def _short(t, n=22):
    core = t.split("（")[0].split("(")[0].strip()
    return core if len(core) <= n else core[:n] + "…"


def _insight(arts, today):
    from collections import Counter

    pool = [a for a in arts if a["date"] == today] or arts
    if not pool:
        return ""
    hot = [a for a in pool if a["score"] >= 80]
    cnt = Counter()
    for a in pool:
        for tp in a["topics"][:2]:
            if tp != "综合":
                cnt[tp] += 1
    tops = cnt.most_common(3)
    lead = max(pool, key=lambda a: a["score"])

    # 标题来自第三方网站，是不可信输入：进 brief（innerHTML）前必须转义
    parts = [f"今日入库 <b>{len(pool)}</b> 篇，其中 <b>{len(hot)}</b> 篇达到高价值线（80 分以上）。"]
    if tops:
        parts.append("议题集中在 " + "、".join(f"<b>{html.escape(k)}</b>（{v}）" for k, v in tops) + "。")
    strong = [a for a in pool if a["ai"] and a["score"] >= 80][:2]
    if strong:
        parts.append("申论建议优先看 " + "、".join(f"《{html.escape(_short(a['title']))}》" for a in strong)
                     + "，模型已标出可命题段落。")
    else:
        parts.append(f"当前最高分是《{html.escape(_short(lead['title'], 26))}》。")
    return "".join(parts)


def _replace(tmp, path, attempts=6):
    """os.replace 带退避重试。

    Windows 下目标文件正被本地服务读着（serve.py 传大分片时句柄可占数秒），
    os.replace 会抛 PermissionError——重试几次等读者放手，比整轮构建崩溃好。
    """
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.2 * (i + 1))


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    _replace(tmp, path)


def _publish(data_dir, bodies, memorable, meta, deep=None):
    """按原子性约定落盘：分片 → 背诵清单 → 精读 → meta，最后清掉不再被引用的旧文件。"""
    body_dir = os.path.join(data_dir, "bodies")
    os.makedirs(body_dir, exist_ok=True)

    wanted = set()
    for d, bmap in bodies.items():
        p = os.path.join(body_dir, f"{d}.json")
        _write_json(p, bmap)
        wanted.add(os.path.normcase(p))
    _write_json(os.path.join(data_dir, "memorable.json"), memorable)
    wanted.add(os.path.normcase(os.path.join(data_dir, "memorable.json")))
    if deep is not None:
        _write_json(os.path.join(data_dir, "deepread.json"), deep)
        wanted.add(os.path.normcase(os.path.join(data_dir, "deepread.json")))
    _write_json(os.path.join(data_dir, "meta.json"), meta)
    wanted.add(os.path.normcase(os.path.join(data_dir, "meta.json")))

    # 清理旧分片与残留 .tmp（构建是单进程互斥的，不会有并发写入中的 .tmp）
    for base, _, files in os.walk(data_dir):
        for f in files:
            p = os.path.normcase(os.path.join(base, f))
            if p not in wanted:
                try:
                    os.remove(p)
                except OSError:
                    pass


def _deep(con, days=14):
    """每日精读：按天分组，正文随卡片一起给——精读页要原文和拆解并排看。"""
    out, order = {}, []
    for r in store.load_deep(con, days=days):
        d = r["date"]
        if d not in out:
            out[d] = {"date": d, "theme": r.get("theme") or "", "items": []}
            order.append(d)
        body = r.get("body") or ""
        out[d]["items"].append({
            "id": r["id"], "rank": r["rank"], "reason": r.get("reason") or "",
            "card": r.get("card") or {},
            "title": r["title"], "source": r["source"], "channel": r["channel"],
            "url": r["url"], "author": r.get("author") or "", "date": r["date"],
            "score": r["score"], "topics": r["topics"], "tracks": r["tracks"],
            "body": body, "wc": len(body),
        })
    return [out[d] for d in order]


def build(con=None, keep_days=30, root=ROOT):
    close = False
    if con is None:
        con, close = store.connect(), True

    rows = store.load_all(con, days=keep_days)
    arts, bodies, memos = [], {}, []
    for r in rows:
        if not r.get("body") or not r.get("date"):
            continue
        ai_angles = r.get("angles") or []
        memo = r.get("memorable") or []
        # 列表页只需摘要级信息；正文与命题角度放进分片，点开才加载
        arts.append({
            "id": r["id"], "source": r["source"], "channel": r["channel"],
            "title": r["title"], "url": r["url"], "date": r["date"],
            "author": r["author"] or "",
            "wc": len(r["body"]),
            "topics": r["topics"], "tracks": r["tracks"],
            "score": r["score"], "reason": r["reason"],
            "summary": r.get("summary") or "",
            # ai_at 非空表示「已处理」，但敏感词拦截的文章没有 AI 结果，不该打 AI 标记
            "ai": bool(r.get("ai_at")) and r.get("ai_score") is not None,
            "memo": len(memo),
        })
        bodies.setdefault(r["date"], {})[str(r["id"])] = {
            "b": r["body"],
            "a": ai_angles if ai_angles else _angles(r),
        }
        # 背诵点汇总成一份清单：句子是独立资产，不必再回原正文里翻
        for m in memo:
            memos.append({
                "id": r["id"], "kind": m.get("kind", "论点段"),
                "text": m.get("text", ""), "usage": m.get("usage", ""),
                "title": r["title"], "source": r["source"], "date": r["date"],
                "channel": r["channel"], "score": r["score"],
                "topics": r["topics"], "url": r["url"],
            })

    dates = sorted({a["date"] for a in arts if a["date"]})
    today = dates[-1] if dates else date.today().isoformat()
    meta = {
        "total": len(arts),
        "today": today,
        "stamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "brief": _insight(arts, today),
        "ai": sum(1 for a in arts if a["ai"]),
        "memo": len(memos),
        "days": len(dates),
        "span": f"{dates[0]} ~ {dates[-1]}" if dates else "",
    }

    p = _paths(root)
    os.makedirs(os.path.join(root, "web"), exist_ok=True)
    deep = _deep(con)
    _publish(p["data"], bodies, {"items": memos, "meta": {
        "total": len(memos),
        "articles": len({m["id"] for m in memos}),
        "kinds": {k: sum(1 for m in memos if m["kind"] == k) for k in (m["kind"] for m in memos)},
        "stamp": meta["stamp"],
    }}, {"articles": arts, "meta": meta},
        {"days": deep, "meta": {"stamp": meta["stamp"], "days": len(deep)}})

    # 页面壳：模板注入配置后单文件原子替换（同样走 _replace：壳被服务进程
    # 读着时直接 os.replace 会抛 PermissionError，把整轮构建尾部崩掉）
    data_base = _data_base(p["online_cfg"])
    with open(p["tpl"], encoding="utf-8") as f:
        html_text = f.read()
    html_text = html_text.replace("/*__META__*/", json.dumps(meta, ensure_ascii=False))
    html_text = html_text.replace("/*__DATA_BASE__*/", json.dumps(data_base, ensure_ascii=False))
    tmp = p["out"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html_text)
    _replace(tmp, p["out"])

    if os.path.exists(p["memo_tpl"]):
        with open(p["memo_tpl"], encoding="utf-8") as f:
            mhtml = f.read()
        mhtml = mhtml.replace("/*__DATA_BASE__*/", json.dumps(data_base, ensure_ascii=False))
        tmp = p["memo_out"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(mhtml)
        _replace(tmp, p["memo_out"])

    if os.path.exists(p["deep_tpl"]):
        with open(p["deep_tpl"], encoding="utf-8") as f:
            dhtml = f.read()
        dhtml = dhtml.replace("/*__DATA_BASE__*/", json.dumps(data_base, ensure_ascii=False))
        tmp = p["deep_out"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(dhtml)
        _replace(tmp, p["deep_out"])

    if close:
        con.close()
    return p["out"]
