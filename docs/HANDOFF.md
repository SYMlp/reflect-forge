# HANDOFF · 反思锻造台 v2 交接

> 交接人：build day 主控会话（toys 槽位，2026-08-29 全天）。接手人：v2 主控会话（本仓槽位新开）。
> 读完本文件 + docs/DESIGN.md 即可开工。build day 全过程的 session 在 toys 槽位与本仓槽位（headless 执行场 25+ 份）。

## 现状一句话

VibeHacks #05 参赛完成（路演被锣截断、核心判断段未讲完，但表单/README/公网站三处齐全替讲）。
产品 v1+v2 重锻炉全部收口：33 commits，公网只读站 forge.lsrabbit.space 活着（7713 demo 实例），本机 normal 实例 7712。

## v2 backlog（build day 全天真实使用中攒出，按优先级分组）

**P0.5 · 公网站脱机化（v2 第一单，约 1 小时）**
0. 静态快照 + GitHub Pages：demo 模式下全站语义已是纯静态——导出脚本抓全部 GET 响应成 api/*.json 快照 + index.html + static/，api() 加静态模式开关（POST 拦截前端已有）；开 Pages、forge.lsrabbit.space DNS 从隧道 CNAME 改指 symlp.github.io（域名不变故 vibecafe 遥测 Origin 白名单继续有效、小红书链接不变）；最后删隧道 forge 路由——站与作者电脑彻底解耦。当前状态：站活在本机 7713 经隧道，电脑关机即死，评选期电脑保持常开。
   **2026-08-30 升急**：链接已发给真实收件人（效仪哥，锻造台 B 端首个真实场景候选）——站的活性挂在作者电脑上，对方哪天点开撞 1033 第一印象就没了。
   **同日定位拍板**：B 端「相辅相成/最后一公里」叙事判为**讲法**，why 不换轴（§0 不动）；讲法只进 README/对外介绍层。

   **2026-08-30 前半段已落（快照导出 + 静态模式 + Pages 就绪）**：
   - 做了什么：① `scripts/export_static.py`——自起 FORGE_DEMO=1 专用实例（随机高位端口，抓完即关，7713 只读实例作兜底数据源）抓全部 6 个 GET（契约 5 个 + 前端真在调的 prospect_banner）存 `site/api/*.json`，复制 index.html（注入 `window.STATIC_MODE=true`）与 static/，写 CNAME + .nojekyll，导出前验 profile.demo=true；② 源 index.html 的 api() 加**休眠**静态分支——置位时 GET 读相对路径 `api/<name>.json`、POST 拒绝（第一道闸仍是快照 demo 位走 demoBlocked），未注入开关时行为不变；③ Pages 开通：分支发布实测只认 `/` 或 `/docs`，选 **build_type=workflow** + `.github/workflows/pages.yml`（零构建纯上传 site/，不挪 docs/），自定义域已在 GitHub 侧登记 forge.lsrabbit.space。
   - 验证读数：workflow run 33263855407 success；`https://symlp.github.io/reflect-forge/` 首页与 6 份 api json、static 资源全部 200，页面含注入开关；本机回归 7712 profile 200（demo=false）、7713 首页 200（无注入开关，休眠分支不触发）；隧道 forge.lsrabbit.space 仍 200（未动）。设 cname 后 github.io 尚未出 301（DNS 未切、域名健康检查未过），内容验证以 github.io 直读 200 为准。
   - 遗留点（等用户点头后半段执行）：DNS 把 forge.lsrabbit.space 的 CNAME 从隧道改指 symlp.github.io → 等 GitHub 域名验证过、https_enforced 打开 → 删隧道 forge 路由。切换前公网站继续由隧道承接，零空窗。

**P0 · 可验证性与人工线地基**
1. **session 标题化 / 人话密度初筛**——料无脸则人工点射线的判断权是空的（"真正病因不是没料，是认不出"）；人话密度=含思考量的天然信号，sonnet 打标第一特征
2. 铁的品级理由（why_grade：为什么判上品）+ 锚上下文窗（前后文）+ 点锚跳回原文——"没有为啥就是纯粹 AI 弄的"
3. 料完整性审计：三级截断（窗口/jsonl/炉子掐头留尾）**可见可选，不许静默**——截过什么在料卡上标出来

**P1 · 投料与流程**
4. 投料口"选卷宗"= 矿场投影面板（弹窗内选，不用 OS 文件选择器）
5. 加工状态标：每份卷宗带 未验/已验矿/已入炉/已成剑
6. 投炉一键开烧；「投炉」按钮改名「取此卷」（命名骗人实证）
7. 验矿双线闸：人工点射免验（人判即验），自动批量必验（贵火只烧富矿）
8. 夜班管线对接：data/assays.json 与 data/irons/ 格式即协议，作者已有的每晚两级打标管线做半小时格式适配即接入

**P2 · 世界观与结构**
9. 侠客名录页：Claude Code/Codex 各一卡，显示佩了什么剑；授剑选派驻地（用户级=行走江湖 / 项目级 .claude/skills/=驻守一城）
10. 江湖地图=项目分区：剑标出身项目、跨项目带剑（团队复用的前一站）
11. 套/形态二层：meta 加 set（领域组，真分类）+ form（兵器形态，自由加图零成本）；十八般降级为皮肤图库
12. 厚剑支持：skill 目录多文件形态（SKILL.md + scripts/ + references/）
13. 重锻底料入口泛化（现在写死一份 drawio-skill；scroll_path 字段名应改 blade_path——命名债）
14. 锻造师画像：冷启动填三五条业务线 → 夜班归线 → 纠正中长大（画像也是锻出来的，不预建）

**P3 · 远景烙印**
15. 炉子即仪表盘（热区完全体：点炉火看 token、点铁砧看今日锻造）
16. 个人件→团队通用底座
17. 秘籍阁扩容：前辈经验做成可对话导师（参照 mentor-leng 模式：引用带出处、外推显式标注）
18. 升级时刻动画（作者现在 2/3 反思场次，差 1 场升初级——真数据可触发）

**基建挂账（非产品）**
19. lsrabbit 域名收口：个人服务全域 Access 白名单、清死 DNS 记录（pan/dl/demo 的 CNAME 还在）、xc 系列按台账遗愿迁 zero-trust 隧道；隧道台账 v31 注释待补

## 接手必守的规矩（build day 实证过的）

- **读写边界**：主控只做读操作+判断产物 Write（任务书/对照表/稿），一切 git 写操作与实现文件改动派 headless（`claude -p --model claude-opus-5` 在本仓目录，session 落本仓槽位）
- **契约仲裁**：改 DESIGN.md §4 契约必须单点拍板，执行段"契约说不通停下来喊人，不自行改"
- **commit 纪律**：小步频繁+过程叙事+Co-Authored-By 署名，不 squash 不改历史
- **总装对照验收**：段验收全绿≠回路闭合，成品逐步骤按回架构图（DESIGN §7.5 黑匣子）
- **模型分层**：验矿/批量便宜火，精炼/设计贵火；日常交互地板 opus5
- **世界观类型系统**：能锻的是剑（可执行），秘籍只读（被 cite 引用滋养锻造）；界面层全武侠，SKILL.md 只在详情层露真身
- **用户原话红线集**：捞是它捞判是我判 / 先手动后自动是信任的顺序 / 截断可见可选不许静默 / 好看的外表让人愿意看看、内核不变

## 运维现状

- 7712 = normal 实例（本机用）；7713 = FORGE_DEMO=1 只读公网实例（隧道 forge.lsrabbit.space 指它，重启命令见 README）
- 隧道 personal（5ff6c5dd）remotely-managed，改路由走 Cloudflare API（token 在环境变量），本地 config.yml 只是台账
- data/ 里是 build day 真实产物（已入库）；~/.claude/skills/ 已佩两把剑（daily-report-delta、ai-work-human-judge）

## 过程资料指针

- 设计正本：docs/DESIGN.md（含契约/等级表/总装验收黑匣子）
- 路演稿：docs/pitch-3min.md（终版在 toys 主控场对话里，含"总量没减少"开场版）
- 认知沉淀：toys/INGEST-QUEUE.md 2026-08-29 的 8 行（看门狗/防护阀/读写边界/迁槽点/总装验收/定位三判断/料完整性/路演复盘），待 kg 收割
- build day 编排史：toys 槽位 2026-08-29 主控 session（打标为"编排场"，编排视角提炼）
