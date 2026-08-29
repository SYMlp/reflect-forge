# Codex 任务书 · 反思锻造台视觉线

你负责「反思锻造台 Reflect Forge」的全部视觉：美术素材 + 武侠皮肤前端。这是今天黑客松的参赛作品，晚上 6 点路演，你的产出决定第一眼印象。后端由另一个 agent 并行开发，你们通过 mock.json 契约协作，**不要写任何后端逻辑，不要改契约结构**。

## 产品一句话

打工人把日常工作交给 AI 干，会话记录就是完整的过程资料；本产品是一座武侠风"锻造台"——把 session 里的精华（铁）用 AI 反思提炼出来，锻成可复用的 skill（剑）。用户是锻造师，Claude Code/Codex 这些 agent 是执剑的侠客。

## 世界观术语（文案必须用这套词，不许自造）

锻造师（用户）/ 侠客（AI agent）/ 剑（skill）/ 铁（session 精华）/ 火（token）/ 反思炉（提炼功能）/ 寻料（挖历史 session）/ 淬火（改版，必带一行 why）/ 兵器谱（十八般兵器，只有剑实装其余灰态）/ 秘籍阁（锁定态，"江湖尚远"）/ 斩活（完成工作）/ 等级：见习→初级→中级→高级→超级锻造师→锻造之神。

## 视觉方向

古典武侠 × 铁匠铺。基调：**炭黑铁灰打底，炉火橙金做焦点，宣纸米白承载正文，朱砂红只做点睛**（等级徽章、淬火按钮）。质感：水墨山水远景 + 金属拉丝 + 炉火辉光。禁忌：不要赛博霓虹、不要圆角卡通、不要纯黑底白字的极客风——要的是"热火朝天的锻造铺"，不是黑客终端。

中文排版：标题可用书法感字体（霞鹜文楷/思源宋体，本地 @font-face 或系统字体栈兜底 `"LXGW WenKai","Noto Serif SC","STKaiti","KaiTi",serif`），正文必须落在衬线可读字体，行高 1.7+。**index.html 第一行必须 `<meta charset="utf-8">` + viewport**，否则中文乱码。

## 交付物

### A. 美术素材（生图，存 `static/` 下，PNG）
1. `bg-forge.png` 主背景：锻造铺内景，炉火在画面一侧，留出中央内容区暗部（做 20% 不透明度衬底用）
2. `sword-1.png`~`sword-4.png` 一套剑四把，档次递进（短匕→长剑→重剑→名剑），同一画风、透明底
3. `furnace.png` 反思炉：一座炉子，炉口有火
4. `scroll.png` 卷轴/秘籍，卷起状态，透明底
5. `badge-1.png`~`badge-6.png` 六枚等级徽章（见习铁牌→锻造之神金印），同构不同贵气
6. 可选：`spark.png` 火花粒子（CSS 动画素材）

素材注意：整套一个画风（水墨+微写实），生成时用统一的风格描述词；单张控制在 500KB 内。

### B. 前端 `index.html`（单文件自包含 + static/ 资产）

页面结构：
- **顶栏**：产品名「反思锻造台」+ 锻造师档案条（名号、等级徽章、四维经验条：反思场次/锻剑数/转正率/斩活数）
- **五个 tab**：反思炉（默认）｜兵器架｜寻料｜兵器谱｜秘籍阁
- **反思炉**：一个大输入区（粘贴 session 文本）+「入炉」按钮 → 出铁列表（每块铁：原话锚引文、类型标签[判断/经验/流程]、品级[上品/中品/下品]）→ 勾选铁 →「锻剑」按钮 → 出剑卡（剑名、v0.1、SKILL.md 预览折叠）。加锻造过程动效：按钮按下后炉火变旺 + 火花粒子 + "锻造中…"（后端要 30s 左右，动效要撑住这段等待）
- **兵器架**：剑卡网格（剑图+名+版本+状态徽标[草稿/已转正]），点开侧滑详情：SKILL.md 内容 + 淬火记录时间线（每条一行 why）+ 两个按钮「转正」「淬火」（淬火弹窗必填一行 why）
- **寻料**：矿脉列表（文件名/日期/大小/品级评注），顶部放一行战绩文案（从 mock 读）
- **兵器谱**：18 般兵器网格，只有"剑"是亮的（实装），其余灰态带"待锻"印章
- **秘籍阁**：锁定态页面，一句"江湖尚远，敬请期待"+ 卷轴素材，再放"交友/换秘籍/赠剑（需重铸）"三个灰按钮

### C. `static/mock.json`（你先造假数据渲染，结构如下，一个字段都不能改）

```json
{
  "profile": {"name": "石锻造", "title": "中级锻造师", "level": 3,
    "exp": {"reflect_sessions": 12, "swords_forged": 5, "temper_rate": 0.6, "works_slain": 23},
    "next_level_req": {"reflect_sessions": 30, "swords_forged": 8, "temper_rate": 0.5, "works_slain": 40}},
  "armory": [{"id": "sw1", "name": "日报剑", "kind": "剑", "status": "forged", "version": "v1.2",
    "why_log": [{"v": "v0.1", "why": "初锻", "at": "2026-08-28"},
                 {"v": "v1.0", "why": "转正：真跑三天日报无返工", "at": "2026-08-28"},
                 {"v": "v1.2", "why": "淬火：加了周汇总视角，因为周五要的是趋势不是流水", "at": "2026-08-29"}],
    "skill_path": "swords/sw1/SKILL.md"}],
  "irons": [{"id": "i1", "text": "报错先看堆栈最底层的 caused by，别从上往下猜", "anchor": "「你先看最底下那个 caused by」", "kind": "判断", "grade": "上品"}],
  "prospect": [{"file": "2026-07-15-session.jsonl", "date": "2026-07-15", "size_kb": 482, "assay": "富矿", "note": "含一次完整排障链路"}],
  "prospect_banner": "上月回捞历史会话 76 场，挖出 900+ 条可复用判断",
  "manifest": {"weapons": [{"name": "剑", "live": true}, {"name": "刀", "live": false}, {"name": "枪", "live": false}, {"name": "棍", "live": false}, {"name": "斧", "live": false}, {"name": "钺", "live": false}, {"name": "钩", "live": false}, {"name": "叉", "live": false}, {"name": "鞭", "live": false}, {"name": "锏", "live": false}, {"name": "锤", "live": false}, {"name": "抓", "live": false}, {"name": "镋", "live": false}, {"name": "棒", "live": false}, {"name": "槊", "live": false}, {"name": "戟", "live": false}, {"name": "弓", "live": false}, {"name": "盾", "live": false}], "scrolls_locked": true}
}
```

数据读取封装成一个 `api()` 函数：现在 fetch `static/mock.json` 按 key 取；联调时只改这个函数指到 `/api/*`。POST 类（reflect/forge/temper）先用假延时 setTimeout 模拟 3 秒返回 mock 数据，动效按真等待设计（后端真实耗时约 30s，loading 态要耐看：炉火动画+一句随机的锻造行话轮播）。

## 约束

- 纯 HTML/CSS/JS 单文件 + static/，无构建、无框架、无外链 CDN 图片（字体 CDN 可以）
- 响应式不用管手机，投影 16:9 大屏优先——**这页是给路演投屏看的，字号往大了走**，演示距离 5 米要能看清 tab 文字
- 所有交互状态齐全：空态（兵器架没剑时给"尚无兵刃，去反思炉锻一把"）、loading、错误
- 做完自己开浏览器过一遍五个 tab，确认无 console 报错、中文无乱码
