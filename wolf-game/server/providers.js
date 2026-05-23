"use strict";

function toOpenAITools(tools) {
  return tools.map(t => ({
    type: "function",
    function: { name: t.name, description: t.description, parameters: t.input_schema },
  }));
}

async function callClaude(conf, { system, user, tools, maxTokens }) {
  const body = {
    model: conf.model,
    max_tokens: maxTokens,
    system,
    messages: [{ role: "user", content: user }],
  };
  if (tools?.length) {
    body.tools = tools;
    body.tool_choice = { type: "any" };
  }
  const resp = await fetch(`${conf.baseURL}/v1/messages`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": conf.apiKey,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`Claude API ${resp.status}: ${await resp.text()}`);
  const data = await resp.json();
  if (tools?.length) {
    const tu = (data.content || []).find(c => c.type === "tool_use");
    if (tu) return { toolName: tu.name, input: tu.input || null };
    // Fallback: 模型没走 tool_use，从 text 段提取 JSON
    const txt = (data.content || []).find(c => c.type === "text")?.text || "";
    const parsed = extractJson(txt);
    return { toolName: parsed ? tools[0].name : null, input: parsed };
  }
  const txt = (data.content || []).find(c => c.type === "text");
  return { text: txt?.text || "" };
}

// 从文本里抠 JSON：先找 ```json 块，再找首个 {...} 平衡块
function extractJson(text) {
  if (!text || typeof text !== "string") return null;
  const block = text.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
  const candidate = block ? block[1] : text;
  // 找首个 { 到与之配对的 } —— 简易栈匹配，跳过字符串里的 }
  const start = candidate.indexOf("{");
  if (start < 0) return null;
  let depth = 0, inStr = false, esc = false;
  for (let i = start; i < candidate.length; i++) {
    const ch = candidate[i];
    if (esc) { esc = false; continue; }
    if (ch === "\\") { esc = true; continue; }
    if (ch === '"') { inStr = !inStr; continue; }
    if (inStr) continue;
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) {
        try { return JSON.parse(candidate.slice(start, i + 1)); }
        catch { return null; }
      }
    }
  }
  return null;
}

async function callOpenAICompat(conf, { system, user, tools, maxTokens, extraBody }) {
  const body = {
    model: conf.model,
    messages: [
      { role: "system", content: system },
      { role: "user", content: user },
    ],
    max_tokens: maxTokens,
    temperature: 0.8,
    ...(extraBody || {}),
  };
  if (tools?.length) {
    body.tools = toOpenAITools(tools);
    body.tool_choice = "required";
  }
  const resp = await fetch(`${conf.baseURL}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${conf.apiKey}`,
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`OpenAI-compat ${resp.status}: ${await resp.text()}`);
  const data = await resp.json();
  const msg = data.choices?.[0]?.message;
  if (tools?.length) {
    const tc = msg?.tool_calls?.[0];
    if (tc) {
      let input = null;
      try { input = JSON.parse(tc.function.arguments); } catch {}
      return { toolName: tc.function.name, input };
    }
    // Fallback: 模型没调 tool 而是吐了 JSON 文本
    const parsed = extractJson(msg?.content || "");
    return { toolName: parsed ? tools[0].name : null, input: parsed };
  }
  return { text: msg?.content || "" };
}

// DeepSeek V4 reasoner 默认开 thinking，但 thinking 模式不支持 tool_calls，
// 显式 disable 避免 400 "does not support this tool_choice"
const REGISTRY = {
  claude:   { call: (conf, opts) => callClaude(conf, opts) },
  openai:   { call: (conf, opts) => callOpenAICompat(conf, opts) },
  deepseek: { call: (conf, opts) => callOpenAICompat(conf, { ...opts, extraBody: { thinking: { type: "disabled" } } }) },
};

async function dispatch(providerName, providerConf, opts) {
  const provider = REGISTRY[providerName];
  if (!provider) throw new Error(`unknown provider: ${providerName}`);
  return provider.call(providerConf, opts);
}

module.exports = { dispatch, REGISTRY };
