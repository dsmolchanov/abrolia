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
    const dnsPanel = document.querySelector("#dns-record-panel");
    const dnsRecords = document.querySelector("#dns-records");
    const dnsStatusPanel = document.querySelector("#dns-status-panel");
    const dnsRecordStatus = document.querySelector("#dns-record-status");
    const googlePanel = document.querySelector("#google-oauth-panel");
    const googleConnect = document.querySelector("#google-connect");
    const googleConfirm = document.querySelector("#google-confirm");
    const googleConfirmLabel = document.querySelector("#google-confirm-label");
    const googleConnected = document.querySelector("#google-connected");
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
    const records = current?.public_status?.dns_records || [];
    dnsRecords.replaceChildren(...records.map((record) => {
      const item = document.createElement("li");
      const value = document.createElement("code");
      const parts = [record.type, record.host];
      if (record.priority !== undefined && record.priority !== null) parts.push(String(record.priority));
      parts.push(record.value);
      value.textContent = parts.filter(Boolean).join(" ");
      item.append(value);
      if (record.purpose) item.append(document.createTextNode(` — ${record.purpose}`));
      return item;
    }));
    dnsPanel.hidden = records.length === 0;
    const recordStatus = current?.public_status?.record_status || {};
    dnsRecordStatus.replaceChildren(...Object.entries(recordStatus).sort().map(([name, ready]) => {
      const item = document.createElement("li");
      item.textContent = `${name}: ${ready ? "verified" : "pending"}`;
      return item;
    }));
    dnsStatusPanel.hidden = Object.keys(recordStatus).length === 0;
    const googleState = current?.public_status?.state;
    const callbackConfirmed = new URLSearchParams(window.location.search).get("google") === "confirm";
    googlePanel.hidden = !["oauth_required", "dedicated_account_confirmation"].includes(googleState)
      && !callbackConfirmed;
    googleConnect.hidden = googleState !== "oauth_required" || callbackConfirmed;
    googleConfirm.hidden = googleState !== "dedicated_account_confirmation" && !callbackConfirmed;
    googleConfirmLabel.hidden = googleConfirm.hidden;
    const maskedAddress = current?.public_status?.connected_address_masked;
    googleConnected.hidden = !maskedAddress;
    if (maskedAddress) googleConnected.querySelector("strong").textContent = maskedAddress;
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
    if (["abrolia_managed", "gmail_agent", "family_domain"].includes(option)) {
      selection.special_category_restriction_acknowledged = true;
      selection.special_category_restriction_receipt_id = crypto.randomUUID();
    }
    if (option === "shared_abrolia") selection = {kind: option, member_phone_test_ref: "synthetic-phone:owner", privacy_notice_receipt_id: crypto.randomUUID()};
    if (option === "dedicated_number") selection = {kind: option, phone_test_ref: "synthetic-phone:owner", privacy_notice_receipt_id: crypto.randomUUID(), linked_device_risk_receipt_id: crypto.randomUUID()};
    if (["telegram", "whatsapp", "web"].includes(option)) selection = {kind: option, actor_id: "synthetic-owner", chat_id: "synthetic-chat"};
    command(`/api/v1/onboarding/steps/${kind}/select`, selection);
  }));
  const domainInput = document.querySelector('input[name="domain"]');
  const domainGuidance = document.querySelector("#domain-guidance");
  const mxWarning = document.querySelector("#mx-change-warning");
  let guidanceRequest = 0;
  async function refreshDomainGuidance() {
    if (!domainInput || !domainGuidance || !mxWarning) return;
    const request = ++guidanceRequest;
    try {
      const response = await fetch(`/api/v1/email/domain/guidance?domain=${encodeURIComponent(domainInput.value)}`);
      if (request !== guidanceRequest) return;
      if (!response.ok) {
        domainGuidance.textContent = "Enter a supported domain you control.";
        return;
      }
      const guidance = await response.json();
      domainGuidance.textContent = guidance.apex_mx_risk
        ? `Recommended mail subdomain: ${guidance.recommended_domain}`
        : `Mail domain: ${guidance.domain}`;
      mxWarning.hidden = !guidance.apex_mx_risk;
      mxWarning.querySelector("input").required = guidance.apex_mx_risk;
    } catch (_error) {
      domainGuidance.textContent = "Domain guidance is temporarily unavailable.";
    }
  }
  domainInput?.addEventListener("change", refreshDomainGuidance);
  domainInput?.addEventListener("blur", refreshDomainGuidance);
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
  document.querySelector("#google-connect")?.addEventListener("click", async () => {
    const status = document.querySelector("#google-status");
    status.textContent = "Opening Google's secure authorization page…";
    try {
      const response = await fetch("/api/v1/email/google/start", {
        method: "POST",
        headers: commandHeaders(state.version),
      });
      if (!response.ok) throw new Error("unavailable");
      const result = await response.json();
      window.location.assign(result.authorization_url);
    } catch (_error) {
      status.textContent = "Google connection is unavailable for this account.";
    }
  });
  document.querySelector("#google-confirm")?.addEventListener("click", async () => {
    const checkbox = document.querySelector("#google-dedicated");
    const status = document.querySelector("#google-status");
    if (!checkbox.checked) {
      status.textContent = "Confirm that this is a separate agent mailbox.";
      return;
    }
    await command("/api/v1/email/google/confirm", {dedicated_mailbox: true});
    history.replaceState(null, "", "/onboarding");
  });
  refreshDomainGuidance();
  refresh();
  window.setInterval(() => {
    const current = state?.steps.find((step) => step.kind === state.current_step);
    if (["runtime_provisioning", "activating"].includes(state?.state)
        || ["provisioning", "verifying"].includes(current?.status)) refresh();
  }, 2500);
}
