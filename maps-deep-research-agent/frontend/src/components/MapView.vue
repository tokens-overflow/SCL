<script setup lang="ts">
import { Loader } from "@googlemaps/js-api-loader";
import { onMounted, ref, watch } from "vue";
import type { MapOverview } from "../types/events";

const props = defineProps<{ overview: MapOverview }>();

const container = ref<HTMLDivElement | null>(null);
let mapInstance: google.maps.Map | null = null;
let markers: google.maps.Marker[] = [];

const apiKey = import.meta.env.VITE_GOOGLE_MAPS_JS_KEY;
const loader = apiKey
  ? new Loader({ apiKey, version: "weekly", libraries: ["maps", "marker"] })
  : null;

async function ensureMap(): Promise<google.maps.Map | null> {
  if (!loader || !container.value) return null;
  if (mapInstance) return mapInstance;
  await loader.load();
  mapInstance = new google.maps.Map(container.value, {
    zoom: 12,
    center: { lat: 35.6595, lng: 139.7005 },
    mapTypeControl: false,
    streetViewControl: false,
  });
  return mapInstance;
}

async function render(overview: MapOverview) {
  const map = await ensureMap();
  if (!map) return;

  markers.forEach((m) => m.setMap(null));
  markers = [];

  const points = overview.markers || [];
  if (points.length === 0) return;

  points.forEach((point) => {
    const marker = new google.maps.Marker({
      position: { lat: point.lat, lng: point.lng },
      map,
      title: `${point.name}${point.rating ? ` (${point.rating}⭐)` : ""}`,
    });
    if (point.url) {
      marker.addListener("click", () => window.open(point.url!, "_blank"));
    }
    markers.push(marker);
  });

  if (overview.bounds) {
    map.fitBounds({
      south: overview.bounds.south,
      north: overview.bounds.north,
      west: overview.bounds.west,
      east: overview.bounds.east,
    });
  } else if (overview.center) {
    map.setCenter(overview.center);
  }
}

onMounted(() => {
  if (props.overview?.markers?.length) {
    render(props.overview);
  }
});

watch(
  () => props.overview,
  (next) => {
    if (next) render(next);
  },
  { deep: true }
);
</script>

<template>
  <section class="map-view">
    <h2 class="panel-title">地图概览</h2>
    <div v-if="!apiKey" class="map-view__warning">
      请在 .env.local 中配置 VITE_GOOGLE_MAPS_JS_KEY 以渲染地图。
    </div>
    <div ref="container" class="map-view__container" />
  </section>
</template>

<style scoped>
.map-view {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.map-view__container {
  flex: 1;
  min-height: 240px;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid #e2e8f0;
}
.map-view__warning {
  font-size: 0.85rem;
  color: #b45309;
  background: #fef3c7;
  padding: 6px 10px;
  border-radius: 6px;
  margin-bottom: 8px;
}
</style>
