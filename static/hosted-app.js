// Hosted-mode-only extras: PWA install support + Web Push notifications for
// the morning follow-up count. Loaded only when templates/index.html renders
// with hosted=true (see hosted_app.py) — the desktop app never loads this.
(function () {
  function isStandalone() {
    return window.navigator.standalone === true ||
      window.matchMedia("(display-mode: standalone)").matches;
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const rawData = atob(base64);
    const arr = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; i++) arr[i] = rawData.charCodeAt(i);
    return arr;
  }

  async function getSubscription() {
    const reg = await navigator.serviceWorker.ready;
    return reg.pushManager.getSubscription();
  }

  async function subscribe() {
    const reg = await navigator.serviceWorker.ready;
    const res = await fetch("/api/push/vapid_public_key");
    const { public_key } = await res.json();
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(public_key),
    });
    await fetch("/api/push/subscribe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sub.toJSON()),
    });
  }

  async function unsubscribe() {
    const sub = await getSubscription();
    if (!sub) return;
    await fetch("/api/push/unsubscribe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint: sub.endpoint }),
    });
    await sub.unsubscribe();
  }

  function injectBar() {
    const bar = document.createElement("div");
    bar.id = "urtoPushBar";
    bar.style.cssText = "display:flex; align-items:center; justify-content:center; gap:10px; padding:8px 16px; font-size:.82rem; color:var(--text-secondary); background:rgba(6,11,22,.5); border-bottom:1px solid var(--border)";
    const main = document.querySelector("main");
    main.parentNode.insertBefore(bar, main);
    return bar;
  }

  async function maybeShowImportPrompt() {
    try {
      const res = await fetch("/api/crm/clients");
      const clients = res.ok ? await res.json() : [];
      if (clients.length > 0) return; // already has real data -- one-time prompt only makes sense on an empty DB
    } catch (e) {
      return;
    }
    const bar = document.createElement("div");
    bar.style.cssText = "display:flex; align-items:center; justify-content:center; gap:10px; flex-wrap:wrap; padding:10px 16px; font-size:.85rem; background:rgba(201,162,39,.12); border-bottom:1px solid rgba(201,162,39,.3)";
    bar.innerHTML = '<span>No clients here yet — bring over your real data from a backup?</span>' +
      '<button class="ghost small" id="urtoImportDataBtn">Import from URTO_Full_Data_Export.json</button>' +
      '<input type="file" id="urtoImportDataInput" accept=".json" style="display:none">' +
      '<span id="urtoImportDataMsg"></span>';
    const main = document.querySelector("main");
    main.parentNode.insertBefore(bar, main);

    const fileInput = document.getElementById("urtoImportDataInput");
    const msg = document.getElementById("urtoImportDataMsg");
    document.getElementById("urtoImportDataBtn").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", async () => {
      if (!fileInput.files.length) return;
      msg.textContent = "Importing…";
      const form = new FormData();
      form.append("file", fileInput.files[0]);
      try {
        const res = await fetch("/api/admin/import_backup_json", { method: "POST", body: form });
        const data = await res.json();
        if (!res.ok || data.error) {
          msg.textContent = "⚠ " + (data.error || "Import failed.");
          return;
        }
        const total = Object.values(data.counts || {}).reduce((a, b) => a + b, 0);
        msg.textContent = `✅ Imported ${total} rows. Reloading…`;
        setTimeout(() => window.location.reload(), 1200);
      } catch (err) {
        msg.textContent = "⚠ Couldn't reach the server.";
      }
    });
  }

  async function setup() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("/sw.js").catch(() => {});
    maybeShowImportPrompt();

    if (!isStandalone()) {
      // iOS only allows Web Push for a PWA actually launched from the Home
      // Screen icon -- a plain Safari tab can't subscribe, so don't offer a
      // button that would just fail; explain the one extra step instead.
      const bar = injectBar();
      bar.textContent = "📱 For notifications: tap Share → \"Add to Home Screen\", then open URTO from that icon.";
      return;
    }

    if (!("PushManager" in window) || !("Notification" in window)) {
      const bar = injectBar();
      bar.textContent = "Push notifications aren't supported in this browser.";
      return;
    }

    const bar = injectBar();
    const btn = document.createElement("button");
    btn.className = "ghost small";
    bar.appendChild(btn);

    async function refresh() {
      const sub = await getSubscription();
      btn.textContent = sub ? "🔔 Notifications On (tap to turn off)" : "🔕 Enable Morning Notifications";
    }

    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const sub = await getSubscription();
        if (sub) {
          await unsubscribe();
        } else {
          if (Notification.permission === "denied") {
            alert('Notifications are blocked for URTO. Enable them in iPhone Settings → Notifications → URTO.');
          } else {
            const perm = await Notification.requestPermission();
            if (perm === "granted") await subscribe();
          }
        }
      } catch (err) {
        alert("Couldn't update notification settings: " + err.message);
      }
      btn.disabled = false;
      refresh();
    });

    refresh();
  }

  window.addEventListener("DOMContentLoaded", setup);
})();
