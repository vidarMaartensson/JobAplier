// Kopiera-knappar: <button type="button" data-copy-target="id"> kopierar fältet med det id:t.
// Utan JavaScript (eller utan urklippsstöd) är fälten vanliga markerbara textrutor.
(function () {
  "use strict";

  function flash(button, text) {
    if (!button.dataset.copyLabel) {
      button.dataset.copyLabel = button.textContent;
    }
    button.textContent = text;
    clearTimeout(button.copyTimer);
    button.copyTimer = setTimeout(function () {
      button.textContent = button.dataset.copyLabel;
    }, 2000);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy-target]");
    if (!button) {
      return;
    }
    var target = document.getElementById(button.dataset.copyTarget);
    if (!target) {
      return;
    }
    var text = target.value || target.textContent;
    var fallback = function () {
      // Markera texten så att den går att kopiera för hand.
      if (typeof target.select === "function") {
        target.select();
      }
      flash(button, "Markerat – kopiera med ⌘C/Ctrl+C");
    };
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      fallback();
      return;
    }
    navigator.clipboard.writeText(text).then(function () {
      flash(button, "Kopierat!");
    }, fallback);
  });
})();
