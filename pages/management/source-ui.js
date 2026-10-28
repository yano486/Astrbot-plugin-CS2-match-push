export const SOURCE_DEFAULTS = Object.freeze({
  data_source_mode: 'pandascore',
  primary_source: 'pandascore',
  hltv_browser_fallback: true,
  hltv_headful: false,
  hltv_request_min_gap: 90,
  hltv_cache_ttl: 180,
  hltv_min_stars: 1,
});

const sourceNames = { pandascore: 'PandaScore', hltv: 'HLTV' };
const modeNames = { pandascore: '仅 PandaScore', hltv: '仅 HLTV', both: '双源' };
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char]);
const sourceName = source => Object.hasOwn(sourceNames, source) ? sourceNames[source] : String(source);

export function sourceControls(mode = 'pandascore') {
  if (!Object.hasOwn(modeNames, mode)) throw new Error('请选择有效的数据源模式。');
  return {
    mode, pandascore: mode !== 'hltv', hltv: mode !== 'pandascore', primary: mode === 'both',
  };
}

export function buildSourcePayload(draft = {}, { clearToken = false, clearProxy = false } = {}) {
  const values = Object.fromEntries(Object.entries(SOURCE_DEFAULTS).map(([key, value]) => [
    key, draft[key] ?? value,
  ]));
  sourceControls(values.data_source_mode);
  if (!Object.hasOwn(sourceNames, values.primary_source)) throw new Error('请选择有效的双源主源。');
  for (const [key, minimum, maximum] of [
    ['hltv_request_min_gap', 1, 3600], ['hltv_cache_ttl', 0, 86400], ['hltv_min_stars', 0, 5],
  ]) {
    const raw = values[key];
    const number = Number(raw);
    if (String(raw).trim() === '' || !Number.isInteger(number) || number < minimum || number > maximum)
      throw new Error('HLTV 请求间隔须为 1 至 3600 秒，缓存期限为 0 至 86400 秒，最低星级为 0 至 5；均须为整数。');
    values[key] = number;
  }
  for (const [key, clear] of [['pandascore_token', clearToken], ['hltv_proxy_url', clearProxy]]) {
    if (clear) values[key] = '';
    else if (String(draft[key] ?? '').trim()) values[key] = String(draft[key]).trim();
  }
  return values;
}

export function isSourceConfigured(response) {
  return response.source_configured === true;
}

export function sourceCheckRows(result, mode) {
  const controls = sourceControls(mode);
  return Object.entries(sourceNames).filter(([key]) => controls[key]).map(([key, label]) => {
    const check = result.sources?.[key];
    // Candidate checks can include ok; saved-source checks return router health.
    const ok = check?.enabled !== false && (typeof check?.ok === 'boolean'
      ? check.ok : ['ready', 'ok'].includes(check?.state));
    return { label, ok, message: check?.message || (ok ? '连接检查通过。' : '尚未取得检查结果，请重新检查。') };
  });
}

export function sourceChecksPassed(result, mode) {
  return result.source?.ok === true && sourceCheckRows(result, mode).every(row => row.ok);
}

export function matchPath(id) {
  return `/api/matches/${encodeURIComponent(String(id))}`;
}

export function sameSourceId(left, right) {
  return left != null && right != null && String(left) === String(right);
}

export function matchTierLabel(match = {}) {
  const pandascoreTier = match._pandascore_tier;
  if (pandascoreTier != null && String(pandascoreTier).trim()) {
    const tier = String(pandascoreTier).toUpperCase();
    return `${tier === 'UNRANKED' ? '未定级' : `${escapeHTML(tier)} 级`}（PandaScore）`;
  }
  const isHltv = match._source === 'hltv' || match._hltv_stars !== undefined ||
    String(match.id || '').startsWith('hltv:');
  if (isHltv) {
    const stars = match._hltv_stars ?? match._stars;
    return `HLTV ${stars == null || !String(stars).trim() ? '星级未知' : `${escapeHTML(stars)} 星`}`;
  }
  const tier = String(match.tournament?.tier || 'unranked').toUpperCase();
  return tier === 'UNRANKED' ? '未定级' : `${escapeHTML(tier)} 级`;
}

export function sourceStatusFromMatches(result, current = {}) {
  return result?.source_status && typeof result.source_status === 'object' && !Array.isArray(result.source_status)
    ? result.source_status : current;
}

function sourceTime(iso) {
  const date = new Date(iso);
  if (!iso || Number.isNaN(date.getTime())) return '未知时间';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

export function renderSourceBadges(record) {
  const actual = record._source || (String(record.id || '').startsWith('hltv:') ? 'hltv' : 'pandascore');
  const sources = [...new Set(record._sources?.length ? record._sources : [actual])];
  const badges = [
    `<span class="badge source-badge" title="${escapeHTML(`来源：${sources.map(sourceName).join(' + ')}`)}">${escapeHTML(`实际来源：${sourceName(actual)}`)}</span>`,
  ];
  if (sources.length > 1) badges.push(`<span class="badge">${escapeHTML(sources.map(sourceName).join(' + '))}</span>`);
  if (record._source_conflicts?.length) {
    const detail = record._source_conflicts.map(item => typeof item === 'string' ? item : JSON.stringify(item)).join('；');
    badges.push(`<span class="badge warning" title="${escapeHTML(detail)}">来源冲突 · 按实际来源显示</span>`);
  }
  if (record._stale === true) badges.push('<span class="badge warning">缓存数据 · 待刷新</span>');
  if (record._fetched_at) badges.push(`<span class="source-updated" title="${escapeHTML(record._fetched_at)}">更新 ${escapeHTML(sourceTime(record._fetched_at))}（北京时间）</span>`);
  return badges.join('');
}

export function renderFollowSourceBadges(team) {
  const source = team._source || (String(team.id || '').startsWith('hltv:') ? 'hltv' : 'pandascore');
  const active = [...new Set(team._active_sources || [])];
  const badges = team._source || team.id != null
    ? [`<span class="badge">${escapeHTML(`来源：${sourceName(source)}`)}</span>`] : [];
  if (active.length) badges.push(`<span class="badge">${escapeHTML(`生效来源：${active.map(sourceName).join(' + ')}`)}</span>`);
  if (team._source_link_pending === true) badges.push('<span class="badge warning" title="部分来源尚未关联；原关注记录已保留，未关联来源不会视为生效。">待关联</span>');
  return badges.join('');
}

export function renderSourceStatus(config, statuses = {}, configured = false) {
  const controls = sourceControls(config.data_source_mode);
  const primary = config.primary_source || 'pandascore';
  const summary = `${modeNames[controls.mode]}${controls.primary ? ` · 主源：${sourceName(primary)}` : ''}`;
  const stateNames = {
    ok: '正常', ready: '正常', healthy: '正常', error: '连接异常', blocked: '访问受阻',
    needs_verification: '需要人工验证', challenge: '需要人工验证',
    unconfigured: '未配置', not_configured: '未配置', unchecked: '尚未检查', idle: '尚未检查',
    unknown: '尚未检查', disabled: '未启用', verifying: '人工验证中',
    unavailable: '环境不可用', closed: '已关闭', backoff: '等待退避',
  };
  const rows = Object.entries(sourceNames).map(([key, label]) => {
    const status = statuses[key] || {};
    const enabled = controls[key] && status.enabled !== false;
    const health = enabled ? status.state || 'unchecked' : 'disabled';
    const healthy = enabled && ['ok', 'ready', 'healthy'].includes(health);
    const healthLabel = Object.hasOwn(stateNames, health) ? stateNames[health] : health;
    return `<div class="source-status-row"><div><strong>${label}</strong><span class="badge${healthy ? ' green' : enabled && !['unchecked', 'unknown', 'idle'].includes(health) ? ' warning' : ''}">${escapeHTML(healthLabel)}</span></div><small>${escapeHTML(enabled ? status.message || '尚未检查连接；配置完整不代表连接正常。' : '当前模式未启用此来源。')}</small>${status.last_success ? `<small title="${escapeHTML(status.last_success)}">最后成功：${escapeHTML(sourceTime(status.last_success))}（北京时间）</small>` : ''}</div>`;
  });
  return `<p class="source-summary">${escapeHTML(summary)} · ${configured ? '配置已填写' : '配置未完成'}<small>配置状态与连接健康状态分别显示；检查使用已保存的配置。</small></p>${rows.join('')}`;
}
