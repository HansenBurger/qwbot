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

// Several template editor modals can exist on one page, so always resolve the
// textarea inside the same form/dialog instead of using a global selector.
function scopedTemplateTextarea(element) {
  const scope = element?.closest("[data-template-form]") || element?.closest("dialog") || document;
  return (
    scope.querySelector("[data-template-textarea]") ||
    scope.querySelector(".template-textarea") ||
    document.querySelector("[data-template-textarea]")
  );
}

// Reminder template variable insertion
document.addEventListener("click", (event) => {
  const tag = event.target.closest("[data-insert-var]");
  if (!tag) return;

  const varName = tag.dataset.insertVar;
  const varLabel = tag.dataset.varLabel || "";
  const textarea = scopedTemplateTextarea(tag);
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
  const textarea = scopedTemplateTextarea(snippetBtn);
  if (!textarea) return;

  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const text = textarea.value;

  textarea.value = text.slice(0, start) + snippet + text.slice(end);
  textarea.selectionStart = textarea.selectionEnd = start + snippet.length;
  textarea.focus();
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

// Variable values for the template preview. The server renders the real ones
// into #template-preview-vars; the fallbacks only cover the case where the
// element is missing (e.g. the page was opened from a stale cache).
function previewVariables() {
  const fallback = {
    title_date: "01月15日",
    current_trading_date: "2026-01-14",
    next_trading_date: "2026-01-15",
    today_batch_start_times: "10:00、15:30",
    today_post_batch_date: "2026-01-16",
    today_batch_detail: "10:00 日终计提（2026-01-14 → 2026-01-15）\n15:30 月末（2026-01-15 → 2026-01-16）",
    progress_doc_link: "https://doc.weixin.qq.com/sheet/example-progress",
    case_assignment_doc_link: "https://doc.weixin.qq.com/sheet/example-cases",
    batch_register_doc_link: "https://doc.weixin.qq.com/sheet/example-batch",
    agenda_doc_link: "https://doc.weixin.qq.com/sheet/example-agenda",
    frontend_link: "http://127.0.0.1:5000",
  };
  const node = document.getElementById("template-preview-vars");
  if (!node) return fallback;
  try {
    return { ...fallback, ...JSON.parse(node.textContent || "{}") };
  } catch (_) {
    return fallback;
  }
}

// Template Preview Handler
(function() {
  document.addEventListener("click", (event) => {
    if (event.target.matches("[data-preview-template]")) {
      event.preventDefault();
      
      const textarea = scopedTemplateTextarea(event.target);
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
    let processed = text;
    for (const [key, value] of Object.entries(previewVariables())) {
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
