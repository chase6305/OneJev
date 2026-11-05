"use strict";
// Small adapter for the installed Viser 1.1 client. It changes only local view
// controls, never the robot API, shared scene state, or camera used for inference.
(() => {
  const root = document.documentElement;
  root.dataset.onejevEmbedded = "true";
  root.dataset.onejevControls = "false";
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.source !== parent) return;
    if (event.data?.type !== "onejev:viewer") return;
    if (typeof event.data.controls === "boolean") {
      root.dataset.onejevControls = String(event.data.controls);
    }
    if (event.data.resetCamera) {
      const reset = [...document.querySelectorAll("button")].find(
        (button) => button.textContent.trim() === "Reset View",
      );
      if (reset) reset.click();
      parent.postMessage(
        { type: "onejev:viewer-reset", ok: Boolean(reset) },
        location.origin,
      );
    }
  });
  parent.postMessage({ type: "onejev:viewer-ready" }, location.origin);
})();
