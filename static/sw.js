// URTO's service worker. Minimal on purpose: no offline caching (URTO always
// needs a live connection to the server for real data anyway), just enough
// to satisfy iOS Safari's "installable PWA" requirement and to receive Web
// Push notifications (the morning follow-up count) while the app isn't open.

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = { title: "URTO", body: "You have an update." };
  try {
    if (event.data) data = event.data.json();
  } catch (e) {
    // non-JSON payload -- fall back to the default text above
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "URTO", {
      body: data.body || "",
      icon: "/static/urto_icon_192.png",
      badge: "/static/urto_icon_192.png",
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(targetUrl);
    })
  );
});
