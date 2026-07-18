<script setup lang="ts">
import { computed } from "vue";
import { allPlaces, researchState } from "../stores/research";
import type { ItineraryDay } from "../types/events";

defineProps<{ itinerary: ItineraryDay[] }>();

const catIcon: Record<string, string> = {
  景点: "⛩",
  餐饮: "🍜",
  购物: "🛍",
  交通: "🚇",
  休息: "☕",
};

// place_id → Google 地图链接，给行程每一站配一个可点进去的地图链接
const mapUrlById = computed<Record<string, string>>(() => {
  const m: Record<string, string> = {};
  for (const p of allPlaces.value) if (p.google_maps_url) m[p.place_id] = p.google_maps_url;
  return m;
});

function mapUrl(slot: { place_id?: string; name: string }): string {
  if (slot.place_id && mapUrlById.value[slot.place_id]) return mapUrlById.value[slot.place_id];
  // 兜底：用名字直接搜 Google 地图
  return "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(slot.name);
}

function dur(min?: number): string {
  if (!min) return "";
  if (min < 60) return `${min}min`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return m ? `${h}h${m}` : `${h}h`;
}
</script>

<template>
  <section class="itin">
    <header class="ih">
      <p class="eyebrow">行程时间线 · Itinerary</p>
    </header>

    <div v-if="!researchState.itinerary?.length" class="empty">
      ○ 行程拟于诸事考竟之后徐徐展开
    </div>

    <div class="days">
      <article
        v-for="(day, di) in researchState.itinerary"
        :key="day.day"
        class="day rise"
        :style="{ animationDelay: `${di * 0.05}s` }"
      >
        <div class="day-head">
          <span class="seal day-seal">{{ day.day }}</span>
          <div class="day-meta">
            <h3 class="day-title">第 {{ day.day }} 日<span v-if="day.title"> · {{ day.title }}</span></h3>
            <p v-if="day.weather" class="day-weather">☼ {{ day.weather }}</p>
          </div>
        </div>

        <ol class="slots">
          <li
            v-for="slot in day.slots"
            :key="slot.time + slot.name"
            class="slot"
          >
            <div class="time-col">
              <span class="time mono">{{ slot.time }}</span>
              <span v-if="slot.duration_min" class="dur mono">{{ dur(slot.duration_min) }}</span>
            </div>

            <div class="slot-body">
              <div class="sname-row">
                <span class="cat" v-if="slot.category">{{ catIcon[slot.category] || "•" }}</span>
                <a
                  class="sname"
                  :href="mapUrl(slot)"
                  target="_blank"
                  rel="noopener"
                  >{{ slot.name }} ↗</a
                >
                <span v-if="slot.ticket && slot.ticket !== '—'" class="ticket">{{ slot.ticket }}</span>
              </div>

              <p v-if="slot.note" class="note">{{ slot.note }}</p>

              <div class="flags">
                <span v-if="slot.open_check" class="flag open">🕑 {{ slot.open_check }}</span>
                <span v-if="slot.tip" class="flag tip">⚠ {{ slot.tip }}</span>
              </div>

              <div v-if="slot.transport" class="transport">
                <span class="t-line" /> {{ slot.transport }}
              </div>
            </div>
          </li>
        </ol>

        <ul v-if="day.cautions?.length" class="cautions">
          <li v-for="(c, i) in day.cautions" :key="i">※ {{ c }}</li>
        </ul>
      </article>
    </div>
  </section>
</template>

<style scoped>
.itin {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.days {
  display: flex;
  flex-direction: column;
  gap: 22px;
  overflow: auto;
  padding-right: 2px;
}
.day {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.day-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--line);
}
.day-seal {
  width: 40px;
  height: 40px;
  font-size: 1.4rem;
  flex: none;
}
.day-title {
  margin: 0;
  font-family: var(--font-brush);
  font-size: 1.2rem;
  font-weight: 400;
  letter-spacing: 1px;
  color: var(--text-hi);
}
.day-weather {
  margin: 2px 0 0;
  font-size: 0.78rem;
  color: var(--mist);
}
.slots {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--line);
  margin-left: 18px;
}
.slot {
  display: grid;
  grid-template-columns: 58px 1fr;
  gap: 12px;
  padding: 11px 12px 11px 14px;
  margin-left: -1px;
  border-left: 2px solid transparent;
  position: relative;
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  transition: background 0.15s, border-color 0.15s;
}
.slot::before {
  content: "";
  position: absolute;
  left: -5px;
  top: 16px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--bg-3);
  border: 1.5px solid var(--accent-dim);
}
.slot:hover {
  background: var(--bg-2);
}
.time-col {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-top: 1px;
}
.time {
  font-size: 0.84rem;
  color: var(--accent);
  font-weight: 700;
}
.dur {
  font-size: 0.64rem;
  color: var(--text-lo);
}
.slot-body {
  min-width: 0;
}
.sname-row {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}
.cat {
  font-size: 0.95rem;
}
.sname {
  font-size: 0.95rem;
  color: var(--text-hi);
  font-weight: 600;
  text-decoration: none;
  border-bottom: 1px solid transparent;
  transition: color 0.15s, border-color 0.15s;
}
.sname:hover {
  color: var(--accent);
  border-bottom-color: var(--accent-dim);
}
.ticket {
  font-size: 0.68rem;
  font-family: var(--font-mono);
  color: var(--jade);
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  padding: 1px 7px;
}
.note {
  margin: 5px 0 0;
  font-size: 0.82rem;
  color: var(--text-mid);
  line-height: 1.55;
}
.flags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.flag {
  font-size: 0.72rem;
  padding: 2px 8px;
  border-radius: var(--radius-xs);
  line-height: 1.4;
}
.flag.open {
  color: var(--info);
  background: rgba(90, 125, 146, 0.1);
}
.flag.tip {
  color: var(--accent);
  background: var(--accent-ghost);
}
.transport {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-top: 8px;
  font-size: 0.72rem;
  color: var(--text-lo);
}
.t-line {
  width: 16px;
  height: 0;
  border-top: 1px dashed var(--line-strong);
}
.cautions {
  list-style: none;
  margin: 4px 0 0 18px;
  padding: 10px 12px;
  background: var(--bg-2);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.cautions li {
  font-size: 0.78rem;
  color: var(--text-mid);
  line-height: 1.5;
}
</style>
