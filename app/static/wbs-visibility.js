(function () {
  "use strict";

  const sections = Array.from(document.querySelectorAll("[data-wbs-member-section]"));
  if (!sections.length) return;

  const storagePrefix = "todo.wbsMemberVisibility.";
  const notice = document.querySelector("[data-wbs-visibility-notice]");
  const sectionsByKey = new Map();
  const pendingStorageUpdates = new WeakSet();

  function storageKey(section) {
    return storagePrefix + section.dataset.projectId + "." + section.dataset.memberId;
  }

  function reportStorageError(error) {
    if (!(error instanceof DOMException)) throw error;
    if (notice) {
      notice.hidden = false;
      notice.textContent = "メンバーごとの開閉状態を保存できません。次回は全員分を開いて表示します。";
    }
    console.warn("WBSの開閉状態を保存できませんでした。", error);
  }

  function setOpen(section, value, fromStorage) {
    if (section.open === value) return;
    if (fromStorage) pendingStorageUpdates.add(section);
    section.open = value;
  }

  sections.forEach(function (section) {
    const key = storageKey(section);
    sectionsByKey.set(key, section);
    try {
      section.open = localStorage.getItem(key) !== "closed";
    } catch (error) {
      reportStorageError(error);
    }
    section.addEventListener("toggle", function () {
      if (pendingStorageUpdates.delete(section)) return;
      try {
        localStorage.setItem(key, section.open ? "open" : "closed");
      } catch (error) {
        reportStorageError(error);
      }
    });
  });

  window.addEventListener("storage", function (event) {
    if (event.key === null) {
      sections.forEach(function (section) {
        setOpen(section, true, true);
      });
      return;
    }
    const section = sectionsByKey.get(event.key);
    if (section) setOpen(section, event.newValue !== "closed", true);
  });
})();
