import { products } from "./catalog.ts";
export const eventNames = [
  "product_view",
  "product_click",
  "gallery_view",
  "max_redirect",
  "signup_started",
  "generation_started",
  "payment_started",
  "payment_success",
] as const;
export type AnalyticsEvent = (typeof eventNames)[number];
export function trackEvent(
  name: AnalyticsEvent,
  payload: { product?: string; category?: string } = {},
) {
  if (typeof window === "undefined") return;
  const detail: { name: AnalyticsEvent; product?: string; category?: string } =
    { name };
  if (payload.product && products.some((p) => p.id === payload.product))
    detail.product = payload.product;
  if (payload.category && /^[a-z-]{1,30}$/.test(payload.category))
    detail.category = payload.category;
  window.dispatchEvent(new CustomEvent("wish:analytics", { detail }));
}
export function attribution(search: string) {
  const q = new URLSearchParams(search),
    values: Record<string, string> = {};
  for (const key of [
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
  ]) {
    const v = q.get(key);
    if (v && /^[a-zA-Z][a-zA-Z0-9_-]{0,47}$/.test(v)) values[key] = v;
  }
  return values;
}
export function captureAttribution() {
  try {
    if (!sessionStorage.getItem("wish:attribution")) {
      const data = attribution(location.search);
      if (Object.keys(data).length)
        sessionStorage.setItem("wish:attribution", JSON.stringify(data));
    }
  } catch {
    /* Storage may be unavailable. */
  }
}
