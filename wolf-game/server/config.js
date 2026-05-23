"use strict";

const fs = require("fs");
const path = require("path");

const CONFIG_PATH = path.join(__dirname, "..", "config.json");

function isPlaceholder(value) {
  if (!value || typeof value !== "string") return true;
  if (value.trim() === "sk-" || value.trim() === "sk-ant-") return true;
  return /FILL|YOUR/i.test(value);
}

function isConfigured(providerConf) {
  return !!providerConf && !isPlaceholder(providerConf.apiKey);
}

function loadConfig() {
  if (!fs.existsSync(CONFIG_PATH)) {
    const exampleHint = `cp config.example.json config.json && # then edit config.json to fill in your real API key`;
    throw new Error(
      `config.json not found at ${CONFIG_PATH}\n` +
      `→ 首次使用请从模板复制并填入你的 API key:\n  ${exampleHint}`
    );
  }
  const raw = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf-8"));
  const providers = raw.providers || {};
  const available = Object.keys(providers).filter(name => isConfigured(providers[name]));
  const requestedDefault = raw.defaultProvider;
  const defaultProvider = available.includes(requestedDefault)
    ? requestedDefault
    : (available[0] || null);
  return {
    port: raw.port || 3001,
    maxTokens: raw.maxTokens || 200,
    defaultProvider,
    providers,
    available,
  };
}

function watchConfig(onReload) {
  let timer = null;
  fs.watchFile(CONFIG_PATH, { interval: 1000 }, (curr, prev) => {
    if (curr.mtimeMs === prev.mtimeMs) return;
    clearTimeout(timer);
    timer = setTimeout(() => {
      try {
        onReload(loadConfig());
      } catch (e) {
        console.error("[config] reload failed:", e.message);
      }
    }, 300);
  });
}

module.exports = { loadConfig, watchConfig, CONFIG_PATH };
