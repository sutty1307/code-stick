/* DepositDesk client.
   The content security policy allows no inline script, so every listener is bound here.
   Nothing is cached across reloads: the register is always drawn from /api/state. */
(function () {
  "use strict";

  var state = {
    csrf: "", mode: "simulator", liveEnabled: false, business: "", source: "",
    recipients: [], payments: [], selectedId: null, trail: [], changedId: null
  };
  var generation = 0, loadSequence = 0, sheetBusy = false, sheetGeneration = 0;
  var busyIds = new Set();

  var LABEL = {
    draft: "Draft", submitting: "Submitting", pending: "Pending", processed: "Processed",
    failed: "Failed", returned: "Returned", cancelled: "Cancelled", needs_review: "Needs review"
  };

  var KIND = { contractor: "Contractor", employee: "Employee", vendor: "Vendor" };

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function money(cents) {
    var whole = Math.floor(Math.abs(cents) / 100), part = Math.abs(cents) % 100;
    return "$" + whole.toLocaleString("en-US") + "." + String(part).padStart(2, "0");
  }

  function uuid() {
    if (window.crypto && crypto.randomUUID) { return crypto.randomUUID(); }
    var bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    var hex = Array.from(bytes, function (b) { return b.toString(16).padStart(2, "0"); }).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
  }

  function shortDate(iso) {
    var when = new Date(iso);
    if (isNaN(when)) { return "\u2014"; }
    return when.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "2-digit" });
  }

  function fullDate(iso) {
    var when = new Date(iso);
    if (isNaN(when)) { return "\u2014"; }
    return when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  var toastTimer = null;
  function toast(message, bad) {
    var node = $("toast");
    node.textContent = message;
    node.className = bad ? "toast toast-bad" : "toast";
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.hidden = true; }, bad ? 7000 : 3500);
  }

  /* --- transport ------------------------------------------------------- */

  function api(path, body, method) {
    var epoch = generation;
    var init = { method: method || (body === undefined ? "GET" : "POST"), credentials: "same-origin", headers: {} };
    if (init.method === "POST") {
      // Every POST carries a body: the server rejects empty ones outright.
      init.headers["Content-Type"] = "application/json";
      init.headers["X-CSRF-Token"] = state.csrf;
      init.body = JSON.stringify(body === undefined ? {} : body);
    }
    return fetch(path, init).then(function (response) {
      return response.json().catch(function () { throw new Error("The server returned an unreadable response. Refresh the register before retrying a payment."); }).then(function (payload) {
        if (epoch !== generation) { var stale = new Error("Request belongs to an earlier session."); stale.stale = true; throw stale; }
        if (response.ok) { return payload; }
        var error = new Error(payload.error || "The request could not be completed.");
        error.status = response.status;
        throw error;
      });
    });
  }

  function guard(error) {
    if (error.stale) { return; }
    if (error && error.status === 401) { showGate(error.message); return; }
    toast(error && error.message ? error.message : "Something went wrong.", true);
  }

  /* --- screens --------------------------------------------------------- */

  function showGate(message) {
    generation += 1;
    closeSheet(true);
    state.csrf = ""; state.payments = []; state.recipients = []; state.selectedId = null; state.trail = [];
    $("shell").hidden = true;
    $("gate").hidden = false;
    var error = $("gate-error");
    if (message) { error.textContent = message; error.hidden = false; } else { error.hidden = true; }
    $("gate-password").value = "";
    $("gate-password").focus();
  }

  function showShell() {
    $("gate").hidden = true;
    $("shell").hidden = false;
  }

  function loadState() {
    var sequence = ++loadSequence;
    return api("/api/state").then(function (payload) {
      if (sequence !== loadSequence) { return; }
      state.csrf = payload.csrf;
      state.mode = payload.mode;
      state.liveEnabled = payload.live_enabled === true;
      state.business = payload.business_name;
      state.source = payload.source_label;
      state.recipients = payload.recipients || [];
      state.payments = payload.payments || [];
      showShell();
      renderChrome(payload);
      renderLedger();
      renderDetail();
      $("last-updated").textContent = "Updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      if (state.selectedId) { return selectPayment(state.selectedId, false); }
    });
  }

  function renderChrome(payload) {
    $("business-name").textContent = state.business;
    $("sign-out").hidden = !!payload.preview_mode;
    $("source-label").textContent = "Paying from: " + state.source;
    var badge = $("env-badge");
    badge.textContent = payload.preview_mode ? "Offline demo" : state.mode === "simulator" ? "Simulator" : state.mode === "dwolla_production" ? "Live connection" : "Dwolla sandbox";
    $("check-connection").hidden = !!payload.preview_mode;
    $("funds-mode").textContent = state.mode === "dwolla_production" ? "USD · Live release " + (state.liveEnabled ? "enabled" : "disabled") : "USD · Test funds only";
    if (payload.preview_mode) { $("mode-banner").textContent = "Interactive demo · Fictional records only. Reloading resets all changes. No bank connection or saved account."; return; }
    $("mode-banner").textContent = payload.live_enabled
      ? "Live ACH release enabled · Releasing a payment can move real money."
      : state.mode === "dwolla_production"
        ? "Live connection · Payment release is disabled. Check the connection and prepare drafts before activation."
      : state.mode === "simulator"
        ? "Local simulator · No bank is contacted. Status changes are simulated."
        : "Dwolla sandbox · Test API transfers only. No live funds can move.";
  }

  /* --- register -------------------------------------------------------- */

  function renderLedger() {
    function sum(status) { return state.payments.filter(function (p) { return p.state === status; }).reduce(function (total, p) { return total + p.amount_cents; }, 0); }
    function count(status) { return state.payments.filter(function (p) { return p.state === status; }).length; }
    function attention(p) { return ["submitting", "needs_review", "failed", "returned"].includes(p.state); }
    $("draft-total").textContent = money(sum("draft")); $("draft-count").textContent = count("draft") + " payments";
    $("pending-total").textContent = money(sum("pending")); $("pending-count").textContent = count("pending") + " payments";
    $("attention-count").textContent = state.payments.filter(attention).length;
    var query = $("search-payments").value.trim().toLocaleLowerCase();
    var filter = $("status-filter").value;
    var visible = state.payments.filter(function (p) {
      return (filter === "all" || (filter === "attention" ? attention(p) : p.state === filter)) &&
        [p.reference, p.recipient_name, p.memo, p.id].join(" ").toLocaleLowerCase().includes(query);
    });
    var body = $("ledger-body");
    body.replaceChildren();
    $("ledger-empty").hidden = visible.length > 0;
    $("empty-title").textContent = state.payments.length ? "No matching payments." : "Your register starts here.";
    $("empty-hint").textContent = state.payments.length ? "Try another name or reference, or change the status filter." : "Add a recipient, then save your first payment for review.";
    $("record-count").textContent = visible.length + " of " + state.payments.length + " records · USD";

    visible.forEach(function (payment) {
      var row = el("tr");
      row.dataset.id = payment.id;
      if (payment.id === state.selectedId) { row.classList.add("is-selected"); row.setAttribute("aria-current", "true"); }
      if (payment.state === "needs_review" || payment.state === "failed" || payment.state === "returned") {
        row.classList.add("row-attention");
      }
      if (payment.id === state.changedId) { row.classList.add("just-changed"); }

      var referenceCell = el("td", "cell-ref");
      var referenceButton = el("button", "reference-button", payment.reference);
      referenceButton.setAttribute("aria-label", "Open payment " + payment.reference);
      referenceCell.appendChild(referenceButton); row.appendChild(referenceCell);

      var name = el("td", "cell-name", payment.recipient_name);
      name.title = payment.recipient_name;
      row.appendChild(name);

      row.appendChild(el("td", "cell-amount", money(payment.amount_cents)));

      var statusCell = el("td");
      statusCell.appendChild(el("span", "status status-" + payment.state, LABEL[payment.state] || payment.state));
      if (payment.refresh_due) {
        var flag = el("span", "flag", "update waiting");
        flag.title = "The provider sent an event about this transfer. Check its status.";
        statusCell.appendChild(flag);
      }
      row.appendChild(statusCell);

      row.appendChild(el("td", "cell-date", shortDate(payment.created_at)));
      body.appendChild(row);
    });
    state.changedId = null;
  }

  function selectPayment(id, scroll) {
    state.selectedId = id;
    state.trail = [];
    renderLedger();
    renderDetail();
    $("detail").classList.add("detail-open");
    return api("/api/payments/" + id).then(function (payload) {
      if (state.selectedId !== id) { return; }
      if (!replacePayment(payload.payment)) { return; }
      state.trail = payload.audit || [];
      renderDetail();
      if (scroll !== false && window.matchMedia("(max-width: 900px)").matches) { $("detail").scrollIntoView({ block: "start" }); }
    }).catch(guard);
  }

  function replacePayment(payment) {
    loadSequence += 1;
    var index = state.payments.findIndex(function (item) { return item.id === payment.id; });
    if (index >= 0 && state.payments[index].updated_at > payment.updated_at) { return false; }
    if (index >= 0) { state.payments[index] = payment; } else { state.payments.unshift(payment); }
    return true;
  }

  function currentPayment() {
    return state.payments.find(function (item) { return item.id === state.selectedId; }) || null;
  }

  /* --- detail ---------------------------------------------------------- */

  function renderDetail() {
    var payment = currentPayment();
    var body = $("detail-body");
    $("detail-close").hidden = !payment;
    $("detail-idle").hidden = !!payment;
    body.hidden = !payment;
    body.replaceChildren();
    if (!payment) { $("detail").classList.remove("detail-open"); return; }

    body.appendChild(el("p", "detail-name", payment.recipient_name));
    body.appendChild(el("p", "detail-amount", money(payment.amount_cents)));
    body.appendChild(el("p", "status status-" + payment.state, LABEL[payment.state] || payment.state));

    var facts = el("dl", "facts");
    [
      ["Reference", payment.reference],
      ["Account", payment.account_label],
      ["Authorization", payment.authorization_ref],
      ["Memo", payment.memo || "\u2014"],
      ["Recorded", fullDate(payment.created_at)],
      ["Released", payment.approved_at ? fullDate(payment.approved_at) : "\u2014"],
      ["Payment ID", payment.id],
      ["Transfer ID", payment.provider_ref || "\u2014"]
    ].forEach(function (pair) {
      var line = el("div");
      line.appendChild(el("dt", null, pair[0]));
      line.appendChild(el("dd", null, pair[1]));
      facts.appendChild(line);
    });
    body.appendChild(facts);

    if (payment.message) {
      var attention = payment.state === "needs_review" || payment.state === "failed" || payment.state === "returned";
      body.appendChild(el("p", attention ? "note note-attention" : "note", payment.message));
    }

    var actions = el("div", "detail-actions");
    function action(label, className, handler) {
      var button = el("button", "btn btn-small " + className, label);
      button.disabled = busyIds.has(payment.id);
      button.addEventListener("click", function () { return handler(); });
      actions.appendChild(button);
    }

    if (payment.state === "draft") {
      action("Release payment", "btn-stamp", function () { openRelease(payment); });
      action("Cancel draft", "", function () { act(payment.id, "cancel", {}, "Draft cancelled."); });
    }
    if (state.mode === "simulator") {
      if (payment.state === "pending") {
        action("Mark processed", "", function () { act(payment.id, "simulate", { state: "processed" }, "Marked processed."); });
        action("Mark failed", "", function () { act(payment.id, "simulate", { state: "failed" }, "Marked failed."); });
      }
      if (payment.state === "processed") {
        action("Mark returned", "", function () { act(payment.id, "simulate", { state: "returned" }, "Marked returned."); });
      }
    } else if (payment.provider_ref) {
      action("Check status", "", function () { act(payment.id, "refresh", {}, "Status checked."); });
    }
    if (payment.can_reconcile) {
      action("Reconcile submission", "btn-primary", function () { openReconcile(payment); });
    }
    if (actions.childElementCount) { body.appendChild(actions); }

    body.appendChild(el("h3", null, "History"));
    var trail = el("ul", "trail");
    if (!state.trail.length) {
      trail.appendChild(el("li", "trail-detail", "No entries yet."));
    }
    state.trail.slice().reverse().forEach(function (entry) {
      var item = el("li");
      item.appendChild(el("strong", null, entry.action.replace(/_/g, " ")));
      item.appendChild(el("span", "trail-detail", " " + entry.detail));
      item.appendChild(el("time", null, fullDate(entry.created_at)));
      trail.appendChild(item);
    });
    body.appendChild(trail);
  }

  function act(id, action, body, success) {
    if (busyIds.has(id)) { return Promise.resolve(); }
    busyIds.add(id); renderDetail();
    return api("/api/payments/" + id + "/" + action, body).then(function (payment) {
      replacePayment(payment);
      state.changedId = payment.id;
      renderLedger();
      toast(success);
      return api("/api/payments/" + id);
    }).then(function (payload) {
      if (state.selectedId !== id) { return; }
      if (!replacePayment(payload.payment)) { return; }
      state.trail = payload.audit || [];
      renderDetail();
    }).catch(function (error) {
      guard(error);
      // The server may have changed the payment even while refusing this request.
      if (!error.stale && error.status !== 401) { loadState().catch(guard); }
    }).finally(function () { busyIds.delete(id); renderDetail(); });
  }

  /* --- sheets ---------------------------------------------------------- */

  var lastFocused = null;

  function openSheet(title, build) {
    if (sheetBusy) { return; }
    sheetGeneration += 1;
    lastFocused = document.activeElement;
    $("sheet-title").textContent = title;
    var body = el("form");
    $("sheet-body").replaceChildren(body);
    build(body);
    $("sheet-close").disabled = false;
    $("overlay").hidden = false;
    if (!$("overlay").open) { $("overlay").showModal(); }
    var first = body.querySelector("input, select, textarea, button");
    if (first) { first.focus(); }
  }

  function closeSheet(force) {
    if (sheetBusy && force !== true) { return; }
    if (force === true) { sheetBusy = false; }
    if ($("overlay").open) { $("overlay").close(); }
    $("overlay").hidden = true;
    $("sheet-body").replaceChildren();
    if (lastFocused && lastFocused.isConnected) { lastFocused.focus(); }
  }

  function field(label, control, hint) {
    var wrap = el("label", "field");
    wrap.appendChild(el("span", "field-label", label));
    wrap.appendChild(control);
    if (hint) { wrap.appendChild(el("span", "field-hint", hint)); }
    return wrap;
  }

  function input(type, attrs) {
    var node = el("input");
    node.type = type;
    Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, attrs[key]); });
    return node;
  }

  function errorSlot(container) {
    var node = el("p", "alert");
    node.setAttribute("role", "alert");
    node.hidden = true;
    container.appendChild(node);
    return function (message) {
      if (!message) { node.hidden = true; return; }
      node.textContent = message;
      node.hidden = false;
      node.scrollIntoView({ block: "nearest" });
    };
  }

  function footer(container, label, className, onSubmit) {
    var sheetId = sheetGeneration;
    var bar = el("div", "detail-actions");
    var submit = el("button", "btn " + className, label);
    var cancel = el("button", "btn btn-quiet", "Cancel");
    submit.type = "submit"; cancel.type = "button";
    cancel.addEventListener("click", closeSheet);
    container.addEventListener("submit", function (event) {
      event.preventDefault();
      if (sheetBusy) { return; }
      sheetBusy = true;
      submit.disabled = true; cancel.disabled = true; $("sheet-close").disabled = true;
      var original = submit.textContent; submit.textContent = "Working…";
      Promise.resolve().then(onSubmit).catch(guard).finally(function () {
        if (sheetId === sheetGeneration) { sheetBusy = false; $("sheet-close").disabled = false; }
        submit.disabled = false; cancel.disabled = false;
        submit.textContent = original;
      });
    });
    bar.appendChild(submit);
    bar.appendChild(cancel);
    container.appendChild(bar);
  }

  /* --- add recipient --------------------------------------------------- */

  function openRecipient() {
    openSheet("Add a recipient", function (body) {
      var name = input("text", { maxlength: "80", autocomplete: "off" });
      var kind = el("select");
      ["contractor", "employee", "vendor"].forEach(function (value) {
        var option = el("option", null, KIND[value]);
        option.value = value;
        kind.appendChild(option);
      });
      var label = input("text", { maxlength: "60", autocomplete: "off" });
      var authorization = input("text", { maxlength: "100", autocomplete: "off" });
      var customer = input("text", { maxlength: "36", autocomplete: "off", spellcheck: "false" });
      var funding = input("text", { maxlength: "36", autocomplete: "off", spellcheck: "false" });
      customer.className = "mono";
      funding.className = "mono";

      body.appendChild(field("Name", name, "How this person or company appears in the register."));
      body.appendChild(field("Relationship", kind));
      body.appendChild(field("Account nickname", label, "Your own label for the account being paid, such as \u201cPayroll checking\u201d."));
      body.appendChild(field("Authorization record", authorization,
        "Reference to your authorization record. Do not enter bank account numbers here."));

      if (state.mode !== "simulator") {
        body.appendChild(field("Dwolla customer ID", customer, "The customer who owns the destination in this provider environment."));
        body.appendChild(field("Dwolla funding source ID", funding, "The recipient bank account already linked with the provider."));
      } else {
        body.appendChild(el("p", "note", "The simulator creates a fictional account reference for this recipient."));
        funding.className = "";
        funding.setAttribute("maxlength", "100");
      }

      var check = el("label", "check");
      var acknowledged = input("checkbox");
      check.appendChild(acknowledged);
      check.appendChild(el("span", null,
        state.mode === "dwolla_production" ? "I confirm the recipient and account details. The authorization record exists and is retrievable." : "This is a test recipient, and the authorization record above exists and is retrievable."));
      body.appendChild(check);

      var showError = errorSlot(body);

      footer(body, "Add recipient", "btn-primary", function () {
        showError("");
        var payload = {
          name: name.value, kind: kind.value, account_label: label.value,
          authorization_ref: authorization.value, acknowledged: acknowledged.checked,
          funding_id: funding.value || null
        };
        if (state.mode !== "simulator") { payload.customer_id = customer.value.trim(); }
        return api("/api/recipients", payload).then(function () {
          closeSheet(true);
          toast("Recipient added.");
          return loadState();
        }).catch(function (error) {
          if (error.status === 401) { return guard(error); }
          showError(error.message);
        });
      });
    });
  }

  /* --- draft a payment ------------------------------------------------- */

  function openPayment() {
    if (!state.recipients.length) {
      toast("Add a recipient before drafting a payment.", true);
      return;
    }
    // Held outside the submit handler so a failed attempt retries as the same
    // request rather than creating a second draft.
    var requestId = uuid();

    openSheet("New payment", function (body) {
      var recipient = el("select");
      state.recipients.forEach(function (item) {
        var option = el("option", null, item.name + " \u2014 " + item.account_label);
        option.value = item.id;
        recipient.appendChild(option);
      });
      var amount = input("text", { inputmode: "decimal", autocomplete: "off", placeholder: "0.00", maxlength: "9" });
      var reference = input("text", { maxlength: "80", autocomplete: "off" });
      var memo = el("textarea");
      memo.setAttribute("maxlength", "140");

      body.appendChild(field("Recipient", recipient));
      body.appendChild(field("Amount in dollars", amount, "Up to $25,000.00 per payment in this build."));
      body.appendChild(field("Reference", reference,
        "Your identifier for this payment, such as an invoice or pay-period number. It has to be unique for this recipient."));
      body.appendChild(field("Memo", memo, "Optional. Stored in this register; not sent as bank addenda."));

      var showError = errorSlot(body);

      footer(body, "Save draft", "btn-primary", function () {
        showError("");
        return api("/api/payments", {
          request_id: requestId, recipient_id: recipient.value,
          amount: amount.value.trim(), reference: reference.value, memo: memo.value
        }).then(function (payment) {
          closeSheet(true);
          $("search-payments").value = ""; $("status-filter").value = "all";
          replacePayment(payment);
          state.selectedId = payment.id;
          state.changedId = payment.id;
          renderLedger();
          selectPayment(payment.id);
          toast("Draft saved. Nothing has been sent.");
        }).catch(function (error) {
          if (error.status === 401) { return guard(error); }
          showError(error.message);
        });
      });
    });
  }

  /* --- release --------------------------------------------------------- */

  function openRelease(payment) {
    if (state.mode === "dwolla_production" && !state.liveEnabled) {
      toast("Live release is disabled on the server. Connection checks and drafts remain available.", true);
      return;
    }
    openSheet("Release payment", function (body) {
      var panel = el("div", "endorse");
      panel.appendChild(el("p", "endorse-to", state.mode === "dwolla_production" ? "Live ACH payment" : "Test payment"));
      panel.appendChild(el("p", "endorse-amount", money(payment.amount_cents)));
      var to = el("p", "endorse-to");
      to.appendChild(document.createTextNode("to "));
      to.appendChild(el("strong", null, payment.recipient_name));
      to.appendChild(document.createTextNode(" \u00b7 " + payment.account_label));
      panel.appendChild(to);
      panel.appendChild(el("p", "field-hint", "From: " + state.source));
      body.appendChild(panel);

      body.appendChild(el("p", "note",
        "Authorization on file: " + payment.authorization_ref + ". Reference " + payment.reference + "."));

      var confirm = input("text", { inputmode: "decimal", autocomplete: "off", placeholder: "0.00", maxlength: "9" });
      body.appendChild(field("Type the amount again", confirm,
        state.mode === "simulator" ? "Confirms a simulated instruction. No bank is contacted." : state.mode === "dwolla_production" ? "This instruction will request a real debit from the business bank and a credit to the recipient." : "Final amount check before a sandbox API instruction is submitted."));

      var check = el("label", "check");
      var authorized = input("checkbox");
      check.appendChild(authorized);
      check.appendChild(el("span", null,
        (state.mode === "dwolla_production" ? "I confirm this will move real money, and I authorize this payment. " : "I authorize this test payment. ") + "A released payment cannot be recalled from here."));
      body.appendChild(check);

      var showError = errorSlot(body);

      footer(body, "Release payment", "btn-stamp", function () {
        showError("");
        if (!authorized.checked) { showError("Tick the authorization box to release this payment."); return; }
        return api("/api/payments/" + payment.id + "/submit", {
          authorized: true, amount: confirm.value.trim(), live_confirmed: state.mode === "dwolla_production" && authorized.checked
        }).then(function (updated) {
          closeSheet(true);
          replacePayment(updated);
          state.changedId = updated.id;
          renderLedger();
          selectPayment(updated.id);
          if (updated.state === "needs_review") {
            toast("Submission outcome is unconfirmed. Reconcile before sending anything else.", true);
          } else if (updated.state === "draft") {
            toast("Nothing was sent. The payment is still a draft.", true);
          } else {
            toast("Payment released.");
          }
        }).catch(function (error) {
          if (error.status === 401) { return guard(error); }
          showError(error.message);
        });
      });
    });
  }

  /* --- wiring ---------------------------------------------------------- */

  function openReconcile(payment) {
    openSheet("Reconcile submission", function (body) {
      body.appendChild(el("p", "note", "Locate this payment in the matching provider dashboard using payment ID " + payment.id + ". We will verify the existing transfer before linking it. No new payment is sent."));
      var reference = input("text", { maxlength: "36", required: "", autocomplete: "off", spellcheck: "false" });
      body.appendChild(field("Existing provider transfer ID", reference, "The UUID of the transfer already created by the provider."));
      var showError = errorSlot(body);
      footer(body, "Verify and link transfer", "btn-primary", function () {
        showError("");
        return api("/api/payments/" + payment.id + "/reconcile", { provider_ref: reference.value.trim() }).then(function (updated) {
          closeSheet(true); replacePayment(updated); renderLedger();
          toast("Existing transfer verified and linked. No new payment sent.");
          return selectPayment(updated.id);
        }).catch(function (error) { if (error.status === 401 || error.stale) { guard(error); } else { showError(error.message); } });
      });
    });
  }

  function signIn() {
    var password = $("gate-password").value;
    var button = $("gate-submit");
    button.disabled = true;
    api("/api/login", { password: password }).then(function (payload) {
      state.csrf = payload.csrf;
      $("gate-error").hidden = true;
      return loadState();
    }).catch(function (error) {
      var node = $("gate-error");
      node.textContent = error.message;
      node.hidden = false;
    }).finally(function () {
      $("gate-password").value = "";
      button.disabled = false;
    });
  }

  $("gate-form").addEventListener("submit", function (event) { event.preventDefault(); signIn(); });

  $("sign-out").addEventListener("click", function () {
    api("/api/logout", {}).then(function () {
      state.payments = [];
      state.recipients = [];
      state.selectedId = null;
      showGate("");
    }).catch(guard);
  });

  $("open-payment").addEventListener("click", openPayment);
  $("open-recipient").addEventListener("click", openRecipient);
  $("check-connection").addEventListener("click", function () {
    var button = this;
    button.disabled = true;
    api("/api/connection").then(function (report) {
      openSheet("Bank connection", function (body) {
        body.appendChild(el("p", "eyebrow", report.mode));
        body.appendChild(el("h3", null, report.connected ? "Source verified with provider" : "Connection not verified"));
        if (report.source_name) { body.appendChild(el("p", "note", report.source_name)); }
        body.appendChild(el("p", "note", report.message));
        body.appendChild(el("p", "small", "Checked: " + fullDate(report.checked_at)));
        body.appendChild(el("p", "small", "Live release: " + (report.live_enabled ? "enabled" : "disabled")));
        body.appendChild(el("p", "small", "This check does not send a payment or confirm bank-statement classification."));
      });
    }).catch(guard).finally(function () { button.disabled = false; });
  });
  $("sheet-close").addEventListener("click", closeSheet);
  $("detail-close").addEventListener("click", function () { state.selectedId = null; state.trail = []; renderDetail(); renderLedger(); $("register-heading").focus(); });
  $("search-payments").addEventListener("input", renderLedger);
  $("status-filter").addEventListener("change", renderLedger);
  $("reload-state").addEventListener("click", function () {
    this.disabled = true; var button = this;
    loadState().catch(guard).finally(function () { button.disabled = false; });
  });
  $("overlay").addEventListener("cancel", function (event) { event.preventDefault(); closeSheet(); });

  $("overlay").addEventListener("click", function (event) {
    if (event.target === $("overlay")) { closeSheet(); }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !$("overlay").hidden) { closeSheet(); }
  });

  $("ledger-body").addEventListener("click", function (event) {
    var row = event.target.closest("tr");
    if (row && row.dataset.id) { selectPayment(row.dataset.id); }
  });

  loadState().catch(function (error) {
    if (error.status === 401) { showGate(""); } else { showGate(error.message); }
  });
})();
