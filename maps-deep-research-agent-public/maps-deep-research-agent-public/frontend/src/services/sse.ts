/**
 * Minimal SSE client that supports POST bodies (EventSource only supports GET).
 *
 * Reads `text/event-stream` chunks via fetch + ReadableStream and dispatches
 * decoded JSON payloads to the provided handler.
 */

import type { ServerEvent } from "../types/events";

export interface StreamOptions {
  signal?: AbortSignal;
  onEvent: (event: ServerEvent) => void;
}

const BACKEND_BASE = import.meta.env.VITE_BACKEND_BASE || "/api";

export async function startResearchStream(
  payload: Record<string, unknown>,
  options: StreamOptions
): Promise<void> {
  const response = await fetch(`${BACKEND_BASE}/research/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: options.signal,
  });

  if (!response.ok || !response.body) {
    const text = await response.text().catch(() => "");
    throw new Error(`Stream request failed: ${response.status} ${text}`);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;

    let separator: number;
    // SSE messages are separated by an empty line ("\n\n").
    while ((separator = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);

      const dataLines: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trim());
        }
      }
      if (dataLines.length === 0) continue;

      try {
        const data = JSON.parse(dataLines.join("\n")) as ServerEvent;
        options.onEvent(data);
      } catch (err) {
        console.warn("Failed to parse SSE chunk", err, dataLines);
      }
    }
  }
}

export async function fetchUsage(): Promise<{
  llm_prompt_tokens: number;
  llm_completion_tokens: number;
  maps_api_calls: number;
  cache_hits: number;
}> {
  const response = await fetch(`${BACKEND_BASE}/usage`);
  if (!response.ok) throw new Error(`usage failed: ${response.status}`);
  return response.json();
}
