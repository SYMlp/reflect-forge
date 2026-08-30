# 剑器组件协议 v2

这套素材不保存每一种完整剑图。数据库保存剑的元数据，浏览器通过 `manifest.json` 与 `renderer.js` 选择零件、对齐结构锚点并生成最终画面。

## 最小数据

```json
{
  "type": "judgment",
  "state": "tempered",
  "skill_id": "daily-report-delta",
  "seed": 7713
}
```

- `type` 选择剑器结构蓝图与默认剑身、剑格。
- `state` 选择钢材纹理、剑柄和可选剑穗。
- `skill_id` 在未显式提供 `seed` 时生成稳定身份种子。
- `seed` 决定安全范围内的比例、冷暖、锈蚀、旧痕与铭纹；同一种子始终复现同一把剑。

## 浏览器接入

```html
<canvas id="sword"></canvas>
<script type="module">
  import {SwordKitRenderer} from "/static/sword-kit/renderer.js";

  const renderer = await SwordKitRenderer.create(
    document.querySelector("#sword"),
    "/static/sword-kit/manifest.json"
  );

  const result = await renderer.render({
    type: "judgment",
    state: "tempered",
    skill_id: "daily-report-delta",
    seed: 7713
  });
</script>
```

`render()` 返回最终零件、参数、回退判词、整体收束比例与锚点误差，可直接显示在兵器详情里。

## 自动适配边界

1. 剑身、剑格、剑柄、剑穗以语义锚点连接，不依赖固定矩形槽位。
2. 每种蓝图限制可接受的长宽变化；越界值会收束并写入 `warnings`。
3. 剑格声明可承载的剑身重量；不兼容时回退到蓝图默认件，不强行拼接。
4. 全剑超出画布时等比收束，连接锚点随整体变换，误差仍保持为零。
5. 剑格后绘制，作为吞口遮住剑身与剑柄的接缝。

打开 `lab.html` 可以交互验证正常装配、越界收束、故意错配和锚点读数。
