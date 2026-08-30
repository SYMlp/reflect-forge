const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

function fnv1a(value) {
  let hash = 0x811c9dc5;
  for (const char of String(value)) {
    hash ^= char.codePointAt(0);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

function mulberry32(seed) {
  let state = seed >>> 0;
  return () => {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function ranged(random, range) {
  return range[0] + (range[1] - range[0]) * random();
}

function anchorPoint(rect, anchor) {
  return {
    x: rect.x + rect.width * anchor.x,
    y: rect.y + rect.height * anchor.y,
  };
}

function rectFromAnchor(width, height, anchor, target) {
  return {
    x: target.x - width * anchor.x,
    y: target.y - height * anchor.y,
    width,
    height,
  };
}

function transformRect(rect, scale, originX, sourceTop, targetTop) {
  return {
    x: originX + (rect.x - originX) * scale,
    y: targetTop + (rect.y - sourceTop) * scale,
    width: rect.width * scale,
    height: rect.height * scale,
  };
}

export class SwordKitRenderer {
  static async create(canvas, manifestUrl = "./manifest.json") {
    const response = await fetch(manifestUrl);
    if (!response.ok) throw new Error(`剑器清单读取失败：${response.status}`);
    const manifest = await response.json();
    if (manifest.version !== 2) throw new Error(`装配器需要 v2 清单，当前为 v${manifest.version}`);
    return new SwordKitRenderer(canvas, manifest, new URL(".", new URL(manifestUrl, location.href)));
  }

  constructor(canvas, manifest, baseUrl) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.manifest = manifest;
    this.baseUrl = baseUrl;
    this.images = new Map();
    canvas.width = manifest.coordinateSpace.width;
    canvas.height = manifest.coordinateSpace.height;
  }

  async image(filename) {
    if (!this.images.has(filename)) {
      const promise = new Promise((resolve, reject) => {
        const image = new Image();
        image.decoding = "async";
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error(`零件图片读取失败：${filename}`));
        image.src = new URL(filename, this.baseUrl).href;
      });
      this.images.set(filename, promise);
    }
    return this.images.get(filename);
  }

  resolve(input = {}) {
    const manifest = this.manifest;
    const warnings = [];
    const type = manifest.blueprints[input.type] ? input.type : manifest.metadata.type[0];
    const stateId = manifest.states[input.state] ? input.state : manifest.metadata.state[0];
    if (input.type && input.type !== type) warnings.push(`未知剑型 ${input.type}，已回退到 ${type}`);
    if (input.state && input.state !== stateId) warnings.push(`未知状态 ${input.state}，已回退到 ${stateId}`);

    const blueprint = manifest.blueprints[type];
    const state = manifest.states[stateId];
    const seedValue = Number.isFinite(Number(input.seed))
      ? Number(input.seed) >>> 0
      : fnv1a(input.skill_id || `${type}:${stateId}`);
    const random = mulberry32(seedValue);
    const requested = input.parts || {};

    const choose = (group, requestedId, fallbackId, label) => {
      const pool = manifest.parts[group];
      if (requestedId && !pool[requestedId]) {
        warnings.push(`${label} ${requestedId} 不存在，已回退到 ${fallbackId}`);
      }
      return requestedId && pool[requestedId] ? requestedId : fallbackId;
    };

    const bladeId = choose("blades", requested.blade, blueprint.defaultParts.blade, "剑身");
    let guardId = choose("guards", requested.guard, blueprint.defaultParts.guard, "剑格");
    const blade = manifest.parts.blades[bladeId];
    let guard = manifest.parts.guards[guardId];
    if (!guard.compatibleBladeWeights.includes(blade.profileWeight)) {
      const fallback = blueprint.defaultParts.guard;
      warnings.push(`${guardId} 剑格不承 ${blade.profileWeight} 剑身，已回退到 ${fallback}`);
      guardId = fallback;
      guard = manifest.parts.guards[guardId];
    }

    const handleId = choose("handles", requested.handle, state.handle, "剑柄");
    const handle = manifest.parts.handles[handleId];
    if (!handle.compatibleBladeWeights.includes(blade.profileWeight)) {
      warnings.push(`${handleId} 剑柄与 ${blade.profileWeight} 剑身不兼容，已回退到 ${state.handle}`);
    }
    const finalHandleId = handle.compatibleBladeWeights.includes(blade.profileWeight) ? handleId : state.handle;
    const finishId = choose("finishes", requested.finish, state.finish, "钢材纹理");
    const tasselId = state.tassel
      ? choose("tassels", requested.tassel, state.tassel, "剑穗")
      : null;

    const proportions = {};
    for (const [name, range] of Object.entries(blueprint.ranges)) {
      const generated = ranged(random, range);
      const requestedValue = Number(input.proportions?.[name]);
      if (Number.isFinite(requestedValue)) {
        const adapted = clamp(requestedValue, range[0], range[1]);
        proportions[name] = adapted;
        if (adapted !== requestedValue) {
          warnings.push(`${name}=${requestedValue.toFixed(2)} 超出安全范围，已收束到 ${adapted.toFixed(2)}`);
        }
      } else {
        proportions[name] = generated;
      }
    }

    const identityRanges = manifest.identity.proceduralRanges;
    const identity = {
      metalWarmth: ranged(random, identityRanges.metalWarmth),
      patina: ranged(random, identityRanges.patina),
      scarCount: Math.floor(ranged(random, [identityRanges.scarCount[0], identityRanges.scarCount[1] + 1])),
      markCount: Math.floor(ranged(random, [identityRanges.markCount[0], identityRanges.markCount[1] + 1])),
    };

    return {
      type,
      state: stateId,
      skill_id: input.skill_id || "unnamed-sword",
      seed: seedValue,
      blueprint,
      stateSpec: state,
      parts: {
        blade: bladeId,
        guard: guardId,
        handle: finalHandleId,
        finish: finishId,
        tassel: tasselId,
      },
      proportions,
      identity,
      warnings,
      debugAnchors: Boolean(input.debugAnchors),
    };
  }

  async loadResolvedParts(resolved) {
    const parts = this.manifest.parts;
    const specs = {
      blade: parts.blades[resolved.parts.blade],
      guard: parts.guards[resolved.parts.guard],
      handle: parts.handles[resolved.parts.handle],
      finish: parts.finishes[resolved.parts.finish],
      tassel: resolved.parts.tassel ? parts.tassels[resolved.parts.tassel] : null,
    };
    const loaded = {};
    for (const [role, spec] of Object.entries(specs)) {
      loaded[role] = spec ? await this.image(spec.file) : null;
    }
    return {specs, loaded};
  }

  layout(resolved, specs, images) {
    const {nominal} = resolved.blueprint;
    const p = resolved.proportions;
    const centerX = this.manifest.coordinateSpace.centerX;

    const bladeHeight = nominal.bladeHeight * p.bladeLength;
    const bladeWidth = bladeHeight * (images.blade.naturalWidth / images.blade.naturalHeight) * p.bladeWidth;
    let blade = {
      x: centerX - bladeWidth / 2,
      y: nominal.tipY,
      width: bladeWidth,
      height: bladeHeight,
    };

    const bladeJoin = anchorPoint(blade, specs.blade.anchors.guardSocket);
    const guardWidth = bladeWidth * nominal.guardToBladeRatio * p.guardWidth;
    const guardHeight = nominal.guardHeight;
    let guard = rectFromAnchor(guardWidth, guardHeight, specs.guard.anchors.bladeSocket, bladeJoin);

    const handleJoin = anchorPoint(guard, specs.guard.anchors.handleSocket);
    const handleHeight = nominal.handleHeight * p.handleLength;
    const handleWidth = handleHeight * (images.handle.naturalWidth / images.handle.naturalHeight);
    let handle = rectFromAnchor(handleWidth, handleHeight, specs.handle.anchors.guardSocket, handleJoin);

    let tassel = null;
    if (specs.tassel && images.tassel) {
      const tasselJoin = anchorPoint(handle, specs.handle.anchors.tasselSocket);
      const tasselHeight = nominal.tasselHeight;
      const tasselWidth = tasselHeight * (images.tassel.naturalWidth / images.tassel.naturalHeight);
      tassel = rectFromAnchor(tasselWidth, tasselHeight, specs.tassel.anchors.gripSocket, tasselJoin);
    }

    const all = [blade, guard, handle, tassel].filter(Boolean);
    const sourceTop = Math.min(...all.map((rect) => rect.y));
    const sourceBottom = Math.max(...all.map((rect) => rect.y + rect.height));
    const safeTop = 24;
    const safeBottom = this.manifest.assembly.safeCanvasBottom;
    const fitScale = Math.min(1, (safeBottom - safeTop) / (sourceBottom - sourceTop));

    if (fitScale < 1 || sourceTop < safeTop || sourceBottom > safeBottom) {
      blade = transformRect(blade, fitScale, centerX, sourceTop, safeTop);
      guard = transformRect(guard, fitScale, centerX, sourceTop, safeTop);
      handle = transformRect(handle, fitScale, centerX, sourceTop, safeTop);
      if (tassel) tassel = transformRect(tassel, fitScale, centerX, sourceTop, safeTop);
    }

    const connections = [
      {
        name: "剑身 → 剑格",
        from: anchorPoint(blade, specs.blade.anchors.guardSocket),
        to: anchorPoint(guard, specs.guard.anchors.bladeSocket),
      },
      {
        name: "剑格 → 剑柄",
        from: anchorPoint(guard, specs.guard.anchors.handleSocket),
        to: anchorPoint(handle, specs.handle.anchors.guardSocket),
      },
    ];
    if (tassel) {
      connections.push({
        name: "剑柄 → 剑穗",
        from: anchorPoint(handle, specs.handle.anchors.tasselSocket),
        to: anchorPoint(tassel, specs.tassel.anchors.gripSocket),
      });
    }

    const shoulder = anchorPoint(blade, specs.blade.anchors.guardSocket);
    const tangEnd = anchorPoint(blade, specs.blade.anchors.tangEnd);
    const centerlineError = Math.max(
      Math.abs(blade.x + blade.width / 2 - centerX),
      Math.abs(guard.x + guard.width / 2 - centerX),
      Math.abs(handle.x + handle.width / 2 - centerX),
      tassel ? Math.abs(tassel.x + tassel.width / 2 - centerX) : 0,
    );
    const structural = {
      shoulderCoverPx: Math.min(shoulder.y - guard.y, guard.y + guard.height - shoulder.y),
      exposedTangAboveGuardPx: Math.max(0, guard.y - shoulder.y),
      guardToBladeRatio: guard.width / blade.width,
      tangSideCoverPx: (handle.width - blade.width * specs.blade.anchors.tangEnd.width) / 2,
      tangVerticalCoverPx: Math.min(tangEnd.y - handle.y, handle.y + handle.height - tangEnd.y),
      centerlineError,
      layerOrder: this.manifest.assembly.drawOrder.join(" → "),
    };

    return {blade, guard, handle, tassel, fitScale, connections, structural};
  }

  bladeSurface(image, finish, rect, resolved) {
    const width = Math.max(1, Math.round(rect.width));
    const height = Math.max(1, Math.round(rect.height));
    const surface = document.createElement("canvas");
    surface.width = width;
    surface.height = height;
    const ctx = surface.getContext("2d");

    const finishRatio = finish.naturalWidth / finish.naturalHeight;
    const targetRatio = width / height;
    let sourceWidth = finish.naturalWidth;
    let sourceHeight = finish.naturalHeight;
    let sourceX = 0;
    let sourceY = 0;
    if (finishRatio > targetRatio) {
      sourceWidth = finish.naturalHeight * targetRatio;
      sourceX = (finish.naturalWidth - sourceWidth) / 2;
    } else {
      sourceHeight = finish.naturalWidth / targetRatio;
      sourceY = (finish.naturalHeight - sourceHeight) / 2;
    }
    ctx.drawImage(finish, sourceX, sourceY, sourceWidth, sourceHeight, 0, 0, width, height);
    ctx.globalCompositeOperation = "destination-in";
    ctx.drawImage(image, 0, 0, width, height);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = resolved.stateSpec.detailOpacity;
    ctx.drawImage(image, 0, 0, width, height);
    ctx.globalAlpha = 1;

    ctx.globalCompositeOperation = "source-atop";
    const warmth = resolved.identity.metalWarmth;
    ctx.fillStyle = warmth >= 0
      ? `rgba(185,91,31,${Math.abs(warmth) * 0.42})`
      : `rgba(74,118,150,${Math.abs(warmth) * 0.38})`;
    ctx.fillRect(0, 0, width, height);

    const markRandom = mulberry32(resolved.seed ^ 0xa5a5f00d);
    const patinaCount = Math.round(18 + resolved.identity.patina * 52);
    for (let index = 0; index < patinaCount; index += 1) {
      const radius = 0.7 + markRandom() * Math.max(1, width * 0.045);
      ctx.fillStyle = `rgba(18,14,10,${0.025 + resolved.identity.patina * 0.1})`;
      ctx.beginPath();
      ctx.arc(markRandom() * width, (0.16 + markRandom() * 0.72) * height, radius, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.lineCap = "round";
    for (let index = 0; index < resolved.identity.scarCount; index += 1) {
      const x = width * (0.3 + markRandom() * 0.4);
      const y = height * (0.24 + markRandom() * 0.58);
      ctx.strokeStyle = "rgba(20,14,10,.48)";
      ctx.lineWidth = Math.max(0.8, width * 0.018);
      ctx.beginPath();
      ctx.moveTo(x - width * 0.18, y - height * 0.008);
      ctx.lineTo(x + width * 0.18, y + height * 0.009);
      ctx.stroke();
    }

    ctx.strokeStyle = "rgba(215,174,96,.28)";
    ctx.lineWidth = Math.max(0.7, width * 0.012);
    for (let index = 0; index < resolved.identity.markCount; index += 1) {
      const y = height * (0.34 + index * 0.055);
      const half = width * (0.11 + markRandom() * 0.07);
      ctx.beginPath();
      ctx.moveTo(width / 2 - half, y);
      ctx.lineTo(width / 2, y + height * 0.008);
      ctx.lineTo(width / 2 + half, y);
      ctx.stroke();
    }
    ctx.globalCompositeOperation = "source-over";
    return surface;
  }

  drawImage(image, rect, shadow = true) {
    const ctx = this.ctx;
    ctx.save();
    if (shadow) {
      ctx.shadowColor = "rgba(0,0,0,.72)";
      ctx.shadowBlur = 12;
      ctx.shadowOffsetX = 4;
      ctx.shadowOffsetY = 7;
    }
    ctx.drawImage(image, rect.x, rect.y, rect.width, rect.height);
    ctx.restore();
  }

  drawBackground() {
    const {ctx, canvas} = this;
    const gradient = ctx.createRadialGradient(canvas.width * 0.5, canvas.height * 0.43, 30, canvas.width * 0.5, canvas.height * 0.45, canvas.height * 0.74);
    gradient.addColorStop(0, "#29251f");
    gradient.addColorStop(0.62, "#171512");
    gradient.addColorStop(1, "#0c0b0a");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "rgba(179,126,61,.1)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(canvas.width * 0.5, 22);
    ctx.lineTo(canvas.width * 0.5, canvas.height - 20);
    ctx.stroke();
  }

  drawAnchors(layout) {
    const ctx = this.ctx;
    ctx.save();
    ctx.font = '12px "Noto Serif SC", serif';
    for (const connection of layout.connections) {
      ctx.strokeStyle = "rgba(210,163,84,.7)";
      ctx.fillStyle = "#d6a552";
      ctx.beginPath();
      ctx.arc(connection.from.x, connection.from.y, 6, 0, Math.PI * 2);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(connection.to.x, connection.to.y, 2.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillText(connection.name, connection.to.x + 10, connection.to.y - 9);
    }
    ctx.restore();
  }

  async render(input = {}) {
    const resolved = this.resolve(input);
    const {specs, loaded} = await this.loadResolvedParts(resolved);
    const layout = this.layout(resolved, specs, loaded);
    const surface = this.bladeSurface(loaded.blade, loaded.finish, layout.blade, resolved);

    const qa = this.manifest.assembly.structuralQa;
    const ratioRange = qa.guardToBladeRatioByType[resolved.type];
    if (layout.structural.shoulderCoverPx < qa.minShoulderCoverPx) {
      resolved.warnings.push(`剑格压肩仅 ${layout.structural.shoulderCoverPx.toFixed(1)}px，低于结构下限`);
    }
    if (layout.structural.exposedTangAboveGuardPx > qa.maxExposedTangAboveGuardPx) {
      resolved.warnings.push(`剑肩与剑格之间露出 ${layout.structural.exposedTangAboveGuardPx.toFixed(1)}px 剑茎`);
    }
    if (layout.structural.guardToBladeRatio < ratioRange[0] || layout.structural.guardToBladeRatio > ratioRange[1]) {
      resolved.warnings.push(`剑格/刃宽比 ${layout.structural.guardToBladeRatio.toFixed(2)} 超出 ${resolved.type} 蓝图`);
    }
    if (layout.structural.tangSideCoverPx < qa.minTangSideCoverPx || layout.structural.tangVerticalCoverPx < 0) {
      resolved.warnings.push("剑柄没有完整包住剑茎，需回退零件或收束比例");
    }
    if (layout.structural.centerlineError > qa.maxCenterlineErrorPx) {
      resolved.warnings.push(`剑器中轴偏移 ${layout.structural.centerlineError.toFixed(3)}px`);
    }

    this.drawBackground();
    this.drawImage(surface, layout.blade);
    this.drawImage(loaded.handle, layout.handle);
    this.drawImage(loaded.guard, layout.guard);
    if (loaded.tassel && layout.tassel) this.drawImage(loaded.tassel, layout.tassel, false);
    if (resolved.debugAnchors) this.drawAnchors(layout);

    const connectionError = layout.connections.reduce((maximum, connection) => {
      const error = Math.hypot(connection.from.x - connection.to.x, connection.from.y - connection.to.y);
      return Math.max(maximum, error);
    }, 0);
    return {
      ...resolved,
      fitScale: layout.fitScale,
      connectionError,
      anchors: layout.connections,
      structural: layout.structural,
    };
  }
}

export {fnv1a};
