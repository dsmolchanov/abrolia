const form = document.getElementById("form");
const input = document.getElementById("input");
const messages = document.getElementById("messages");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;

  // Double-submit CSRF: echo the session's csrf cookie back in a header,
  // as every other mutating endpoint requires.
  const csrfEntry = document.cookie
    .split("; ")
    .find((entry) => entry.split("=")[0].endsWith("csrf"));
  const csrfToken = csrfEntry
    ? decodeURIComponent(csrfEntry.split("=").slice(1).join("="))
    : "";

  const response = await fetch("/api/web/message", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify({ text }),
  });
  const data = await response.json().catch(() => ({}));
  const message = document.createElement("p");
  message.textContent = data.reply || data.detail || "ошибка";
  messages.appendChild(message);
  input.value = "";
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("./sw.js").catch(() => {});
}
