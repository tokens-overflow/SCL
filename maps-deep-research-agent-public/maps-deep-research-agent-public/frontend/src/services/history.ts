/**
 * Lightweight run history persisted in localStorage.
 * Stores a compact snapshot per completed run so users can revisit / restore.
 */

import type { ItineraryDay, MapOverview, TaskNode } from "../types/events";

const KEY = "cartograph.history.v1";
const MAX = 20;

export interface RunSnapshot {
  runId: string;
  topic: string;
  language: "zh" | "en";
  savedAt: number;
  status: "succeeded" | "failed";
  tasks: TaskNode[];
  reportMarkdown: string;
  itinerary: ItineraryDay[];
  mapOverview: MapOverview;
  usage: {
    llm_prompt_tokens: number;
    llm_completion_tokens: number;
    maps_api_calls: number;
    elapsed_seconds: number;
  } | null;
}

export function loadHistory(): RunSnapshot[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as RunSnapshot[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function saveSnapshot(snap: RunSnapshot): RunSnapshot[] {
  const list = loadHistory().filter((s) => s.runId !== snap.runId);
  list.unshift(snap);
  const trimmed = list.slice(0, MAX);
  try {
    localStorage.setItem(KEY, JSON.stringify(trimmed));
  } catch {
    /* quota — ignore */
  }
  return trimmed;
}

export function removeSnapshot(runId: string): RunSnapshot[] {
  const list = loadHistory().filter((s) => s.runId !== runId);
  localStorage.setItem(KEY, JSON.stringify(list));
  return list;
}

export function clearHistory(): void {
  localStorage.removeItem(KEY);
}
