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

1. 剑身的吞口锚点取自透明像素轮廓里“刃身骤然收窄为剑茎”的真实剑肩，不使用图片底边；剑格、剑柄、剑穗依次连接。
2. 每种蓝图限制可接受的长宽变化；越界值会收束并写入 `warnings`。
3. 剑格声明可承载的剑身重量；不兼容时回退到蓝图默认件，不强行拼接。
4. 全剑超出画布时等比收束，连接锚点随整体变换，误差仍保持为零。
5. 图层固定为 `bladeSurface → handle → guard → tassel`，剑格最后压住剑肩和剑柄上缘，剑柄同时包住后方剑茎。
6. 结构验收同时检查锚点误差、吞口压肩像素与相对深度、格刃比例、剑柄包茎余量和中轴偏移；薄剑格按自身高度判覆盖，不能用厚剑格的绝对像素硬套，更不能只凭“两个错误锚点恰好相等”。

打开 `lab.html` 可以交互验证正常装配、越界收束、故意错配和锚点读数。

## 精工零件库与预设

试验台的“零件品质”读取 `manifest.presets`，可以整套切换稳定版与四套精工剑：

- `judgment-premium`：裁决，冷钢中脊、薄翼铜格、深色皮柄。
- `judgment-frost-premium`：霜裁，霜银窄锋、云翼银格、靛蓝丝柄。
- `experience-mountain-premium`：玄岳，黑锻阔脊、山纹承力格、双手皮柄。
- `process-hundredfold-premium`：百炼，明亮锻纹、锤鳞工造格、绛红缠柄。

剑身、剑格、剑柄也可以分别覆盖，形成混搭。装配器先按 `profileWeight` 判断承重兼容；不兼容时优先回退同蓝图默认件，再寻找同系列兼容件。剑格宽度读取 `preferredBladeRatioByType`，跨剑型时按当前蓝图重新开合，原图纵横比保持不变。

精工档只做可选覆盖，不修改蓝图默认件。所有进入零件库的 PNG 都是真透明通道，单件小于 500KB；带假棋盘、辉光残影或家族纹样漂移的候选不入库。
