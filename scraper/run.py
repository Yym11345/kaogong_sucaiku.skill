"""抓取主流程：一条显式流水线，五种模式 + 公共尾部。

    模式（互斥）                做什么
    -------------------------  ------------------------------------------
    默认（抓取）                发现 → 抓正文 → 规则评估 → 入库 → 清理超期
    --reclassify               只重算规则评估（不动 AI 结果、不抓取）
    --refetch                  按库内 URL 重抓正文 → 重算规则评估；
                               正文有实质变化才清空背诵点（避免无谓重计费）
    --ai-only                  只补跑 AI 研判
    --memorize-only            只补跑背诵提取

    公共尾部（各模式共享）      [--ai] → [--memorize] → 建站

每个环节幂等：规则评估可反复重算，AI / 背诵以 ai_at / memorable 非空为准跳过。
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from . import classify, sources, store


def crawl_day(day, per_source_limit, workers=6, skip_live=False):
    picked = []
    for name, fn in sources.LISTERS.items():
        if skip_live and name in sources.LIVE_ONLY:
            continue
        try:
            arts = fn(day)
        except Exception as e:
            print(f"  ! {name} 列表失败: {type(e).__name__}: {e}")
            continue
        # 评论/理论类优先，其次按标题去重
        arts.sort(key=lambda a: (0 if any(k in a.channel for k in ("评论", "时评", "理论", "观察")) else 1))
        cap = 60 if name in sources.LIVE_ONLY else per_source_limit  # 频道页源量小，全抓
        picked.extend(arts[:cap] if cap else arts)
        print(f"  · {name}: {len(arts)} 篇候选")

    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(sources.fetch_body, a): a for a in picked}
        for f in as_completed(futs):
            a = futs[f]
            try:
                art = f.result()
            except Exception:
                art = a
            if art.body and len(art.body) >= 150:
                out.append(art)
    return out


def _rule_fields(cls):
    return (cls["score"], json.dumps(cls["topics"], ensure_ascii=False),
            json.dumps(cls["tracks"], ensure_ascii=False),
            cls["reason"], json.dumps(cls["signals"], ensure_ascii=False))


def reclassify(con):
    """规则调整后重算库内文章的规则评估。只写 rule_* 并刷新展示列，AI 结果原样保留。"""
    rows = con.execute("SELECT id,title,body,source,channel,date FROM articles").fetchall()
    for r in rows:
        cls = classify.classify(dict(r))
        con.execute(
            "UPDATE articles SET rule_score=?,rule_topics=?,rule_tracks=?,rule_reason=?,rule_signals=? WHERE id=?",
            _rule_fields(cls) + (r["id"],),
        )
    store.update_display(con, *[r["id"] for r in rows])
    con.commit()
    print(f"重新分类 {len(rows)} 篇（AI 研判结果已保留）")


def refetch(con, workers=6):
    """清洗规则变更后，按库内 URL 重抓正文并重算规则评估（不重新发现文章）。

    正文有实质变化才清空背诵点（memorable 置空才会重提取，否则不重复计费）。
    AI 研判结果保留：重判要计费，而议题/命题角度很少随清洗规则翻转——
    已知取舍，见 README「已知限制」。
    """
    rows = con.execute("SELECT id,source,channel,title,url,date,body FROM articles").fetchall()
    arts = [sources.Article(r["source"], r["channel"], r["title"], r["url"], r["date"]) for r in rows]
    print(f"重新抓取正文 {len(arts)} 篇…")

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(sources.fetch_body, a): (a, r) for a, r in zip(arts, rows)}
        for f in as_completed(futs):
            a, r = futs[f]
            try:
                a = f.result()
            except Exception:
                continue
            if not a.body or len(a.body) < 150:
                continue
            cls = classify.classify(a)
            sets = "title=?,body=?,rule_score=?,rule_topics=?,rule_tracks=?,rule_reason=?,rule_signals=?"
            vals = [a.title, a.body] + list(_rule_fields(cls))
            if (a.body or "") != (r["body"] or ""):
                sets += ",memorable=NULL"  # 正文变了，旧背诵点可能不再逐字对应
            con.execute(f"UPDATE articles SET {sets} WHERE id=?", vals + [r["id"]])
            done += 1
    store.update_display(con, *[r["id"] for r in rows])
    con.commit()
    print(f"已更新 {done}/{len(arts)} 篇")


def run_ai(con=None, limit=40, min_score=0, workers=6, max_age_days=None):
    """对命题价值靠前的候选做 LLM 研判，结果写入 ai_* 列并刷新展示列。"""
    from . import llm
    if con is None:
        con = store.connect()
    todo = store.pending_ai(con, limit=limit, min_score=min_score, max_age_days=max_age_days)
    if not todo:
        print("AI 研判：没有待处理文章")
        return 0
    print(f"AI 研判：{len(todo)} 篇（{llm.MODEL}，并发 {workers}）…")

    def on_done(done, total, aid, res):
        # 边完成边落库：中途中断也不丢已有结果
        if res:
            store.save_ai(con, aid, res)
        if done % 5 == 0 or done == total:
            print(f"  {'✓' if res else '×'} {done}/{total}")

    try:
        results = llm.batch(todo, workers=workers, on_done=on_done)
    except RuntimeError as e:
        print(f"  ! {e}")
        con.commit()
        return 0

    con.commit()
    blocked = sum(1 for r in results.values() if r.get("blocked"))
    ok = len(results) - blocked
    print(f"  → 成功 {ok}/{len(todo)} 篇"
          + (f"（{blocked} 篇被接口敏感词拦截，已标记跳过）" if blocked else ""))
    return ok


def run_memo(con=None, limit=200, min_score=80, workers=6, max_age_days=None):
    """提取背诵点。独立于命题研判：不改动两套评估。"""
    from . import llm
    if con is None:
        con = store.connect()
    todo = store.pending_memo(con, limit=limit, min_score=min_score, max_age_days=max_age_days)
    if not todo:
        print("背诵提取：没有待处理文章")
        return 0
    print(f"背诵提取：{len(todo)} 篇（{llm.MODEL}，并发 {workers}）…")

    def on_done(done, total, aid, res):
        # 边完成边落库：中途中断不丢已有结果
        if isinstance(res, dict) and res.get("blocked"):
            store.save_memo_blocked(con, aid, res.get("why", ""))
        elif res is not None:
            store.save_memo(con, aid, res)
        if done % 5 == 0 or done == total:
            print(f"  {'✓' if res is not None else '×'} {done}/{total}")

    try:
        results = llm.memo_batch(todo, workers=workers, on_done=on_done)
    except RuntimeError as e:
        print(f"  ! {e}")
        con.commit()
        return 0

    con.commit()
    blocked = sum(1 for r in results.values() if isinstance(r, dict) and r.get("blocked"))
    print(f"  → 成功 {len(results) - blocked}/{len(todo)} 篇"
          + (f"（{blocked} 篇被敏感词拦截，已记录原因）" if blocked else ""))
    return len(results) - blocked


def run_deep(con=None, day=None, n=5, force=False):
    """每日精读：挑 5 篇生成中心理解速记卡。当天已生成则跳过。"""
    from . import deepread
    if con is None:
        con = store.connect()
    return deepread.run(con, day=day, n=n, force=force)


def cloud_config():
    """读 config/cloud.json。返回 {"env": ..., "auto": bool, "deploy": bool}，没配就返回 None。"""
    import os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "config", "cloud.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not (d.get("env") or "").strip():
        return None
    return {"env": d["env"].strip(),
            "auto": d.get("auto_upload", True) is not False,
            "deploy": d.get("auto_deploy", True) is not False}


def build_site(keep_days=30, upload=False, deploy=None):
    """生成站点。

    云存储上传：--upload-cloud 强制上传；或在 config/cloud.json 里配了环境 ID 时
    自动上传（这样定时任务不用改命令，配一次就一直在推）。

    静态托管部署：网页版走的是静态托管，和云存储是两条独立通路——只传云存储
    的话小程序数据会更新，但公网网页仍停在旧版本。所以构建完一并部署。
    """
    from . import build
    path = build.build(keep_days=keep_days)
    print(f"网页已生成：{path}")
    cfg = cloud_config()
    if upload or (cfg and cfg["auto"]):
        upload_cloud(cfg["env"] if cfg else None)
    want_deploy = deploy if deploy is not None else (cfg and cfg["deploy"])
    if want_deploy:
        deploy_hosting(cfg["env"] if cfg else None)
    return path


def _cloud_cli():
    """找云开发 CLI（tcb / cloudbase）。找不到返回 None。"""
    import os
    import shutil
    for name in ("tcb", "cloudbase"):
        p = shutil.which(name)
        if p:
            return p
    for p in (r"D:\ai-projects\yilai\npm-global\tcb.cmd",
              r"D:\ai-projects\yilai\npm-global\cloudbase.cmd"):
        if os.path.exists(p):
            return p
    return None


def refresh_auth():
    """跑一条只读命令逼 CLI 用 refreshToken 换新的临时密钥。

    CloudBase 的临时密钥只有 2 小时，过期后靠 refreshToken 惰性续期。续期是
    联网请求，偶发失败时 CLI 不重试，于是整批上传全部报 "No valid identity"。
    上传前先探一次，能把这类瞬时失败挡在批量操作之前。
    """
    import subprocess
    cli = _cloud_cli()
    if not cli:
        return False
    try:
        r = subprocess.run([cli, "env", "list"], capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=90)
        return r.returncode == 0
    except Exception:
        return False


def deploy_hosting(env=None):
    """把 web/ 发到云开发静态托管（网页版的公网通路）。

    只传云存储不会更新网页——两者是独立的。失败只提示，不影响抓取主流程。
    """
    import os
    import subprocess
    cli = _cloud_cli()
    if not cli:
        print("  ! 找不到云开发 CLI，跳过静态托管部署")
        return False
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [cli, "hosting", "deploy", os.path.join(root, "web"), "/",
           "--entry", "index.html"]
    if env:
        cmd += ["-e", env]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="ignore", timeout=600)
        if r.returncode == 0:
            print("  静态托管已更新（网页版刷新即最新）")
            return True
        print("  ! 静态托管部署失败：")
        print("    " + ((r.stdout or "") + (r.stderr or "")).strip()[-400:])
    except Exception as e:
        print(f"  ! 静态托管部署出错：{e}")
    return False


def upload_cloud(env=None):
    """把 web/data 传到微信云存储。

    上传脚本路径用环境变量 KAOGONG_UPLOAD_SCRIPT 覆盖，默认指向小程序项目里的
    tools/upload_cloud.py。任何失败都只打印提示，不影响抓取主流程。
    """
    import os
    import subprocess
    script = os.environ.get("KAOGONG_UPLOAD_SCRIPT",
                            r"D:\ai-projects\考公素材库\tools\upload_cloud.py")
    if not os.path.exists(script):
        print(f"! 找不到云端上传脚本：{script}（跳过）")
        return
    cmd = [sys.executable, script, "--write-config"]
    if env:
        cmd += ["--env", env]
    # 上传脚本会打印 ✓ 等字符，默认 GBK 管道下直接 UnicodeEncodeError 崩掉
    child_env = {**os.environ, "PYTHONUTF8": "1"}
    # CREATE_NO_WINDOW：计划任务经 pythonw 调起时不继承控制台，
    # 不加这个旗标子进程会自己开一个黑框弹出来
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    def _once():
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=child_env, creationflags=flags)
        for line in ((r.stdout or "") + (r.stderr or "")).splitlines():
            print("  " + line)
        return r.returncode == 0

    try:
        ok = _once()
        if not ok:
            # 凭证续期偶发失败会让整批上传全挂，刷新一次再重试
            print("  · 上传未成功，刷新登录态后重试一次…")
            refresh_auth()
            ok = _once()
        if not ok:
            print("  ! 云端上传未成功（不影响本地站点）")
    except Exception as e:
        print(f"  ! 云端上传出错：{e}（不影响本地站点）")


def crawl(con, args):
    t0 = time.time()
    total_new = 0
    for i in range(args.days):
        day = date.today() - timedelta(days=i)
        print(f"[{day}] 抓取中…")
        arts = crawl_day(day, args.limit, skip_live=(i > 0))
        new = 0
        for a in arts:
            try:
                c = classify.classify(a)
                if store.save(con, a, c):
                    new += 1
            except Exception as e:
                print(f"  ! 入库失败 {a.title[:20]}: {e}")
        total_new += new
        print(f"  → 新增 {new} 篇（有效 {len(arts)} 篇）")

    s = store.stats(con)
    print(f"\n抓取完成：新增 {total_new} 篇 | 库内共 {s['total']} 篇 | 均分 {s['avg']} | 耗时 {time.time()-t0:.0f}s")

    gone = store.prune(con, args.keep_days)
    if gone:
        print(f"清理超期：{gone} 篇（保留 {args.keep_days} 天）")


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="回溯天数")
    ap.add_argument("--limit", type=int, default=20, help="每源每天上限")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--ai", action="store_true", help="启用 LLM 命题研判")
    ap.add_argument("--ai-top", type=int, default=40, help="每轮 AI 处理篇数上限")
    ap.add_argument("--memorize", action="store_true", help="启用背诵点提取")
    ap.add_argument("--memo-top", type=int, default=200, help="每轮背诵提取篇数上限")
    ap.add_argument("--memo-min-score", type=int, default=80, help="背诵提取的最低命题价值")
    ap.add_argument("--deep", action="store_true", help="生成每日精读（5 篇中心理解速记卡）")
    ap.add_argument("--deep-day", default=None, help="精读日期，默认今天（YYYY-MM-DD）")
    ap.add_argument("--deep-force", action="store_true", help="该天已生成也重做")
    ap.add_argument("--keep-days", type=int, default=30, help="数据保留天数，超期清理")
    ap.add_argument("--upload-cloud", action="store_true",
                    help="构建后把 web/data 传到微信云存储（小程序的数据通路之一）")
    ap.add_argument("--deploy", dest="deploy", action="store_true", default=None,
                    help="构建后强制部署 web/ 到云开发静态托管（网页版公网通路）")
    ap.add_argument("--no-deploy", dest="deploy", action="store_false",
                    help="本次不部署静态托管，只更新本地与云存储")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--reclassify", action="store_true", help="仅按新规则重算规则评估，AI 结果保留")
    mode.add_argument("--refetch", action="store_true", help="重抓库内正文并重算；正文变了才重提背诵点")
    mode.add_argument("--ai-only", action="store_true", help="只跑 AI 研判，不抓取")
    mode.add_argument("--memorize-only", action="store_true", help="只跑背诵提取")
    mode.add_argument("--deep-only", action="store_true", help="只跑每日精读，不抓取")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    con = store.connect()

    if args.refetch:
        refetch(con)
    elif args.reclassify:
        reclassify(con)
    elif not (args.ai_only or args.memorize_only or args.deep_only):
        crawl(con, args)

    # 临期文章不进 AI/背诵候选：计费一次只剩几天寿命不值当，至少留 7 天
    max_age = max(1, args.keep_days - 7)
    if args.ai or args.ai_only:
        run_ai(con, limit=args.ai_top, max_age_days=max_age)
    if args.memorize or args.memorize_only:
        run_memo(con, limit=args.memo_top, min_score=args.memo_min_score, max_age_days=max_age)
    # 精读要挑高分文章，必须排在 AI 研判之后——否则挑的是规则分，选出来的不是最好的
    if args.deep or args.deep_only:
        run_deep(con, day=args.deep_day, force=args.deep_force)

    if not args.no_build:
        build_site(args.keep_days, args.upload_cloud, deploy=args.deploy)
    con.close()


if __name__ == "__main__":
    main(sys.argv[1:])
