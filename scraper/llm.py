"""MiniMax-M3.1-Flash 命题研判。

分工说明
--------
抓取不交给大模型——爬虫更快、更稳、零成本。
大模型做它真正擅长的事：读懂一篇文章，判断它能被改编成什么题、从哪一段取材。
这是规则引擎做不到的：规则只能说「6 处对策句」，模型能说「第四段可命制产业转型对策题」。

成本控制：只对规则分靠前的候选调用，结果落库后不再重复请求。
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from .classify import TOPICS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = os.path.join(ROOT, "config", "secret.json")

API_URL = "https://api.minimaxi.com/anthropic/v1/messages"
MODEL = "MiniMax-M3.1-Flash"
MAX_TOKENS = 3000  # 最高配置：留足 thinking + 结构化输出空间

_cfg = None


def config():
    """密钥优先级：环境变量 > config/secret.json。"""
    global _cfg
    if _cfg is not None:
        return _cfg
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    base = API_URL
    if not key and os.path.exists(SECRET):
        try:
            with open(SECRET, encoding="utf-8") as f:
                d = json.load(f)
            key = (d.get("api_key") or "").strip()
            base = d.get("api_url") or base
        except Exception as e:
            # 静默吞掉会把 AI 环节变成「0 成功且无解释」，务必说一声
            print(f"! config/secret.json 读取失败（{type(e).__name__}），AI 研判将被跳过",
                  file=sys.stderr)
    _cfg = {"key": key, "url": base, "model": MODEL}
    return _cfg


PROMPT = """你是资深公务员考试命题研究员，熟悉国考与省考申论的取材规律。分析下面这篇党媒文章。

标题：{title}
来源：{source} · {channel}
正文：
{body}

严格只输出一个 JSON 对象，不要解释文字，不要 markdown 代码块：
{{
  "topics": ["从这15个里选1-3个：基层治理/乡村振兴/科技创新/营商环境/生态文明/文化自信/民生保障/数字经济/法治建设/干部作风/舆情应对/对外开放/人口人才/安全底线/消费内需"],
  "tracks": ["从这4个里选1-2个，最多3个，只选真正适用的：申论素材(有治理议题且留有对策空间)／言语语料(议论性段落，可出逻辑填空或片段阅读)／常识时政(仅限政策文件、会议、法规、领导人讲话类，评论文章一般不选)／面试热点(有社会争议或治理难题)"],
  "score": 0到100的整数，代表被真题选中的可能性。注意：纯粹的会议通稿、领导人活动报道、企业宣传稿应低于40分,
  "summary": "一句话说清核心观点，不超过40字",
  "angles": ["2到4条命题角度。每条必须具体到段落和题型，例如：第四段列举三地做法，可命制归纳概括题。不要写空话"],
  "why": "判断依据，不超过50字"
}}"""


def _strip_fence(s):
    s = (s or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse(text):
    s = _strip_fence(text)
    try:
        return json.loads(s)
    except Exception:
        pass
    # 容错：截取第一个完整 JSON 对象
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    return None
    return None


# urllib 会自动读取 Windows 注册表里的系统代理（Clash 等），把请求转进代理隧道，
# 而本项目用的是国内 API，直连正常，走代理反而被掐断 TLS
# （UNEXPECTED_EOF_WHILE_READING）。因此优先直连，传输层失败再退回默认通道。
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_default = urllib.request.build_opener()


class Blocked(RuntimeError):
    """内容被接口的输入过滤拦截（new_sensitive），重试无意义。

    典型是领导人活动通稿类内容——接口按敏感词规则直接拒收，
    换模型或重试都不会通过，只能标记为「无法研判」并回退规则分。
    """


def _post(cfg, payload, timeout):
    """POST 到 Anthropic 兼容接口，返回解析后的 JSON。"""
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "x-api-key": cfg["key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    err = None
    for opener in (_direct, _default):
        req = urllib.request.Request(cfg["url"], data=data, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RuntimeError(f"鉴权失败({e.code})，请检查 API Key")
            try:
                detail = e.read().decode("utf-8", "ignore")
            except Exception:
                detail = ""
            if "new_sensitive" in detail:
                raise Blocked("接口输入敏感词拦截（new_sensitive），该文无法研判")
            raise  # HTTP 层错误换通道也没用，交给外层退避重试
        except Exception as e:
            err = e  # 传输层失败（TLS/超时）：换通道再试一次
    raise err


def analyze(title, source, channel, body, timeout=150, retries=2):
    """返回 dict 或 None。失败不抛异常，交由调用方回退到规则分。"""
    cfg = config()
    if not cfg["key"]:
        return None

    prompt = PROMPT.format(title=title, source=source, channel=channel, body=(body or "")[:1600])
    payload = {
        "model": cfg["model"],
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }

    for attempt in range(retries + 1):
        try:
            d = _post(cfg, payload, timeout)
            text = "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text")
            obj = _parse(text)
            if obj and "score" in obj:
                return _normalize(obj, d.get("usage", {}), title)
        except RuntimeError:
            raise
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4 * (attempt + 1))
                continue
            # 5xx 等服务端错误：退避后再试，连发三次不等待基本三发全挂
            if attempt == retries:
                return None
            time.sleep(2 * (attempt + 1))
        except Exception:
            if attempt == retries:
                return None
            time.sleep(2 * (attempt + 1))
    return None


TOPIC_SET = set(TOPICS)  # 与 classify.TOPICS 同源，防止两份清单漂移
TRACK_SET = {"申论素材", "言语语料", "常识时政", "面试热点"}


# ---------------------------------------------------------------- 背诵提取
#
# 与命题研判分开跑：研判回答「这篇能不能命题」，背诵提取回答「哪几句值得背」。
# 两件事标准不同、消耗也不同，所以不混在一个 prompt 里——背诵提取输出短、可以全量跑，
# 且不触碰已有评分。

MEMO_KINDS = ("论点段", "案例段", "规范段", "金句段")

MEMO_MIN_CHARS = 80      # 低于这个长度就是单句，没有上下文价值
MEMO_MAX_CHARS = 250     # 再长背不动、也塞不进作文段落
MEMO_WINDOW = 2400       # 截取正文的字数：段落级材料需要比研判更长的窗口

# 背诵提取的 thinking 预算不可控（thinking 参数被接口忽略），
# 思考吃满 max_tokens 就会一个字符都吐不出来。这里给足额度，
# 并在重试时逐次加码——报错是「没说完」，加额度就能说完。
MEMO_MAX_TOKENS = 6000

MEMO_PROMPT = """你是资深公务员考试申论教练，正在帮考生挑选可以直接背下来、改写后写进申论作文的段落。

标题：{title}
来源：{source} · {channel}
正文：
{body}

严格只输出一个 JSON 对象，不要解释文字，不要 markdown 代码块：
{{
  "memorable": [
    {{"kind": "论点段|案例段|规范段|金句段",
      "text": "从正文里逐字摘抄的完整段落或 2-3 句连续片段（80 到 250 字，必须逐字连续照抄，不得改写、拼接或跨段）",
      "usage": "怎么用（不超过20字，如：可作分论点主体）"}}
  ]
}}

四类的挑选标准：
- 论点段：有观点、有展开的完整论证段落，可作分论点主体，最多 2 条
- 案例段：完整叙述的案例段落，主体、做法、成效齐全，可作论据，最多 2 条
- 规范段：政策理论表述集中的段落，采分关键词密集，最多 2 条
- 金句段：含点睛句、比喻或排比的开头段／结尾段，可作开头引子或结尾升华，最多 2 条

每段 80 到 250 字，总共 2 到 4 段，宁缺毋滥。凑不出合适段落就少给几条。
不要给单句——单句没有上下文，背下来写进作文接不上。

直接给结果，不要写分析过程。"""


def _unquote(s):
    """去掉成对包裹的引号——单独剥会吃掉前引号却留下后引号。"""
    s = s.strip()
    pairs = {"“": "”", "「": "」", "『": "』", '"': '"', "'": "'"}
    if len(s) >= 2 and s[0] in pairs and s[-1] == pairs[s[0]]:
        s = s[1:-1].strip()
    return s


def _norm_memo(obj):
    """清洗模型返回的背诵点，并校验段落确实逐字出自正文。"""
    items = obj.get("memorable")
    if not isinstance(items, list):
        items = []
    body_core = re.sub(r"[^\w一-鿿]", "", obj.get("_body") or "")
    out = []
    for it in items[:6]:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind", "")).strip()
        kind = kind if kind in MEMO_KINDS else "论点段"
        text = _unquote(str(it.get("text", "")))
        if not (MEMO_MIN_CHARS <= len(text) <= MEMO_MAX_CHARS):
            continue
        # 整段必须逐字出自正文——否则考生会背下一段原刊里没有的话。
        # 段落级比单句严格：整段都要对得上，一个字都不能差。
        core = re.sub(r"[^\w一-鿿]", "", text)
        if not core or (body_core and core not in body_core):
            continue
        out.append({"kind": kind, "text": text, "usage": str(it.get("usage", "")).strip()[:24]})
    return out


def extract_memo(title, source, channel, body, timeout=150, retries=2):
    """提取背诵点。成功返回 list（可能为空），失败返回 None（重试耗尽）。"""
    cfg = config()
    if not cfg["key"]:
        return None

    text = (body or "")[:MEMO_WINDOW]
    prompt = MEMO_PROMPT.format(title=title, source=source, channel=channel, body=text)
    payload = {
        "model": cfg["model"],
        "max_tokens": MEMO_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }

    for attempt in range(retries + 1):
        payload["max_tokens"] = MEMO_MAX_TOKENS + 2000 * attempt
        try:
            d = _post(cfg, payload, timeout)
            raw = "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text")
            obj = _parse(raw)
            if isinstance(obj, dict):
                obj["_body"] = text
                items = _norm_memo(obj)
                if items or attempt == retries:
                    return items
                # 一条都没挑出来：换个说法再问一次。模型对文章可背性的判断有随机性，
                # 同一篇换个问法往往就能挑出完整段落。
                payload["messages"][0]["content"] = prompt + (
                    "\n\n刚才一段都没挑出来。请再找一次：挑一个完整的论证段落或案例段落，"
                    "逐字照抄 80 到 250 字，不要只给一句话。")
                continue
        except RuntimeError:
            raise  # 鉴权失败 / 敏感词拦截，直接上抛
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4 * (attempt + 1))
                continue
            # 5xx 之类的服务端错误也要退避——连着撞三次不等待，很容易三发全挂
            time.sleep(2 * (attempt + 1))
            continue
        except Exception:
            if attempt == retries:
                return None
            time.sleep(2 * (attempt + 1))
    return None


def memo_batch(articles, workers=4, on_done=None):
    """并发提取背诵点。articles: [(id, title, source, channel, body)]"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {}
    total = len(articles)

    def task(item):
        aid, title, source, channel, body = item
        return aid, extract_memo(title, source, channel, body)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(task, a): a[0] for a in articles}
        for f in as_completed(futs):
            try:
                aid, res = f.result()
            except Exception as e:
                aid, res = futs[f], None
                if isinstance(e, Blocked):
                    res = {"blocked": True, "why": str(e)}
                elif "鉴权" in str(e):
                    raise
            done += 1
            if res is not None:
                results[aid] = res
            if on_done:
                on_done(done, total, aid, res)
    return results


def _normalize(obj, usage, title=""):
    topics = [t for t in obj.get("topics", []) if t in TOPIC_SET][:3] or ["综合"]
    tracks = [t for t in obj.get("tracks", []) if t in TRACK_SET][:3] or ["泛时政"]

    # 「常识时政」只在真正的政策/会议/法规文本上成立。
    # 评论文章被模型顺手打上这个标签，会让它失去筛选意义。
    if "常识时政" in tracks and "申论素材" in tracks:
        is_policy = re.search(r"《[^》]{2,30}》", title) or re.search(
            r"会见|致信|贺信|出席|讲话|主持召开|印发|出台|条例|办法|规定", title
        )
        if not is_policy:
            tracks.remove("常识时政")
    tracks = tracks or ["申论素材"]

    try:
        score = int(round(float(obj.get("score", 60))))
    except Exception:
        score = 60
    angles = [str(a).strip()[:80] for a in obj.get("angles", []) if str(a).strip()][:4]
    return {
        "topics": topics,
        "tracks": tracks,
        "score": max(0, min(100, score)),
        "summary": str(obj.get("summary", "")).strip()[:60],
        "angles": angles,
        "why": str(obj.get("why", "")).strip()[:80],
        "usage": usage,
    }


def batch(articles, workers=4, on_done=None):
    """并发研判。articles: [(id, title, source, channel, body)]"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {}
    total = len(articles)

    def task(item):
        aid, title, source, channel, body = item
        return aid, analyze(title, source, channel, body)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(task, a): a[0] for a in articles}
        for f in as_completed(futs):
            try:
                aid, res = f.result()
            except Exception as e:
                aid, res = futs[f], None
                if isinstance(e, Blocked):
                    res = {"blocked": True, "why": str(e)}
                elif "鉴权" in str(e):
                    raise
            done += 1
            if res:
                results[aid] = res
            if on_done:
                on_done(done, total, aid, res)
    return results
