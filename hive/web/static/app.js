// Dashboard shell glue. No build step: a plain script, loaded after htmx.
(function () {
  "use strict";

  // Remember who is at the keyboard. Pages include `#me` (name="by") in the actions that need a human actor.
  var me = document.getElementById("me");
  if (me) {
    try {
      me.value = localStorage.getItem("hive.me") || "";
    } catch (e) {
      /* storage blocked: the field still works for this page load */
    }
    me.addEventListener("input", function () {
      try {
        localStorage.setItem("hive.me", me.value.trim());
      } catch (e) {
        /* ignore */
      }
    });
  }

  // htmx skips swapping 4xx/5xx responses. A target marked `data-swap-errors` opts in, so the server can
  // answer a failed action with the re-rendered fragment plus an error banner and still use a real status.
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail.target;
    if (event.detail.xhr.status >= 400 && target && target.hasAttribute("data-swap-errors")) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });
})();
