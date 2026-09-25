/* CRSW web deposit form.
   The server is the authority on every rule; this script only previews,
   pre-flags noise using the rules the server embeds in the page, and
   drives the three calls: POST /deposits, PUT each file, POST finalise. */
(function () {
  "use strict";
  var RULES = JSON.parse(document.getElementById("rules").textContent);
  var form = document.getElementById("deposit-form");
  var $ = function (id) { return document.getElementById(id); };
  var files = [];          // {file, member, noise, include, row}

  // ----- deposit slip preview ---------------------------------------
  function slug(s) {
    return (s || "").toLowerCase().replace(/ /g, "-").replace(/[^a-z0-9-]/g, "")
      .replace(/-{2,}/g, "-").replace(/^-+|-+$/g, "");
  }
  function val(name) {
    var el = form.elements[name];
    if (!el) return "";
    if (el.length !== undefined && el[0] && el[0].type === "radio") {
      for (var i = 0; i < el.length; i++) if (el[i].checked) return el[i].value;
      return "";
    }
    return el.value.trim();
  }
  function part(v, ph) { return v ? v : '<span class="ph">' + ph + "</span>"; }
  function updateSlip() {
    var ds = slug(val("dataset"));
    $("path-preview").innerHTML = [
      part(val("strand"), "strand"), part(slug(val("project")), "project"),
      part(val("sensitivity"), "sensitivity"), part(val("state"), "state"),
      part(ds, "dataset")].join("/") + "/";
    $("record-preview").innerHTML = "dataset." + part(ds, "dataset") + ".json";
    var sel = $("domain");
    var opt = sel.options[sel.selectedIndex];
    var steward = opt && opt.dataset.steward && opt.dataset.steward !== "TBC" ? opt.dataset.steward : "";
    $("slip-steward").textContent = steward || (opt && opt.value ? "to be confirmed" : "set by the domain");
    $("steward-hint").textContent = steward ? "Steward: " + steward : "The steward is set by the domain.";
    var lic = $("license");
    if (!lic.value) lic.placeholder = val("sensitivity") === "amber" ? "internal-only" : "CC-BY-4.0";
    var kept = files.filter(function (f) { return !f.noise || f.include; });
    $("slip-files").textContent = kept.length ? kept.length + " file" + (kept.length === 1 ? "" : "s") + ", " + human(kept.reduce(function (n, f) { return n + f.file.size; }, 0)) : "none yet";
  }
  form.addEventListener("input", updateSlip);
  form.addEventListener("change", updateSlip);
  // A derived dataset should say what it came from: open the origin
  // section when that source type is chosen.
  $("source_type").addEventListener("change", function () {
    if (this.value === "derived") $("origin").open = true;
  });
  $("abstract").addEventListener("input", function () {
    var n = this.value.trim() ? this.value.trim().split(/\s+/).length : 0;
    $("wordcount").textContent = n + " word" + (n === 1 ? "" : "s");
  });

  // ----- files --------------------------------------------------------
  function human(n) {
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }
  function isNoise(member) {
    var parts = member.split("/"), name = parts[parts.length - 1];
    if (RULES.noise_file_names.indexOf(name) >= 0) return true;
    for (var i = 0; i < RULES.noise_file_prefixes.length; i++) if (name.indexOf(RULES.noise_file_prefixes[i]) === 0) return true;
    for (var j = 0; j < parts.length - 1; j++) if (RULES.noise_dir_names.indexOf(parts[j]) >= 0) return true;
    return false;
  }
  function memberFor(file, fromFolder) {
    var rel = file.webkitRelativePath || file.name;
    if (fromFolder && rel.indexOf("/") > 0) rel = rel.slice(rel.indexOf("/") + 1); // the folder's own name is never part of the key
    return rel.normalize ? rel.normalize("NFC") : rel;
  }
  function addFiles(list, fromFolder) {
    var seen = {};
    files.forEach(function (f) { seen[f.member] = true; });
    var dropped = 0;
    Array.prototype.forEach.call(list, function (file) {
      var member = memberFor(file, fromFolder);
      if (seen[member]) { dropped++; return; }
      seen[member] = true;
      files.push({ file: file, member: member, noise: isNoise(member), include: false });
    });
    files.sort(function (a, b) { return a.member < b.member ? -1 : a.member > b.member ? 1 : 0; });
    renderFiles(dropped);
  }
  function renderFiles(dropped) {
    var tbody = $("files").querySelector("tbody");
    tbody.innerHTML = "";
    var noise = 0, total = 0;
    files.forEach(function (f) {
      var tr = document.createElement("tr");
      if (f.noise) { tr.className = "noise"; noise++; }
      var td1 = document.createElement("td");
      td1.textContent = f.member;
      if (f.noise) {
        var lab = document.createElement("label");
        var cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = f.include;
        cb.addEventListener("change", function () { f.include = cb.checked; updateSlip(); });
        lab.appendChild(cb); lab.appendChild(document.createTextNode(" include anyway"));
        td1.appendChild(lab);
      }
      var td2 = document.createElement("td"); td2.className = "num"; td2.textContent = human(f.file.size);
      var td3 = document.createElement("td");
      td3.innerHTML = '<span class="bar"><i></i></span><span class="state"></span>';
      tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3);
      tbody.appendChild(tr);
      f.row = tr;
      if (!f.noise || f.include) total += f.file.size;
    });
    $("files").hidden = !files.length;
    $("files-empty").hidden = !!files.length;
    $("clear-files").hidden = !files.length;
    $("files-summary").textContent = files.length + " file" + (files.length === 1 ? "" : "s") + (noise ? ", " + noise + " excluded as OS noise (tick to include)" : "");
    $("files-total").textContent = human(total);
    var note = $("picker-note");
    note.hidden = !dropped;
    if (dropped) note.textContent = dropped + " already in the list, skipped.";
    updateSlip();
  }
  $("pick-files").addEventListener("change", function () { addFiles(this.files, false); this.value = ""; });
  $("pick-folder").addEventListener("change", function () { addFiles(this.files, true); this.value = ""; });
  $("clear-files").addEventListener("click", function () { files = []; renderFiles(0); });

  // ----- errors ---------------------------------------------------------
  function clearErrors() {
    form.querySelectorAll(".field").forEach(function (f) {
      f.classList.remove("invalid");
      var e = f.querySelector(".error"); e.hidden = true; e.textContent = "";
    });
  }
  function showErrors(errors) {
    var first = null;
    Object.keys(errors).forEach(function (name) {
      var f = form.querySelector('.field[data-field="' + name + '"]');
      if (!f) return;
      var d = f.closest("details");
      if (d) d.open = true;
      f.classList.add("invalid");
      var e = f.querySelector(".error"); e.hidden = false; e.textContent = errors[name];
      if (!first) first = f;
    });
    if (first) first.scrollIntoView({ block: "center" });
  }
  function setStatus(text) { $("status").textContent = text; }

  // ----- the three calls ------------------------------------------------
  function metadata() {
    var m = {};
    ["strand", "sensitivity", "state", "project", "dataset", "domain", "version",
     "coverage_start", "coverage_end", "abstract", "license", "creator",
     "source_type", "source_detail", "derived_from", "provenance_activity",
     "provenance_tool", "provenance_repo", "provenance_commit",
     "provenance_description"].forEach(function (k) { m[k] = val(k); });
    m.subject = Array.prototype.map.call(form.querySelectorAll('input[name=subject]:checked'), function (c) { return c.value; });
    return m;
  }
  function putFile(depositId, f) {
    return new Promise(function (resolve, reject) {
      var url = "/deposits/" + depositId + "/files/" + f.member.split("/").map(encodeURIComponent).join("/");
      if (f.noise) url += "?include_noise=1";
      var xhr = new XMLHttpRequest();
      var bar = f.row.querySelector(".bar > i"), state = f.row.querySelector(".state");
      xhr.upload.addEventListener("progress", function (ev) {
        if (ev.lengthComputable) bar.style.width = Math.round(100 * ev.loaded / ev.total) + "%";
      });
      xhr.addEventListener("load", function () {
        var body; try { body = JSON.parse(xhr.responseText); } catch (e) { body = {}; }
        if (xhr.status === 200) {
          bar.style.width = "100%"; f.row.classList.add("done");
          state.textContent = "stored, " + body.bytes + " bytes";
          resolve(body);
        } else {
          f.row.classList.add("failed");
          state.textContent = (body.detail && (body.detail.problems || body.detail)) || ("failed (" + xhr.status + ")");
          reject(new Error(state.textContent));
        }
      });
      xhr.addEventListener("error", function () { f.row.classList.add("failed"); state.textContent = "connection lost"; reject(new Error("connection lost")); });
      xhr.open("PUT", url);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.send(f.file);
    });
  }
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : null })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, status: r.status, body: j }; }); });
  }
  function fail(msg) {
    setStatus("");
    var box = $("result"); box.hidden = false; box.className = "result failed";
    box.innerHTML = "<h3>Not deposited</h3><p></p>";
    box.querySelector("p").textContent = msg;
    $("submit").disabled = false;
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    clearErrors();
    $("result").hidden = true; $("warnings").hidden = true;
    var toSend = files.filter(function (f) { return !f.noise || f.include; });
    if (!toSend.length) { fail("Choose at least one file first."); return; }
    $("submit").disabled = true;
    setStatus("Checking the description…");
    post("/deposits", metadata()).then(function (r) {
      if (!r.ok) {
        if (r.status === 422 && r.body.detail && r.body.detail.errors) {
          showErrors(r.body.detail.errors); fail("Some fields need attention; see the messages next to them.");
        } else fail(r.body.detail || ("The service refused the request (" + r.status + ")."));
        return;
      }
      var dep = r.body;
      if (dep.warnings && dep.warnings.length) {
        var ul = $("warnings"); ul.hidden = false; ul.innerHTML = "";
        dep.warnings.forEach(function (w) { var li = document.createElement("li"); li.textContent = w; ul.appendChild(li); });
      }
      var i = 0;
      function next() {
        if (i >= toSend.length) return Promise.resolve();
        var f = toSend[i++];
        setStatus("Uploading " + i + " of " + toSend.length + ": " + f.member);
        return putFile(dep.id, f).then(next);
      }
      return next().then(function () {
        setStatus("Writing the dataset record…");
        return post("/deposits/" + dep.id + "/finalise");
      }).then(function (r) {
        if (!r.ok) {
          var d = r.body.detail;
          fail(typeof d === "string" ? d : JSON.stringify(d));
          return;
        }
        setStatus("");
        var box = $("result"); box.hidden = false; box.className = "result";
        box.innerHTML = "<h3>Deposited</h3><p>" + r.body.files + " file(s) and the dataset record are in the holding area.</p>" +
          "<p>Record: <code></code></p><p><a></a></p>";
        box.querySelector("code").textContent = r.body.record_key;
        var a = box.querySelector("a"); a.href = "/deposits/" + dep.id + "/summary"; a.textContent = "View the deposit summary";
        form.querySelectorAll("input, select, textarea, button").forEach(function (el) { el.disabled = true; });
      });
    }).catch(function (e) { fail(e.message || String(e)); });
  });

  updateSlip();
})();
