/* Nexo POS — live daily-closing calculator.
 *
 * Recomputes cash_variance / reconciliation_difference as the user types the
 * opening float and the per-denomination bill/coin counts, using the SAME
 * formulas and the SAME integer-cents arithmetic as the server
 * (app.py::daily_closing). This is a convenience preview only — the server
 * recomputes everything from its own data and re-enforces the block on
 * submit; nothing here is trusted.
 *
 * Blocking rule (confirmed with the product owner 2026-08-25): a non-zero
 * variance blocks finalization. Only users holding
 * PERM_CLOSING_VARIANCE_OVERRIDE (admin, store_manager) can proceed, and
 * only after entering a justification note.
 */
(function () {
  "use strict";

  var form = document.getElementById("closing-form");
  if (!form) return; // already-closed view has no form

  var cashSalesCents = parseInt(form.dataset.cashSales, 10) || 0;
  var transferSalesCents = parseInt(form.dataset.transferSales, 10) || 0;
  var inventoryWithdrawnCents = parseInt(form.dataset.inventoryWithdrawn, 10) || 0;
  var carriedOpeningFloat = form.dataset.openingFloat;
  var canOverride = form.dataset.canOverride === "1";
  var sessionsOpen = form.dataset.sessionsOpen === "1";

  var openingInput = document.getElementById("opening-float-input");
  var denomInputs = Array.prototype.slice.call(document.querySelectorAll(".denom-input"));
  var submitButton = document.getElementById("closing-submit");
  var justificationWrap = document.getElementById("justification-wrap");
  var justificationField = justificationWrap
    ? justificationWrap.querySelector("textarea")
    : null;
  var messageEl = document.getElementById("calc-message");

  function fmt(cents) {
    var sign = cents < 0 ? "-" : "";
    var abs = Math.abs(cents);
    return sign + "$" + Math.floor(abs / 100) + "." + String(abs % 100).padStart(2, "0");
  }

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  /* Parse a decimal-string money input into integer cents without going
     through float arithmetic — mirrors money.py::parse_to_cents so the
     preview can't disagree with the server over a rounding edge case. */
  function moneyToCents(raw) {
    var value = (raw || "").trim();
    if (!value) return 0;
    if (!/^\d+(\.\d{0,2})?$/.test(value)) return null;
    var parts = value.split(".");
    var whole = parseInt(parts[0], 10) || 0;
    var frac = parts[1] ? (parts[1] + "00").slice(0, 2) : "00";
    return whole * 100 + parseInt(frac, 10);
  }

  function openingFloatCents() {
    if (carriedOpeningFloat !== "") return parseInt(carriedOpeningFloat, 10) || 0;
    if (!openingInput) return 0;
    return moneyToCents(openingInput.value);
  }

  function recalc() {
    var countedCents = 0;
    denomInputs.forEach(function (input) {
      var denomValue = parseInt(input.dataset.valueCents, 10) || 0;
      var quantity = parseInt(input.value, 10);
      if (!quantity || quantity < 0) quantity = 0;
      var subtotal = denomValue * quantity;
      countedCents += subtotal;
      var cell = document.querySelector('.denom-subtotal[data-for="' + denomValue + '"]');
      if (cell) cell.textContent = fmt(subtotal);
    });
    setText("counted-total", fmt(countedCents));

    var opening = openingFloatCents();
    var openingMissing = opening === null;
    if (openingMissing) opening = 0;

    var expectedCents = opening + cashSalesCents;
    var cashVarianceCents = countedCents - expectedCents;
    var recordedRevenueCents = cashSalesCents + transferSalesCents;
    var reconDiffCents = recordedRevenueCents - inventoryWithdrawnCents;

    setText("calc-opening", fmt(opening));
    setText("calc-expected", fmt(expectedCents));
    setText("calc-counted", fmt(countedCents));
    setText("calc-cash-variance", fmt(cashVarianceCents));
    setText("calc-recon", fmt(reconDiffCents));

    var varianceEl = document.getElementById("calc-cash-variance");
    var reconEl = document.getElementById("calc-recon");
    if (varianceEl) varianceEl.className = cashVarianceCents === 0 ? "variance-ok" : "variance-bad";
    if (reconEl) reconEl.className = reconDiffCents === 0 ? "variance-ok" : "variance-bad";

    var hasVariance = cashVarianceCents !== 0 || reconDiffCents !== 0;

    if (justificationWrap) {
      justificationWrap.hidden = !(hasVariance && canOverride);
      if (justificationField) justificationField.required = hasVariance && canOverride;
    }

    var blocked = false;
    var message = "";

    if (sessionsOpen) {
      blocked = true;
      message = "Hay cajas abiertas en esta tienda. Ciérralas antes de finalizar.";
    } else if (openingMissing) {
      blocked = true;
      message = "Ingresa un saldo inicial válido (por ejemplo 1500.00).";
    } else if (hasVariance && !canOverride) {
      blocked = true;
      message =
        "No se puede finalizar: hay una diferencia sin resolver (caja " +
        fmt(cashVarianceCents) + ", conciliación " + fmt(reconDiffCents) +
        "). Vuelve a contar el efectivo o solicita autorización a un gerente/administrador.";
    } else if (hasVariance) {
      message =
        "Hay una diferencia (caja " + fmt(cashVarianceCents) + ", conciliación " +
        fmt(reconDiffCents) + "). Como tienes permiso de autorización, puedes finalizar " +
        "escribiendo una nota de justificación.";
    } else {
      message = "Sin diferencias. El cierre puede finalizarse.";
    }

    if (messageEl) {
      messageEl.textContent = message;
      messageEl.className = blocked ? "flash error" : hasVariance ? "flash warning" : "flash success";
    }
    if (submitButton) submitButton.disabled = blocked;
  }

  denomInputs.forEach(function (input) {
    input.addEventListener("input", recalc);
  });
  if (openingInput) openingInput.addEventListener("input", recalc);

  recalc();
})();
