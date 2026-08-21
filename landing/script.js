const messages = {
  de: {
    subject: "Wandertag am Donnerstag",
    body:
      'Bitte geben Sie Ihrem Kind <mark>bis Dienstag 12&nbsp;€</mark> mit. Treffpunkt ist <mark>um 8:15 Uhr</mark> am Haupteingang. Bitte bestätigen Sie die Teilnahme.',
  },
  fr: {
    subject: "Sortie scolaire jeudi",
    body:
      'Merci de remettre <mark>12&nbsp;€ avant mardi</mark> à votre enfant. Rendez-vous <mark>à 8&nbsp;h&nbsp;15</mark> devant l’entrée principale. Merci de confirmer sa participation.',
  },
  nl: {
    subject: "Schooluitje op donderdag",
    body:
      'Geef uw kind uiterlijk <mark>dinsdag €&nbsp;12</mark> mee. We verzamelen <mark>om 08.15 uur</mark> bij de hoofdingang. Bevestig alstublieft de deelname.',
  },
};

const header = document.querySelector("[data-header]");
const demo = document.querySelector("[data-relay-demo]");
const messageCard = demo?.querySelector(".message-card");
const subject = demo?.querySelector("[data-message-subject]");
const body = demo?.querySelector("[data-message-body]");
const languageTabs = demo?.querySelectorAll("[data-language]") ?? [];
const approveButton = demo?.querySelector("[data-approve-demo]");
const approveLabel = demo?.querySelector("[data-approve-label]");
const proposalStatus = demo?.querySelector("[data-proposal-status]");
const approvalResult = demo?.querySelector("[data-approval-result]");
const pilotForm = document.querySelector("[data-pilot-form]");

const updateHeader = () => {
  header?.classList.toggle("is-scrolled", window.scrollY > 18);
};

updateHeader();
window.addEventListener("scroll", updateHeader, { passive: true });

languageTabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    const language = tab.dataset.language;
    const nextMessage = messages[language];

    if (!nextMessage || tab.classList.contains("is-active")) return;

    languageTabs.forEach((item) => {
      const isSelected = item === tab;
      item.classList.toggle("is-active", isSelected);
      item.setAttribute("aria-selected", String(isSelected));
    });

    messageCard?.classList.add("is-switching");
    window.setTimeout(() => {
      if (subject) subject.textContent = nextMessage.subject;
      if (body) body.innerHTML = nextMessage.body;
      messageCard?.classList.remove("is-switching");
    }, 130);
  });
});

approveButton?.addEventListener("click", () => {
  const approved = approveButton.getAttribute("aria-pressed") === "true";
  const nextApproved = !approved;

  approveButton.setAttribute("aria-pressed", String(nextApproved));
  demo?.classList.toggle("is-approved", nextApproved);

  if (approveLabel) {
    approveLabel.textContent = nextApproved ? "Approved by you" : "Review & approve demo";
  }
  if (proposalStatus) {
    proposalStatus.textContent = nextApproved ? "Approved" : "Needs your approval";
  }
  if (approvalResult) {
    approvalResult.textContent = nextApproved
      ? "Calendar plan ready · reply remains a draft"
      : "Nothing is added or sent yet.";
  }
});

pilotForm?.addEventListener("submit", (event) => {
  event.preventDefault();

  const data = new FormData(pilotForm);
  const name = String(data.get("name") ?? "").trim();
  const email = String(data.get("email") ?? "").trim();
  const country = String(data.get("country") ?? "").trim();
  const languages = String(data.get("languages") ?? "").trim();
  const admin = String(data.get("admin") ?? "").trim();

  const subjectLine = `Abrolia private pilot request — ${name}`;
  const message = [
    "Hi Abrolia,",
    "",
    "I would like to hear about the private pilot.",
    "",
    `Name: ${name}`,
    `Email: ${email}`,
    `Country: ${country}`,
    `Languages at home: ${languages}`,
    `What takes the most time: ${admin || "—"}`,
    "",
    "Please contact me about the Abrolia private pilot.",
  ].join("\n");

  window.location.href = `mailto:hello@abrolia.com?subject=${encodeURIComponent(subjectLine)}&body=${encodeURIComponent(message)}`;
});
