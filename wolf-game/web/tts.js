/* =============================================================
   Werewolf · TTS (Text-to-Speech) Module
   ============================================================= */
/* 独立的语音合成模块，从 game.js 抽离。

   依赖（最小化）：
   - Web Speech API（speechSynthesis、SpeechSynthesisUtterance）
   - DOM：document.getElementById/querySelectorAll（仅用于 seat 高亮 CSS）
   - agent 对象的：idx、personality.aggro、personality.talkative

   暴露：
   - 全局 const TTS = { enabled, available, init, speak, pause, resume, stop }
   - TTS._internals（性别表、voice 模式、纯函数）—— 供测试 / 外部工具按需访问

   策略：
   - 12 个 agent 按 GENDERS 表分两组 voice
   - 同性别组内按 idx 顺序轮转分配 voice
   - pitch = 性别基线 + 组内偏移（让同性别不撞声）+ aggro 微调
   - rate = 0.85 + talkative * 0.45
*/

const TTS = (function () {

  // ── 配置：可调，独立于运行时逻辑 ──

  // 12 agent 性别表（idx 顺序与 web/agents.js 的 NAMES 一致）
  //   F=阿狸 紫霞    花木兰    貂蝉 荆轲       虞姬 小乔
  //   M=        白起     李白         韩信 程咬金        典韦
  const GENDERS = ["F","F","M","F","M","F","F","M","M","F","F","M"];

  // 普通话 voice 名字白名单（Microsoft Online Natural / 旧 voice 兼容）
  // 明确排除粤语（WanLung / HiuGaai / HiuMaan）和方言（Yunxia 辽宁 / Xiaobei 东北 / Xiaoni 陕西）
  // 女声：标准普通话（zh-CN）+ 台湾国语（zh-TW）共 13 个
  //   Xiaoxiao / Xiaoyi / Xiaochen / Xiaohan / Xiaomeng / Xiaomo / Xiaoqiu / Xiaorui /
  //   Xiaoshuang / Xiaoxuan / Xiaoyan / Xiaoyou / Xiaozhen / HsiaoChen / HsiaoYu
  // 男声：标准普通话 + 台湾国语共 8 个
  //   Yunfeng / Yunhao / Yunjian / Yunxi / Yunyang / Yunye / Yunze / YunJhe
  const VOICE_PATTERNS = {
    F: /Xiaoxiao|Xiaoyi|Xiaochen|Xiaohan|Xiaomeng|Xiaomo|Xiaoqiu|Xiaorui|Xiaoshuang|Xiaoxuan|Xiaoyan|Xiaoyou|Xiaozhen|HsiaoChen|HsiaoYu|Huihui|Yaoyao|Female/i,
    M: /Yunfeng|Yunhao|Yunjian|Yunxi|Yunyang|Yunye|Yunze|YunJhe|Kangkang|Male/i,
  };

  // 性别相关的 pitch 基线 + 偏移范围（F 声更高、M 声更低；range 控制同性别组内差异）
  const PITCH_PARAMS = {
    F: { base: 1.10, range: 0.40 },
    M: { base: 0.70, range: 0.30 },
  };

  const SPEAK_TIMEOUT_MS = 15000;

  // ── 纯函数（可测试，无副作用）──

  // 计算 agent 在自己性别组内的位次（用于 voice 轮转和 pitch 偏移）
  function genderOrder(idx) {
    const gender = GENDERS[idx];
    const sameGender = GENDERS.reduce((acc, g, i) => {
      if (g === gender) acc.push(i);
      return acc;
    }, []);
    return { gender, orderInGender: sameGender.indexOf(idx), count: sameGender.length };
  }

  // 为 12 个 idx 各分配一个 voice：同性别 1:1 不重复优先；同性别池不足时从异性别剩余里借
  // 返回 length=GENDERS.length 的数组，每个槽位是一个 voice 或 null（池子为空时）
  function assignVoices(voicesF, voicesM, voicesAny) {
    const assignment = new Array(GENDERS.length).fill(null);
    const usedF = new Set();
    const usedM = new Set();
    // 第一轮：同性别池里按 orderInGender 顺序拿，每人一个，不重复
    for (let idx = 0; idx < GENDERS.length; idx++) {
      const gender = GENDERS[idx];
      const pool = gender === "F" ? voicesF : voicesM;
      const used = gender === "F" ? usedF : usedM;
      const v = pool.find(x => !used.has(x));
      if (v) { assignment[idx] = v; used.add(v); }
    }
    // 第二轮：同性别池用完的槽位，从全池里挑还没用过的（无论性别），仍保不重复
    const globalUsed = new Set([...usedF, ...usedM]);
    for (let idx = 0; idx < GENDERS.length; idx++) {
      if (assignment[idx]) continue;
      const v = voicesAny.find(x => !globalUsed.has(x));
      if (v) { assignment[idx] = v; globalUsed.add(v); }
    }
    // 第三轮：voice 数小于 12 的极端情况，退化到 mod 轮转（不再保证不重复，已通过 warn 提示）
    for (let idx = 0; idx < GENDERS.length; idx++) {
      if (assignment[idx]) continue;
      const gender = GENDERS[idx];
      const pool = (gender === "F" ? voicesF : voicesM);
      const fallbackPool = pool.length ? pool : voicesAny;
      if (fallbackPool.length === 0) continue;
      assignment[idx] = fallbackPool[idx % fallbackPool.length];
    }
    return assignment;
  }

  // 根据 agent 计算 SpeechSynthesisUtterance 的 pitch / rate
  function computeParams(agent) {
    const { gender, orderInGender, count } = genderOrder(agent.idx);
    const { base, range } = PITCH_PARAMS[gender];
    const norm = (count > 1) ? (orderInGender / (count - 1) - 0.5) : 0;
    const groupOffset = norm * range;
    const aggroAdjust = (0.5 - agent.personality.aggro) * 0.15;
    const pitch = Math.max(0.5, Math.min(1.5, base + groupOffset + aggroAdjust));
    const rate  = 0.85 + agent.personality.talkative * 0.45;
    return { pitch: +pitch.toFixed(2), rate: +rate.toFixed(2) };
  }

  // ── TTS 单例 ──

  const tts = {
    enabled: false,
    available: typeof speechSynthesis !== "undefined",
    voicesF: [],
    voicesM: [],
    voicesAny: [],
    // idx → voice 的固定分配表（12 槽位，1:1 不重复）
    voiceAssignment: new Array(GENDERS.length).fill(null),

    init() {
      if (!this.available) return;
      const refresh = () => {
        const all = speechSynthesis.getVoices();
        // 只保留普通话（zh-CN / zh-TW 国语），排除 zh-HK 粤语
        this.voicesAny = all.filter(v => /^zh-(CN|TW)/i.test(v.lang));
        if (this.voicesAny.length === 0) this.voicesAny = all.filter(v => /^zh/i.test(v.lang) && !/^zh-HK/i.test(v.lang));
        if (this.voicesAny.length === 0) this.voicesAny = all;
        this.voicesF = this.voicesAny.filter(v => VOICE_PATTERNS.F.test(v.name));
        this.voicesM = this.voicesAny.filter(v => VOICE_PATTERNS.M.test(v.name));
        // 未匹配 VOICE_PATTERNS 性别的中文 voice：按"哪边少塞哪边"的策略兜底，让 F/M 池均衡
        // 注：voicesAny 同时仍持有所有 voice，assignVoices 用 globalUsed 集合避免第二轮重复挑同一个
        this.voicesAny.forEach(v => {
          if (!this.voicesF.includes(v) && !this.voicesM.includes(v)) {
            if (this.voicesF.length <= this.voicesM.length) this.voicesF.push(v);
            else this.voicesM.push(v);
          }
        });
        this.voiceAssignment = assignVoices(this.voicesF, this.voicesM, this.voicesAny);
        const filled = this.voiceAssignment.filter(Boolean).length;
        const unique = new Set(this.voiceAssignment.filter(Boolean)).size;
        console.log(`[TTS] voices loaded: ${this.voicesAny.length} zh voices (F=${this.voicesF.length} M=${this.voicesM.length}), assigned ${filled}/12 unique=${unique}`);
        if (unique < filled) console.warn("[TTS] voice 池不足，存在重复分配。建议系统安装更多普通话 voice。");
      };
      refresh();
      speechSynthesis.addEventListener("voiceschanged", refresh);
    },

    // 内部：直接查固定分配表（12 人 1:1 不重复，同性别优先）
    _pickVoice(agent) {
      return this.voiceAssignment[agent.idx] || this.voicesAny[0] || null;
    },

    speak(text, agent) {
      if (!this.enabled || !this.available) return Promise.resolve();
      return new Promise(resolve => {
        try { speechSynthesis.cancel(); } catch {}
        const seat = document.getElementById(`seat-${agent.idx}`);
        let finished = false;
        const done = () => {
          if (finished) return;
          finished = true;
          seat?.classList.remove("tts-active");
          resolve();
        };
        const u = new SpeechSynthesisUtterance(text);
        u.lang = "zh-CN";
        const v = this._pickVoice(agent);
        if (v) u.voice = v;
        const { pitch, rate } = computeParams(agent);
        u.pitch  = pitch;
        u.rate   = rate;
        u.volume = 1;
        u.onend   = done;
        u.onerror = done;
        seat?.classList.add("tts-active");
        speechSynthesis.speak(u);
        setTimeout(done, SPEAK_TIMEOUT_MS);
      });
    },

    pause()  { if (this.available) speechSynthesis.pause();  },
    resume() { if (this.available) speechSynthesis.resume(); },
    stop() {
      if (this.available) { try { speechSynthesis.cancel(); } catch {} }
      document.querySelectorAll(".seat.tts-active").forEach(el => el.classList.remove("tts-active"));
    },
  };

  // 暴露纯函数 + 配置给测试 / 外部工具
  tts._internals = { GENDERS, VOICE_PATTERNS, PITCH_PARAMS, genderOrder, computeParams, assignVoices };

  return tts;
})();

// 兼容 verify 脚本 / Node vm 环境：把 TTS 挂到 globalThis（浏览器顶层 const 已经全局可见）
if (typeof window !== "undefined") window.TTS = TTS;
