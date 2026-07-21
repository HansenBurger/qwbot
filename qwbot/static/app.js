document.addEventListener("click", (event) => {
  const sourceSubmitButton = event.target.closest("[data-date-source]");
  if (sourceSubmitButton) {
    const form = sourceSubmitButton.closest("form");
    const sourceInput = form?.querySelector(`[name='${sourceSubmitButton.dataset.dateSource}']`);
    if (sourceInput) {
      sourceSubmitButton.value = sourceInput.value;
    }
  }

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

  const addDocButton = event.target.closest("[data-add-doc-link]");
  if (addDocButton) {
    const editor = addDocButton.closest("[data-doc-link-editor]");
    const list = editor?.querySelector("[data-doc-link-list]");
    if (list) {
      list.insertAdjacentHTML("beforeend", docLinkRowTemplate());
    }
    return;
  }

  const removeDocButton = event.target.closest("[data-remove-doc-link]");
  if (removeDocButton) {
    const row = removeDocButton.closest(".doc-link-row");
    const list = removeDocButton.closest("[data-doc-link-list]");
    if (row && list && list.querySelectorAll(".doc-link-row").length > 1) {
      row.remove();
    } else if (row) {
      row.querySelectorAll("input").forEach((input) => {
        input.value = "";
      });
    }
    return;
  }

  const previewButton = event.target.closest("[data-preview-notification]");
  if (previewButton) {
    renderNotificationPreview(previewButton.closest("form"));
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

function docLinkRowTemplate() {
  return `
    <div class="doc-link-row">
      <input name="doc_label" placeholder="描述，如：进度统计">
      <input type="url" name="doc_url" placeholder="https://doc.weixin.qq.com/...">
      <button type="button" class="danger-button compact-button" data-remove-doc-link>删除</button>
    </div>
  `;
}

function renderNotificationPreview(form) {
  if (!form) {
    return;
  }

  const title = form.querySelector("input[name='title']")?.value.trim() || "消息通知";
  const content = form.querySelector("textarea[name='content']")?.value.trim() || "";
  const atAll = form.querySelector("input[name='at_all']")?.checked;
  const docLines = Array.from(form.querySelectorAll(".doc-link-row"))
    .map((row) => {
      const label = row.querySelector("input[name='doc_label']")?.value.trim() || "在线文档";
      const url = row.querySelector("input[name='doc_url']")?.value.trim();
      return url ? `- [${label}](${url})` : "";
    })
    .filter(Boolean);

  const lines = [`【${title}】`, "", content];
  if (docLines.length) {
    lines.push("", "相关文档：", ...docLines);
  }
  if (atAll) {
    lines.push("", "@所有人");
  }

  const preview = form.querySelector("[data-notification-preview]");
  const previewContent = form.querySelector("[data-notification-preview-content]");
  if (preview && previewContent) {
    previewContent.textContent = lines.join("\n");
    preview.hidden = false;
  }
}

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

// Reminder template variable insertion
document.addEventListener("click", (event) => {
  const tag = event.target.closest("[data-insert-var]");
  if (!tag) return;

  const varName = tag.dataset.insertVar;
  const textarea = document.querySelector(".template-textarea");
  if (!textarea) return;

  const insertion = "{" + varName + "}";
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const text = textarea.value;

  textarea.value = text.slice(0, start) + insertion + text.slice(end);
  textarea.selectionStart = textarea.selectionEnd = start + insertion.length;
  textarea.focus();
});

// Reset template to default
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-reset-template]");
  if (!button) return;

  if (!confirm("确认恢复默认模板？当前修改将丢失。")) return;

  const textarea = document.querySelector(".template-textarea");
  if (!textarea) return;

  // Get default template from data attribute or reload
  const defaultTemplate = button.dataset.defaultTemplate;
  if (defaultTemplate) {
    textarea.value = defaultTemplate;
  }
});
