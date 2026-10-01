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

  // ----- the upstream-dataset lookup (read role only) -------------------
  // Searches /datasets.json and appends the chosen identifier as a new
  // line of the derived-from box, so the reference is found, not typed.
  (function () {
    var open = $("lookup-open"), close = $("lookup-close"), panel = $("lookup-panel"),
        q = $("lookup-q"), list = $("lookup-results"), box = $("derived_from");
    if (!open) return;
    var timer = null, seq = 0;
    function show(on) {
      panel.hidden = !on;
      open.setAttribute("aria-expanded", on ? "true" : "false");
      if (on) { q.focus(); search(); } else { open.focus(); }
    }
    function addLine(identifier) {
      var lines = box.value.split(/\r?\n/).filter(function (l) { return l.trim(); });
      if (lines.indexOf(identifier) < 0) lines.push(identifier);
      box.value = lines.join("\n") + "\n";
      box.dispatchEvent(new Event("input", { bubbles: true }));
      panel.hidden = true;
      open.setAttribute("aria-expanded", "false");
      box.focus();
    }
    function render(items) {
      list.textContent = "";
      if (!items.length) {
        var none = document.createElement("li"); none.className = "empty";
        none.textContent = q.value.trim() ? "Nothing matches." : "No datasets in the index yet.";
        list.appendChild(none);
        return;
      }
      items.forEach(function (d) {
        var li = document.createElement("li");
        var b = document.createElement("button"); b.type = "button";
        var name = document.createElement("strong"); name.textContent = d.dataset + " ";
        var tag = document.createElement("span"); tag.className = "tag sens " + d.sensitivity; tag.textContent = d.sensitivity;
        var id = document.createElement("code"); id.textContent = d.identifier;
        var ab = document.createElement("small"); ab.textContent = d.abstract || "";
        b.appendChild(name); b.appendChild(tag); b.appendChild(document.createElement("br"));
        b.appendChild(id); b.appendChild(document.createElement("br")); b.appendChild(ab);
        b.addEventListener("click", function () { addLine(d.identifier); });
        li.appendChild(b); list.appendChild(li);
      });
    }
    function search() {
      var mine = ++seq;
      fetch("/datasets.json?limit=20&q=" + encodeURIComponent(q.value.trim()), { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : { datasets: [] }; })
        .then(function (body) { if (mine === seq) render(body.datasets || []); })
        .catch(function () { if (mine === seq) render([]); });
    }
    open.addEventListener("click", function () { show(panel.hidden); });
    close.addEventListener("click", function () { show(false); });
    q.addEventListener("input", function () {
      clearTimeout(timer); timer = setTimeout(search, 200);
    });
    panel.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); show(false); }
    });
  })();

  // ----- the dataset this deposit lands in (read role only) -------------
  // As the researcher names the dataset, /deposits/lookup says whether it
  // exists, what is in it, a same-named dataset elsewhere in the project,
  // near-matches, and deposits still waiting for promotion. The panel
  // under the dataset field says what will happen before any upload.
  var dest = null;             // the last lookup's answer, or null
  var destination = (function () {
    var panel = $("destination");
    if (!panel) return { confirm: function () { return false; }, manifest: function () { return null; },
                         blocked: function () { return null; }, pending: function () { return false; } };
    var text = $("destination-text"), notes = $("destination-notes"),
        existing = $("destination-existing"), confirmBox = $("confirm-existing"),
        restricted = $("restricted"), restrictedNote = $("restricted-note"),
        projects = $("project-options"), datasets = $("dataset-options");
    var timer = null, seq = 0, rows = [], rowsStrand = "";

    function option(list, values) {
      list.textContent = "";
      values.forEach(function (v) { var o = document.createElement("option"); o.value = v; list.appendChild(o); });
    }
    function distinct(items, key) {
      var seen = {}, out = [];
      items.forEach(function (r) { if (r[key] && !seen[r[key]]) { seen[r[key]] = true; out.push(r[key]); } });
      return out.sort();
    }
    // The strand's datasets, once per strand, feed both pickers.
    function loadRows(strand) {
      if (!strand || strand === rowsStrand) { pickers(); return; }
      rowsStrand = strand;
      fetch("/datasets.json?limit=500&strand=" + encodeURIComponent(strand), { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : { datasets: [] }; })
        .then(function (body) { rows = body.datasets || []; pickers(); })
        .catch(function () { rows = []; pickers(); });
    }
    function pickers() {
      var project = slug(val("project"));
      option(projects, distinct(rows, "project"));
      option(datasets, distinct(rows.filter(function (r) { return !project || r.project === project; }), "dataset"));
    }
    function li(textContent, code) {
      var el = document.createElement("li");
      el.appendChild(document.createTextNode(textContent));
      if (code) { var c = document.createElement("code"); c.textContent = code; el.appendChild(c); }
      notes.appendChild(el);
      return el;
    }
    function render(d) {
      dest = d;
      notes.textContent = ""; notes.hidden = true;
      existing.hidden = true; restricted.hidden = true; restrictedNote.hidden = true;
      panel.className = "destination";
      if (!d) { panel.hidden = true; return; }
      panel.hidden = false;
      var sum = d.summary;
      if (d.exists && sum) {
        panel.classList.add("exists");
        text.innerHTML = "";
        text.appendChild(document.createTextNode("This dataset exists: "));
        var code = document.createElement("code"); code.textContent = d.identifier; text.appendChild(code);
        text.appendChild(document.createTextNode(", " + sum.files + " file" + (sum.files === 1 ? "" : "s") +
          ", version " + (sum.version || "?") + ", last changed " + (sum.modified || "").slice(0, 10) +
          (sum.depositor ? " by " + sum.depositor : "") + ". Your files will be added to it; a file with the same " +
          "path replaces the stored one (the previous copy is kept by storage versioning). Nothing is removed."));
        existing.hidden = false;
      } else if (d.pending && d.pending.length) {
        panel.classList.add("pending");
        var p = d.pending[0];
        text.textContent = "A deposit to this dataset by " + p.user + " is waiting for promotion (" +
          p.files + " file" + (p.files === 1 ? "" : "s") + "). Your files will be added to the same dataset.";
        existing.hidden = false;
        $("use-details").hidden = true;
      } else {
        panel.classList.add("new");
        text.textContent = "New dataset: it will be created at " + d.identifier + ".";
        restricted.hidden = false;
        restrictedNote.hidden = val("restricted") !== "yes";
      }
      $("use-details").hidden = !(d.exists && sum);
      if (!(d.exists || (d.pending && d.pending.length))) confirmBox.checked = false;
      (d.elsewhere || []).forEach(function (r) {
        li("A dataset called " + r.dataset + " already exists at ", r.identifier)
          .appendChild(document.createTextNode(". Depositing here creates a second, separate dataset."));
      });
      if (d.similar && d.similar.project.length) li("Similar to existing project " + d.similar.project.join(", ") + ". Sure this is different?");
      if (d.similar && d.similar.dataset.length) li("Similar to existing dataset " + d.similar.dataset.join(", ") + " in this project. Sure this is different?");
      if (d.exists && d.pending && d.pending.length) li(d.pending.length + " deposit(s) to it are still waiting for promotion.");
      notes.hidden = !notes.children.length;
    }
    function lookup() {
      var strand = val("strand"), project = slug(val("project")), sensitivity = val("sensitivity"),
          state = val("state"), dataset = slug(val("dataset"));
      loadRows(strand);
      if (!(strand && project && sensitivity && state && dataset)) { render(null); return; }
      var mine = ++seq;
      fetch("/deposits/lookup?" + ["strand=" + strand, "project=" + encodeURIComponent(project),
            "sensitivity=" + sensitivity, "state=" + state, "dataset=" + encodeURIComponent(dataset)].join("&"),
            { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (mine === seq) render(d); })
        .catch(function () { if (mine === seq) render(null); });
    }
    function schedule() { clearTimeout(timer); timer = setTimeout(lookup, 250); }
    ["project", "dataset"].forEach(function (id) { $(id).addEventListener("input", schedule); });
    form.addEventListener("change", function (ev) {
      var n = ev.target && ev.target.name;
      if (n === "strand" || n === "sensitivity" || n === "state") schedule();
      if (n === "restricted") restrictedNote.hidden = val("restricted") !== "yes";
    });
    // "Use its details": the record's values, the same ones the
    // command-line tool offers as defaults.
    $("use-details").addEventListener("click", function () {
      if (!dest || !dest.defaults) return;
      var d = dest.defaults;
      [["version", "version"], ["abstract", "abstract"], ["license", "license"], ["creator", "creator"],
       ["coverage_start", "coverage_start"], ["coverage_end", "coverage_end"],
       ["source_detail", "source_detail"]].forEach(function (pair) {
        if (d[pair[1]] !== undefined) $(pair[0]).value = d[pair[1]];
      });
      ["domain", "source_type"].forEach(function (id) {
        if (d[id] !== undefined && Array.prototype.some.call($(id).options, function (o) { return o.value === d[id]; })) $(id).value = d[id];
      });
      if (d.subject) {
        form.querySelectorAll('input[name=subject]').forEach(function (c) { c.checked = d.subject.indexOf(c.value) >= 0; });
      }
      $("abstract").dispatchEvent(new Event("input", { bubbles: true }));
      updateSlip();
      if (d.version) {
        var hint = $("version").closest(".field").querySelector(".hint");
        hint.textContent = "Currently " + d.version + ". Keep it, or raise it only if this is a new version of the dataset.";
      }
    });
    return {
      // What stands in the way of submitting, as a field error, or null.
      blocked: function () {
        if (!dest) return null;
        if (dest.exists || (dest.pending && dest.pending.length)) {
          if (!confirmBox.checked) return { dataset: "This dataset exists. Tick \"Add my files to this dataset\" to go on, or choose another name." };
        } else if (val("restricted") === "yes") {
          return { dataset: "Not deposited: set up the access restriction first, then come back." };
        }
        return null;
      },
      confirm: function () { return !!confirmBox.checked; },
      manifest: function () { return dest && dest.exists ? dest.manifest : null; },
      pending: function () { return !!(dest && !dest.exists && dest.pending && dest.pending.length); }
    };
  })();

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
    if (destination.confirm()) m.confirm_existing = true;
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
          var mark = outcome(f.member, body);
          if (mark) {
            var m = document.createElement("span"); m.className = "mark " + mark; m.textContent = mark;
            state.appendChild(m);
          }
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
  // Added, updated or unchanged against the existing dataset's manifest
  // (the CLI's own rule: same path, checksum and size is unchanged).
  function outcome(member, stored) {
    var manifest = destination.manifest();
    if (!manifest) return destination.pending() ? null : (dest ? "added" : null);
    var prior = null;
    manifest.forEach(function (e) { if (e.path === member) prior = e; });
    if (!prior) return "added";
    return prior.checksum_sha256 === stored.checksum_sha256 && prior.bytes === stored.bytes ? "unchanged" : "updated";
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
    var gate = destination.blocked();
    if (gate) { showErrors(gate); fail(gate.dataset); return; }
    $("submit").disabled = true;
    setStatus("Checking the description…");
    post("/deposits", metadata()).then(function (r) {
      if (!r.ok) {
        if (r.status === 422 && r.body.detail && r.body.detail.errors) {
          showErrors(r.body.detail.errors); fail("Some fields need attention; see the messages next to them.");
        } else if (r.status === 409 && r.body.detail && r.body.detail.message) {
          // The service knows more than the page did (the index moved on):
          // show it where the name is and ask for the tick.
          showErrors({ dataset: r.body.detail.message + ". Tick \"Add my files to this dataset\" to go on." });
          $("destination").hidden = false;
          $("destination-existing").hidden = false;
          fail("The dataset already exists; confirm under the dataset name, then deposit again.");
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
        var what = r.body.files + " file(s) and the dataset record are in the holding area.";
        if (r.body.merges_with) {
          what = "Added to " + r.body.merges_with.split("/").pop() + ": " +
            r.body.added.length + " added, " + r.body.updated.length + " updated, " +
            r.body.unchanged.length + " unchanged. The checking step merges them into the dataset within a few minutes.";
        }
        box.innerHTML = "<h3>Deposited</h3><p></p><p>Record: <code></code></p><p><a></a></p>";
        box.querySelector("p").textContent = what;
        box.querySelector("code").textContent = r.body.record_key;
        var a = box.querySelector("a"); a.href = "/deposits/" + dep.id + "/summary"; a.textContent = "View the deposit summary";
        form.querySelectorAll("input, select, textarea, button").forEach(function (el) { el.disabled = true; });
      });
    }).catch(function (e) { fail(e.message || String(e)); });
  });

  updateSlip();
})();
