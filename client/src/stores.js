import { writable } from "svelte/store";

export const title = writable("");
export const logged_in = writable(false);
export const current_page = writable("plan");
export const settings = writable({});
export const preferences = writable({});
export const active_modal = writable("");