/* CO2.js per-visit estimate — Demo's standard rollout snippet.
   Requires co2.min.js (CO2.js v0.19.0, @tgwf/co2, SWD model v4) loaded first.
   Computes transferred bytes locally from the Performance API.
   No external calls, no tracking. Renders one line into the page footer:
   "This visit ≈ X g CO₂e · estimated with CO2.js (The Green Web Foundation)" */
(function () {
  function bytesTransferred() {
    var total = 0;
    try {
      var nav = performance.getEntriesByType("navigation")[0];
      if (nav) total += nav.transferSize || nav.encodedBodySize || 0;
      var entries = performance.getEntriesByType("resource");
      for (var i = 0; i < entries.length; i++) {
        total += entries[i].transferSize || entries[i].encodedBodySize || 0;
      }
    } catch (e) {}
    return total;
  }

  function render() {
    if (!window.CO2) return false;
    var bytes = bytesTransferred();
    if (!bytes) return false;
    var grams = new window.CO2({ model: "swd", version: 4 }).perByte(bytes);
    var el = document.querySelector("[data-co2-estimate]");
    if (!el) {
      el = document.createElement("p");
      el.setAttribute("data-co2-estimate", "");
      el.className = "co2-estimate";
      el.style.cssText =
        "font-size:0.8em;opacity:0.75;text-align:center;margin:0.75rem 0 0;";
      var footer = document.querySelector("footer");
      if (footer) footer.appendChild(el);
      else document.body.appendChild(el);
    }
    el.textContent =
      "This visit \u2248 " +
      grams.toFixed(2) +
      " g CO\u2082e \u00B7 estimated with CO2.js (The Green Web Foundation)";
    return true;
  }

  // SPAs render their footer after load: retry briefly until render succeeds.
  function attempt(tries) {
    if (render()) return;
    if (tries > 0) setTimeout(function () { attempt(tries - 1); }, 600);
  }

  if (document.readyState === "complete") attempt(8);
  else
    window.addEventListener("load", function () {
      attempt(8);
    });
})();
