/**
 * 身份判定与目标选择（纯函数，可在宿主之外单测）。
 *
 * 2026-11-17 首批修复 A01 拆出来；同日晚 R1 回修把「只看缓存」改成「先看活会话当前头」：
 *   缓存只能说明"缓存生成那一刻它像谁"，不能说明"现在它还是谁"；而 live 列表里的
 *   header 是宿主当下的状态。所以判据分两截——预设名单（registry）来自缓存，
 *   角色判定优先用 live header，缓存只作兜底，两截矛盾就拒（identity_conflict）。
 *
 * 边界：这里**不读会话文件**，也不发请求；宿主能力（listSessions）由 index.js 注入。
 */
import { readFileSync } from 'node:fs';

export const ROLE_FEISHU_BOT = 'feishu_bot';
export const ROLE_DESKTOP = 'desktop';
export const ROLE_DELEGATED = 'delegated';
export const ROLE_UNKNOWN = 'unknown';
// 可通过环境变量 FEISHU_LEGACY_SESSION_PREFIX 覆盖（默认 dfc: 为飞书通道历史会话前缀技术约定）
export const LEGACY_FEISHU_PREFIX = String(process.env.FEISHU_LEGACY_SESSION_PREFIX || 'dfc:');

/**
 * 读身份缓存。过期、读不出、或**没有 registry 判据**都算不可用（不放宽）。
 * registry 是 session_identity.refresh() 写进去的预设名单；旧版缓存没有它，
 * 插件就没法用当前头判活会话，只能重新刷新，不能猜。
 */
export const readIdentityCache = (path, maxAgeMs = 10 * 60 * 1000, now = Date.now()) => {
  try {
    const c = JSON.parse(readFileSync(path, 'utf8'));
    const builtAt = Date.parse(c.built_at || '');
    const age = Number.isFinite(builtAt) ? now - builtAt : Infinity;
    const byId = new Map();
    for (const row of Object.values(c.sessions || {})) {
      if (!row) continue;
      if (row.session_id) byId.set(String(row.session_id), row);
      if (row.dir_name) byId.set(String(row.dir_name), row);
    }
    const registry = c.registry && typeof c.registry === 'object' ? c.registry : null;
    const hasRegistry = Boolean(registry && Array.isArray(registry.feishu_bot_presets)
      && registry.feishu_bot_presets.length);
    return {
      ok: age <= maxAgeMs && hasRegistry,
      age,
      builtAt: c.built_at || null,
      registry,
      byId,
      error: hasRegistry ? undefined : '缓存没有 registry 判据（旧版缓存）',
    };
  } catch (error) {
    return { ok: false, age: null, builtAt: null, registry: null, byId: new Map(),
      error: String(error && error.message ? error.message : error) };
  }
};

export const roleOf = (identity, sessionId) => {
  const row = identity && identity.byId ? identity.byId.get(String(sessionId || '')) : null;
  return row && row.role ? row.role : ROLE_UNKNOWN;
};

/** 这个会话在缓存里的完整行（给 /target 的证据用）。 */
export const rowOf = (identity, sessionId) =>
  (identity && identity.byId ? identity.byId.get(String(sessionId || '')) : null) || null;

/**
 * 按会话**当前头**判角色：与 Python 的 classify() 同一套规则、同一优先级。
 *   1) 委派（origin/parentSession/delegationDepth）—— 必须最先判，旧通道前缀派出的子任务也照此
 *   2) 旧飞书通道前缀 dfc:
 *   3) 预设命中飞书助手名单
 *   4) 顶层且预设全在桌面名单
 *   5) 其余未知
 * header 里没有预设（null/空）时返回 null，表示"这条判不了"，交给缓存兜底。
 */
export const classifyHeader = (header, registry) => {
  const h = header || {};
  const sid = String(h.id || '');
  const preset = typeof h.agentPreset === 'string' ? h.agentPreset : '';
  const feishu_bot = new Set((registry && registry.feishu_bot_presets) || []);
  const desk = new Set((registry && registry.desktop_presets) || []);

  if (h.origin === 'subagent' || (Number.isInteger(h.delegationDepth) && h.delegationDepth > 0) || h.parentSession) {
    return { role: ROLE_DELEGATED, basis: 'delegated',
      evidence: 'origin=' + JSON.stringify(h.origin ?? null) + ' delegationDepth=' + JSON.stringify(h.delegationDepth ?? null)
        + ' parentSession=' + JSON.stringify(h.parentSession ?? null) };
  }
  const legacyPrefix = String((registry && registry.legacy_feishu_prefix) || LEGACY_FEISHU_PREFIX);
  if (legacyPrefix && sid.startsWith(legacyPrefix)) {
    return { role: ROLE_FEISHU_BOT, basis: 'legacy-feishu-prefix', evidence: '会话 id 前缀 ' + legacyPrefix };
  }
  if (!preset) return null;
  if (feishu_bot.has(preset)) return { role: ROLE_FEISHU_BOT, basis: 'preset', evidence: '预设 ' + preset + ' 在飞书助手名单' };
  if (desk.has(preset)) return { role: ROLE_DESKTOP, basis: 'preset', evidence: '预设 ' + preset + ' 在桌面名单' };
  return { role: ROLE_UNKNOWN, basis: 'preset-unknown', evidence: '预设 ' + preset + ' 不在任何名单' };
};

/**
 * 投递前复验（R1 的核心）：**缓存可用 + 目标活着（或已持久化、可叫醒） + 角色按当前头判**。
 * 三步缺一不可；缓存与当前头给出的角色不一致就拒（不替你选一个）。
 *
 * 2026-11-17 晚 R3（冷唤醒）：投递不再要求目标**此刻活着**。已持久化、身份能从
 * 当前头判清的会话也算合格——投递时由宿主 resolveAgent() 把它叫醒。仍然拒绝
 * "既不在活列表、又没持久化记录"的会话；身份判定和活会话完全同一套，不放松。
 *
 * @param {object} args
 *   identity: readIdentityCache() 的返回值
 *   sessions: 宿主会话清单（活 + 已持久化；含 header 字段）
 *   sessionId: 目标
 *   expect: 'feishu_bot' | 'desktop' | undefined
 *   allowPersisted: 允许"已持久化但当前不活"的目标（默认 false；投递方显式打开）
 */
export const verifyTarget = ({ identity, sessions, sessionId, expect, allowPersisted = false }) => {
  if (!identity || !identity.ok) {
    return { ok: false, code: 422, error: 'identity_unavailable',
      detail: (identity && identity.error) || '身份缓存过期或缺失',
      hint: '先跑 python tools/session_identity.py --refresh' };
  }
  const sid = String(sessionId || '');
  const record = (sessions || []).find((s) => s && String(s.sessionId) === sid) || null;
  if (!record) {
    return { ok: false, code: 410, error: 'target_not_live',
      detail: '目标会话不在宿主的会话清单里（既没活着、也没有持久化记录），不投递',
      hint: '让收件方开一个窗口，或改用 /candidates 重新解析', sessionId: sid };
  }
  if (record.archived) {
    return { ok: false, code: 410, error: 'target_archived',
      detail: '目标会话已归档（archived），不论是否持久化都不投递',
      hint: '归档会话不可投；让收件方开新窗口，或改用 /candidates 重新解析', sessionId: sid };
  }
  if (!record.live && !(allowPersisted && record.persisted)) {
    return { ok: false, code: 410, error: 'target_not_live',
      detail: '目标会话当前没活着，也没有可用的持久化记录；不投递',
      hint: '让收件方发一句把它叫醒，或确认会话文件还在', sessionId: sid };
  }

  const cachedRole = roleOf(identity, sid);
  const fromHeader = classifyHeader(record.header, identity.registry);
  const headerRole = fromHeader ? fromHeader.role : null;

  if (headerRole === ROLE_DELEGATED) {
    return { ok: false, code: 422, error: 'target_delegated', role: ROLE_DELEGATED,
      detail: '目标是委派/子代理会话，不能当工作投递目标', evidence: fromHeader.evidence };
  }

  const role = headerRole || cachedRole;
  const source = headerRole ? (record.live ? 'live-header' : 'persisted-header') : 'cache';

  if (role === ROLE_UNKNOWN) {
    return { ok: false, code: 422, error: 'target_unconfirmed', role,
      detail: '目标会话身份未确认（预设缺失或未登记），不投递',
      evidence: fromHeader ? fromHeader.evidence : '缓存里也没有可判证据' };
  }
  if (headerRole && cachedRole !== ROLE_UNKNOWN && cachedRole !== headerRole) {
    return { ok: false, code: 409, error: 'identity_conflict', role: headerRole, cached_role: cachedRole,
      detail: '当前头判 ' + headerRole + '，缓存判 ' + cachedRole + '，两者矛盾，不替你选',
      hint: '跑 python tools/session_identity.py --refresh 后重试' };
  }
  if (expect && role !== expect) {
    return { ok: false, code: 422, error: 'target_role_mismatch', role, expect,
      detail: '目标身份是 ' + role + '，与调用方声明的 ' + expect + ' 不符' };
  }
  return {
    ok: true, role, source,
    sessionId: sid,
    live: Boolean(record.live),
    persisted: Boolean(record.persisted),
    cache_built_at: identity.builtAt,
    evidence: fromHeader
      ? fromHeader.evidence + (record.live ? '' : '（当前不活，投递时叫醒）')
      : '缓存 ' + role + '（当前头没有预设）',
  };
};

/**
 * 从"活着的会话"里挑唯一合格目标。
 * 规则与候选数量无关：**唯一就用，多于一个就是歧义，零个就是没目标**。
 * （旧实现"候选多于 8 个改按创建时间"已删除：那会让 8→9 时选出另一个人。）
 */
export const pickUnique = (liveSessions, identity, role, cwdKeyword = '') => {
  if (!identity || !identity.ok) {
    return { ok: false, code: 422, error: 'identity_unavailable', role,
      detail: (identity && identity.error) || '身份缓存过期或缺失',
      hint: '先跑 python tools/session_identity.py --refresh' };
  }
  const live = (liveSessions || []).filter((s) => s && s.sessionId && !s.archived);
  const inCwd = cwdKeyword
    ? live.filter((s) => String(s.cwd || '').toLowerCase().includes(String(cwdKeyword).toLowerCase()))
    : live;
  const roleOfLive = (s) => {
    const h = classifyHeader(s.header, identity.registry);
    return h ? h.role : roleOf(identity, s.sessionId);
  };
  const qualified = inCwd.filter((s) => roleOfLive(s) === role);
  if (!qualified.length) {
    return { ok: false, code: 404, error: 'no_target', role,
      detail: '该 cwd 下没有身份为 ' + role + ' 且仍活着的会话',
      seen: inCwd.map((s) => ({ sessionId: s.sessionId, role: roleOfLive(s) })),
      cache_built_at: identity.builtAt };
  }
  if (qualified.length > 1) {
    return { ok: false, code: 409, error: 'ambiguous', role,
      detail: '有 ' + qualified.length + ' 个合格目标，不按最近活动替你挑',
      candidates: qualified.map((s) => ({ sessionId: s.sessionId, createdAt: s.createdAt })),
      cache_built_at: identity.builtAt };
  }
  const one = qualified[0];
  return { ok: true, role,
    session: { sessionId: one.sessionId, cwd: one.cwd, createdAt: one.createdAt, live: one.live },
    candidates: 1,
    basis: ['live=1', 'identity=' + role, 'cache_built_at=' + identity.builtAt] };
};
