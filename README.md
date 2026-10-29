# 考公素材库 skill

抓取党媒评论，按公务员考试的命题逻辑打分，给出可命题角度。
**按需抓取，不囤数据**——你说几天就抓几天，抓完即用，用完即弃。

```
$ kaogong show --days 3

# 考公素材 · 最近 3 天

共 42 篇，按日期与命题价值排序

## 1. "三张桌子"看作风（评论员观察）

人民日报 05版·评论｜2026-10-29｜1454 字｜命题价值 88
标签：申论素材、言语语料、面试热点、干部作风、民生保障、基层治理

**核心观点**：干部下基层要坐得住群众的"三张桌子"，作风实不实群众最有发言权。
**可命题角度**
- 正文第三至五段：概括"三张桌子"分别对应的工作方法，可命制归纳概括题。
- 正文第六至八段：分析形式主义调研的具体表现与危害，可命制分析论证题。

原文：http://paper.people.com.cn/rmrb/pc/content/202610/29/content_30188338.html
```

## 能做什么

- **五个源**：人民日报、光明网、半月谈、中国青年报、经济日报
- **15 个议题**：基层治理、乡村振兴、科技创新、营商环境、生态文明、文化自信、
  民生保障、数字经济、法治建设、干部作风、舆情应对、对外开放、人口人才、
  安全底线、消费内需
- **四类用途**：申论素材、言语语料、常识时政、面试热点
- **五维打分**：来源、文体、话题、语言、时效，满分 100
- **三种深加工**（可选，调模型）：AI 命题研判、背诵段提取、每日精读速记卡

## 快速开始

需要 Python 3.9+，纯标准库，**无需安装任何依赖**。

```bash
git clone https://github.com/Yym11345/kaogong-skill.git
cd kaogong-skill

python scripts/kaogong.py show --days 1
```

第一次跑会抓取（约 1—2 分钟），之后同一天再跑是秒回。

Windows 上也可以直接双击 `kaogong.bat`，不带参数就显示今天的素材。

## 命令

| 命令 | 作用 |
|---|---|
| `show --days N` | 看最近 N 天（默认 1） |
| `show --days 7 --topic 基层治理` | 按议题筛 |
| `show --days 3 --track 申论素材` | 按用途筛 |
| `show --days 5 --min-score 80` | 只要高价值的 |
| `search 乡村振兴 --days 7` | 关键词搜索 |
| `memo --days 2` | 可直接背的段落 |
| `deep --days 1` | 每日精读速记卡 |
| `fetch --days 3` | 显式抓取 |
| `stats` | 库里现在有什么 |
| `clean` | 清空缓存 |
| `doctor` | 环境自检 |

常用参数：

- `--days N` 最近几天，1—30
- `--topic` 议题 / `--track` 用途 / `--source` 媒体
- `--limit N` 返回条数，默认 20
- `--json` 输出结构化数据
- `--no-fetch` 只查本地不联网
- `--refresh` 忽略已抓记录强制重抓
- `--keep N` 本地只保留几天，默认等于 `--days`

## 装成 WorkBuddy skill

这个仓库本身就是 skill 包。复制到 skills 目录：

```bash
cp -r . ~/.workbuddy-ai/skills/kaogong
```

之后直接问「最近三天有什么适合练对策题的申论素材」，助手会自己跑脚本。
**天数由你说**，说几天查几天。

## AI 深加工（可选，会计费）

默认抓取只做规则打分，**免费、秒级**。以下三个环节才调模型：

```bash
python scripts/kaogong.py fetch --days 3 --ai --ai-top 20   # 命题研判
python scripts/kaogong.py fetch --days 2 --memo             # 背诵段提取
python scripts/kaogong.py deep --days 1                     # 精读速记卡
```

配置密钥（`config/secret.json`，已被 gitignore）：

```bash
cp config/secret.example.json config/secret.json
# 编辑填入 api_key
```

也支持环境变量 `MINIMAX_API_KEY`，优先级更高。

所有环节**幂等**：`ai_at` / `memorable` 非空即跳过，重跑不会重复计费。

## 数据放在哪

默认 `%LOCALAPPDATA%\kaogong-skill\`（Linux/macOS 为 `~/.cache/kaogong-skill/`），
可用环境变量 `KAOGONG_DATA` 改。

每次抓取后按 `--keep` 清理超期文章，所以库常年只有几天的量——
抓 2 天约 0.5 MB，不会越滚越大。想彻底清空用 `clean`。

`covered.json` 记录哪些日期已抓过，避免反复抓同一个空日期（周末版面少）。

## 项目结构

```
├── SKILL.md                 skill 说明：触发条件、命令、边界
├── scripts/kaogong.py       CLI 入口，8 个子命令
├── references/engine.md     引擎结构、议题、评分口径
├── kaogong.bat              Windows 双击入口
├── scraper/                 抓取引擎
│   ├── sources.py           五源列表解析 + 正文提取
│   ├── classify.py          五维规则打分
│   ├── store.py             SQLite 持久层
│   ├── llm.py               AI 研判与背诵提取
│   ├── deepread.py          每日精读
│   └── net.py / dom.py      网络与 HTML 解析
└── config/secret.example.json
```

## 测试

```bash
python -m unittest discover -s tests
```

纯标准库，无 pytest。

## 说明

- 正文来自第三方媒体，版权归原媒体所有，本工具仅作个人备考检索与学习之用。
- 抓取依赖各家媒体的版面结构，改版后可能需要更新选择器。
- 评分用于排序筛选，不代表真题命中率。
- 光明网、半月谈是频道页，只反映当前最新，回溯历史日期时可能没有对应内容。

## License

MIT
