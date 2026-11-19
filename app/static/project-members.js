(function () {
  "use strict";

  function updateSharedInvitationArea(form) {
    const checkbox = form.querySelector("[data-shared-checkbox]");
    const area = form.querySelector("[data-shared-invite-area]");
    const options = area && area.querySelector("[data-invite-options]");
    if (!checkbox || !area) return;
    area.hidden = !checkbox.checked;
    if (options) options.disabled = !checkbox.checked;
  }

  document.body.addEventListener("change", function (event) {
    if (!event.target.matches("[data-shared-checkbox]")) return;
    updateSharedInvitationArea(event.target.closest("form"));
  });

  document.body.addEventListener("input", function (event) {
    const search = event.target.closest("[data-invite-search]");
    if (!search) return;
    const picker = search.closest("[data-invite-picker]");
    const query = search.value.trim().toLocaleLowerCase();
    picker.querySelectorAll("[data-invite-options] option").forEach(function (option) {
      const matches = option.textContent.toLocaleLowerCase().includes(query);
      option.hidden = !matches && !option.selected;
    });
  });

  document.querySelectorAll("form").forEach(updateSharedInvitationArea);
})();
