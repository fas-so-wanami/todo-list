(function () {
  "use strict";

  const view = document.body.classList.contains("mini-mode") ? "mini" : "board";
  const storagePrefix = "todo.childVisibility." + view + ".";
  const choices = new Map();
  const storageFailures = new Set();
  const validModes = new Set(["all", "done", "none"]);
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const feedbackRows = new WeakSet();

  function readMode(parentId) {
    if (!choices.has(parentId)) {
      let savedMode = null;
      try {
        savedMode = localStorage.getItem(storagePrefix + parentId);
      } catch (error) {
        storageFailures.add(parentId);
      }
      choices.set(parentId, validModes.has(savedMode) ? savedMode : "done");
    }
    return choices.get(parentId);
  }

  function animateChange(change) {
    if (document.startViewTransition && !motion.matches) {
      const transition = document.startViewTransition(change);
      transition.finished.catch(function () {});
    } else {
      change();
    }
  }

  function applyVisibility() {
    document.querySelectorAll("[data-child-controls]").forEach(function (controls) {
      const parentId = controls.dataset.childControls;
      const mode = readMode(parentId);
      const parent = controls.closest("[data-task-id]");
      const children = Array.from(parent.parentElement.children).filter(function (row) {
        return row.dataset.parentId === parentId;
      });
      let hiddenCount = 0;
      let consultationCount = 0;
      let overdueCount = 0;
      const visibleChildren = [];

      children.forEach(function (row) {
        const done = row.classList.contains("status-done");
        const keepFeedback = row.dataset.visibilityFeedback === "pending";
        const hasRejectedDraft = Boolean(row.querySelector("[data-unsaved-draft]"));
        const hidden = (mode === "none" || (mode === "done" && done)) && !keepFeedback && !hasRejectedDraft;
        if (hidden && row.contains(document.activeElement)) {
          controls.querySelector('[data-child-mode="' + mode + '"]').focus();
        }
        row.hidden = hidden;
        row.classList.remove("visible-last-child");
        if (hidden) {
          hiddenCount += 1;
          if (!done && row.classList.contains("blocked")) consultationCount += 1;
          if (row.dataset.childOverdue === "true") overdueCount += 1;
        } else {
          visibleChildren.push(row);
        }
      });
      if (visibleChildren.length) visibleChildren[visibleChildren.length - 1].classList.add("visible-last-child");
      parent.dataset.childrenVisible = String(visibleChildren.length > 0);
      controls.querySelectorAll("[data-child-mode]").forEach(function (button) {
        button.setAttribute("aria-pressed", String(button.dataset.childMode === mode));
      });
      controls.querySelector("[data-child-hidden-count]").textContent = hiddenCount + "件収納中";
      const alerts = controls.querySelector("[data-child-alerts]");
      alerts.replaceChildren();
      if (consultationCount) {
        const consultation = document.createElement("span");
        consultation.className = "child-consultation-count";
        consultation.textContent = "相談 " + consultationCount + "件";
        alerts.append(consultation);
      }
      if (overdueCount) {
        const overdue = document.createElement("span");
        overdue.className = "child-overdue-count";
        overdue.textContent = "超過 " + overdueCount + "件";
        alerts.append(overdue);
      }
      const notice = controls.querySelector("[data-child-storage-notice]");
      notice.hidden = !storageFailures.has(parentId);
      notice.textContent = notice.hidden ? "" : "表示の選択を保存できません。次回は初期表示に戻ります。";
    });
  }

  function applyTransitionNames() {
    document.querySelectorAll("[data-view-transition-name]").forEach(function (row) {
      row.style.viewTransitionName = row.dataset.viewTransitionName;
    });
  }

  function restoreVisibility() {
    applyTransitionNames();
    document.querySelectorAll("[data-parent-id].just-completed").forEach(function (row) {
      if (!row.dataset.parentId || feedbackRows.has(row)) return;
      feedbackRows.add(row);
      if (motion.matches || document.documentElement.dataset.completionEffects === "off") return;
      row.dataset.visibilityFeedback = "pending";
      window.setTimeout(function () {
        delete row.dataset.visibilityFeedback;
        if (row.isConnected) animateChange(applyVisibility);
      }, 1100);
    });
    applyVisibility();
  }

  document.body.addEventListener("click", function (event) {
    const button = event.target.closest("[data-child-mode]");
    if (!button || document.documentElement.hasAttribute("data-view-paused")) return;
    const parentId = button.closest("[data-child-controls]").dataset.childControls;
    const mode = button.dataset.childMode;
    choices.set(parentId, mode);
    try {
      localStorage.setItem(storagePrefix + parentId, mode);
      storageFailures.delete(parentId);
    } catch (error) {
      storageFailures.add(parentId);
    }
    animateChange(applyVisibility);
  });

  window.addEventListener("storage", function (event) {
    if (event.key === null || event.key.startsWith(storagePrefix)) {
      choices.clear();
      animateChange(applyVisibility);
    }
  });
  document.body.addEventListener("htmx:afterSwap", applyTransitionNames);
  document.body.addEventListener("htmx:afterSettle", restoreVisibility);
  restoreVisibility();
})();