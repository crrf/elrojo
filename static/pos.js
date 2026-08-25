/* Nexo POS — Odoo-style point-of-sale screen.
 *
 * Product grid + live cart, all client-side. On "Cobrar" it serializes the
 * cart into the SAME flat form fields the server already expects
 * (item_product_id_N / item_quantity_N / payment_method_N / payment_amount_N,
 * see app.py::_parse_sale_form) and submits one normal POST — no AJAX, no
 * change to the backend contract, and the page still works as a plain form
 * submission end to end.
 *
 * All money is integer cents here, mirroring money.py, so the preview total
 * can never disagree with the server's over float rounding. The server
 * revalidates everything (stock under lock, price from the catalog, payment
 * total match); nothing computed here is trusted.
 */
(function () {
  "use strict";

  var form = document.getElementById("pos-form");
  if (!form) return;

  var maxItems = parseInt(form.dataset.maxItems, 10) || 8;
  var maxPayments = parseInt(form.dataset.maxPayments, 10) || 3;

  var products = {};
  JSON.parse(form.dataset.products || "[]").forEach(function (p) {
    products[String(p.id)] = p;
  });
  var stockByStore = JSON.parse(form.dataset.stock || "{}");

  var storeSelect = document.getElementById("pos-store");
  var searchInput = document.getElementById("pos-search");
  var grid = document.getElementById("pos-grid");
  var cartBody = document.getElementById("pos-cart-lines");
  var emptyRow = document.getElementById("pos-cart-empty");
  var paymentsWrap = document.getElementById("pos-payments");
  var addPaymentBtn = document.getElementById("pos-add-payment");
  var paymentTemplate = document.getElementById("pos-payment-row-template");
  var submitBtn = document.getElementById("pos-submit");
  var messageEl = document.getElementById("pos-message");
  var hiddenFields = document.getElementById("pos-hidden-fields");

  var cart = []; // [{productId, quantity}]

  function fmt(cents) {
    var sign = cents < 0 ? "-" : "";
    var abs = Math.abs(cents);
    return sign + "$" + Math.floor(abs / 100) + "." + String(abs % 100).padStart(2, "0");
  }

  /* Mirrors money.py::parse_to_cents — no float arithmetic. */
  function moneyToCents(raw) {
    var value = (raw || "").trim();
    if (!value) return 0;
    if (!/^\d+(\.\d{0,2})?$/.test(value)) return null;
    var parts = value.split(".");
    var whole = parseInt(parts[0], 10) || 0;
    var frac = parts[1] ? (parts[1] + "00").slice(0, 2) : "00";
    return whole * 100 + parseInt(frac, 10);
  }

  function stockFor(productId) {
    var storeStock = stockByStore[storeSelect.value] || {};
    return storeStock[String(productId)] || 0;
  }

  function refreshTileStock() {
    document.querySelectorAll("[data-stock-for]").forEach(function (el) {
      var productId = el.dataset.stockFor;
      var available = stockFor(productId);
      el.textContent = available + " disp.";
      el.className = "pos-tile-stock" + (available <= 0 ? " pos-stock-out" : "");
    });
  }

  function addToCart(productId) {
    var line = cart.find(function (l) { return l.productId === productId; });
    if (line) {
      line.quantity += 1;
    } else {
      if (cart.length >= maxItems) {
        flashMessage("Máximo " + maxItems + " productos distintos por venta.", "error");
        return;
      }
      cart.push({ productId: productId, quantity: 1 });
    }
    render();
  }

  function setQuantity(productId, quantity) {
    var index = cart.findIndex(function (l) { return l.productId === productId; });
    if (index === -1) return;
    if (quantity <= 0) {
      cart.splice(index, 1);
    } else {
      cart[index].quantity = quantity;
    }
    render();
  }

  function cartTotalCents() {
    return cart.reduce(function (sum, line) {
      var product = products[line.productId];
      return sum + (product ? product.price_cents * line.quantity : 0);
    }, 0);
  }

  function paidTotalCents() {
    var total = 0;
    var invalid = false;
    paymentsWrap.querySelectorAll(".pos-payment-amount").forEach(function (input) {
      var cents = moneyToCents(input.value);
      if (cents === null) { invalid = true; return; }
      total += cents;
    });
    return invalid ? null : total;
  }

  function flashMessage(text, kind) {
    messageEl.textContent = text;
    messageEl.className = kind ? "flash " + kind : "muted";
  }

  function renderCart() {
    cartBody.querySelectorAll(".pos-cart-line").forEach(function (row) { row.remove(); });
    emptyRow.hidden = cart.length > 0;

    cart.forEach(function (line) {
      var product = products[line.productId];
      if (!product) return;
      var row = document.createElement("tr");
      row.className = "pos-cart-line";

      var nameCell = document.createElement("td");
      nameCell.textContent = product.name;
      var unit = document.createElement("small");
      unit.className = "muted";
      unit.textContent = " " + fmt(product.price_cents) + " c/u";
      nameCell.appendChild(unit);

      var qtyCell = document.createElement("td");
      var qtyInput = document.createElement("input");
      qtyInput.type = "number";
      qtyInput.min = "0";
      qtyInput.step = "1";
      qtyInput.value = String(line.quantity);
      qtyInput.className = "pos-qty-input";
      qtyInput.addEventListener("input", function () {
        setQuantity(line.productId, parseInt(qtyInput.value, 10) || 0);
      });
      qtyCell.appendChild(qtyInput);

      var subtotalCell = document.createElement("td");
      subtotalCell.textContent = fmt(product.price_cents * line.quantity);

      var removeCell = document.createElement("td");
      var removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "button-secondary";
      removeBtn.textContent = "×";
      removeBtn.addEventListener("click", function () { setQuantity(line.productId, 0); });
      removeCell.appendChild(removeBtn);

      row.append(nameCell, qtyCell, subtotalCell, removeCell);
      cartBody.appendChild(row);
    });
  }

  function render() {
    renderCart();
    refreshTileStock();

    var totalCents = cartTotalCents();
    var paidCents = paidTotalCents();
    document.getElementById("pos-total").textContent = fmt(totalCents);
    document.getElementById("pos-paid").textContent = paidCents === null ? "—" : fmt(paidCents);
    document.getElementById("pos-due").textContent =
      paidCents === null ? "—" : fmt(totalCents - paidCents);

    // Client-side stock warning. The server re-checks under lock at submit
    // time (that's the authoritative check) — this just avoids a pointless
    // round-trip and tells the cashier before they take payment.
    var overstocked = cart.filter(function (line) {
      return line.quantity > stockFor(line.productId);
    });

    var blocked = false;
    if (cart.length === 0) {
      blocked = true;
      flashMessage("Agrega productos y pagos para continuar.", "");
    } else if (overstocked.length > 0) {
      blocked = true;
      var names = overstocked.map(function (l) {
        return products[l.productId].name + " (disp. " + stockFor(l.productId) + ")";
      });
      flashMessage("Stock insuficiente: " + names.join(", "), "error");
    } else if (paidCents === null) {
      blocked = true;
      flashMessage("Hay un monto de pago con formato inválido.", "error");
    } else if (paidCents !== totalCents) {
      blocked = true;
      flashMessage(
        "El total de pagos (" + fmt(paidCents) + ") debe coincidir exactamente con el total (" +
        fmt(totalCents) + ").", "warning"
      );
    } else {
      flashMessage("Listo para cobrar.", "success");
    }
    submitBtn.disabled = blocked;
  }

  function addPaymentRow(prefillCents) {
    if (paymentsWrap.querySelectorAll(".pos-payment-row").length >= maxPayments) {
      flashMessage("Máximo " + maxPayments + " pagos por venta.", "error");
      return;
    }
    var node = paymentTemplate.content.cloneNode(true);
    var row = node.querySelector(".pos-payment-row");
    var amountInput = row.querySelector(".pos-payment-amount");
    if (prefillCents && prefillCents > 0) {
      amountInput.value = (Math.floor(prefillCents / 100)) + "." + String(prefillCents % 100).padStart(2, "0");
    }
    amountInput.addEventListener("input", render);
    row.querySelector(".pos-payment-method").addEventListener("change", render);
    row.querySelector(".pos-remove-payment").addEventListener("click", function () {
      row.remove();
      render();
    });
    paymentsWrap.appendChild(node);
    render();
  }

  /* Serialize cart + payments into the flat field names the server parses. */
  form.addEventListener("submit", function (event) {
    hiddenFields.textContent = "";
    if (submitBtn.disabled) { event.preventDefault(); return; }

    cart.forEach(function (line, index) {
      var n = index + 1;
      hiddenFields.appendChild(makeHidden("item_product_id_" + n, line.productId));
      hiddenFields.appendChild(makeHidden("item_quantity_" + n, String(line.quantity)));
    });
    var paymentIndex = 0;
    paymentsWrap.querySelectorAll(".pos-payment-row").forEach(function (row) {
      var amount = row.querySelector(".pos-payment-amount").value.trim();
      if (!amount) return;
      paymentIndex += 1;
      hiddenFields.appendChild(
        makeHidden("payment_method_" + paymentIndex, row.querySelector(".pos-payment-method").value)
      );
      hiddenFields.appendChild(makeHidden("payment_amount_" + paymentIndex, amount));
    });
  });

  function makeHidden(name, value) {
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    return input;
  }

  grid.addEventListener("click", function (event) {
    var tile = event.target.closest(".pos-tile");
    if (tile) addToCart(tile.dataset.productId);
  });

  searchInput.addEventListener("input", function () {
    var term = searchInput.value.trim().toLowerCase();
    document.querySelectorAll(".pos-tile").forEach(function (tile) {
      var haystack = (tile.dataset.name + " " + tile.dataset.sku).toLowerCase();
      tile.hidden = term !== "" && haystack.indexOf(term) === -1;
    });
  });

  storeSelect.addEventListener("change", render);
  addPaymentBtn.addEventListener("click", function () {
    var due = cartTotalCents() - (paidTotalCents() || 0);
    addPaymentRow(due > 0 ? due : 0);
  });

  addPaymentRow(0);
  render();
})();
