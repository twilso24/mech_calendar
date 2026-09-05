// Mech.Calendar floating-window chrome.
// The shared draggable/resizable surface chrome (setupFloatingSurfaceModalChrome) is
// opt-in per surface: without this hook, "Open as window" stays a fixed-size modal.
// Mirrors file-browser-store.js setupFloatingModal() options.
import { setupFloatingSurfaceModalChrome } from "/js/surfaces.js";

export default async function calendarFloatingChrome({ modalPath, modal } = {}) {
  if (!modalPath || !modalPath.startsWith("/plugins/mech_calendar/")) return;
  const element = modal?.element;
  if (!element || !document.contains(element)) return;
  setupFloatingSurfaceModalChrome({
    modal: element,
    modalClass: "mech-calendar-modal",
    minWidth: 420,
    minHeight: 360,
  });
}
