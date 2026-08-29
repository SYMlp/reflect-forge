# 反思锻造台 · Reflect Forge — 总设计稿

> 黑客松：VibeFriends × 原点学堂「Vibe Coding for 准点下班」，2026-08-29 build day。
> 时间盒：11:00–17:00 六小时，17:00–18:00 彩排，晚上 3 分钟路演。
> 底子：`toys/component-forge/`（main.py 1013 行：扫件/调 claude CLI/草稿转正带 why，已实跑）。
> 本仓 = 独立公开参赛仓，代码从 component-forge 搬骨架重组，武侠世界观全新皮。

## 0. Why（锚架构用）

**打工人的经验不随下班蒸发，而是长成他自己带得走的资产。**

- 描述性状态，不是动词（冷式 why 判据：动词是 how）。
- 路演口号版照用「用反思释放打工人」，两个版本各干各的活。
- 从这个 why 推功能，全部有指向性：
  - 经验散在哪 → session 里（前提：活都让 AI 干、下指令带 why → 记录完整无失真）
  - 人为什么捞不出来 → 记不全 + 信息茧房 → **反思炉**（AI 对着完整记录反思，不对着记忆反思）
  - 捞出来的东西什么形态 → **剑 = SKILL.md**（行业标准形态，带得走）
  - 旧账里还有没有 → **寻料**（挖历史 session）
  - 怎么知道自己长进了 → **锻造师等级**（经验条按真实锻造数据算）

## 1. 世界观映射表（术语唯一真相源，前后端文案统一从这取）

| 世界观 | 现实 | 说明 |
|:--|:--|:--|
| 反思锻造台 Reflect Forge | 本产品 | |
| 锻造师 | 用户（每个 vibe coder） | 单机单锻造师，注册系统不做 |
| 侠客 | Claude Code / Codex 等 agent | **锻造师不挥剑，锻剑；挥剑的是侠客**（路演点睛句） |
| 兵器（剑） | skill（SKILL.md） | 一把剑 = 一个子 skill |
| 主武器类型 / 一套剑 | 一类领域的 skill 组 | 本期只做剑，一套 |
| 铁（材料） | session 里真正有价值的精华 | 判断 / 经验 / 流程改进 三类 |
| 火 | token | 锻造要烧火 = 提炼要花 token |
| 反思炉 | session 分析提炼功能 | 核心新功能 |
| 寻料 | 扫历史 session 找矿 | 真实数字背书：上月回捞 76 场、捞出 900+ 条判断 |
| 锻造秘籍 | 反思时学的外部资料/前辈经验 | 本期烙印（锁定态），v2 接导师 agent |
| 探险 / 斩活 | 一类类工作 / 完成的工作 | |
| 淬火 / 重铸 | skill 改版（必带一行 why） | component-forge 已有机制 |
| 兵器谱 | 十八般兵器全表 | **只有剑实装，其余 17 种是烙印**（灰色待锻，游戏惯例） |
| 等级 | 见习→初级→中级→高级→超级锻造师→锻造之神 | 六级，多维经验条 |
| 赠剑 / 换秘籍 / 交友 | 锻造师社交 | 纯烙印文案，本期不做 |

## 2. 范围切分（6 小时一套剑）

**做（demo 主线，按顺序就是演示脚本）**：
1. **反思炉**：粘贴 session 文本或选 jsonl 文件 → 调 claude CLI 提炼 → 出铁（三类各带原话锚+品级）
2. **锻剑**：选铁（或说一句场景）→ 锻成剑 v0.1（SKILL.md 草稿）→ 试用后转正 / 淬火改版带一行 why
3. **兵器架**：已锻的剑列表，每把带版本、why 链（淬火记录）、状态
4. **锻造师档案**：等级 + 四维经验条（反思场次 / 锻剑数 / 转正率 / 斩活数），数据从落盘记录真算
5. **寻料**：扫目录列 session 文件（日期/大小），AI 快评矿脉品级；时间不够降级为静态展示真实回捞战绩
6. **兵器谱 + 秘籍阁**：烙印页（十八般兵器灰态 + 秘籍锁定态 + 社交入口"江湖尚远"）

**不做（说都不要说服我）**：注册/多用户、真社交、十八般兵器实装、秘籍全文、复杂成就引擎、Docker/构建链。

**demo 剑组（已拍板 2026-08-29）**：「述职剑法」套装——日报剑 / 周报剑 / 述职剑 / 复盘剑。贴"准点下班"主题，评委全是打工人，且 component-forge 昨天实跑就是日报场景有真数据。

**定调（用户原话，2026-08-29）**：「好看的外表其实就是让人家愿意看看。咱们内核不变。」——皮肤的职责是获客第一眼，内核始终是那条回路：完整过程资料 → AI 反思提炼 → skill 长成个人件。皮不许反过来绑架核。

## 3. 技术架构

```
reflect-forge/
├── main.py            # 纯 stdlib HTTP 服务，端口 7712（7711 被 component-forge 占）
├── index.html         # 单页，武侠皮，Codex 主刀
├── static/            # 美术素材（Codex 生成）+ mock.json（联调契约共用）
├── forge_prompts/     # 调 claude CLI 用的提炼/锻造 prompt 模板
├── data/
│   ├── irons/         # 铁（提炼产物，json 落盘）
│   ├── swords/        # 剑（SKILL.md + meta.json：版本/why 链/状态）
│   └── profile.json   # 锻造师档案与经验值
└── docs/DESIGN.md     # 本文件
```

- 调 AI 只走 `claude` CLI subprocess（订阅登录态，红线：不碰 ANTHROPIC_API_KEY）。component-forge 的 `_claude_env()` 手法直接搬。
- 单机文件落盘，不上数据库。
- index.html 第一行必带 `<meta charset="utf-8">` + viewport。

## 4. API 契约（两线并行的合同，谁也别改，要改先喊）

```
GET  /api/profile   → {name, title, level, exp:{reflect_sessions, swords_forged, swords_promoted, works_slain}, temper_rate, next_level_req:{level, ...四维门槛, met, current}}
                      修正 2026-08-29：晋升四维全部用单调计数（swords_promoted=转正把数，对 §5 表第三维）；
                      temper_rate（转正率）降为档案卡"质量"展示读数，不参与晋升判定。
                      level=§5 等级名（"见习锻造师"），title=称号（"初见炉火"等，后端定）。
                      iron id 格式 <session_hash8>-i<n>，跨 session 唯一。
GET  /api/armory    → [{id, name, kind:"剑", status:"draft|forged", version, why_log:[{v, why, at}], skill_path}]
POST /api/reflect   → 入 {source:"paste"|"file", content|path} 出 {irons:[{id, text, anchor, kind:"判断|经验|流程", grade:"上品|中品|下品", cite?:{name, kind:"剑|秘籍"}}]}
                      cite 为可选字段：这块铁的判断引用了已有的剑/秘籍时标注（demo 预跑数据保证至少 1-2 块铁带 cite，体系实证闭环的露点）
POST /api/forge     → 入 {iron_ids:[..]} 或 {scene:"一句话场景"} 出 {sword:{id, name, version:"v0.1", skill_md}}
POST /api/temper    → 入 {sword_id, why, action:"promote"|"revise", patch?} 出 {version, why_log}
GET  /api/prospect  → [{file, date, size_kb, assay:"富矿|贫矿|未验", note}]
GET  /api/manifest  → {weapons:[{name:"剑", live:true}, {name:"刀", live:false}, ...共18],
                      featured_scroll:{name:"前辈剑谱·why 三问", desc:"一位带 AI 团队的前辈三小时深谈提炼", cited:"今晨为本产品定 why 时引用——动词不是 why，why 一定是描述性的"},
                      scrolls_locked_rest:true}
                      秘籍阁 = 一本真秘籍实卡（带"今晨被引用"印）+ 其余锁定态
```

- 联调前 Codex 前端只吃 `static/mock.json`（同结构假数据），T2 换真接口，改一个 fetch 前缀即可。

**追加 2026-08-29 · 佩剑出口（新接口，不改上面任何旧契约）**

```
POST /api/bestow    → 入 {sword_id} 出 {bestowed_to, name, version, triggers}
                      动作：data/swords/<id>/SKILL.md → ~/.claude/skills/<id>/SKILL.md（用户级目录，
                      claude 原生发现，不需注册不需重启）。bestowed_to = 落位绝对路径。
                      triggers 从 SKILL.md frontmatter 取（triggers 优先，退 description）。
                      仅 status=forged 可授，草稿剑 → 400 stage=not_forged
                      「草稿剑不出鞘，先斩一活转正再来」。
                      重复授剑 = 覆盖更新（淬火出新版本后重授是正路）。
                      副作用：meta.json 写入 bestowed / bestowed_to / bestowed_at / bestowed_version。
GET  /api/armory    → 追加两个字段（增量，旧字段一个没动）：bestowed:bool, bestowed_to:str
```

为什么这条出口是回路的最后一环：剑锻好了不装到侠客身上，它就只是仓库里一个 md 文件。
锻造师不挥剑——挥剑的是侠客，而侠客只认 `~/.claude/skills/`。

## 5. 等级规则（写死，够 demo 用）

| 等级 | 反思场次 | 锻剑数 | 转正剑 | 斩活数 |
|:--|--:|--:|--:|--:|
| 见习锻造师 | 0 | 0 | 0 | 0 |
| 初级锻造师 | 3 | 1 | 0 | 3 |
| 中级锻造师 | 10 | 3 | 1 | 10 |
| 高级锻造师 | 30 | 8 | 4 | 40 |
| 超级锻造师 | 80 | 20 | 12 | 150 |
| 锻造之神 | 200 | 50 | 30 | 500 |

四维全部达标才晋升（用户原案）。demo 时用真实数据初始化 profile（他的真实底账：26 工作区/1667 份会话/33 件 skill/回捞 76 场——按规则落在中级~高级之间，现场算给观众看）。

## 6. 分工

| 线 | 谁 | 干什么 | 提示词 |
|:--|:--|:--|:--|
| 视觉线 | **Codex** | 生成美术素材（背景/剑×4/炉/卷轴/徽章×6）+ 武侠皮 index.html 静态版（吃 mock.json） | `prompts/codex-visual.md` |
| 逻辑线 | **Claude Code 执行会话** | 搬 component-forge 骨架 + 反思炉/锻剑/淬火/档案/寻料 API + 数据落盘 | `prompts/claude-backend.md` |
| 主节点 | **本会话（Fable）** | 设计稿、契约仲裁、两线集成审查、路演稿武侠版、demo 预跑把关 | — |
| 人肉线 | **锻造师本人** | 拍板、现场跑 demo 素材（喂自己真 session 预跑）、（可选）seedance 视频 | — |

## 7. 时间表（11:00 起算）

| 段 | 时间 | 干什么 | 完成判据 |
|:--|:--|:--|:--|
| T0 定盘 | 11:00–11:40 | 拍板待定项 → 建 GitHub 仓 push 骨架 → 两份提示词发出，两线开工 | 两个 agent 都在干活 |
| T1 并行 | 11:40–13:40 | Codex：素材+静态皮；CC：后端 API 全通（curl 自测） | 皮能看、API 能 curl |
| T2 合体 | 13:40–15:00 | 皮接真 API，主线跑通：粘 session→出铁→锻剑→上架→档案涨经验 | demo 主线全流程无手动干预 |
| T3 增色 | 15:00–16:00 | 寻料页+兵器谱烙印+等级动效+README「5 分钟装上」；人肉线可去搞视频 | 外人照 README 能跑起来 |
| T4 收口 | 16:00–17:00 | 用真 session 预跑 demo 数据 + **录屏备份**（现场断网保险）+ 修 bug | 录屏在手 |
| 彩排 | 17:00–18:00 | 路演稿武侠版 + 试讲一遍 | 3 分钟内讲完 |

**风险与保险**：
- 现场 demo 不裸奔：T4 预跑好的数据就是演示数据，现场只点已验证的路径；录屏是最后保险。
- claude CLI 调用慢（提炼 30s+）：demo 时用预跑结果，现场真调一次当"活证"就够。
- 两线接口打架：mock.json 是合同，改契约必须过主节点。

## 7.5 总装对照验收（今日教训，2026-08-29 T2 合体后）

- **触发**：三段验收全绿 + 合体主线跑通，本以为回路已闭合；对照架构图逐步骤过一遍才发现有断点——**绿灯只证明"写了的都对"，不证明"该有的都在"**。
- **判据**：成品必须**逐步骤按回架构图对照**，才分得清"有意砍"与"真漏"。测试和验收清单只覆盖已被想到的东西，架构图覆盖的是全集。
- **本次真漏**：佩剑出口——剑锻成后回到侠客手里那一步没接（已补）。**本次有意砍**：场景入口与通用件库，v2 再开锋，图上留虚线。
- **排除**：没有把它降级为"补个测试用例"——测试只会长出下一个绿灯，长不出被漏掉的那条边；对照物必须是图。

## 8. 宣传视频判决（主节点推荐，可推翻）

**降级为 P2 彩蛋，不进关键路径。** 理由：评分主体是产品+路演；30 秒以上的古典小电影（分镜+生成+剪辑+字幕）至少吃 1.5–2h 且质量方差大；"上来把质量拉高"这个目的由 Codex 静态海报 + 开场页氛围图达成 80%。若 T3 你自己有空档，用 seedance 生 3–4 个镜头（炉火/锻打/剑成/出鞘）拼 20 秒纯氛围片当开场，**功能介绍不塞视频里**——功能靠 demo 讲。

## 9. 赛规合规（VibeHacks #05，违背撤奖）

- **首 commit 时间** ✓：本仓 root commit 2026-08-29 上午（要求 8/28 18:00 之后）；公开仓，无需加协作者
- **commit 记录须体现完整 Vibe Coding 过程**：两线任务书已内置 commit 纪律（小步频繁+过程叙事+AI 署名，不 squash 不改历史）。这条跟产品理念同构——commit 历史本身就是过程性资料，路演可以点一句
- **过度抄袭**：纯 stdlib 自写、零开源依赖；前身 component-forge 是同作者赛前原型，README 已诚实声明来源（藏着才是风险，声明是加分）
- **路演硬控时**：3 分钟**含上下台切换**，鼓起锣落——实际口播按 **2 分 35 秒 / ~650 字**备稿，T4 改稿按此压；顺序随机不可调

## 10. 路演武侠版要点（T4 后成稿，骨架先立）

昨天稿子的骨架全保留（受众→引子→三个坎→解决→数字→收尾），换三处：
1. 产品名改「反思锻造台」，点睛句加「你不挥剑，你锻剑——挥剑的是 AI 侠客」
2. 解决段按世界观讲：铁=session 精华、火=token、反思炉、淬火带一行 why
3. 寻料数字上台：上月回捞一个多月历史会话 76 场，挖出 900+ 条能复用的判断——「我不愿意重复干活，我希望每个操作都有意义」原话保留
