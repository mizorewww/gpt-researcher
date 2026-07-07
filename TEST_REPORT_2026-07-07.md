# GPT Researcher 本地测试汇报

## 测试目标

验证本仓库在本地使用 `uv venv`、Tavily 检索、DeepSeek LLM、OpenRouter embedding 时，能否完成一次中文美股市场研究任务。

测试问题原文：

```text
调查上周的美股市场,调查不同板块的表现(注意:不要把上周改成绝对日期,就是要测试researcher的能力)
```

## 本地配置

- 虚拟环境：`uv venv` 创建的 `.venv`，Python 3.11.15。
- 依赖安装：`uv pip install -r requirements.txt`。
- 本地密钥文件：`.env`，已被 `.gitignore` 忽略，不进入版本控制。
- 检索器：`tavily`。
- LLM：`deepseek:deepseek-v4-pro`，用于 fast/smart/strategic 三类调用。
- Embedding：`openrouter:qwen/qwen3-embedding-8b`。
- 报告语言：`LANGUAGE=chinese`。

## 执行命令

```bash
uv venv
uv pip install -r requirements.txt
.venv/bin/python cli.py "调查上周的美股市场,调查不同板块的表现(注意:不要把上周改成绝对日期,就是要测试researcher的能力)" --report_type research_report --tone objective --report_source web --no-pdf --no-docx
```

## 测试过程

第一次完整运行时，OpenRouter embedding 在上下文压缩阶段返回 `401 User not found`。排查后发现是 `.env` 中 OpenRouter key 有一个字符大小写写错。修正后用下面的 smoke test 验证 embedding 可用：

```bash
.venv/bin/python - <<'PY'
from dotenv import load_dotenv
load_dotenv('.env')
from gpt_researcher.memory.embeddings import Memory
emb = Memory('openrouter', 'qwen/qwen3-embedding-8b').get_embeddings()
print(len(emb.embed_query('hello world')))
PY
```

验证结果：返回 4096 维向量。

第二次完整运行成功完成研究、抓取、上下文压缩和报告生成。

## 产物

- 成功报告：`outputs/上周美股板块表现.md`
- 失败/半成功首轮报告：`outputs/上周美股板块轮动.md`

成功报告 frontmatter 摘要：

```yaml
title: "上周美股板块表现"
report_type: "research_report"
report_source: "web"
tone: "objective"
sources_count: 20
total_cost_usd: 0.229352
```

## 主要观察

1. 基础链路通过：Tavily 检索、网页抓取、OpenRouter embedding 压缩、DeepSeek 报告生成都能跑通。
2. Agent 自动选择为 Finance Agent，符合任务类型。
3. 研究过程中抓取了 20 个来源，部分网页因内容过短或反爬限制被跳过，但整体没有阻断流程。
4. `--no-pdf --no-docx` 下只验证 Markdown 生成，没有测试 PDF/DOCX 导出。
5. 生成报告中出现了相对时间被绝对化的问题。用户明确要求不要把“上周”改成绝对日期，但报告正文第一句写成了“在刚刚过去的一周（2026年6月30日至7月3日）”。同时，规划阶段也生成过包含 `2026` 的子查询。因此，这项 researcher 能力测试没有通过。

## 结论

当前配置下项目可以本地成功运行并生成研究报告，但相对时间约束遵循不稳定。若要把这类能力做稳，需要在 query planning 和 report generation 两层都加入更强的 prompt/guardrail，禁止把用户原始相对时间表达改写为绝对日期。
