(function () {
  const origin = window.location.origin;
  const kind = document.body.classList.contains("mini-mode") ? "mini" : "full";
  const panel = document.getElementById("view-handoff");
  const message = document.getElementById("view-handoff-message");
  const returnButton = document.getElementById("view-handoff-return");
  const requests = new Set();
  const reportSnapshots = new WeakMap();
  let reportForm = document.getElementById("report-form");
  let savedReport = reportForm ? formSnapshot(reportForm) : null;
  let pending = null;
  let destination = null;

  function formSnapshot(form) {
    return JSON.stringify(Array.from(new FormData(form).entries()));
  }

  function hasDraft() {
    const report = document.getElementById("report-form");
    if (report && (formSnapshot(report) !== savedReport || document.querySelector("#report-summary .notice.error"))) return true;
    if (document.querySelector("[data-unsaved-draft]")) return true;
    return Array.from(document.querySelectorAll("input[name], textarea[name], select[name]")).some(function (field) {
      if (field.form === report || field.type === "hidden") return false;
      if (field.type === "checkbox" || field.type === "radio") return field.checked !== field.defaultChecked;
      if (field.tagName === "SELECT") {
        const initial = Array.from(field.options).find(function (option) { return option.defaultSelected; }) || field.options[0];
        return initial && field.value !== initial.value;
      }
      return field.value !== field.defaultValue;
    });
  }

  function showError(text) {
    const alert = document.getElementById("connection-alert");
    alert.textContent = text;
    alert.hidden = false;
  }

  function pause(text) {
    document.documentElement.dataset.viewPaused = "true";
    document.querySelector("main").inert = true;
    document.querySelector("header").inert = true;
    message.textContent = text;
    panel.hidden = false;
    returnButton.textContent = pending ? "切替をやめて戻る" : "この画面を再開する";
    window.dispatchEvent(new CustomEvent("todo-view-paused", { detail: true }));
  }

  function resume() {
    delete document.documentElement.dataset.viewPaused;
    document.querySelector("main").inert = false;
    document.querySelector("header").inert = false;
    panel.hidden = true;
    window.dispatchEvent(new CustomEvent("todo-view-paused", { detail: false }));
  }

  function clearPending() {
    window.clearInterval(pending.probe);
    window.clearTimeout(pending.timeout);
    pending = null;
  }

  function fail(text) {
    if (pending.created && !pending.target.closed) pending.target.close();
    clearPending();
    resume();
    showError(text);
  }

  function switchView(nextKind, preferredWindow) {
    if (pending) return;
    if (requests.size) {
      showError("保存・通信が終わってから、もう一度切り替えてください。");
      return;
    }
    if (hasDraft() && !window.confirm("保存していない入力があります。切り替えると書きかけは失われます。切り替えますか？")) return;
    let target = preferredWindow || (nextKind === "full" ? window.opener : null);
    if (!target || target.closed) target = window.open("", "todo-" + nextKind, nextKind === "mini" ? "width=400,height=680" : "width=1280,height=850");
    if (!target) {
      showError("別の表示を開けませんでした。ブラウザのポップアップを許可して、もう一度お試しください。");
      return;
    }
    let created;
    try {
      created = target.location.href === "about:blank";
      if (created) target.location.replace(nextKind === "mini" ? "/mini" : "/");
    } catch (error) {
      if (!(error instanceof DOMException)) throw error;
      console.warn("切り替え先の画面を確認できませんでした。", error);
      showError("切り替え先の画面を確認できませんでした。この画面は閉じずに残しています。");
      return;
    }
    destination = target;
    const token = window.crypto.randomUUID();
    pending = { target: target, created: created, token: token, kind: nextKind };
    pause("別の表示を準備しています。準備できたことを確認してから、この画面を閉じます。");
    function probe() {
      if (target.closed) {
        fail("切り替え先が閉じられたため、この画面に戻りました。");
        return;
      }
      target.postMessage({ type: "todo-view-probe", token: token, kind: nextKind }, origin);
    }
    pending.probe = window.setInterval(probe, 200);
    pending.timeout = window.setTimeout(function () { fail("切り替え先の準備を確認できませんでした。この画面は閉じずに残しています。"); }, 10000);
    target.focus();
    probe();
  }

  document.body.addEventListener("htmx:beforeRequest", function (event) {
    requests.add(event.detail.xhr);
    const form = event.detail.elt.closest("form");
    if (form && form.id === "report-form") reportSnapshots.set(event.detail.xhr, formSnapshot(form));
  });
  document.body.addEventListener("htmx:afterRequest", function (event) {
    requests.delete(event.detail.xhr);
    const snapshot = reportSnapshots.get(event.detail.xhr);
    if (snapshot && event.detail.successful && !document.querySelector("#report-summary .notice.error")) savedReport = snapshot;
    reportSnapshots.delete(event.detail.xhr);
  });
  document.body.addEventListener("htmx:afterSettle", function () {
    const current = document.getElementById("report-form");
    if (current !== reportForm) {
      reportForm = current;
      savedReport = current ? formSnapshot(current) : null;
    }
  });
  window.addEventListener("message", function (event) {
    if (event.origin !== origin || !event.source || event.source === window || !event.data) return;
    const data = event.data;
    if (data.type === "todo-view-probe" && data.kind === kind && typeof data.token === "string") {
      if (document.documentElement.dataset.viewPaused) {
        if (event.source !== destination || pending) return;
        const keepDraft = hasDraft();
        resume();
        if (!keepDraft) {
          window.location.reload();
          return;
        }
      }
      event.source.postMessage({ type: "todo-view-ready", token: data.token, kind: kind }, origin);
    } else if (data.type === "todo-view-ready" && pending && event.source === pending.target && data.token === pending.token && data.kind === pending.kind) {
      const label = pending.kind === "mini" ? "ミニ表示" : "全体表示";
      clearPending();
      pause(label + "を開きました。このタブ（窓）を閉じてください。この画面の自動更新は停止しています。");
      window.close();
    } else if (data.type === "todo-view-return" && (event.source === window.opener || event.source === destination)) {
      switchView(kind === "mini" ? "full" : "mini", event.source);
    }
  });
  returnButton.addEventListener("click", function () {
    if (pending) {
      if (pending.created && !pending.target.closed) pending.target.close();
      clearPending();
      resume();
    } else if (destination && !destination.closed) {
      destination.postMessage({ type: "todo-view-return" }, origin);
      destination.focus();
    } else {
      const keepDraft = hasDraft();
      resume();
      if (!keepDraft) window.location.reload();
    }
  });
  const miniButton = document.getElementById("open-mini");
  if (miniButton) miniButton.addEventListener("click", function () { switchView("mini"); });
  const fullButton = document.getElementById("back-to-full");
  if (fullButton) fullButton.addEventListener("click", function (event) {
    event.preventDefault();
    switchView("full");
  });
})();
