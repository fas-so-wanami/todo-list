(function () {
  "use strict";

  const storageKey = "todo.taskSort";
  const validFields = new Set(["progress", "due", "importance", "urgency", "title"]);
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const collator = new Intl.Collator("ja", { numeric: true, sensitivity: "base" });
  const defaults = { field: "due", direction: "asc" };
  let currentSetting = readSetting();
  let storageWarningShown = false;

  function parseSetting(value) {
    try {
      const setting = typeof value === "string" ? JSON.parse(value) : value;
      if (setting && validFields.has(setting.field) && ["asc", "desc"].includes(setting.direction)) {
        return { field: setting.field, direction: setting.direction };
      }
    } catch (error) {
      return defaults;
    }
    return defaults;
  }

  function readSetting() {
    try {
      return parseSetting(localStorage.getItem(storageKey));
    } catch (error) {
      return defaults;
    }
  }

  function showStorageWarning() {
    if (storageWarningShown) return;
    storageWarningShown = true;
    const alertBox = document.getElementById("connection-alert");
    if (alertBox) {
      alertBox.textContent = "並び順を保存できませんでした。今回は変更できますが、次回は期限の近い順に戻ります。";
      alertBox.hidden = false;
    }
  }

  function saveSetting(setting) {
    try {
      localStorage.setItem(storageKey, JSON.stringify(setting));
    } catch (error) {
      showStorageWarning();
    }
  }

  function description(setting) {
    const descriptions = {
      progress: setting.direction === "asc" ? "進捗が低い順" : "進捗が高い順",
      due: setting.direction === "asc" ? "期限が近い順" : "期限が遠い順",
      importance: setting.direction === "asc" ? "重要度が低い順" : "重要度が高い順",
      urgency: setting.direction === "asc" ? "緊急性が低い順" : "緊急性が高い順",
      title: setting.direction === "asc" ? "タスク名の文字順" : "タスク名の逆順"
    };
    return descriptions[setting.field];
  }

  function updateButtons(setting) {
    document.querySelectorAll("[data-task-sort]").forEach(function (button) {
      const field = button.dataset.taskSort;
      const active = field === setting.field;
      const label = button.firstElementChild.textContent.trim();
      button.setAttribute("aria-pressed", String(active));
      button.setAttribute("aria-label", active
        ? label + "で並べ替え。現在は" + description(setting)
        : label + "で昇順に並べ替え");
      button.title = button.getAttribute("aria-label");
      button.querySelector(".task-sort-indicator").textContent = active
        ? (setting.direction === "asc" ? "↑" : "↓")
        : "↕";
    });
  }

  function compareRows(left, right, setting) {
    const waitingDifference = Number(left.dataset.sortWaiting) - Number(right.dataset.sortWaiting);
    if (waitingDifference) return waitingDifference;

    let comparison = 0;
    if (setting.field === "due") {
      const leftDue = left.dataset.sortDue;
      const rightDue = right.dataset.sortDue;
      if (!leftDue || !rightDue) {
        if (!leftDue && rightDue) return 1;
        if (leftDue && !rightDue) return -1;
      } else {
        comparison = leftDue < rightDue ? -1 : leftDue > rightDue ? 1 : 0;
      }
    } else if (setting.field === "title") {
      comparison = collator.compare(left.dataset.sortTitle, right.dataset.sortTitle);
    } else {
      const attribute = "sort" + setting.field[0].toUpperCase() + setting.field.slice(1);
      const leftValue = Number(left.dataset[attribute]);
      const rightValue = Number(right.dataset[attribute]);
      comparison = leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0;
    }

    if (comparison && setting.direction === "desc") comparison *= -1;
    return comparison || Number(left.dataset.taskId) - Number(right.dataset.taskId);
  }

  function sortList(list, setting) {
    const taskRows = Array.from(list.children).filter(function (row) {
      return row.hasAttribute("data-task-id");
    });
    if (taskRows.length < 2) return;

    const rowsById = new Map(taskRows.map(function (row) { return [row.dataset.taskId, row]; }));
    const childrenByParent = new Map();
    const roots = [];
    taskRows.forEach(function (row) {
      const parentId = row.dataset.parentId;
      if (parentId && rowsById.has(parentId)) {
        if (!childrenByParent.has(parentId)) childrenByParent.set(parentId, []);
        childrenByParent.get(parentId).push(row);
      } else {
        roots.push(row);
      }
    });

    roots.sort(function (left, right) { return compareRows(left, right, setting); });
    const orderedRows = [];
    roots.forEach(function (root) {
      orderedRows.push(root);
      const children = (childrenByParent.get(root.dataset.taskId) || [])
        .sort(function (left, right) { return compareRows(left, right, setting); });
      children.forEach(function (child) {
        child.classList.remove("last-child", "visible-last-child");
      });
      if (children.length) children[children.length - 1].classList.add("last-child");
      const visibleChildren = children.filter(function (child) { return !child.hidden; });
      if (visibleChildren.length) visibleChildren[visibleChildren.length - 1].classList.add("visible-last-child");
      orderedRows.push.apply(orderedRows, children);
    });

    const fragment = document.createDocumentFragment();
    orderedRows.forEach(function (row) { fragment.append(row); });
    Array.from(list.children).filter(function (row) {
      return !row.hasAttribute("data-task-id");
    }).forEach(function (row) { fragment.append(row); });
    list.append(fragment);
  }

  function applySetting(setting, animate) {
    currentSetting = setting;
    updateButtons(setting);
    const change = function () {
      document.querySelectorAll("#main-content .task-list, #main-content .mini-list").forEach(function (list) {
        sortList(list, setting);
      });
    };
    if (animate && document.startViewTransition && !motion.matches) {
      document.startViewTransition(change).finished.catch(function () {});
    } else {
      change();
    }
  }

  document.body.addEventListener("click", function (event) {
    const button = event.target.closest("[data-task-sort]");
    if (!button || document.documentElement.hasAttribute("data-view-paused")) return;
    const field = button.dataset.taskSort;
    const direction = currentSetting.field === field && currentSetting.direction === "asc" ? "desc" : "asc";
    const setting = { field: field, direction: direction };
    saveSetting(setting);
    applySetting(setting, true);
  });

  window.addEventListener("storage", function (event) {
    if (event.key === storageKey || event.key === null) {
      applySetting(parseSetting(event.newValue), true);
    }
  });

  document.body.addEventListener("htmx:afterSettle", function () {
    applySetting(currentSetting, false);
  });

  applySetting(currentSetting, false);
})();