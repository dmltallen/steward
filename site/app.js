/* Steward landing. Two A/B pricing variants, free-to-paid funnel, event log stub.
   No build step. Open this file directly or serve the folder statically.
   Events are buffered into window.__stewardEvents. To ship them somewhere, set
   window.STEWARD_EVENTS_ENDPOINT before this script runs and the logger will
   POST newline-delimited JSON there. Until then nothing leaves the page. */

(function () {
  'use strict';

  var PRICES = { a: '$79', b: '$149' };
  window.__stewardEvents = [];

  var ctx = {
    variant: (window._stewardVariant === 'b') ? 'b' : 'a',
    url: window.location.href,
    ts: 0,
    plan: null
  };

  function track(name, props) {
    var ev = {
      e: name,
      variant: ctx.variant,
      url: ctx.url,
      ts: new Date().toISOString()
    };
    if (props) {
      for (var k in props) { if (Object.prototype.hasOwnProperty.call(props, k)) ev[k] = props[k]; }
    }
    window.__stewardEvents.push(ev);
    window.dispatchEvent(new CustomEvent('steward:event', { detail: ev }));

    var endpoint = window.STEWARD_EVENTS_ENDPOINT;
    if (endpoint) {
      try {
        fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-ndjson' },
          body: JSON.stringify(ev) + '\n',
          keepalive: true
        }).catch(function () { /* fire and forget */ });
      } catch (err) { /* no-op: offline is fine */ }
    }
    return ev;
  }

  function applyVariant(variant) {
    ctx.variant = variant;
    document.body.setAttribute('data-variant', variant);
    var amount = document.getElementById('price-amount');
    if (amount) { amount.textContent = PRICES[variant]; }
    document.querySelectorAll('.price-tab').forEach(function (btn) {
      btn.classList.toggle('is-active', btn.getAttribute('data-variant') === variant);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    var wantAbDemo = (new URLSearchParams(window.location.search).get('ab') === 'show');
    if (!wantAbDemo) {
      document.querySelectorAll('.test-note, .price-switch').forEach(function (el) {
        el.style.display = 'none';
      });
    }
    applyVariant(ctx.variant);
    track('page_view');

    document.querySelectorAll('.price-tab').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var v = btn.getAttribute('data-variant');
        if (v !== ctx.variant) {
          applyVariant(v);
          track('variant_switch', { to: v });
        }
      });
    });

    var cta = document.querySelector('.price-cta');
    if (cta) {
      cta.addEventListener('click', function () {
        track('price_cta_click', { plan: cta.getAttribute('data-plan') });
      });
    }

    var form = document.getElementById('capture-form');
    if (form) {
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        var input = document.getElementById('capture-email');
        var status = document.getElementById('capture-status');
        var raw = input ? input.value.trim() : '';
        if (!raw || raw.indexOf('@') < 1) {
          if (status) {
            status.hidden = false;
            status.textContent = 'Enter a valid email to join the list.';
          }
          track('email_submit_invalid');
          return;
        }
        track('email_submit', {
          email: raw, /* stub: not transmitted until an endpoint is configured */
          plan: ctx.variant === 'b' ? 'variant-b' : 'variant-a'
        });
        if (status) {
          status.hidden = false;
          status.textContent = 'You are on the list. We will email you when the public build is ready.';
        }
        form.reset();
      });
    }

    track('funnel_view', { step: 'landing', variant: ctx.variant });
  });
})();
