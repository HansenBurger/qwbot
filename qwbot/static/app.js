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
  const varLabel = tag.dataset.varLabel || "";
  const textarea = document.querySelector("[data-template-textarea]") || document.querySelector(".template-textarea");
  if (!textarea) return;

  // If the variable has a label (doc link var), insert as [点击]{var_name} link format
  const insertion = varLabel ? "[点击]({" + varName + "})" : "{" + varName + "}";
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const text = textarea.value;

  textarea.value = text.slice(0, start) + insertion + text.slice(end);
  // Place cursor after the insertion
  textarea.selectionStart = textarea.selectionEnd = start + insertion.length;
  textarea.focus();
});




// ── Template caching via localStorage ──
// Cache textarea content before any page reload (var save/delete actions)
// and restore it on page load if present.
(function () {
  const CACHE_KEY = "qwbot_template_cache";

  function cacheTemplateContent() {
    const textarea = document.querySelector("[data-template-textarea]");
    if (!textarea) return;
    const form = textarea.closest("form");
    if (!form) return;
    const data = {
      content: textarea.value,
      templateId: form.querySelector("[data-template-id-field]")?.value || "",
      templateName: form.querySelector("[data-template-name-field]")?.value || "",
    };
    try {
      sessionStorage.setItem(CACHE_KEY, JSON.stringify(data));
    } catch (_) {}
  }

  function restoreTemplateContent() {
    const textarea = document.querySelector("[data-template-textarea]");
    if (!textarea) return;
    let cached;
    try {
      cached = JSON.parse(sessionStorage.getItem(CACHE_KEY));
    } catch (_) {}
    if (!cached || !cached.content) return;

    const form = textarea.closest("form");
    const currentId = form?.querySelector("[data-template-id-field]")?.value || "";

    // Only restore if editing the same template (or no template id set)
    if (cached.templateId && currentId && cached.templateId !== currentId) {
      sessionStorage.removeItem(CACHE_KEY);
      return;
    }

    // Check if content was actually modified (different from server-rendered)
    const serverContent = textarea.defaultValue;
    if (cached.content === serverContent) {
      sessionStorage.removeItem(CACHE_KEY);
      return;
    }

    textarea.value = cached.content;
    sessionStorage.removeItem(CACHE_KEY);

    // Show a subtle notification
    const label = document.querySelector("[data-template-display-name]");
    if (label && cached.templateName) {
      label.textContent = cached.templateName + " (已恢复未保存的编辑)";
      setTimeout(() => {
        label.textContent = cached.templateName;
      }, 4000);
    }
  }

  // Cache before form submissions that aren't the template save form
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form) return;
    // Don't cache if this IS the template save form (it saves the template)
    if (form.hasAttribute("data-template-cache")) return;
    // Cache for any other form submission on the page
    cacheTemplateContent();
  });

  // Cache before clicking links that navigate away
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (link && !link.target) {
      cacheTemplateContent();
    }
  });

  // Restore on page load
  document.addEventListener("DOMContentLoaded", restoreTemplateContent);
})();

// ── Template list: load template into editor ──
document.addEventListener("click", (event) => {
  const loadBtn = event.target.closest("[data-load-template]");
  if (!loadBtn) return;

  const templateContent = loadBtn.dataset.templateContent || "";
  const templateName = loadBtn.dataset.templateName || "";
  const templateId = loadBtn.dataset.templateId || "";

  const textarea = document.querySelector("[data-template-textarea]");
  if (!textarea) return;

  // Warn if there are unsaved changes
  if (textarea.value !== textarea.defaultValue && textarea.defaultValue) {
    if (!confirm("当前编辑器中有未保存的修改，加载此模板将覆盖当前内容。继续？")) {
      return;
    }
  }

  textarea.value = templateContent;
  textarea.defaultValue = templateContent;

  const form = textarea.closest("form");
  if (form) {
    const idField = form.querySelector("[data-template-id-field]");
    const nameField = form.querySelector("[data-template-name-field]");
    if (idField) idField.value = templateId;
    if (nameField) nameField.value = templateName;
  }

  const label = document.querySelector("[data-template-display-name]");
  if (label) label.textContent = templateName;

  // Scroll to editor
  textarea.scrollIntoView({ behavior: "smooth", block: "center" });
  textarea.focus();
});

// ── Save as new template ──
document.addEventListener("click", (event) => {
  const saveAsBtn = event.target.closest("[data-save-as-template]");
  if (!saveAsBtn) return;

  const textarea = document.querySelector("[data-template-textarea]");
  if (!textarea || !textarea.value.trim()) {
    alert("模板内容为空，无法保存。");
    return;
  }

  const name = prompt("请输入新模板名称：");
  if (!name || !name.trim()) return;

  // Create a temporary form to submit
  const form = document.createElement("form");
  form.method = "post";
  form.action = "/reminder-templates";
  form.innerHTML = `
    <input type="hidden" name="name" value="${name.trim()}">
    <input type="hidden" name="template_content">
  `;
  // Cache current textarea before page reload
  try {
    sessionStorage.setItem("qwbot_template_cache", JSON.stringify({
      content: textarea.value,
      templateId: "",
      templateName: "",
    }));
  } catch (_) {}

  form.querySelector("[name='template_content']").value = textarea.value;
  document.body.appendChild(form);
  form.submit();
});

// ── Pinyin auto-conversion for var_name ──
(function () {
  // Convert Chinese text to pinyin snake_case using pinyin-pro if available
  function toPinyinSnakeCase(str) {
    if (!str) return "";

    // Check if pinyin-pro is available
    if (typeof pinyinPro !== "undefined" && pinyinPro.pinyin) {
      // Check if string contains Chinese characters
      if (/[\u4e00-\u9fff]/.test(str)) {
        try {
          const pinyinResult = pinyinPro.pinyin(str, { toneType: "none", type: "array" });
          return pinyinResult
            .join("_")
            .toLowerCase()
            .replace(/[^a-z0-9_]/g, "")
            .replace(/_+/g, "_")
            .replace(/^_|_$/g, "");
        } catch (_) {}
      }
    }

    // Fallback: basic snake_case for non-Chinese text
    return str
      .toLowerCase()
      .replace(/[\s\-\.]+/g, "_")
      .replace(/[^a-z0-9_]/g, "")
      .replace(/_+/g, "_")
      .replace(/^_|_$/g, "");
  }

  // Auto-fill var_name only when saving (form submit), avoiding IME timing issues
  document.addEventListener("submit", (event) => {
    const form = event.target;
    const varNameInput = form.querySelector("[data-auto-varname]");
    const varLabelInput = form.querySelector("[data-var-label-source]");

    if (!varNameInput || !varLabelInput) return;

    // Auto-fill if var_name is empty
    if (!varNameInput.value.trim()) {
      const label = varLabelInput.value.trim();
      if (label) {
        const generated = toPinyinSnakeCase(label);
        if (generated) {
          varNameInput.value = generated + "_doc_link";
        } else {
          // pinyinPro not available or conversion failed, block and ask user to fill manually
          event.preventDefault();
          varNameInput.focus();
          alert("无法自动生成变量名，请手动填写（如：jin_du_tong_ji_doc_link）");
          return;
        }
      }
    }

    // Block if still empty after auto-fill attempt
    if (!varNameInput.value.trim()) {
      event.preventDefault();
      varNameInput.focus();
      alert("请输入变量名，或先填写描述后自动生成");
    }
  });

  // Auto-format var_name as user types (sanitize to snake_case)
  document.addEventListener("input", (event) => {
    const target = event.target;
    if (!target.matches("[data-auto-varname]")) return;

    const cursorPos = target.selectionStart;
    const before = target.value;
    const after = before
      .toLowerCase()
      .replace(/[\s\-\.]+/g, "_")
      .replace(/[^a-z0-9_\u4e00-\u9fff]/g, "")
      .replace(/_+/g, "_");
    if (before !== after) {
      target.value = after;
      target.selectionStart = target.selectionEnd = Math.min(cursorPos, after.length);
    }
  });
})();

// ── Style snippet insertion ──
document.addEventListener("click", (event) => {
  const snippetBtn = event.target.closest("[data-insert-snippet]");
  if (!snippetBtn) return;

  const snippet = snippetBtn.dataset.insertSnippet;
  const textarea = document.querySelector("[data-template-textarea]") || document.querySelector(".template-textarea");
  if (!textarea) return;

  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const text = textarea.value;

  textarea.value = text.slice(0, start) + snippet + text.slice(end);
  textarea.selectionStart = textarea.selectionEnd = start + snippet.length;
  textarea.focus();
});

// ── Document link variable insertion in notification modal ──
document.addEventListener("click", (event) => {
  const varBtn = event.target.closest("[data-insert-doc-var]");
  if (!varBtn) return;

  const varName = varBtn.dataset.insertDocVar;
  const varLabel = varBtn.dataset.varLabel || "";
  const editor = varBtn.closest("[data-doc-link-editor]");
  if (!editor) return;

  const list = editor.querySelector("[data-doc-link-list]");
  if (!list) return;

  // Check if there's an empty row
  const rows = list.querySelectorAll(".doc-link-row");
  let targetRow = null;
  for (const row of rows) {
    const labelInput = row.querySelector("input[name='doc_label']");
    const urlInput = row.querySelector("input[name='doc_url']");
    if (labelInput && urlInput && !labelInput.value && !urlInput.value) {
      targetRow = row;
      break;
    }
  }

  // If no empty row, add a new one
  if (!targetRow) {
    list.insertAdjacentHTML("beforeend", docLinkRowTemplate());
    targetRow = list.querySelector(".doc-link-row:last-child");
  }

  if (targetRow) {
    const labelInput = targetRow.querySelector("input[name='doc_label']");
    const urlInput = targetRow.querySelector("input[name='doc_url']");
    if (labelInput && !labelInput.value && varLabel) {
      labelInput.value = varLabel;
    }
    if (urlInput) {
      urlInput.value = "{" + varName + "}";
      urlInput.focus();
    }
  }
});

// ── Document Link Variable Modal Search ──
document.addEventListener("input", (event) => {
  if (event.target.id === "doc-link-var-search") {
    const searchTerm = event.target.value.toLowerCase();
    const allRows = document.querySelectorAll("#all-doc-link-var-list .doc-link-var-table-row");
    const noResults = document.getElementById("doc-link-var-no-results");
    
    let visibleCount = 0;
    
    allRows.forEach(row => {
      const varName = row.dataset.varName || "";
      const varLabel = row.dataset.varLabel || "";
      
      if (varName.includes(searchTerm) || varLabel.includes(searchTerm)) {
        row.style.display = "";
        visibleCount++;
      } else {
        row.style.display = "none";
      }
    });
    
    if (noResults) {
      noResults.style.display = visibleCount === 0 ? "" : "none";
    }
  }
});

// Template Preview Handler
(function() {
  document.addEventListener("click", (event) => {
    if (event.target.matches("[data-preview-template]")) {
      event.preventDefault();
      
      const textarea = document.querySelector("[data-template-textarea]");
      if (!textarea) return;
      
      const template = textarea.value;
      const rendered = renderWeComMarkdown(template);
      
      const previewContent = document.querySelector("#template-preview-content");
      if (previewContent) {
        previewContent.innerHTML = rendered;
        
        const modal = document.querySelector("#template-preview-modal");
        if (modal) {
          modal.showModal();
        }
      }
    }
  });
  
  function renderWeComMarkdown(text) {
    // Replace template variables with sample values
    const sampleValues = {
      "title_date": "01月15日",
      "progress_doc_url": "[进度文档](https://example.com/progress)",
      "case_assignment_doc_url": "[用例分配](https://example.com/cases)",
      "batch_register_doc_url": "[批次登记](https://example.com/batch)",
      "agenda_doc_url": "[议程安排](https://example.com/agenda)",
      "frontend_url": "[前端登记](https://example.com/frontend)"
    };
    
    let processed = text;
    for (const [key, value] of Object.entries(sampleValues)) {
      const regex = new RegExp("\\{" + key + "\\}", "g");
      processed = processed.replace(regex, value);
    }
    
    // Convert markdown-like syntax to HTML (WeChat Work style)
    const lines = processed.split("\n");
    const htmlLines = [];
    let inList = false;
    let listType = "";
    
    for (let line of lines) {
      // Headers
      if (line.startsWith("## ")) {
        if (inList) { htmlLines.push(listType === "ul" ? "</ul>" : "</ol>"); inList = false; }
        htmlLines.push("<h2>" + inlineFormat(line.substring(3)) + "</h2>");
        continue;
      }
      
      if (line.startsWith("# ")) {
        if (inList) { htmlLines.push(listType === "ul" ? "</ul>" : "</ol>"); inList = false; }
        htmlLines.push("<h3>" + inlineFormat(line.substring(2)) + "</h3>");
        continue;
      }
      
      // Unordered list
      if (line.startsWith("- ") || line.startsWith("* ")) {
        if (!inList || listType !== "ul") {
          if (inList) htmlLines.push(listType === "ul" ? "</ul>" : "</ol>");
          htmlLines.push("<ul>");
          inList = true;
          listType = "ul";
        }
        htmlLines.push("<li>" + inlineFormat(line.substring(2)) + "</li>");
        continue;
      }
      
      // Ordered list
      const olMatch = line.match(/^(\d+)\.\s+(.*)/);
      if (olMatch) {
        if (!inList || listType !== "ol") {
          if (inList) htmlLines.push(listType === "ul" ? "</ul>" : "</ol>");
          htmlLines.push("<ol>");
          inList = true;
          listType = "ol";
        }
        htmlLines.push("<li>" + inlineFormat(olMatch[2]) + "</li>");
        continue;
      }
      
      // Empty line
      if (line.trim() === "") {
        if (inList) { htmlLines.push(listType === "ul" ? "</ul>" : "</ol>"); inList = false; }
        continue;
      }
      
      // Regular paragraph
      if (inList) { htmlLines.push(listType === "ul" ? "</ul>" : "</ol>"); inList = false; }
      htmlLines.push("<p>" + inlineFormat(line) + "</p>");
    }
    
    if (inList) {
      htmlLines.push(listType === "ul" ? "</ul>" : "</ol>");
    }
    
    return htmlLines.join("");
  }
  
  function inlineFormat(text) {
    // Bold **text**
    text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    
    // Italic *text*
    text = text.replace(/\*(.+?)\*/g, "<em>$1</em>");
    
    // Links [text](url)
    text = text.replace(/\[(.+?)\]\((.+?)\)/g, '<a href="$2" target="_blank">$1</a>');
    
    // Font color tags <font color="xxx">text</font>
    text = text.replace(/&lt;font color=&quot;(.+?)&quot;&gt;(.+?)&lt;\/font&gt;/g, '<font color="$1">$2</font>');
    
    // Also handle raw HTML font tags (in case they're not escaped)
    text = text.replace(/<font color="(.+?)">(.+?)<\/font>/g, '<font color="$1">$2</font>');
    
    return text;
  }
})();

// Test send handler
document.addEventListener("click", (event) => {
  if (!event.target.matches("[data-test-send]")) return;

  if (!confirm("确认向测试机器人发送当前模板消息？")) return;

  const textarea = document.querySelector("[data-template-textarea]");
  if (!textarea) return;

  const form = document.createElement("form");
  form.method = "post";
  form.action = "/reminder-template/test-send";
  form.innerHTML = '<input type="hidden" name="template">';
  form.querySelector("input[name='template']").value = textarea.value;
  document.body.appendChild(form);
  form.submit();
});
