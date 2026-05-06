(function () {
  "use strict";

  const DISMISS_KEY = "softx_pwa_dismiss_until";
  const INSTALL_HIDE_KEY = "softx_pwa_installed";
  const DISMISS_HOURS = 24;

  const isStandalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  if (isStandalone) {
    localStorage.setItem(INSTALL_HIDE_KEY, "1");
    return;
  }

  const now = Date.now();
  const dismissUntil = parseInt(localStorage.getItem(DISMISS_KEY) || "0", 10);
  if (dismissUntil && dismissUntil > now) return;
  if (localStorage.getItem(INSTALL_HIDE_KEY) === "1") return;

  let deferredPrompt = null;
  let bannerShown = false;
  let waitingForPrompt = false;
  const isiOS = /iphone|ipad|ipod/i.test(navigator.userAgent || "");
  const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent || "");

  function removeBanner() {
    const existing = document.getElementById("pwaInstallBanner");
    if (existing) existing.remove();
  }

  function postponeBanner() {
    const until = Date.now() + DISMISS_HOURS * 60 * 60 * 1000;
    localStorage.setItem(DISMISS_KEY, String(until));
    removeBanner();
  }

  async function triggerInstall() {
    if (deferredPrompt) {
      deferredPrompt.prompt();
      const result = await deferredPrompt.userChoice;
      if (result && result.outcome === "accepted") {
        localStorage.setItem(INSTALL_HIDE_KEY, "1");
      } else {
        postponeBanner();
      }
      deferredPrompt = null;
      removeBanner();
      return;
    }

    if (isiOS && isSafari) {
      const hint = document.getElementById("pwaInstallHint");
      if (hint) {
        hint.textContent = "iPhone/iPad: Share icon -> Add to Home Screen";
        hint.classList.add("show");
      }
      return;
    }

    waitingForPrompt = true;
    const hint = document.getElementById("pwaInstallHint");
    if (hint) {
      hint.textContent = "Install prompt প্রস্তুত হচ্ছে, আবার Install চাপুন...";
      hint.classList.add("show");
    }
  }

  function showBanner() {
    if (bannerShown) return;
    bannerShown = true;

    const banner = document.createElement("section");
    banner.className = "pwa-install-banner";
    banner.id = "pwaInstallBanner";
    banner.innerHTML = `
      <div class="pwa-install-copy">
        <strong>Install Soft X App</strong>
        <span>Open like app on Android, iPhone, Windows, Mac.</span>
        <small id="pwaInstallHint"></small>
      </div>
      <div class="pwa-install-actions">
        <button type="button" id="pwaInstallBtn">Install</button>
        <button type="button" class="pwa-skip" id="pwaSkipBtn">Later</button>
      </div>
    `;
    document.body.appendChild(banner);

    const installBtn = document.getElementById("pwaInstallBtn");
    const skipBtn = document.getElementById("pwaSkipBtn");
    if (installBtn) installBtn.addEventListener("click", triggerInstall);
    if (skipBtn) skipBtn.addEventListener("click", postponeBanner);
  }

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredPrompt = event;
    if (!bannerShown) showBanner();
    if (waitingForPrompt) {
      waitingForPrompt = false;
      triggerInstall().catch(() => undefined);
    }
  });

  window.addEventListener("appinstalled", () => {
    localStorage.setItem(INSTALL_HIDE_KEY, "1");
    removeBanner();
  });

  window.addEventListener("load", () => {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/service-worker.js").catch(() => undefined);
    }
    // Always show friendly install option shortly after load (if not installed).
    window.setTimeout(showBanner, 1200);
  });
})();
