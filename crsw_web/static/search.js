// "Why this?" on a search result: ask the service for the chat model's
// one-sentence reading of what connects the passage to the question.
// Fetched only when clicked, so an ordinary search costs nothing extra.
(function () {
  "use strict";
  var q = document.getElementById("q");
  if (!q) return;
  document.querySelectorAll("button.why-ask").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var li = btn.closest("li"), said = li.querySelector(".why-said");
      btn.disabled = true;
      btn.textContent = "Asking\u2026";
      var params = new URLSearchParams({
        q: q.value, identifier: btn.dataset.identifier,
        member: btn.dataset.member, position: btn.dataset.position
      });
      fetch("/search/why?" + params.toString(), { credentials: "same-origin" })
        .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
        .then(function (res) {
          var text = res.ok && res.body.why ? res.body.why
                   : (res.body && (res.body.notice || res.body.detail)) || "No explanation just now.";
          said.textContent = "The model's reading: " + text;
          said.hidden = false;
          btn.hidden = true;
        })
        .catch(function () {
          said.textContent = "No explanation just now.";
          said.hidden = false;
          btn.disabled = false;
          btn.textContent = "Why this?";
        });
    });
  });
})();
