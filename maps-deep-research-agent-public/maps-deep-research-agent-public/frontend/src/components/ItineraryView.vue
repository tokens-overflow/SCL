<script setup lang="ts">
import type { ItineraryDay } from "../types/events";

const props = defineProps<{ itinerary: ItineraryDay[] }>();
</script>

<template>
  <section>
    <h2 class="panel-title">推荐行程</h2>
    <div v-if="!props.itinerary?.length" class="itinerary-empty">
      暂无行程建议。
    </div>
    <div v-for="day in props.itinerary" :key="day.day" class="itinerary-day">
      <h3>第 {{ day.day }} 天</h3>
      <ul>
        <li v-for="slot in day.slots" :key="slot.time + slot.name">
          <strong>{{ slot.time }}</strong> · {{ slot.name }}
          <span v-if="slot.note" class="itinerary-note">— {{ slot.note }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<style scoped>
.itinerary-empty {
  font-size: 0.85rem;
  color: #94a3b8;
}
.itinerary-day {
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 8px 12px;
  margin-bottom: 8px;
  background: #f8fafc;
}
.itinerary-day h3 {
  margin: 4px 0;
  font-size: 1rem;
}
.itinerary-day ul {
  margin: 4px 0;
  padding-left: 18px;
  font-size: 0.9rem;
}
.itinerary-note {
  color: #64748b;
}
</style>
