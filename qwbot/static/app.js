document.addEventListener("click", (event) => {
  const openButton = event.target.closest("[data-open-modal]");
  if (openButton) {
    const dialog = document.getElementById(openButton.dataset.openModal);
    if (dialog) {
      dialog.showModal();
    }
    return;
  }

  const closeButton = event.target.closest("[data-close-modal]");
  if (closeButton) {
    const dialog = document.getElementById(closeButton.dataset.closeModal);
    if (dialog) {
      dialog.close();
    }
    return;
  }

  const confirmButton = event.target.closest("[data-confirm]");
  if (confirmButton && !window.confirm(confirmButton.dataset.confirm)) {
    event.preventDefault();
  }
});

document.addEventListener("cancel", (event) => {
  if (event.target.matches("dialog")) {
    event.target.close();
  }
});

function syncBlockReasonField(form) {
  const statusSelect = form.querySelector("select[name='execution_status']");
  const reasonField = form.querySelector(".block-reason-field");
  const reasonInput = form.querySelector("textarea[name='block_reason']");
  if (!statusSelect || !reasonField || !reasonInput) {
    return;
  }

  const isBlocked = statusSelect.value === "有阻塞";
  reasonField.hidden = !isBlocked;
  reasonInput.required = isBlocked;
  if (!isBlocked) {
    reasonInput.value = "";
  }
}

document.addEventListener("change", (event) => {
  if (event.target.matches("select[name='execution_status']")) {
    syncBlockReasonField(event.target.closest("form"));
  }
});

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".modal-card").forEach(syncBlockReasonField);
  startClock();
});

function startClock() {
  updateClock();
  window.setInterval(updateClock, 60 * 1000);
}

function updateClock() {
  const dateElement = document.querySelector("[data-clock-date]");
  const timeElement = document.querySelector("[data-clock-time]");
  if (!dateElement || !timeElement) {
    return;
  }

  const now = new Date();
  dateElement.textContent = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "long",
  }).format(now);
  timeElement.textContent = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(now);
}
