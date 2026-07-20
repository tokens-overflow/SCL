// Mirrors the typed SSE events emitted by the backend (backend/src/models.py).

export type TaskStatus =
  | "pending"
  | "in_progress"
  | "completed"
  | "skipped"
  | "failed";

export interface Place {
  place_id: string;
  name: string;
  address: string;
  lat: number;
  lng: number;
  rating: number | null;
  user_ratings_total: number | null;
  price_level: number | null;
  categories: string[];
  opening_hours: string[];
  website: string | null;
  phone: string | null;
  photo_reference: string | null;
  google_maps_url: string | null;
  editorial_summary?: string | null;
  reviews?: string[];
}

export interface RouteLeg {
  origin: string;
  destination: string;
  mode: string;
  distance_meters: number;
  duration_seconds: number;
  polyline: string | null;
}

export interface TaskEvidence {
  places: Place[];
  routes: RouteLeg[];
  raw_calls: number;
  notes: string[];
}

export interface TaskNode {
  id: number;
  title: string;
  intent: string;
  query: string;
  tool: "places" | "geocoding" | "directions" | "distance_matrix";
  tool_args: Record<string, unknown>;
  depends_on: number[];
  status: TaskStatus;
  summary: string;
  evidence: TaskEvidence;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
}

export interface MapMarker {
  place_id: string;
  name: string;
  lat: number;
  lng: number;
  rating: number | null;
  url: string | null;
}

export interface MapOverview {
  center?: { lat: number; lng: number };
  bounds?: { south: number; north: number; west: number; east: number };
  markers?: MapMarker[];
}

export type ServerEvent =
  | { type: "status"; timestamp: number; message: string; task_id?: number | null }
  | { type: "plan_ready"; timestamp: number; run_id: string; tasks: TaskNode[] }
  | {
      type: "task_update";
      timestamp: number;
      task_id: number;
      status: TaskStatus;
      summary?: string | null;
      detail?: string | null;
      evidence?: TaskEvidence | null;
    }
  | {
      type: "summary_chunk";
      timestamp: number;
      task_id: number;
      content: string;
    }
  | {
      type: "tool_call";
      timestamp: number;
      task_id: number;
      tool: string;
      request: Record<string, unknown>;
      cached: boolean;
    }
  | {
      type: "tool_result";
      timestamp: number;
      task_id: number;
      tool: string;
      place_count: number;
      route_count: number;
      duration_ms: number;
      error?: string | null;
    }
  | {
      type: "report";
      timestamp: number;
      markdown: string;
      itinerary: ItineraryDay[];
      map_overview: MapOverview;
    }
  | {
      type: "usage";
      timestamp: number;
      llm_prompt_tokens: number;
      llm_completion_tokens: number;
      maps_api_calls: number;
      elapsed_seconds: number;
    }
  | { type: "error"; timestamp: number; detail: string; task_id?: number | null }
  | { type: "done"; timestamp: number };

export interface ItinerarySlot {
  time: string;
  duration_min?: number;
  place_id?: string;
  name: string;
  category?: string;
  ticket?: string;
  open_check?: string;
  transport?: string;
  note?: string;
  tip?: string;
}

export interface ItineraryDay {
  day: number;
  title?: string;
  weather?: string;
  slots: ItinerarySlot[];
  cautions?: string[];
}
