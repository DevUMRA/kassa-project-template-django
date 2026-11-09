'use strict';

/*
 * Дүкен: касса, түгендеу, есептер. Құрастыру құралдарынсыз (build) таза JS.
 * Деректер /api/ арқылы сессиямен; өзгертетін сұраныстарға X-CSRFToken тақырыбы қосылады.
 * Қауіпсіздік: API деректері тек textContent арқылы шығарылады (innerHTML қолданылмайды).
 */
(() => {
  // ===================== Көмекшілер =====================

  const view = document.getElementById('view');

  /** DOM элементі: h('td', {class: 'num', onclick: fn}, 'мәтін', [балалар]) */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else if (key === 'class') el.className = value;
      else if (key in el && typeof value !== 'string') el[key] = value;
      else el.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  class ApiError extends Error {
    constructor(messages, status) {
      super(messages.join('\n'));
      this.messages = messages;
      this.status = status;
    }
  }

  /** DRF қателерінің кез келген пішінін ({detail}, өріс қателері, тізімдер) жолдар тізіміне жинайды. */
  /** API өрістерінің атаулары — қате хабарының алдына қойылады («Себебі: Бұл мән міндетті.») */
  const FIELD_LABELS = {
    items: 'Тауарлар', product_id: 'Тауар', barcode: 'Штрихкод', quantity: 'Саны', price: 'Бағасы',
    comment: 'Түсініктеме', reason: 'Себебі', supplier_id: 'Жеткізуші', product_ids: 'Тауарлар',
    date_from: 'Басы', date_to: 'Соңы', group_by: 'Топтау', warehouse: 'Қойма', doc_type: 'Түрі',
    status: 'Мәртебесі', low: 'Қалдық ≤', search: 'Іздеу',
  };

  function collectMessages(data, prefix = '') {
    if (data === null || data === undefined) return [];
    if (typeof data === 'string') return [prefix + data];
    if (Array.isArray(data)) {
      // Жолдар тізімінің қатесі ({items: [{}, {quantity: [...]}]}) — жол нөмірімен
      const perLine = data.some((x) => x && typeof x === 'object' && !Array.isArray(x));
      return data.flatMap((x, i) => collectMessages(x, perLine ? `${prefix}${i + 1}-жол: ` : prefix));
    }
    if (typeof data === 'object') {
      return Object.entries(data).flatMap(([key, value]) => {
        const label = key === 'detail' || key === 'non_field_errors' ? '' : `${FIELD_LABELS[key] || key}: `;
        return collectMessages(value, prefix + label);
      });
    }
    return [prefix + String(data)];
  }

  async function api(path, { method = 'GET', body, params } = {}) {
    const query = params
      ? '?' + new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v !== null && v !== undefined))
      : '';
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (method !== 'GET') headers['X-CSRFToken'] = window.CSRF_TOKEN;

    let response;
    try {
      response = await fetch('/api' + path + query, {
        method, headers, credentials: 'same-origin',
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      throw new ApiError(['Серверге қосылу мүмкін емес. Желіні тексеріңіз.'], 0);
    }
    if (response.status === 401) {
      // Сессия бітті — қайта кіру
      window.location.href = '/login/?next=' + encodeURIComponent(window.location.pathname + window.location.hash);
      throw new ApiError(['Сессия аяқталды.'], 401);
    }
    const data = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      const messages = collectMessages(data);
      throw new ApiError(messages.length ? messages : [`Қате: HTTP ${response.status}`], response.status);
    }
    return data;
  }

  const moneyFormat = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const qtyFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 });
  const money = (value) => moneyFormat.format(Number(value || 0)) + ' ₸';
  const qty = (value) => qtyFormat.format(Number(value || 0));
  const dateTime = (iso) => (iso ? new Date(iso).toLocaleString('ru-RU') : '—');
  const date = (iso) => (iso ? new Date(iso + (iso.length === 10 ? 'T00:00:00' : '')).toLocaleDateString('ru-RU') : '—');
  const isoToday = () => new Date().toLocaleDateString('sv-SE'); // YYYY-MM-DD, жергілікті уақыт
  const isoMonthStart = () => isoToday().slice(0, 8) + '01';
  const STATUS = { DRAFT: 'Жоба', POSTED: 'Өткізілді', CANCELED: 'Күші жойылды' };
  const statusBadge = (status) => h('span', { class: `badge badge-${status}` }, STATUS[status] || status);
  const signClass = (value) => (Number(value) < 0 ? 'num neg' : Number(value) > 0 ? 'num pos' : 'num');

  function alertBox(messages, kind = 'error') {
    const list = Array.isArray(messages) ? messages : [messages];
    return h('div', { class: `alert alert-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
      list.length === 1 ? list[0] : h('ul', null, list.map((m) => h('li', null, m))));
  }

  let toastTimer;
  function toast(message, kind = 'success') {
    const el = document.getElementById('toast');
    el.className = `toast alert alert-${kind}`;
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
  }

  /** Батырманы сұраныс біткенше бұғаттайды — екі рет жіберуден қорғау. */
  async function busy(button, action) {
    if (button.disabled) return undefined;
    button.disabled = true;
    try {
      return await action();
    } finally {
      button.disabled = false;
    }
  }

  function debounce(fn, ms) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
  }

  function table(head, rows, foot) {
    return h('div', { class: 'table-wrap' },
      h('table', null,
        h('thead', null, h('tr', null, head.map(([title, cls]) => h('th', { class: cls }, title)))),
        h('tbody', null, rows.length ? rows : h('tr', null, h('td', { class: 'empty', colspan: head.length }, 'Деректер жоқ'))),
        foot ? h('tfoot', null, foot) : null));
  }

  /** Кестені CSV (Excel үшін ; бөлгішпен, UTF-8 BOM) ретінде жүктейді. */
  function downloadCsv(tableEl, filename) {
    const escape = (text) => /[";\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    const lines = [...tableEl.querySelectorAll('tr')]
      .filter((tr) => !tr.querySelector('.empty'))
      .map((tr) => [...tr.children].map((cell) => escape(cell.textContent.trim())).join(';'));
    const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    const link = h('a', { href: URL.createObjectURL(blob), download: filename });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  // ===================== Қолданба =====================

  let me = null;

  const SECTIONS = {
    pos: { title: 'Касса', render: renderPos },
    warehouse: { title: 'Қойма', render: renderWarehouse },
    inventory: { title: 'Түгендеу', render: renderInventory },
    documents: { title: 'Құжаттар', render: renderDocuments },
    reports: { title: 'Есептер', render: renderReports },
  };

  async function init() {
    try {
      me = await api('/me/');
    } catch (e) {
      view.replaceChildren(alertBox(e.messages));
      return;
    }
    document.getElementById('user-info').textContent =
      `${me.full_name} · ${me.role_display}${me.warehouse ? ' · ' + me.warehouse.name : ''}`;
    window.addEventListener('hashchange', route);
    route();
  }

  function route() {
    window.onbeforeunload = null; // түгендеу бетінің «сақталмаған өзгеріс» ескертуі тек сол бетте
    const available = me.sections.filter((key) => SECTIONS[key]);
    const [section, ...rest] = window.location.hash.slice(1).split('/');
    const current = available.includes(section) ? section : available[0];

    document.getElementById('tabs').replaceChildren(...available.map((key) =>
      h('button', {
        class: 'tab', role: 'tab', 'aria-selected': String(key === current),
        onclick: () => { window.location.hash = key; },
      }, SECTIONS[key].title)));

    if (!current) {
      view.replaceChildren(alertBox('Сіздің рөліңізге қолжетімді бөлім жоқ.', 'info'));
      return;
    }
    // Касса мен қоймалық операциялар қызметкердің қоймасында жасалады
    const needsWarehouse = current === 'pos' || current === 'warehouse' || (current === 'inventory' && !me.is_manager);
    if (needsWarehouse && !me.warehouse) {
      view.replaceChildren(alertBox('Сізге қойма бекітілмеген. Әкімшіге хабарласыңыз.', 'info'));
      return;
    }
    SECTIONS[current].render(rest);
  }

  // ===================== Касса =====================

  function renderPos(args) {
    const mode = args[0] === 'return' ? 'return' : 'sale';
    const switcher = h('div', { class: 'row' },
      h('button', { class: 'tab', 'aria-selected': String(mode === 'sale'), onclick: () => { window.location.hash = 'pos'; } }, 'Сату'),
      h('button', { class: 'tab', 'aria-selected': String(mode === 'return'), onclick: () => { window.location.hash = 'pos/return'; } }, 'Қайтару'));
    const body = h('div');
    view.replaceChildren(h('div', { class: 'stack' }, switcher, body));
    if (mode === 'sale') renderSale(body);
    else renderReturn(body);
  }

  function renderSale(root) {
    /** productId → {product, quantity} */
    const cart = new Map();
    const scan = h('input', { class: 'scan grow', placeholder: 'Штрихкодты сканерлеңіз немесе енгізіңіз', autocomplete: 'off', 'aria-label': 'Штрихкод' });
    const search = h('input', { class: 'grow', placeholder: 'Атауы немесе артикулы бойынша іздеу', autocomplete: 'off', 'aria-label': 'Тауар іздеу' });
    const results = h('div', { class: 'search-results' });
    const cartBox = h('div');
    const side = h('div', { class: 'stack' });
    let lastReceipt = null;

    root.replaceChildren(h('div', { class: 'grid grid-pos' },
      h('section', { class: 'card stack' },
        h('h2', null, 'Чек'),
        h('div', { class: 'row' }, scan),
        h('div', { class: 'row' }, search),
        results,
        cartBox),
      h('aside', { class: 'card' }, side)));

    function addProduct(product, amount = 1) {
      const line = cart.get(product.id) || { product, quantity: 0 };
      line.quantity = Math.round((line.quantity + amount) * 1000) / 1000;
      line.product = product; // қалдық жаңартылады
      cart.set(product.id, line);
      lastReceipt = null;
      draw();
    }

    scan.addEventListener('keydown', async (event) => {
      if (event.key !== 'Enter') return;
      const code = scan.value.trim();
      scan.value = '';
      if (!code) return;
      try {
        addProduct(await api(`/pos/products/barcode/${encodeURIComponent(code)}/`));
      } catch (e) {
        toast(e.status === 404 ? `Штрихкод табылмады: ${code}` : e.message, 'error');
      }
    });

    search.addEventListener('input', debounce(async () => {
      const term = search.value.trim();
      if (term.length < 2) { results.replaceChildren(); return; }
      try {
        const data = await api('/pos/products/', { params: { search: term } });
        results.replaceChildren(table(
          [['Тауар'], ['Штрихкод'], ['Бағасы', 'num'], ['Қалдық', 'num']],
          data.results.map((product) => h('tr', {
            class: 'clickable', tabindex: '0',
            onclick: () => { addProduct(product); search.value = ''; results.replaceChildren(); scan.focus(); },
            onkeydown: (e) => { if (e.key === 'Enter') e.currentTarget.click(); },
          },
          h('td', null, product.name), h('td', null, product.barcode || '—'),
          h('td', { class: 'num' }, money(product.sale_price)), h('td', { class: 'num' }, qty(product.available))))));
      } catch (e) {
        results.replaceChildren(alertBox(e.messages));
      }
    }, 300));

    function total() {
      let sum = 0;
      for (const { product, quantity } of cart.values()) sum += Number(product.sale_price) * quantity;
      return sum;
    }

    function draw() {
      const rows = [...cart.values()].map(({ product, quantity }) => {
        const input = h('input', {
          class: 'qty', type: 'number', min: '0.001', step: 'any', value: String(quantity), 'aria-label': `${product.name} саны`,
          onchange: () => {
            const value = Number(input.value);
            if (!(value > 0)) { input.value = String(quantity); return; }
            cart.get(product.id).quantity = value;
            draw();
          },
        });
        const short = quantity > Number(product.available);
        return h('tr', null,
          h('td', null, product.name, short ? h('div', { class: 'neg' }, `Қалдық: ${qty(product.available)}`) : null),
          h('td', { class: 'num' }, money(product.sale_price)),
          h('td', { class: 'num' }, input),
          h('td', { class: 'num' }, money(Number(product.sale_price) * quantity)),
          h('td', null, h('button', {
            class: 'btn-icon', title: 'Жою', 'aria-label': `${product.name} жою`,
            onclick: () => { cart.delete(product.id); draw(); },
          }, '✕')));
      });
      cartBox.replaceChildren(table([['Тауар'], ['Бағасы', 'num'], ['Саны', 'num'], ['Сомасы', 'num'], ['']], rows));

      const sellButton = h('button', { class: 'btn btn-primary btn-big', disabled: cart.size === 0 }, 'Сату');
      sellButton.addEventListener('click', () => busy(sellButton, sell));
      const clearButton = h('button', {
        class: 'btn', disabled: cart.size === 0, onclick: () => { cart.clear(); draw(); scan.focus(); },
      }, 'Тазалау');
      side.replaceChildren(
        h('div', { class: 'total' }, h('span', null, 'Жиыны'), h('span', null, money(total()))),
        sellButton, clearButton,
        lastReceipt ? receiptView(lastReceipt, 'Чек') : null);
    }

    async function sell() {
      const items = [...cart.values()].map(({ product, quantity }) => ({ product_id: product.id, quantity: String(quantity) }));
      try {
        lastReceipt = await api('/pos/sales/', { method: 'POST', body: { items } });
        cart.clear();
        toast(`Чек №${lastReceipt.number}: ${money(lastReceipt.total)}`);
      } catch (e) {
        side.append(alertBox(e.messages));
        return;
      }
      draw();
      scan.focus();
    }

    draw();
    scan.focus();
  }

  function receiptView(doc, title) {
    return h('div', { class: 'receipt card' },
      h('h3', null, `${title} №${doc.number}`),
      h('div', { class: 'muted' }, dateTime(doc.posted_at || doc.date)),
      h('hr'),
      doc.items.map((item) => h('div', null,
        h('div', null, item.product_name),
        h('div', { class: 'line' },
          h('span', null, `${qty(item.quantity)} × ${money(item.price)}`), h('span', null, money(item.total))))),
      h('hr'),
      h('div', { class: 'line' }, h('strong', null, 'Жиыны'), h('strong', null, money(doc.total))));
  }

  function renderReturn(root) {
    returnScreen(root, {
      title: 'Сатудан қайтару',
      docLabel: 'Чек',
      placeholder: 'Чек нөмірі, мысалы SAL-000034',
      path: '/pos/sales/',
      soldTitle: 'Сатылды',
      info: (sale) => `${dateTime(sale.posted_at)} · ${sale.cashier} · ${money(sale.total)}`,
    });
  }

  /** Негіз құжатты (чек, қабылдау) нөмірі бойынша тауып, одан қайтару жасайтын экран. */
  function returnScreen(root, config, initialNumber) {
    const number = h('input', { class: 'scan grow', placeholder: config.placeholder, autocomplete: 'off', 'aria-label': `${config.docLabel} нөмірі` });
    const findButton = h('button', { class: 'btn btn-primary' }, 'Табу');
    const body = h('div', { class: 'stack' });
    root.replaceChildren(h('section', { class: 'card stack' },
      h('h2', null, config.title),
      h('div', { class: 'row' }, number, findButton),
      body));

    async function find() {
      const value = number.value.trim().toUpperCase();
      if (!value) return;
      try {
        const data = await api(config.path, { params: { search: value } });
        if (!data.results.length) { body.replaceChildren(alertBox(`${config.docLabel} табылмады: ${value}`)); return; }
        drawSale(data.results[0]);
      } catch (e) {
        body.replaceChildren(alertBox(e.messages));
      }
    }
    findButton.addEventListener('click', () => busy(findButton, find));
    number.addEventListener('keydown', (e) => { if (e.key === 'Enter') busy(findButton, find); });

    function drawSale(sale, notice) {
      const inputs = new Map();
      const rows = sale.items.map((item) => {
        const left = Number(sale.returnable[String(item.product_id)] || 0);
        const input = h('input', {
          class: 'qty', type: 'number', min: '0', max: String(left), step: 'any', value: '0',
          disabled: left <= 0, 'aria-label': `${item.product_name} қайтару саны`,
        });
        inputs.set(item.product_id, input);
        return h('tr', null,
          h('td', null, item.product_name),
          h('td', { class: 'num' }, money(item.price)),
          h('td', { class: 'num' }, qty(item.quantity)),
          h('td', { class: 'num' }, qty(left)),
          h('td', { class: 'num' }, input));
      });
      const reason = h('input', { class: 'grow', maxlength: '500', placeholder: 'Мысалы: ақаулы, сәйкес келмеді', autocomplete: 'off' });
      const returnButton = h('button', { class: 'btn btn-primary', disabled: sale.status !== 'POSTED' }, 'Қайтаруды өткізу');
      const message = h('div');
      returnButton.addEventListener('click', () => busy(returnButton, async () => {
        const items = [...inputs].filter(([, input]) => Number(input.value) > 0)
          .map(([productId, input]) => ({ product_id: productId, quantity: input.value }));
        if (!items.length) { message.replaceChildren(alertBox('Қайтаратын санды енгізіңіз.')); return; }
        try {
          const result = await api(`${config.path}${sale.id}/returns/`, { method: 'POST', body: { items, comment: reason.value } });
          toast(`Қайтару №${result.number}: ${money(result.total)}`);
          drawSale(await api(`${config.path}${sale.id}/`), receiptView(result, 'Қайтару'));
        } catch (e) {
          message.replaceChildren(alertBox(e.messages));
        }
      }));

      body.replaceChildren(
        h('div', { class: 'row' },
          h('strong', null, `${config.docLabel} №${sale.number}`), statusBadge(sale.status),
          h('span', { class: 'muted' }, config.info(sale))),
        sale.status !== 'POSTED' ? alertBox('Өткізілмеген немесе күші жойылған құжаттан қайтаруға болмайды.', 'info') : null,
        table([['Тауар'], ['Бағасы', 'num'], [config.soldTitle, 'num'], ['Қайтаруға болады', 'num'], ['Қайтару', 'num']], rows),
        sale.status === 'POSTED' ? h('div', { class: 'row' }, h('label', { class: 'grow' }, 'Қайтару себебі', reason), returnButton) : null,
        message,
        sale.returns.length ? h('div', { class: 'muted' },
          'Бұрынғы қайтарулар: ', sale.returns.map((r) => `№${r.number} (${STATUS[r.status]}, ${money(r.total)}`
            + `${r.comment ? ` — ${r.comment}` : ''}${r.cancel_reason ? `; күшін жою себебі: ${r.cancel_reason}` : ''})`).join(', ')) : null,
        notice || null);
    }

    if (initialNumber) {
      number.value = initialNumber;
      busy(findButton, find);
    } else {
      number.focus();
    }
  }

  // ===================== Қойма =====================

  const WAREHOUSE_MODES = {
    receipt: { title: 'Қабылдау', render: renderReceipt },
    'supplier-return': { title: 'Жеткізушіге қайтару', render: renderSupplierReturn },
    'write-off': { title: 'Есептен шығару', render: renderWriteOff },
  };

  function renderWarehouse(args) {
    const mode = WAREHOUSE_MODES[args[0]] ? args[0] : 'receipt';
    const switcher = h('div', { class: 'row' }, Object.entries(WAREHOUSE_MODES).map(([key, m]) =>
      h('button', { class: 'tab', 'aria-selected': String(key === mode), onclick: () => { window.location.hash = `warehouse/${key}`; } }, m.title)));
    const body = h('div');
    view.replaceChildren(h('div', { class: 'stack' }, switcher, body));
    WAREHOUSE_MODES[mode].render(body, args.slice(1));
  }

  /** Тауар жолдарын жинау: сканер + іздеу, саны және (withPrice болса) бағасы. */
  function lineEditor({ withPrice = false, onChange = () => {} } = {}) {
    /** productId → {product, quantity, price} */
    const lines = new Map();
    const scan = h('input', { class: 'scan grow', placeholder: 'Штрихкодты сканерлеңіз немесе енгізіңіз', autocomplete: 'off', 'aria-label': 'Штрихкод' });
    const search = h('input', { class: 'grow', placeholder: 'Атауы немесе артикулы бойынша іздеу', autocomplete: 'off', 'aria-label': 'Тауар іздеу' });
    const results = h('div', { class: 'search-results' });
    const box = h('div');

    function add(product) {
      const line = lines.get(product.id) || { product, quantity: 0, price: '' };
      line.quantity = Math.round((line.quantity + 1) * 1000) / 1000;
      line.product = product;
      lines.set(product.id, line);
      draw();
    }

    scan.addEventListener('keydown', async (event) => {
      if (event.key !== 'Enter') return;
      const code = scan.value.trim();
      scan.value = '';
      if (!code) return;
      try {
        add(await api(`/pos/products/barcode/${encodeURIComponent(code)}/`));
      } catch (e) {
        toast(e.status === 404 ? `Штрихкод табылмады: ${code}` : e.message, 'error');
      }
    });

    search.addEventListener('input', debounce(async () => {
      const term = search.value.trim();
      if (term.length < 2) { results.replaceChildren(); return; }
      try {
        const data = await api('/pos/products/', { params: { search: term } });
        results.replaceChildren(table([['Тауар'], ['Штрихкод'], ['Қалдық', 'num']],
          data.results.map((product) => h('tr', {
            class: 'clickable', tabindex: '0',
            onclick: () => { add(product); search.value = ''; results.replaceChildren(); scan.focus(); },
            onkeydown: (e) => { if (e.key === 'Enter') e.currentTarget.click(); },
          }, h('td', null, product.name), h('td', null, product.barcode || '—'), h('td', { class: 'num' }, qty(product.available))))));
      } catch (e) {
        results.replaceChildren(alertBox(e.messages));
      }
    }, 300));

    function draw() {
      const rows = [...lines.values()].map((line) => {
        const { product } = line;
        const quantity = h('input', {
          class: 'qty', type: 'number', min: '0.001', step: 'any', value: String(line.quantity), 'aria-label': `${product.name} саны`,
          onchange: () => {
            const value = Number(quantity.value);
            if (!(value > 0)) { quantity.value = String(line.quantity); return; }
            line.quantity = value;
            draw();
          },
        });
        const price = withPrice ? h('input', {
          class: 'qty', type: 'number', min: '0', step: '0.01', value: line.price, placeholder: '0.00', 'aria-label': `${product.name} бағасы`,
          onchange: () => { line.price = price.value; draw(); },
        }) : null;
        // Кіріс қалдықты арттырады — тек шығыста ескертеміз
        const short = !withPrice && line.quantity > Number(product.available);
        return h('tr', null,
          h('td', null, product.name,
            h('div', { class: short ? 'neg' : 'muted' }, `Қалдық: ${qty(product.available)}`)),
          h('td', { class: 'num' }, quantity),
          withPrice ? h('td', { class: 'num' }, price) : null,
          withPrice ? h('td', { class: 'num' }, line.price === '' ? '—' : money(Number(line.price) * line.quantity)) : null,
          h('td', null, h('button', {
            class: 'btn-icon', title: 'Жою', 'aria-label': `${product.name} жою`,
            onclick: () => { lines.delete(product.id); draw(); },
          }, '✕')));
      });
      const head = withPrice
        ? [['Тауар'], ['Саны', 'num'], ['Бағасы', 'num'], ['Сомасы', 'num'], ['']]
        : [['Тауар'], ['Саны', 'num'], ['']];
      box.replaceChildren(table(head, rows));
      if (ready) onChange();
    }

    let ready = false;
    draw();
    // Шақырушы қайтарылған объектіні айнымалыға жазып үлгерсін
    ready = true;
    queueMicrotask(onChange);
    return {
      element: h('div', { class: 'stack' }, h('div', { class: 'row' }, scan), h('div', { class: 'row' }, search), results, box),
      get size() { return lines.size; },
      total: () => [...lines.values()].reduce((sum, l) => sum + Number(l.price || 0) * l.quantity, 0),
      /** Бағасы енгізілмеген жолдар (withPrice болғанда) */
      missingPrices: () => [...lines.values()].filter((l) => withPrice && l.price === '').map((l) => l.product.name),
      items: () => [...lines.values()].map((l) => ({
        product_id: l.product.id, quantity: String(l.quantity), ...(withPrice ? { price: String(l.price) } : {}),
      })),
      clear: () => { lines.clear(); draw(); },
      focus: () => scan.focus(),
    };
  }

  /** Қойма құжаттарының соңғы тізімі (жаңасы бірінші). */
  async function recentDocuments(box, path, columns, onOpen) {
    box.replaceChildren(h('p', { class: 'muted' }, 'Жүктелуде…'));
    try {
      const data = await api(path);
      box.replaceChildren(table(
        [['Нөмірі'], ['Күні'], ...columns.map(([title]) => [title]), ['Мәртебесі'], ['Сомасы', 'num']],
        data.results.map((doc) => h('tr', onOpen ? {
          class: 'clickable', tabindex: '0', onclick: () => onOpen(doc),
          onkeydown: (e) => { if (e.key === 'Enter') e.currentTarget.click(); },
        } : null,
        h('td', null, doc.number), h('td', null, dateTime(doc.posted_at || doc.date)),
        columns.map(([, value]) => h('td', null, value(doc))),
        h('td', null, statusBadge(doc.status)), h('td', { class: 'num' }, money(doc.total))))));
    } catch (e) {
      box.replaceChildren(alertBox(e.messages));
    }
  }

  async function loadAll(path) {
    const all = [];
    for (let page = 1; ; page += 1) {
      const data = await api(path, { params: { page } });
      all.push(...data.results);
      if (!data.next) return all;
    }
  }

  function renderReceipt(root) {
    const supplier = h('select', { 'aria-label': 'Жеткізуші' }, h('option', { value: '' }, 'Жүктелуде…'));
    const comment = h('input', { class: 'grow', maxlength: '500', placeholder: 'Мысалы: накладной №17', autocomplete: 'off' });
    const totalBox = h('div', { class: 'total' });
    const submit = h('button', { class: 'btn btn-primary btn-big' }, 'Қабылдау');
    const side = h('div', { class: 'stack' });
    const recent = h('div');
    const editor = lineEditor({
      withPrice: true,
      onChange: () => {
        totalBox.replaceChildren(h('span', null, 'Жиыны'), h('span', null, money(editor.total())));
        submit.disabled = editor.size === 0;
      },
    });

    root.replaceChildren(h('div', { class: 'stack' },
      h('div', { class: 'grid grid-pos' },
        h('section', { class: 'card stack' },
          h('h2', null, `Жеткізушіден қабылдау · ${me.warehouse.name}`),
          h('div', { class: 'row' }, h('label', { class: 'grow' }, 'Жеткізуші', supplier), h('label', { class: 'grow' }, 'Түсініктеме', comment)),
          editor.element),
        h('aside', { class: 'card stack' }, totalBox, submit, side)),
      h('section', { class: 'card stack' }, h('h2', null, 'Соңғы қабылдаулар'),
        h('p', { class: 'muted' }, 'Жеткізушіге қайтару үшін қабылдауды басыңыз.'), recent)));

    loadAll('/warehouse/suppliers/').then((suppliers) => {
      supplier.replaceChildren(h('option', { value: '' }, '— таңдаңыз —'),
        suppliers.map((s) => h('option', { value: String(s.id) }, s.bin ? `${s.name} (БСН ${s.bin})` : s.name)));
      if (!suppliers.length) side.replaceChildren(alertBox('Жеткізушілер жоқ — әкімшіге хабарласыңыз.', 'info'));
    }).catch((e) => side.replaceChildren(alertBox(e.messages)));

    const showRecent = () => recentDocuments(recent, '/warehouse/receipts/', [['Жеткізуші', (d) => d.supplier]],
      (doc) => { window.location.hash = `warehouse/supplier-return/${doc.number}`; });

    submit.addEventListener('click', () => busy(submit, async () => {
      const errors = [];
      if (!supplier.value) errors.push('Жеткізушіні таңдаңыз.');
      const missing = editor.missingPrices();
      if (missing.length) errors.push(`Бағасы енгізілмеген: ${missing.join(', ')}.`);
      if (errors.length) { side.replaceChildren(alertBox(errors)); return; }
      try {
        const result = await api('/warehouse/receipts/', {
          method: 'POST', body: { supplier_id: Number(supplier.value), items: editor.items(), comment: comment.value },
        });
        toast(`Қабылдау №${result.number}: ${money(result.total)}`);
        editor.clear();
        comment.value = '';
        side.replaceChildren(receiptView(result, 'Қабылдау'));
        showRecent();
        editor.focus();
      } catch (e) {
        side.replaceChildren(alertBox(e.messages));
      }
    }));

    showRecent();
    editor.focus();
  }

  function renderSupplierReturn(root, args) {
    returnScreen(root, {
      title: 'Жеткізушіге қайтару',
      docLabel: 'Қабылдау',
      placeholder: 'Қабылдау нөмірі, мысалы RCV-000001',
      path: '/warehouse/receipts/',
      soldTitle: 'Қабылданды',
      info: (receipt) => `${dateTime(receipt.posted_at)} · ${receipt.supplier} · ${money(receipt.total)}`,
    }, args[0] ? decodeURIComponent(args[0]) : '');
  }

  function renderWriteOff(root) {
    const reason = h('input', { class: 'grow', maxlength: '500', required: true, placeholder: 'Мысалы: бүлінген, мерзімі өткен', autocomplete: 'off' });
    const submit = h('button', { class: 'btn btn-primary' }, 'Есептен шығару');
    const message = h('div');
    const recent = h('div');
    const editor = lineEditor({ onChange: () => { submit.disabled = editor.size === 0; } });

    root.replaceChildren(h('div', { class: 'stack' },
      h('section', { class: 'card stack' },
        h('h2', null, `Есептен шығару · ${me.warehouse.name}`),
        editor.element,
        h('div', { class: 'row' }, h('label', { class: 'grow' }, 'Себебі (міндетті)', reason), submit),
        message),
      h('section', { class: 'card stack' }, h('h2', null, 'Соңғы есептен шығарулар'), recent)));

    const showRecent = () => recentDocuments(recent, '/warehouse/write-offs/',
      [['Себебі', (d) => d.comment || '—'], ['Жауапты', (d) => d.responsible]]);

    submit.addEventListener('click', () => busy(submit, async () => {
      if (!reason.value.trim()) { message.replaceChildren(alertBox('Есептен шығару себебін көрсетіңіз.')); reason.focus(); return; }
      if (!window.confirm('Тауарлар қалдықтан шығарылады. Жалғастырасыз ба?')) return;
      try {
        const result = await api('/warehouse/write-offs/', { method: 'POST', body: { items: editor.items(), comment: reason.value } });
        toast(`Есептен шығару №${result.number}: ${money(result.total)}`);
        editor.clear();
        reason.value = '';
        message.replaceChildren(receiptView(result, 'Есептен шығару'));
        showRecent();
        editor.focus();
      } catch (e) {
        message.replaceChildren(alertBox(e.messages));
      }
    }));

    showRecent();
    editor.focus();
  }

  // ===================== Түгендеу =====================

  function renderInventory(args) {
    if (args[0]) renderAudit(Number(args[0]));
    else renderAuditList();
  }

  async function renderAuditList() {
    const listBox = h('div', null, h('p', { class: 'muted' }, 'Жүктелуде…'));
    view.replaceChildren(h('div', { class: 'stack' },
      startAuditCard(),
      h('section', { class: 'card stack' }, h('h2', null, 'Түгендеулер'), listBox)));
    try {
      const data = await api('/inventory/audits/');
      listBox.replaceChildren(table(
        [['Нөмірі'], ['Күні'], ['Қойма'], ['Мәртебесі'], ['Саналды', 'num'], ['Жауапты']],
        data.results.map((audit) => h('tr', {
          class: 'clickable', tabindex: '0',
          onclick: () => { window.location.hash = `inventory/${audit.id}`; },
          onkeydown: (e) => { if (e.key === 'Enter') e.currentTarget.click(); },
        },
        h('td', null, audit.number), h('td', null, dateTime(audit.date)), h('td', null, audit.warehouse),
        h('td', null, statusBadge(audit.status)),
        h('td', { class: 'num' }, `${audit.progress.counted} / ${audit.progress.total}`),
        h('td', null, audit.responsible)))));
    } catch (e) {
      listBox.replaceChildren(alertBox(e.messages));
    }
  }

  function startAuditCard() {
    if (!me.warehouse) {
      return h('section', { class: 'card' }, alertBox('Түгендеуді бастау үшін сізге қойма бекітілуі керек.', 'info'));
    }
    /** productId → product */
    const selected = new Map();
    const search = h('input', { class: 'grow', placeholder: 'Түгенделетін тауарды іздеу', autocomplete: 'off', 'aria-label': 'Тауар іздеу' });
    const results = h('div', { class: 'search-results' });
    const selectedBox = h('div', { class: 'muted' });
    const startSelected = h('button', { class: 'btn btn-primary', disabled: true }, 'Таңдалғандарды түгендеу');
    const startAll = h('button', { class: 'btn' }, 'Барлық тауарды түгендеу');
    const comment = h('input', { class: 'grow', maxlength: '500', placeholder: 'Мысалы: жоспарлы, ауысым тапсыру', autocomplete: 'off' });
    const message = h('div');

    function drawSelected() {
      startSelected.disabled = selected.size === 0;
      selectedBox.replaceChildren(selected.size
        ? ['Таңдалды: ', [...selected.values()].map((product) => h('span', { class: 'badge' }, product.name,
          h('button', { class: 'btn-icon', 'aria-label': `${product.name} алып тастау`, onclick: () => { selected.delete(product.id); drawSelected(); } }, '✕')))]
        : 'Тауар таңдалмаған.');
    }

    search.addEventListener('input', debounce(async () => {
      const term = search.value.trim();
      if (term.length < 2) { results.replaceChildren(); return; }
      try {
        const data = await api('/pos/products/', { params: { search: term } });
        results.replaceChildren(table([['Тауар'], ['Штрихкод'], ['Қалдық', 'num']],
          data.results.map((product) => h('tr', {
            class: 'clickable', tabindex: '0',
            onclick: () => { selected.set(product.id, product); drawSelected(); },
            onkeydown: (e) => { if (e.key === 'Enter') e.currentTarget.click(); },
          }, h('td', null, product.name), h('td', null, product.barcode || '—'), h('td', { class: 'num' }, qty(product.available))))));
      } catch (e) {
        results.replaceChildren(alertBox(e.messages));
      }
    }, 300));

    async function start(payload) {
      try {
        const audit = await api('/inventory/audits/', { method: 'POST', body: { ...payload, comment: comment.value } });
        toast(`Түгендеу №${audit.number} басталды. Тауарлар бойынша қозғалыс бұғатталды.`);
        window.location.hash = `inventory/${audit.id}`;
      } catch (e) {
        message.replaceChildren(alertBox(e.messages));
      }
    }
    startSelected.addEventListener('click', () => busy(startSelected, () => start({ product_ids: [...selected.keys()] })));
    startAll.addEventListener('click', () => busy(startAll, () => {
      if (!window.confirm('Қоймадағы барлық белсенді тауар түгенделеді және бекітілгенше олармен сату тоқтайды. Жалғастырасыз ба?')) return undefined;
      return start({ all: true });
    }));

    drawSelected();
    return h('section', { class: 'card stack' },
      h('h2', null, `Жаңа түгендеу · ${me.warehouse.name}`),
      h('p', { class: 'muted' }, 'Басталған сәттен бастап таңдалған тауарлар бойынша сату, қабылдау және басқа қозғалыстар бекітілгенше тоқтайды.'),
      h('div', { class: 'row' }, search), results, selectedBox,
      h('div', { class: 'row' }, h('label', { class: 'grow' }, 'Себебі (міндетті емес)', comment)),
      h('div', { class: 'row' }, startSelected, startAll), message);
  }

  async function renderAudit(id) {
    view.replaceChildren(h('p', { class: 'muted' }, 'Жүктелуде…'));
    let audit;
    try {
      audit = await api(`/inventory/audits/${id}/`);
    } catch (e) {
      view.replaceChildren(alertBox(e.messages));
      return;
    }

    const editable = audit.status === 'DRAFT';
    /** productId → енгізілген сан (жол); тек өзгертілгендері сақталады */
    const counts = new Map();
    const dirty = new Set();
    for (const item of audit.items) counts.set(item.product_id, item.counted_quantity ?? '');

    const byBarcode = new Map(audit.items.filter((i) => i.barcode).map((i) => [i.barcode, i]));
    const scan = h('input', { class: 'scan grow', placeholder: 'Штрихкодты сканерлеңіз (+1 немесе көрсетілген сан)', autocomplete: 'off', 'aria-label': 'Штрихкод' });
    const step = h('input', { class: 'qty', type: 'number', min: '0', step: 'any', value: '1', 'aria-label': 'Әр сканерлеуге қосылатын сан' });
    const tableBox = h('div');
    const message = h('div');
    const inputs = new Map();

    function drawTable() {
      inputs.clear();
      const rows = audit.items.map((item) => {
        const value = counts.get(item.product_id);
        const counted = value !== '' && value !== null;
        const difference = counted ? Number(value) - Number(item.expected_quantity ?? 0) : null;
        const input = h('input', {
          class: 'qty', type: 'number', min: '0', step: 'any', value: counted ? String(value) : '',
          disabled: !editable, placeholder: '—', 'aria-label': `${item.product_name} нақты саны`,
          oninput: () => { counts.set(item.product_id, input.value); dirty.add(item.product_id); drawSummary(); },
          onchange: () => drawTable(),
        });
        inputs.set(item.product_id, input);
        return h('tr', { class: difference ? 'diff-row' : null },
          h('td', null, item.product_name, h('div', { class: 'muted' }, item.barcode || item.sku)),
          h('td', { class: 'num' }, item.expected_quantity === null ? '—' : qty(item.expected_quantity)),
          h('td', { class: 'num' }, input),
          h('td', { class: difference === null ? 'num' : signClass(difference) },
            difference === null ? '—' : (difference > 0 ? '+' : '') + qty(difference)));
      });
      tableBox.replaceChildren(table([['Тауар'], ['Есептік', 'num'], ['Нақты', 'num'], ['Айырма', 'num']], rows));
      drawSummary();
    }

    const summary = h('div', { class: 'muted' });
    function drawSummary() {
      const counted = [...counts.values()].filter((v) => v !== '' && v !== null).length;
      summary.textContent = `Саналды: ${counted} / ${audit.items.length}` + (dirty.size ? ` · сақталмаған өзгеріс: ${dirty.size}` : '');
      saveButton.disabled = dirty.size === 0;
    }

    scan.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      const code = scan.value.trim();
      scan.value = '';
      if (!code) return;
      const item = byBarcode.get(code);
      if (!item) { toast(`Бұл тауар түгендеуде жоқ: ${code}`, 'error'); return; }
      const current = Number(counts.get(item.product_id) || 0);
      counts.set(item.product_id, String(Math.round((current + Number(step.value || 1)) * 1000) / 1000));
      dirty.add(item.product_id);
      drawTable();
      inputs.get(item.product_id)?.scrollIntoView({ block: 'nearest' });
    });

    async function save() {
      const items = [...dirty].filter((productId) => counts.get(productId) !== '')
        .map((productId) => ({ product_id: productId, quantity: String(counts.get(productId)) }));
      if (!items.length) return true;
      try {
        audit = await api(`/inventory/audits/${id}/counts/`, { method: 'POST', body: { items } });
        dirty.clear();
        for (const item of audit.items) counts.set(item.product_id, item.counted_quantity ?? '');
        message.replaceChildren();
        drawTable();
        return true;
      } catch (e) {
        message.replaceChildren(alertBox(e.messages));
        return false;
      }
    }

    const saveButton = h('button', { class: 'btn btn-primary', disabled: true }, 'Сақтау');
    saveButton.addEventListener('click', () => busy(saveButton, async () => { if (await save()) toast('Сандар сақталды.'); }));

    // Бекіткенде Басқарушы айырманың түсіндірмесін жаза алады (бастағандағы түсініктеме алдын ала толтырылған)
    const comment = h('input', { class: 'grow', maxlength: '500', value: audit.comment, placeholder: 'Айырманың түсіндірмесі', autocomplete: 'off' });
    const approveButton = h('button', { class: 'btn btn-primary' }, 'Бекіту');
    approveButton.addEventListener('click', () => busy(approveButton, async () => {
      if (!window.confirm('Қалдықтар нақты санға теңестіріледі, айырма журналға жазылады. Бекітесіз бе?')) return;
      if (!(await save())) return;
      try {
        await api(`/inventory/audits/${id}/approve/`, { method: 'POST', body: { comment: comment.value } });
        toast(`Түгендеу №${audit.number} бекітілді.`);
        renderAudit(id);
      } catch (e) {
        message.replaceChildren(alertBox(e.messages));
      }
    }));

    // Өткізілген түгендеудің күшін жою себебі міндетті (сервер де тексереді)
    const posted = audit.status === 'POSTED';
    const cancelReason = h('input', {
      class: 'grow', maxlength: '500', autocomplete: 'off',
      placeholder: posted ? 'Күшін жою себебі (міндетті)' : 'Күшін жою себебі (міндетті емес)',
    });
    const cancelButton = h('button', { class: 'btn btn-danger' }, 'Күшін жою');
    cancelButton.addEventListener('click', () => busy(cancelButton, async () => {
      if (posted && !cancelReason.value.trim()) {
        message.replaceChildren(alertBox('Өткізілген түгендеудің күшін жою себебін көрсетіңіз.'));
        cancelReason.focus();
        return;
      }
      const question = posted
        ? 'Түгендеудің күшін жоясыз ба? Қалдықтар түгендеуге дейінгі күйге қайтарылады.'
        : 'Түгендеудің күшін жоясыз ба? Тауарлар бойынша бұғат алынады.';
      if (!window.confirm(question)) return;
      try {
        await api(`/inventory/audits/${id}/cancel/`, { method: 'POST', body: { reason: cancelReason.value } });
        toast(`Түгендеу №${audit.number}: күші жойылды.`);
        renderAudit(id);
      } catch (e) {
        message.replaceChildren(alertBox(e.messages));
      }
    }));

    view.replaceChildren(h('section', { class: 'card stack' },
      h('div', { class: 'row' },
        h('button', { class: 'btn-link', onclick: () => { window.location.hash = 'inventory'; } }, '← Тізім'),
        h('h2', { style: 'margin:0' }, `Түгендеу №${audit.number}`), statusBadge(audit.status),
        h('span', { class: 'muted' }, `${audit.warehouse} · ${dateTime(audit.date)} · ${audit.responsible}`)),
      audit.comment && !(editable && me.is_manager) ? h('p', null, h('span', { class: 'muted' }, 'Себебі: '), audit.comment) : null,
      audit.status === 'CANCELED' ? alertBox(
        `Күші жойылды: ${dateTime(audit.canceled_at)}${audit.canceled_by ? ` · ${audit.canceled_by}` : ''}`
        + (audit.cancel_reason ? ` · Себебі: ${audit.cancel_reason}` : ''), 'info') : null,
      editable ? h('div', { class: 'row' }, scan, h('label', null, 'Қадам', step)) : null,
      tableBox,
      editable && me.is_manager ? h('div', { class: 'row' }, h('label', { class: 'grow' }, 'Түсініктеме', comment)) : null,
      h('div', { class: 'row' }, summary, h('span', { class: 'spacer' }),
        editable ? saveButton : null,
        editable && me.is_manager ? approveButton : null),
      audit.status !== 'CANCELED' && me.is_manager ? h('div', { class: 'row' }, cancelReason, cancelButton) : null,
      editable && !me.is_manager ? h('p', { class: 'muted' }, 'Санауды аяқтаған соң Басқарушы бекітеді.') : null,
      message));

    window.onbeforeunload = () => (dirty.size ? true : undefined);
    drawTable();
    if (editable) scan.focus();
  }

  // ===================== Құжаттар (Басқарушы) =====================

  const DOC_TYPES = {
    SALE: 'Сату', SALE_RETURN: 'Сатушылық қайтару', RECEIPT: 'Жеткізушіден қабылдау',
    SUPPLIER_RETURN: 'Жеткізушіге қайтару', WRITE_OFF: 'Есептен шығару', INVENTORY: 'Түгендеу',
  };
  /** Тізім беті ауысқанда сүзгілер сақталады */
  const documentFilters = { doc_type: '', status: '', search: '', date_from: '', date_to: '', page: 1 };

  function renderDocuments(args) {
    if (args[0]) renderDocument(Number(args[0]));
    else renderDocumentList();
  }

  const docLink = (doc) => h('button', {
    class: 'btn-link', onclick: () => { window.location.hash = `documents/${doc.id}`; },
  }, `№${doc.number}`);

  function renderDocumentList() {
    const f = documentFilters;
    const select = (options, value, label) => h('select', { 'aria-label': label },
      h('option', { value: '' }, 'Барлығы'), Object.entries(options).map(([k, v]) => h('option', { value: k, selected: k === value }, v)));
    const docType = select(DOC_TYPES, f.doc_type, 'Түрі');
    const status = select(STATUS, f.status, 'Мәртебесі');
    const search = h('input', { value: f.search, placeholder: 'мыс. SAL-000034', autocomplete: 'off' });
    const dateFrom = h('input', { type: 'date', value: f.date_from });
    const dateTo = h('input', { type: 'date', value: f.date_to });
    const showButton = h('button', { class: 'btn btn-primary' }, 'Көрсету');
    const output = h('div', { class: 'stack' });
    const field = (label, control) => h('label', null, label, control);

    async function show(page = 1) {
      Object.assign(f, {
        doc_type: docType.value, status: status.value, search: search.value.trim(),
        date_from: dateFrom.value, date_to: dateTo.value, page,
      });
      try {
        const data = await api('/documents/', { params: f });
        output.replaceChildren(
          table([['Нөмірі'], ['Күні'], ['Түрі'], ['Мәртебесі'], ['Қойма'], ['Жауапты'], ['Сомасы', 'num']],
            data.results.map((d) => h('tr', {
              class: 'clickable', tabindex: '0',
              onclick: () => { window.location.hash = `documents/${d.id}`; },
              onkeydown: (e) => { if (e.key === 'Enter') e.currentTarget.click(); },
            },
            h('td', null, d.number), h('td', null, dateTime(d.date)), h('td', null, d.doc_type_display),
            h('td', null, statusBadge(d.status)), h('td', null, d.warehouse), h('td', null, d.responsible),
            h('td', { class: 'num' }, money(d.total))))),
          h('div', { class: 'row' },
            h('span', { class: 'muted' }, `Барлығы: ${data.count}`), h('span', { class: 'spacer' }),
            h('button', { class: 'btn', disabled: !data.previous, onclick: () => show(page - 1) }, '← Алдыңғы'),
            h('button', { class: 'btn', disabled: !data.next, onclick: () => show(page + 1) }, 'Келесі →')));
      } catch (e) {
        output.replaceChildren(alertBox(e.messages));
      }
    }
    showButton.addEventListener('click', () => busy(showButton, () => show()));
    search.addEventListener('keydown', (e) => { if (e.key === 'Enter') busy(showButton, () => show()); });

    view.replaceChildren(h('div', { class: 'stack' },
      h('section', { class: 'card stack' }, h('h2', null, 'Құжаттар'),
        h('div', { class: 'row' }, field('Түрі', docType), field('Мәртебесі', status), field('Нөмірі', search),
          field('Басы', dateFrom), field('Соңы', dateTo), showButton)),
      h('section', { class: 'card' }, output)));
    show(f.page);
  }

  async function renderDocument(id) {
    view.replaceChildren(h('p', { class: 'muted' }, 'Жүктелуде…'));
    let doc;
    try {
      doc = await api(`/documents/${id}/`);
    } catch (e) {
      view.replaceChildren(alertBox(e.messages));
      return;
    }
    const info = (label, value) => (value ? h('div', null, h('span', { class: 'muted' }, `${label}: `), value) : null);
    const message = h('div');
    const reason = h('input', {
      class: 'grow', maxlength: '500', autocomplete: 'off',
      placeholder: doc.status === 'POSTED' ? 'Күшін жою себебі (міндетті)' : 'Күшін жою себебі (міндетті емес)',
    });
    const cancelButton = h('button', { class: 'btn btn-danger' }, 'Күшін жою');
    cancelButton.addEventListener('click', () => busy(cancelButton, async () => {
      if (doc.status === 'POSTED' && !reason.value.trim()) {
        message.replaceChildren(alertBox('Өткізілген құжаттың күшін жою себебін көрсетіңіз.'));
        reason.focus();
        return;
      }
      const question = doc.status === 'POSTED'
        ? `№${doc.number} құжатының күшін жоясыз ба? Кері қозғалыстар жазылып, қалдық құжатқа дейінгі күйге қайтады.`
        : `№${doc.number} жобасының күшін жоясыз ба?`;
      if (!window.confirm(question)) return;
      try {
        await api(`/documents/${id}/cancel/`, { method: 'POST', body: { reason: reason.value } });
        toast(`№${doc.number}: күші жойылды.`);
        renderDocument(id);
      } catch (e) {
        message.replaceChildren(alertBox(e.messages));
      }
    }));

    const postedReturns = doc.returns.filter((r) => r.status === 'POSTED');
    view.replaceChildren(h('section', { class: 'card stack' },
      h('div', { class: 'row' },
        h('button', { class: 'btn-link', onclick: () => { window.location.hash = 'documents'; } }, '← Тізім'),
        h('h2', { style: 'margin:0' }, `${doc.doc_type_display} №${doc.number}`), statusBadge(doc.status)),
      h('div', { class: 'stack' },
        info('Қойма', doc.warehouse),
        info('Жеткізуші', doc.supplier),
        info('Жауапты', doc.responsible),
        info('Жасалды', dateTime(doc.date)),
        info('Өткізілді', doc.posted_at && dateTime(doc.posted_at)),
        info('Түсініктеме', doc.comment),
        doc.base_document ? h('div', null, h('span', { class: 'muted' }, 'Негіз құжат: '), docLink(doc.base_document)) : null,
        doc.returns.length ? h('div', null, h('span', { class: 'muted' }, 'Қайтарулар: '),
          doc.returns.map((r) => [docLink(r), ` (${STATUS[r.status]}) `])) : null),
      doc.status === 'CANCELED' ? alertBox(
        `Күші жойылды: ${dateTime(doc.canceled_at)}${doc.canceled_by ? ` · ${doc.canceled_by}` : ''}`
        + (doc.cancel_reason ? ` · Себебі: ${doc.cancel_reason}` : ''), 'info') : null,
      table([['Тауар'], ['Саны', 'num'], ['Бағасы', 'num'], ['Сомасы', 'num']],
        doc.items.map((item) => h('tr', null, h('td', null, item.product_name), h('td', { class: 'num' }, qty(item.quantity)),
          h('td', { class: 'num' }, money(item.price)), h('td', { class: 'num' }, money(item.total)))),
        totalRow([['Жиыны'], [''], [''], [money(doc.total), 'num']])),
      doc.status !== 'CANCELED' && postedReturns.length
        ? alertBox('Бұл құжаттың өткізілген қайтарулары бар — алдымен солардың күшін жойыңыз.', 'info') : null,
      doc.status !== 'CANCELED' ? h('div', { class: 'row' }, reason, cancelButton) : null,
      message));
  }

  // ===================== Есептер =====================

  const REPORTS = {
    stock: { title: 'Қалдықтар', period: false },
    turnover: { title: 'Айналым ведомосі', period: true },
    sales: { title: 'Сату есебі', period: true, managerOnly: true },
    inventory: { title: 'Түгендеу нәтижелері', period: true },
    movements: { title: 'Қозғалыс журналы', period: true },
  };

  async function renderReports(args) {
    const available = Object.entries(REPORTS).filter(([, r]) => !r.managerOnly || me.is_manager);
    const report = h('select', { 'aria-label': 'Есеп' }, available.map(([key, r]) => h('option', { value: key }, r.title)));
    if (args[0] && REPORTS[args[0]]) report.value = args[0];
    const dateFrom = h('input', { type: 'date', value: isoMonthStart() });
    const dateTo = h('input', { type: 'date', value: isoToday() });
    const warehouse = h('select', null, h('option', { value: '' }, 'Барлық қойма'));
    const groupBy = h('select', null,
      h('option', { value: 'day' }, 'Күн'), h('option', { value: 'product' }, 'Тауар'), h('option', { value: 'cashier' }, 'Кассир'));
    const search = h('input', { placeholder: 'Атауы / артикулы / штрихкоды' });
    const low = h('input', { type: 'number', min: '0', step: 'any', placeholder: 'мыс. 10', class: 'qty' });
    const showButton = h('button', { class: 'btn btn-primary' }, 'Көрсету');
    const csvButton = h('button', { class: 'btn', disabled: true }, 'CSV жүктеу');
    const output = h('div', { class: 'stack' });

    if (me.is_manager) {
      try {
        for (const w of await api('/warehouses/')) warehouse.append(h('option', { value: String(w.id) }, w.name));
      } catch (e) {
        output.replaceChildren(alertBox(e.messages));
      }
    }

    const field = (label, control) => h('label', null, label, control);
    const fields = {
      period: [field('Басы', dateFrom), field('Соңы', dateTo)],
      warehouse: me.is_manager ? [field('Қойма', warehouse)] : [],
      groupBy: [field('Топтау', groupBy)],
      stock: [field('Іздеу', search), field('Қалдық ≤', low)],
    };
    const controls = h('div', { class: 'row' });
    function drawControls() {
      const r = REPORTS[report.value];
      controls.replaceChildren(field('Есеп', report), r.period ? fields.period : null, fields.warehouse,
        report.value === 'sales' ? fields.groupBy : null, report.value === 'stock' ? fields.stock : null,
        showButton, csvButton);
    }
    report.addEventListener('change', () => { drawControls(); output.replaceChildren(); csvButton.disabled = true; });

    async function show(page) {
      const r = REPORTS[report.value];
      const params = { warehouse: warehouse.value };
      if (r.period) Object.assign(params, { date_from: dateFrom.value, date_to: dateTo.value });
      if (report.value === 'sales') params.group_by = groupBy.value;
      if (report.value === 'stock') Object.assign(params, { search: search.value.trim(), low: low.value });
      if (report.value === 'movements' && page) params.page = page;
      try {
        const data = await api(`/reports/${report.value}/`, { params });
        output.replaceChildren(...RENDERERS[report.value](data, show));
        csvButton.disabled = !output.querySelector('table');
      } catch (e) {
        output.replaceChildren(alertBox(e.messages));
        csvButton.disabled = true;
      }
    }
    showButton.addEventListener('click', () => busy(showButton, () => show()));
    csvButton.addEventListener('click', () => {
      const tables = output.querySelectorAll('table');
      tables.forEach((t, i) => downloadCsv(t, `${report.value}_${dateFrom.value}_${dateTo.value}${tables.length > 1 ? '_' + (i + 1) : ''}.csv`));
    });

    drawControls();
    view.replaceChildren(h('section', { class: 'card stack' }, h('h2', null, 'Есептер'), controls), output);
    show();
  }

  const totalRow = (cells) => h('tr', null, cells.map(([value, cls]) => h('td', { class: cls }, value)));

  const RENDERERS = {
    stock(data) {
      return [table(
        [['Тауар'], ['Артикул'], ['Қойма'], ['Қалдық', 'num'], ['Бағасы', 'num'], ['Құны', 'num']],
        data.rows.map((r) => h('tr', null, h('td', null, r.product_name), h('td', null, r.sku), h('td', null, r.warehouse),
          h('td', { class: 'num' }, `${qty(r.quantity)} ${r.unit}`), h('td', { class: 'num' }, money(r.sale_price)),
          h('td', { class: 'num' }, money(r.value)))),
        totalRow([[`Позиция: ${data.totals.positions}`], [''], [''], [''], [''], [money(data.totals.value), 'num']]))];
    },

    turnover(data) {
      return [table(
        [['Тауар'], ['Қойма'], ['Бастапқы', 'num'], ['Кіріс', 'num'], ['Шығыс', 'num'], ['Соңғы', 'num']],
        data.rows.map((r) => h('tr', null, h('td', null, r.product_name), h('td', null, r.warehouse),
          h('td', { class: 'num' }, qty(r.opening)), h('td', { class: 'num pos' }, qty(r.incoming)),
          h('td', { class: 'num neg' }, qty(r.outgoing)), h('td', { class: 'num' }, qty(r.closing)))),
        totalRow([['Барлығы'], [''], [qty(data.totals.opening), 'num'], [qty(data.totals.incoming), 'num'],
          [qty(data.totals.outgoing), 'num'], [qty(data.totals.closing), 'num']]))];
    },

    sales(data) {
      const keyTitle = { day: 'Күн', product: 'Тауар', cashier: 'Кассир' }[data.group_by];
      const key = (r) => (data.group_by === 'day' ? date(r.day) : data.group_by === 'product' ? r.product_name : r.cashier);
      return [table(
        [[keyTitle], ['Чек', 'num'], ['Сатылды', 'num'], ['Сату сомасы', 'num'], ['Қайтару', 'num'], ['Қайтару сомасы', 'num'], ['Түсім', 'num']],
        data.rows.map((r) => h('tr', null, h('td', null, key(r)), h('td', { class: 'num' }, r.receipts),
          h('td', { class: 'num' }, qty(r.sold_qty)), h('td', { class: 'num' }, money(r.sold_amount)),
          h('td', { class: 'num' }, `${r.returns} / ${qty(r.returned_qty)}`), h('td', { class: 'num neg' }, money(r.returned_amount)),
          h('td', { class: 'num' }, money(r.net_amount)))),
        totalRow([['Барлығы'], [data.totals.receipts, 'num'], [''], [money(data.totals.sold_amount), 'num'],
          [data.totals.returns, 'num'], [money(data.totals.returned_amount), 'num'], [money(data.totals.net_amount), 'num']]))];
    },

    inventory(data) {
      const t = data.totals;
      const lines = data.audits.flatMap((audit) => audit.lines.map((line) => h('tr', null,
        h('td', null, audit.number), h('td', null, dateTime(audit.posted_at)), h('td', null, audit.warehouse),
        h('td', null, line.product_name), h('td', { class: signClass(line.difference) }, qty(line.difference)),
        h('td', { class: 'num' }, money(line.price)), h('td', { class: signClass(line.amount) }, money(line.amount)))));
      const writeOffs = data.write_offs;
      return [
        h('p', null, `Бекітілген түгендеу: ${t.audits} · кемшілік: ${qty(t.shortage_qty)} (${money(t.shortage_amount)}) · артық: ${qty(t.surplus_qty)} (${money(t.surplus_amount)})`),
        h('h3', null, 'Түгендеулер'),
        table([['Түгендеу'], ['Бекітілді'], ['Қойма'], ['Түсініктеме'], ['Кемшілік', 'num'], ['Артық', 'num']],
          data.audits.map((audit) => h('tr', null, h('td', null, audit.number), h('td', null, dateTime(audit.posted_at)),
            h('td', null, audit.warehouse), h('td', null, audit.comment || '—'),
            h('td', { class: 'num neg' }, money(audit.shortage_amount)), h('td', { class: 'num pos' }, money(audit.surplus_amount))))),
        h('h3', null, 'Айырмасы бар тауарлар'),
        table([['Түгендеу'], ['Бекітілді'], ['Қойма'], ['Тауар'], ['Айырма', 'num'], ['Бағасы', 'num'], ['Сомасы', 'num']], lines),
        h('h3', null, 'Есептен шығарулар'),
        table([['Тауар'], ['Артикул'], ['Құжат', 'num'], ['Саны', 'num'], ['Сомасы', 'num']],
          writeOffs.rows.map((r) => h('tr', null, h('td', null, r.product_name), h('td', null, r.sku),
            h('td', { class: 'num' }, r.documents), h('td', { class: 'num' }, qty(r.quantity)), h('td', { class: 'num' }, money(r.amount)))),
          totalRow([['Барлығы'], [''], [''], [qty(writeOffs.total_quantity), 'num'], [money(writeOffs.total_amount), 'num']])),
        h('h3', null, 'Есептен шығару құжаттары'),
        table([['Құжат'], ['Өткізілді'], ['Қойма'], ['Себебі'], ['Саны', 'num'], ['Сомасы', 'num']],
          writeOffs.documents.map((d) => h('tr', null, h('td', null, d.number), h('td', null, dateTime(d.posted_at)),
            h('td', null, d.warehouse), h('td', null, d.reason || '—'),
            h('td', { class: 'num' }, qty(d.quantity)), h('td', { class: 'num' }, money(d.amount))))),
      ];
    },

    movements(data, show) {
      const pageOf = (url) => (url ? Number(new URL(url, window.location.origin).searchParams.get('page') || 1) : null);
      const prev = pageOf(data.previous);
      const next = pageOf(data.next);
      return [
        table([['Уақыты'], ['Тауар'], ['Қойма'], ['Саны', 'num'], ['Құжат'], ['Себебі'], ['Қызметкер']],
          data.results.map((m) => h('tr', null, h('td', null, dateTime(m.date)), h('td', null, m.product_name),
            h('td', null, m.warehouse), h('td', { class: signClass(m.quantity) }, qty(m.quantity)),
            h('td', null, `${m.doc_type_display} №${m.document_number}${m.is_reversal ? ' (кері)' : ''}`),
            // Кері жазбада — күшін жою себебі
            h('td', null, m.is_reversal ? (m.cancel_reason ? `Күшін жою: ${m.cancel_reason}` : '') : (m.comment || '')),
            h('td', null, m.user)))),
        h('div', { class: 'row' },
          h('span', { class: 'muted' }, `Барлығы: ${data.count}`), h('span', { class: 'spacer' }),
          h('button', { class: 'btn', disabled: !data.previous, onclick: () => show(prev) }, '← Алдыңғы'),
          h('button', { class: 'btn', disabled: !data.next, onclick: () => show(next) }, 'Келесі →')),
      ];
    },
  };

  init();
})();
