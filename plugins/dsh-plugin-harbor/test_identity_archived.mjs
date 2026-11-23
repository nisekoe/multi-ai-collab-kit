import { verifyTarget, pickUnique } from './lib/identity.js';

const REG = { feishu_bot_presets: ['feishu_bot'], desktop_presets: ['cordis', 'standard'] };
const byId = new Map();
const mk = (sid, role) => {
  const r = { session_id: sid, role };
  byId.set(sid, r);
  return r;
};
const ident = () => ({ ok: true, registry: REG, byId, builtAt: '2026-11-23T00:00:00Z' });
const rec = (sid, { live = true, persisted = true, archived = false, preset = 'cordis' } = {}) => ({
  sessionId: sid,
  cwd: 'C:/x/default-workspace',
  createdAt: 't',
  live,
  persisted,
  archived,
  header: { id: sid, agentPreset: preset, delegationDepth: null, origin: null, parentSession: null },
});

let pass = 0;
let fail = 0;
const t = (name, cond) => {
  if (cond) {
    pass += 1;
    console.log('PASS', name);
  } else {
    fail += 1;
    console.log('FAIL', name);
  }
};

const ARCH = 'session-arch-1';
const LIVE = 'session-live-1';
const LIVE2 = 'session-live-2';
const COLD = 'session-cold-1';
mk(ARCH, 'desktop');
mk(LIVE, 'desktop');
mk(LIVE2, 'desktop');
mk(COLD, 'desktop');

// A: archived + persisted + allowPersisted => rejected 410
let r = verifyTarget({
  identity: ident(),
  sessions: [rec(ARCH, { live: false, persisted: true, archived: true })],
  sessionId: ARCH,
  expect: 'desktop',
  allowPersisted: true,
});
t('A archived persisted allowPersisted rejected 410', r.ok === false && r.error === 'target_archived' && r.code === 410);

// B: archived live=true => rejected
r = verifyTarget({
  identity: ident(),
  sessions: [rec(ARCH, { live: true, archived: true })],
  sessionId: ARCH,
  expect: 'desktop',
  allowPersisted: true,
});
t('B archived live rejected', r.ok === false && r.error === 'target_archived');

// C: non-archived cold persisted allowPersisted => ok (cold wake preserved)
r = verifyTarget({
  identity: ident(),
  sessions: [rec(COLD, { live: false, persisted: true, archived: false })],
  sessionId: COLD,
  expect: 'desktop',
  allowPersisted: true,
});
t('C cold non-archived still ok', r.ok === true && r.role === 'desktop');

// D: live non-archived ok
r = verifyTarget({
  identity: ident(),
  sessions: [rec(LIVE)],
  sessionId: LIVE,
  expect: 'desktop',
});
t('D live ok', r.ok === true);

// E: pickUnique excludes archived even if live=true
r = pickUnique([rec(ARCH, { live: true, archived: true }), rec(LIVE)], ident(), 'desktop', 'default-workspace');
t('E pickUnique excludes archived -> unique live', r.ok === true && r.session.sessionId === LIVE);

// F: pickUnique two live non-archived => ambiguous
r = pickUnique([rec(LIVE), rec(LIVE2)], ident(), 'desktop', 'default-workspace');
t('F two live ambiguous', r.ok === false && r.error === 'ambiguous');

// G: delegated regression
const del = rec('session-del-1', { preset: 'cordis' });
del.header.origin = 'subagent';
mk('session-del-1', 'desktop');
r = verifyTarget({
  identity: ident(),
  sessions: [del],
  sessionId: 'session-del-1',
  expect: 'desktop',
});
t('G delegated still rejected', r.ok === false && r.error === 'target_delegated');

console.log('IDENTITY_TEST pass=' + pass + ' fail=' + fail);
if (fail) process.exit(1);
