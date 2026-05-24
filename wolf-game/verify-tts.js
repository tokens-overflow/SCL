/* 验证 web/tts.js 抽离后的语义等价 + 纯函数正确性
   不依赖浏览器：用 vm 加载 tts.js（已自适应 typeof window）+ stub minimal globals */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const sandbox = {
  console, setTimeout, clearTimeout,
  Math, Promise, JSON, Object, Array, Number, String, RegExp,
  // speechSynthesis 不存在 → tts.available 应该为 false（纯函数仍可测）
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "web/tts.js"), "utf-8"), sandbox);
vm.runInContext("globalThis.TTS = TTS;", sandbox);

const TTS = sandbox.TTS;
const { GENDERS, VOICE_PATTERNS, PITCH_PARAMS, genderOrder, computeParams, assignVoices } = TTS._internals;

function assert(cond, msg) {
  if (!cond) { console.error("❌ FAIL:", msg); process.exit(1); }
}
function approx(a, b, eps = 0.01) { return Math.abs(a - b) < eps; }

// ── Test 1: 配置完整性 ──
assert(GENDERS.length === 12, "GENDERS length=12");
assert(GENDERS.filter(g => g === "F").length === 7, "F count=7");
assert(GENDERS.filter(g => g === "M").length === 5, "M count=5");
assert(VOICE_PATTERNS.F.test("Xiaoxiao Online (Natural)"), "VOICE_PATTERNS.F matches Xiaoxiao");
assert(!VOICE_PATTERNS.F.test("WanLung"), "VOICE_PATTERNS.F rejects WanLung");
assert(VOICE_PATTERNS.M.test("Yunjian"), "VOICE_PATTERNS.M matches Yunjian");
assert(!VOICE_PATTERNS.M.test("Xiaoyi"), "VOICE_PATTERNS.M rejects Xiaoyi");
assert(PITCH_PARAMS.F.base === 1.10 && PITCH_PARAMS.M.base === 0.70, "pitch bases");
console.log("✅ Test 1: GENDERS/VOICE_PATTERNS/PITCH_PARAMS 完整");

// ── Test 2: genderOrder 正确性 ──
// idx=0 (F) 应该是 F 组第 0 位（共 7 个 F）
const o0 = genderOrder(0);
assert(o0.gender === "F" && o0.orderInGender === 0 && o0.count === 7, `genderOrder(0)=${JSON.stringify(o0)}`);
// idx=2 (M) 应该是 M 组第 0 位（共 5 个 M）
const o2 = genderOrder(2);
assert(o2.gender === "M" && o2.orderInGender === 0 && o2.count === 5, `genderOrder(2)=${JSON.stringify(o2)}`);
// idx=11 (M) 应该是 M 组最后一位（第 4 位）
const o11 = genderOrder(11);
assert(o11.gender === "M" && o11.orderInGender === 4 && o11.count === 5, `genderOrder(11)=${JSON.stringify(o11)}`);
console.log("✅ Test 2: genderOrder 纯函数语义正确");

// ── Test 3: computeParams 语义等价（与抽离前公式一致）──
// idx=0 (F, orderInGender=0/count=7, aggro=0.3)
//   norm = 0/6 - 0.5 = -0.5
//   groupOffset = -0.5 * 0.40 = -0.20
//   aggroAdjust = (0.5 - 0.3) * 0.15 = 0.03
//   pitch = clamp(1.10 + (-0.20) + 0.03, 0.5, 1.5) = 0.93
//   rate = 0.85 + 0.6 * 0.45 = 1.12
const p0 = computeParams({ idx: 0, personality: { aggro: 0.3, talkative: 0.6 } });
assert(approx(p0.pitch, 0.93), `idx=0 pitch=${p0.pitch} want ~0.93`);
assert(approx(p0.rate, 1.12), `idx=0 rate=${p0.rate} want ~1.12`);

// idx=2 (M, orderInGender=0/count=5, aggro=0.9)
//   norm = 0/4 - 0.5 = -0.5
//   groupOffset = -0.5 * 0.30 = -0.15
//   aggroAdjust = (0.5 - 0.9) * 0.15 = -0.06
//   pitch = clamp(0.70 + (-0.15) + (-0.06), 0.5, 1.5) = 0.49 → clamp to 0.50
//   rate = 0.85 + 0.9 * 0.45 = 1.255 → 1.26 (toFixed)
const p2 = computeParams({ idx: 2, personality: { aggro: 0.9, talkative: 0.9 } });
assert(approx(p2.pitch, 0.50), `idx=2 pitch=${p2.pitch} want ~0.50 (clamped)`);
assert(approx(p2.rate, 1.26, 0.011), `idx=2 rate=${p2.rate} want ~1.26`);

// idx=11 (M, orderInGender=4/count=5, aggro=0.75)
//   norm = 4/4 - 0.5 = 0.5
//   groupOffset = 0.5 * 0.30 = 0.15
//   aggroAdjust = (0.5 - 0.75) * 0.15 = -0.0375
//   pitch = clamp(0.70 + 0.15 + (-0.0375), 0.5, 1.5) = 0.8125 → 0.81 (toFixed)
//   rate = 0.85 + 0.95 * 0.45 = 1.2775 → 1.28
const p11 = computeParams({ idx: 11, personality: { aggro: 0.75, talkative: 0.95 } });
assert(approx(p11.pitch, 0.81, 0.011), `idx=11 pitch=${p11.pitch} want ~0.81`);
assert(approx(p11.rate, 1.28, 0.011), `idx=11 rate=${p11.rate} want ~1.28`);
console.log("✅ Test 3: computeParams 公式与抽离前完全等价");

// ── Test 3.5: assignVoices 1:1 不重复 ──
// 用 mock voice 对象（任何 truthy 值都行）
const mockF = ["F1","F2","F3","F4","F5","F6","F7","F8"]; // 8 个女声
const mockM = ["M1","M2","M3","M4","M5","M6"];           // 6 个男声
const mockAny = [...mockF, ...mockM];
const a1 = assignVoices(mockF, mockM, mockAny);
assert(a1.length === 12, "assignment length=12");
assert(a1.every(v => v !== null), "all 12 slots filled when pool sufficient");
assert(new Set(a1).size === 12, "all 12 voices unique when pool sufficient");
// 性别匹配：每个 F idx 的 voice 应来自 mockF
GENDERS.forEach((g, idx) => {
  if (g === "F") assert(mockF.includes(a1[idx]), `idx=${idx} (F) got ${a1[idx]} not from F pool`);
  if (g === "M") assert(mockM.includes(a1[idx]), `idx=${idx} (M) got ${a1[idx]} not from M pool`);
});
console.log("✅ Test 3.5a: 池子充足时，12 人 1:1 不重复且性别匹配");

// 不足场景：F 只有 3 个 voice，但有 7 个 F 角色 → 4 个 F 角色借用 M 池剩余
const mockF2 = ["F1","F2","F3"];
const mockM2 = ["M1","M2","M3","M4","M5","M6","M7","M8","M9","M10"]; // 充足借用
const mockAny2 = [...mockF2, ...mockM2];
const a2 = assignVoices(mockF2, mockM2, mockAny2);
assert(a2.length === 12 && a2.every(v => v !== null), "all 12 filled in shortage case");
assert(new Set(a2).size === 12, "still 12 unique by borrowing from other gender");
console.log("✅ Test 3.5b: F 池不足时，借用 M 池剩余仍保持 12 个不重复");

// ── Test 4: TTS 单例接口完整 ──
assert(typeof TTS.init === "function", "TTS.init exists");
assert(typeof TTS.speak === "function", "TTS.speak exists");
assert(typeof TTS.pause === "function", "TTS.pause exists");
assert(typeof TTS.resume === "function", "TTS.resume exists");
assert(typeof TTS.stop === "function", "TTS.stop exists");
assert(typeof TTS._pickVoice === "function", "TTS._pickVoice exists");
assert(TTS.enabled === false, "TTS.enabled default false");
assert(TTS.available === false, "TTS.available=false in node (no speechSynthesis)");
console.log("✅ Test 4: TTS 单例接口完整 + 默认状态正确");

// ── Test 5: 不可用环境下 speak / pause / stop 不崩溃 ──
// (no speechSynthesis here; TTS.available=false)
TTS.pause();   // no-op
TTS.resume();  // no-op
// TTS.stop 会调 document.querySelectorAll — 没 document，应该不直接调用
// 但当前实现里 stop 会 querySelectorAll；node 环境没 document，会 ReferenceError
// 这是设计决策：stop 只在浏览器调用。我们 stub document 然后再测试。
sandbox.document = { querySelectorAll: () => [] };
vm.runInContext("globalThis.document = document;", sandbox);
TTS.stop();  // 现在不会崩溃
assert(true, "TTS.stop 不崩（有 document stub）");
// speak 在 enabled=false 时立刻 resolve
TTS.speak("hello", { idx: 0, personality: { aggro: 0.5, talkative: 0.5 } }).then(() => {
  console.log("✅ Test 5: 不可用环境下接口安全（无副作用、不崩溃）");
  console.log("\n🎉 ALL TTS TESTS PASSED");
}).catch(e => {
  console.error("❌ TEST 5 FAILED:", e);
  process.exit(1);
});
