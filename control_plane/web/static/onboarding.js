"use strict";

const page = document.body.dataset.page;

function cookie(name) {
  return document.cookie.split("; ").find((part) => part.startsWith(`${name}=`))?.split("=")[1];
}

function commandHeaders(version) {
  return {
    "Content-Type": "application/json",
    "Origin": window.location.origin,
    "X-CSRF-Token": decodeURIComponent(cookie("__Host-abrolia_csrf") || ""),
    "Idempotency-Key": crypto.randomUUID(),
    "If-Match": String(version),
  };
}

if (page === "start") {
  document.querySelector("#request-link")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = event.currentTarget.querySelector(".form-status");
    await fetch("/api/v1/auth/request-link", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({email: new FormData(event.currentTarget).get("email")}),
    });
    status.textContent = "If this synthetic address is eligible, its secure link has been sent.";
  });
}

if (page === "verify") {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const token = fragment.get("token");
  history.replaceState(null, "", window.location.pathname);
  const status = document.querySelector("#verify-status");
  if (!token) {
    status.textContent = "This link is missing or has already been cleared.";
  } else {
    fetch("/api/v1/auth/consume", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token}),
    }).then(async (response) => {
      if (!response.ok) throw new Error("invalid");
      const result = await response.json();
      window.location.replace(result.next);
    }).catch(() => { status.textContent = "This link is invalid or expired. Request a new one."; });
  }
}

if (page === "onboarding") {
  const shell = document.querySelector(".onboarding-shell");
  let state = null;
  let commandInFlight = false;

  function setInteractive(enabled) {
    shell.querySelectorAll("#profile-form button, [data-select], #retry-step, #check-step, [data-reset]")
      .forEach((control) => { control.disabled = !enabled; });
  }

  function enableControls(selector) {
    shell.querySelectorAll(selector).forEach((control) => { control.disabled = false; });
  }

  // The server-rendered version is informative, but only a refreshed JSON
  // snapshot may drive an enhanced command. Server forms remain the no-JS path.
  setInteractive(false);

  function render(snapshot) {
    state = snapshot;
    shell.dataset.version = snapshot.version;
    shell.dataset.workflowState = snapshot.state;
    const workflowOwnsView = ["runtime_provisioning", "activating", "complete", "cancelled"]
      .includes(snapshot.state);
    document.querySelectorAll("[data-progress]").forEach((item) => {
      item.classList.toggle(
        "active",
        !workflowOwnsView && item.dataset.progress === snapshot.current_step,
      );
      item.classList.toggle("complete", snapshot.state === "complete");
    });
    document.querySelectorAll("[data-step]").forEach((item) => {
      item.hidden = workflowOwnsView || item.dataset.step !== snapshot.current_step;
    });
    const current = snapshot.steps.find((step) => step.kind === snapshot.current_step);
    const title = document.querySelector("#status-title");
    const copy = document.querySelector("#status-copy");
    const retry = document.querySelector("#retry-step");
    const check = document.querySelector("#check-step");
    const labels = {
      available: ["Ready", "Choose an option to continue."],
      selected: ["Selection saved", "The durable worker will begin this setup."],
      provisioning: ["Setting up", "The durable worker is applying your selection."],
      waiting_user: ["Waiting for you", "Complete the synthetic verification, then check again."],
      verifying: ["Checking verification", "The durable worker is inspecting the existing provider result."],
      failed: ["Needs attention", "The provider returned a safe error. You can retry this selection."],
      verified: ["Verified", "This choice is locked. Use explicit reset to change it."],
      cancelled: ["Setup cancelled", "No new onboarding work will be started."],
    };
    const workflows = {
      runtime_provisioning: ["Preparing your private runtime", "The durable worker is creating the synthetic runtime and immutable configuration."],
      activating: ["Activating your runtime", "Waiting for the runtime to install and acknowledge the exact configuration revision."],
      complete: ["Setup complete", "Your synthetic household runtime is active."],
      cancelled: ["Setup cancelled", "No new onboarding work will be started."],
    };
    const message = workflows[snapshot.state] || labels[current?.status]
      || ["Ready", "Choose an option to continue."];
    title.textContent = message[0]; copy.textContent = message[1];
    retry.hidden = workflowOwnsView || current?.status !== "failed";
    retry.dataset.kind = current?.kind || "";
    check.hidden = workflowOwnsView || current?.status !== "waiting_user";
    check.dataset.kind = current?.kind || "";
    setInteractive(false);
    if (!commandInFlight && !workflowOwnsView) {
      enableControls("[data-reset]");
      if (current?.status === "available") {
        if (current.kind === "profile") enableControls("#profile-form button");
        else enableControls(`[data-step="${current.kind}"] [data-select]`);
      }
      if (current?.status === "failed") enableControls("#retry-step");
      if (current?.status === "waiting_user") enableControls("#check-step");
    }
  }

  async function refresh() {
    try {
      const response = await fetch("/api/v1/onboarding/current", {headers: {"Accept": "application/json"}});
      if (response.status === 401) { window.location.replace("/start"); return false; }
      if (!response.ok) return false;
      render(await response.json());
      return true;
    } catch (_error) {
      document.querySelector("#status-title").textContent = "Connection interrupted";
      document.querySelector("#status-copy").textContent = "No command was sent. Reconnect to refresh durable state.";
      return false;
    }
  }

  async function command(path, body) {
    if (commandInFlight) return;
    if (state === null && !(await refresh())) return;
    commandInFlight = true;
    setInteractive(false);
    try {
      const response = await fetch(path, {
        method: path.endsWith("/profile") ? "PUT" : "POST",
        headers: commandHeaders(state.version),
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (response.status === 401) { window.location.replace("/start"); return; }
      if (response.ok) render(await response.json());
      else if (!(await refresh())) state = null;
    } catch (_error) {
      if (!(await refresh())) state = null;
    } finally {
      commandInFlight = false;
      if (state !== null) render(state); else setInteractive(false);
    }
  }

  document.querySelector("#profile-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    command("/api/v1/onboarding/profile", values);
  });
  document.querySelectorAll("[data-select]").forEach((button) => button.addEventListener("click", (event) => {
    event.preventDefault();
    const form = button.closest("form");
    if (form && !form.checkValidity()) { form.reportValidity(); return; }
    const kind = button.dataset.select;
    const option = button.dataset.kind;
    let selection = {kind: option};
    if (option === "abrolia_managed") selection.local_part = "family.assistant";
    if (option === "gmail_agent") selection.separate_agent_account_acknowledged = true;
    if (option === "family_domain") selection = {
      kind: option,
      domain: form.elements.domain.value,
      local_part: form.elements.local_part.value,
      mx_change_acknowledged: form.elements.mx_change_acknowledged.checked,
    };
    if (option === "shared_abrolia") selection = {kind: option, member_phone_test_ref: "synthetic-phone:owner", privacy_notice_receipt_id: crypto.randomUUID()};
    if (option === "dedicated_number") selection = {kind: option, phone_test_ref: "synthetic-phone:owner", privacy_notice_receipt_id: crypto.randomUUID(), linked_device_risk_receipt_id: crypto.randomUUID()};
    if (["telegram", "whatsapp", "web"].includes(option)) selection = {kind: option, actor_id: "synthetic-owner", chat_id: "synthetic-chat"};
    command(`/api/v1/onboarding/steps/${kind}/select`, selection);
  }));
  document.querySelector("#retry-step")?.addEventListener("click", (event) => {
    event.preventDefault();
    command(`/api/v1/onboarding/steps/${event.currentTarget.dataset.kind}/retry`);
  });
  document.querySelector("#check-step")?.addEventListener("click", (event) => {
    event.preventDefault();
    command(`/api/v1/onboarding/steps/${event.currentTarget.dataset.kind}/check`);
  });
  document.querySelectorAll("[data-reset]").forEach((button) => button.addEventListener("click", (event) => {
    event.preventDefault();
    command(`/api/v1/onboarding/reset/${button.dataset.reset}`);
  }));
  refresh();
  window.setInterval(() => {
    const current = state?.steps.find((step) => step.kind === state.current_step);
    if (["runtime_provisioning", "activating"].includes(state?.state)
        || ["provisioning", "verifying"].includes(current?.status)) refresh();
  }, 2500);
}
