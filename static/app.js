/* SmartTrack UI helpers — progressive enhancement only. Every form also works without JavaScript. */
(function () {
  'use strict';
  var store = {
    get: function (k) { try { return JSON.parse(sessionStorage.getItem(k) || 'null'); } catch (e) { return null; } },
    set: function (k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
    del: function (k) { try { sessionStorage.removeItem(k); } catch (e) {} }
  };

  function openDialog(d) { if (d && !d.open) { if (d.showModal) d.showModal(); else d.setAttribute('open', ''); var f = d.querySelector('input:not([type=hidden]),select,textarea'); if (f) f.focus(); } }
  function closeDialog(d) { if (d && d.open) { if (d.close) d.close(); else d.removeAttribute('open'); } }

  /* Sidebar on small screens */
  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-nav-toggle]');
    if (t) { document.body.classList.toggle('nav-open'); t.setAttribute('aria-expanded', document.body.classList.contains('nav-open')); return; }
    if (e.target.closest('[data-nav-close]')) document.body.classList.remove('nav-open');
  });

  /* Dialogs: <button data-open="dialog-id">, <button data-close> */
  document.addEventListener('click', function (e) {
    var o = e.target.closest('[data-open]');
    if (o) {
      e.preventDefault();
      var d = document.getElementById(o.getAttribute('data-open'));
      if (d) {
        // Optional: copy data-set-* attributes into fields of the dialog's form (reusable dialogs).
        Array.prototype.forEach.call(o.attributes, function (a) {
          if (a.name.indexOf('data-set-') === 0) {
            var key = a.name.slice(9), el = d.querySelector('[name="' + key + '"]') || d.querySelector('[data-text="' + key + '"]');
            if (!el) return;
            if ('value' in el && el.tagName !== 'DD' && el.tagName !== 'SPAN' && el.tagName !== 'STRONG') el.value = a.value; else el.textContent = a.value;
          }
        });
        openDialog(d);
      }
      closeMenus();
      return;
    }
    if (e.target.closest('[data-close]')) { e.preventDefault(); closeDialog(e.target.closest('dialog')); }
  });
  document.addEventListener('click', function (e) {
    if (e.target.tagName === 'DIALOG' && e.target.classList.contains('modal')) {
      var r = e.target.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) closeDialog(e.target);
    }
  });

  /* Action menus (<details class="menu">): close on outside click / Escape */
  function closeMenus(except) {
    document.querySelectorAll('details.menu[open]').forEach(function (m) { if (m !== except) m.removeAttribute('open'); });
  }
  document.addEventListener('click', function (e) { closeMenus(e.target.closest('details.menu')); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { closeMenus(); document.body.classList.remove('nav-open'); } });

  /* Confirmation dialog for destructive actions: <form data-confirm="Text" data-confirm-label="Delete"> */
  var pending = null;
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (f.hasAttribute('data-confirm') && !f.__confirmed) {
      e.preventDefault();
      var d = document.getElementById('confirm-dialog');
      if (!d) { if (window.confirm(f.getAttribute('data-confirm'))) { f.__confirmed = true; f.submit(); } return; }
      d.querySelector('[data-confirm-text]').textContent = f.getAttribute('data-confirm');
      var ok = d.querySelector('[data-confirm-ok]');
      ok.textContent = f.getAttribute('data-confirm-label') || 'Confirm';
      ok.className = 'btn ' + (f.hasAttribute('data-confirm-safe') ? 'primary' : 'danger-solid');
      pending = f; openDialog(d); return;
    }
    // Remember entered values so they survive a validation error round-trip.
    if (f.id && f.hasAttribute('data-persist')) {
      var data = {};
      Array.prototype.forEach.call(f.elements, function (el) {
        if (!el.name || el.name === 'csrf_token' || el.type === 'password' || el.type === 'file' || el.type === 'hidden' || el.type === 'submit') return;
        if (el.type === 'checkbox') data[el.name] = el.checked; else data[el.name] = el.value;
      });
      store.set('st-form', { id: f.id, data: data, path: location.pathname + location.search });
    }
    var b = e.submitter || f.querySelector('button[type=submit],button:not([type])');
    if (b && !b.hasAttribute('data-no-loading')) {
      // Keep the clicked button's name/value even though it becomes inactive.
      if (b.name) { var h = document.createElement('input'); h.type = 'hidden'; h.name = b.name; h.value = b.value; f.appendChild(h); }
      setTimeout(function () { b.classList.add('is-loading'); }, 0);
    }
  });
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-confirm-ok]') && pending) {
      var f = pending; pending = null; f.__confirmed = true; closeDialog(document.getElementById('confirm-dialog'));
      if (f.requestSubmit) f.requestSubmit(); else f.submit();
    }
  });

  /* Client-side table search: <input data-filter="#table-id"> */
  document.addEventListener('input', function (e) {
    var sel = e.target.getAttribute && e.target.getAttribute('data-filter');
    if (!sel) return;
    var q = e.target.value.trim().toLowerCase(), table = document.querySelector(sel), shown = 0;
    if (!table) return;
    table.querySelectorAll('tbody tr[data-row]').forEach(function (tr) {
      var hit = !q || tr.textContent.toLowerCase().indexOf(q) !== -1; tr.hidden = !hit; if (hit) shown++;
    });
    var none = table.querySelector('tr[data-no-match]'); if (none) none.hidden = shown !== 0;
  });

  /* Select-all checkboxes: <input type=checkbox data-check-all=".row-check"> */
  document.addEventListener('change', function (e) {
    var sel = e.target.getAttribute && e.target.getAttribute('data-check-all');
    if (sel) document.querySelectorAll(sel).forEach(function (c) { c.checked = e.target.checked; });
  });

  /* Auto-submit selects in filter bars */
  document.addEventListener('change', function (e) { if (e.target.hasAttribute && e.target.hasAttribute('data-autosubmit')) e.target.form.submit(); });

  /* After a validation error: restore the last form's input and reopen its dialog. */
  document.addEventListener('DOMContentLoaded', function () {
    var saved = store.get('st-form');
    var failed = document.querySelector('.flash .alert:not(.good)');
    if (saved && failed) {
      var f = document.getElementById(saved.id);
      if (f) {
        Object.keys(saved.data).forEach(function (k) {
          var el = f.elements[k]; if (!el || el.type === 'hidden') return;
          if (el.type === 'checkbox') el.checked = !!saved.data[k]; else if (el.value !== undefined) el.value = saved.data[k];
        });
        var d = f.closest('dialog'); if (d) openDialog(d);
      }
    }
    store.del('st-form');
    var auto = document.querySelector('dialog[data-autoopen]'); if (auto) openDialog(auto);
  });
})();
