# Claude Code 任务书 · 反思锻造台逻辑线

你负责「反思锻造台 Reflect Forge」的后端与数据层。今天黑客松 build day，晚上 6 点路演。视觉由 Codex 并行开发（吃 static/mock.json 假数据），你们的合同是 API 契约——**契约在 docs/DESIGN.md §4，一个字段都不许改，要改先停下来喊人**。

## 你有一个现成的底子

`D:\Project\personal\toys\component-forge\main.py`（1013 行，纯 stdlib，昨天已实跑验证）里这些直接搬：
- HTTP 服务骨架（stdlib http.server，无框架）
- `_claude_env()`：subprocess 调 `claude` CLI 的环境处理（**红线：移除 ANTHROPIC_API_KEY 等厂商 key，只走 CLI 订阅登录态；prompt 走 stdin**）
- 扫 skill 库、落盘 v0.1 草稿、转正/带 why 改版的机制
- config.json / 日志的处理方式

搬的时候按新世界观重命名（见 DESIGN.md §1 术语表），不要留 component-forge 的旧名。

## 新目录（本仓根）

```
main.py              # 端口 7712
forge_prompts/       # claude CLI 的 prompt 模板（提炼、锻剑、评矿）
data/irons/          # 铁：json 逐条落盘
data/swords/<id>/    # 剑：SKILL.md + meta.json（版本/why_log/状态）
data/profile.json    # 锻造师档案
```

## 实现顺序（严格按此序，每步 curl 自测通过再下一步）

### 1. `GET /api/profile` + 等级引擎
- profile.json 初始化字段见契约；等级规则表在 DESIGN.md §5，四维全达标才晋升
- `next_level_req` 返回下一级门槛，前端画经验条用
- 经验值不手填：reflect_sessions = data/irons/ 按 session 计数，swords_forged = data/swords/ 计数，temper_rate = 已转正/总数，works_slain 从 config.json 读初始值（用户真实底账）+ 每次 forge 完成 +1

### 2. `POST /api/reflect` 反思炉（核心，最先打通）
- 入参：`{source:"paste", content:"..."}` 或 `{source:"file", path:"..."}`（jsonl 转纯文本再喂）
- 调 claude CLI，prompt 模板落 `forge_prompts/reflect.md`，要点：
  - 角色：反思炉，从一段工作 session 记录里提炼"铁"
  - 三类：**判断**（当事人下的可复用判断/取舍）、**经验**（踩坑与解法）、**流程**（下次这类活怎么干更好）
  - 每块铁必须带 `anchor`（原文逐字引用 ≤30 字）——没有原话锚的不要
  - 品级：上品=换个项目还能用 / 中品=同类活能用 / 下品=仅本次有效
  - 输出严格 JSON 数组，字段同契约 irons
- 产物落 `data/irons/<session_hash>.json`，响应给前端
- 超时 120s，CLI 报错时返回结构化错误（前端有错误态）

### 3. `POST /api/forge` 锻剑
- 两种入参：勾选的 iron_ids，或一句话 scene（后者走 component-forge 已有的选件逻辑）
- prompt 模板 `forge_prompts/forge.md`：把选中的铁锻成一份真 SKILL.md（frontmatter：name/description/触发词；正文：判据、步骤、反例），剑名武侠风但描述里写清真实用途
- 落 `data/swords/<id>/SKILL.md` + meta.json（version: "v0.1", status: "draft", why_log: [{v:"v0.1", why:"初锻", at:今天}]）

### 4. `POST /api/temper` 淬火/转正
- promote：status→forged，why_log 追加（why 必填，缺 why 拒绝——这是产品红线，不是校验细节）
- revise：version 递增 0.1，patch 应用到 SKILL.md（调 claude CLI 按 why 改写），why_log 追加

### 5. `GET /api/armory`
- 扫 data/swords/ 拼装，含 why_log 全量（前端画时间线）

### 6. `GET /api/prospect` 寻料
- 入参可选 dir（默认 config.json 里配的 session 目录）
- 列 .jsonl 文件（名/日期/大小），assay 先全部"未验"
- 加一个 `POST /api/prospect/assay`：对单个文件调 claude CLI 快评（prompt 模板 `forge_prompts/assay.md`：只回富矿/贫矿+一句 note，限 10s 内容截断到前 200 行）——时间不够这个接口可以砍，列表必须有
- `prospect_banner` 从 config.json 读（真实战绩文案）

### 7. `GET /api/manifest`
- 静态返回十八般兵器表（只有剑 live:true）+ scrolls_locked:true，直接抄 mock.json 结构

## 验收（T1 结束时全过）

```bash
curl -s localhost:7712/api/profile | python -m json.tool
curl -s -X POST localhost:7712/api/reflect -d '{"source":"paste","content":"<一段真 session>"}' | python -m json.tool
curl -s -X POST localhost:7712/api/forge -d '{"iron_ids":["i1"]}' | python -m json.tool
curl -s -X POST localhost:7712/api/temper -d '{"sword_id":"sw1","action":"promote","why":"真跑过一次没返工"}'
curl -s localhost:7712/api/armory | python -m json.tool
curl -s localhost:7712/api/prospect | python -m json.tool
```

外加：response 全部 `Content-Type: application/json; charset=utf-8`，中文不转义成 \uXXXX（json.dumps ensure_ascii=False），前端直接渲染。

## Commit 纪律（赛规硬要求：commit 记录须体现完整 Vibe Coding 过程）

- 每打通一个接口就 commit 一次，不攒大包
- message 头：`feat(逻辑线): xxx`，正文一两行写为什么这么做/踩了什么坑——过程叙事就是参赛内容
- 每个 commit 带 `Co-Authored-By: Claude <noreply@anthropic.com>` 署名，真实体现人机协作
- 不 squash、不 rebase 改历史

## 约束

- 纯 Python stdlib，零 pip 依赖（保住 README 的"5 分钟装上"卖点）
- 不写测试文件（一次性参赛品），但每个接口 curl 自测
- 不做鉴权/多用户
- 静态文件服务：`/` 返回 index.html，`/static/*` 直出（Codex 的产物落进来就能用）
- 遇到契约不合理的地方：停下来说，不要自己改契约
