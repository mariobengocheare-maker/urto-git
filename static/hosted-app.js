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
    bar.style.cssText = "display:flex; flex-wrap:wrap; align-items:center; justify-content:center; gap:10px; padding:8px 16px; font-size:.82rem; color:var(--text-secondary); background:rgba(6,11,22,.5); border-bottom:1px solid var(--border)";
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

  // Morning voice briefing (see build order #75). iOS can't autoplay
  // custom audio from a background push notification, so this is the
  // realistic version: the notification arrives as normal text, and the
  // instant Mario taps it and the app opens, it speaks the full briefing
  // out loud -- ?briefing=1 (set by the push's own click-through URL, see
  // hosted_app.py's send_morning_digest) is the signal that this open came
  // from that notification specifically, not just any normal app launch.
  function pickBestVoice(voices) {
    if (!voices.length) return null;
    const enUS = voices.filter((v) => v.lang === "en-US");
    // iOS exposes real "Enhanced"/"Premium" quality voices once Mario
    // downloads one for free in Settings -> Accessibility -> Spoken
    // Content -> Voices -- use one automatically if it's there.
    const enhanced = enUS.find((v) => /enhanced|premium/i.test(v.name));
    if (enhanced) return enhanced;
    const defaultUS = enUS.find((v) => v.default) || enUS[0];
    if (defaultUS) return defaultUS;
    const anyEn = voices.find((v) => v.lang && v.lang.startsWith("en"));
    return anyEn || voices[0];
  }

  function getVoicesAsync() {
    return new Promise((resolve) => {
      const voices = speechSynthesis.getVoices();
      if (voices.length) { resolve(voices); return; }
      speechSynthesis.onvoiceschanged = () => resolve(speechSynthesis.getVoices());
      setTimeout(() => resolve(speechSynthesis.getVoices()), 1000);
    });
  }

  async function maybeSpeakMorningBriefing() {
    const params = new URLSearchParams(window.location.search);
    if (params.get("briefing") !== "1") return;
    const url = new URL(window.location.href);
    url.searchParams.delete("briefing");
    window.history.replaceState({}, "", url);

    if (!("speechSynthesis" in window)) return;
    try {
      const res = await fetch("/api/notifications/briefing_text");
      const data = await res.json();
      if (!data.text) return;
      const voices = await getVoicesAsync();
      const utter = new SpeechSynthesisUtterance(data.text);
      const voice = pickBestVoice(voices);
      if (voice) utter.voice = voice;
      utter.rate = 0.98;
      speechSynthesis.speak(utter);
    } catch (e) {
      // A failed briefing fetch/speak should never block the app loading.
    }
  }

  async function setup() {
    maybeSpeakMorningBriefing();

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

    // A self-serve way to check whether push delivery itself works at
    // all, independent of any scheduling logic (the morning digest, the
    // 1hr/15min event reminders) -- if this doesn't arrive, the problem
    // is the subscription/permission, not the reminder timing.
    const testBtn = document.createElement("button");
    testBtn.className = "ghost small";
    testBtn.textContent = "Send Test";
    bar.appendChild(testBtn);
    const testMsg = document.createElement("span");
    testMsg.style.cssText = "font-size:.8rem;";
    bar.appendChild(testMsg);
    testBtn.addEventListener("click", async () => {
      testBtn.disabled = true;
      testMsg.textContent = "Sending…";
      try {
        const res = await fetch("/api/push/test", { method: "POST" });
        const data = await res.json();
        if (data.error) {
          testMsg.textContent = "⚠ " + data.error;
        } else if (data.sent_to > 0) {
          testMsg.textContent = `✅ Sent to ${data.sent_to} device${data.sent_to === 1 ? "" : "s"} — check your phone.`;
        } else {
          testMsg.textContent = "⚠ No active subscription — tap 🔕 Enable Morning Notifications first.";
        }
      } catch (e) {
        testMsg.textContent = "⚠ Couldn't reach the server.";
      }
      testBtn.disabled = false;
    });

    // Notification TIMING is a separate, tucked-away control from the
    // on/off toggle above -- a small gear that reveals a plain time input,
    // rather than cluttering the bar with a picker Mario isn't using most
    // days. Saved server-side (crm.get_notification_time/set_notification_time)
    // so it takes effect immediately with no redeploy, and applies the same
    // whether he's on his phone or ever checks it from a browser tab.
    const gearBtn = document.createElement("button");
    gearBtn.className = "ghost small";
    gearBtn.textContent = "⚙";
    gearBtn.title = "Notification time";
    bar.appendChild(gearBtn);

    const timeRow = document.createElement("div");
    timeRow.style.cssText = "display:none; align-items:center; gap:8px; width:100%; justify-content:center; padding-top:8px;";
    timeRow.innerHTML =
      '<span>Send the morning follow-up alert at</span>' +
      '<input type="time" id="urtoNotifyTimeInput">' +
      '<span>(Miami time)</span>' +
      '<button class="ghost small" id="urtoNotifyTimeSaveBtn">Save</button>' +
      '<span id="urtoNotifyTimeMsg"></span>';
    bar.appendChild(timeRow);

    const timeInput = timeRow.querySelector("#urtoNotifyTimeInput");
    const timeMsg = timeRow.querySelector("#urtoNotifyTimeMsg");

    gearBtn.addEventListener("click", async () => {
      const showing = timeRow.style.display === "flex";
      if (showing) {
        timeRow.style.display = "none";
        return;
      }
      try {
        const res = await fetch("/api/notifications/settings");
        const data = await res.json();
        timeInput.value = data.time || "07:00";
      } catch (e) {
        timeInput.value = "07:00";
      }
      timeRow.style.display = "flex";
    });

    timeRow.querySelector("#urtoNotifyTimeSaveBtn").addEventListener("click", async () => {
      if (!timeInput.value) return;
      timeMsg.textContent = "Saving…";
      try {
        const res = await fetch("/api/notifications/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ time: timeInput.value }),
        });
        const data = await res.json();
        // formatTime12() (defined in the main inline <script>, same page)
        // -- data.time is the raw 24-hour "HH:MM" the server stores; every
        // timestamp shown to Mario must be 12-hour with AM/PM, never bare
        // 24-hour (see CLAUDE.md's own established rule on this).
        timeMsg.textContent = res.ok ? `✅ Saved — alerts now at ${formatTime12(data.time)}` : "⚠ " + (data.error || "Couldn't save.");
      } catch (e) {
        timeMsg.textContent = "⚠ Couldn't reach the server.";
      }
    });

    // The real "talking alarm" (see build order #76): iOS still can't speak
    // from a background push, but an iOS Shortcuts "Time of Day" automation
    // CAN fetch a URL and speak the result completely unattended. This is a
    // tucked-away link (Mario's own "tucked away" phrasing, matching the
    // notification-time gear above) that shows the exact URL to paste into
    // that Shortcut, built client-side from window.location.origin so it's
    // always correct for wherever this page is actually being served from.
    const alarmBtn = document.createElement("button");
    alarmBtn.className = "ghost small";
    alarmBtn.textContent = "⏰";
    alarmBtn.title = "Alarm Shortcut link";
    bar.appendChild(alarmBtn);

    const alarmRow = document.createElement("div");
    alarmRow.style.cssText = "display:none; flex-direction:column; align-items:center; gap:6px; width:100%; padding-top:8px;";
    alarmRow.innerHTML =
      '<span>Paste this URL into an iOS Shortcuts "Get Contents of URL" step, then "Speak Text" — set it to run on a Time of Day automation for a hands-free spoken alarm:</span>' +
      '<input type="text" id="urtoAlarmUrlInput" readonly style="width:100%; max-width:520px; font-size:.78rem;">' +
      '<div style="display:flex; gap:8px;">' +
      '<button class="ghost small" id="urtoAlarmCopyBtn">Copy</button>' +
      '<button class="ghost small" id="urtoAlarmRegenBtn">Regenerate link</button>' +
      '</div>' +
      '<span id="urtoAlarmMsg"></span>';
    bar.appendChild(alarmRow);

    const alarmUrlInput = alarmRow.querySelector("#urtoAlarmUrlInput");
    const alarmMsg = alarmRow.querySelector("#urtoAlarmMsg");

    function buildAlarmUrl(token) {
      return `${window.location.origin}/api/notifications/briefing_public?token=${token}`;
    }

    alarmBtn.addEventListener("click", async () => {
      const showing = alarmRow.style.display === "flex";
      if (showing) {
        alarmRow.style.display = "none";
        return;
      }
      alarmMsg.textContent = "";
      try {
        const res = await fetch("/api/notifications/briefing_token");
        const data = await res.json();
        alarmUrlInput.value = buildAlarmUrl(data.token);
      } catch (e) {
        alarmMsg.textContent = "⚠ Couldn't reach the server.";
      }
      alarmRow.style.display = "flex";
    });

    alarmRow.querySelector("#urtoAlarmCopyBtn").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(alarmUrlInput.value);
        alarmMsg.textContent = "✅ Copied.";
      } catch (e) {
        alarmUrlInput.select();
        alarmMsg.textContent = "Select and copy manually.";
      }
    });

    alarmRow.querySelector("#urtoAlarmRegenBtn").addEventListener("click", async () => {
      if (!confirm("This invalidates the current alarm link — any Shortcut already using it will stop working until you update it with the new one. Continue?")) return;
      alarmMsg.textContent = "Regenerating…";
      try {
        const res = await fetch("/api/notifications/briefing_token/regenerate", { method: "POST" });
        const data = await res.json();
        alarmUrlInput.value = buildAlarmUrl(data.token);
        alarmMsg.textContent = "✅ New link generated — update your Shortcut with this one.";
      } catch (e) {
        alarmMsg.textContent = "⚠ Couldn't reach the server.";
      }
    });

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
