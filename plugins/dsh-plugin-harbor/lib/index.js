/**
 * 港湾（harbor）：给飞书助手一个投递口，把一句话送进指定 DSH 会话。
 *
 * 为什么不用 HTTP 网关 + mux 协议：那要反推私有帧格式，且要 token。
 * 这里跑在宿主进程**内部**，直接用 cordis 服务 `sessionController` 的
 * `prompt()` 方法——不碰协议、不要 token，也不触发 session/writer-held
 * （那是挡"第二个宿主的 viewer"，不是挡宿主自己）。
 *
 * 两条入口，共用同一套处理逻辑：
 *   1) 宿主自带 webServer 的 127.0.0.1 路由（桌面 app 内部用）
 *   2) 插件自己起的**局域网小服务**（绑一个具体本机 IP，带口令）
 *      —— 不动桌面 app 的监听，避免弄坏用户的窗口；
 *         又因为同在宿主进程里，所以照样能 prompt 本会话。
 *
 * 接口：
 *   GET  /api/harbor/health                 健康检查（不需要口令）；含身份缓存可用性
 *   GET  /api/harbor/sessions?limit=10      列会话（拿 sessionId）
 *   GET  /api/harbor/candidates?cwd=&role=  工作投递的目标解析：唯一合格才给，歧义即 409
 *   GET  /api/harbor/latest?cwd=关键词       普通"最近会话"查询（不是身份解析，投递别用它）
 *   POST /api/harbor/say                    往会话投一句话
 *        body {"sessionId":"…","text":"…","mode":"queue"|"steer","expect?":"feishu_bot"|"desktop"}
 *        expect 给了就复验身份；不给也照样拦住委派会话与身份未确认的会话
 *   POST /api/harbor/feishu_bot-reply         飞书助手定向回话给桌面助手（独立口令）
 *        body {"sessionId?":"…","text":"…","replyId?":"…","cwd?":"…"}
 *        2026-11-17 起 sessionId 可省略，但**缺省不再是"取最新"**：走身份解析，
 *        只在这条 cwd 下恰好一个合格的桌面会话时才投；两个以上返回 409 歧义。
 *
 * 身份从哪来（2026-11-17 首批修复）：
 *   会话归属由 tools/session_identity.py 读原始会话头（agentPreset/origin/
 *   parentSession/delegationDepth）与预设切换事件判定，落 工作台/_tmp_/会话身份.json。
 *   插件只读这份缓存，并负责 live 判断；缓存过期或缺失一律拒绝投递，不放宽成猜。
 */
import { createServer } from 'node:http';
import { readFileSync, appendFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { homedir } from 'node:os';
import { readIdentityCache, verifyTarget as verifyTargetOf, roleOf, rowOf, pickUnique } from './identity.js';
import { randomUUID } from 'node:crypto';
// 2026-11-17 R1：删掉这条 import。它指向 app.asar 里的宿主动态库，只在 Electron 里解得开，
// 而这份代码在普通 node 下要能被离线用例直接加载（否则 dispatch 只能靠重启宿主验）。
// 它也从没被用到——/say 一直在用 sessionController.prompt 的 content 数组。

// 2026-11-22 批7：运行时路径跟随写主开关（与 tools/session_identity.py、archive_paths 映射一致）。
// hub 模式落 档案库根 对应区；old 模式或读不到开关回落旧路径。改开关后需重启宿主才生效（加载时读一次）。
const _OLD_TMP = (process.env.HARBOR_TMP_DIR || '').trim().replace(/\\/g, '/') || (process.cwd().replace(/\\/g, '/') + '/_tmp_');
const _HUB_ROOT = (() => {
  try {
    const base = process.env.LOCALAPPDATA || ((process.env.USERPROFILE || '') + '/AppData/Local');
    const dir = base + '/agent-archive';
    if (readFileSync(dir + '/write_mode.txt', 'utf8').trim() !== 'hub') return '';
    return readFileSync(dir + '/root.txt', 'utf8').trim().replace(/\\/g, '/');
  } catch { return ''; }
})();
const _runtimePath = (hubRel, oldName) => (_HUB_ROOT ? _HUB_ROOT + '/' + hubRel : _OLD_TMP + '/' + oldName);

export const name = 'dsh-plugin-harbor';
export const inject = ['webServer', 'sessionController', 'agents'];

const MAX_BODY = 256 * 1024;

function json(res, code, obj) {
  const body = JSON.stringify(obj, null, 2);
  res.writeHead(code, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  });
  res.end(body);
}

async function readBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY) throw new Error('body too large');
    chunks.push(chunk);
  }
  if (!size) return {};
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

/**
 * 局域网口子到底开不开（2026-11-17 第二批 第二-7）。
 * 以前只看 lanHost/lanPort，allowLan=false 照样绑配置的 lanPort——配置里的「关」根本不生效。
 * 现在两件事都要成立：allowLan 明确为 true，且给了具体 IP 和端口。
 */
export const lanPlan = (config = {}) => {
  const enabled = config.allowLan === true;
  const host = String(config.lanHost || '').trim();
  const port = Number(config.lanPort || 0);
  return { enabled, host, port, start: enabled && Boolean(host) && port > 0 };
};
export function apply(ctx, config = {}) {
  const token = (config.token || '').trim();
  let feishuReplyToken = (config.feishuReplyToken || '').trim();
  // 飞书助手回话时若不手填 sessionId，就按这个 cwd 自动挑桌面助手当前窗口（2026-11-16 加）
  let feishuSessionCwd = String(config.feishuSessionCwd || '').trim();
  try {
    const chPath = (process.env.DSH_CHANNEL_FILE || (_HUB_ROOT ? _HUB_ROOT + '/_state/通道.json' : '')).trim();
    if (chPath) {
      const ch = JSON.parse(readFileSync(chPath, 'utf8'));
      if (!feishuReplyToken) feishuReplyToken = String((ch.desktop_bot && ch.desktop_bot.feishu_reply_token) || '').trim();
      if (!feishuSessionCwd) feishuSessionCwd = String((ch.desktop_bot && ch.desktop_bot.session_cwd) || '').trim();
    }
  } catch {}
  if (!feishuSessionCwd) feishuSessionCwd = 'default-workspace';
  const plan = lanPlan(config);
  const allowLan = plan.enabled;
  const lanHost = plan.host;
  const lanPort = plan.port;

  const authorized = (req, expected = token) => {
    if (!expected) return false;
    const url = new URL(req.url || '/', 'http://harbor.invalid');
    const given = url.searchParams.get('token') || req.headers['x-harbor-token'] || '';
    const a = Buffer.from(String(given));
    const b = Buffer.from(expected);
    if (a.length !== b.length) return false;
    return a.equals(b);
  };

  // 2026-11-17 R1：把头里的身份字段一起带出来。原先只留 id/cwd/createdAt/live，
  // 插件就只能信缓存——目标刚换了预设、或已经死掉，它看不出来。现在按当前头判。
  // 2026-11-23 归档闸（绝不往已归档对话投）：优先宿主 workspaceRegistry 服务，
  // 取不到退而读 dsh_home()/storages/workspace.json 的 global.archivedSessionIds。
  const dshHome = () => {
    const env = process.env.DSH_HOME;
    if (env) return env.replace(/\\/g, '/');
    const up = process.env.USERPROFILE;
    if (up) return (up + '/.dsh').replace(/\\/g, '/');
    const hd = process.env.HOMEDRIVE, hp = process.env.HOMEPATH;
    if (hd && hp) return (hd + hp + '/.dsh').replace(/\\/g, '/');
    return (homedir() + '/.dsh').replace(/\\/g, '/');
  };
  const readArchivedIds = () => {
    try {
      const wr = ctx.get('workspaceRegistry');
      const a = wr && wr.archivedSessionIds;
      if (a) {
        const arr = Array.isArray(a) ? a : (typeof a === 'object' ? Object.keys(a) : null);
        if (arr) return new Set(arr.map(String));
      }
    } catch {}
    try {
      const ws = JSON.parse(readFileSync(dshHome() + '/storages/workspace.json', 'utf8'));
      const a = (ws && ws.global && ws.global.archivedSessionIds) || [];
      const arr = Array.isArray(a) ? a : Object.keys(a || {});
      return new Set(arr.map(String));
    } catch { return new Set(); }
  };
  const shapes = (records, archivedSet) =>
    (records || [])
      .map((r) => {
        const h = (r && r.header) || {};
        return {
          sessionId: h.id,
          cwd: h.cwd,
          createdAt: h.createdAt,
          live: Boolean(r && r.live),
          persisted: Boolean(r && r.persisted),
          archived: Boolean(archivedSet && archivedSet.has(String(h.id))),
          header: {
            id: h.id,
            agentPreset: h.agentPreset ?? null,
            delegationDepth: h.delegationDepth ?? null,
            origin: h.origin ?? null,
            parentSession: h.parentSession ?? null,
          },
        };
      })
      .filter((x) => x.sessionId);

  const fetchSessions = async () => {
    const q = ctx.get('sessionQuery');
    if (!q || typeof q.listSessions !== 'function') return null;
    return shapes(await q.listSessions(), readArchivedIds());
  };

  /** 取一个会话的事件清单（只有 seq/type/time，没有正文）。 */
  const listEvents = async (sessionId) => {
    const q = ctx.get('sessionQuery');
    if (!q || typeof q.listEvents !== 'function') return null;
    return (await q.listEvents(sessionId)) || [];
  };

  /** 取单条事件的完整内容（正文在这里）。 */
  const readEvent = async (sessionId, seq) => {
    const q = ctx.get('sessionQuery');
    if (!q || typeof q.readEvent !== 'function') return null;
    return await q.readEvent({ sessionId, seq });
  };

  // ---- 身份判定（2026-11-17 首批修复 A01） ----
  // 规则与目标选择在 lib/identity.js（纯函数，可离线单测）；这里只接宿主能力：
  // 读缓存、问 live。两边不重复判，也不互相猜。
  const IDENTITY_CACHE = String(config.identityCache || process.env.DSH_IDENTITY_CACHE ||
    _runtimePath('_local/cache/会话身份.json', '会话身份.json'));
  const IDENTITY_MAX_AGE_MS = 10 * 60 * 1000;
  const readIdentity = () => readIdentityCache(IDENTITY_CACHE, IDENTITY_MAX_AGE_MS);

  /**
   * 工作投递的目标解析：身份缓存 + live 过滤，规则在 identity.js 的 pickUnique。
   * 唯一合格才返回；多于一个报歧义（不按最近活动/创建时间替你挑）；零个报无目标；
   * 证据不全（缓存过期、缺失、读不出）一律拒绝，**不放宽成"取最新"**。
   */
  const resolveTarget = async (cwd, role) => {
    const identity = readIdentity();
    const all = identity.ok ? await fetchSessions() : [];
    if (identity.ok && all === null) {
      return { ok: false, code: 500, error: 'sessionQuery 不可用', role };
    }
    return pickUnique((all || []).filter((s) => s.live && !s.archived), identity, role, cwd);
  };

  /**
   * 投递前的**统一复验**（2026-11-17 R1/R3）：缓存可用 + 目标活着或已持久化 + 角色按当前头判。
   * 三条投递路径（显式、通道绑定、正向解析）都走这一个函数，谁也不许只查缓存。
   */
  const verifyDelivery = async (sessionId, expect) => {
    const identity = readIdentity();
    if (!identity.ok) return verifyTargetOf({ identity, sessions: [], sessionId, expect });
    const all = await fetchSessions();
    if (all === null) {
      return { ok: false, code: 500, error: 'sessionQuery 不可用',
        detail: '宿主没提供 sessionQuery，无法确认目标此刻是否还活着，不投递' };
    }
    // 2026-11-17 晚 R3：把已持久化、当前不活的会话也交给身份判定；
    // 投递时由宿主 resolveAgent() 叫醒。仍然只信当前头/缓存，不放松身份。
    const rec = (all || []).find((s) => s && String(s.sessionId) === String(sessionId));
    if (rec && rec.archived) {
      return { ok: false, code: 410, error: 'target_archived', sessionId,
        detail: '目标会话已归档，绝不投递（插件归档闸）' };
    }
    return verifyTargetOf({ identity, sessions: all, sessionId, expect, allowPersisted: true });
  };

  /**
   * 来件关联（R2）：谁投进哪条会话，插件记一行；回话时先沿这条关系回去。
   * 只记 ID 与时间，不记正文——正文已经进了会话事件，没必要再抄一份。
   */
  const INBOUND_LEDGER = String(config.inboundLedger || process.env.DSH_INBOUND_LEDGER ||
    _runtimePath('_state/来件关联.jsonl', '来件关联.jsonl'));
  const recordInbound = (row) => {
    try {
      mkdirSync(dirname(INBOUND_LEDGER), { recursive: true });
      appendFileSync(INBOUND_LEDGER,
        JSON.stringify({ at: new Date().toISOString(), ...row }) + '\n', 'utf8');
      return true;
    } catch {
      return false;
    }
  };

  /**
   * 消息去重（2026-11-17 补 GPT 审计 §4.2 第 9 条）：同一个 msgId 只投一次。
   * 存在一张很小的 jsonl 里，只留最近 2000 条；读不出来就当没记过——
   * 宁可重复一条（对方会看见同一个号），也不要因为读不了文件就卡死投递。
   */
  const MSG_LEDGER = String(config.msgLedger || process.env.DSH_MSG_LEDGER ||
    _runtimePath('_local/cache/消息去重.jsonl', '消息去重.jsonl'));
  const MSG_LEDGER_KEEP = 2000;
  const msgSeen = (msgId) => {
    if (!msgId) return false;
    try {
      const rows = readFileSync(MSG_LEDGER, 'utf8').split('\n').filter(Boolean).slice(-MSG_LEDGER_KEEP);
      for (const ln of rows) {
        try { if (JSON.parse(ln).msgId === msgId) return true; } catch {}
      }
      return false;
    } catch { return false; }
  };
  const msgMark = (msgId, extra = {}) => {
    if (!msgId) return false;
    try {
      mkdirSync(dirname(MSG_LEDGER), { recursive: true });
      appendFileSync(MSG_LEDGER, JSON.stringify({ at: new Date().toISOString(), msgId, ...extra }) + '\n', 'utf8');
      return true;
    } catch { return false; }
  };

  /** 从一条事件里抽出 assistant 的文本（结构可能随版本变，取不到就返回空串）。 */
  const assistantText = (ev) => {
    const data = (ev && ev.data) || {};
    const msg = data.message || data;
    const content = msg && msg.content;
    if (typeof content === 'string') return content;
    if (!Array.isArray(content)) return '';
    return content
      .filter((p) => p && (p.type === 'text' || typeof p.text === 'string'))
      .map((p) => p.text || '')
      .join('');
  };

  const isAssistantMessage = (type) => typeof type === 'string' && type.includes('assistant') && type.includes('message');

  /**
   * 按 /latest 的规则挑一个目标会话：cwd 过滤 → live 优先 → 按最后事件时间排。
   * 抽取出来给 /latest、/tail、/feishu_bot-reply 三处共用，保证"没给 sessionId 就自动取"
   * 这条规则只有一份，不会漂。返回 {ok, error} 或 {ok, session, candidates, sortedBy}。
   */
  const resolveSession = async (cwd = '', opts = {}) => {
    const all = await fetchSessions();
    if (all === null) return { ok: false, error: 'sessionQuery 不可用' };
    let pool = cwd ? all.filter((s) => (s.cwd || '').includes(cwd)) : all.slice();
    const exclude = Array.isArray(opts.exclude) ? opts.exclude.filter(Boolean) : [];
    if (exclude.length) pool = pool.filter((s) => !exclude.includes(s.sessionId));
    const live = pool.filter((s) => s.live);
    const scan = live.length ? live : pool;
    if (!scan.length) return { ok: false, error: 'no session' };
    // 2026-11-17（首批修复 A01）：**规则只此一条**——活着的会话按最后一条事件时间排，
    // 查不到事件时间的退到创建时间。删掉了原来"候选多于 8 个就改按创建时间"的分支：
    // 那会让候选从 8 个变 9 个时选出另一个人，属于语义悄悄改变，不是性能优化。
    const cap = 40;
    const head = scan.slice(0, cap);
    const canTime = typeof ctx.get('sessionQuery')?.listEvents === 'function';
    if (canTime) {
      await Promise.all(
        head.map(async (s) => {
          try {
            const evs = await listEvents(s.sessionId);
            const last = evs && evs.length ? evs[evs.length - 1] : null;
            s.lastEventTime = last ? Number(last.time) || 0 : 0;
          } catch {
            s.lastEventTime = 0;
          }
        }),
      );
      head.sort((a, b) => (b.lastEventTime || 0) - (a.lastEventTime || 0) || (b.createdAt || 0) - (a.createdAt || 0));
    } else {
      head.sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
    }
    return {
      ok: true,
      session: head[0],
      candidates: head.length,
      sortedBy: canTime ? 'last-event' : 'createdAt',
      truncated: scan.length > cap,
    };
  };

  /** 所有入口共用的分发。返回 true 表示这个路径归港湾管。 */
  async function dispatch(req, res) {
    const url = new URL(req.url || '/', 'http://harbor.invalid');
    const path = url.pathname;

    if (path === '/api/harbor/health') {
      const identity = readIdentity();
      json(res, 200, {
        ok: true,
        plugin: name,
        version: '2026-11-17-batch1-r3-coldwake',
        accepts_persisted: true,
        allowLan,
        needToken: Boolean(token),
        lan: plan.start ? `${lanHost}:${lanPort}` : null,
        identity: {
          available: identity.ok,
          built_at: identity.builtAt,
          age_ms: Number.isFinite(identity.age) ? identity.age : null,
          max_age_ms: IDENTITY_MAX_AGE_MS,
          sessions: identity.byId.size,
          registry: identity.registry ? {
            feishu_bot_presets: identity.registry.feishu_bot_presets || [],
            desktop_presets: identity.registry.desktop_presets || [],
          } : null,
        },
        verify: {
          requires_live: true,
          role_source: 'live-header-first',
          routes: ['/api/harbor/target'],
        },
        time: new Date().toISOString(),
      });
      return true;
    }

    if (path === '/api/harbor/sessions') {
      if (!authorized(req)) {
        json(res, 401, { ok: false, error: 'unauthorized' });
        return true;
      }
      try {
        const limit = Math.max(1, Math.min(Number(url.searchParams.get('limit') || 10), 50));
        const all = await fetchSessions();
        if (all === null) {
          json(res, 200, { ok: false, error: 'sessionQuery 不可用', sessions: [] });
          return true;
        }
        const cwd = url.searchParams.get('cwd') || '';
        const filtered = cwd ? all.filter((s) => (s.cwd || '').includes(cwd)) : all;
        filtered.sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
        json(res, 200, {
          ok: true,
          count: filtered.length,
          total: all.length,
          sessions: filtered.slice(0, limit),
        });
      } catch (error) {
        json(res, 500, { ok: false, error: String(error && error.message ? error.message : error) });
      }
      return true;
    }

    // 工作投递的目标解析（2026-11-17 首批修复）：唯一合格才给，歧义就拒。
    //   GET /api/harbor/candidates?cwd=关键词&role=desktop|feishu_bot
    if (path === '/api/harbor/candidates') {
      if (!authorized(req)) {
        json(res, 401, { ok: false, error: 'unauthorized' });
        return true;
      }
      try {
        const cwd = url.searchParams.get('cwd') || '';
        const role = (url.searchParams.get('role') || 'desktop').trim();
        const got = await resolveTarget(cwd, role);
        json(res, got.ok ? 200 : (got.code || 200), got);
      } catch (error) {
        json(res, 500, { ok: false, error: String(error && error.message ? error.message : error) });
      }
      return true;
    }

    // 普通"最近会话"查询。**保留，但工作投递不再走这里**（说明 §5.4）：
    // 它按活跃时间挑，分不清两侧；只有当调用方明确带 role 时，才改用身份解析。
    if (path === '/api/harbor/latest') {
      if (!authorized(req)) {
        json(res, 401, { ok: false, error: 'unauthorized' });
        return true;
      }
      try {
        const cwd = url.searchParams.get('cwd') || '';
        const role = (url.searchParams.get('role') || '').trim();
        if (role) {
          const got = await resolveTarget(cwd, role);
          json(res, got.ok ? 200 : (got.code || 200), got);
          return true;
        }
        const got = await resolveSession(cwd);
        if (!got.ok) {
          json(res, got.error === 'no session' ? 404 : 200, { ok: false, error: got.error });
          return true;
        }
        json(res, 200, {
          ok: true,
          session: got.session,
          candidates: got.candidates,
          sortedBy: got.sortedBy,
          truncated: Boolean(got.truncated),
          note: '这是最近会话查询，不是身份解析；工作投递请用 /candidates',
        });
      } catch (error) {
        json(res, 500, { ok: false, error: String(error && error.message ? error.message : error) });
      }
      return true;
    }

    // 只读回话口：拿"我最近说的话"，带游标（飞书助手 2026-11-11 10:08 要求）
    //   GET /api/harbor/tail?sessionId=…&n=3&cursor=<上次最后一个 seq>&cwd=…
    //   返回 {cursor, items:[{seq, time, text}]}；next 传 cursor 只拿新增。
    // 2026-11-17 第二批（第二-4）：加 order=asc 正序分页。
    //   默认 desc 是「拿最新 n 条」——新增 40 条时只回最后 3 条，游标却推进到 40，
    //   中间 37 条就永远拿不到。asc 从游标往后 n 条一段，能一页页取完（has_more 告诉你还有没有）。
    if (path === '/api/harbor/tail') {
      if (!authorized(req)) {
        json(res, 401, { ok: false, error: 'unauthorized' });
        return true;
      }
      try {
        const n = Math.max(1, Math.min(Number(url.searchParams.get('n') || 3), 30));
        const cursorRaw = url.searchParams.get('cursor');
        const cursor = cursorRaw === null || cursorRaw === '' ? null : Number(cursorRaw);
        let sessionId = (url.searchParams.get('sessionId') || '').trim();

        // 没给 sessionId 就按 latest 的规则挑一个（保留 cwd 过滤，与 /latest 一致）
        if (!sessionId) {
          const cwd = url.searchParams.get('cwd') || '';
          const got = await resolveSession(cwd);
          if (!got.ok) {
            json(res, got.error === 'no session' ? 404 : 200, { ok: false, error: got.error, items: [], cursor: null });
            return true;
          }
          sessionId = got.session.sessionId;
        }

        const wantAsc = (url.searchParams.get('order') || '').toLowerCase() === 'asc';
        const events = await listEvents(sessionId);
        if (events === null) {
          json(res, 200, { ok: false, error: 'sessionQuery 不可用', items: [], cursor: null });
          return true;
        }
        // 只要 assistant 正文，且（给了游标时）只拿游标之后的新增
        let picks = events.filter((e) => isAssistantMessage(e.type));
        if (cursor !== null && Number.isFinite(cursor)) picks = picks.filter((e) => Number(e.seq) > cursor);
        if (wantAsc) {
          // 正序：从游标往后逐条扫，凑够 n 条带正文的就停；游标推到"扫过的最大 seq"，
          // 这样即使中途全是工具事件也不会原地打转（每页至少前进一格）。
          let scannedMaxAsc = cursor === null ? null : cursor;
          const itemsAsc = [];
          let hasMore = false;
          for (let i = 0; i < picks.length; i += 1) {
            const e = picks[i];
            const seqNum = Number(e.seq);
            if (scannedMaxAsc === null || seqNum > scannedMaxAsc) scannedMaxAsc = seqNum;
            const win = await readEvent(sessionId, e.seq);
            const text = assistantText(win && win.target);
            if (text && text.trim()) {
              itemsAsc.push({ seq: seqNum, time: Number(e.time) || null, text });
              if (itemsAsc.length >= n) {
                hasMore = i < picks.length - 1;
                break;
              }
            }
          }
          json(res, 200, {
            ok: true, sessionId, cursor: scannedMaxAsc, count: itemsAsc.length,
            has_more: hasMore, order: 'asc', items: itemsAsc,
          });
          return true;
        }
        picks = picks.slice(-n);

        const items = [];
        let scannedMax = cursor === null ? null : cursor;
        for (const e of picks) {
          // 游标必须推进到"扫过的最大 seq"，哪怕这条没有正文——
          // 否则纯工具调用的 assistant 事件会被每次重扫（2026-11-11 10:20 实测到的问题）
          const seqNum = Number(e.seq);
          if (scannedMax === null || seqNum > scannedMax) scannedMax = seqNum;
          const win = await readEvent(sessionId, e.seq);
          const target = (win && win.target) || null;
          const text = assistantText(target);
          if (!text || !text.trim()) continue;
          items.push({ seq: seqNum, time: Number(e.time) || null, text });
        }
        // 游标 = 扫过的最大 seq（不是"最后一条有正文的 seq"）
        json(res, 200, {
          ok: true, sessionId, cursor: scannedMax, count: items.length,
          has_more: false, order: 'desc', items,
        });
      } catch (error) {
        json(res, 500, { ok: false, error: String(error && error.message ? error.message : error) });
      }
      return true;
    }

    // 统一目标验收（R1）：Python 侧三条投递路径都先打这里，再决定投不投。
    //   GET /api/harbor/target?sessionId=…&expect=feishu_bot|desktop
    //   200 = 可用（附 role/source/证据）；409 = 与缓存矛盾／歧义；410 = 已不在活会话里；
    //   422 = 身份不可用／委派／未确认／与声明不符。
    if (path === '/api/harbor/target') {
      if (!authorized(req)) {
        json(res, 401, { ok: false, error: 'unauthorized' });
        return true;
      }
      const sessionId = (url.searchParams.get('sessionId') || '').trim();
      const expect = (url.searchParams.get('expect') || '').trim();
      if (!sessionId) {
        json(res, 400, { ok: false, error: 'sessionId required' });
        return true;
      }
      try {
        const got = await verifyDelivery(sessionId, expect);
        json(res, got.ok ? 200 : (got.code || 422), {
          ...got,
          expect: expect || null,
          cache_path: IDENTITY_CACHE,
        });
      } catch (error) {
        json(res, 500, { ok: false, error: String(error && error.message ? error.message : error) });
      }
      return true;
    }

    if (path === '/api/harbor/feishu_bot-reply') {
      if (req.method !== 'POST') {
        json(res, 405, { ok: false, error: 'use POST' });
        return true;
      }
      if (!authorized(req, feishuReplyToken)) {
        json(res, 401, { ok: false, error: 'unauthorized' });
        return true;
      }
      let body;
      try { body = await readBody(req); }
      catch (error) {
        json(res, 400, { ok: false, error: 'bad body: ' + String(error && error.message) });
        return true;
      }
      let sessionId = String(body.sessionId || '').trim();
      const text = String(body.text || '').trim();
      const replyId = String(body.replyId || randomUUID()).trim();
      if (!text) {
        json(res, 400, { ok: false, error: 'text required' });
        return true;
      }
      // 没给 sessionId 就做**身份解析**（不是"取最新"）：唯一合格才投，歧义就拒。
      // 2026-11-17 首批修复：原来这里是 resolveSession（活跃时间），飞书助手刚说完话时会把话投给自己。
      let basis = ['explicit_session_id'];
      if (!sessionId) {
        const cwd = String(body.cwd || feishuSessionCwd || '').trim();
        const got = await resolveTarget(cwd, 'desktop');
        if (!got.ok) {
          json(res, got.code || 200, got);
          return true;
        }
        sessionId = got.session.sessionId;
        basis = got.basis || [];
      }
      // 显式给的 sessionId 同样要过身份关：不能投到飞书助手自己、委派会话或未确认会话，
      // 也不能投给一个已经关掉的窗口（R1：缓存合格 ≠ 目标还活着）。
      const checked = await verifyDelivery(sessionId, 'desktop');
      if (!checked.ok) {
        json(res, checked.code || 422, checked);
        return true;
      }
      const dupKey = 'reply:' + replyId;
      if (msgSeen(dupKey)) {
        json(res, 200, { ok: true, accepted: false, duplicate: true, replyId, sessionId,
          note: '这个 replyId 之前投过，没有重复投' });
        return true;
      }
      try {
        const ack = await ctx.sessionController.prompt(
          { requestId: 'astrbot-feishu_bot-' + replyId, sessionId, mode: 'queue', content: [{ type: 'text', text }] },
          new AbortController().signal,
        );
        msgMark(dupKey, { sessionId, channel: 'feishu_bot-reply' });
        const from = String(body.from || '').trim();
        recordInbound({ to: sessionId, from: from || null,
          fromRole: from ? roleOf(readIdentity(), from) : null,
          taskId: body.taskId ? String(body.taskId) : null,
          requestId: 'astrbot-feishu_bot-' + replyId, mode: 'queue', channel: 'feishu_bot-reply' });
        json(res, 200, { ok: true, accepted: true, source: 'astrbot-feishu_bot', replyId, sessionId,
          role: checked.role, verified: checked, basis, result: ack ?? null,
          note: 'accepted 只表示已受理，不代表对方已完成' });
      } catch (error) {
        json(res, 500, { ok: false, error: String(error && error.message ? error.message : error) });
      }
      return true;
    }

    // 冷唤醒（2026-11-17 晚 R3）：只把目标会话挂回宿主，不发任何消息。
    //   POST /api/harbor/wake {sessionId, expect}
    // 给"投递前先确认能叫醒"和排查用；/say 在冷目标上会自己调同一条路。
    if (path === '/api/harbor/wake') {
      if (req.method !== 'POST') {
        json(res, 405, { ok: false, error: 'use POST' });
        return true;
      }
      if (!authorized(req)) {
        json(res, 401, { ok: false, error: 'unauthorized' });
        return true;
      }
      let body;
      try {
        body = await readBody(req);
      } catch (error) {
        json(res, 400, { ok: false, error: 'bad body: ' + String(error && error.message) });
        return true;
      }
      const sessionId = String(body.sessionId || '').trim();
      const expect = ([body.expect, body.role].find((v) => typeof v === 'string' && v.trim()) || '').trim();
      if (!sessionId) {
        json(res, 400, { ok: false, error: 'sessionId required' });
        return true;
      }
      const checked = await verifyDelivery(sessionId, expect);
      if (!checked.ok) {
        json(res, checked.code || 422, checked);
        return true;
      }
      if (checked.live) {
        json(res, 200, { ok: true, woke: false, live: true, sessionId,
          role: checked.role, verified: checked, note: '目标本来就在活会话里' });
        return true;
      }
      try {
        const resolved = await ctx.sessionController.resolveAgent(sessionId);
        if (resolved && resolved.error) throw resolved.error;
        json(res, 200, { ok: true, woke: true, live: true, sessionId,
          role: checked.role, verified: checked, note: '已叫醒；没有发送任何消息' });
      } catch (error) {
        json(res, 502, { ok: false, woke: false, error: 'wake_failed', sessionId,
          detail: String(error && error.message ? error.message : error),
          verified: checked,
          hint: '会话文件可能损坏、被别的写者占着，或已经不在宿主里；先让她发一句再试' });
      }
      return true;
    }

    if (path === '/api/harbor/say') {
      if (req.method !== 'POST') {
        json(res, 405, { ok: false, error: 'use POST' });
        return true;
      }
      if (!authorized(req)) {
        json(res, 401, { ok: false, error: 'unauthorized' });
        return true;
      }
      let body;
      try {
        body = await readBody(req);
      } catch (error) {
        json(res, 400, { ok: false, error: 'bad body: ' + String(error && error.message) });
        return true;
      }
      const sessionId = String(body.sessionId || '').trim();
      const text = String(body.text || '').trim();
      const mode = body.mode === 'steer' ? 'steer' : 'queue';
      // 调用方声明"我要投给谁"（expect=feishu_bot|desktop）。不给也照样拦住委派与未确认会话。
      const expect = ([body.expect, body.role].find((v) => typeof v === 'string' && v.trim()) || '').trim();
      if (!sessionId) {
        json(res, 400, { ok: false, error: 'sessionId required',
          hint: '工作投递先 GET /api/harbor/candidates?cwd=…&role=… 拿唯一目标' });
        return true;
      }
      if (!text) {
        json(res, 400, { ok: false, error: 'text required' });
        return true;
      }
      const checked = await verifyDelivery(sessionId, expect);
      if (!checked.ok) {
        json(res, checked.code || 422, checked);
        return true;
      }
      const msgId = String(body.msgId || '').trim();
      const taskId = body.taskId ? String(body.taskId) : '';
      const replyTo = body.replyTo ? String(body.replyTo) : '';
      if (msgId && msgSeen(msgId)) {
        json(res, 200, { ok: true, accepted: false, duplicate: true, msgId, sessionId,
          taskId: taskId || null, replyTo: replyTo || null,
          note: '这个 msgId 之前投过，没有重复投' });
        return true;
      }
      // 2026-11-17 晚 R3：冷会话先叫醒，再投。resolveAgent() 只把 Agent 挂回宿主，
      // 不生成任何消息；失败就明确报 wake_failed，不把正文投进一个起不来的会话。
      let woke = false;
      if (!checked.live) {
        try {
          const resolved = await ctx.sessionController.resolveAgent(sessionId);
          if (resolved && resolved.error) throw resolved.error;
          woke = true;
        } catch (error) {
          json(res, 502, { ok: false, error: 'wake_failed',
            sessionId, detail: String(error && error.message ? error.message : error),
            verified: checked,
            hint: '会话文件可能损坏、被别的写者占着，或已经不在宿主里；先让她发一句再试' });
          return true;
        }
      }
      const requestId = 'harbor-' + randomUUID();
      const from = String(body.from || '').trim();
      const om = String(text).match(/^\[ORIGIN\s+peer_agent=([^\s\]|]+)([^\]]*)\]/);
      const originPeer = om ? om[1] : null;
      const originTrust = om ? ((/trust=([^\s\]|]+)/.exec(om[2]) || [])[1] || null) : null;
      const originNoIrrev = om ? /no_irreversible_approval/.test(om[2]) : false;
      try {
        const ack = await ctx.sessionController.prompt(
          { requestId, sessionId, mode, content: [{ type: 'text', text }] },
          new AbortController().signal,
        );
        msgMark(msgId, { sessionId, channel: 'say' });
        const logged = recordInbound({ to: sessionId, from: from || null,
          fromRole: from ? roleOf(readIdentity(), from) : null,
          taskId: taskId || null, msgId: msgId || null, replyTo: replyTo || null,
          requestId, mode, channel: 'say',
          originPeer, originTrust, originNoIrreversible: originNoIrrev });
        json(res, 200, { ok: true, accepted: true, mode, sessionId, requestId,
          role: checked.role, verified: checked, woke, expect: expect || null,
          msgId: msgId || null, taskId: taskId || null, replyTo: replyTo || null,
          inbound_logged: logged, result: ack ?? null,
          note: 'accepted 只表示已受理，不代表对方已完成' });
      } catch (error) {
        json(res, 500, {
          ok: false,
          error: String(error && error.code ? error.code : 'failed'),
          message: String(error && error.message ? error.message : error),
        });
      }
      return true;
    }

    return false;
  }

  ctx.effect(() => {
    const disposers = [];

    // 入口 1：宿主自带 webServer（127.0.0.1）
    for (const routePath of [
      '/api/harbor/health',
      '/api/harbor/sessions',
      '/api/harbor/candidates',
      '/api/harbor/latest',
      '/api/harbor/tail',
      '/api/harbor/target',
      '/api/harbor/wake',
      '/api/harbor/say',
      '/api/harbor/feishu_bot-reply',
    ]) {
      disposers.push(
        ctx.webServer.register({
          kind: 'exact',
          path: routePath,
          handler: (req, res) => {
            dispatch(req, res).catch((error) => {
              json(res, 500, { ok: false, error: String(error && error.message ? error.message : error) });
            });
          },
        }),
      );
    }

    // 入口 2：局域网小服务（绑具体 IP，不动桌面 app 的监听）
    let lan = null;
    if (plan.start) {
      try {
        lan = createServer((req, res) => {
          dispatch(req, res).then(
            (handled) => {
              if (!handled) json(res, 404, { ok: false, error: 'not found' });
            },
            (error) => {
              json(res, 500, { ok: false, error: String(error && error.message ? error.message : error) });
            },
          );
        });
        const bind = (attempt = 1) => {
          lan.listen(lanPort, lanHost, () => {
            ctx.logger?.info?.('[harbor] 局域网口子监听 %s:%d', lanHost, lanPort);
          });
          lan.once('error', (error) => {
            // 2026-11-11 23:5x 加：原来失败一次就永久放弃。
            // 开机/登录时网卡可能还没拿到 IP，bind 会 EADDRNOTAVAIL，之后配置的 lanPort 就一直是死的
            // （今晚 12:28 那次宿主重启后就是这样，谁都没收到报警）。
            const msg = error && error.message ? error.message : String(error);
            if (attempt >= 20) {
              ctx.logger?.warn?.('[harbor] 局域网口子起不来（已重试 %d 次，放弃）：%s', attempt, msg);
              return;
            }
            ctx.logger?.warn?.('[harbor] 局域网口子起不来：%s（%d/20，15 秒后重试）', msg, attempt);
            setTimeout(() => bind(attempt + 1), 15000);
          });
        };
        bind();
      } catch (error) {
        ctx.logger?.warn?.('[harbor] 局域网口子创建失败：%s', error && error.message);
      }
    }

    return () => {
      for (const dispose of disposers) {
        try {
          dispose();
        } catch {
          /* 关停时忽略 */
        }
      }
      if (lan) {
        try {
          lan.close();
        } catch {
          /* 关停时忽略 */
        }
      }
    };
  });
}

export default { name, inject, apply };
