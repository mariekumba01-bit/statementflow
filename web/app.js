/* StatementFlow — application front (vanilla JS, pas de framework, pas de build).
   Sert la landing, la démo, l'authentification, le tableau de bord et l'admin.
   Communique uniquement avec l'API same-origin (/v1/…) — aucun secret côté
   navigateur : la session vit dans un cookie HttpOnly posé par le serveur. */
'use strict';

/* ── utilitaires ── */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const API = '/v1';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    credentials: 'same-origin',
    headers: opts.body && !(opts.body instanceof FormData)
      ? { 'Content-Type': 'application/json' } : undefined,
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* corps vide */ }
  if (!res.ok) {
    const msg = data && data.error ? data.error.message
      : `Erreur ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function msg(el, text, kind = 'info') {
  if (typeof el === 'string') el = $(el);
  if (!el) return;
  el.className = `msg ${kind}`;
  el.textContent = text;
  el.classList.remove('hidden');
}

function fmtFcfa(n) {
  return Number(n || 0).toLocaleString('fr-FR') + ' FCFA';
}

function fmtDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('fr-FR', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch (_) { return iso; }
}

/* ── tracking visites (attribution) ── */
const TRACK = {
  aid: (() => {
    let v = localStorage.getItem('sf_aid');
    if (!v) {
      v = 'a' + Math.random().toString(36).slice(2, 12)
        + Date.now().toString(36);
      localStorage.setItem('sf_aid', v);
    }
    return v;
  })(),
  source() {
    const q = new URLSearchParams(location.search);
    return q.get('ref') || '';
  },
  referrer() { return document.referrer || ''; },
  track() {
    if (sessionStorage.getItem('sf_tracked')) return;
    sessionStorage.setItem('sf_tracked', '1');
    navigator.sendBeacon?.(
      API + '/track',
      new Blob([JSON.stringify({
        type: 'visit',
        path: location.pathname,
        anonymous_id: TRACK.aid,
        ref: TRACK.source(),
        referrer: TRACK.referrer(),
      })], { type: 'application/json' }));
  },
};

/* ── boutons "créer un compte" avec attribution ── */
function signupLink() {
  const q = new URLSearchParams();
  if (TRACK.source()) q.set('ref', TRACK.source());
  const s = q.toString();
  return '/signup.html' + (s ? '?' + s : '');
}
function decorateSignupLinks() {
  $$('a[data-signup]').forEach((a) => { a.href = signupLink(); });
}

/* ── session ── */
async function session() {
  try { return await api('/auth/session'); }
  catch (_) { return { authenticated: false }; }
}

/* garde les pages privées : redirige vers la connexion si besoin */
async function requireAuth() {
  const s = await session();
  if (!s.authenticated) {
    location.href = '/login.html?next='
      + encodeURIComponent(location.pathname);
    return null;
  }
  return s;
}

async function logout() {
  try { await api('/auth/logout', { method: 'POST' }); }
  catch (_) { /* la session serveur peut déjà être expirée */ }
  location.href = '/';
}

/* ── inscription ── */
async function doSignup(form, statusEl) {
  const btn = form.querySelector('button[type="submit"]');
  btn.disabled = true;
  try {
    const body = {
      email: form.email.value.trim(),
      password: form.password.value,
      ref: TRACK.source(),
      referrer: TRACK.referrer(),
      anonymous_id: TRACK.aid,
    };
    const res = await api('/auth/signup', {
      method: 'POST', body: JSON.stringify(body),
    });
    if (res.api_key) sessionStorage.setItem('sf_new_key', res.api_key);
    if (res.email_verification === 'sent') {
      msg(statusEl,
        'Compte créé ! Ouvrez le lien reçu par email pour activer votre '
        + 'tableau de bord.', 'ok');
    } else {
      msg(statusEl, 'Compte créé !', 'ok');
      setTimeout(() => { location.href = '/dashboard.html'; }, 700);
    }
  } catch (e) {
    msg(statusEl, e.message, 'err');
  } finally { btn.disabled = false; }
}

/* ── connexion ── */
async function doLogin(form, statusEl) {
  const btn = form.querySelector('button[type="submit"]');
  btn.disabled = true;
  try {
    await api('/auth/login', {
      method: 'POST',
      body: JSON.stringify({
        email: form.email.value.trim(),
        password: form.password.value,
      }),
    });
    const q = new URLSearchParams(location.search);
    const next = q.get('next');
    location.href = next && next.startsWith('/')
      ? next : '/dashboard.html';
  } catch (e) {
    msg(statusEl, e.message, 'err');
  } finally { btn.disabled = false; }
}

/* ── vérification email ── */
async function doVerify() {
  const q = new URLSearchParams(location.search);
  const token = q.get('token');
  const el = $('#status');
  if (!token) { msg(el, 'Lien invalide : token manquant.', 'err'); return; }
  try {
    await api('/auth/verify', {
      method: 'POST', body: JSON.stringify({ token }),
    });
    msg(el, 'Adresse vérifiée ! Vous pouvez vous connecter.', 'ok');
    setTimeout(() => { location.href = '/login.html'; }, 1200);
  } catch (e) { msg(el, e.message, 'err'); }
}

/* ── mot de passe oublié / reset ── */
async function doForgot(form, statusEl) {
  const btn = form.querySelector('button[type="submit"]');
  btn.disabled = true;
  try {
    await api('/auth/password/forgot', {
      method: 'POST',
      body: JSON.stringify({ email: form.email.value.trim() }),
    });
    msg(statusEl, 'Si un compte existe pour cet email, un lien de '
      + 'réinitialisation vient d\'être envoyé (valable 1 heure).', 'ok');
  } catch (e) {
    msg(statusEl, e.message, 'err');
  } finally { btn.disabled = false; }
}

async function doReset(form, statusEl) {
  const q = new URLSearchParams(location.search);
  const token = q.get('token');
  if (!token) { msg(statusEl, 'Lien invalide : token manquant.', 'err'); return; }
  const btn = form.querySelector('button[type="submit"]');
  btn.disabled = true;
  try {
    await api('/auth/password/reset', {
      method: 'POST',
      body: JSON.stringify({ token, password: form.password.value }),
    });
    msg(statusEl, 'Mot de passe modifié : reconnectez-vous.', 'ok');
    setTimeout(() => { location.href = '/login.html'; }, 1200);
  } catch (e) {
    msg(statusEl, e.message, 'err');
  } finally { btn.disabled = false; }
}

/* ── tableau de bord ── */
const PACKS = {
  starter: { credits: 500, amount: 5000, label: 'Starter' },
  pro: { credits: 2500, amount: 20000, label: 'Pro' },
  business: { credits: 10000, amount: 60000, label: 'Business' },
};

async function dashInit() {
  const s = await requireAuth();
  if (!s) return;
  $('#who').textContent = s.email;
  $('#credits').textContent = s.credits == null ? '—'
    : Number(s.credits).toLocaleString('fr-FR');
  const verifyNote = $('#verify-note');
  if (!s.email_verified) {
    verifyNote.classList.remove('hidden');
    if (s.verified_via === 'auto_no_smtp') {
      $('#verify-text').textContent =
        'Votre adresse n\'a pas encore été vérifiée par email '
        + '(service d\'envoi non configuré sur cette instance). '
        + 'Votre compte reste utilisable.';
    }
  }
  const newKey = sessionStorage.getItem('sf_new_key');
  if (newKey) {
    sessionStorage.removeItem('sf_new_key');
    $('#key-line').classList.remove('hidden');
    $('#new-key').textContent = newKey;
  }
  await Promise.all([loadConversions(), loadTopups()]);
}

async function loadConversions() {
  const el = $('#conversions');
  try {
    const res = await api('/account/conversions?limit=20');
    if (!res.records.length) {
      el.innerHTML = '<p class="muted center mt2">Aucune conversion pour '
        + 'l\'instant — déposez votre premier relevé ci-dessus.</p>';
      return;
    }
    el.innerHTML = '<div class="tbl-wrap"><table class="tbl">'
      + '<thead><tr><th>Date</th><th>Fichier</th><th>Format</th>'
      + '<th class="num">Transactions</th><th class="num">Crédits</th>'
      + '<th>Statut</th></tr></thead><tbody>'
      + res.records.map((r) => {
        const pill = r.status === 'done' ? 'ok'
          : r.status === 'error' ? 'err' : 'pend';
        const label = r.status === 'done' ? 'réussie'
          : r.status === 'error' ? 'échec' : 'en cours';
        return `<tr><td>${esc(fmtDate(r.created_at))}</td>`
          + `<td>${esc(r.filename)}</td><td>${esc(r.kind || '')}</td>`
          + `<td class="num">${r.transactions ?? '—'}</td>`
          + `<td class="num">${r.credits_charged ?? '—'}</td>`
          + `<td><span class="pill ${pill}">${label}</span></td></tr>`;
      }).join('') + '</tbody></table></div>';
  } catch (e) {
    el.innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  }
}

async function loadTopups() {
  const el = $('#topups');
  try {
    const res = await api('/account/topups');
    if (!res.records.length) {
      el.innerHTML = '<p class="muted center mt2">Aucun achat pour '
        + 'l\'instant.</p>';
      return;
    }
    el.innerHTML = '<div class="tbl-wrap"><table class="tbl">'
      + '<thead><tr><th>Date</th><th>Pack</th><th class="num">Crédits</th>'
      + '<th class="num">Montant</th><th>Statut</th></tr></thead><tbody>'
      + res.records.map((r) => {
        const pill = r.status === 'activated' ? 'ok'
          : r.status === 'failed' ? 'err' : 'pend';
        const label = r.status === 'activated' ? 'activé'
          : r.status === 'failed' ? 'échoué' : 'en attente';
        return `<tr><td>${esc(fmtDate(r.created_at))}</td>`
          + `<td>${esc(PACKS[r.pack] ? PACKS[r.pack].label : r.pack)}</td>`
          + `<td class="num">${Number(r.credits).toLocaleString('fr-FR')}</td>`
          + `<td class="num">${fmtFcfa(r.amount_fcfa)}</td>`
          + `<td><span class="pill ${pill}">${label}</span></td></tr>`;
      }).join('') + '</tbody></table></div>';
  } catch (e) {
    el.innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  }
}

/* upload + progression + résultat */
let currentJob = null;

async function uploadFile(file) {
  const zone = $('#dropzone');
  const prog = $('#progress');
  const out = $('#result');
  out.classList.add('hidden');
  if (file.size > 20 * 1024 * 1024) {
    msg('#convert-status', 'Fichier trop volumineux (20 Mo maximum).', 'err');
    return;
  }
  zone.classList.add('hidden');
  prog.classList.remove('hidden');
  msg('#convert-status', 'Conversion en cours…', 'info');
  try {
    const fd = new FormData();
    fd.append('file', file);
    const res = await api('/account/convert', { method: 'POST', body: fd });
    currentJob = res.job_id;
    await pollResult(res.job_id);
  } catch (e) {
    prog.classList.add('hidden');
    zone.classList.remove('hidden');
    if (e.status === 402) {
      msg('#convert-status', e.message + ' Rechargez ci-dessous.', 'err');
    } else {
      msg('#convert-status', e.message, 'err');
    }
  }
}

async function pollResult(jobId) {
  for (let i = 0; i < 90; i++) {
    let res;
    try { res = await api(`/account/result/${jobId}`); }
    catch (e) {
      if (e.status === 202) { await sleep(2000); continue; }
      throw e;
    }
    if (res.status === 'processing') { await sleep(2000); continue; }
    $('#progress').classList.add('hidden');
    $('#dropzone').classList.remove('hidden');
    if (res.status === 'error') {
      msg('#convert-status', 'Échec de conversion : ' + res.error
        + ' — aucun crédit débité pour la part non traitée.', 'err');
      return;
    }
    msg('#convert-status', 'Conversion réussie !', 'ok');
    renderResult(res);
    const s = await session();
    $('#credits').textContent = s.credits == null ? '—'
      : Number(s.credits).toLocaleString('fr-FR');
    loadConversions();
    return;
  }
  msg('#convert-status', 'Délai dépassé : votre résultat reste '
    + 'récupérable quelques minutes via l\'historique.', 'err');
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function renderResult(res) {
  const out = $('#result');
  out.classList.remove('hidden');
  const d = res.document || {};
  const u = res.usage || {};
  const b = res.billing || {};
  $('#res-meta').innerHTML =
    `<div class="row" style="gap:18px;flex-wrap:wrap">`
    + `<div><div class="kpi">Transactions</div><div class="big">${u.transactions ?? '—'}</div></div>`
    + `<div><div class="kpi">Pages / lignes</div><div class="big">${u.pages ?? '—'} / ${u.rows ?? '—'}</div></div>`
    + `<div><div class="kpi">Crédits débités</div><div class="big">${b.credits_charged ?? '—'}</div></div>`
    + `<div><div class="kpi">Solde restant</div><div class="big">${b.credits_remaining ?? '—'}</div></div>`
    + `</div>`;
  const txs = (res.transactions || []).slice(0, 50);
  $('#res-txs').innerHTML = txs.length
    ? '<div class="tbl-wrap tx-scroll"><table class="tbl">'
      + '<thead><tr><th>Date</th><th>Libellé</th>'
      + '<th class="num">Débit</th><th class="num">Crédit</th></tr>'
      + '</thead><tbody>'
      + txs.map((t) => {
        const amt = Number(t.amount || 0);
        const debit = amt < 0 ? Math.abs(amt).toLocaleString('fr-FR') : '';
        const credit = amt >= 0 ? amt.toLocaleString('fr-FR') : '';
        return `<tr><td>${esc(t.date || '')}</td>`
          + `<td>${esc(t.label || t.description || '')}</td>`
          + `<td class="num">${debit}</td><td class="num">${credit}</td></tr>`;
      }).join('') + '</tbody></table></div>'
      + (res.transactions.length > 50
        ? `<p class="muted small">50 premières transactions affichées — `
          + `le JSON complet contient les ${res.transactions.length}.</p>` : '')
    : '<p class="muted">Aucune transaction détectée.</p>';
  const checks = res.checks || [];
  $('#res-checks').innerHTML = checks.length
    ? checks.map((c) => {
      const sev = c.severity === 'error' ? 'err'
        : c.severity === 'warning' ? 'warn' : 'ok';
      return `<div class="alert-line ${sev}">`
        + `<span class="kind">${esc(c.code || c.severity || '')}</span>`
        + `<span>${esc(c.message || '')}</span></div>`;
    }).join('')
    : '<p class="muted">Aucune alerte de cohérence détectée.</p>';
  const dl = $('#dl-json');
  dl.onclick = () => {
    const blob = new Blob(
      [JSON.stringify(res, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (res.job_id || 'resultat') + '.json';
    a.click();
    URL.revokeObjectURL(a.href);
  };
  out.scrollIntoView({ behavior: 'smooth' });
}

/* achat de packs */
async function buyPack(pack, btn) {
  btn.disabled = true;
  try {
    const res = await api('/account/topup', {
      method: 'POST', body: JSON.stringify({ pack }),
    });
    if (!res.checkout_url) throw new Error(
      'La passerelle n\'a pas renvoyé d\'URL de paiement.');
    sessionStorage.setItem('sf_pending_topup', res.reference);
    location.href = res.checkout_url;
  } catch (e) {
    msg('#buy-status', e.message, 'err');
    btn.disabled = false;
  }
}

/* au retour de la passerelle : reprise du rechargement en attente */
async function resumePendingTopup() {
  const ref = sessionStorage.getItem('sf_pending_topup');
  const q = new URLSearchParams(location.search);
  const fromGateway = q.get('pay') === 'back';
  if (!ref && !fromGateway) return;
  const el = $('#buy-status');
  if (!el) return;
  msg(el, 'Vérification du paiement…', 'info');
  for (let i = 0; i < 30; i++) {
    let res;
    try { res = await api(`/account/topup/${ref}`); }
    catch (e) { await sleep(3000); continue; }
    if (res.status === 'activated') {
      sessionStorage.removeItem('sf_pending_topup');
      msg(el, `Paiement confirmé : ${Number(res.credits).toLocaleString('fr-FR')} `
        + `crédits ajoutés. Nouveau solde : ${res.balance}.`, 'ok');
      const s = await session();
      $('#credits').textContent = s.credits == null ? '—'
        : Number(s.credits).toLocaleString('fr-FR');
      loadTopups();
      return;
    }
    if (res.status === 'failed') {
      sessionStorage.removeItem('sf_pending_topup');
      msg(el, 'Paiement non abouti : aucun crédit n\'a été débité. Vous '
        + 'pouvez réessayer.', 'err');
      loadTopups();
      return;
    }
    await sleep(4000);
  }
  msg(el, 'Le paiement est encore en cours de confirmation. Cette page '
    + 'vérifiera automatiquement à votre prochaine visite.', 'info');
}

/* régénération de clé API */
async function rotateKey() {
  if (!confirm('Régénérer votre clé API ? L\'ancienne cessera '
    + 'immédiatement de fonctionner (vos crédits sont conservés).')) return;
  try {
    const res = await api('/account/key/rotate', {
      method: 'POST', body: JSON.stringify({ confirm: true }),
    });
    $('#key-line').classList.remove('hidden');
    $('#new-key').textContent = res.api_key;
    msg('#key-status', `Clé régénérée — ${res.credits_transferred} crédits `
      + 'transférés. Conservez-la : elle ne sera plus affichée.', 'ok');
  } catch (e) { msg('#key-status', e.message, 'err'); }
}

function copyKey() {
  const key = $('#new-key').textContent;
  navigator.clipboard?.writeText(key);
}

/* ── admin ── */
async function adminInit() {
  const key = sessionStorage.getItem('sf_admin_key');
  if (!key) {
    $('#ask-key').classList.remove('hidden');
    return;
  }
  await loadStats(key);
}

async function adminSubmit(form) {
  const key = form.key.value.trim();
  sessionStorage.setItem('sf_admin_key', key);
  $('#ask-key').classList.add('hidden');
  await loadStats(key);
}

async function loadStats(key) {
  const el = $('#stats');
  try {
    const res = await fetch(API + '/admin/stats', {
      headers: { 'X-Admin-Key': key },
    });
    if (!res.ok) throw new Error('Clé d\'administration invalide ('
      + res.status + ').');
    const s = await res.json();
    sessionStorage.removeItem('sf_admin_key');
    renderStats(s);
  } catch (e) {
    sessionStorage.removeItem('sf_admin_key');
    $('#ask-key').classList.remove('hidden');
    msg('#admin-status', e.message, 'err');
  }
}

function renderStats(s) {
  $('#stats').classList.remove('hidden');
  const conv = s.signups ? Math.round(
    (s.active_users / s.signups) * 100) : 0;
  $('#stats').innerHTML =
    `<div class="grid c4 mt2">
      <div class="card"><div class="kpi">Visiteurs</div><div class="big">${s.visitors}</div></div>
      <div class="card"><div class="kpi">Comptes créés</div><div class="big">${s.signups}</div></div>
      <div class="card"><div class="kpi">Clients actifs</div><div class="big">${s.active_users}</div></div>
      <div class="card"><div class="kpi">Taux d'activation</div><div class="big">${conv}%</div></div>
      <div class="card"><div class="kpi">Conversions réussies</div><div class="big">${s.conversions_done}</div></div>
      <div class="card"><div class="kpi">Crédits consommés</div><div class="big">${Number(s.credits_consumed).toLocaleString('fr-FR')}</div></div>
      <div class="card"><div class="kpi">Paiements encaissés</div><div class="big">${s.payments_activated}</div></div>
      <div class="card"><div class="kpi">CA encaissé</div><div class="big">${fmtFcfa(s.revenue_fcfa)}</div></div>
    </div>
    <h3 class="mt3">Packs les plus vendus</h3>
    ${packsTable(s.packs)}
    <h3 class="mt3">Sources d'inscription</h3>
    ${sourcesTable(s.signup_sources)}`;
}

function packsTable(packs) {
  if (!packs || !packs.length) {
    return '<p class="muted">Aucun pack vendu pour l\'instant.</p>';
  }
  return '<div class="tbl-wrap"><table class="tbl">'
    + '<thead><tr><th>Pack</th><th class="num">Ventes</th>'
    + '<th class="num">Crédits vendus</th><th class="num">CA</th></tr>'
    + '</thead><tbody>'
    + packs.map((p) => `<tr><td>${esc(PACKS[p.pack] ? PACKS[p.pack].label : p.pack)}</td>`
      + `<td class="num">${p.count}</td>`
      + `<td class="num">${Number(p.credits_sold).toLocaleString('fr-FR')}</td>`
      + `<td class="num">${fmtFcfa(p.revenue_fcfa)}</td></tr>`).join('')
    + '</tbody></table></div>';
}

function sourcesTable(sources) {
  if (!sources || !sources.length) {
    return '<p class="muted">Aucune inscription suivie pour l\'instant.</p>';
  }
  return '<div class="tbl-wrap"><table class="tbl">'
    + '<thead><tr><th>Source</th><th class="num">Inscriptions</th></tr></thead><tbody>'
    + sources.map((s) => `<tr><td>${esc(s.source)}</td>`
      + `<td class="num">${s.count}</td></tr>`).join('')
    + '</tbody></table></div>';
}

/* ── démo interactive (moteur réel, sans compte, sans débit) ── */
async function demoInit() {
  const zone = $('#demo-zone');
  if (!zone) return;
  zone.addEventListener('click', () => $('#demo-file').click());
  const input = $('#demo-file');
  input.addEventListener('change', () => {
    if (input.files[0]) runDemo(input.files[0]);
  });
  $$('#demo-samples button').forEach((b) => {
    b.addEventListener('click', async () => {
      const res = await fetch(b.dataset.src);
      const blob = await res.blob();
      runDemo(new File([blob], b.dataset.name, { type: blob.type }));
    });
  });
}

async function runDemo(file) {
  const out = $('#demo-out');
  const status = $('#demo-status');
  msg(status, 'Analyse en cours (moteur réel, aucun crédit débité)…', 'info');
  out.classList.add('hidden');
  try {
    const fd = new FormData();
    fd.append('file', file);
    const res = await api('/demo/analyze', { method: 'POST', body: fd });
    msg(status, 'Conversion réussie — voici le résultat réel du moteur.', 'ok');
    $('#demo-input').textContent = res.input_preview;
    const pre = $('#demo-json');
    pre.textContent = JSON.stringify(res.result, null, 2);
    out.classList.remove('hidden');
  } catch (e) {
    msg(status, e.message, 'err');
  }
}

/* ── démarrage page ── */
document.addEventListener('DOMContentLoaded', () => {
  TRACK.track();
  decorateSignupLinks();
  /* PWA : enregistrement du service worker (cache pages statiques
     uniquement — jamais /v1/*) */
  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
  const page = document.body.dataset.page;
  if (page === 'signup' && $('#signup-form')) {
    $('#signup-form').addEventListener('submit', (ev) => {
      ev.preventDefault(); doSignup(ev.target, '#status');
    });
  }
  if (page === 'login' && $('#login-form')) {
    $('#login-form').addEventListener('submit', (ev) => {
      ev.preventDefault(); doLogin(ev.target, '#status');
    });
  }
  if (page === 'verify') doVerify();
  if (page === 'forgot' && $('#forgot-form')) {
    $('#forgot-form').addEventListener('submit', (ev) => {
      ev.preventDefault(); doForgot(ev.target, '#status');
    });
  }
  if (page === 'reset' && $('#reset-form')) {
    $('#reset-form').addEventListener('submit', (ev) => {
      ev.preventDefault(); doReset(ev.target, '#status');
    });
  }
  if (page === 'dashboard') {
    dashInit();
    const zone = $('#dropzone');
    zone.addEventListener('click', () => $('#file-input').click());
    zone.addEventListener('dragover', (ev) => {
      ev.preventDefault(); zone.classList.add('drag');
    });
    zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
    zone.addEventListener('drop', (ev) => {
      ev.preventDefault(); zone.classList.remove('drag');
      if (ev.dataTransfer.files[0]) uploadFile(ev.dataTransfer.files[0]);
    });
    $('#file-input').addEventListener('change', (ev) => {
      if (ev.target.files[0]) uploadFile(ev.target.files[0]);
      ev.target.value = '';
    });
    resumePendingTopup();
  }
  if (page === 'admin') adminInit();
  if (page === 'demo') demoInit();
});
